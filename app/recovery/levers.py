"""Stage 3a of the red trigger: the lever catalogue. Rule-based – the code chooses donors, amounts and combinations;
an LLM never does. Every candidate is a list of guided scenarios (app/crew/scenarios.build_guided_scenario), so it uses
exactly the parameters of the Steering Console and can be opened there 1:1.

| Cause       | Lever                         | Scenario(s)                                    | Parameters chosen by the code            |
|-------------|-------------------------------|------------------------------------------------|------------------------------------------|
| cost        | draw on the reserve           | reserve_draw                                   | the gap, capped by reserve minus floor   |
| cost        | move budget (partial)         | budget_transfer donor -> red                   | min(underspend of the donor, gap)        |
| cost        | re-baseline                   | rebaseline                                     | new budget = forecast                    |
| cost        | reduce scope                  | schedule (scope_change_pct)                    | share of the remaining cost that closes  |
|             |                               |                                                | the gap, capped by recovery_max_scope_cut|
| cost        | stop a donor                  | cancel donor, free budget to the red project   | lowest priority, not protected           |
| schedule    | move capacity                 | resource_transfer donor -> red + schedule      | 1 FTE of a shared team, donor stays below|
|             |                               | (capacity +x %)                                | overload_alert_pct                       |
| schedule    | re-plan a successor           | schedule (months) on the successor             | only when its required-by date breaks    |
| schedule    | move the end date, honestly   | rebaseline (end date)                          | the expected end                         |
| resources   | pause a competing project     | on_hold                                        | lowest priority in the overloaded team   |
| scope/risk  | treat a risk                  | risk_response Mitigate                         | top accepted risks; only with a cost     |
| predecessor | the same levers on a red      | as above, one level                            |                                          |
|             | direct predecessor            |                                                |                                          |

Donors: never red themselves, never blocked by the Data Steward, never protected (priority up to
cut_protected_max_priority), never of a higher priority than the red project; chosen by lowest priority first, then
lowest value score, then lowest "risk of not performing" (the order of the portfolio cut). A candidate with missing
figures is listed as not possible, never silently skipped."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from app.core.business_rules import BusinessRules
from app.recovery.diagnosis import COST, RESOURCES, SCHEDULE, SCOPE_RISK, Cause

RESERVE, TRANSFER, REBASELINE, SCOPE_CUT, DONOR_STOP = "reserve", "transfer", "rebaseline", "scope_cut", "donor_stop"
CAPACITY, SUCCESSOR, END_SHIFT, COMPETITOR_HOLD, RISK = "capacity", "successor_replan", "end_shift", "competitor_hold", "risk"
LEVER_LABELS = {
    RESERVE: "Draw on the management reserve", TRANSFER: "Move unused budget", REBASELINE: "Re-baseline the budget",
    SCOPE_CUT: "Reduce scope", DONOR_STOP: "Stop a lower-priority project", CAPACITY: "Move team capacity",
    SUCCESSOR: "Re-plan a successor", END_SHIFT: "Move the end date (honest re-baseline)",
    COMPETITOR_HOLD: "Pause a competing project", RISK: "Mitigate a risk",
}
CATALOGUE = tuple(LEVER_LABELS)
REASON = "Recovery measure proposed by the red trigger"
CLOSED = ("closed", "cancelled", "canceled", "complete", "completed")


@dataclass
class Candidate:
    id: str
    lever: str
    cause: str
    project: str  # the project the measure helps (the red one, or its red predecessor)
    steps: list[dict[str, Any]]  # guided scenarios: {"action", "project", "target", "params"}
    rationale: str
    partial: bool = False  # closes only part of the gap; counts in a package only
    donors: list[str] = field(default_factory=list)
    touches_others: bool = False  # changes a project other than `project`
    money_to_project: float = 0.0  # budget the measure moves to `project`

    @property
    def label(self) -> str:
        return LEVER_LABELS.get(self.lever, self.lever)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "label": self.label}


@dataclass
class NotPossible:
    lever: str
    cause: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "label": LEVER_LABELS.get(self.lever, self.lever)}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _step(action: str, project: str, target: str | None = None, **params: Any) -> dict[str, Any]:
    return {"action": action, "project": project, "target": target, "params": params}


def donor_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lowest priority first (highest number), then lowest value score, then lowest risk of not performing."""
    from app.crew.tools.portfolio_tools import rnp_severity

    return sorted(rows, key=lambda r: (-(_num(r.get("priority")) or 0), _num(r.get("value_score")) or 0,
                                       rnp_severity(r.get("risk_of_not_performing")), str(r.get("project_number"))))


