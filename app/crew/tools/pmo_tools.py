"""PMO use cases beyond budget, schedule and staff moves: resume after a hold, a portfolio cost stress test,
re-baselining, stage gates, the management reserve and risk responses.

Every tool calculates deterministically (no LLM), works on the RLS-filtered scenario context, writes only through
record_change / record_row_change (the four-eyes approval applies to the ledger) and reports structured facts for
the verdict engine. Business assumptions and thresholds come from the governance config (current_rules()).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.core.business_rules import current_rules
from app.crew.tools import gate_checks
from app.crew.tools.context import (
    as_of,
    base_fte,
    blocked_reason,
    first_call,
    get_dataset,
    get_facts,
    get_ledger,
    get_scenario_df,
    get_table,
    pending_rows,
    record_change,
    record_fact,
    record_row_change,
    row_value,
    table_with_pending,
)
from app.crew.tools.simulator_tools import (
    BUDGET_FIELDS,
    INPUT_PREFIX,
    _budget_delta,
    _delay_forecast,
    _eur,
    _limit_error,
    _monthly_burn,
    _num,
    _open_project,
    _opt_num,
    _record_budget,
    _safe,
    _to_date,
    indicative_note,
    repeated_call,
)
from app.crew.tools.timeline_effects import schedule_effects
from app.crew.tools.values import DAYS_PER_MONTH
from app.data_adapters.dataset import CLOSED_PROJECT_STATES, PHASES
from app.llm.tools import tool

PAUSED = "On Hold"
DEFAULT_RESUME_STATE = "Work in Progress"
RESPONSES = ("Mitigate", "Accept", "Avoid", "Transfer")
LEVELS = ("Low", "Moderate", "High")
COST_STRESS_TYPES = ("all", "opex", "capex")


# =========================================================================== A1 resume after a hold
def _hold_fact(pid: str) -> dict[str, Any] | None:
    return next((f for f in reversed(get_facts()) if f.get("type") == "on_hold" and f.get("project") == pid), None)


def _state_before_hold(pid: str) -> str:
    """The state the project had before it was put on hold in this scenario (else Work in Progress)."""
    for c in reversed(get_ledger()):
        if c["project_id"] == pid and c["field"] == "state" and c.get("new") == PAUSED and c.get("old") not in (None, PAUSED):
            return str(c["old"])
    return DEFAULT_RESUME_STATE


def _team_load(pid: str, month_of: date, factor: float) -> list[tuple[str, float]]:
    """Utilisation per team of the project in the month of `month_of` if the project's demand is scaled by `factor`
    (returning team vs. the team before the hold)."""
    rp = get_dataset().resource_plans
    if rp.empty or not {"month", "assignment_group", "capacity_hours", "demand_hours"} <= set(rp.columns):
        return []
    month = get_dataset().reference_month(month_of)
    rows = rp[rp["month"] == month]
    if rows.empty:
        rows = get_dataset().current_allocation(month_of)
    teams = sorted(rows[rows["project_number"].astype(str) == pid]["assignment_group"].astype(str).unique())
    loads = []
    for team in teams:
        team_rows = rows[rows["assignment_group"].astype(str) == team]
        cap = float(pd.to_numeric(team_rows["capacity_hours"], errors="coerce").fillna(0).sum())
        demand = pd.to_numeric(team_rows["demand_hours"], errors="coerce").fillna(0)
        own = float(demand[team_rows["project_number"].astype(str) == pid].sum())
        if cap > 0:
            loads.append((team, (float(demand.sum()) - own + own * factor) / cap * 100))
    return loads


@_safe
def resume(project_id: str, restart_date: Any = None, fte: float | None = None) -> str:
    p = _open_project(project_id, allow_blocked=True, needs=("budget_cost_eur", "actual_cost_eur"))
    if isinstance(p, str):
        return p
    pid = str(p["project_number"])
    if str(p.get("state")) != PAUSED:
        return f"{INPUT_PREFIX} {pid} is '{p.get('state')}', not on hold – only a paused project can be resumed."
    today = as_of()
    restart = _to_date(restart_date) if restart_date not in (None, "") else today
    if restart is None:
        return f"{INPUT_PREFIX} restart_date '{restart_date}' is not a date."
    if restart < today:
        return f"{INPUT_PREFIX} The restart date {restart:%Y-%m-%d} lies before the reference date {today:%Y-%m-%d}."
    end = _to_date(p.get("planned_end_date"))
    if end is None:
        return f"ERROR: No planned_end_date recorded for {pid}."
    returning = _num(fte, float("nan")) if fte is not None else float("nan")
    before_hold = base_fte(pid) or _num(p.get("current_fte"))
    if fte is not None and (math.isnan(returning) or returning < 0):
        return f"{INPUT_PREFIX} fte must be zero or a positive number."
    if math.isnan(returning):
        returning = before_hold

    rules = current_rules()
    hold = _hold_fact(pid)
    planned_restart = _to_date(hold.get("restart")) if hold else None
    planned_restart = planned_restart or restart
    shift_days = (restart - planned_restart).days
    burn = _monthly_burn(p)
    # a hold simulated in this scenario already paid its ramp-up; a hold recorded in the source has not
    restart_cost = 0.0 if hold else burn * rules.hold_restart_ramp_months
    standby = _num(hold.get("standby_fte")) if hold else 0.0
    standby_delta = standby * rules.fte_monthly_cost_eur * shift_days / DAYS_PER_MONTH
    extra = restart_cost + standby_delta
    new_end = end + timedelta(days=shift_days)
    state = _state_before_hold(pid)

    record_change(pid, "state", state, "simulate_resume", f"restart {restart:%Y-%m-%d}")
    if shift_days:
        record_change(pid, "planned_end_date", new_end.isoformat(), "simulate_resume",
                      f"restart {abs(shift_days)} days {'later' if shift_days > 0 else 'earlier'} than planned")
    changed_team = abs(returning - before_hold) >= 0.05
    if changed_team:
        record_change(pid, "allocated_fte", round(returning, 2), "simulate_resume", "restart team",
                      table="Resource_Plans", scenario_field="current_fte")
    lines = [f"RESULT simulate_resume | {pid} ({p['project_name']}) resumes on {restart:%Y-%m-%d} as '{state}'",
             f"- Planned restart: {planned_restart:%Y-%m-%d}" + (" (from the hold of this scenario)" if hold else
                                                                 " (no planned restart recorded – none assumed)"),
             f"- Restart team: {returning:g} FTE (before the hold: {before_hold:g} FTE)"]
    if hold:
        lines.append("- Restart ramp-up: already costed in the hold of this scenario.")
    else:
        lines.append(f"- Restart ramp-up: {rules.hold_restart_ramp_months:g} month(s) × burn {_eur(burn)} = "
                     f"{_eur(restart_cost)}")
    if shift_days and standby:
        lines.append(f"- Standby team ({standby:g} FTE) {'longer' if shift_days > 0 else 'shorter'} by "
                     f"{abs(shift_days) / DAYS_PER_MONTH:.1f} months: {_eur(standby_delta)}")
    lines.append(f"- Planned end: {end:%Y-%m-%d} -> {new_end:%Y-%m-%d}")
    if abs(extra) >= 0.5:
        lines += _delay_forecast(p, extra, "simulate_resume")
    factor = returning / before_hold if before_hold else 1.0
    for team, load in _team_load(pid, restart, factor):
        lines.append(f"- Utilisation {team} after the restart: {load:.0f} %")
        if load > rules.overload_alert_pct:
            lines.append(f"  ⚠ {team} becomes overloaded (> {rules.overload_alert_pct:g} %) – delivery at risk.")
            record_fact("overload", project=pid, utilisation_pct=round(load, 1))
    record_fact("resume", project=pid, restart=restart.isoformat(), fte=round(returning, 2),
                fte_before=round(before_hold, 2), fte_changed=changed_team, extra_cost=round(extra, 2))
    lines += indicative_note(pid)
    if shift_days:
        lines += schedule_effects(p, end, new_end, shift_days / DAYS_PER_MONTH, max(extra, 0.0))
    return "\n".join(lines)


# =========================================================================== A5 cost stress test
def _scope_mask(df: pd.DataFrame, scope: str) -> pd.Series:
    wanted = scope.strip().casefold()
    mask = pd.Series(False, index=df.index)
    for col in ("portfolio", "business_unit", "project_number"):
        if col in df.columns:
            mask |= df[col].astype(str).str.strip().str.casefold() == wanted
    return mask


def _overrun_class(eac: float, budget: float, rules) -> str:
    overrun = eac - budget
    if overrun < rules.overrun_tolerance_eur:
        return "within budget"
    return "NO-GO" if (eac / budget - 1) * 100 > rules.eac_overrun_nogo_pct else "CONDITIONAL"


@_safe
def cost_stress(pct: float, cost_type: str = "all", scope: str | None = None) -> str:
    rules = current_rules()
    rise = _num(pct, float("nan"))
    if math.isnan(rise) or rise <= 0:
        return f"{INPUT_PREFIX} pct must be a positive cost increase in percent."
    if rise > rules.max_budget_change_pct:
        return f"ERROR: A cost increase of {rise:g} % exceeds the limit of {rules.max_budget_change_pct:.0f} %."
    kind = str(cost_type or "all").strip().lower()
    if kind == "total":
        kind = "all"
    if kind not in COST_STRESS_TYPES:
        return f"{INPUT_PREFIX} cost_type '{cost_type}' invalid – allowed: all, opex, capex."
    projects = get_scenario_df()
    if scope and str(scope).strip():
        mask = _scope_mask(projects, str(scope))
        if not mask.any():
            options = sorted(set(projects.get("portfolio", pd.Series(dtype=str)).dropna().astype(str))
                             | set(projects.get("business_unit", pd.Series(dtype=str)).dropna().astype(str)))
            return f"{INPUT_PREFIX} Scope '{scope}' matches no portfolio or business unit. Available: {', '.join(options)}."
        projects = projects[mask]

    rows, excluded = [], []
    for p in projects.to_dict(orient="records"):
        pid = str(p["project_number"])
        if str(p.get("state")) in CLOSED_PROJECT_STATES:
            continue
        if blocked_reason(pid):
            excluded.append(f"{pid} (quality gate)")
            continue
        budget, eac = _num(p.get("budget_cost_eur")), _num(p.get("estimate_at_completion_eur"))
        if budget <= 0 or eac <= 0:
            excluded.append(f"{pid} (no budget or EAC)")
            continue
        etc = _opt_num(p.get("estimate_to_completion_eur"))
        remaining = etc if etc is not None else max(budget - _num(p.get("actual_cost_eur")), 0.0)
        capex, opex = _num(p.get("planned_capital_eur")), _num(p.get("planned_operating_eur"))
        share = capex / (capex + opex) if capex + opex > 0 else 0.5
        part = remaining * {"all": 1.0, "capex": share, "opex": 1 - share}[kind]
        extra = part * rise / 100
        new_eac = eac + extra
        rows.append({"pid": pid, "name": p.get("project_name"), "extra": extra, "budget": budget, "eac": eac,
                     "new_eac": new_eac, "before": _overrun_class(eac, budget, rules),
                     "after": _overrun_class(new_eac, budget, rules), "after_pct": (new_eac / budget - 1) * 100})
    if not rows:
        return f"{INPUT_PREFIX} No project in scope can be stress-tested (all closed, blocked or without EAC)."
    rows.sort(key=lambda r: -r["after_pct"])
    flips = [r["pid"] for r in rows if r["after"] == "NO-GO" and r["before"] != "NO-GO"]
    breaching = [r["pid"] for r in rows if r["after"] == "NO-GO"]
    total = sum(r["extra"] for r in rows)
    label = {"all": "remaining cost", "opex": "remaining OpEx", "capex": "remaining CapEx"}[kind]
    lines = [f"RESULT simulate_cost_stress | +{rise:g} % on the {label} of {len(rows)} project(s)"
             f"{' in ' + str(scope) if scope else ''} – extra need {_eur(total)}",
             "- Ranking by forecast overrun after the increase (budget is unchanged; nothing is written):"]
    for rank, r in enumerate(rows, start=1):
        flip = " ⚠ tips to NO-GO" if r["pid"] in flips else ""
        lines.append(f"  {rank}. {r['pid']} ({r['name']}) – EAC {_eur(r['eac'])} -> {_eur(r['new_eac'])} vs. budget "
                     f"{_eur(r['budget'])} ({r['after_pct']:+.1f} %): {r['before']} -> {r['after']}{flip}")
    if excluded:
        lines.append(f"- Excluded: {', '.join(excluded)}")
    lines.append(f"- {len(flips)} project(s) would newly breach the forecast-overrun rule "
                 f"({rules.eac_overrun_nogo_pct:g} %); {len(breaching)} would be above it in total.")
    record_fact("cost_stress", pct=rise, cost_type=kind, scope=scope or None, projects=len(rows),
                extra=round(total, 2), flips=flips, breaching=breaching)
    return "\n".join(lines)


# =========================================================================== A2 re-baselining
def _baseline_history(pid: str, p: dict[str, Any]) -> list[dict[str, Any]]:
    """Baseline versions of the project (scenario rows included), oldest first. Without a recorded value, version 1
    stands for the approved budget and end date of the source."""
    rows = table_with_pending("Baselines", pid)
    history = []
    for i, r in enumerate(rows.to_dict(orient="records"), start=1):
        history.append({**r, "version": int(_num(r.get("version"), i))})
    history.sort(key=lambda r: r["version"])
    original_budget = _num(_original_value(pid, "budget_cost_eur")) or _num(p.get("budget_cost_eur"))
    original_end = (_to_date(_original_value(pid, "approved_end_date")) or _to_date(_original_value(pid, "planned_end_date"))
                    or _to_date(p.get("planned_end_date")))
    if not history:
        history = [{"version": 1, "baseline_budget_eur": original_budget, "baseline_end_date": original_end,
                    "created_date": None, "implicit": True}]
    for r in history:
        if _opt_num(r.get("baseline_budget_eur")) is None:
            r["baseline_budget_eur"] = original_budget
        if _to_date(r.get("baseline_end_date")) is None:
            r["baseline_end_date"] = original_end
    return history


def _original_value(pid: str, column: str) -> Any:
    projects = get_dataset().projects
    row = projects[projects["project_number"].astype(str) == pid]
    return row[column].iloc[0] if not row.empty and column in row.columns else None


def _months(a: date, b: date) -> float:
    return (b - a).days / DAYS_PER_MONTH


@_safe
def rebaseline(project_id: str, new_budget_eur: float | None = None, new_end_date: Any = None, reason: str = "") -> str:
    p = _open_project(project_id, needs=BUDGET_FIELDS)
    if isinstance(p, str):
        return p
    pid = str(p["project_number"])
    reason = str(reason or "").strip()
    if not reason:
        return f"{INPUT_PREFIX} A re-baseline needs a reason (why the current plan is no longer realistic)."
    budget = _num(p.get("budget_cost_eur"))
    new_budget = _opt_num(new_budget_eur) if new_budget_eur not in (None, "") else None
    if new_budget is not None and new_budget <= 0:
        return f"{INPUT_PREFIX} new_budget_eur must be a positive amount."
    end = _to_date(p.get("planned_end_date"))
    new_end = _to_date(new_end_date) if new_end_date not in (None, "") else None
    if new_end_date not in (None, "") and new_end is None:
        return f"{INPUT_PREFIX} new_end_date '{new_end_date}' is not a date."
    budget_moves = new_budget is not None and abs(new_budget - budget) >= 0.5
    end_moves = new_end is not None and new_end != end
    if not (budget_moves or end_moves):
        return f"{INPUT_PREFIX} A re-baseline must change the budget or the end date of {pid}."
    if end_moves and new_end <= as_of():
        return f"{INPUT_PREFIX} The new end date {new_end:%Y-%m-%d} lies before the reference date."

    rules = current_rules()
    history = _baseline_history(pid, p)
    first, current = history[0], history[-1]
    version = max(r["version"] for r in history) + 1
    lines = [f"RESULT simulate_rebaseline | {pid} ({p['project_name']}) – baseline version {version}",
             f"- Reason: {reason}"]
    final_budget = budget
    if budget_moves:
        delta = new_budget - budget
        error = _limit_error(pid, delta, budget)
        if error:
            return error
        r = _budget_delta(p, delta, "total")
        if isinstance(r, str):
            return r
        _record_budget(r, "simulate_rebaseline", f"re-baseline v{version}")
        final_budget = new_budget
        lines.append(f"- Budget: {_eur(budget)} -> {_eur(new_budget)}")
        lines += [f"  {w}" for w in r["warnings"]]
    final_end = new_end if end_moves else end
    if end_moves:
        record_change(pid, "planned_end_date", new_end.isoformat(), "simulate_rebaseline", f"re-baseline v{version}")
        record_change(pid, "approved_end_date", new_end.isoformat(), "simulate_rebaseline", f"re-baseline v{version}")
        lines.append(f"- End date: {end:%Y-%m-%d} -> {new_end:%Y-%m-%d}")

    b1, bc = _num(first["baseline_budget_eur"]), _num(current["baseline_budget_eur"])
    e1, ec = _to_date(first["baseline_end_date"]), _to_date(current["baseline_end_date"])
    var_b1 = (final_budget - b1) / b1 * 100 if b1 else 0.0
    var_bc = (final_budget - bc) / bc * 100 if bc else 0.0
    var_m1 = _months(e1, final_end) if e1 and final_end else 0.0
    var_mc = _months(ec, final_end) if ec and final_end else 0.0
    start = _to_date(p.get("actual_start_date")) or _to_date(p.get("planned_start_date"))
    duration = _months(start, e1) if start and e1 and e1 > start else 0.0
    var_m1_pct = var_m1 / duration * 100 if duration else 0.0
    eac = _num(p.get("estimate_at_completion_eur"))
    lines += [
        f"- Variance vs. original baseline (v1): budget {_eur(final_budget - b1)} ({var_b1:+.1f} %), "
        f"end {var_m1:+.1f} months" + (f" ({var_m1_pct:+.1f} % of the planned duration)" if duration else ""),
        f"- Variance vs. current baseline (v{current['version']}): budget {_eur(final_budget - bc)} ({var_bc:+.1f} %), "
        f"end {var_mc:+.1f} months",
        f"- Forecast vs. new baseline: EAC {_eur(eac)} – gap {_eur(eac - final_budget)}" if eac else
        "- ⚠ No estimate at completion (EAC) recorded – the new baseline cannot be checked against the forecast.",
    ]
    year_ago = as_of() - timedelta(days=365)
    recent = [r for r in history if r["version"] >= 2 and (d := _to_date(r.get("created_date"))) is not None
              and year_ago <= d <= as_of()]
    count = len(recent) + 1
    deviation = max(abs(var_b1), abs(var_m1_pct))
    board = bool(rules.rebaseline_board_threshold_pct) and deviation > rules.rebaseline_board_threshold_pct
    too_many = count > rules.max_rebaselines_per_year
    lines.append(f"- Re-baselines of {pid} in the last 12 months incl. this one: {count} "
                 f"(limit {rules.max_rebaselines_per_year})")
    if board:
        lines.append(f"⚠ Cumulative deviation from the original baseline {deviation:.1f} % exceeds "
                     f"{rules.rebaseline_board_threshold_pct:g} % – board approval required.")
    number = f"BL-{pid}-V{version:02d}"
    row = {"baseline_number": number, "project_number": pid, "name": f"Version {version}", "description": reason,
           "created_date": as_of().isoformat(), "version": version, "baseline_budget_eur": round(final_budget, 2),
           "baseline_end_date": final_end.isoformat() if final_end else None,
           "baseline_eac_eur": round(eac, 2) if eac else None, "reason": reason, "approved_by": None,
           "approval_date": None}
    record_row_change("Baselines", pid, number, "simulate_rebaseline", row=row,
                      summary=f"Version {version}: budget {_eur(final_budget)}, end {final_end:%Y-%m-%d}"
                      if final_end else f"Version {version}: budget {_eur(final_budget)}", note=reason)
    record_fact("rebaseline", project=pid, version=version, budget_variance_pct=round(var_b1, 1),
                schedule_variance_months=round(var_m1, 1), deviation_pct=round(deviation, 1), board=board,
                count=count, limit=rules.max_rebaselines_per_year, too_many=too_many)
    if end_moves:
        lines += schedule_effects(p, end, new_end, _months(end, new_end), 0.0)
    return "\n".join(lines)


# =========================================================================== A4 stage gates
@_safe
def advance_phase(project_id: str, target_phase: str | None = None) -> str:
    # the gate itself checks data quality (mandatory criterion), so a blocked project is checked, not refused
    p = _open_project(project_id, allow_blocked=True)
    if isinstance(p, str):
        return p
    pid = str(p["project_number"])
    current = gate_checks.normalize_phase(p.get("phase"))
    if current is None:
        return (f"{INPUT_PREFIX} The phase '{p.get('phase')}' of {pid} is not a stage-gate phase "
                f"({', '.join(PHASES)}) – correct the master data first.")
    index = PHASES.index(current)
    if index == len(PHASES) - 1:
        return f"{INPUT_PREFIX} {pid} is already '{current}' – there is no further gate."
    target = gate_checks.normalize_phase(target_phase) if target_phase else PHASES[index + 1]
    if target is None:
        return f"{INPUT_PREFIX} '{target_phase}' is not a stage-gate phase ({', '.join(PHASES)})."
    if PHASES.index(target) <= index:
        return f"{INPUT_PREFIX} {pid} cannot go back from '{current}' to '{target}' – gates only lead forward."
    if PHASES.index(target) > index + 1:
        return (f"{INPUT_PREFIX} {pid} cannot skip a phase ('{current}' -> '{target}'); the next gate is "
                f"'{PHASES[index + 1]}'.")
    results = gate_checks.evaluate(p, current, target)
    mandatory = [r for r in results if r.kind == gate_checks.MANDATORY and not r.passed]
    advisory = [r for r in results if r.kind == gate_checks.ADVISORY and not r.passed]

    lines = [f"RESULT simulate_stage_gate | {pid} ({p['project_name']}) – gate {current} -> {target}",
             "| Criterion | Kind | Met | Value |", "|---|---|---|---|"]
    lines += [f"| {r.label} | {r.kind} | {'yes' if r.passed else 'no'} | {r.value} |" for r in results]
    if not results:
        lines.append("- No criteria configured for this transition.")
    record_change(pid, "phase", target, "simulate_stage_gate", f"stage gate {current} -> {target}")
    approvals = table_with_pending("Approvals", pid)
    iteration = int(pd.to_numeric(approvals.get("iteration"), errors="coerce").max()) + 1 \
        if not approvals.empty and "iteration" in approvals.columns and approvals["iteration"].notna().any() else 1
    number = f"APR-{pid}-G{iteration:02d}"
    row = {"approval_number": number, "project_number": pid, "state": "Approved", "approver": None,
           "approved_by": None, "approval_date": None,
           "short_description": f"Stage gate {current} -> {target}", "updated": as_of().isoformat(),
           "iteration": iteration}
    record_row_change("Approvals", pid, number, "simulate_stage_gate", row=row,
                      summary=f"Stage gate {current} -> {target} (iteration {iteration})")
    if mandatory:
        record_fact("gate_mandatory_failed", project=pid, transition=gate_checks.transition(current, target),
                    criteria=[f"{r.label} ({r.value})" for r in mandatory])
        lines.append(f"⛔ Mandatory criteria not met: {', '.join(r.label for r in mandatory)}.")
    for r in advisory:
        record_fact("gate_advisory_failed", project=pid, successor=r.criterion, criterion=r.label, value=r.value)
        lines.append(f"⚠ Advisory criterion not met: {r.label} ({r.value}).")
    record_fact("stage_gate", project=pid, transition=gate_checks.transition(current, target),
                passed=not mandatory)
    return "\n".join(lines)


# =========================================================================== A6 management reserve
def reserve_balance() -> float:
    """Start amount + every booking of the source (portfolio total, also in a filtered view) + this scenario's rows."""
    pending = sum(_num(r.get("amount_eur")) for r in pending_rows("Reserve_Ledger"))
    return current_rules().management_reserve_eur + get_dataset().reserve_movement() + pending


