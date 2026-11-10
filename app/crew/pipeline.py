"""Orchestration: Data Steward -> Orchestrator (intent routing) -> Simulator/Analyst -> Advisory.

Without API keys (or on LLM errors) a deterministic offline mode runs that calls the same
tools via a rule parser – so the app stays demonstrable even without an LLM.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import pandas as pd

from app.core import clock
from app.core.business_rules import current_rules
from app.core.runtime_config import AGENT_ROLES, get_runtime_config
from app.core.security import mask_dataset_pii, mask_pii_data, register_pii_from_dataset
from app.crew.advisory_check import check_advisory
from app.crew.agents import DataStewardAgent
from app.crew.guard_rails import (
    NO_SIMULATION_MESSAGE,
    STRUCTURED,
    _ask_back,
    _strip_fences,
    apply_plan_guard_rails,
    size_mismatch,
)
from app.crew.paper import (
    _parser_error,
    _render_offline_paper,
    assemble_paper,
    forced_notice,
    notes_section_md,
    scenario_section_md,
    stream_advisory,
)
from app.crew.parser import (
    _ACCEL_KW,
    _CLUSTER_NAMES,
    _RESERVE_KW,
    _VALUE_CUT_KW,
    ASK_QUANTITY,
    ASK_QUANTITY_MESSAGE,
    CAPACITY_HINTS,
    DEFAULT_HOLD_MONTHS,
    SCOPE_HINTS,
    UNRESOLVED_PROJECT,
    _has,
    _parse_amount_eur,
    _parse_cost_type,
    _parse_date,
    _parse_fte,
    _parse_level,
    _parse_months,
    _parse_percent,
    _parse_phase,
    _parse_response,
    _parse_risk,
    _parse_skill,
    _parse_team,
    _signed,
    find_projects_in_text,
    heuristic_plan,
    is_change_request,
    is_unresolved_change_request,
    unresolved_project_message,
)
from app.crew.tasks import IntentPlan, SubIntent, portfolio_markdown
from app.crew.tools import (
    benefit_tools,
    graph_tools,
    merge_tools,
    pmo_tools,
    portfolio_tools,
    simulator_tools,
    skill_tools,
)
from app.crew.tools import context as ctx
from app.crew.tools.data_steward_tools import DataQualityReport
from app.crew.verdict import Verdict, enforce_verdict, lead_summary, verdict_from_context
from app.data_adapters.dataset import CLOSED_PROJECT_STATES, PortfolioDataset
from app.observability.tracer import trace_span

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    decision_paper: str
    mode: str  # "agents" | "offline" | "blocked" | "planning"
    steward_report: DataQualityReport | None = None
    intent_plan: IntentPlan | None = None
    proposed_changes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    verdict: Verdict | None = None  # deterministic, rule-based
    scenario_state: dict[str, Any] | None = None  # cumulative scenario for the next step (scenario memory)
    verdict_s: float | None = None  # seconds until the rule-based verdict was known (before the paper is written)
    evidence: str = ""  # tool results the paper is based on – the same in LLM and offline mode


# =========================================================================== Offline execution
def _run_offline(request: str, plan: IntentPlan, steward: DataQualityReport, role: str,
                 forced: list[str] | None = None) -> tuple[str, str]:
    """Executes the plan deterministically. Structured parameters (guided Steering Console) take precedence;
    otherwise values are parsed from free text. Returns (decision paper, evidence = all tool results)."""
    sim_results, graph_results = _execute_intents(request, plan.sub_intents)
    paper = _render_offline_paper(request, plan, sim_results, graph_results, steward, role, forced or [])
    return paper, "\n\n".join(dict.fromkeys(sim_results + graph_results))


# The LLM tool(s) an intent family stands for: a deterministic run registers them, so the Simulator agent cannot
# apply the same change a second time in this step
_FAMILY_TOOLS = {
    "cancel": ("simulate_project_cancellation",), "budget_transfer": ("simulate_budget_transfer",),
    "resource_transfer": ("simulate_resource_transfer",), "budget_change": ("simulate_budget_change",),
    "schedule": ("simulate_agile_vs_waterfall_shift", "simulate_timeline_shift"), "on_hold": ("simulate_on_hold",),
    "portfolio_cut": ("simulate_portfolio_cut",), "resume": ("simulate_resume",),
    "cost_stress": ("simulate_cost_stress",), "rebaseline": ("simulate_rebaseline",),
    "stage_gate": ("simulate_stage_gate",), "reserve_draw": ("simulate_reserve_draw",),
    "reserve_return": ("simulate_reserve_return",), "benefit_review": ("simulate_benefit_review",),
    "merge": ("simulate_merge",),
}


def _register_executed(kind: str, ids: list[str], params: dict[str, Any] | None = None) -> None:
    for tool in _FAMILY_TOOLS.get(kind, ()):
        simulator_tools.repeated_call(tool, ids[0] if ids else "")
    params = params or {}
    if kind == "risk_response" and params.get("risk_number"):  # these two tools are keyed by risk / skill
        ctx.first_call("simulate_risk_response", str(params["risk_number"]).strip().upper())
    if kind == "skill_transfer" and ids and params.get("skill"):
        ctx.first_call("simulate_skill_transfer", f"{ids[0]}|{params['skill']}")


def _stress_scope(request: str) -> str | None:
    """A portfolio or business unit the stress test is limited to, if the request names one."""
    projects = ctx.get_dataset().projects
    tl = request.lower()
    for column in ("portfolio", "business_unit"):
        if column in projects.columns:
            for value in sorted(set(projects[column].dropna().astype(str)), key=len, reverse=True):
                if value.lower() in tl:
                    return value
    return None


def _rebaseline_reason(request: str) -> str:
    m = re.search(r"(?:because|due to|reason:?|weil|wegen|grund:?|da)\s+(.+)", request, flags=re.IGNORECASE)
    return (m.group(1) if m else request).strip().rstrip(".")


def _rebaseline_end(pid: str, request: str) -> str | None:
    explicit = _parse_date(request)
    months = _parse_months(request)
    if explicit or months is None:
        return explicit
    try:
        end = pd.to_datetime(ctx.get_project(pid)["planned_end_date"]) + pd.DateOffset(months=abs(months))
    except (ctx.ProjectNotFound, KeyError, TypeError, ValueError):
        return None
    return end.date().isoformat()


def _execute_pmo(kind: str, request: str, params: dict[str, Any], ids: list[str]) -> str | None:
    """The PMO actions (resume, stress test, re-baseline, stage gate, reserve, skill transfer, risk response);
    None if `kind` is none of them."""
    needed = {"resume": 1, "rebaseline": 1, "stage_gate": 1, "reserve_draw": 1, "reserve_return": 1,
              "skill_transfer": 2, "benefit_review": 1, "merge": 2}.get(kind, 0)
    if len(ids) < needed:
        return _parser_error("INPUT ERROR: The request does not name the project" + ("s (source and target)." if needed == 2
                                                                                   else "."))
    if kind == "resume":
        fte = params["fte"] if "fte" in params else _parse_fte(request)
        return pmo_tools.resume(ids[0], params.get("restart_date") or _parse_date(request), fte)
    if kind == "cost_stress":
        pct = params.get("pct") or _parse_percent(request)
        if not pct:
            return _parser_error("INPUT ERROR: No cost increase in % recognised for the stress test.")
        cost_type = params.get("cost_type") or {"total": "all"}.get(_parse_cost_type(request), _parse_cost_type(request))
        scope = params["scope"] if "scope" in params else _stress_scope(request)
        return pmo_tools.cost_stress(abs(float(pct)), cost_type, scope)
    if kind == "rebaseline":
        budget = params["new_budget_eur"] if "new_budget_eur" in params else _parse_amount_eur(request)
        end = params["new_end_date"] if "new_end_date" in params else _rebaseline_end(ids[0], request)
        return pmo_tools.rebaseline(ids[0], budget, end, params.get("reason") or _rebaseline_reason(request))
    if kind == "stage_gate":
        return pmo_tools.advance_phase(ids[0], params.get("target_phase") or _parse_phase(request))
    if kind == "benefit_review":
        return benefit_tools.benefit_review(ids[0])
    if kind == "merge":
        return merge_tools.merge_projects(ids[0], ids[1])
    if kind in ("reserve_draw", "reserve_return"):
        amount = params.get("amount") or _parse_amount_eur(request)
        if not amount:
            return _parser_error("INPUT ERROR: No amount (EUR) recognised for the management reserve.")
        book = pmo_tools.reserve_draw if kind == "reserve_draw" else pmo_tools.reserve_return
        return book(ids[0], float(amount), params.get("reason") or request)
    if kind == "skill_transfer":
        skill = params.get("skill") or _parse_skill(request)
        fte = params["fte"] if "fte" in params else (_parse_fte(request) or 0)
        months = params.get("months") or abs(_parse_months(request) or 3)
        return skill_tools.skill_transfer(skill or "", fte, ids[0], ids[1], int(months))
    if kind == "risk_response":
        risk = params.get("risk_number") or _parse_risk(request)
        if not risk:
            return _parser_error("INPUT ERROR: No risk (RSK-...) recognised for the risk response.")
        response = params.get("response") or _parse_response(request) or ""
        cost = params["cost_eur"] if "cost_eur" in params else (_parse_amount_eur(request) or 0.0)
        funding = params.get("funding") or ("reserve" if _has(request.lower(), _RESERVE_KW) else "project")
        return pmo_tools.risk_response(risk, response, cost, params.get("new_probability") or _parse_level(request),
                                       funding)
    return None


def _execute_intents(request: str, intents: list[SubIntent]) -> tuple[list[str], list[str]]:
    """Runs the tools for the given intents deterministically. Returns (simulation results, graph results)."""
    sim_results: list[str] = []
    graph_results: list[str] = []
    tl = request.lower()

    for intent in intents:
        params = intent.parameters or {}
        kind = params.get("action") or intent.description.split(":", 1)[0]
        ids = intent.project_ids
        if kind == "risk_response" and "risk_number" not in params:  # free text: the risk is read from the request
            params = {**params, "risk_number": _parse_risk(request)}
        if kind == "skill_transfer" and "skill" not in params:
            params = {**params, "skill": _parse_skill(request)}
        _register_executed(kind, ids, params)
        pmo = _execute_pmo(kind, request, params, ids)
        if pmo is not None:
            sim_results.append(pmo)
        elif kind == UNRESOLVED_PROJECT:
            sim_results.append(_parser_error(unresolved_project_message(request, ctx.get_dataset().projects)))
        elif kind == ASK_QUANTITY:
            sim_results.append(_parser_error(ASK_QUANTITY_MESSAGE))
        elif kind == "cancel":
            sim_results.append(simulator_tools.project_cancellation(ids[0]))
        elif kind == "budget_transfer":
            amount = params.get("amount") or _parse_amount_eur(request)
            if not amount:  # "Stop X and move the budget to Y": use the remaining free budget
                try:
                    src = ctx.get_project(ids[0])
                    amount = max(float(src["budget_cost_eur"]) - float(src["actual_cost_eur"] or 0), 0)
                    if str(src.get("state")) in CLOSED_PROJECT_STATES:  # a stopped project pays its exit costs first
                        amount *= 1 - current_rules().exit_cost_rate_pct / 100
                except (ctx.ProjectNotFound, TypeError, ValueError) as exc:
                    sim_results.append(_parser_error(f"INPUT ERROR: Free budget cannot be determined: {exc}"))
                    continue
            cost_type = params.get("cost_type") or _parse_cost_type(request)
            sim_results.append(simulator_tools.budget_transfer(ids[0], ids[1], amount, cost_type))
        elif kind == "resource_transfer":
            fte = params["fte"] if "fte" in params else (_parse_fte(request) or 0)
            team = params["team"] if "team" in params else _parse_team(request)
            sim_results.append(simulator_tools.resource_transfer(ids[0], ids[1], fte, team))
        elif kind == "schedule":
            if {"capacity_change_pct", "scope_change_pct", "months_delta"} & params.keys():
                cap_pct = float(params.get("capacity_change_pct", 0))
                scope_pct = float(params.get("scope_change_pct", 0))
                months = float(params.get("months_delta", 0))
            else:
                cap = _parse_percent(request, CAPACITY_HINTS)
                scope = _parse_percent(request, SCOPE_HINTS)
                months = abs(_parse_months(request) or 0)
                if _has(tl, _ACCEL_KW):
                    months = -months
                cap_pct = _signed(cap, request) if cap is not None else 0.0
                reduce_scope = _has(tl, ("reduzier", "kürz", "kuerz", "reduce", "cut", "shrink"))
                scope_pct = -abs(scope) if scope is not None and reduce_scope else abs(scope or 0)
            sim_results.append(simulator_tools.agile_vs_waterfall_shift(
                ids[0], capacity_change_pct=cap_pct, scope_change_pct=scope_pct, months_delta=months))
        elif kind == "budget_change":
            if "delta" in params:
                value, is_perc = float(params["delta"]), bool(params.get("is_perc", False))
                cost_type = params.get("cost_type", "total")
            else:
                cost_type = _parse_cost_type(request)
                perc = _parse_percent(request)
                value, is_perc = (perc, True) if perc is not None else (_parse_amount_eur(request), False)
                if value is None:
                    sim_results.append(_parser_error("INPUT ERROR: No amount or percentage recognised for the budget change."))
                    continue
                value = _signed(value, request)
            sim_results.append(simulator_tools.budget_change(ids[0], value, is_perc, cost_type))
        elif kind == "unstructured":
            sim_results.append(simulator_tools.unstructured_scenario(ids[0], params.get("scenario_desc") or request))
        elif kind == "on_hold":
            months = params.get("months") or abs(_parse_months(request) or DEFAULT_HOLD_MONTHS)
            retention = params.get("retention_pct")
            if retention is None:
                retention = _parse_percent(request, ("keep", "retain", "behalt", "standby", "capacity", "kapazit"))
            sim_results.append(portfolio_tools.on_hold(ids[0], months, retention))
        elif kind == "portfolio_cut":
            target = params.get("target_eur") or _parse_amount_eur(request)
            if not target:
                sim_results.append(_parser_error("INPUT ERROR: No savings target (amount in EUR) recognised for the portfolio cut."))
                continue
            method = params.get("method") or ("value" if _has(request.lower(), _VALUE_CUT_KW) else "ranked")
            sim_results.append(portfolio_tools.portfolio_cut(target, method))
        elif kind == "downstream/upstream":
            for pid in ids:
                graph_results.append(graph_tools.downstream_impact(pid))
                graph_results.append(graph_tools.upstream_bottlenecks(pid))
            if len(ids) >= 2:
                graph_results.append(graph_tools.shared_resources(ids[0], ids[1]))
        elif kind == "cut_dependents":
            pass  # the portfolio_cut tool checks the dependents of every stopped project itself (deterministic)
        elif kind == "task_impact":
            graph_results.append(graph_tools.task_level_impact(ids[0]))
        elif kind == "team_bottlenecks":
            skill_lines = skill_tools.skill_bottleneck_lines()
            graph_results.append(graph_tools.team_bottlenecks() + ("\n" + "\n".join(skill_lines) if skill_lines else ""))
        elif kind == "skill_capacity":
            graph_results.append(skill_tools.skill_capacity(params.get("skill") or _parse_skill(request) or None))
        elif kind == "cluster_risk":
            keyword = params.get("keyword")
            if keyword is None:
                keyword = next((c for c, kws in _CLUSTER_NAMES.items() if any(k in tl for k in kws)), "")
            graph_results.append(graph_tools.cluster_risk(keyword))
    return sim_results, graph_results


def _check_dependents(ledger_before: int, checked: set[str]) -> set[str]:
    """Dependency check (dependency graph) for every project changed in this step since `ledger_before` and not yet in
    `checked`. Records the domino fact the verdict engine needs. Returns the checked projects."""
    changed = [str(c["project_id"]) for c in ctx.get_ledger()[ledger_before:]]
    for pid in dict.fromkeys(changed):
        if pid not in checked:
            graph_tools.downstream_impact(pid)
            checked.add(pid)
    return checked


def scenario_memory_md(turns: list[str], ledger: list[dict[str, Any]]) -> str:
    """Conversation memory for the agents: earlier steps of this scenario and the changes already applied to the
    working copy. The next step must build on them."""
    changes = "\n".join(f"- {c['project_id']} {c['field']}: {c.get('old')} -> {c.get('new')} ({c.get('source_tool')})"
                        for c in ledger) or "- (none)"
    steps = "\n".join(f"{i}. {t}" for i, t in enumerate(turns, start=1))
    return (f"SCENARIO SO FAR (earlier steps of this conversation – the table below already contains their effect; "
            f"build on it, do not start from scratch):\n{steps}\nChanges already in the scenario:\n{changes}")


# =========================================================================== Agent execution
def _run_agents(
    request: str, portfolio_md: str, steward: DataQualityReport | str, role: str, projects: pd.DataFrame,
    plan_override: IntentPlan | None = None,
    variant: str = "main",
    forced: list[str] | None = None,
    on_verdict=None,
    on_paper=None,
) -> tuple[str, IntentPlan, list[str], str]:
    """Returns (decision paper, plan, warnings, evidence = tool results of this step + specialist assessments).
    `steward` may be the report or its (already annotated) markdown. `variant="shadow"` builds every agent with
    its per-agent shadow model (agents without one reuse their main model).
    `on_verdict(verdict)` is called once, before the Advisory writes; with `on_paper(text_so_far)` the Advisory's text
    is streamed (masked, complete lines) – without it the Advisory runs as an agent task."""
    from app.crew import agents as ag
    from app.crew import tasks as tk
    from app.llm.agent import Crew

    warnings: list[str] = []
    guard_notes: list[str] = []
    steward_md = steward if isinstance(steward, str) else steward.to_markdown(max_rows=20)
    simulations_before = len(ctx.get_tool_results("simulation"))
    graphs_before = len(ctx.get_tool_results("graph"))
    ledger_before = len(ctx.get_ledger())
    specialist_plan = plan_override
    guided_done: list[str] = []

    # Phase 1: orchestrator splits intents – skipped for guided scenarios (the plan is already exact)
    if plan_override is not None:
        plan = plan_override
        # The builder's values are exact: its simulations run here, deterministically, like a certain parser intent.
        # The Simulator still runs and assesses the results, without tools, so it cannot re-type the values.
        guided = [i for i in plan.sub_intents if i.intent_type == "simulation"]
        if guided:
            guided_done, _ = _execute_intents(request, guided)
    else:
        with trace_span("orchestrator.intent_routing", variant=variant):
            try:
                orchestrator = ag.build_orchestrator_agent(variant)
                orch_task = tk.create_orchestration_task(orchestrator, request, portfolio_md)
                out = Crew(agents=[orchestrator], tasks=[orch_task]).kickoff()
                plan = out.pydantic if isinstance(getattr(out, "pydantic", None), IntentPlan) else IntentPlan.model_validate_json(out.raw)
            except Exception as exc:
                logger.exception("Orchestrator failed – falling back to rule-based parser")
                warnings.append(f"Orchestrator fallback to rule-based parser ({exc.__class__.__name__}).")
                plan = heuristic_plan(request, projects)
        # Same rule as the offline parser, independent of the LLM plan: a change request without a resolvable
        # project is answered with a request to restate it – never as an analysis, never with a guessed project
        if is_unresolved_change_request(request, projects) and not _has_resolvable_simulation(plan):
            _parser_error(unresolved_project_message(request, projects))
            plan = plan.model_copy(update={
                "sub_intents": [i for i in plan.sub_intents if i.intent_type != "simulation"],
                "requires_simulation": False})
        # Deterministic guardrails: certain parser intents run here, vague amounts are asked back, quality gate
        specialist_plan, plan, guard_notes = apply_plan_guard_rails(request, plan, projects, _execute_intents)
        warnings.extend(guard_notes)
        # The code ran the orchestrator's structured plan: the Simulator still assesses those results (same agents)
        if not specialist_plan.requires_simulation and any(
                (i.parameters or {}).get("source") == STRUCTURED for i in plan.sub_intents):
            guided_done = ctx.get_tool_results("simulation")[simulations_before:]
            if guided_done:
                specialist_plan = specialist_plan.model_copy(update={"requires_simulation": True})

    ledger_after_rails = len(ctx.get_ledger())  # changes after this point come from the Simulator agent
    # Every project changed so far gets its dependency check here, deterministically – not only when a model plans
    # a graph analysis or the Analyst calls the tool
    checked = _check_dependents(ledger_before, set())

    # Phase 2: only the specialists that are needed
    agents_used, specialist_tasks = [], []
    if specialist_plan.requires_simulation and guided_done:
        sim = ag.build_simulator_agent(variant, assess_only=True)
        agents_used.append(sim)
        specialist_tasks.append(tk.create_assessment_task(sim, request, guided_done))
    elif specialist_plan.requires_simulation:
        sim = ag.build_simulator_agent(variant)
        agents_used.append(sim)
        done = [i.description for i in plan.sub_intents if i not in specialist_plan.sub_intents]
        sim_request = request + (f"\n(Already simulated deterministically – do not repeat: {'; '.join(done)})"
                                 if done else "")
        specialist_tasks.append(tk.create_simulation_task(sim, sim_request, specialist_plan, portfolio_md))
    if specialist_plan.requires_graph_analysis:
        analyst = ag.build_analyst_agent(variant)
        agents_used.append(analyst)
        analyst_request = request + (f"\n(Downstream impact already checked deterministically – do not repeat: "
                                     f"{', '.join(sorted(checked))})" if checked else "")
        specialist_tasks.append(tk.create_analysis_task(analyst, analyst_request, specialist_plan, portfolio_md))

    specialist_outputs: list[str] = []
    if specialist_tasks:
        with trace_span("crew.specialists", variant=variant, simulation=plan.requires_simulation,
                        graph=plan.requires_graph_analysis):
            result = Crew(agents=agents_used, tasks=specialist_tasks).kickoff()
        specialist_outputs = [getattr(t, "raw", "") for t in (getattr(result, "tasks_output", None) or [])]
    _check_dependents(ledger_before, checked)  # projects the Simulator agent changed
    # The Simulator agent's arguments against the request: a wrong unit or sign is asked back, never decided
    if specialist_plan.requires_simulation and not guided_done:
        mismatch = size_mismatch(request, ctx.get_ledger()[ledger_after_rails:])
        if mismatch:
            _ask_back(mismatch)
    # A change request never ends without a decision: without a simulation result in this step, ask to restate it
    if is_change_request(request) and len(ctx.get_tool_results("simulation")) == simulations_before:
        _parser_error(NO_SIMULATION_MESSAGE)
    # The Advisory gets the tool results verbatim from the context – the specialists only add a short assessment
    sim_results = ctx.get_tool_results("simulation")[simulations_before:]
    graph_results = ctx.get_tool_results("graph")[graphs_before:]
    assessment = "\n\n".join(o for o in specialist_outputs if o)
    evidence = "\n\n".join(e for e in ("\n\n".join(dict.fromkeys(sim_results + graph_results)),
                                         f"Specialist assessment:\n{assessment}" if assessment else "") if e)

    # Phase 3: the verdict is computed deterministically from what the TOOLS recorded – not from the
    # specialists' prose – and handed to the Advisory as final. The Advisory only explains it.
    verdict = verdict_from_context(forced)
    if on_verdict is not None:
        on_verdict(verdict)
    paper = None
    with trace_span("crew.advisory", variant=variant, verdict=verdict.label, streamed=on_paper is not None):
        if on_paper is not None:
            try:
                description = tk.advisory_task_description(request, plan, evidence, steward_md, role, verdict)
                paper = stream_advisory(description, variant, on_paper)
            except Exception as exc:  # noqa: BLE001 - the verdict and changes are final; only the text is retried
                logger.warning("Advisory streaming failed (%s) – writing the paper without streaming",
                               exc.__class__.__name__)
                on_paper("")
                paper = None
        if paper is None:
            advisory = ag.build_advisory_agent(variant)
            advisory_task = tk.create_advisory_task(advisory, request, plan, evidence, steward_md, role, verdict)
            paper = Crew(agents=[advisory], tasks=[advisory_task]).kickoff().raw
    # Every figure and verdict word of the AI text against what the agents were given – reported, never rewritten
    findings = check_advisory(_strip_fences(paper), "\n".join(
        (request, portfolio_md, evidence, steward_md, verdict.to_markdown(), str(verdict.rules))), verdict)
    if findings:
        logger.warning("Advisory check: %d finding(s)", len(findings))
        warnings.extend(findings)
    paper = assemble_paper(_strip_fences(paper), scenario_section_md(ctx.get_ledger(), sim_results),
                           notes_section_md(steward_md, sim_results, graph_results, guard_notes))
    return paper, plan, warnings, evidence


def _has_resolvable_simulation(plan: IntentPlan) -> bool:
    """True if a simulation intent of the plan names a project the scenario context can resolve."""
    for intent in plan.sub_intents:
        if intent.intent_type != "simulation":
            continue
        for ref in intent.project_ids:
            try:
                ctx.resolve_project_id(ref)
                return True
            except ctx.ProjectNotFound:
                continue
    return False


# =========================================================================== Public API
def _run_planning(request: str, dataset: PortfolioDataset, role: str, warnings: list[str], started: float,
                  as_of: date, on_verdict) -> PipelineResult:
    """A planning question: the scenario engine answers on the whole portfolio – only for the roles that plan it."""
    from app.planning import chat as planning_chat
    from app.planning.scenario import RANK, VALUE, run_scenario

    if role not in planning_chat.PLANNING_ROLES:
        return PipelineResult(decision_paper="## Planning scenario\n\nPlanning scenarios cover the whole portfolio – "
                                             "the PMO calculates them. Ask the PMO, or open the demands and the plan "
                                             "analysis on the Planning page.", mode="planning", warnings=warnings,
                              duration_s=time.perf_counter() - started)
    with trace_span("planning_scenario"):
        spec = planning_chat.parse_question(request, dataset, as_of)
        result = run_scenario(dataset, spec, as_of)
        rank_result = run_scenario(dataset, replace(spec, method=RANK), as_of) if spec.method == VALUE else None
    verdict_s = time.perf_counter() - started
    if on_verdict is not None:
        try:
            on_verdict({"verdict": result.verdict, "proposed_changes": [], "mode": "planning"})
        except Exception:  # a display problem never stops the answer
            logger.exception("Early verdict display failed")
    paper = planning_chat.paper(result, spec, rank_result) + "\n\n" + result.verdict.to_markdown()
    return PipelineResult(decision_paper=mask_pii_data(paper), mode="planning", warnings=warnings,
                          duration_s=time.perf_counter() - started, verdict=result.verdict, verdict_s=verdict_s,
                          evidence=f"planning scenario {spec}")


def run_steering_pipeline(
    user_prompt: str,
    dataset: PortfolioDataset,
    role: str,
    user_id: str | None,
    pii_source: PortfolioDataset | None = None,
    plan: IntentPlan | None = None,
    force: bool = False,
    base_state: dict[str, Any] | None = None,
    on_verdict=None,
    on_paper=None,
    as_of: date | None = None,
    use_llm: bool = True,
) -> PipelineResult:
    """UI entry point. `dataset` must already be RLS-filtered.
    `plan`: exact plan from the guided Steering Console (skips intent parsing / orchestrator).
    `force`: PMO-only "Force Simulation" – the Data Steward still reports all findings, but blocked projects
    are passed to the Simulator and Analyst anyway (value guards for missing numbers remain active).
    `base_state`: scenario memory – the cumulative state of the previous step; this request builds on
    the already manipulated data and the result covers the whole scenario (changes, verdict).
    `on_verdict(preview)`: called once as soon as the rule-based verdict is known – before the decision paper is written
    – with {"verdict", "proposed_changes", "mode"}; the UI shows the result card early. `on_paper(text_so_far)`: the
    LLM paper is streamed (masked, complete lines). Without the callbacks the result is the same, just at the end.
    `as_of`: reference date of every date-dependent calculation (default today); it is stored with the verdict, so
    the same request with the same `as_of` gives the same verdict on any system day. `use_llm=False`: the rule-based
    path only (the quick calculation of the question boxes) – same verdict, no LLM call, no shadow run."""
    from app.core.security import ROLE_PMO

    guided_plan = plan
    as_of = as_of or clock.today()
    started = time.perf_counter()
    cfg = get_runtime_config()  # Admin Console settings (defaults from the env config)
    warnings: list[str] = []
    if force and role != ROLE_PMO:
        warnings.append("'Force simulation' is reserved for the PMO role – request executed with the quality gate active.")
        force = False

    with trace_span("steering_pipeline", role=role, llm_available=cfg.llm_available, pii_masking=cfg.pii_masking_enabled,
                    forced=force):
        # 0) Register PII names from the full dataset (names of other users are masked too)
        register_pii_from_dataset(pii_source if pii_source is not None else dataset)
        request = mask_pii_data(user_prompt.strip())

        # 0b) A question about the annual plan is answered by the planning engine (deterministic, no LLM)
        if guided_plan is None and not base_state:
            from app.planning import chat as planning_chat

            if planning_chat.is_planning_question(request, dataset, find_projects_in_text(request, dataset.projects),
                                                  as_of):
                return _run_planning(request, dataset, role, warnings, started, as_of, on_verdict)

        # 1) Data Steward – deterministic, BEFORE the agents run
        with trace_span("data_steward"):
            steward = DataStewardAgent().run(dataset, as_of)
        if not steward.can_proceed and not (force and dataset is not None and not dataset.projects.empty):
            return PipelineResult(
                decision_paper="## ⛔ Analysis blocked\n\nThe Data Steward did not release the data.\n\n" + steward.to_markdown(),
                mode="blocked",
                steward_report=steward,
                warnings=warnings,
                duration_s=time.perf_counter() - started,
            )

        # 2) Scenario context: only PII-masked data reaches tools/LLMs; quality gate for blocked projects
        forced = sorted(steward.blocked_reasons) if force else []
        blocked = {} if force else steward.blocked_reasons
        if forced:
            logger.warning("Forced simulation by %s (%s) overriding quality gate for %s", user_id, role, ", ".join(forced))
            warnings.append(forced_notice(forced))
        safe = mask_dataset_pii(dataset)
        ctx.set_portfolio_context(safe, blocked, base_state=base_state, as_of=as_of)
        turns = list((base_state or {}).get("turns") or [])
        # Bounded LLM context – named projects (guided plan or free text) always stay in the table
        focus = [pid for sub in (guided_plan.sub_intents if guided_plan else []) for pid in sub.project_ids]
        focus += find_projects_in_text(request, safe.projects)
        focus += [str(c.get("project_id")) for c in (base_state or {}).get("changes") or []]
        # The table shows the CURRENT scenario values (including earlier steps) and the scenario history
        portfolio_md = portfolio_markdown(ctx.get_scenario_df(), steward.project_flags(), focus_ids=focus)
        if turns:
            portfolio_md = scenario_memory_md(turns, ctx.get_ledger()) + "\n\n" + portfolio_md
            warnings.append(f"Step {len(turns) + 1} of a what-if – builds on: " + " → ".join(turns[-3:]))
        steward_md = ((forced_notice(forced) + "\n\n") if forced else "") + steward.to_markdown(max_rows=20)
        if not cfg.pii_masking_enabled:
            warnings.append("PII masking is disabled (Admin Console) – real names may be sent to LLM providers.")

        # Verdict first: the UI may show the rule-based result before the paper is written. Called at most once.
        early: dict[str, Any] = {}

        def notify_verdict(verdict: Verdict, mode: str) -> None:
            if early:
                return
            early.update(label=verdict.label, at=time.perf_counter() - started)
            if on_verdict is None:
                return
            try:
                on_verdict({"verdict": verdict, "proposed_changes": ctx.get_ledger(), "mode": mode})
            except Exception:  # a display problem never stops the analysis
                logger.exception("Early verdict display failed")

        # 3) LLM agents or offline fallback
        if cfg.llm_available and use_llm:
            try:
                paper, plan, crew_warnings, evidence = _run_agents(
                    request, portfolio_md, steward_md, role, safe.projects, guided_plan, forced=forced,
                    on_verdict=lambda v: notify_verdict(v, "agents"), on_paper=on_paper)
                warnings.extend(crew_warnings)
                mode, primary_models = "agents", {a: cfg.model_for_role(a) for a in AGENT_ROLES}
            except Exception as exc:
                logger.exception("LLM pipeline failed – offline fallback")
                # the exception text is for the System Admin (system log); readers see that the offline mode answered
                warnings.append(f"LLM pipeline failed ({exc.__class__.__name__}) – offline fallback active.")
                ctx.set_portfolio_context(safe, blocked, base_state=base_state, as_of=as_of)  # reset to the last step
                plan = guided_plan or heuristic_plan(request, safe.projects)
                (paper, evidence), mode = _run_offline(request, plan, steward, role, forced), "offline"
                primary_models = {"all": "rule-based (offline)"}
        else:
            missing = cfg.missing_keys_for_models() if use_llm else []
            if missing:  # key names are configuration detail: system log (System Admin), not the business report
                logger.info("Offline mode: missing keys %s", ", ".join(missing))
                warnings.append("Offline mode – the deterministic rule-based parser answered.")
            plan = guided_plan or heuristic_plan(request, safe.projects)
            (paper, evidence), mode = _run_offline(request, plan, steward, role, forced), "offline"
            primary_models = {"all": "rule-based (offline)"}

        # 3b) The rule-based verdict is authoritative in every mode – the recommendation line of the paper
        # is forced to it and the verdict basis is appended (an LLM can explain, never decide)
        verdict = verdict_from_context(forced)
        notify_verdict(verdict, mode)  # offline mode: the paper is already written – the card comes now
        if early.get("label") != verdict.label:  # cannot happen by design: the verdict is computed from the same facts
            logger.error("Early verdict differed from the final verdict – the final verdict applies")
        paper, deviated = enforce_verdict(paper, verdict)
        paper = lead_summary(paper, verdict)
        if deviated and mode == "agents":
            logger.warning("Advisory recommendation deviated from the rule-based verdict – corrected to %s", verdict.label)
            warnings.append(f"The AI-written recommendation deviated from the rule-based verdict and was corrected to "
                            f"'{verdict.label}'.")
        duration = time.perf_counter() - started
        paper = mask_pii_data(paper)
        # 4) Shadow mode: the same crew with per-agent shadow models in the background – logging only, no effect
        if cfg.shadow_available and use_llm:
            from app.crew.shadow import start_shadow_run

            start_shadow_run(
                request=request, safe_dataset=safe, blocked=blocked, portfolio_md=portfolio_md, steward_md=steward_md,
                role=role, plan_override=guided_plan,
                primary={"models": primary_models, "duration_s": round(duration, 2), "result": paper},
                dq_summary=steward, forced=forced, base_state=base_state, as_of=as_of,
            )

        return PipelineResult(
            decision_paper=paper,
            mode=mode,
            steward_report=steward,
            intent_plan=plan,
            proposed_changes=ctx.get_ledger(),
            warnings=warnings,
            duration_s=duration,
            verdict=verdict,
            scenario_state=ctx.export_state(turns + [request]),
            verdict_s=early["at"] if early else None,
            evidence=mask_pii_data(evidence),
        )
