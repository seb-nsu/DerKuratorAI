"""Deterministic simulation tools for the Simulator Agent ("The Quant").

The LLM only extracts parameters – all numbers are calculated here in Python from the ServiceNow data.
Guard rail violations are returned as "ERROR: ..." strings so the LLM passes them on verbatim instead of
calculating itself. Invalid input (a project that is already stopped, identical source and target, a team that is not
allocated, a value out of its range) starts with "INPUT ERROR:" – the analysis is incomplete, not a business NO-GO.

ROI definition (matches planned_roi_pct in the dataset): ROI = (benefit − total cost) / total cost.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.core.business_rules import current_rules
from app.core.security import mask_pii_data
from app.crew.tools.context import (
    ProjectNotFound,
    as_of,
    base_fte,
    blocked_reason,
    first_call,
    get_dataset,
    get_facts,
    get_project,
    get_table,
    record_change,
    record_fact,
    record_tool_result,
    resolve_project_id,
)
from app.crew.tools.timeline_effects import schedule_effects
from app.crew.tools.values import DAYS_PER_MONTH, eur, num, to_date
from app.data_adapters.dataset import CLOSED_PROJECT_STATES, CLOSED_TASK_STATES, DONE_STORY_STATES
from app.llm.tools import tool

logger = logging.getLogger(__name__)

# Business assumptions (FTE rate, exit cost, sprint length, guardrail limits, overload alert) are not constants:
# they come from the environment's governance config at call time (see current_rules()).
COST_TYPES = {"total", "capex", "opex"}
KI_PREFIX = "⚠️ [AI ESTIMATE]"
HEURISTIC_PREFIX = "⚠️ [HEURISTIC PLACEHOLDER – no AI]"  # offline: no model ran, so it is not an AI estimate
INTERNAL_PREFIX = "INTERNAL ERROR:"  # code defect, not a business guardrail
INPUT_PREFIX = "INPUT ERROR:"  # invalid or contradictory tool input: the analysis is incomplete, not a business NO-GO
BUDGET_FIELDS = ("budget_cost_eur", "actual_cost_eur", "planned_capital_eur", "planned_operating_eur")
# An approved budget of exactly 0 is treated as missing: otherwise _budget_delta falls back to the total cost and
# the ledger proposes a budget that was never approved. Actual costs / CapEx / OpEx of 0 stay legitimate.
ZERO_MEANS_MISSING = {"budget_cost_eur"}
BENEFIT_MISSING = "planned_benefit_eur missing – ROI cannot be calculated."
RAMP_UP_WARNING = "Ramp-up > 50 % – plan 1–2 months of onboarding loss (Brooks's law)."


# =========================================================================== helpers
_eur, _to_date = eur, to_date


def _num(value: Any, default: float = 0.0) -> float:
    return num(value, default)


def _opt_num(value: Any) -> float | None:
    return num(value)


def _add_months(d: date, months: float) -> date:
    return d + timedelta(days=round(months * DAYS_PER_MONTH))


def _months_between(a: date, b: date) -> float:
    return (b - a).days / DAYS_PER_MONTH


def _roi(benefit: float | None, cost: float) -> float | None:
    if benefit is None or cost <= 0:
        return None
    return (benefit - cost) / cost * 100


def _fmt_roi(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} %"


def _open_project(
    project_id: str, allow_closed: bool = False, allow_blocked: bool = False, needs: tuple[str, ...] = ()
) -> dict[str, Any] | str:
    """Loads the project from the scenario and checks quality gate, state and calculation-relevant values."""
    try:
        p = get_project(project_id)
    except ProjectNotFound as exc:
        return str(exc)
    pid = p["project_number"]
    reason = blocked_reason(pid)
    if reason and not allow_blocked:
        return (
            f"ERROR: Data quality – {pid} is blocked for quantitative simulation by the Data Steward "
            f"({reason}). Please fix the master data; a qualitative estimate via simulate_unstructured_scenario is possible."
        )
    # Required fields are configurable, so values without which the tool would miscalculate are checked as well
    missing = [f for f in needs if _opt_num(p.get(f)) is None or (f in ZERO_MEANS_MISSING and _opt_num(p.get(f)) == 0)]
    if missing:
        return (f"ERROR: Data quality – {pid}: values missing for this calculation (empty or 0) in {', '.join(missing)}. "
                "No simulation with incomplete financial data.")
    if not allow_closed and str(p.get("state")) in CLOSED_PROJECT_STATES:
        return f"{INPUT_PREFIX} Project {pid} is already '{p['state']}' – no simulation possible."
    return p


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            logger.exception("Simulation tool %s failed", fn.__name__)
            result = f"{INTERNAL_PREFIX} calculation defect in {fn.__name__} ({exc.__class__.__name__}: {exc}) – not a business rule."
        record_tool_result("simulation", fn.__name__, result)  # verdict engine input
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _actual_split(p: dict[str, Any]) -> tuple[float, float]:
    """Actual costs by CapEx/OpEx (from Cost_Plans; fallback: planned ratio)."""
    capex, opex = _num(p.get("actual_capex_eur")), _num(p.get("actual_opex_eur"))
    if capex or opex:
        return capex, opex
    actual = _num(p.get("actual_cost_eur"))
    capex_plan, opex_plan = _num(p.get("planned_capital_eur")), _num(p.get("planned_operating_eur"))
    # share from the CapEx/OpEx plan itself (as in _budget_delta) – never divide by total_planned_cost_eur, which may be 0
    share = capex_plan / (capex_plan + opex_plan) if capex_plan + opex_plan > 0 else 0.5
    share = min(max(share, 0.0), 1.0)
    return actual * share, actual * (1 - share)


def _overdue(p: dict[str, Any]) -> bool:
    """The planned end date has already passed (the remaining duration is no basis for a burn rate)."""
    end = _to_date(p.get("planned_end_date"))
    return end is not None and end <= as_of()


def _monthly_burn(p: dict[str, Any]) -> float:
    """Monthly burn rate: ETC / remaining duration (at least one month, so a project in its last weeks does not burn
    more than its remaining cost); without an ETC, and for a project already past its planned end (whose remaining
    duration says nothing about its burn), the current FTE × blended rate; an overdue project without a team on
    record spreads its budget over the planned duration."""
    etc = _num(p.get("estimate_to_completion_eur"))
    end = _to_date(p.get("planned_end_date"))
    remaining = _months_between(as_of(), end) if end else 0
    if etc > 0 and not _overdue(p):
        return etc / max(remaining, 1.0)
    team = max(_num(p.get("current_fte")) * current_rules().fte_monthly_cost_eur, 0.0)
    if team > 0 or etc <= 0:
        return team
    start = _to_date(p.get("planned_start_date"))
    duration = _months_between(start, end) if start and end else 0.0
    budget = _num(p.get("budget_cost_eur"))
    return budget / duration if duration >= 1 and budget > 0 else etc / max(duration, 1.0)


# =========================================================================== budget core
def _budget_delta(p: dict[str, Any], delta: float, cost_type: str, benefit_impact_pct: float = 0.0) -> dict[str, Any] | str:
    """Calculates a budget change incl. CapEx/OpEx split and ROI – without saving it."""
    pid = p["project_number"]
    cost_type = (cost_type or "total").strip().lower()
    if cost_type not in COST_TYPES:
        return f"{INPUT_PREFIX} cost_type '{cost_type}' invalid – allowed: total, capex, opex."

    capex, opex = _num(p.get("planned_capital_eur")), _num(p.get("planned_operating_eur"))
    total = _num(p.get("total_planned_cost_eur")) or capex + opex
    budget = _num(p.get("budget_cost_eur")) or total
    actual = _num(p.get("actual_cost_eur"))
    eac = _num(p.get("estimate_at_completion_eur"))
    act_capex, act_opex = _actual_split(p)

    d_capex, d_opex = (delta, 0.0) if cost_type == "capex" else (0.0, delta)
    notes: list[str] = []
    if cost_type not in ("capex", "opex"):  # proportional to the planned split; cuts limited to the headroom (plan − actual)
        share = capex / (capex + opex) if capex + opex else 0.5
        d_capex, d_opex = delta * share, delta * (1 - share)
        if delta < 0:
            capex_room, opex_room = max(capex - act_capex, 0.0), max(opex - act_opex, 0.0)
            if -d_capex > capex_room:
                moved = -d_capex - capex_room
                d_opex -= moved
                d_capex = -capex_room
                notes.append(f"ℹ️ Only {_eur(capex_room)} CapEx headroom (already capitalised {_eur(act_capex)}) – "
                             f"{_eur(moved)} of the cut moved to OpEx.")
            if -d_opex > opex_room:
                moved = -d_opex - opex_room
                d_capex -= moved
                d_opex = -opex_room
                notes.append(f"ℹ️ Only {_eur(opex_room)} OpEx headroom (already spent {_eur(act_opex)}) – "
                             f"{_eur(moved)} of the cut moved to CapEx.")

    new_capex, new_opex = capex + d_capex, opex + d_opex
    new_total, new_budget = total + delta, budget + delta

    if new_capex < act_capex - 0.5:
        return (f"ERROR: CapEx undercut – new CapEx budget ({_eur(new_capex)}) is below the already capitalised "
                f"CapEx costs ({_eur(act_capex)}) of {pid}.")
    if new_opex < act_opex - 0.5:
        return (f"ERROR: OpEx undercut – new OpEx budget ({_eur(new_opex)}) is below the OpEx costs already "
                f"spent ({_eur(act_opex)}) of {pid}.")
    if new_budget <= 0:
        return f"ERROR: Budget undercut – new budget ({_eur(new_budget)}) would be ≤ 0."
    if new_budget < actual:
        return (f"ERROR: Budget undercut – new budget ({_eur(new_budget)}) is below the actual costs "
                f"({_eur(actual)}) of {pid}.")

    benefit = _opt_num(p.get("planned_benefit_eur"))
    new_benefit = benefit * (1 + benefit_impact_pct / 100) if benefit is not None else None
    # ROI and return use the expected cost: a budget cut below the forecast does not lower what the project will
    # cost, so it must not raise the ROI or the planned return
    expected_cost = max(eac, new_total) if eac else new_total
    warnings = []
    if eac and new_budget < eac:
        warnings.append(f"⚠ New budget does not cover the forecast: EAC {_eur(eac)} → overrun {_eur(eac - new_budget)}.")
    if not eac:  # a missing EAC must never read as "no overrun"
        warnings.append("⚠ No estimate at completion (EAC) recorded – the forecast-overrun check is not possible.")
    new_roi = _roi(new_benefit, expected_cost)
    if new_roi is not None and new_roi < 0:
        warnings.append("⚠ ROI turns negative – business case no longer viable.")
    if benefit is None:
        warnings.append(f"⚠ {BENEFIT_MISSING}")

    roi = _opt_num(p.get("planned_roi_pct"))
    return {
        "pid": pid, "name": p.get("project_name"), "cost_type": cost_type, "delta": delta, "eac": eac,
        "capex": capex, "opex": opex, "new_capex": new_capex, "new_opex": new_opex,
        "total": total, "new_total": new_total, "budget": budget, "new_budget": new_budget,
        "benefit": benefit, "new_benefit": new_benefit, "expected_cost": expected_cost,
        "roi": roi if roi is not None else _roi(benefit, total), "new_roi": new_roi,
        "ret": _opt_num(p.get("planned_return_eur")),
        "new_ret": new_benefit - expected_cost if new_benefit is not None else None, "warnings": warnings,
        "notes": notes,
    }


def _record_budget(r: dict[str, Any], tool_name: str, note: str = "") -> None:
    pid = r["pid"]
    record_change(pid, "budget_cost_eur", round(r["new_budget"], 2), tool_name, note)
    record_change(pid, "total_planned_cost_eur", round(r["new_total"], 2), tool_name, note)
    if r["new_capex"] != r["capex"]:
        record_change(pid, "planned_capital_eur", round(r["new_capex"], 2), tool_name, note)
    if r["new_opex"] != r["opex"]:
        record_change(pid, "planned_operating_eur", round(r["new_opex"], 2), tool_name, note)
    if r["new_benefit"] is not None and r["new_benefit"] != r["benefit"]:
        record_change(pid, "planned_benefit_eur", round(r["new_benefit"], 2), tool_name, note)
    # derived values are written only when the expected cost or the benefit really changes them
    if r["new_roi"] is not None and (r["roi"] is None or abs(r["new_roi"] - r["roi"]) >= 0.05):
        record_change(pid, "planned_roi_pct", round(r["new_roi"], 1), tool_name, note)
    if r["new_ret"] is not None and (r["ret"] is None or abs(r["new_ret"] - r["ret"]) >= 0.5):
        record_change(pid, "planned_return_eur", round(r["new_ret"], 2), tool_name, note)
    # structured facts for the deterministic verdict engine
    record_fact("budget_change", project=pid, delta=round(r["delta"], 2), new_budget=round(r["new_budget"], 2))
    if r.get("forecast_applicable", True):  # a stopped project has no forecast left to cover
        if r["eac"] and r["new_budget"] < r["eac"]:
            record_fact("budget_overrun", project=pid, eac=round(r["eac"], 2), new_budget=round(r["new_budget"], 2),
                        overrun=round(r["eac"] - r["new_budget"], 2),
                        overrun_pct=round((r["eac"] / r["new_budget"] - 1) * 100, 1))
        if not r["eac"]:
            record_fact("eac_missing", project=pid)
    if r["new_roi"] is not None and r["new_roi"] < 0:
        record_fact("negative_roi", project=pid, new_roi=round(r["new_roi"], 1))
    if f"⚠ {BENEFIT_MISSING}" in r["warnings"]:  # structured tool warning – the verdict never parses report text
        record_fact("benefit_missing", project=pid, warning=BENEFIT_MISSING)


def _delay_forecast(p: dict[str, Any], extra_cost: float, tool_name: str) -> list[str]:
    """A delay raises the forecast: the new EAC (EAC + cost of delay) goes to the ledger (writable field) and is judged
    by the same forecast-overrun rule as a budget change (verdict rule 3: NO-GO above eac_overrun_nogo_pct).
    The remaining cost (ETC) moves with it, so the burn rate of a following step is the same as in one larger step."""
    pid = p["project_number"]
    eac, budget = _num(p.get("estimate_at_completion_eur")), _num(p.get("budget_cost_eur"))
    if not eac:  # a missing EAC must never read as "no overrun"
        record_fact("eac_missing", project=pid)
        return ["⚠ No estimate at completion (EAC) recorded – the forecast-overrun check is not possible."]
    new_eac = round(eac + extra_cost, 2)
    record_change(pid, "estimate_at_completion_eur", new_eac, tool_name, "schedule effect added to the forecast")
    etc = _opt_num(p.get("estimate_to_completion_eur"))
    if etc is not None:
        record_change(pid, "estimate_to_completion_eur", round(max(etc + extra_cost, 0.0), 2), tool_name,
                      "schedule effect added to the remaining cost")
    lines = [f"- New EAC: {_eur(new_eac)} (budget {_eur(budget)})"]
    if budget and new_eac > budget:
        record_fact("budget_overrun", project=pid, eac=new_eac, new_budget=round(budget, 2),
                    overrun=round(new_eac - budget, 2), overrun_pct=round((new_eac / budget - 1) * 100, 1))
        lines.append(f"⚠ Budget does not cover the forecast after the schedule change: EAC {_eur(new_eac)} → overrun "
                     f"{_eur(new_eac - budget)}.")
    return lines


def _delay_factor() -> float:
    """Share of the burn rate a month of delay costs (business rule; 100 % = the team stays fully on board)."""
    return current_rules().delay_cost_factor_pct / 100


def _delay_basis(burn: float) -> str:
    factor = current_rules().delay_cost_factor_pct
    return f"burn rate {_eur(burn)}/month" + (f" × {factor:g} %" if factor != 100 else "")


def _recorded_delay(project_id: str) -> dict[str, Any]:
    """Net delay (calendar days, cost of delay) that earlier schedule shifts of this scenario added to the project."""
    fact = next((f for f in get_facts() if f.get("type") == "schedule_delay" and f.get("project") == str(project_id)), None)
    return {"days": int((fact or {}).get("days") or 0), "cost": float((fact or {}).get("cost") or 0.0)}


def _acceleration_cost(fte: float, remaining_months: float, months: float) -> tuple[float, float, float]:
    """Finishing the same work `months` (< 0) earlier needs proportionally more capacity over the shorter remaining
    duration. Returns (capacity factor, extra cost per month, total extra cost)."""
    new_remaining = remaining_months + months
    factor = remaining_months / new_remaining
    monthly_extra = fte * (factor - 1) * current_rules().fte_monthly_cost_eur
    return factor, monthly_extra, monthly_extra * new_remaining


def _acceleration_error(remaining_months: float, months: float) -> str | None:
    if months < 0 and remaining_months + months <= 0.5:
        return f"ERROR: Acceleration by {abs(months):g} months impossible – only {remaining_months:.1f} months remaining."
    return None


def _budget_lines(r: dict[str, Any]) -> list[str]:
    return [
        f"- Budget: {_eur(r['budget'])} -> {_eur(r['new_budget'])} (Δ {_eur(r['delta'])}, {r['delta'] / max(r['budget'], 1) * 100:+.1f}%)",
        f"- CapEx: {_eur(r['capex'])} -> {_eur(r['new_capex'])} | OpEx: {_eur(r['opex'])} -> {_eur(r['new_opex'])}",
        f"- ROI: {_fmt_roi(r['roi'])} -> {_fmt_roi(r['new_roi'])}"
        + (f" (benefit {_eur(r['benefit'])} -> {_eur(r['new_benefit'])})" if r["new_benefit"] != r["benefit"] and r["benefit"] else "")
        + (f" – on the expected cost (EAC {_eur(r['eac'])}), not the cut budget" if r["eac"] and r["eac"] > r["new_total"] else ""),
        *r.get("notes", []),
        *r["warnings"],
    ]


def _limit_error(project_id: str, amount: float, base: float) -> str | None:
    """A euro amount is held to the same guardrail as a percentage: at most max_budget_change_pct of the budget."""
    limit = current_rules().max_budget_change_pct
    if base > 0 and abs(amount) / base * 100 > limit:
        return (f"ERROR: A change of {_eur(abs(amount))} on {project_id} is {abs(amount) / base * 100:,.0f} % of its budget "
                f"{_eur(base)} – above the limit of ±{limit:.0f}%.")
    return None


# =========================================================================== core logic
@_safe
def budget_change(project_id: str, delta: float, is_perc: bool = False, cost_type: str = "total", benefit_impact_pct: float = 0.0) -> str:
    p = _open_project(project_id, needs=BUDGET_FIELDS)
    if isinstance(p, str):
        return p
    delta = _num(delta, float("nan"))
    if math.isnan(delta) or delta == 0:
        return f"{INPUT_PREFIX} delta must be a non-zero number."
    limit = current_rules().max_budget_change_pct
    if is_perc and abs(delta) > limit:
        return f"ERROR: Percentage change {delta}% exceeds the limit of ±{limit:.0f}%."
    ct = (cost_type or "total").lower()
    base = {"capex": _num(p.get("planned_capital_eur")), "opex": _num(p.get("planned_operating_eur"))}.get(
        ct, _num(p.get("budget_cost_eur"))
    )
    amount = base * delta / 100 if is_perc else delta
    error = None if is_perc else _limit_error(p["project_number"], amount, base)
    if error:
        return error

    r = _budget_delta(p, amount, ct, _num(benefit_impact_pct))
    if isinstance(r, str):
        return r
    _record_budget(r, "simulate_budget_change")
    label = {"capex": "CapEx", "opex": "OpEx"}.get(r["cost_type"], "total budget (proportional CapEx/OpEx)")
    return "\n".join([f"RESULT simulate_budget_change | {r['pid']} ({r['name']}) – {label}", *_budget_lines(r)])


@_safe
def budget_transfer(source_id: str, target_id: str, amount: float, cost_type: str = "total") -> str:
    src = _open_project(source_id, allow_closed=True, needs=BUDGET_FIELDS)
    if isinstance(src, str):
        return src
    tgt = _open_project(target_id, needs=BUDGET_FIELDS)
    if isinstance(tgt, str):
        return tgt
    if src["project_number"] == tgt["project_number"]:
        return f"{INPUT_PREFIX} Source and target are identical."
    amount = _num(amount, float("nan"))
    if math.isnan(amount) or amount <= 0:
        return f"{INPUT_PREFIX} amount must be a positive number."

    available = _num(src.get("budget_cost_eur")) - _num(src.get("actual_cost_eur"))
    if amount > available:
        return (f"ERROR: Budget undercut – {src['project_number']} only has {_eur(available)} available "
                f"(budget − actual costs), requested: {_eur(amount)}.")
    r_src = _budget_delta(src, -amount, cost_type)
    if isinstance(r_src, str):
        return r_src
    ct = (cost_type or "total").lower()
    tgt_base = {"capex": _num(tgt.get("planned_capital_eur")), "opex": _num(tgt.get("planned_operating_eur"))}.get(
        ct, _num(tgt.get("budget_cost_eur")))
    error = _limit_error(tgt["project_number"], amount, tgt_base)  # the receiving budget, as for a direct increase
    if error:
        return error
    r_tgt = _budget_delta(tgt, amount, cost_type)
    if isinstance(r_tgt, str):
        return r_tgt
    if str(src.get("state")) in CLOSED_PROJECT_STATES:  # stopped project: ROI/forecast no longer meaningful
        r_src["new_roi"] = None
        r_src["forecast_applicable"] = False  # its old EAC must not be compared with the released budget
        r_src["warnings"] = [f"ℹ️ ROI not applicable – {src['project_number']} is '{src['state']}'.",
                             "ℹ️ Forecast not applicable – project stopped."]

    _record_budget(r_src, "simulate_budget_transfer", f"-> {tgt['project_number']}")
    _record_budget(r_tgt, "simulate_budget_transfer", f"<- {src['project_number']}")
    # one transfer = one board decision, even though both budgets change
    record_fact("budget_transfer", project=src["project_number"], successor=tgt["project_number"], delta=round(amount, 2))
    return "\n".join([
        f"RESULT simulate_budget_transfer | {_eur(amount)} ({cost_type}) from {src['project_number']} to {tgt['project_number']}",
        f"Source {src['project_number']} ({src['project_name']}):", *_budget_lines(r_src),
        f"Target {tgt['project_number']} ({tgt['project_name']}):", *_budget_lines(r_tgt),
    ])


@_safe
def resource_transfer(source_id: str, target_id: str, fte_count: float, team: str = "") -> str:
    src = _open_project(source_id, allow_closed=True)
    if isinstance(src, str):
        return src
    tgt = _open_project(target_id)
    if isinstance(tgt, str):
        return tgt
    if src["project_number"] == tgt["project_number"]:
        return f"{INPUT_PREFIX} Source and target are identical."
    fte = _num(fte_count, float("nan"))
    if math.isnan(fte) or fte <= 0:
        return f"{INPUT_PREFIX} fte_count must be positive."

    ds = get_dataset()
    alloc = ds.current_allocation(as_of())
    src_alloc = alloc[alloc["project_number"] == src["project_number"]]
    tgt_alloc = alloc[alloc["project_number"] == tgt["project_number"]]
    team = (team or "").strip()
    if team:
        src_alloc = src_alloc[src_alloc["assignment_group"].str.casefold() == team.casefold()]
        if src_alloc.empty:
            return f"{INPUT_PREFIX} {team} is not allocated to {src['project_number']} in the current month."
    available = _num(src.get("current_fte")) if not team else float(pd.to_numeric(src_alloc["allocated_fte"]).sum())
    if fte > available:
        return (f"ERROR: Insufficient resources – {src['project_number']} only has {available:g} FTE"
                f"{' in ' + team if team else ''}, requested: {fte:g}.")

    src_fte, tgt_fte = _num(src.get("current_fte")), _num(tgt.get("current_fte"))
    new_src, new_tgt = src_fte - fte, tgt_fte + fte
    rules = current_rules()
    rate = rules.fte_monthly_cost_eur
    lines = [
        f"RESULT simulate_resource_transfer | {fte:g} FTE{' (' + team + ')' if team else ''} from {src['project_number']} to {tgt['project_number']}",
        f"- {src['project_number']}: {src_fte:g} -> {new_src:g} FTE (burn rate {_eur(-fte * rate)}/month)",
        f"- {tgt['project_number']}: {tgt_fte:g} -> {new_tgt:g} FTE (burn rate +{_eur(fte * rate)}/month)",
    ]
    # Write the FTE move first: it re-evaluates the team load of both projects, so it drops their earlier overload
    # findings (also from a previous step) before the new utilisation is judged
    record_change(src["project_number"], "allocated_fte", new_src, "simulate_resource_transfer",
                  f"-> {tgt['project_number']}" + (f" ({team})" if team else ""), table="Resource_Plans", scenario_field="current_fte")
    record_change(tgt["project_number"], "allocated_fte", new_tgt, "simulate_resource_transfer",
                  f"<- {src['project_number']}" + (f" ({team})" if team else ""), table="Resource_Plans", scenario_field="current_fte")
    # utilisation effect: demand stays, capacity moves (starting from the scenario, so earlier moves count)
    sides = [("source", src, src_alloc, -1, src_fte), ("target", tgt, tgt_alloc, +1, tgt_fte)]
    if str(src.get("state")) in CLOSED_PROJECT_STATES:  # a stopped project has no work left: its team is free
        sides = sides[1:]
        lines.append(f"- Utilisation source: {src['project_number']} is '{src['state']}' – no remaining demand.")
    for label, proj, rows, sign, fte_before in sides:
        cap, dem, fte_now = (float(pd.to_numeric(rows[c]).sum()) for c in ("capacity_hours", "demand_hours", "allocated_fte"))
        if cap > 0 and fte_now > 0:
            hours_per_fte = cap / fte_now
            before = base_fte(proj["project_number"])
            earlier = fte_before - (fte_before if before is None else before)  # FTE moved by earlier steps
            cap = max(cap + earlier * hours_per_fte, 0.0)
            new_cap = max(cap + sign * fte * hours_per_fte, 0.0)
            new_util = dem / new_cap * 100 if new_cap else float("inf")
            old_util = f"{dem / cap * 100:.0f} %" if cap else "∞"
            lines.append(f"- Utilisation {label}: {old_util} -> {'∞' if new_util == float('inf') else f'{new_util:.0f} %'}")
            if new_util > rules.overload_alert_pct:
                lines.append(f"  ⚠ {label.capitalize()} becomes overloaded (> {rules.overload_alert_pct:g} %) – delivery at risk.")
                record_fact("overload", project=proj["project_number"],
                            utilisation_pct=None if new_util == float("inf") else round(new_util, 1))
    if tgt_fte > 0 and fte / tgt_fte > 0.5:
        lines.append(f"⚠ {RAMP_UP_WARNING}")
        record_fact("ramp_up", project=tgt["project_number"], warning=RAMP_UP_WARNING)
    lines.append("ℹ️ Resource plans are not changed automatically – execute via Resource Management.")
    return "\n".join(lines)


@_safe
def timeline_shift(project_id: str, months_delta: int) -> str:
    p = _open_project(project_id)
    if isinstance(p, str):
        return p
    try:
        months = int(round(float(months_delta)))
    except (TypeError, ValueError):
        return f"{INPUT_PREFIX} months_delta must be an integer."
    if months == 0:
        return f"{INPUT_PREFIX} months_delta must not be 0."
    max_shift = current_rules().max_timeline_shift_months
    if abs(months) > max_shift:
        return f"ERROR: A shift of {months} months exceeds the limit of ±{max_shift}."
    end = _to_date(p.get("planned_end_date"))
    if end is None:
        return f"ERROR: No planned_end_date recorded for {p['project_number']}."

    new_end = _add_months(end, months)
    burn = _monthly_burn(p)
    remaining = _months_between(as_of(), end)
    error = _acceleration_error(remaining, months)
    if error:
        return error

    pid = p["project_number"]
    record_change(pid, "planned_end_date", new_end.isoformat(), "simulate_timeline_shift")
    lines = [f"RESULT simulate_timeline_shift | {pid} ({p['project_name']}) {months:+d} months",
             f"- Planned end: {end:%Y-%m-%d} -> {new_end:%Y-%m-%d}"]
    delay = _recorded_delay(pid)
    days = (new_end - end).days
    accel = 0.0
    if months > 0:
        # the calendar days actually added, so steps add up exactly
        spread = burn * _delay_factor() * days / DAYS_PER_MONTH
        record_fact("schedule_delay", project=pid, days=delay["days"] + days, cost=round(delay["cost"] + spread, 2))
        lines += [f"- Cost of delay ({_delay_basis(burn)}): {_eur(spread)}",
                  *([f"- {pid} is already past its planned end ({end:%Y-%m-%d}): the burn rate is the team cost "
                     "(FTE × rate), not the remaining estimate over the remaining time."] if _overdue(p) else []),
                  *_delay_forecast(p, spread, "simulate_timeline_shift"),
                  f"- Benefit realisation shifts by {months} months."]
    else:
        # an earlier delay of this scenario is undone first: its cost of delay is credited, nothing is accelerated
        undo = min(-days, delay["days"])
        spread = -delay["cost"] * undo / delay["days"] if undo > 0 else 0.0
        if undo > 0:
            record_fact("schedule_delay", project=pid, days=delay["days"] - undo, cost=round(delay["cost"] + spread, 2))
            lines.append(f"- Earlier delay undone: {undo / DAYS_PER_MONTH:.1f} months, cost of delay credited "
                         f"{_eur(-spread)}")
        rest = (-days - undo) / DAYS_PER_MONTH
        if rest > 0:
            fte = _num(p.get("current_fte"))
            factor, monthly_extra, accel = _acceleration_cost(fte, remaining - undo / DAYS_PER_MONTH, -rest)
            lines += [f"- Required capacity: ×{factor:.2f} ({fte:g} -> {fte * factor:.1f} FTE)",
                      f"- Additional cost per month: {_eur(monthly_extra)} · acceleration cost {_eur(accel)}"]
        lines += _delay_forecast(p, spread + accel, "simulate_timeline_shift")
    # Every schedule move checks successor deadlines (dependency graph) and re-values the benefit timing (NPV)
    lines += schedule_effects(p, end, new_end, months, accel, spread_cost=spread)
    return "\n".join(lines)


def _agile_forecast(p: dict[str, Any], capacity_change_pct: float, scope_change_pct: float) -> dict[str, Any] | str:
    """Sprint forecast from story points: velocity = completed SP / elapsed sprints since actual start."""
    stories = get_table("Stories", p["project_number"])
    if stories.empty:
        return "no stories available"
    sp = pd.to_numeric(stories["story_points"], errors="coerce").fillna(0)
    done = stories["state"].isin(DONE_STORY_STATES)
    completed, remaining = float(sp[done].sum()), float(sp[~done].sum())
    start = _to_date(p.get("actual_start_date")) or _to_date(p.get("planned_start_date"))
    if start is None:
        return "no start date for velocity calculation"
    sprint_days = current_rules().sprint_length_days
    elapsed = max((as_of() - start).days / sprint_days, 1.0)
    velocity = completed / elapsed
    if velocity <= 0:
        return "velocity = 0 (no completed story points)"
    new_velocity = velocity * (1 + capacity_change_pct / 100)
    if new_velocity <= 0:
        return "capacity ≤ 0"
    new_remaining = remaining * (1 + scope_change_pct / 100)
    base_sprints, new_sprints = remaining / velocity, new_remaining / new_velocity
    return {
        "velocity": velocity, "new_velocity": new_velocity, "remaining_sp": remaining, "new_remaining_sp": new_remaining,
        "completed_sp": completed, "base_sprints": base_sprints, "new_sprints": new_sprints,
        "shift_sprints": new_sprints - base_sprints,
        "shift_months": (new_sprints - base_sprints) * sprint_days / DAYS_PER_MONTH, "sprint_days": sprint_days,
    }


def _waterfall_forecast(p: dict[str, Any], capacity_change_pct: float, scope_change_pct: float) -> dict[str, Any] | str:
    """Classic forecast in months: remaining effort (open tasks) / monthly capacity."""
    tasks = get_table("Project_Tasks", p["project_number"])
    open_tasks = tasks[~tasks["state"].isin(CLOSED_TASK_STATES)] if not tasks.empty else tasks
    end = _to_date(p.get("planned_end_date"))
    if end is None:
        return "no planned_end_date"
    base_months = max(_months_between(as_of(), end), 0.0)
    rem_hours = float((pd.to_numeric(open_tasks["planned_effort_hours"], errors="coerce").fillna(0)
                       * (1 - pd.to_numeric(open_tasks["percent_complete"], errors="coerce").fillna(0) / 100)).sum()) if not open_tasks.empty else 0.0
    factor = (1 + scope_change_pct / 100) / (1 + capacity_change_pct / 100) if capacity_change_pct > -100 else float("inf")
    if factor == float("inf"):
        return "capacity ≤ 0"
    new_months = base_months * factor
    return {"base_months": base_months, "new_months": new_months, "shift_months": new_months - base_months,
            "remaining_hours": rem_hours, "open_tasks": len(open_tasks)}


@_safe
def agile_vs_waterfall_shift(project_id: str, capacity_change_pct: float = 0.0, scope_change_pct: float = 0.0, months_delta: float = 0.0) -> str:
    p = _open_project(project_id)
    if isinstance(p, str):
        return p
    cap, scope, months = _num(capacity_change_pct), _num(scope_change_pct), _num(months_delta)
    if cap == 0 and scope == 0 and months == 0:
        return f"{INPUT_PREFIX} At least one of capacity_change_pct, scope_change_pct or months_delta must be ≠ 0."
    if cap <= -100:
        return f"{INPUT_PREFIX} capacity_change_pct ≤ -100 % means no capacity left."
    max_shift = current_rules().max_timeline_shift_months  # same guardrail as the direct timeline shift
    if abs(months) > max_shift:
        return f"ERROR: A shift of {months:g} months exceeds the limit of ±{max_shift}."
    etype = str(p.get("execution_type") or "Waterfall")
    end = _to_date(p.get("planned_end_date"))
    if end is None:
        return f"ERROR: No planned_end_date recorded for {p['project_number']}."
    burn = _monthly_burn(p)
    lines = [f"RESULT simulate_agile_vs_waterfall_shift | {p['project_number']} ({p['project_name']}) – method: {etype}",
             f"- Inputs: capacity {cap:+.0f} %, scope {scope:+.0f} %, direct shift {months:+g} months"]
    shifts: list[float] = []

    if etype in ("Agile", "Hybrid"):
        a = _agile_forecast(p, cap, scope)
        if isinstance(a, str):
            lines.append(f"- Agile component not calculable ({a}) – no velocity yet, e.g. a newly started project.")
            if etype == "Agile":  # fall back to the time-based forecast instead of failing the scenario
                w = _waterfall_forecast(p, cap, scope)
                if isinstance(w, str):
                    if months:
                        shifts.append(months)
                        lines.append(f"- [Fallback] Direct shift only: {months:+g} months (no scope/capacity forecast: {w})")
                else:
                    shifts.append(w["shift_months"] + months)
                    lines += [
                        f"- [Fallback] Time-based forecast (remaining duration × scope/capacity factor): "
                        f"{w['base_months']:.1f} -> {w['new_months'] + months:.1f} months",
                        f"- [Fallback] Shift: {w['shift_months'] + months:+.1f} months – indicative until a velocity "
                        "is established (completed story points)",
                    ]
        else:
            direct_sprints = months * DAYS_PER_MONTH / a["sprint_days"]
            total_sprints = a["shift_sprints"] + direct_sprints
            shifts.append(a["shift_months"] + months)
            lines += [
                f"- [Agile] Velocity: {a['velocity']:.1f} -> {a['new_velocity']:.1f} SP/sprint ({a['sprint_days']} days)",
                f"- [Agile] Remaining backlog: {a['remaining_sp']:.0f} -> {a['new_remaining_sp']:.0f} SP "
                f"({a['completed_sp']:.0f} SP done) ⇒ {a['base_sprints']:.1f} -> {a['new_sprints'] + direct_sprints:.1f} sprints",
                f"- [Agile] Shift: {total_sprints:+.1f} sprints (≈ {a['shift_months'] + months:+.1f} months)",
            ]
            if months < 0:
                lines.append(f"- [Agile] For {months:+g} months ≈ {abs(direct_sprints) * a['velocity']:.0f} SP would have to be descoped.")
    if etype in ("Waterfall", "Hybrid"):
        w = _waterfall_forecast(p, cap, scope)
        if isinstance(w, str):
            lines.append(f"- Waterfall component not calculable: {w}")
        else:
            shifts.append(w["shift_months"] + months)
            lines += [
                f"- [Waterfall] Remaining duration: {w['base_months']:.1f} -> {w['new_months'] + months:.1f} months "
                f"({w['open_tasks']} open tasks, {w['remaining_hours']:.0f} h remaining effort)",
                f"- [Waterfall] Shift: {w['shift_months'] + months:+.1f} months",
            ]
    if not shifts:
        return "\n".join(lines + ["ERROR: No forecast possible."])

    critical = max(shifts, key=abs) if etype != "Hybrid" else max(shifts)
    if etype == "Hybrid":
        lines.append("- [Hybrid] Critical path = max(Agile, Waterfall)")
    remaining = max(_months_between(as_of(), end), 0.0)
    new_remaining = remaining + critical
    error = _acceleration_error(new_remaining - months, months)  # the direct shift on top of the scope/capacity forecast
    if error:
        return error
    cost = _schedule_cost(p, burn, remaining, new_remaining, cap, scope, months)
    new_end = _add_months(end, critical)
    lines.append(f"- Planned end: {end:%Y-%m-%d} -> {new_end:%Y-%m-%d} ({critical:+.1f} months)")
    lines += cost["lines"]
    shifted = abs(critical) >= 0.05
    if shifted:  # end date first, then the forecast – the same ledger order as timeline_shift
        record_change(p["project_number"], "planned_end_date", new_end.isoformat(), "simulate_agile_vs_waterfall_shift")
    if abs(cost["delta"]) >= 1:
        lines += _delay_forecast(p, cost["delta"], "simulate_agile_vs_waterfall_shift")
    if shifted:
        # Successor deadlines (dependency graph) and NPV/benefit timing for the forecast end date
        lines += schedule_effects(p, end, new_end, critical, max(cost["delta"], 0.0))
    return "\n".join(lines)


def _schedule_cost(p: dict[str, Any], burn: float, remaining: float, new_remaining: float, cap: float, scope: float,
                   months: float) -> dict[str, Any]:
    """Forecast change of a schedule scenario. Capacity changes the duration, not the effort: half the team for twice
    the time costs the same. Scope changes the effort (ETC × scope %). A direct delay keeps the team on board (new burn
    rate × months); a direct acceleration needs extra capacity over the shorter remaining duration."""
    etc = _num(p.get("estimate_to_completion_eur")) or burn * remaining
    new_burn = burn * (1 + cap / 100)
    work = etc * scope / 100
    delay = new_burn * _delay_factor() * months if months > 0 else 0.0
    accel = 0.0
    lines = []
    if scope:
        lines.append(f"- Remaining cost (scope {scope:+.0f} %): ETC {_eur(etc)} -> {_eur(etc + work)}")
    if cap:
        lines.append(f"- Team capacity {cap:+.0f} %: burn rate {_eur(burn)} -> {_eur(new_burn)}/month – same effort, "
                     f"{'longer' if cap < 0 else 'shorter'} duration, no extra cost")
    if months > 0:
        lines.append(f"- Cost of delay ({_delay_basis(new_burn)}): {_eur(delay)}")
    elif months < 0:
        fte = _num(p.get("current_fte")) * (1 + cap / 100)
        factor, monthly_extra, accel = _acceleration_cost(fte, new_remaining - months, months)
        lines.append(f"- Acceleration: capacity ×{factor:.2f} for {new_remaining:.1f} months, "
                     f"{_eur(monthly_extra)}/month · acceleration cost {_eur(accel)}")
    delta = work + delay + accel
    if scope or months:
        lines.append(f"- Forecast change: {_eur(delta)}")
    return {"delta": delta, "lines": lines}


@_safe
def project_cancellation(project_id: str) -> str:
    # a stop is possible despite the quality gate (stopping a project with poor master data is a valid decision);
    # its figures are then indicative – a condition, not a NO-GO
    p = _open_project(project_id, allow_blocked=True, needs=("budget_cost_eur", "actual_cost_eur"))
    if isinstance(p, str):
        return p
    budget, actual = _num(p.get("budget_cost_eur")), _num(p.get("actual_cost_eur"))
    act_capex, _ = _actual_split(p)
    remaining = max(budget - actual, 0.0)
    rules = current_rules()
    exit_rate = rules.exit_cost_rate_pct / 100
    exit_cost = remaining * exit_rate
    fte = _num(p.get("current_fte"))
    benefit = _opt_num(p.get("planned_benefit_eur"))
    alloc = get_dataset().current_allocation(as_of())
    teams = sorted(alloc[alloc["project_number"] == p["project_number"]]["assignment_group"].astype(str).unique())

    pid = p["project_number"]
    hard = _hard_dependents(pid)
    record_change(pid, "state", "Canceled", "simulate_project_cancellation")
    record_fact("cancel", project=pid, sunk_pct=round(actual / max(budget, 1) * 100, 1), impairment=round(act_capex, 2),
                benefit_forgone=round(benefit, 2) if benefit is not None else None, hard_dependents=hard,
                exit_cost=round(exit_cost, 2))
    note = indicative_note(pid)
    return "\n".join([
        f"RESULT simulate_project_cancellation | {p['project_number']} ({p['project_name']}), progress {_num(p.get('percent_complete')):g} %",
        f"- Sunk costs: {_eur(actual)} ({actual / max(budget, 1) * 100:.0f} % of budget), of which capitalised CapEx {_eur(act_capex)} "
        "→ impairment risk",
        f"- Remaining budget: {_eur(remaining)} | exit/contract costs ({exit_rate:.0%}): {_eur(exit_cost)}",
        f"- Net budget released: {_eur(remaining - exit_cost)}",
        f"- Capacity released: {fte:g} FTE (≈ {_eur(fte * rules.fte_monthly_cost_eur)}/month) from {', '.join(teams) or 'n/a'}",
        f"- Planned benefit forgone: {_eur(benefit) if benefit is not None else 'n/a'}",
        f"- Note: released budget can be moved via simulate_budget_transfer (max. {_eur(remaining)}); "
        "check affected tasks via get_task_level_impact.",
        *note,
    ])


def indicative_note(project_id: str) -> list[str]:
    """For a stop or hold of a project the quality gate blocks: records the `indicative` fact (a condition) and
    returns the report line."""
    reason = blocked_reason(project_id)
    if not reason:
        return []
    record_fact("indicative", project=str(project_id), reason=reason)
    return [f"⚠ Master data incomplete ({reason}) – figures indicative."]


def _hard_dependents(project_id: str) -> int:
    """Direct successors with a hard dependency (Dependencies table, restricted projects included: a stop must never
    look harmless because the PM cannot see the successor)."""
    deps = get_table("Dependencies")
    if deps.empty or not {"predecessor_project_number", "dependency_type"} <= set(deps.columns):
        return 0
    rows = deps[deps["predecessor_project_number"].astype(str) == str(project_id)]
    return int((rows["dependency_type"].astype(str).str.casefold() == "hard").sum())


def unstructured_scenario(project_id: str, scenario_desc: str) -> str:
    """Qualitative LLM estimate – always flagged with the AI prefix (also for blocked projects)."""
    result = _unstructured_scenario(project_id, scenario_desc)
    record_tool_result("simulation", "unstructured_scenario", result)  # verdict engine input
    return result


def _unstructured_scenario(project_id: str, scenario_desc: str) -> str:
    try:
        p = _open_project(project_id, allow_closed=True, allow_blocked=True)
        if isinstance(p, str):
            return f"{KI_PREFIX} {p}"
        desc = mask_pii_data(str(scenario_desc or "")).strip()
        if not desc:
            return f"{KI_PREFIX} ERROR: scenario_desc is empty."
        facts = (
            f"Project {p['project_number']} '{p['project_name']}' ({p.get('execution_type')}): budget {_eur(_num(p.get('budget_cost_eur')))}, "
            f"actual {_eur(_num(p.get('actual_cost_eur')))}, EAC {_eur(_num(p.get('estimate_at_completion_eur')))}, "
            f"{_num(p.get('percent_complete')):g} % complete, status {p.get('status')}, planned end {_to_date(p.get('planned_end_date')) or 'n/a'}, "
            f"{_num(p.get('high_impact_risks')):g} high-impact risks"
        )
        try:
            from app.core.runtime_config import get_runtime_config
            from app.llm.client import complete  # local import avoids cycles

            text = complete(get_runtime_config().model_for_role("simulator"), (
                "You are an experienced IT portfolio controller. Give a concise qualitative assessment "
                "(max. 6 bullet points) with a range (optimistic/realistic/pessimistic). "
                "State assumptions explicitly. Answer in English."), f"Facts: {facts}\nScenario: {desc}",
                purpose="unstructured estimate")
        except Exception as exc:  # noqa: BLE001 - offline / missing key fallback
            logger.info("Unstructured estimate without LLM (%s)", exc)
            return (f"{HEURISTIC_PREFIX} Rough rule of thumb for: '{desc}'. {facts}. Assumption: qualitative "
                    "scenarios typically carry 5–15 % cost and 1–3 months schedule risk; expert assessment required.")
        return f"{KI_PREFIX} {text}"
    except Exception as exc:
        logger.exception("unstructured_scenario failed")
        return f"{KI_PREFIX} {INTERNAL_PREFIX} estimate not possible ({exc.__class__.__name__}: {exc})"



# =========================================================================== agent tools
def repeated_call(tool: str, project_ref: str) -> str | None:
    """None for the first call of a simulation tool on a project in this step; else the note that the repeated call
    was dropped (the first result stands). An unknown project passes – the tool reports it itself."""
    try:
        pid = resolve_project_id(project_ref) if project_ref else ""
    except ProjectNotFound:
        return None
    if first_call(tool, pid):
        return None
    logger.warning("Repeated tool call dropped: %s for %s", tool, pid or "the portfolio")
    return (f"SKIPPED: {tool} already ran for {pid or 'the portfolio'} in this step – the repeated call was dropped, "
            "the first result stands.")


@tool("simulate_budget_change")
def simulate_budget_change(project_id: str, delta: float, is_perc: bool = False, cost_type: str = "total", benefit_impact_pct: float = 0.0) -> str:
    """Simulates a budget increase/cut with CapEx/OpEx split and recalculates planned_roi_pct.
    project_id: e.g. 'PRJ-DEMO-1002'. delta: amount in EUR or percent (is_perc=True), negative = cut.
    cost_type: 'capex', 'opex' or 'total' (proportional). benefit_impact_pct: optional benefit change in %
    (e.g. -10 if a CapEx cut reduces scope and thus benefit). Pass ERROR strings on unchanged."""
    if skipped := repeated_call("simulate_budget_change", project_id):
        return skipped
    return budget_change(project_id, delta, is_perc, cost_type, benefit_impact_pct)


@tool("simulate_budget_transfer")
def simulate_budget_transfer(source_id: str, target_id: str, amount: float, cost_type: str = "total") -> str:
    """Simulates a budget transfer (amount in EUR) from source_id to target_id incl. ROI of both projects.
    cost_type: 'capex', 'opex' or 'total'. Checks that the source does not fall below its actual costs."""
    if skipped := repeated_call("simulate_budget_transfer", source_id):
        return skipped
    return budget_transfer(source_id, target_id, amount, cost_type)


@tool("simulate_resource_transfer")
def simulate_resource_transfer(source_id: str, target_id: str, fte_count: float, team: str = "") -> str:
    """Simulates moving fte_count FTE from source_id to target_id (optionally only from one team,
    e.g. 'Team Quartz'). Calculates burn rate and utilisation effect for both projects."""
    if skipped := repeated_call("simulate_resource_transfer", source_id):
        return skipped
    return resource_transfer(source_id, target_id, fte_count, team)


@tool("simulate_timeline_shift")
def simulate_timeline_shift(project_id: str, months_delta: int) -> str:
    """Simulates a direct schedule shift in months (> 0 delay with cost of delay, < 0 acceleration)."""
    if skipped := repeated_call("simulate_timeline_shift", project_id):
        return skipped
    return timeline_shift(project_id, months_delta)


@tool("simulate_agile_vs_waterfall_shift")
def simulate_agile_vs_waterfall_shift(project_id: str, capacity_change_pct: float = 0.0, scope_change_pct: float = 0.0, months_delta: float = 0.0) -> str:
    """Method-aware schedule forecast based on execution_type: Agile -> sprints via story points and velocity,
    Waterfall -> months via remaining effort of open tasks, Hybrid -> critical path of both.
    capacity_change_pct: capacity change in % (e.g. -20 if a team is pulled), scope_change_pct: scope change in %,
    months_delta: additional direct shift in months."""
    if skipped := repeated_call("simulate_agile_vs_waterfall_shift", project_id):
        return skipped
    return agile_vs_waterfall_shift(project_id, capacity_change_pct, scope_change_pct, months_delta)


@tool("simulate_project_cancellation")
def simulate_project_cancellation(project_id: str) -> str:
    """Simulates stopping a project: sunk costs, CapEx impairment risk, exit costs, released budget,
    FTE/teams and forgone benefit."""
    if skipped := repeated_call("simulate_project_cancellation", project_id):
        return skipped
    return project_cancellation(project_id)


@tool("simulate_unstructured_scenario")
def simulate_unstructured_scenario(project_id: str, scenario_desc: str) -> str:
    """Fallback ONLY for qualitative scenarios without a matching calculation tool (e.g. 'supplier drops out').
    Returns an assessment flagged '⚠️ [AI ESTIMATE]' – no reliable numbers."""
    return unstructured_scenario(project_id, scenario_desc)


SIMULATOR_TOOLS = [
    simulate_budget_change,
    simulate_budget_transfer,
    simulate_resource_transfer,
    simulate_timeline_shift,
    simulate_agile_vs_waterfall_shift,
    simulate_project_cancellation,
    simulate_unstructured_scenario,
]