def _reserve_number(pid: str) -> str:
    stamp = as_of().strftime("%Y%m%d")
    taken = set(table_with_pending("Reserve_Ledger").get("entry_number", pd.Series(dtype=str)).astype(str))
    n = 1
    while f"RES-{stamp}-{pid}-{n}" in taken:
        n += 1
    return f"RES-{stamp}-{pid}-{n}"


def _book_reserve(p: dict[str, Any], amount: float, reason: str, tool_name: str, kind: str) -> tuple[list[str], str | None]:
    """Books a draw (kind 'draw', budget +amount) or a return (kind 'return', budget −amount) against the reserve.
    Returns (report lines, error)."""
    rules = current_rules()
    pid = str(p["project_number"])
    balance = reserve_balance()
    draw = kind == "draw"
    if draw and amount > balance + 0.005:
        return [], (f"ERROR: Management reserve insufficient – balance {_eur(balance)}, requested {_eur(amount)} "
                    f"for {pid}.")
    error = _limit_error(pid, amount, _num(p.get("budget_cost_eur")))
    if error:
        return [], error
    r = _budget_delta(p, amount if draw else -amount, "total")
    if isinstance(r, str):
        return [], r
    _record_budget(r, tool_name, f"management reserve {kind}: {reason}")
    after = balance - amount if draw else balance + amount
    number = _reserve_number(pid)
    row = {"entry_number": number, "date": as_of().isoformat(), "project_number": pid,
           "amount_eur": round(-amount if draw else amount, 2), "type": kind, "reason": reason, "approved_by": None,
           "proposal_id": None, "balance_after_eur": round(after, 2)}
    record_row_change("Reserve_Ledger", pid, number, tool_name, row=row,
                      summary=f"{kind.capitalize()} {_eur(amount)} (balance after {_eur(after)})", note=reason)
    floor = rules.management_reserve_eur * rules.reserve_floor_pct / 100
    lines = [f"- Management reserve: {_eur(balance)} -> {_eur(after)} (start {_eur(rules.management_reserve_eur)}, "
             f"floor {_eur(floor)})",
             f"- Budget {pid}: {_eur(r['budget'])} -> {_eur(r['new_budget'])}"]
    lines += [f"  {w}" for w in r["warnings"]]
    if draw and after < floor:
        lines.append(f"⚠ The reserve falls below its floor of {rules.reserve_floor_pct:g} % ({_eur(floor)}).")
        record_fact("reserve_floor", project=pid, balance=round(after, 2), floor=round(floor, 2))
    record_fact("reserve_booking", project=pid, kind=kind, amount=round(amount, 2), balance_after=round(after, 2))
    return lines, None


