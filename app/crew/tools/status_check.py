"""Status check of the sources (E2): the traffic light a project reports – set by hand in ServiceNow, Jira or an Excel
list – against a status calculated from its numbers. Deterministic, no LLM: the reported status is never changed,
only a project reported better than its numbers is listed ("reported green, calculated red").

Indicators (thresholds in the business rules, `status_*`):
- cost: forecast (EAC) above the budget, in % of the budget;
- burn: share of the budget spent minus share of the work done, in percentage points;
- schedule: planned end date after the approved end date in days; an open project past its planned end is red;
- not updated: the last status report is older than `status_report_max_age_days`.

The calculated status is the worst indicator. The result names the figures behind it and no person."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from app.core.business_rules import BusinessRules, current_rules

ORDER = {"Green": 0, "Yellow": 1, "Red": 2}
CLOSED_STATES = ("closed", "cancelled", "canceled", "complete", "completed")


@dataclass
class StatusCheck:
    project_id: str
    project_name: str
    reported: str | None
    calculated: str
    reasons: list[str] = field(default_factory=list)  # one line per indicator that is not green
    indicators: dict[str, Any] = field(default_factory=dict)
    last_report: str | None = None  # ISO date of the latest status report
    stale: bool = False
    source: str | None = None

    @property
    def mismatch(self) -> bool:
        """True when the project reports a better status than its numbers give."""
        return self.reported in ORDER and ORDER[self.calculated] > ORDER[self.reported]

    @property
    def label(self) -> str:
        if self.reported not in ORDER:
            return "no reported status"
        if self.mismatch:
            return f"reported {self.reported.lower()}, calculated {self.calculated.lower()}"
        return "consistent"

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "project_name": self.project_name, "reported": self.reported,
                "calculated": self.calculated, "mismatch": self.mismatch, "label": self.label,
                "reasons": list(self.reasons), "indicators": dict(self.indicators), "last_report": self.last_report,
                "stale": self.stale, "source": self.source}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _day(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or not str(value).strip():
        return None
    try:
        stamp = pd.Timestamp(str(value)[:10])
    except (ValueError, TypeError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def _level(value: float | None, yellow: float, red: float) -> str:
    if value is None:
        return "Green"
    return "Red" if value >= red else "Yellow" if value >= yellow else "Green"


def _worst(levels: list[str]) -> str:
    return max(levels, key=lambda s: ORDER[s]) if levels else "Green"


def check_project(row: dict[str, Any], today: date, rules: BusinessRules,
                  last_report: date | None = None) -> StatusCheck:
    """The calculated status of one project row (canonical Projects columns)."""
    budget, eac = _num(row.get("budget_cost_eur")), _num(row.get("estimate_at_completion_eur"))
    actual, progress = _num(row.get("actual_cost_eur")), _num(row.get("percent_complete"))
    overrun = (eac - budget) / budget * 100 if budget and eac is not None else None
    burn_gap = actual / budget * 100 - progress if budget and actual is not None and progress is not None else None
    approved, planned = _day(row.get("approved_end_date")), _day(row.get("planned_end_date"))
    slip = (planned - approved).days if planned and approved else None
    overdue = bool(planned and planned < today and (progress is None or progress < 100))

    levels: dict[str, str] = {
        "cost": _level(overrun, rules.status_cost_yellow_pct, rules.status_cost_red_pct),
        "burn": _level(burn_gap, rules.status_burn_gap_yellow_pct, rules.status_burn_gap_red_pct),
        "schedule": "Red" if overdue else _level(slip, rules.status_slip_yellow_days, rules.status_slip_red_days),
    }
    reasons = []
    if levels["cost"] != "Green":
        reasons.append(f"forecast €{eac:,.0f} is {round(overrun, 1):g} % over the budget of €{budget:,.0f}")
    if levels["burn"] != "Green":
        reasons.append(f"{actual / budget * 100:.0f} % of the budget spent at {progress:g} % complete")
    if overdue:
        reasons.append(f"planned end {planned.isoformat()} has passed at {progress if progress is not None else 0:g} "
                       "% complete")
    elif levels["schedule"] != "Green":
        reasons.append(f"planned end {planned.isoformat()} is {slip} days after the approved end "
                       f"{approved.isoformat()}")
    stale = bool(last_report and (today - last_report).days > rules.status_report_max_age_days)
    if stale:
        reasons.append(f"last status report {last_report.isoformat()} is {(today - last_report).days} days old")
    reported = str(row.get("status") or "").strip() or None
    return StatusCheck(
        project_id=str(row.get("project_number")), project_name=str(row.get("project_name") or ""),
        reported=reported, calculated=_worst(list(levels.values())), reasons=reasons,
        indicators={"forecast_overrun_pct": None if overrun is None else round(overrun, 1),
                    "burn_gap_pts": None if burn_gap is None else round(burn_gap, 1), "end_slip_days": slip,
                    "overdue": overdue, "levels": levels},
        last_report=last_report.isoformat() if last_report else None, stale=stale,
        source=(str(row["source_system"]) if row.get("source_system") else None))


def _last_reports(status_reports: pd.DataFrame | None) -> dict[str, date]:
    if status_reports is None or status_reports.empty or not {"project_number", "status_date"} <= set(
            status_reports.columns):
        return {}
    last: dict[str, date] = {}
    for pid, value in zip(status_reports["project_number"], status_reports["status_date"], strict=False):
        day = _day(value)
        if day and (str(pid) not in last or day > last[str(pid)]):
            last[str(pid)] = day
    return last


def check_portfolio(projects: pd.DataFrame, status_reports: pd.DataFrame | None, today: date,
                    rules: BusinessRules | None = None) -> list[StatusCheck]:
    """Every open project of the portfolio, mismatches first (worst first), then the rest by number."""
    rules = rules or current_rules()
    last = _last_reports(status_reports)
    results = []
    for row in projects.to_dict("records"):
        if str(row.get("state") or "").strip().lower() in CLOSED_STATES:
            continue
        results.append(check_project(row, today, rules, last.get(str(row.get("project_number")))))
    return sorted(results, key=lambda c: (not c.mismatch, -ORDER[c.calculated], c.project_id))


def check_dataset(dataset: Any, today: date, rules: BusinessRules | None = None) -> list[StatusCheck]:
    return check_portfolio(dataset.projects, getattr(dataset, "status_reports", None), today, rules)
