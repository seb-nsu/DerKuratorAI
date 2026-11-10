"""Consequences of moving a project's end date – shared by every simulation that shifts a schedule
(timeline shift, method-aware forecast, on hold).

1. Dependency deadlines (dependency graph): the new end date is checked against the `required_by_date` of every direct
   successor. A newly missed date or a growing delay is reported as a "⚠ RISK" line and recorded as the fact
   `deadline_breach`, which the verdict engine turns into a CONDITIONAL GO condition.
2. NPV & benefit timing: benefit tranches (Benefit_Plans, fallback planned_benefit_eur at the planned end) move with
   the schedule. The change in present value is estimated with the project's discount rate (business-rule default
   otherwise), together with the present value of the extra cost (cost of delay / standby). The undiscounted ROI
   including the extra cost is reported next to it. NPV figures are estimates and labelled as such.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from app.core.business_rules import current_rules
from app.crew.tools.context import (
    RESTRICTED_DEPENDENTS,
    RESTRICTED_SUCCESSOR,
    allowed_project_ids,
    as_of,
    clear_facts,
    get_table,
    record_change,
    record_fact,
)
from app.crew.tools.values import DAYS_PER_MONTH
from app.crew.tools.values import eur as _eur
from app.crew.tools.values import num as _num
from app.crew.tools.values import to_date as _to_date


def shift_date(d: date, months: float) -> date:
    return d + timedelta(days=round(months * DAYS_PER_MONTH))


# =========================================================================== 1. dependency deadlines
def deadline_check(project_id: str, old_end: date, new_end: date) -> list[str]:
    """Report lines for all direct successors whose required-by date is affected by moving old_end -> new_end."""
    from app.crew.tools.graph_tools import GraphUnavailable, successor_deadlines

    if new_end <= old_end:
        return []
    try:
        rows = successor_deadlines(project_id)
    except GraphUnavailable as exc:
        record_fact("graph_unavailable", tool="deadline_check", project=project_id)
        return [f"- ℹ️ Dependency deadline check not possible – graph database unavailable ({str(exc)[:120]})."]
    clear_facts("graph_unavailable", tool="deadline_check")  # the graph answers again: an earlier outage is over
    if not rows:
        return ["- Dependency deadlines: no dependent projects – no deadline at risk."]
    allowed = allowed_project_ids()
    lines = ["- Dependency deadlines (direct successors):"]
    hidden_breach = False
    for row in rows:
        if row["id"] not in allowed:  # outside the scope: no id, date or count – one neutral line below
            required = _to_date(row.get("required_by"))
            if required is not None and (new_end - required).days > 0:
                hidden_breach = True
                record_fact("deadline_breach", project=project_id, successor=RESTRICTED_SUCCESSOR, restricted=True,
                            new=(old_end - required).days <= 0)
            continue
        succ = row["id"]
        kind = row.get("type") or "unspecified"
        required = _to_date(row.get("required_by"))
        if required is None:
            lines.append(f"  - {succ}: {kind} dependency without required-by date – not checkable.")
            continue
        late_before, late_after = (old_end - required).days, (new_end - required).days
        if late_after <= 0:
            lines.append(f"  - ✓ {succ}: required by {required:%Y-%m-%d} – still met ({-late_after} days slack).")
            continue
        newly = late_before <= 0
        record_fact("deadline_breach", project=project_id, successor=succ, dependency_type=kind, new=newly,
                    required_by=required.isoformat(), days_late=late_after, days_late_before=max(late_before, 0))
        if newly:
            lines.append(f"  - ⚠ RISK: {succ} misses the required-by date {required:%Y-%m-%d} of its {kind} dependency "
                         f"(new end {new_end:%Y-%m-%d}, {late_after} days late). Condition: agree a new date or a "
                         "mitigation with the successor.")
        else:
            lines.append(f"  - ⚠ RISK: {succ} already waits beyond its required-by date {required:%Y-%m-%d} ({kind}); "
                         f"lateness grows from {late_before} to {late_after} days.")
    if hidden_breach:
        lines.append(f"  - ⚠ RISK: {RESTRICTED_DEPENDENTS}")
    if len(lines) == 1:
        lines = ["- Dependency deadlines: no dependent projects in your scope."]
    return lines


# =========================================================================== 2. NPV & benefit timing
def _benefit_tranches(p: dict[str, Any]) -> tuple[list[tuple[float, date]], str]:
    plans = get_table("Benefit_Plans", str(p["project_number"]))
    tranches = []
    if not plans.empty and {"planned_benefit_eur", "realization_start_date"} <= set(plans.columns):
        for row in plans.to_dict(orient="records"):
            amount, when = _num(row.get("planned_benefit_eur")), _to_date(row.get("realization_start_date"))
            if amount and when:
                tranches.append((amount, when))
    if tranches:
        return tranches, f"{len(tranches)} benefit tranche(s) from Benefit_Plans"
    benefit, end = _num(p.get("planned_benefit_eur")), _to_date(p.get("planned_end_date"))
    if benefit and end:
        return [(benefit, end)], "planned_benefit_eur realised at the planned end (no Benefit_Plans)"
    return [], ""


def _original(p: dict[str, Any], column: str) -> Any:
    """The project's value before any step of the scenario (the loaded dataset, not the scenario copy)."""
    from app.crew.tools.context import get_dataset  # local import keeps this module free of a dataset dependency

    projects = get_dataset().projects
    row = projects[projects["project_number"].astype(str) == str(p["project_number"])]
    return row[column].iloc[0] if not row.empty and column in row else None


