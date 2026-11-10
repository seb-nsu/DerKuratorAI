"""Task definitions for the agent pipeline.

Note: tool outputs in descriptions carry no curly braces (replaced by parentheses) – the prompts stay the same as
they were evaluated with.
"""
from __future__ import annotations

from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from app.crew.verdict import condition_owner_text

# What the Advisory writes itself: the explanation. The figures (section 3) and the data-quality notes (section 7) are
# rendered by the code from the tool results (app/crew/pipeline.py) and inserted into the final paper.
ADVISORY_SECTIONS_TEMPLATE = """\
# Executive Decision Paper
**Request:** <request in one sentence> | **Date:** <today> | **Recommendation:** <the FINAL VERDICT exactly as given>

## 1. Management Summary
<3–5 bullet points with the key figures>

## 2. Current Situation
<short description of the affected projects with budget, FTE, status>

## 4. Dependencies & Systemic Risks
<domino effects, bottlenecks, concentration risks from the graph analysis>

## 5. Recommendation & Conditions
<why the rule engine reached this verdict; every given condition with owner and date>

## 6. Next Steps
<numbered list with owner (role) and date>
"""


STRUCTURED_ACTION = Literal["budget_change", "budget_transfer", "cancel", "on_hold", "schedule", ""]


class SubIntent(BaseModel):
    intent_type: Literal["simulation", "graph_analysis", "general"] = Field(
        description="simulation = budget/FTE/schedule/stop calculation; graph_analysis = dependencies/risks"
    )
    description: str = Field(description="Precise description of the sub-task incl. parameters")
    project_ids: list[str] = Field(default_factory=list, description="Affected project_ids, e.g. PRJ-DEMO-1001")
    # The structured plan: action and values as typed fields. The code executes a complete one deterministically –
    # but only with values the request itself states (sign and unit read from the request, not from the model).
    action: STRUCTURED_ACTION = Field(
        default="", description="For a simulation: budget_change, budget_transfer, cancel, on_hold or schedule; "
                                "empty for anything else")
    amount_eur: float | None = Field(default=None, description="Amount in EUR exactly as the request states it")
    percent: float | None = Field(default=None, description="Percentage exactly as the request states it")
    months: float | None = Field(default=None, description="Number of months exactly as the request states it")
    cost_type: Literal["capex", "opex", "total", ""] = Field(default="", description="Budget part the request names")
    # Structured tool parameters from the guided Steering Console (e.g. {"action": "budget_change",
    # "delta": -10, "is_perc": True, "cost_type": "capex"}). Deliberately NOT in the LLM JSON schema: free-form dicts
    # break strict structured output; the orchestrator therefore never fills them itself.
    parameters: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)


class IntentPlan(BaseModel):
    summary: str = Field(description="One-sentence summary of the request")
    sub_intents: list[SubIntent] = Field(default_factory=list)
    requires_simulation: bool = False
    requires_graph_analysis: bool = False


_CONTEXT_COLUMNS = {
    "project_number": "ID",
    "project_name": "Project",
    "status": "RAG",
    "execution_type": "Method",
    "percent_complete": "Progress %",
    "budget_cost_eur": "Budget",
    "actual_cost_eur": "Actual",
    "estimate_at_completion_eur": "EAC",
    "planned_capital_eur": "CapEx",
    "planned_operating_eur": "OpEx",
    "planned_roi_pct": "ROI %",
    "planned_end_date": "Planned end",
    "current_fte": "FTE",
    "peak_utilization_pct": "Peak utilisation %",
    "dq_flag": "Data quality",
}
_MONEY = {"budget_cost_eur", "actual_cost_eur", "estimate_at_completion_eur", "planned_capital_eur", "planned_operating_eur"}


PORTFOLIO_CONTEXT_MAX_ROWS = 25  # ~65 tokens per row; embedded in up to 3 task prompts (plus the shadow crew)


def _context_rows(view: pd.DataFrame, focus_ids: list[str] | None, max_rows: int) -> pd.DataFrame:
    """Projects named in the request/plan always stay; the remaining slots go to the most relevant
    projects – red status first, then highest risk score, then largest budget."""
    pids = view["project_number"].astype(str)
    in_focus = pids.isin({str(p) for p in focus_ids or []})
    rest = view[~in_focus].assign(
        _red=view["status"].eq("Red") if "status" in view.columns else False,
        _risk=pd.to_numeric(view["risk_score"], errors="coerce") if "risk_score" in view.columns else 0,
        _budget=pd.to_numeric(view["budget_cost_eur"], errors="coerce") if "budget_cost_eur" in view.columns else 0,
    ).sort_values(["_red", "_risk", "_budget"], ascending=False, na_position="last")
    slots = max(max_rows - int(in_focus.sum()), 0)
    return pd.concat([view[in_focus], rest.head(slots)[view.columns]])


