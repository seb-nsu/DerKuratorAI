"""The annual plan from submission to project: the PMO submits a saved scenario, the Portfolio Board approves it
(four-eyes through the proposal workflow, written with an operation id in the write journal), and an approved demand
becomes a project when it starts (a second PMO approves the conversion, because it sets the first baseline).

What an approved plan sets per demand: In or forced in with its start in the binding year → Approved (the budget of
that year is released); in the plan with a later start → Planned; Out → Deferred (back into the next round)."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.core import clock, proposals
from app.core.security import ROLE_BOARD
from app.data_adapters.base import new_operation_id
from app.data_adapters.dataset import DEMAND_KEY, PK, PortfolioDataset
from app.planning import store
from app.planning.model import resource_plans, year_plans
from app.planning.scenario import DEFERRED, FORCED, IN, OUT, ScenarioResult, run_scenario

HOURS_PER_FTE = 160  # monthly hours of one FTE, as in the resource plans
APPROVED_DEMAND, PLANNED_DEMAND, DEFERRED_DEMAND, CONVERTED = "Approved", "Planned", "Deferred", "Converted"


class PlanError(ValueError):
    """The plan cannot be submitted, approved or converted (the message says why)."""


def demand_status_after(result: ScenarioResult) -> dict[str, str]:
    """The status an approved plan gives each of its demands."""
    out: dict[str, str] = {}
    binding = result.binding_year
    for row in result.items.to_dict(orient="records") if not result.items.empty else []:
        if row["decision"] in (IN, FORCED, DEFERRED):
            start = str(row.get("start") or "")
            out[row[DEMAND_KEY]] = APPROVED_DEMAND if start.startswith(str(binding)) else PLANNED_DEMAND
        elif row["decision"] == OUT:
            out[row[DEMAND_KEY]] = DEFERRED_DEMAND
    return out


def plan_changes(ds: PortfolioDataset, result: ScenarioResult, scenario_id: str, by: str) -> list[dict[str, Any]]:
    """The change set of the approval: the Approved rows of the scenario and the new status of its demands."""
    changes = store.save_changes(ds, result, by, status=store.APPROVED, scenario_id=scenario_id)
    current = dict(zip(ds.demands[DEMAND_KEY].astype(str), ds.demands["demand_status"], strict=True)) \
        if not ds.demands.empty else {}
    for did, status in sorted(demand_status_after(result).items()):
        if current.get(did) != status:
            changes.append({"table": "Demands", "project_id": did, "key": did, "field": "demand_status",
                            "old": current.get(did), "new": status, "source_tool": "plan_approval"})
    return changes


def plan_paper(result: ScenarioResult, scenario_id: str) -> str:
    counts = result.items["decision"].value_counts() if not result.items.empty else pd.Series(dtype=int)
    lines = [f"**Annual plan {result.years[0]}–{result.years[-1]}** · scenario {scenario_id} “{result.input.name}”",
             "", f"{store.spec_text(result.input)}", "",
             f"{counts.get(IN, 0) + counts.get(FORCED, 0)} in, {counts.get(DEFERRED, 0)} deferred, "
             f"{counts.get(OUT, 0)} out.", "", result.verdict.to_markdown()]
    return "\n".join(lines)


def submit_plan(ds: PortfolioDataset, scenario_id: str, by: str, role: str, provider: Any,
                as_of: date | None = None) -> dict[str, Any]:
    """Recalculates the saved scenario on today's data, records it as Submitted and puts the approval before the
    Portfolio Board. Returns the proposal. Raises PlanError."""
    saved = store.saved_scenarios(ds)
    row = saved[saved["scenario_id"].astype(str) == scenario_id] if not saved.empty else saved
    if row.empty:
        raise PlanError(f"Scenario {scenario_id} not found.")
    if row.iloc[0]["scenario_status"] != store.DRAFT:
        raise PlanError(f"Scenario {scenario_id} is already {row.iloc[0]['scenario_status']}.")
    if any(p.get("kind") == proposals.PLAN for p in proposals.list_proposals(status=proposals.SUBMITTED)):
        raise PlanError("Another annual plan is waiting for the Portfolio Board – decide it first.")
    result = run_scenario(ds, store.input_of(row.iloc[0]), as_of)
    if result.verdict.decision == "ANALYSIS INCOMPLETE":
        raise PlanError("The plan is incomplete – fix the data of the named demands first: "
                        + "; ".join(result.verdict.reasons[1:]))
    provider.apply_changes(store.save_changes(ds, result, by, status=store.SUBMITTED, scenario_id=scenario_id),
                           user=by, role=role, reason="planning_scenario")
    changes = plan_changes(ds, result, scenario_id, by)
    return proposals.submit_proposal(by, role, f"Approve annual plan {scenario_id} “{result.input.name}”",
                                     plan_paper(result, scenario_id), result.verdict.label, changes,
                                     verdict_record=result.verdict.to_dict(), kind=proposals.PLAN)


def approve_plan(proposal_id: str, by: str, role: str, provider: Any, comment: str = "",
                 override: bool = False) -> Any:
    """The Portfolio Board's approval: reserve, write the change set (snapshot, write journal with one operation id),
    then record the decision. Raises ProposalError or the write errors (the reservation is released)."""
    if role != ROLE_BOARD:
        raise proposals.ProposalError("The annual plan is approved by the Portfolio Board.")
    proposal = proposals.get_proposal(proposal_id)
    operation = new_operation_id()
    proposals.validate_decision(proposal_id, proposals.APPROVED, by, role, comment=comment, override=override)
    proposals.reserve_apply(proposal_id, by, role, operation, comment=comment, override=override)
    try:
        result = provider.apply_changes(proposal["changes"], user=by, reason=f"proposal_{proposal_id}", role=role,
                                        proposal_id=proposal_id, operation_id=operation)
    except Exception as exc:
        proposals.release_apply(proposal_id, operation, exc.__class__.__name__)
        raise
    proposals.decide(proposal_id, proposals.APPROVED, by, role, comment=comment, snapshot_id=result.snapshot_id,
                     override=override, operation_id=operation)
    return result


def reject_plan(proposal_id: str, by: str, role: str, reason: str) -> dict[str, Any]:
    return proposals.decide(proposal_id, proposals.REJECTED, by, role, comment=reason)


# --------------------------------------------------------------------------- demand -> project
def _next_project_number(ds: PortfolioDataset) -> str:
    numbers = ds.projects[PK].astype(str) if not ds.projects.empty else pd.Series(dtype=str)
    prefix = "PRJ-DEMO-" if numbers.str.startswith("PRJ-DEMO-").any() else "PRJ-"
    digits = pd.to_numeric(numbers.str.extract(r"(\d+)$")[0], errors="coerce").dropna()
    n = int(digits.max()) + 1 if not digits.empty else 1001
    while f"{prefix}{n}" in set(numbers):
        n += 1
    return f"{prefix}{n}"


def _months(year: int, quarter: int) -> list[str]:
    return [date(year, (quarter - 1) * 3 + m, 1).isoformat() for m in (1, 2, 3)]


def conversion_changes(ds: PortfolioDataset, demand: str, by: str) -> list[dict[str, Any]]:
    """An approved demand becomes a project: master data 1:1, quarterly money as monthly Cost_Plans, capacity as
    Resource_Plans, the first baseline, and the link in both directions. Raises PlanError."""
    rows = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand] if not ds.demands.empty else ds.demands
    if rows.empty:
        raise PlanError(f"Demand {demand} not found.")
    d = rows.iloc[0].to_dict()
    if d.get("demand_status") != APPROVED_DEMAND:
        raise PlanError(f"Only an approved demand becomes a project – {demand} is {d.get('demand_status')}.")
    plans = year_plans(ds)
    plans = plans[plans[DEMAND_KEY] == demand]
    if plans.empty:
        raise PlanError(f"{demand} has no annual figures.")
    pid = _next_project_number(ds)
    money = plans[(plans["capex_eur"] + plans["opex_eur"]) > 0].sort_values(["year", "quarter"])
    first, last = money.iloc[0], money.iloc[-1]
    start = date(int(first["year"]), (int(first["quarter"]) - 1) * 3 + 1, 1)
    end = (pd.Timestamp(date(int(last["year"]), int(last["quarter"]) * 3, 1)) + pd.offsets.MonthEnd(0)).date()
    capex, opex = float(plans["capex_eur"].sum()), float(plans["opex_eur"].sum())
    total = capex + opex
    project = {PK: pid, "project_name": d.get("demand_name"), "project_manager": d.get("demand_owner"),
               "project_sponsor": d.get("project_sponsor"), "business_unit": d.get("business_unit"),
               "portfolio": d.get("portfolio"), "business_service": d.get("business_service"),
               "business_capability": d.get("business_capability"), "execution_type": d.get("execution_type") or "Hybrid",
               "priority": d.get("priority"), "tshirt_size": d.get("tshirt_size"), "percent_complete": 0.0,
               "project_summary": d.get("project_summary"), "business_case": d.get("business_case"),
               "state": "Pending", "phase": "Initiation", "status": "Green", "on_hold": False,
               "approved_start_date": start.isoformat(), "planned_start_date": start.isoformat(), "planned_end_date": end.isoformat(),
               "approved_end_date": end.isoformat(), "budget_cost_eur": total, "total_planned_cost_eur": total,
               "planned_capital_eur": capex, "planned_operating_eur": opex, "actual_cost_eur": 0.0,
               "estimate_to_completion_eur": total, "estimate_at_completion_eur": total,
               "planned_benefit_eur": d.get("planned_benefit_eur"), "planned_return_eur": d.get("planned_return_eur"),
               "planned_roi_pct": d.get("planned_roi_pct"), "discount_rate_pct": d.get("discount_rate_pct"),
               "net_present_value_eur": d.get("net_present_value_eur")}
    project = {k: v for k, v in project.items() if not (v is None or (isinstance(v, float) and pd.isna(v)))
               and (ds.projects.empty or k in ds.projects.columns)}
    changes: list[dict[str, Any]] = [{"op": "append", "table": "Projects", "project_id": pid, "key": pid,
                                      "field": "Projects", "old": None, "new": f"{pid} from demand {demand}",
                                      "row": project, "source_tool": "demand_conversion"}]
    n = 0
    for r in money.to_dict(orient="records"):
        for kind, label in (("capex_eur", "CapEx"), ("opex_eur", "OpEx")):
            if r[kind] <= 0:
                continue
            for month in _months(int(r["year"]), int(r["quarter"])):
                n += 1
                number = f"CP-{pid}-{n:03d}"
                changes.append({"op": "append", "table": "Cost_Plans", "project_id": pid, "key": number,
                                "field": "Cost_Plans", "old": None, "new": f"{label} {month[:7]}",
                                "row": {"cost_plan_number": number, PK: pid, "month": month, "cost_type": label,
                                        "planned_cost_eur": round(r[kind] / 3, 2)},
                                "source_tool": "demand_conversion"})
    res = resource_plans(ds)
    for i, r in enumerate(res[res[DEMAND_KEY] == demand].to_dict(orient="records"), start=1):
        for j, month in enumerate(_months(int(r["year"]), int(r["quarter"])), start=1):
            number = f"RP-{pid}-{i:03d}-{j}"
            changes.append({"op": "append", "table": "Resource_Plans", "project_id": pid, "key": number,
                            "field": "Resource_Plans", "old": None, "new": f"{r['assignment_group']} {month[:7]}",
                            "row": {"resource_plan_number": number, PK: pid, "month": month,
                                    "assignment_group": r["assignment_group"], "capacity_hours": 0.0,
                                    "demand_hours": round(r["fte"] * HOURS_PER_FTE, 1), "utilization_pct": 0.0,
                                    "requested_fte": r["fte"], "allocated_fte": r["fte"]},
                            "source_tool": "demand_conversion"})
    baseline = f"BL-{pid}-001"
    changes.append({"op": "append", "table": "Baselines", "project_id": pid, "key": baseline, "field": "Baselines",
                    "old": None, "new": f"First baseline of {pid}",
                    "row": {"baseline_number": baseline, PK: pid, "name": clock.today().isoformat(),
                            "created_date": clock.today().isoformat(), "version": 1, "baseline_budget_eur": total,
                            "baseline_end_date": end.isoformat(), "baseline_eac_eur": total,
                            "reason": f"Approved demand {demand} converted", "approved_by": None,
                            "approval_date": None},
                    "source_tool": "demand_conversion"})
    changes += [{"table": "Demands", "project_id": demand, "key": demand, "field": "demand_status",
                 "old": APPROVED_DEMAND, "new": CONVERTED, "source_tool": "demand_conversion"},
                {"table": "Demands", "project_id": demand, "key": demand, "field": "converted_project_number",
                 "old": d.get("converted_project_number") if pd.notna(d.get("converted_project_number")) else None,
                 "new": pid, "source_tool": "demand_conversion"}]
    return changes


def submit_conversion(ds: PortfolioDataset, demand: str, by: str, role: str) -> dict[str, Any]:
    """The conversion sets a first baseline – a second PMO approves it in the inbox."""
    changes = conversion_changes(ds, demand, by)
    pid = changes[0]["key"]
    return proposals.submit_proposal(by, role, f"Convert approved demand {demand} into project {pid}",
                                     f"**Demand {demand} becomes project {pid}**\n\nMaster data 1:1, the quarterly money "
                                     "as monthly cost plans, the capacity as resource plans and the first baseline.",
                                     None, changes)


def project_of(ds: PortfolioDataset, demand: str) -> str | None:
    rows = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand] if not ds.demands.empty else ds.demands
    value = rows.iloc[0].get("converted_project_number") if not rows.empty else None
    return str(value) if value is not None and pd.notna(value) and str(value) else None


def demand_of(ds: PortfolioDataset, project: str) -> str | None:
    d = ds.demands
    if not d.empty and "converted_project_number" in d.columns:
        hit = d[d["converted_project_number"].astype(str) == project]
        if not hit.empty:
            return str(hit.iloc[0][DEMAND_KEY])
    return None