def _offset_days(p: dict[str, Any], old_end: date | None) -> int:
    """Days by which earlier steps of the scenario already moved the project's end (benefit tranches move with it)."""
    original = _to_date(_original(p, "planned_end_date"))
    return (old_end - original).days if old_end and original else 0


def npv_effect(p: dict[str, Any], months_shift: float, extra_cost: float, new_end: date,
               old_end: date | None = None, spread_cost: float = 0.0) -> list[str]:
    """Estimated ΔNPV and ROI after moving the schedule by `months_shift`. `extra_cost` (standby, acceleration) is
    discounted at the new end; `spread_cost` (cost of delay; negative = an earlier delay credited back) is spread
    evenly over the days between the old and the new end. The step starts from the scenario – its NPV and the benefit
    dates as moved by earlier steps – so two steps give the same result as one."""
    pid = str(p["project_number"])
    tranches, basis = _benefit_tranches(p)
    if not tranches:
        return ["- ℹ️ NPV impact not calculable – no benefit plan and no planned benefit recorded."]
    offset = _offset_days(p, old_end)
    tranches = [(a, d + timedelta(days=offset)) for a, d in tranches]
    rules = current_rules()
    rate_pct = _num(p.get("discount_rate_pct"))
    rate_pct = rate_pct if rate_pct is not None else rules.default_discount_rate_pct
    r, today = rate_pct / 100, as_of()

    def pv(amount: float, when: date) -> float:
        years = max((when - today).days / 365.25, 0.0)
        return amount / (1 + r) ** years

    pv_before = sum(pv(a, d) for a, d in tranches)
    pv_after = sum(pv(a, shift_date(d, months_shift)) for a, d in tranches)

    def pv_spread(amount: float, start: date, end: date) -> float:
        """PV of an amount spent evenly between two dates (continuous discounting – additive over sub-periods)."""
        t0, t1 = sorted(max((d - today).days / 365.25, 0.0) for d in (start, end))
        if t1 - t0 < 1e-9 or r == 0:
            return pv(amount, end)
        k = math.log(1 + r)
        return amount * ((1 + r) ** -t0 - (1 + r) ** -t1) / (k * (t1 - t0))

    pv_cost = (pv(extra_cost, new_end) if extra_cost > 0 else 0.0) + (
        pv_spread(spread_cost, old_end or new_end, new_end) if spread_cost else 0.0)
    extra_cost = max(extra_cost, 0.0) + spread_cost
    delta_benefit, delta_npv = pv_after - pv_before, pv_after - pv_before - pv_cost
    lines = [f"- NPV impact (estimate, {rate_pct:g} % discount rate, {basis}): PV of benefits {_eur(delta_benefit)}"
             f" · PV of extra cost {_eur(-pv_cost)} · ΔNPV {_eur(delta_npv)}"]
    base_npv = _num(p.get("net_present_value_eur"))
    if base_npv is not None:
        new_npv = base_npv + delta_npv
        record_change(pid, "net_present_value_eur", round(new_npv, 2), "schedule_effects", "NPV estimate after the shift")
        lines.append(f"- NPV: {_eur(base_npv)} -> {_eur(new_npv)}")
        original = _original(p, "net_present_value_eur")  # compared with the start of the scenario, not the last step
        start_npv = _num(original) if original is not None else base_npv
        if start_npv is not None and start_npv >= 0 > new_npv:
            lines.append(f"- ⚠ NPV turns negative for {pid} – the business case no longer creates value at "
                         f"{rate_pct:g} % discount rate.")
            record_fact("npv_negative", project=pid, npv_before=round(start_npv, 2), npv_after=round(new_npv, 2))
    benefit_total = sum(a for a, _ in tranches)
    # ROI on the forecast before and after this step (the EAC already carries earlier steps)
    cost = _num(p.get("estimate_at_completion_eur")) or _num(p.get("total_planned_cost_eur")) or _num(p.get("budget_cost_eur"))
    if cost and cost > 0 and cost + extra_cost > 0:
        roi_before = (benefit_total - cost) / cost * 100
        roi_after = (benefit_total - cost - extra_cost) / (cost + extra_cost) * 100
        lines.append(f"- ROI on the forecast (undiscounted, incl. extra cost): {roi_before:.1f} % -> {roi_after:.1f} %")
    record_fact("npv_change", project=pid, months_shift=round(months_shift, 2), delta_npv=round(delta_npv, 2),
                delta_pv_benefits=round(delta_benefit, 2), extra_cost=round(extra_cost, 2), discount_rate_pct=rate_pct)
    return lines


def schedule_effects(p: dict[str, Any], old_end: date, new_end: date, months_shift: float, extra_cost: float,
                     spread_cost: float = 0.0) -> list[str]:
    """Everything a schedule move triggers: deadline check (delays only) + NPV/ROI estimate."""
    lines = deadline_check(str(p["project_number"]), old_end, new_end)
    if abs(months_shift) >= 0.05:
        lines += npv_effect(p, months_shift, extra_cost, new_end, old_end, spread_cost)
    return lines
