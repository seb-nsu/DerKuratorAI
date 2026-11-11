"""The Planning page without its front end: the demand list and detail in the reader's scope, the inputs of a plan
scenario and their choices, the tables of a calculated scenario, the Portfolio Board's landing figures, the report
choices and the writes of the page (assessment, weight change, draft scenario, plan decision).

Used by the HTTP API (app/api/routes/planning.py). All figures come
from app/planning – deterministic."""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pandas as pd

from app.approval_flow import Outcome, audit, audit_best_effort, written_audit_failed
from app.core import clock, proposals
from app.core.business_rules import current_rules
from app.core.proposals import ProposalError
from app.core.security import ROLE_BOARD, ROLE_PMO, log_security_event
from app.data_adapters import DataConflictError, DataSourceError, PortfolioDataset, get_data_provider
from app.data_adapters.base import WriteNotAuthorizedError
from app.data_adapters.dataset import DEMAND_KEY
from app.planning import approval, assessment, store
from app.planning.model import horizon, resource_plans, start_quarter, year_plans, yearly_totals
from app.planning.scenario import METHODS, RANK, ScenarioInput, ScenarioResult, compare, run_scenario
from app.planning.scoring import ELIGIBLE_STATUSES, criteria, latest_scores, ranking, score_breakdown
from app.planning.steward import check_demands, known_scopes

logger = logging.getLogger("derkurator.views.planning")

DECISIONS = ("In", "Forced-in", "Deferred", "Out")
ASSESSABLE_STATUSES = ("Submitted", "Assessed", "Planned", "Deferred")
MASTER_FIELDS = ("requester", "project_sponsor", "business_service", "business_capability", "execution_type",
                 "tshirt_size", "strategic_goal", "mandatory_reason", "earliest_start_date", "latest_start_date",
                 "project_summary", "business_case", "converted_project_number")
SCOPE_KINDS = ("business_unit", "portfolio")
BUDGET_FACTOR_RANGE = (-30, 30)  # the envelopes larger or smaller by up to this share (in steps of 5 %)
BUDGET_FACTOR_STEP = 5


def sees_whole_plan(role: str) -> bool:
    """The PMO and the Portfolio Board see every demand, its rank and its place in the plan; a Project Manager only
    the own demands (a rank among the own demands only would mislead)."""
    return role in (ROLE_PMO, ROLE_BOARD)


# --------------------------------------------------------------------------- demands
def assessment_age(ds: PortfolioDataset) -> dict[str, str]:
    """Age of each demand's oldest current assessment ('120 days'), marked once it needs a fresh assessment."""
    scores = latest_scores(ds)
    if scores.empty:
        return {}
    oldest = pd.to_datetime(scores["assessed_on"], errors="coerce").groupby(scores[DEMAND_KEY].astype(str)).min()
    limit = current_rules().planning_score_max_age_days
    out = {}
    for did, on in oldest.items():
        if pd.notna(on):
            days = (pd.Timestamp(clock.today()) - on.normalize()).days
            out[did] = f"{days} days" + (" ▲ re-assess" if days > limit else "")
    return out


def demand_table(ds: PortfolioDataset, role: str) -> pd.DataFrame:
    """The demand list in rank order with the money per planning year; without the rank for a Project Manager."""
    ranked = ranking(ds)
    if ranked.empty:
        return ranked
    totals = yearly_totals(ds)
    first, last = horizon()
    frame = pd.DataFrame({
        "Rank": ranked["rank"], "Demand": ranked[DEMAND_KEY], "Name": ranked["demand_name"],
        "Business unit": ranked["business_unit"], "Portfolio": ranked["portfolio"], "Type": ranked["demand_type"],
        "Status": ranked["demand_status"], "Score": ranked["score"],
        "Start": ranked[DEMAND_KEY].map(start_quarter(ds)),
        "Assessed": ranked[DEMAND_KEY].map(assessment_age(ds)),
    })
    for year in range(first, last + 1):
        frame[str(year)] = ranked[DEMAND_KEY].map(totals[year] if year in getattr(totals, "columns", []) else {}).fillna(0)
    if not sees_whole_plan(role):
        frame = frame.drop(columns=["Rank"])
    return frame