def eligible_donors(projects: pd.DataFrame, red_row: dict[str, Any], red: set[str], blocked: set[str],
                    rules: BusinessRules, strictly_lower: bool = False) -> list[dict[str, Any]]:
    own = _num(red_row.get("priority"))
    out = []
    for row in projects.to_dict("records"):
        pid = str(row.get("project_number"))
        prio = _num(row.get("priority"))
        if pid == str(red_row.get("project_number")) or pid in red or pid in blocked:
            continue
        if str(row.get("state") or "").lower() in CLOSED or str(row.get("state") or "") == "On Hold":
            continue
        if prio is None or prio <= rules.cut_protected_max_priority:  # missing priority never makes a donor
            continue
        if own is not None and (prio < own or (strictly_lower and prio == own)):
            continue
        out.append(row)
    return donor_order(out)


def _team_rows(resource_plans: pd.DataFrame, today: Any) -> pd.DataFrame:
    from app.recovery.impact import teams_of

    rows = teams_of(resource_plans, today)
    return rows.assign(project_number=rows["project_number"].astype(str)) if not rows.empty else rows


def generate(project_id: str, causes: list[Cause], impact: dict[str, Any], dataset: Any, red: set[str],
             blocked: set[str], today: Any, rules: BusinessRules,
             depth: int = 0) -> tuple[list[Candidate], list[NotPossible]]:
    """Candidates for the causes of one red project (bounded by recovery_max_candidates)."""
    projects = dataset.projects
    rows = {str(r.get("project_number")): r for r in projects.to_dict("records")}
    red_row = rows.get(str(project_id), {})
    out: list[Candidate] = []
    not_possible: list[NotPossible] = []
    n = [0]

    def add(lever: str, cause: str, steps: list[dict[str, Any]], rationale: str, **kw: Any) -> None:
        n[0] += 1
        out.append(Candidate(f"{'P' if depth else 'C'}{n[0]}", lever, cause, project_id, steps, rationale, **kw))

    budget = _num(red_row.get("budget_cost_eur")) or 0.0
    eac = _num(red_row.get("estimate_at_completion_eur"))
    actual = _num(red_row.get("actual_cost_eur")) or 0.0
    etc = _num(red_row.get("estimate_to_completion_eur"))
    if etc is None and eac is not None:
        etc = max(eac - actual, 0.0)
    for cause in causes:
        if cause.kind == COST:
            gap = cause.gap_eur
            if gap < rules.recovery_min_gap_eur:
                not_possible.append(NotPossible(RESERVE, COST, f"The cost gap of €{gap:,.0f} is below the threshold of "
                                                               f"€{rules.recovery_min_gap_eur:,.0f} – no options."))
                continue
            free = (impact.get("budget") or {}).get("reserve_free_eur") or 0.0
            if free > 0:
                amount = round(min(gap, free), 0)
                add(RESERVE, COST, [_step("reserve_draw", project_id, amount=amount, reason=REASON)],
                    f"Draw €{amount:,.0f} of €{free:,.0f} free reserve (above the floor) for the gap of €{gap:,.0f}.",
                    partial=amount < gap, money_to_project=amount)
            else:
                not_possible.append(NotPossible(RESERVE, COST, "The management reserve is at or below its floor."))
            donors = eligible_donors(projects, red_row, red, blocked, rules)
            spend = {u["project_id"]: u["underspend_eur"] for u in (impact.get("budget") or {}).get("underspend") or []}
            transfers = [d for d in donors if spend.get(str(d["project_number"]), 0) > 0][:rules.recovery_max_donors]
            for d in transfers:
                pid = str(d["project_number"])
                amount = round(min(spend[pid], gap), 0)
                add(TRANSFER, COST, [_step("budget_transfer", pid, project_id, amount=amount)],
                    f"{pid} forecasts €{spend[pid]:,.0f} below its budget (priority {_num(d.get('priority')):g}); "
                    f"move €{amount:,.0f}.", partial=amount < gap, donors=[pid], touches_others=True,
                    money_to_project=amount)
            if not transfers:
                not_possible.append(NotPossible(TRANSFER, COST, "No eligible donor forecasts less than its budget "
                                                                "(red, blocked, protected or higher-priority projects "
                                                                "are never donors)."))
            if eac is not None:
                add(REBASELINE, COST, [_step("rebaseline", project_id, new_budget_eur=round(eac, 0),
                                             reason="Budget follows the forecast (red trigger)")],
                    f"Set the budget to the forecast of €{eac:,.0f}: closes the gap formally, the forecast stays.",
                    money_to_project=round(eac - budget, 0))
            if etc and etc > 0 and rules.recovery_max_scope_cut_pct > 0:
                pct = min(math.ceil(gap / etc * 100), rules.recovery_max_scope_cut_pct)
                add(SCOPE_CUT, COST, [_step("schedule", project_id, scope_change_pct=-float(pct))],
                    f"Reduce the remaining scope by {pct:g} % (the remaining cost is €{etc:,.0f}); the benefit "
                    "drops accordingly.", partial=etc * pct / 100 < gap)
            else:
                not_possible.append(NotPossible(SCOPE_CUT, COST, "No remaining cost (ETC) known – a scope reduction "
                                                                 "cannot be calculated."))
            stops = eligible_donors(projects, red_row, red, blocked, rules, strictly_lower=True)[:1]
            for d in stops:
                pid = str(d["project_number"])
                add(DONOR_STOP, COST, [_step("cancel", pid, project_id)],
                    f"Stop {pid} (priority {_num(d.get('priority')):g}, {(_num(d.get('percent_complete')) or 0):g} % "
                    "complete) and move its free budget after exit costs.", donors=[pid], touches_others=True,
                    partial=True)
            if not stops:
                not_possible.append(NotPossible(DONOR_STOP, COST, "No project of lower priority may be stopped "
                                                                  "(protected, red or blocked)."))
        elif cause.kind == SCHEDULE:
            team_rows = _team_rows(dataset.resource_plans, today)
            own_teams = set(team_rows[team_rows["project_number"] == str(project_id)]["assignment_group"].astype(str)) \
                if not team_rows.empty else set()
            donors = eligible_donors(projects, red_row, red, blocked, rules)
            red_fte = _num(red_row.get("current_fte")) or None
            moved = 0
            for d in donors:
                pid = str(d["project_number"])
                if team_rows.empty or moved >= rules.recovery_max_donors:
                    break
                theirs = team_rows[team_rows["project_number"] == pid]
                shared = sorted(own_teams & set(theirs["assignment_group"].astype(str)))
                for team in shared:
                    fte = pd.to_numeric(theirs[theirs["assignment_group"].astype(str) == team]["allocated_fte"],
                                        errors="coerce").fillna(0).mean()
                    if fte < 1:
                        continue
                    if red_fte is None:
                        red_fte = float(pd.to_numeric(team_rows[team_rows["project_number"] == str(project_id)]
                                                      ["allocated_fte"], errors="coerce").fillna(0).sum()) or 1.0
                    pct = round(1.0 / red_fte * 100, 1)
                    add(CAPACITY, SCHEDULE, [_step("resource_transfer", pid, project_id, fte=1.0, team=team),
                                             _step("schedule", project_id, capacity_change_pct=pct)],
                        f"Move 1 FTE of {team} from {pid} (priority {_num(d.get('priority')):g}) – about +{pct:g} % "
                        "capacity on the red project.", donors=[pid], touches_others=True)
                    moved += 1
                    break
            if not moved:
                not_possible.append(NotPossible(CAPACITY, SCHEDULE, "No eligible donor shares a team with the project "
                                                                    "(red, blocked, protected or higher-priority "
                                                                    "projects keep their capacity)."))
            for b in impact.get("deadline_breaches") or []:
                months = max(1, math.ceil(b["days_late"] / 30.44))
                add(SUCCESSOR, SCHEDULE, [_step("schedule", b["successor"], months_delta=float(months))],
                    f"{b['successor']} needs the result by {b['required_by']}; at the expected delay it is "
                    f"{b['days_late']} days late – re-plan it by {months} month(s).", touches_others=True,
                    partial=True)
            if impact.get("expected_end"):
                end = impact["expected_end"]
                add(END_SHIFT, SCHEDULE, [_step("rebaseline", project_id, new_end_date=end,
                                                reason="End date follows the expected end (red trigger)")],
                    f"Re-baseline the end date to the expected end {end}; shows the domino and NPV effect openly.")
        elif cause.kind == RESOURCES:
            team_rows = _team_rows(dataset.resource_plans, today)
            overloaded = {t["team"] for t in cause.teams}
            paused = 0
            if not team_rows.empty and overloaded:
                for d in eligible_donors(projects, red_row, red, blocked, rules, strictly_lower=True):
                    pid = str(d["project_number"])
                    shared = overloaded & set(team_rows[team_rows["project_number"] == pid]["assignment_group"]
                                              .astype(str))
                    if shared:
                        add(COMPETITOR_HOLD, RESOURCES, [_step("on_hold", pid, months=3.0)],
                            f"Pause {pid} (priority {_num(d.get('priority')):g}) for 3 months to relieve "
                            f"{', '.join(sorted(shared))}.", donors=[pid], touches_others=True)
                        paused += 1
                        break
            if not paused:
                not_possible.append(NotPossible(COMPETITOR_HOLD, RESOURCES,
                                                "No project of lower priority works in the overloaded team(s)."
                                                if overloaded else "No overloaded team in the resource plans – the "
                                                                   "red rating comes from the status report only."))
        elif cause.kind == SCOPE_RISK:
            for r in cause.risks[:2]:
                if r.get("cost_eur") is None:
                    not_possible.append(NotPossible(RISK, SCOPE_RISK, f"{r['risk_number']}: no cost of a mitigation in "
                                                                      "the risk data – the response cannot be "
                                                                      "calculated."))
                    continue
                add(RISK, SCOPE_RISK, [_step("risk_response", project_id, risk_number=r["risk_number"],
                                             response="Mitigate", cost_eur=r["cost_eur"], funding="project")],
                    f"Mitigate {r['risk_number']} (exposure €{r['exposure_eur']:,.0f}) for €{r['cost_eur']:,.0f}.")
    if depth == 0 and impact.get("upstream_red"):
        for pred in impact["upstream_red"][:1]:  # one level only
            pred_row = rows.get(pred)
            if not pred_row:
                continue
            gap = max((_num(pred_row.get("estimate_at_completion_eur")) or 0) - (_num(pred_row.get("budget_cost_eur"))
                                                                                  or 0), 0)
            if gap >= rules.recovery_min_gap_eur and (impact.get("budget") or {}).get("reserve_free_eur", 0) > 0:
                amount = round(min(gap, impact["budget"]["reserve_free_eur"]), 0)
                add(RESERVE, COST, [_step("reserve_draw", pred, amount=amount,
                                          reason=f"Red predecessor of {project_id} (red trigger)")],
                    f"The red predecessor {pred} holds {project_id} back; close its cost gap of €{gap:,.0f} first.",
                    touches_others=True, money_to_project=amount)
                out[-1].project = pred
    return out[:rules.recovery_max_candidates], not_possible