def portfolio_markdown(view: pd.DataFrame, dq_flags: dict[str, str] | None = None, focus_ids: list[str] | None = None,
                       max_rows: int = PORTFOLIO_CONTEXT_MAX_ROWS) -> str:
    """Compact, PII-free portfolio table (project view incl. aggregates) for the LLM context.
    Large portfolios are capped to `max_rows` (named projects + top projects by risk/budget) to bound token usage."""
    if view is None or view.empty:
        return "(no projects within your permissions)"
    total = len(view)
    view = _context_rows(view, focus_ids, max_rows) if total > max_rows else view.copy()
    view["dq_flag"] = view["project_number"].astype(str).map(dq_flags or {}).fillna("ok")
    cols = [c for c in _CONTEXT_COLUMNS if c in view.columns]
    lines = ["| " + " | ".join(_CONTEXT_COLUMNS[c] for c in cols) + " |", "|" + "---|" * len(cols)]
    for row in view[cols].to_dict(orient="records"):
        cells = []
        for c in cols:
            v = row[c]
            if v is None or (not isinstance(v, str) and pd.isna(v)):
                v = "–"
            elif c in _MONEY:
                v = f"€{float(v):,.0f}"
            elif c == "planned_end_date":
                v = pd.to_datetime(v).date().isoformat()
            elif isinstance(v, float):
                v = f"{v:g}"
            cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    if total > len(view):
        lines.append(f"\n(Context limited to {len(view)} of {total} projects: the projects named in the request plus the "
                     "most critical ones by RAG status, risk and budget. Other projects exist but are not listed – "
                     "do not assume they are unaffected.)")
    return "\n".join(lines)


def _plan_text(plan: IntentPlan, intent_type: str) -> str:
    items = [i for i in plan.sub_intents if i.intent_type == intent_type]
    if not items:
        return "- (no explicit sub-intents – derive them from the request)"

    def params(i: SubIntent) -> str:
        shown = {k: v for k, v in i.parameters.items() if k != "action"}
        return f" – exact tool parameters: {_no_braces(str(shown))}" if shown else ""

    return "\n".join(f"- {i.description} (projects: {', '.join(i.project_ids) or 'n/a'}){params(i)}" for i in items)


def create_orchestration_task(agent, request: str, portfolio_md: str):
    from app.llm.agent import Task

    return Task(
        description=(
            f"Management steering request:\n\"{request}\"\n\n"
            f"Portfolio within the user's permissions:\n{portfolio_md}\n\n"
            "Split the request into sub-intents. Assign each intent the type simulation, graph_analysis or general "
            "and map project names to the project numbers in the table. Describe parameters explicitly: amounts in "
            "EUR or percent, cost_type (capex/opex/total), FTE and team, capacity/scope change in %, months. "
            "Set requires_graph_analysis to true whenever projects are stopped, delayed or resources are moved "
            "(check project and task domino, team bottlenecks). Pausing a project is a simulation intent 'on hold' "
            "(not a stop). A savings target across the portfolio without named projects is ONE simulation intent "
            "'portfolio cut' with no project IDs. PMO actions are simulation intents of their own: 'resume' (after a hold), "
            "'cost stress' (portfolio-wide cost increase in %, no project IDs), 're-baseline' (new budget and/or end "
            "date with a reason), 'stage gate' (next phase), 'reserve draw' / 'reserve return' (management reserve), "
            "'skill transfer' (FTE of one skill between two projects) and 'risk response' (risk RSK-..., Mitigate/"
            "Accept/Avoid/Transfer). Plan the requested simulation for projects with data quality "
            "'⛔ blocked' as well – the simulation tool reports the quality-gate block. Never invent an amount, "
            "percentage, duration or project the request does not state: describe such an intent without the value. "
            "For a budget change, budget transfer, stop, hold or delay also fill action and the values the request "
            "states (amount_eur, percent, months, cost_type) – copied from the request, never calculated; a "
            "transfer lists the source project first."
        ),
        expected_output="A structured IntentPlan.",
        agent=agent,
        output_pydantic=IntentPlan,
    )


def create_simulation_task(agent, request: str, plan: IntentPlan, portfolio_md: str):
    from app.llm.agent import Task

    return Task(
        description=(
            f"Request: \"{request}\"\n\nYour sub-tasks:\n{_plan_text(plan, 'simulation')}\n\n"
            f"Portfolio:\n{portfolio_md}\n\n"
            "Run the matching simulation tool for every sub-task. The tools build on each other "
            "(e.g. first simulate_project_cancellation, then simulate_budget_transfer with the released amount). "
            "If no amount is given, use the available amount reported by the tool. For budget changes pass "
            "cost_type (capex/opex/total) if the request names it. Schedule questions: simulate_agile_vs_waterfall_shift. "
            "Pausing a project: simulate_on_hold. A savings target without named projects: simulate_portfolio_cut "
            "(method 'value' when the request asks for a strategic or value-optimal cut). "
            "Resuming: simulate_resume. Cost increase across the portfolio: simulate_cost_stress. New baseline: "
            "simulate_rebaseline (reason mandatory). Phase change: simulate_stage_gate. Management reserve: "
            "simulate_reserve_draw / simulate_reserve_return. Skill: simulate_skill_transfer. Risk decision: "
            "simulate_risk_response. Realised benefits after go-live: simulate_benefit_review. Merging a project into "
            "another (source first, target second): simulate_merge."
        ),
        expected_output=(
            "One line per tool call: the tool name and one sentence of assessment. Do not repeat the tool results – "
            "the system passes them on verbatim."
        ),
        agent=agent,
    )