@_safe
def reserve_draw(project_id: str, amount_eur: float, reason: str = "") -> str:
    p = _open_project(project_id, needs=BUDGET_FIELDS)
    if isinstance(p, str):
        return p
    amount = _num(amount_eur, float("nan"))
    if math.isnan(amount) or amount <= 0:
        return f"{INPUT_PREFIX} amount_eur must be a positive amount."
    reason = str(reason or "").strip()
    if not reason:
        return f"{INPUT_PREFIX} A draw on the management reserve needs a reason."
    lines, error = _book_reserve(p, amount, reason, "simulate_reserve_draw", "draw")
    if error:
        return error
    return "\n".join([f"RESULT simulate_reserve_draw | {_eur(amount)} from the management reserve to "
                      f"{p['project_number']} ({p['project_name']})", f"- Reason: {reason}", *lines])


@_safe
def reserve_return(project_id: str, amount_eur: float, reason: str = "") -> str:
    p = _open_project(project_id, allow_closed=True, needs=BUDGET_FIELDS)
    if isinstance(p, str):
        return p
    amount = _num(amount_eur, float("nan"))
    if math.isnan(amount) or amount <= 0:
        return f"{INPUT_PREFIX} amount_eur must be a positive amount."
    reason = str(reason or "").strip() or "unused budget returned"
    lines, error = _book_reserve(p, amount, reason, "simulate_reserve_return", "return")
    if error:
        return error
    return "\n".join([f"RESULT simulate_reserve_return | {_eur(amount)} from {p['project_number']} "
                      f"({p['project_name']}) back to the management reserve", f"- Reason: {reason}", *lines])


