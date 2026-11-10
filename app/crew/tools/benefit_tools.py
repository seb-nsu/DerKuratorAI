"""Benefit realisation after go-live: the benefit tranches already due (Benefit_Plans, realisation date not after the
reference date) against the actual benefits reported for them (Benefit_Actuals).

Deterministic (no LLM), on the RLS-filtered scenario context; it writes nothing. Amounts are in EUR – a source kept in
another currency is converted when the dataset is loaded (Exchange_Rates). The thresholds are business rules:
realisation below `benefit_realization_nogo_pct` -> NO-GO (the business case no longer holds), below
`benefit_realization_min_pct` -> CONDITIONAL GO (corrective plan by the sponsor), a tranche due longer than
`benefit_report_grace_days` without any actual -> CONDITIONAL GO (the PM reports it). Without Benefit_Plans the
project's planned_benefit_eur counts as one tranche at the planned end.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.core.business_rules import current_rules
from app.crew.tools.context import as_of, get_table, record_fact
from app.crew.tools.simulator_tools import _eur, _num, _open_project, _safe, _to_date, repeated_call
from app.llm.tools import tool

UNASSIGNED = "(not assigned to a tranche)"


def benefit_tranches(plans: pd.DataFrame, p: dict[str, Any]) -> list[dict[str, Any]]:
    """Planned tranches of a project: plan number, type, amount, realisation date, owner."""
    tranches = []
    for row in plans.to_dict(orient="records"):
        when = _to_date(row.get("realization_start_date"))
        amount = _num(row.get("planned_benefit_eur"))
        if when is None or amount <= 0:
            continue
        tranches.append({"plan": str(row.get("benefit_plan_number") or ""), "type": row.get("benefit_type") or "–",
                         "planned": amount, "due": when, "owner": row.get("owner")})
    if not tranches:
        when, amount = _to_date(p.get("planned_end_date")), _num(p.get("planned_benefit_eur"))
        if when is not None and amount > 0:
            tranches.append({"plan": "", "type": "planned benefit (project)", "planned": amount, "due": when,
                             "owner": p.get("project_sponsor")})
    return sorted(tranches, key=lambda t: (t["due"], t["plan"]))


def realisation(plans: pd.DataFrame, actuals: pd.DataFrame, p: dict[str, Any], today: date) -> dict[str, Any]:
    """Planned vs. actual benefit of the due tranches of one project (its Benefit_Plans and Benefit_Actuals rows);
    actuals without a known tranche count for the project. Actuals of tranches not yet due are shown as realised early
    but never offset a shortfall of the due ones. Pure – used by the tool and the project page."""
    tranches = benefit_tranches(plans, p)
    by_plan: dict[str, float] = {}
    if not actuals.empty and "actual_benefit_eur" in actuals.columns:
        plan_col = actuals["benefit_plan_number"].astype(str).str.strip() if "benefit_plan_number" in actuals.columns \
            else pd.Series("", index=actuals.index)
        for plan, amount in zip(plan_col, pd.to_numeric(actuals["actual_benefit_eur"], errors="coerce").fillna(0),
                                strict=True):
            key = plan if plan and plan not in ("nan", "None") else UNASSIGNED
            by_plan[key] = by_plan.get(key, 0.0) + float(amount)
    known = {t["plan"] for t in tranches if t["plan"]}
    rows = []
    for t in tranches:
        actual = by_plan.get(t["plan"], 0.0) if t["plan"] else 0.0
        rows.append({**t, "actual": actual, "is_due": t["due"] <= today, "days_due": (today - t["due"]).days})
    unassigned = sum(v for k, v in by_plan.items() if k not in known)
    planned_due = sum(r["planned"] for r in rows if r["is_due"])
    actual_due = sum(r["actual"] for r in rows if r["is_due"]) + unassigned
    early = sum(r["actual"] for r in rows if not r["is_due"])
    pct = round(actual_due / planned_due * 100, 1) if planned_due > 0 else None
    return {"tranches": rows, "planned_due": planned_due, "actual": actual_due, "unassigned": unassigned, "pct": pct,
            "early": early, "planned_total": sum(r["planned"] for r in rows)}


@_safe
def benefit_review(project_id: str) -> str:
    p = _open_project(project_id, allow_blocked=True, allow_closed=True)
    if isinstance(p, str):
        return p
    pid, rules = str(p["project_number"]), current_rules()
    r = realisation(get_table("Benefit_Plans", pid), get_table("Benefit_Actuals", pid), p, as_of())
    lines = [f"RESULT simulate_benefit_review | {pid} ({p['project_name']}) – benefit realisation as of {as_of():%Y-%m-%d}",
             "| Tranche | Type | Due | Planned | Actual | Status |", "|---|---|---|---|---|---|"]
    grace = timedelta(days=rules.benefit_report_grace_days)
    unreported = []
    for t in r["tranches"]:
        if not t["is_due"]:
            status = "not due yet"
        elif t["actual"] <= 0 and t["days_due"] > grace.days:
            status = f"due {t['days_due']} days – no actual reported"
            unreported.append(t)
        elif t["actual"] <= 0:
            status = "due – report pending"
        else:
            status = f"{t['actual'] / t['planned'] * 100:.0f} % realised"
        lines.append(f"| {t['plan'] or '–'} | {t['type']} | {t['due']:%Y-%m-%d} | {_eur(t['planned'])} | "
                     f"{_eur(t['actual'])} | {status} |")
    if not r["tranches"]:
        lines.append("| – | – | – | – | – | no planned benefit recorded |")
    if r["unassigned"]:
        lines.append(f"- Actual benefit not assigned to a tranche: {_eur(r['unassigned'])} (counted for the project)")
    if r["early"]:
        lines.append(f"- Realised early on tranches not yet due: {_eur(r['early'])} (not counted against the due "
                     "tranches)")
    lines.append(f"- Due so far: {_eur(r['planned_due'])} of {_eur(r['planned_total'])} planned; realised "
                 f"{_eur(r['actual'])}" + (f" = {r['pct']:g} %" if r["pct"] is not None else ""))
    if r["pct"] is None:
        lines.append("- No tranche is due yet – nothing to assess.")
    else:
        lines.append(f"- Thresholds: corrective plan below {rules.benefit_realization_min_pct:g} %, NO-GO below "
                     f"{rules.benefit_realization_nogo_pct:g} %" if rules.benefit_realization_nogo_pct else
                     f"- Threshold: corrective plan below {rules.benefit_realization_min_pct:g} %")
    for t in unreported:
        record_fact("benefit_unreported", project=pid, successor=t["plan"] or pid, due=t["due"].isoformat(),
                    days=t["days_due"], owner=t.get("owner"))
    if r["pct"] is not None:
        if rules.benefit_realization_nogo_pct and r["pct"] < rules.benefit_realization_nogo_pct:
            lines.append(f"⛔ Only {r['pct']:g} % of the due benefit is realised – the business case no longer holds.")
            record_fact("benefit_case_failed", project=pid, pct=r["pct"], planned=round(r["planned_due"], 2),
                        actual=round(r["actual"], 2))
        elif r["pct"] < rules.benefit_realization_min_pct:
            lines.append(f"⚠ Benefit realisation {r['pct']:g} % is below {rules.benefit_realization_min_pct:g} %.")
            record_fact("benefit_shortfall", project=pid, pct=r["pct"], shortfall=round(r["planned_due"] - r["actual"], 2))
    record_fact("benefit_review", project=pid, pct=r["pct"], planned_due=round(r["planned_due"], 2),
                actual=round(r["actual"], 2))
    return "\n".join(lines)


@tool("simulate_benefit_review")
def simulate_benefit_review(project_id: str) -> str:
    """Benefit realisation review of a project after go-live: planned benefit tranches already due against the
    actual benefits reported, with the realisation rate and the tranches still unreported. Writes nothing."""
    if skipped := repeated_call("simulate_benefit_review", project_id):
        return skipped
    return benefit_review(project_id)


BENEFIT_TOOLS = [simulate_benefit_review]
