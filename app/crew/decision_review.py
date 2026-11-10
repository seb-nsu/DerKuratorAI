"""Decision review (E6): what became of the approved decisions. Six and twelve months after an approval the
decided values are compared with today's data of the same projects – the budget it set against today's forecast,
the end date it set against today's planned end – and the verdicts are scored: how often did a GO, a CONDITIONAL GO
or an overridden NO-GO hold? Conditions whose due date has passed are listed for a check.

Deterministic, no LLM, read-only: it reads the proposal log and the loaded portfolio and writes nothing. The result
names projects and verdicts, no person."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.core import clock
from app.core.business_rules import BusinessRules, current_rules

REVIEW_AFTER_DAYS = (180, 365)
BUDGET_FIELDS = ("budget_cost_eur",)
DATE_FIELDS = ("planned_end_date", "approved_end_date")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _day(value: Any) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        stamp = pd.Timestamp(str(value)[:10])
    except (ValueError, TypeError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def _decided_on(proposal: dict[str, Any]) -> date | None:
    try:
        return clock.parse(proposal.get("decided_at") or proposal.get("timestamp")).date()
    except (TypeError, ValueError):
        return None


def _outcome(change: dict[str, Any], row: dict[str, Any] | None, rules: BusinessRules) -> dict[str, Any] | None:
    """Decided value against today's data for one change; None for a field the review does not judge."""
    field = str(change.get("field"))
    if field not in BUDGET_FIELDS + DATE_FIELDS:
        return None
    base = {"project_id": str(change.get("project_id")), "field": field, "decided": change.get("new")}
    if row is None:
        return {**base, "status": "gone", "text": "project no longer in the data"}
    if field in BUDGET_FIELDS:
        decided = _num(change.get("new"))
        forecast = _num(row.get("estimate_at_completion_eur"))
        if decided is None or forecast is None:
            return {**base, "status": "unknown", "text": "no forecast to compare"}
        gap = forecast - decided
        held = gap <= rules.overrun_tolerance_eur
        pct = gap / decided * 100 if decided else 0.0
        return {**base, "today": forecast, "deviation_pct": round(pct, 1), "status": "held" if held else "missed",
                "text": (f"forecast €{forecast:,.0f} within the decided budget €{decided:,.0f}" if held else
                         f"forecast €{forecast:,.0f} is {pct:.1f} % over the decided budget €{decided:,.0f}")}
    decided_day, today_day = _day(change.get("new")), _day(row.get("planned_end_date"))
    if decided_day is None or today_day is None:
        return {**base, "status": "unknown", "text": "no end date to compare"}
    slip = (today_day - decided_day).days
    held = slip <= 0
    return {**base, "today": today_day.isoformat(), "deviation_days": slip, "status": "held" if held else "missed",
            "text": (f"planned end {today_day.isoformat()} keeps the decided {decided_day.isoformat()}" if held else
                     f"planned end {today_day.isoformat()} is {slip} days after the decided {decided_day.isoformat()}")}


def review_decisions(proposals: list[dict[str, Any]], projects: pd.DataFrame, today: date | None = None,
                     rules: BusinessRules | None = None) -> dict[str, Any]:
    """Every approved proposal with its age, its review stage, the outcome of its budget and date changes and its
    overdue conditions; plus the hit rate per verdict."""
    today = today or clock.today()
    rules = rules or current_rules()
    rows = {str(r.get("project_number")): r for r in projects.to_dict("records")} if not projects.empty else {}
    decisions = []
    for proposal in proposals:
        if proposal.get("status") != "approved":
            continue
        decided = _decided_on(proposal)
        if decided is None:
            continue
        age = (today - decided).days
        stage = "12 months" if age >= REVIEW_AFTER_DAYS[1] else "6 months" if age >= REVIEW_AFTER_DAYS[0] else None
        next_review = next((decided + timedelta(days=d) for d in REVIEW_AFTER_DAYS if age < d), None)
        record = proposal.get("verdict_record") or {}
        outcomes = [o for c in proposal.get("changes") or []
                    if (o := _outcome(c, rows.get(str(c.get("project_id"))), rules)) is not None]
        judged = [o for o in outcomes if o["status"] in ("held", "missed")]
        overdue = [d for d in record.get("condition_details") or []
                   if _day(d.get("due")) and _day(d.get("due")) < today]
        decision = str(record.get("decision") or proposal.get("verdict") or "unknown")
        if proposal.get("override"):
            decision = f"{decision} (overridden)"
        decisions.append({
            "proposal_id": proposal.get("id"), "decided_on": decided.isoformat(), "age_days": age,
            "review_stage": stage, "next_review": next_review.isoformat() if next_review else None,
            "verdict": decision, "rule": record.get("rule") or "", "projects": proposal.get("projects") or [],
            "outcomes": outcomes,
            "held": None if not judged else all(o["status"] == "held" for o in judged),
            "conditions_to_check": [{"condition": d.get("condition"), "owner_role": d.get("owner_role"),
                                     "due": d.get("due")} for d in overdue]})
    decisions.sort(key=lambda d: (d["review_stage"] is None, d["decided_on"]))
    scores: dict[str, dict[str, int]] = {}
    for d in decisions:
        if d["held"] is None:
            continue
        entry = scores.setdefault(d["verdict"], {"decisions": 0, "held": 0})
        entry["decisions"] += 1
        entry["held"] += int(d["held"])
    hit_rate = [{"verdict": v, **s, "hit_rate_pct": round(s["held"] / s["decisions"] * 100, 1)}
                for v, s in sorted(scores.items())]
    return {"as_of": today.isoformat(), "decisions": decisions, "hit_rate": hit_rate,
            "due_for_review": sum(1 for d in decisions if d["review_stage"]),
            "conditions_overdue": sum(len(d["conditions_to_check"]) for d in decisions)}
