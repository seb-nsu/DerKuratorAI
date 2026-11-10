"""Guided scenarios of the Steering Console: UI inputs become an exact IntentPlan plus a readable request text.
The pipeline executes the plan without re-parsing free text (offline) or hands it to the specialist agents without
the orchestrator detour (LLM mode)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.crew.tasks import IntentPlan, SubIntent

SCENARIOS: dict[str, str] = {
    "budget_change": "Adjust budget",
    "budget_transfer": "Transfer budget",
    "cancel": "Stop project",
    "resource_transfer": "Move resources",
    "schedule": "Schedule / scope / capacity",
    "on_hold": "Put on hold",
    "portfolio_cut": "Portfolio cut (savings target)",
    "resume": "Resume after hold",
    "cost_stress": "Cost stress test (portfolio)",
    "rebaseline": "Re-baseline",
    "stage_gate": "Stage gate (next phase)",
    "reserve_draw": "Management reserve",
    "skill_transfer": "Move skill capacity",
    "risk_response": "Risk response",
    "benefit_review": "Benefit realisation review",
    "merge": "Merge projects",
}
PORTFOLIO_ACTIONS = {"portfolio_cut", "cost_stress"}  # scenarios across the whole scope – no single project to pick
TARGET_ACTIONS = {"budget_transfer", "resource_transfer", "cancel", "skill_transfer", "merge"}  # a second project to pick
RISK_RESPONSES = ("Mitigate", "Accept", "Avoid", "Transfer")
STRESS_COST_TYPES = {"all": "Remaining cost (all)", "opex": "Remaining OpEx", "capex": "Remaining CapEx"}
CUT_METHOD_LABELS = {"ranked": "Priority ranking", "value": "Least strategic value lost"}
COST_TYPE_LABELS = {"total": "Total budget", "capex": "CapEx", "opex": "OpEx"}


@dataclass
class GuidedScenario:
    request: str
    plan: IntentPlan


def _eur(value: float) -> str:
    return f"€{value:,.0f}"


def _sim(action: str, description: str, ids: list[str], **params: Any) -> SubIntent:
    return SubIntent(intent_type="simulation", description=description, project_ids=ids, parameters={"action": action, **params})


def _graph(action: str, description: str, ids: list[str], **params: Any) -> SubIntent:
    return SubIntent(intent_type="graph_analysis", description=description, project_ids=ids, parameters={"action": action, **params})


def build_guided_scenario(action: str, project: str, names: dict[str, str], target: str | None = None, **p: Any) -> GuidedScenario:
    """Builds request text + plan. `names`: project_number -> project_name (for readable texts)."""
    if action not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {action}")
    if action == "portfolio_cut":  # no project – the tool ranks the whole scope
        target = float(p.get("target_eur", 0) or 0)
        if target <= 0:
            raise ValueError("Please enter a savings target > 0.")
        method = p.get("method") or "ranked"
        if method not in CUT_METHOD_LABELS:
            raise ValueError(f"Unknown cut method: {method}")
        request = f"Find {_eur(target)} savings across the portfolio (" + \
            ("value-optimal cut list, least strategic value lost)" if method == "value" else "cut list)")
        plan = IntentPlan(summary=f"Guided scenario: {request}",
                          sub_intents=[_sim("portfolio_cut", request, [], target_eur=target, method=method),
                                       _graph("cut_dependents", "Check the dependents of every stopped project", [])],
                          requires_simulation=True, requires_graph_analysis=True)
        return GuidedScenario(request=request, plan=plan)
    if action == "cost_stress":  # no project – the tool ranks the whole scope
        pct = float(p.get("pct", 0) or 0)
        if pct <= 0:
            raise ValueError("Please enter a cost increase in % (> 0).")
        cost_type, scope = p.get("cost_type", "all"), (p.get("scope") or "").strip()
        request = (f"Stress test: {STRESS_COST_TYPES[cost_type].lower()} +{pct:g} %"
                   + (f" in {scope}" if scope else " across the portfolio"))
        plan = IntentPlan(summary=f"Guided scenario: {request}",
                          sub_intents=[_sim("cost_stress", request, [], pct=pct, cost_type=cost_type, scope=scope or None)],
                          requires_simulation=True, requires_graph_analysis=False)
        return GuidedScenario(request=request, plan=plan)
    if action in ("budget_transfer", "resource_transfer", "skill_transfer", "merge") and (not target or target == project):
        raise ValueError("Please choose a target project different from the source project.")

    label = lambda pid: f"{pid} ({names.get(pid, pid)})"  # noqa: E731
    intents: list[SubIntent] = []

    if action == "budget_change":
        value = float(p.get("value", 0))
        if value <= 0:
            raise ValueError("Please enter an amount or percentage > 0.")
        is_perc, direction = bool(p.get("is_perc", True)), p.get("direction", "decrease")
        cost_type = p.get("cost_type", "total")
        delta = -value if direction == "decrease" else value
        amount = f"{value:g} %" if is_perc else _eur(value)
        verb = "Cut" if delta < 0 else "Increase"
        request = f"{verb} {COST_TYPE_LABELS[cost_type]} of {label(project)} by {amount}"
        intents.append(_sim("budget_change", request, [project], delta=delta, is_perc=is_perc, cost_type=cost_type))
    elif action == "budget_transfer":
        amount = float(p.get("amount", 0))
        cost_type = p.get("cost_type", "total")
        what = _eur(amount) if amount > 0 else "the entire free budget"
        request = f"Transfer {what} ({COST_TYPE_LABELS[cost_type]}) from {label(project)} to {label(target)}"
        intents.append(_sim("budget_transfer", request, [project, target], amount=amount or None, cost_type=cost_type))
    elif action == "cancel":
        request = f"Stop {label(project)}"
        intents.append(_sim("cancel", request, [project]))
        if target and target != project:
            request += f" and transfer the free budget (after exit costs) to {label(target)}"
            intents.append(_sim("budget_transfer", f"Transfer free budget from {project} to {target}",
                                [project, target], amount=None, cost_type="total"))
        intents.append(_graph("task_impact", f"Task domino when stopping {project}", [project]))
    elif action == "resource_transfer":
        fte = float(p.get("fte", 0))
        if fte <= 0:
            raise ValueError("Please enter an FTE count > 0.")
        team = (p.get("team") or "").strip()
        request = f"Move {fte:g} FTE{' from ' + team if team else ''} from {label(project)} to {label(target)}"
        intents.append(_sim("resource_transfer", request, [project, target], fte=fte, team=team))
        intents.append(_graph("team_bottlenecks", "Check team bottlenecks after the transfer", []))
    elif action == "schedule":
        cap, scope, months = (float(p.get(k, 0) or 0) for k in ("capacity_change_pct", "scope_change_pct", "months_delta"))
        if cap == 0 and scope == 0 and months == 0:
            raise ValueError("Please change at least the schedule, scope or capacity.")
        parts = [f"schedule {months:+g} months" if months else "", f"scope {scope:+g} %" if scope else "",
                 f"capacity {cap:+g} %" if cap else ""]
        request = f"Schedule forecast for {label(project)}: " + ", ".join(x for x in parts if x)
        intents.append(_sim("schedule", request, [project], capacity_change_pct=cap, scope_change_pct=scope, months_delta=months))
    elif action == "on_hold":
        months = float(p.get("months", 0) or 0)
        if months <= 0:
            raise ValueError("Please enter the hold duration in months (> 0).")
        retention = p.get("retention_pct")
        kept = f", keeping {float(retention):g} % capacity" if retention is not None else ""
        request = f"Put {label(project)} on hold for {months:g} months{kept}"
        intents.append(_sim("on_hold", request, [project], months=months,
                            retention_pct=None if retention is None else float(retention)))
        intents.append(_graph("task_impact", f"Task domino while {project} is on hold", [project]))
    elif action == "resume":
        restart = str(p.get("restart_date") or "").strip() or None
        fte = p.get("fte")
        request = f"Resume {label(project)}" + (f" on {restart}" if restart else "") + \
            (f" with {float(fte):g} FTE" if fte is not None else "")
        intents.append(_sim("resume", request, [project], restart_date=restart,
                            fte=None if fte is None else float(fte)))
    elif action == "rebaseline":
        reason = str(p.get("reason") or "").strip()
        budget = float(p.get("new_budget_eur") or 0) or None
        end = str(p.get("new_end_date") or "").strip() or None
        if not reason:
            raise ValueError("Please give the reason for the re-baseline.")
        if budget is None and end is None:
            raise ValueError("Please enter a new budget or a new end date.")
        parts = [f"budget {_eur(budget)}" if budget else "", f"end date {end}" if end else ""]
        request = f"Re-baseline {label(project)}: " + ", ".join(x for x in parts if x) + f" (reason: {reason})"
        intents.append(_sim("rebaseline", request, [project], new_budget_eur=budget, new_end_date=end, reason=reason))
    elif action == "stage_gate":
        phase = (p.get("target_phase") or "").strip() or None
        request = f"Check the stage gate of {label(project)}" + (f" into {phase}" if phase else " into its next phase")
        intents.append(_sim("stage_gate", request, [project], target_phase=phase))
    elif action == "reserve_draw":
        amount = float(p.get("amount", 0) or 0)
        if amount <= 0:
            raise ValueError("Please enter an amount > 0.")
        reason = str(p.get("reason") or "").strip()
        if p.get("direction", "draw") == "return":
            request = f"Return {_eur(amount)} of {label(project)} to the management reserve"
            intents.append(_sim("reserve_return", request, [project], amount=amount, reason=reason))
        else:
            if not reason:
                raise ValueError("Please give the reason for the draw.")
            request = f"Draw {_eur(amount)} from the management reserve for {label(project)} (reason: {reason})"
            intents.append(_sim("reserve_draw", request, [project], amount=amount, reason=reason))
    elif action == "skill_transfer":
        skill, fte, months = str(p.get("skill") or "").strip(), float(p.get("fte", 0) or 0), int(p.get("months", 3) or 3)
        if not skill:
            raise ValueError("Please choose a skill.")
        if fte <= 0:
            raise ValueError("Please enter an FTE count > 0.")
        request = f"Move {fte:g} FTE {skill} from {label(project)} to {label(target)} for {months} month(s)"
        intents.append(_sim("skill_transfer", request, [project, target], skill=skill, fte=fte, months=months))
        intents.append(_graph("skill_capacity", f"Check the {skill} capacity after the transfer", [], skill=skill))
    elif action == "merge":
        request = f"Merge {label(project)} into {label(target)}"
        intents.append(_sim("merge", request, [project, target]))
    elif action == "benefit_review":
        request = f"Review the benefit realisation of {label(project)}"
        intents.append(_sim("benefit_review", request, [project]))
    elif action == "risk_response":
        risk = str(p.get("risk_number") or "").strip()
        response = p.get("response", "Mitigate")
        if not risk:
            raise ValueError("Please choose a risk.")
        if response not in RISK_RESPONSES:
            raise ValueError(f"Unknown response: {response}")
        cost = float(p.get("cost_eur", 0) or 0)
        funding = p.get("funding", "project")
        request = (f"{response} risk {risk} of {label(project)}" + (f" for {_eur(cost)}" if cost else "")
                   + (" from the management reserve" if cost and funding == "reserve" else ""))
        intents.append(_sim("risk_response", request, [project], risk_number=risk, response=response, cost_eur=cost,
                            new_probability=p.get("new_probability") or None, funding=funding))

    graph_ids = [project] + ([target] if target and target != project and action in TARGET_ACTIONS - {"cancel"} else [])
    intents.append(_graph("downstream/upstream", "Check project dependencies and domino effect", graph_ids))
    plan = IntentPlan(
        summary=f"Guided scenario: {request}",
        sub_intents=intents,
        requires_simulation=True,
        requires_graph_analysis=True,
    )
    return GuidedScenario(request=request, plan=plan)