def create_assessment_task(agent, request: str, results: list[str]):
    """Simulator task for a guided plan: the tools already ran with the exact builder values."""
    from app.llm.agent import Task

    shown = _no_braces("\n\n".join(results))
    return Task(
        description=(
            f"Request: \"{request}\"\n\nThe simulations were already run deterministically with the exact values from "
            f"the scenario builder. Do not call any tool and do not recalculate. Results:\n{shown}"
        ),
        expected_output="One line per simulation: the tool name and one sentence of assessment.",
        agent=agent,
    )


def create_analysis_task(agent, request: str, plan: IntentPlan, portfolio_md: str):
    from app.llm.agent import Task

    return Task(
        description=(
            f"Request: \"{request}\"\n\nYour sub-tasks:\n{_plan_text(plan, 'graph_analysis')}\n\n"
            f"Portfolio:\n{portfolio_md}\n\n"
            "For all affected projects check the domino effect (get_downstream_impact) and bottlenecks "
            "(get_upstream_bottlenecks). For project stops check the task domino (get_task_level_impact). "
            "For transfers between two projects check find_shared_resources, for resource questions "
            "identify_team_bottlenecks; for a skill analyse_skill_capacity. Use detect_cluster_risk where relevant."
        ),
        expected_output=("At most 5 bullet points with the key dependency and bottleneck findings and a rating "
                         "(high/medium/low). Do not repeat the tool results – the system passes them on verbatim."),
        agent=agent,
    )


def _no_braces(text: str) -> str:
    """Curly braces of tool outputs become parentheses – the prompts stay as they were evaluated."""
    return str(text or "").replace("{", "(").replace("}", ")")


def create_advisory_task(agent, request: str, plan: IntentPlan, specialist_results: str, steward_md: str, role: str,
                         verdict):
    """The verdict is computed by the rule engine (app/crew/verdict.py) and injected as FINAL.
    The Advisory explains and structures it – it never decides or changes it."""
    from app.llm.agent import Task

    return Task(
        description=advisory_task_description(request, plan, specialist_results, steward_md, role, verdict),
        expected_output="Executive Decision Paper in Markdown with the given recommendation.",
        agent=agent,
    )


def advisory_task_description(request: str, plan: IntentPlan, specialist_results: str, steward_md: str, role: str,
                              verdict) -> str:
    """The Advisory's instructions – shared by the agent task and the streamed call (same prompt in both paths)."""
    reasons = "\n".join(f"- {r}" for r in verdict.reasons) or "- (none)"
    conditions = "\n".join(f"- {d['condition']} ({condition_owner_text(d)})" for d in verdict.condition_details()) or "- (none)"
    return (
        f"Request: \"{request}\"\nRequesting role: {role}\nOrchestrator summary: {plan.summary}\n\n"
        f"FINAL VERDICT (determined by the deterministic rule engine – binding, you must not change it):\n"
        f"{verdict.label}\nRule hits:\n{_no_braces(reasons)}\nConditions that must appear in section 5:\n"
        f"{_no_braces(conditions)}\n\n"
        f"Tool results (verbatim) and your colleagues' assessment:\n{_no_braces(specialist_results)}\n\n"
        f"Data Steward findings:\n{_no_braces(steward_md)}\n\n"
        "Write an Executive Decision Paper in English in Markdown following exactly this outline:\n\n"
        f"{ADVISORY_SECTIONS_TEMPLATE}\n"
        "Sections 3 (scenario figures) and 7 (data quality & notes) are added by the system from the tool results "
        "– do not write them. Be concise: short bullet points, no repetition of tables.\n"
        f"Rules: the **Recommendation:** line must read exactly '{verdict.label}'. Your job is to explain this "
        "verdict for the board with the rule hits and figures above – do not soften, upgrade or downgrade it, and "
        "do not introduce a different recommendation anywhere in the paper. List every condition above in "
        "section 5 (add owner and date). Take numbers only from the prior work. 'INTERNAL ERROR' results are "
        "system defects – name the affected part in section 1. Flag AI estimates with ⚠️ [AI ESTIMATE]. If the "
        "findings mention a forced simulation, state it prominently in section 1. No introduction, no code "
        "fences – only the paper."
    )
