"""Stage 2a of the red trigger: why is the project red? Deterministic, from the indicators of the status check and the
dimensions of the latest status report (schedule, cost, resources, scope). A project can have several causes; each
is handled on its own and carries the gap that a measure has to close.

| Cause        | Detected by                                                           | Gap                        |
|--------------|-----------------------------------------------------------------------|----------------------------|
| cost         | forecast or burn indicator red                                        | € (EAC − budget)           |
| schedule     | end-date indicator red (slip or planned end passed)                   | days                       |
| resources    | `resources = Red` in the report or a team above overload_alert_pct    | hours / FTE per team       |
| scope_risk   | `scope = Red` in the report or a high/high risk that is only accepted | exposure in €              |
| reporting    | only the status report is too old                                     | none: update the status    |
| unmeasured   | reported red, every indicator green                                   | none: record the cause     |

No LLM: free text of the report is passed on as a hint only (`issues_decisions`), never as a cause."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from app.core.business_rules import BusinessRules
from app.crew.tools.status_check import StatusCheck

COST, SCHEDULE, RESOURCES, SCOPE_RISK, REPORTING, UNMEASURED = (
    "cost", "schedule", "resources", "scope_risk", "reporting", "unmeasured")
CAUSE_LABELS = {COST: "Cost", SCHEDULE: "Schedule", RESOURCES: "Resources", SCOPE_RISK: "Scope / risk",
                REPORTING: "Reporting gap", UNMEASURED: "Red without a measurable gap"}
LEVELS = ("Low", "Moderate", "High")


@dataclass
class Cause:
    kind: str
    text: str
    gap_eur: float = 0.0
    gap_days: int = 0
    teams: list[dict[str, Any]] = field(default_factory=list)  # {"team", "month", "utilisation_pct", "excess_hours"}
    risks: list[dict[str, Any]] = field(default_factory=list)  # {"risk_number", "exposure_eur", ...}

    @property
    def label(self) -> str:
        return CAUSE_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "label": self.label}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _day(value: Any) -> date | None:
    try:
        stamp = pd.Timestamp(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def latest_report(status_reports: pd.DataFrame | None, project_id: str, today: date) -> dict[str, Any] | None:
    """The newest status report up to the reference date."""
    if status_reports is None or status_reports.empty or "project_number" not in status_reports.columns:
        return None
    rows = status_reports[status_reports["project_number"].astype(str) == str(project_id)].copy()
    if rows.empty:
        return None
    rows["_day"] = rows["status_date"].map(_day)
    rows = rows[rows["_day"].map(lambda d: d is not None and d <= today)]
    return None if rows.empty else rows.sort_values("_day").iloc[-1].drop(labels=["_day"]).to_dict()


def _overloaded_teams(resource_plans: pd.DataFrame | None, project_id: str, today: date,
                      rules: BusinessRules, months: int = 3) -> list[dict[str, Any]]:
    if resource_plans is None or resource_plans.empty:
        return []
    rows = resource_plans[resource_plans["project_number"].astype(str) == str(project_id)].copy()
    if rows.empty:
        return []
    start = pd.Timestamp(today.replace(day=1))
    end = start + pd.DateOffset(months=months)
    rows["_month"] = pd.to_datetime(rows["month"], errors="coerce")
    rows = rows[(rows["_month"] >= start) & (rows["_month"] < end)]
    out = []
    for r in rows.to_dict("records"):
        util = _num(r.get("utilization_pct"))
        if util is not None and util > rules.overload_alert_pct:
            out.append({"team": str(r.get("assignment_group")), "month": r["_month"].date().isoformat(),
                        "utilisation_pct": round(util, 1),
                        "excess_hours": round(max((_num(r.get("demand_hours")) or 0) - (_num(r.get("capacity_hours"))
                                                                                       or 0), 0), 0)})
    return sorted(out, key=lambda t: (t["month"], -t["utilisation_pct"]))


def _accepted_risks(risks: pd.DataFrame | None, project_id: str, rules: BusinessRules) -> list[dict[str, Any]]:
    """Risks whose exposure is at least the moderate×high scale and that are only accepted (no active response)."""
    if risks is None or risks.empty or "project_number" not in risks.columns:
        return []
    prob = {"Low": rules.risk_probability_low_pct, "Moderate": rules.risk_probability_moderate_pct,
            "High": rules.risk_probability_high_pct}
    impact = {"Low": rules.risk_impact_low_eur, "Moderate": rules.risk_impact_moderate_eur,
              "High": rules.risk_impact_high_eur}
    floor = rules.risk_probability_moderate_pct / 100 * rules.risk_impact_high_eur
    out = []
    for r in risks[risks["project_number"].astype(str) == str(project_id)].to_dict("records"):
        status = str(r.get("risk_status") or "").strip()
        if status not in ("Accept", "Open", ""):
            continue
        p, i = str(r.get("probability") or "Moderate"), str(r.get("impact") or "Moderate")
        exposure = prob.get(p, prob["Moderate"]) / 100 * impact.get(i, impact["Moderate"])
        if exposure >= floor:
            out.append({"risk_number": str(r.get("risk_number")), "probability": p, "impact": i, "status": status,
                        "exposure_eur": round(exposure, 0), "cost_eur": _num(r.get("mitigation_cost_eur")),
                        "cluster": r.get("systemic_risk_cluster")})
    return sorted(out, key=lambda x: -x["exposure_eur"])


def diagnose(check: StatusCheck, row: dict[str, Any], dataset: Any, today: date,
             rules: BusinessRules) -> tuple[list[Cause], dict[str, Any] | None]:
    """The causes of one red project and its latest status report (or None)."""
    report = latest_report(getattr(dataset, "status_reports", None), check.project_id, today)
    levels = (check.indicators or {}).get("levels") or {}
    causes: list[Cause] = []
    budget, eac = _num(row.get("budget_cost_eur")), _num(row.get("estimate_at_completion_eur"))
    actual, progress = _num(row.get("actual_cost_eur")), _num(row.get("percent_complete"))
    if levels.get("cost") == "Red" or levels.get("burn") == "Red":
        gap = max((eac or 0) - (budget or 0), 0.0) if budget is not None and eac is not None else 0.0
        basis = "forecast"
        if gap == 0 and budget and actual is not None and progress:
            gap = max(actual / (progress / 100) - budget, 0.0)  # spending ahead of progress: the burn forecast
            basis = "burn rate"
        text = (f"forecast €{eac:,.0f} against a budget of €{budget:,.0f}" if basis == "forecast" and eac is not None
                else f"{actual / budget * 100:.0f} % of the budget spent at {progress:g} % complete"
                if budget and actual is not None and progress is not None else "cost indicator red")
        causes.append(Cause(COST, f"Cost: {text} – gap €{gap:,.0f} ({basis}).", gap_eur=round(gap, 2)))
    if levels.get("schedule") == "Red":
        slip = int((check.indicators or {}).get("end_slip_days") or 0)
        planned = _day(row.get("planned_end_date"))
        overdue = (today - planned).days if planned and planned < today else 0
        days = max(slip, overdue)
        text = (f"planned end {planned.isoformat()} has passed ({overdue} days)" if overdue
                else f"planned end {slip} days after the approved end")
        causes.append(Cause(SCHEDULE, f"Schedule: {text}.", gap_days=days))
    teams = _overloaded_teams(getattr(dataset, "resource_plans", None), check.project_id, today, rules)
    if teams or (report and str(report.get("resources")) == "Red"):
        text = (", ".join(f"{t['team']} {t['utilisation_pct']:g} % in {t['month'][:7]}" for t in teams[:3])
                if teams else "the latest status report rates resources red")
        causes.append(Cause(RESOURCES, f"Resources: {text}.", teams=teams))
    risks = _accepted_risks(getattr(dataset, "risks", None), check.project_id, rules)
    if risks or (report and str(report.get("scope")) == "Red"):
        exposure = sum(r["exposure_eur"] for r in risks)
        text = (f"{len(risks)} accepted risk(s) with an exposure of €{exposure:,.0f}" if risks
                else "the latest status report rates scope red")
        causes.append(Cause(SCOPE_RISK, f"Scope / risk: {text}.", gap_eur=exposure, risks=risks))
    if not causes:
        if check.stale:
            causes.append(Cause(REPORTING, f"Reporting gap: the last status report ({check.last_report}) is older "
                                           f"than {rules.status_report_max_age_days} days – update the status."))
        if check.reported == "Red":
            causes.append(Cause(UNMEASURED, "Reported red, but every indicator is green – the cause is not in the "
                                            "numbers; record it (cost, schedule, resources or scope)."))
    return causes, report