def filter_demands(table: pd.DataFrame, units: list[str] | None = None, statuses: list[str] | None = None,
                   year: str | None = None) -> pd.DataFrame:
    """The demand list filtered by business unit, status and a planning year with money ('any year' / None = all)."""
    shown = table
    if units:
        shown = shown[shown["Business unit"].isin(units)]
    if statuses:
        shown = shown[shown["Status"].isin(statuses)]
    if year and year != "any year" and year in shown.columns:
        shown = shown[shown[year] > 0]
    return shown


def demand_issues(ds: PortfolioDataset, role: str) -> list:
    """Data-quality findings of the demands; references to other demands only for readers of the whole plan."""
    return check_demands(ds, references=sees_whole_plan(role))


def demand_row(ds: PortfolioDataset, demand: str) -> dict[str, Any] | None:
    rows = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand] if not ds.demands.empty else ds.demands
    return None if rows.empty else rows.iloc[0].to_dict()


def master_data(row: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([{"Field": f, "Value": "–" if pd.isna(row.get(f))
                          else str(row.get(f))[:10] if f.endswith("_date") else str(row.get(f))}
                         for f in MASTER_FIELDS if f in row])


def annual_figures(ds: PortfolioDataset, demand: str) -> pd.DataFrame:
    """CapEx, OpEx and benefit of the demand per quarter."""
    plans = year_plans(ds)
    plans = plans[plans[DEMAND_KEY] == demand]
    if plans.empty:
        return pd.DataFrame()
    show = plans.assign(Quarter=plans["year"].astype(str) + "-Q" + plans["quarter"].astype(str))
    return show[["Quarter", "capex_eur", "opex_eur", "benefit_eur", "confidence"]].rename(columns={
        "capex_eur": "CapEx", "opex_eur": "OpEx", "benefit_eur": "Benefit", "confidence": "Confidence"})


def capacity_demand(ds: PortfolioDataset, demand: str) -> pd.DataFrame:
    """The demand's FTE per team (rows) and quarter (columns)."""
    res = resource_plans(ds)
    res = res[res[DEMAND_KEY] == demand]
    if res.empty:
        return pd.DataFrame()
    res = res.assign(Quarter=res["year"].astype(str) + "-Q" + res["quarter"].astype(str))
    pivot = res.pivot_table(index="assignment_group", columns="Quarter", values="fte", aggfunc="sum",
                            fill_value=0).reset_index().rename(columns={"assignment_group": "Team"})
    pivot.columns.name = None
    return pivot


def assessment_parts(ds: PortfolioDataset, demand: str) -> pd.DataFrame:
    """The current score per criterion with its weighted contribution (their sum is the demand's score)."""
    parts = score_breakdown(ds)
    parts = parts[parts[DEMAND_KEY].astype(str) == demand]
    if parts.empty:
        return pd.DataFrame()
    return parts[["criterion", "score", "points", "weight", "contribution", "assessed_by", "assessed_on",
                  "comment"]].rename(columns=lambda c: c.replace("_", " ").capitalize())


# --------------------------------------------------------------------------- assessment and weights
def assessable_demands(ds: PortfolioDataset) -> pd.DataFrame:
    """Scored demands still open for an assessment, in rank order (mandatory demands are marked, not scored)."""
    ranked = ranking(ds)
    if ranked.empty:
        return ranked
    return ranked[(ranked["demand_type"] != "Mandatory") & ranked["demand_status"].isin(ASSESSABLE_STATUSES)]


def assessment_form(ds: PortfolioDataset, demand: str) -> list[dict[str, Any]]:
    """Every criterion with its scale, weight and the current score (else the lowest score) as the pre-fill."""
    current = score_breakdown(ds)
    current = current[current[DEMAND_KEY].astype(str) == demand].set_index("criterion")
    out = []
    for crit in criteria(ds).to_dict(orient="records"):
        value = current.at[crit["criterion"], "score"] if crit["criterion"] in current.index else crit["scale_min"]
        out.append({"criterion": crit["criterion"], "direction": crit["direction"], "weight": float(crit["weight"]),
                    "scale_min": float(crit["scale_min"]), "scale_max": float(crit["scale_max"]),
                    "value": float(value)})
    return out


def save_scores(ds: PortfolioDataset, demand: str, scores: dict[str, float], comments: dict[str, str], by: str,
                role: str) -> Any:
    """Appends the assessment (the latest score per criterion counts; history stays). Returns the write result.
    Raises ValueError (a score outside its scale) or the write errors."""
    changes = assessment.score_changes(ds, demand, scores, by, comments)
    return get_data_provider().apply_changes(changes, user=by, reason="demand_assessment", role=role)


def submit_weights(ds: PortfolioDataset, weights: dict[str, float], by: str, role: str) -> dict[str, Any] | None:
    """A weight change is a proposal a second PMO approves (four-eyes). None if no weight changes. Raises
    ValueError or ProposalError."""
    changes = assessment.weight_changes(ds, weights)
    if not changes:
        return None
    text = ", ".join(f"{c['key']} {c['old']:g} → {c['new']:g}" for c in changes)
    return proposals.submit_proposal(by, role, f"Change criterion weights: {text}",
                                     f"**Change of the scoring weights**\n\n{text}\n\nThe weights decide the rank of "
                                     "every demand – a second PMO approves (four-eyes, audit trail).", None, changes)


def convertible_demands(ds: PortfolioDataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(approved demands waiting for their conversion, demands already converted into a project)."""
    d = ds.demands
    if d.empty:
        return d, d
    converted = d[d["demand_status"] == approval.CONVERTED][[DEMAND_KEY, "demand_name", "converted_project_number"]]
    return d[d["demand_status"] == approval.APPROVED_DEMAND], converted.rename(columns={
        DEMAND_KEY: "Demand", "demand_name": "Name", "converted_project_number": "Project"})


# --------------------------------------------------------------------------- scenario inputs
def scenario_candidates(ds: PortfolioDataset) -> dict[str, str]:
    """Demands a scenario can force in, exclude or move: id -> 'id · name', in rank order."""
    ranked = ranking(ds)
    candidates = ranked[ranked["demand_status"].isin(ELIGIBLE_STATUSES)] if not ranked.empty else ranked
    return {d: f"{d} · {n}" for d, n in zip(candidates.get(DEMAND_KEY, []), candidates.get("demand_name", []),
                                            strict=True)}


def scope_options(ds: PortfolioDataset) -> list[str]:
    """'' (whole portfolio) and every 'business_unit:…' / 'portfolio:…' of the data."""
    scopes = known_scopes(ds)
    return [""] + [f"{k}:{v}" for k in SCOPE_KINDS for v in sorted(scopes.get(k, ()))]


def year_bounds(first: int, last: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Allowed ranges of the first and the last planning year of a scenario."""
    return (first - 1, last + 5), (first - 1, last + 7)


def build_scenario_input(name: str, year_from: int, year_to: int, budget_factor_pct: float, scope: str | None,
                         forced: list[str], forced_reason: str, excluded: list[str], later: list[str],
                         earlier: list[str], method: str) -> ScenarioInput:
    """The scenario of the PMO's choices: one reason for every forced demand, the last year never before the first."""
    return ScenarioInput(name=name.strip() or "Scenario", year_from=int(year_from),
                         year_to=int(max(year_from, year_to)), budget_factor_pct=float(budget_factor_pct),
                         scope=scope or None, forced_in={d: forced_reason.strip() for d in forced},
                         excluded={d: "" for d in excluded}, moves={**{d: 1 for d in later}, **{d: -1 for d in earlier}},
                         method=method)


def check_scenario_choices(ds: PortfolioDataset, year_from: int, year_to: int, budget_factor_pct: float,
                           scope: str | None, forced: list[str], excluded: list[str], later: list[str],
                           earlier: list[str], method: str) -> None:
    """The choices the planning inputs offer, checked for a client that sends them itself. Raises ValueError."""
    first, last = horizon()
    (from_low, from_high), (to_low, to_high) = year_bounds(first, last)
    if not from_low <= year_from <= from_high or not to_low <= year_to <= to_high:
        raise ValueError(f"Planning years must lie within {from_low}–{to_high}.")
    low, high = BUDGET_FACTOR_RANGE
    if not low <= budget_factor_pct <= high or budget_factor_pct % BUDGET_FACTOR_STEP:
        raise ValueError(f"The budget factor is a multiple of {BUDGET_FACTOR_STEP} % between {low} and {high} %.")
    if (scope or "") not in scope_options(ds):
        raise ValueError("Unknown scope.")
    if method not in METHODS:
        raise ValueError("Unknown filling method.")
    candidates = set(scenario_candidates(ds))
    unknown = sorted((set(forced) | set(excluded) | set(later) | set(earlier)) - candidates)
    if unknown:
        raise ValueError(f"Not a demand open for planning: {', '.join(unknown)}")
    if set(forced) & set(excluded) or set(later) & set(excluded) or set(earlier) & (set(excluded) | set(later)):
        raise ValueError("A demand can be forced in, excluded or moved – only one of them (a move not when excluded).")


# --------------------------------------------------------------------------- calculated scenario
def decision_counts(result: ScenarioResult) -> dict[str, int]:
    counts = result.items["decision"].value_counts() if not result.items.empty else pd.Series(dtype=int)
    return {k: int(counts.get(k, 0)) for k in DECISIONS}


def scenario_items(result: ScenarioResult) -> pd.DataFrame:
    """The decision per demand with its start, end, peak FTE and money per planning year."""
    items = result.items
    if items.empty:
        return items
    money = [c for c in items.columns if c.startswith(("CapEx ", "OpEx "))]
    return items[["rank", DEMAND_KEY, "demand_name", "decision", "reason", "start", "end", "demand_type", "score",
                  "business_unit", "fte_peak", *money]].rename(columns={
                      "rank": "Rank", DEMAND_KEY: "Demand", "demand_name": "Name", "business_unit": "Business unit",
                      "demand_type": "Type", "score": "Score", "decision": "Decision", "start": "Start", "end": "End",
                      "fte_peak": "Peak FTE", "reason": "Reason"})


def budget_lights(budget: pd.DataFrame) -> pd.DataFrame:
    """The envelopes with their traffic light: over the limit, tight from 90 %, else OK."""
    if budget.empty:
        return budget
    lights = budget.assign(Status=[("■ Over limit" if u is not None and u > 100 else "▲ Tight" if u is not None
                                    and u >= 90 else "● OK") for u in budget["Used %"]])
    money = ("Cap", "Limit", "Committed", "Planned", "Free")
    return lights[["Status", "Scope type", "Scope", "Year", "Quarter", "Kind", "Cap type", *money, "Used %"]]


def capacity_grid(result: ScenarioResult) -> pd.DataFrame:
    """Used % of the usable FTE per team (rows) and quarter (columns), running projects included."""
    cap = result.capacity
    if cap.empty:
        return pd.DataFrame()
    return cap.pivot_table(index="Team", columns="Quarter", values="Used %", aggfunc="first")


def capacity_heatmap(grid: pd.DataFrame):
    """The capacity grid as a Plotly heat map: darker = fuller, above 100 % the team is overbooked."""
    import plotly.express as px

    fig = px.imshow(grid.round(0), text_auto=True, aspect="auto", color_continuous_scale="Blues", zmin=0, zmax=120,
                    labels={"color": "Used %", "x": "", "y": ""})
    fig.update_layout(height=80 + 30 * len(grid), margin=dict(l=10, r=10, t=10, b=10))
    return fig


def optimizer_comparison(ds: PortfolioDataset, result: ScenarioResult) -> tuple[list[str], pd.DataFrame, pd.DataFrame]:
    """What the value-optimal choice does differently from the rank order, and why: (summary, figures, changes)."""
    return compare(run_scenario(ds, replace(result.input, method=RANK), result.as_of), result)


def save_draft(ds: PortfolioDataset, result: ScenarioResult, by: str, role: str) -> tuple[str, Any]:
    """Stores the input and the decision per demand as a draft (the PMO writes it directly; the approval follows
    separately). Returns (scenario id, write result). Raises ValueError or the write errors."""
    changes = store.save_changes(ds, result, by)
    written = get_data_provider().apply_changes(changes, user=by, reason="planning_scenario", role=role)
    return changes[0]["row"]["scenario_id"], written


# --------------------------------------------------------------------------- annual plan
def pending_plans() -> list[dict[str, Any]]:
    """Annual plans waiting for the Portfolio Board."""
    return [p for p in proposals.list_proposals(status=proposals.SUBMITTED) if p.get("kind") == proposals.PLAN]


def plan_change_rows(proposal: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([{"Table": c.get("table"), "Key": c.get("key"), "Change": c.get("new")}
                         for c in proposal.get("changes") or []])


def board_summary(ds: PortfolioDataset, base: ScenarioResult, pending: list[dict[str, Any]]) -> dict[str, Any]:
    """The Board's landing: what waits, which plan is in force, how the base plan stands today."""
    plan = store.approved_plan(ds)
    return {"waiting": len(pending),
            "plan_in_force": None if plan is None else {
                "scenario_id": plan["scenario_id"], "scenario_name": plan["scenario_name"],
                "year_from": int(plan["year_from"]), "year_to": int(plan["year_to"]),
                "approved_by": plan.get("approved_by") or None},
            "base_plan_verdict": base.verdict.label, "demands": len(ds.demands),
            "in_base_plan": int((base.items["decision"] == "In").sum()) if not base.items.empty else 0}


def decide_plan(proposal_id: str, approve: bool, by: str, role: str, comment: str = "",
                override: bool = False) -> Outcome:
    """The Portfolio Board's decision on an annual plan with its audit entry (only the Board decides a plan, never
    its own requester – enforced in app/planning/approval.py and the proposal store). Approving writes the plan
    with a snapshot; a NO-GO plan only with an override and its reason."""
    proposal = proposals.get_proposal(proposal_id)
    try:
        if approve:
            result = approval.approve_plan(proposal_id, by, role, get_data_provider(), comment=comment,
                                           override=override)
        else:
            approval.reject_plan(proposal_id, by, role, comment)
    except (ProposalError, WriteNotAuthorizedError, DataConflictError, DataSourceError) as exc:
        return Outcome("error", f"Not decided: {exc}")
    requester = (proposal or {}).get("requester")
    if not approve:
        audit_best_effort("plan_rejected", role=role, user_id=by, proposal_id=proposal_id, requester=requester)
        log_security_event("Annual plan sent back (Portfolio Board)", role=role, user=by, proposal=proposal_id)
        return Outcome("success", "Annual plan sent back to the PMO.")
    overridden = bool(override and proposals.requires_override((proposal or {}).get("verdict")))
    try:
        audit("plan_approved", role=role, user_id=by, proposal_id=proposal_id, requester=requester,
              applied=result.applied, snapshot_id=result.snapshot_id, operation_id=result.operation_id,
              override=overridden, verdict=(proposal or {}).get("verdict"),
              verdict_record=(proposal or {}).get("verdict_record"))
    except Exception:  # noqa: BLE001 - written and decided: say so, never "nothing was confirmed"
        return Outcome("warning", written_audit_failed(result.applied, result.snapshot_id), written=True,
                       snapshot_id=result.snapshot_id)
    log_security_event("Annual plan approved with verdict override (Portfolio Board)" if overridden else
                       "Annual plan approved (Portfolio Board)", role=role, user=by, proposal=proposal_id,
                       fields=result.applied, override=overridden, operation=result.operation_id)
    return Outcome("success", f"Annual plan approved (operation {result.operation_id}, snapshot "
                              f"{result.snapshot_id}).", written=True, snapshot_id=result.snapshot_id)


def report_options(ds: PortfolioDataset, calculated: dict[str, ScenarioResult] | None = None) -> dict[str, ScenarioInput]:
    """The plans a board pack can show: the approved plan, every saved scenario, the scenarios calculated in this
    session and the base plan."""
    options: dict[str, ScenarioInput] = {}
    plan = store.approved_plan(ds)
    if plan is not None:
        options[f"Approved plan {plan['scenario_id']}"] = store.input_of(plan)
    saved = store.saved_scenarios(ds)
    for row in saved.to_dict(orient="records") if not saved.empty else []:
        options.setdefault(f"{row['scenario_id']} · {row['scenario_name']} ({row['scenario_status']})",
                           store.input_of(row))
    for name, result in (calculated or {}).items():
        options.setdefault(f"Calculated: {name}", result.input)
    options.setdefault("Base plan (today's data)", ScenarioInput())
    return options