# =========================================================================== A7 risk response
def _probability(level: str) -> float:
    rules = current_rules()
    return {"Low": rules.risk_probability_low_pct, "Moderate": rules.risk_probability_moderate_pct,
            "High": rules.risk_probability_high_pct}.get(level, rules.risk_probability_moderate_pct) / 100


def _impact(level: str) -> float:
    rules = current_rules()
    return {"Low": rules.risk_impact_low_eur, "Moderate": rules.risk_impact_moderate_eur,
            "High": rules.risk_impact_high_eur}.get(level, rules.risk_impact_moderate_eur)


def _level(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return next((lvl for lvl in LEVELS if lvl.casefold() == text), None)


def risk_exposure(probability: Any, impact: Any) -> float:
    """Exposure in € = probability (risk scale) × impact (risk scale). Unknown levels count as Moderate."""
    return _probability(_level(probability) or "Moderate") * _impact(_level(impact) or "Moderate")


def _exposure_after(response: str, probability: str, impact: str) -> float:
    if response in ("Avoid", "Transfer"):  # the project no longer carries the risk (scope change / insurer, supplier)
        return 0.0
    return risk_exposure(probability, impact)


def project_exposure(pid: str) -> float:
    risks = get_table("Risks", pid)
    total = 0.0
    for r in risks.to_dict(orient="records"):
        key = str(r.get("risk_number"))
        status = str(row_value("Risks", key, "risk_status") or "")
        total += _exposure_after(status if status in ("Avoid", "Transfer") and _responded(key) else "Accept",
                                 row_value("Risks", key, "probability"), r.get("impact"))
    return total


def _responded(key: str) -> bool:
    return any(c.get("table") == "Risks" and str(c.get("key")) == key for c in get_ledger())


@_safe
def risk_response(risk_number: str, response: str, cost_eur: float = 0.0, new_probability: str | None = None,
                  funding: str = "project") -> str:
    ref = str(risk_number or "").strip()
    risks = get_table("Risks")
    match = risks[risks["risk_number"].astype(str).str.casefold() == ref.casefold()] if not risks.empty else risks
    if match.empty:
        return f"{INPUT_PREFIX} Risk '{ref}' not found or no permission."
    risk = match.iloc[0].to_dict()
    key, pid = str(risk["risk_number"]), str(risk["project_number"])
    answer = next((r for r in RESPONSES if r.casefold() == str(response or "").strip().casefold()), None)
    if answer is None:
        return f"{INPUT_PREFIX} response '{response}' invalid – allowed: {', '.join(RESPONSES)}."
    cost = _num(cost_eur, float("nan"))
    if math.isnan(cost) or cost < 0:
        return f"{INPUT_PREFIX} cost_eur must be zero or a positive amount."
    source = str(funding or "project").strip().lower()
    if source not in ("project", "reserve"):
        return f"{INPUT_PREFIX} funding must be 'project' or 'reserve'."
    p = _open_project(pid, needs=BUDGET_FIELDS if cost > 0 else ())
    if isinstance(p, str):
        return p
    probability = _level(row_value("Risks", key, "probability")) or "Moderate"
    impact = _level(risk.get("impact")) or "Moderate"
    if new_probability not in (None, ""):
        target = _level(new_probability)
        if target is None:
            return f"{INPUT_PREFIX} new_probability must be one of {', '.join(LEVELS)}."
    elif answer == "Mitigate":
        target = LEVELS[max(LEVELS.index(probability) - 1, 0)]
    else:
        target = probability
    if answer == "Accept" and cost > 0:
        return f"{INPUT_PREFIX} Accepting a risk costs nothing up front – use Mitigate, Avoid or Transfer for a response with cost."
    project_before = project_exposure(pid)
    before = risk_exposure(probability, impact)
    after = 0.0 if answer in ("Avoid", "Transfer") else risk_exposure(target, impact)
    reduction = before - after
    rules = current_rules()

    lines = [f"RESULT simulate_risk_response | {key} ({pid}): {answer}"
             + (f" for {_eur(cost)} ({'management reserve' if source == 'reserve' else 'project budget'})" if cost else ""),
             f"- Risk: {risk.get('short_description')} – probability {probability} -> {target}, impact {impact}",
             f"- Exposure: {_eur(before)} -> {_eur(after)} (reduction {_eur(reduction)})"]
    if cost > 0:
        if source == "reserve":
            booked, error = _book_reserve(p, cost, f"risk response {key}", "simulate_risk_response", "draw")
        else:
            r = _budget_delta(p, cost, "total")
            if isinstance(r, str):
                return r
            _record_budget(r, "simulate_risk_response", f"risk response {key}")
            booked, error = [f"- Budget {pid}: {_eur(r['budget'])} -> {_eur(r['new_budget'])}",
                             *[f"  {w}" for w in r["warnings"]]], None
        if error:
            return error
        lines += booked
        ratio = reduction / cost
        lines.append(f"- Exposure reduction per € spent: {ratio:.2f} (minimum {rules.risk_response_min_benefit_ratio:g})")
        if ratio < rules.risk_response_min_benefit_ratio:
            lines.append("⚠ The response costs more than the exposure it removes.")
            record_fact("risk_response_low_benefit", project=pid, successor=key, cost=round(cost, 2),
                        reduction=round(reduction, 2), ratio=round(ratio, 2))
    record_row_change("Risks", pid, key, "simulate_risk_response", field="risk_status", new_value=answer)
    record_row_change("Risks", pid, key, "simulate_risk_response", field="mitigation",
                      new_value=f"{answer} decided {as_of():%Y-%m-%d}" + (f" ({_eur(cost)})" if cost else ""))
    if target != probability:
        record_row_change("Risks", pid, key, "simulate_risk_response", field="probability", new_value=target)
    if answer == "Accept" and probability == "High" and impact == "High":
        lines.append("⚠ A high/high risk is accepted – the risk owner signs off.")
        record_fact("risk_accept_high", project=pid, successor=key, owner=risk.get("assigned_to"))
    project_after = project_exposure(pid)
    lines.append(f"- Total risk exposure of {pid}: {_eur(project_before)} -> {_eur(project_after)}")
    record_fact("risk_response", project=pid, successor=key, response=answer, exposure_before=round(before, 2),
                exposure_after=round(after, 2), cost=round(cost, 2))
    return "\n".join(lines)


# =========================================================================== agent tools
@tool("simulate_resume")
def simulate_resume(project_id: str, restart_date: str = "", fte: float = -1.0) -> str:
    """Resumes a project that is ON HOLD (the counterpart of simulate_on_hold): restart date (default: today, never
    in the past), restart ramp-up cost, new end date, returning team (fte; -1 = the team before the hold) and its
    effect on team utilisation and successor deadlines."""
    if skipped := repeated_call("simulate_resume", project_id):
        return skipped
    return resume(project_id, restart_date or None, None if fte is None or fte < 0 else fte)


@tool("simulate_cost_stress")
def simulate_cost_stress(pct: float, cost_type: str = "all", scope: str = "") -> str:
    """Portfolio cost stress test ('what if staff costs rise by 8 %?'): raises the REMAINING cost of every project
    in scope by pct (cost_type all/opex/capex; scope = a portfolio or business unit, empty = all) and ranks which
    projects would breach the forecast-overrun rule. Writes nothing."""
    if skipped := repeated_call("simulate_cost_stress", ""):
        return skipped
    return cost_stress(pct, cost_type, scope or None)


@tool("simulate_rebaseline")
def simulate_rebaseline(project_id: str, reason: str, new_budget_eur: float = 0.0, new_end_date: str = "") -> str:
    """Re-baselines a project: a new approved budget (new_budget_eur, 0 = unchanged) and/or end date (new_end_date,
    YYYY-MM-DD) with a mandatory reason. Reports the variance against the original and the current baseline and
    adds a new baseline version (four-eyes)."""
    if skipped := repeated_call("simulate_rebaseline", project_id):
        return skipped
    return rebaseline(project_id, new_budget_eur or None, new_end_date or None, reason)


@tool("simulate_stage_gate")
def simulate_stage_gate(project_id: str, target_phase: str = "") -> str:
    """Checks the stage gate of a project into its next phase (Initiation -> Planning -> Executing -> Closing ->
    Closed; target_phase empty = the next one): every configured criterion with its value; mandatory criteria
    decide, advisory ones become conditions."""
    if skipped := repeated_call("simulate_stage_gate", project_id):
        return skipped
    return advance_phase(project_id, target_phase or None)


@tool("simulate_reserve_draw")
def simulate_reserve_draw(project_id: str, amount_eur: float, reason: str) -> str:
    """Draws amount_eur from the portfolio's management reserve into the project's budget (instead of taking it from
    another project). Checks the reserve balance, its floor, the board threshold and the forecast."""
    if skipped := repeated_call("simulate_reserve_draw", project_id):
        return skipped
    return reserve_draw(project_id, amount_eur, reason)


@tool("simulate_reserve_return")
def simulate_reserve_return(project_id: str, amount_eur: float, reason: str = "") -> str:
    """Returns amount_eur of a project's budget to the management reserve (counter-booking of a draw)."""
    if skipped := repeated_call("simulate_reserve_return", project_id):
        return skipped
    return reserve_return(project_id, amount_eur, reason)


@tool("simulate_risk_response")
def simulate_risk_response(risk_number: str, response: str, cost_eur: float = 0.0, new_probability: str = "",
                           funding: str = "project") -> str:
    """Decides the response to a risk (RSK-...): Mitigate, Accept, Avoid or Transfer, with its cost (cost_eur),
    the probability afterwards (Low/Moderate/High, default one level lower for Mitigate) and the funding (project
    budget or management reserve). Reports exposure before/after and the cost-benefit ratio."""
    if not first_call("simulate_risk_response", str(risk_number).strip().upper()):  # keyed by risk, not project
        return (f"SKIPPED: simulate_risk_response already ran for {risk_number} in this step – the repeated call was "
                "dropped, the first result stands.")
    return risk_response(risk_number, response, cost_eur, new_probability or None, funding)


PMO_TOOLS = [simulate_resume, simulate_cost_stress, simulate_rebaseline, simulate_stage_gate, simulate_reserve_draw,
             simulate_reserve_return, simulate_risk_response]
