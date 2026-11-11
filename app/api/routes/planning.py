"""Planning: demands and their assessment, plan scenarios against the budget envelopes and capacity limits, and the
annual plan the Portfolio Board approves – the logic lives in app/views/planning.py and
app/planning/.

Visibility as on the page: a Project Manager sees only the demands they own (no rank, no plan position); the PMO and
the Portfolio Board see everything. The PMO assesses, calculates and saves scenarios, submits weight changes and
conversions (a second PMO approves them in /proposals) and submits a plan; only the Portfolio Board decides a plan
(proposals of kind `plan`), never its own. A demand or plan outside the caller's scope answers 404.

Scenarios are stateless: the client sends the scenario input, the server calculates it on today's data (saving a
draft recalculates it too)."""
from __future__ import annotations

import json
import logging
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app import approval_flow
from app.api import limits
from app.api.deps import Scope, clear_data_cache, data_user, records, scope
from app.api.planning_models import (
    AssessmentCandidates,
    AssessmentForm,
    BoardLanding,
    ComparisonView,
    Conversions,
    CriteriaList,
    DecisionOutcome,
    DemandDetail,
    DemandList,
    DraftSaved,
    MultiYearReport,
    PlanAnalysis,
    PlanDetail,
    PlanList,
    PlanningContext,
    ProposalAnswer,
    ReportOptions,
    SavedScenarios,
    ScenarioOptions,
    ScenarioView,
    SlipImpact,
    WriteResult,
)
from app.api.routes.proposals import _summary
from app.approval_flow import audit_best_effort
from app.core import clock, proposals
from app.core.auth import User
from app.core.formatting import field_label
from app.core.proposals import ProposalError
from app.core.security import ROLE_BOARD, ROLE_PMO, log_security_event
from app.data_adapters import DataConflictError, DataSourceError, get_data_provider
from app.data_adapters.base import WriteNotAuthorizedError
from app.planning import analysis, approval, reports, store
from app.planning.model import horizon
from app.planning.scenario import METHODS, VALUE, ScenarioResult, run_scenario
from app.planning.scoring import criteria
from app.views import planning as views

router = APIRouter(prefix="/planning", tags=["planning"])
logger = logging.getLogger("derkurator.api.planning")

MAX_COMPARED = 3
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REFUSED = {409: {"model": DecisionOutcome, "description": "Not decided (the message says why)"}}
BOARD_PACK = {200: {"content": {XLSX: {}, "text/html": {}},
                    "description": "The multi-year view (json) or the board pack (xlsx, html)"}}
HEADER_FIELDS = ("demand_number", "demand_name", "demand_status", "demand_type", "business_unit", "portfolio",
                 "demand_owner", "duration_months", "net_present_value_eur", "run_cost_after_go_live_eur",
                 "mandatory_reason")


# --------------------------------------------------------------------------- role gates
def planner(user: User = Depends(data_user)) -> User:
    """The PMO: assessment, weights, scenarios, conversion, plan submission."""
    if user.role != ROLE_PMO:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the PMO works on the plan.")
    return user


def plan_reader(user: User = Depends(data_user)) -> User:
    """Readers of the whole plan: the PMO and the Portfolio Board."""
    if not views.sees_whole_plan(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the PMO and the Portfolio Board see the whole plan.")
    return user


def board(user: User = Depends(data_user)) -> User:
    if user.role != ROLE_BOARD:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The annual plan is decided by the Portfolio Board.")
    return user


def _refused(exc: Exception, what: str) -> HTTPException:
    """A refused planning action as the answer the page shows as its error message."""
    if isinstance(exc, WriteNotAuthorizedError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, DataConflictError | ProposalError | approval.PlanError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, DataSourceError):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:  # ValueError: an input outside its scale or choices
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, f"{what}: {exc}")


def _demand(ctx: Scope, demand_id: str) -> dict:
    row = views.demand_row(ctx.visible, demand_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demand not found.")
    return row


def _figure(fig) -> dict:
    return json.loads(fig.to_json())


# --------------------------------------------------------------------------- scenario input and result
class ScenarioBody(BaseModel):
    name: str = Field("Base plan", max_length=120)
    year_from: int | None = None  # default: the first planning year
    year_to: int | None = None  # default: the last planning year
    budget_factor_pct: float = 0.0  # every envelope larger or smaller by this share (multiple of 5, -30..30)
    scope: str = Field("", max_length=200)  # "" = whole portfolio, "business_unit:…" or "portfolio:…"
    forced_in: list[str] = Field(default_factory=list, max_length=200)
    forced_reason: str = Field("", max_length=500)  # one reason for every forced demand
    excluded: list[str] = Field(default_factory=list, max_length=200)
    later: list[str] = Field(default_factory=list, max_length=200)  # move one year later
    earlier: list[str] = Field(default_factory=list, max_length=200)  # move one year earlier
    method: str = "rank"  # rank | value


def _scenario_input(ctx: Scope, body: ScenarioBody):
    first, last = horizon()
    year_from = body.year_from if body.year_from is not None else first
    year_to = body.year_to if body.year_to is not None else last
    try:
        views.check_scenario_choices(ctx.visible, year_from, year_to, body.budget_factor_pct, body.scope,
                                     body.forced_in, body.excluded, body.later, body.earlier, body.method)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return views.build_scenario_input(body.name, year_from, year_to, body.budget_factor_pct, body.scope,
                                      body.forced_in, body.forced_reason, body.excluded, body.later, body.earlier,
                                      body.method)


def _verdict_json(result: ScenarioResult) -> dict:
    return {"name": result.input.name, "input": result.input.to_dict(), "input_text": store.spec_text(result.input),
            "years": list(result.years), "binding_year": result.binding_year, "as_of": result.as_of.isoformat(),
            "verdict": result.verdict.to_dict(), "estimated_commitments": result.estimated_commitments,
            "solved": result.solved}


def _scenario_json(ctx: Scope, result: ScenarioResult) -> dict:
    grid = views.capacity_grid(result)
    out = {**_verdict_json(result), "decision_counts": views.decision_counts(result),
           "items": records(views.scenario_items(result)), "budget": records(views.budget_lights(result.budget)),
           "capacity": records(result.capacity),
           "capacity_grid": records(grid.reset_index()) if not grid.empty else [],
           "capacity_chart": _figure(views.capacity_heatmap(grid)) if not grid.empty else None, "optimizer": None}
    if result.input.method == VALUE:
        summary, figures, changes = views.optimizer_comparison(ctx.visible, result)
        out["optimizer"] = {"summary": summary, "figures": records(figures), "changes": records(changes)}
    return out


# --------------------------------------------------------------------------- page and demands
@router.get("")
def planning_context(user: User = Depends(data_user), ctx: Scope = Depends(scope)) -> PlanningContext:
    """What the page shows the caller: planning horizon, demands in scope and the sections of the role."""
    first, last = horizon()
    sections = ["demands"]
    if user.role == ROLE_BOARD:
        sections = ["plan_approval", "demands", "analysis", "saved_scenarios", "reports"]
    elif user.role == ROLE_PMO:
        sections += ["analysis", "scenarios", "saved_scenarios", "reports", "pmo_tools"]
    return {"first_year": first, "last_year": last, "reference_date": clock.today().isoformat(),
            "demands": len(ctx.visible.demands), "sections": sections, "methods": METHODS}


@router.get("/demands")
def demands(business_unit: list[str] = Query([]), demand_status: list[str] = Query([], alias="status"),
            year: str | None = Query(None, description="Has money in this year"), user: User = Depends(data_user),
            ctx: Scope = Depends(scope)) -> DemandList:
    table = views.demand_table(ctx.visible, user.role)
    issues = views.demand_issues(ctx.visible, user.role)
    findings = [{"demand": i.project_id, "severity": getattr(i.severity, "value", i.severity), "field": i.column,
                 "message": i.message} for i in issues]
    if table.empty:
        return {"demands": [], "total": 0, "rank_shown": False, "filters": {}, "data_quality_findings": findings}
    first, last = horizon()
    shown = views.filter_demands(table, business_unit, demand_status, year)
    return {"demands": records(shown), "total": len(shown), "rank_shown": views.sees_whole_plan(user.role),
            "filters": {"business_units": sorted(table["Business unit"].dropna().unique()),
                        "statuses": sorted(table["Status"].dropna().unique()),
                        "years": [str(y) for y in range(first, last + 1)]},
            "data_quality_findings": findings}


@router.get("/demands/{demand_id}")
def demand_detail(demand_id: str, user: User = Depends(data_user), ctx: Scope = Depends(scope)) -> DemandDetail:
    row = _demand(ctx, demand_id)
    ds, full = ctx.visible, views.sees_whole_plan(user.role)
    plan = analysis.base_plan(ds) if full else None
    master = views.master_data(row)
    figures = views.annual_figures(ds, demand_id)
    parts = views.assessment_parts(ds, demand_id)
    chain = analysis.dependency_chain(ds, demand_id, plan)
    out = {
        "demand": records(pd.DataFrame([{f: row.get(f) for f in HEADER_FIELDS}]))[0],
        "master_data": [{**r, "label": field_label(r["Field"])} for r in records(master)],
        "annual_figures": records(figures),
        "annual_totals": {"capex_eur": float(figures["CapEx"].sum()) if not figures.empty else 0.0,
                          "opex_eur": float(figures["OpEx"].sum()) if not figures.empty else 0.0},
        "capacity": records(views.capacity_demand(ds, demand_id)),
        "assessment": {"mandatory": row.get("demand_type") == "Mandatory", "parts": records(parts),
                       "score": float(parts["Contribution"].sum()) if not parts.empty else None},
        "dependencies": records(chain),
        "outside_scope_links": analysis.hidden_links(ds, demand_id),  # a neutral hint: no ids, no count
        "can_slip": bool(not chain.empty and (chain["Direction"] == "Needed by").any()),
        "position": None,
    }
    if full:
        item = plan.items[plan.items["demand_number"] == demand_id] if not plan.items.empty else plan.items
        first = item.iloc[0] if not item.empty else None
        out["position"] = {
            "decision": None if first is None else first["decision"],
            "reason": None if first is None else (str(first.get("reason") or "").strip() or None),
            "budget": records(analysis.budget_position(ds, demand_id, plan)),
            "capacity": records(analysis.capacity_position(ds, demand_id, plan))}
    return out


@router.get("/demands/{demand_id}/slip")
def demand_slip(demand_id: str, quarters: int = Query(1, ge=1, le=4), user: User = Depends(data_user),
                ctx: Scope = Depends(scope)) -> SlipImpact:
    """What a delay of the demand by 1-4 quarters pushes along (only items in the caller's view are named)."""
    _demand(ctx, demand_id)
    plan = analysis.base_plan(ctx.visible) if views.sees_whole_plan(user.role) else None
    impact = analysis.slip_impact(ctx.visible, demand_id, quarters, plan)
    late = int((impact["Still fits"] != "yes").sum()) if not impact.empty else 0
    return {"quarters": quarters, "moves": records(impact), "moved": len(impact), "late": late}


# --------------------------------------------------------------------------- whole plan (PMO, Board)
@router.get("/analysis")
def plan_analysis(user: User = Depends(plan_reader), ctx: Scope = Depends(scope)) -> PlanAnalysis:
    """The base plan (rank order, today's data): verdict, budget and capacity hot spots, critical chains."""
    plan = analysis.base_plan(ctx.visible)
    budget, capacity = analysis.hot_spots(plan)
    return {**_verdict_json(plan), "hot_share": analysis.HOT_SHARE,
            "budget_hot_spots": records(budget.drop(columns=["Cap"], errors="ignore")),
            "capacity_hot_spots": records(capacity),
            "critical_chains": records(analysis.critical_chains(ctx.visible, plan))}


@router.get("/board")
def board_landing(user: User = Depends(plan_reader), ctx: Scope = Depends(scope)) -> BoardLanding:
    """The Board's landing figures: plans waiting, the plan in force, the base plan's verdict today."""
    approval_flow.run_deadlines()
    return views.board_summary(ctx.visible, analysis.base_plan(ctx.visible), views.pending_plans())


# --------------------------------------------------------------------------- PMO tools
@router.get("/criteria")
def scoring_criteria(user: User = Depends(planner), ctx: Scope = Depends(scope)) -> CriteriaList:
    model = criteria(ctx.visible)
    return {"criteria": records(model[["criterion", "weight", "scale_min", "scale_max", "direction"]])
            if not model.empty else []}


@router.get("/assessment")
def assessment_candidates(user: User = Depends(planner), ctx: Scope = Depends(scope)) -> AssessmentCandidates:
    """Scored demands open for an assessment (mandatory demands are marked, not scored)."""
    open_ = views.assessable_demands(ctx.visible)
    return {"demands": records(open_[["demand_number", "demand_name", "demand_status", "score"]])
            if not open_.empty else []}


@router.get("/demands/{demand_id}/assessment")
def assessment_form(demand_id: str, user: User = Depends(planner), ctx: Scope = Depends(scope)) -> AssessmentForm:
    _demand(ctx, demand_id)
    return {"demand": demand_id, "criteria": views.assessment_form(ctx.visible, demand_id)}


class Assessment(BaseModel):
    scores: dict[str, float] = Field(min_length=1, max_length=50)
    comments: dict[str, str] = Field(default_factory=dict, max_length=50)


@router.post("/demands/{demand_id}/assessment")
def save_assessment(demand_id: str, body: Assessment, user: User = Depends(planner),
                    ctx: Scope = Depends(scope)) -> WriteResult:
    """Appends the scores (the latest per criterion counts, history stays) – written directly, as on the page."""
    _demand(ctx, demand_id)
    if demand_id not in set(views.assessable_demands(ctx.visible).get("demand_number", [])):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{demand_id} is not open for an assessment.")
    if any(len(c) > 500 for c in body.comments.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A comment has at most 500 characters.")
    try:
        result = views.save_scores(ctx.visible, demand_id, body.scores, body.comments, user.user_id, user.role)
    except (ValueError, DataSourceError) as exc:
        raise _refused(exc, "Assessment not saved") from exc
    clear_data_cache()
    audit_best_effort("demand_assessed", role=user.role, user_id=user.user_id, demand=demand_id,
                      criteria=sorted(body.scores), applied=result.applied, snapshot_id=result.snapshot_id)
    return {"message": f"Assessment of {demand_id} saved ({result.applied} change(s), snapshot "
                       f"{result.snapshot_id}).", "applied": result.applied, "snapshot_id": result.snapshot_id}


class Weights(BaseModel):
    weights: dict[str, float] = Field(min_length=1, max_length=50)


@router.post("/weights", status_code=status.HTTP_201_CREATED)
def submit_weights(body: Weights, response: Response, user: User = Depends(planner),
                   ctx: Scope = Depends(scope)) -> ProposalAnswer:
    """A weight change decides the rank of every demand: a proposal a second PMO approves (POST /proposals/{id}/approve).
    Answers 200 without a proposal when no weight changes."""
    try:
        entry = views.submit_weights(ctx.visible, body.weights, user.user_id, user.role)
    except (ValueError, ProposalError) as exc:
        raise _refused(exc, "Weights not submitted") from exc
    if entry is None:
        response.status_code = status.HTTP_200_OK
        return ProposalAnswer(message="No weight changed.")
    audit_best_effort("weight_change_submitted", role=user.role, user_id=user.user_id, proposal_id=entry["id"],
                      changes=entry["changes"])
    log_security_event("Weight change proposal submitted", role=user.role, user=user.user_id, proposal=entry["id"],
                       changes=len(entry["changes"]))
    return ProposalAnswer(message=f"Weight change submitted as proposal {entry['id']} – a second PMO approves it in "
                                  "Approvals.", proposal=_summary(entry))


@router.get("/conversions")
def conversions(user: User = Depends(planner), ctx: Scope = Depends(scope)) -> Conversions:
    """Approved demands waiting for their conversion into a project, and the demands already converted."""
    approved, converted = views.convertible_demands(ctx.visible)
    return {"approved": records(approved[["demand_number", "demand_name"]]) if not approved.empty else [],
            "converted": records(converted)}


class Conversion(BaseModel):
    demand: str = Field(min_length=1, max_length=64)


@router.post("/conversions", status_code=status.HTTP_201_CREATED)
def convert(body: Conversion, user: User = Depends(planner), ctx: Scope = Depends(scope)) -> ProposalAnswer:
    """The conversion sets a first baseline: a proposal a second PMO approves (POST /proposals/{id}/approve)."""
    _demand(ctx, body.demand)
    try:
        entry = approval.submit_conversion(ctx.visible, body.demand, user.user_id, user.role)
    except (approval.PlanError, ProposalError) as exc:
        raise _refused(exc, "Not converted") from exc
    audit_best_effort("conversion_submitted", role=user.role, user_id=user.user_id, proposal_id=entry["id"],
                      demand=body.demand, project=entry["changes"][0]["key"])
    log_security_event("Demand conversion proposal submitted", role=user.role, user=user.user_id,
                       proposal=entry["id"], changes=len(entry["changes"]))
    return {"message": f"Conversion of {body.demand} into {entry['changes'][0]['key']} submitted – a second PMO "
                       "approves it in Approvals (it sets the first baseline).", "proposal": _summary(entry)}


# --------------------------------------------------------------------------- scenarios
@router.get("/scenario-options")
def scenario_options(user: User = Depends(planner), ctx: Scope = Depends(scope)) -> ScenarioOptions:
    """The choices of the scenario inputs: demands open for planning, scopes, years, budget factor, methods."""
    first, last = horizon()
    (from_low, from_high), (to_low, to_high) = views.year_bounds(first, last)
    low, high = views.BUDGET_FACTOR_RANGE
    return {"candidates": [{"demand": d, "label": label} for d, label in views.scenario_candidates(ctx.visible).items()],
            "scopes": views.scope_options(ctx.visible), "methods": METHODS,
            "year_from": {"default": first, "min": from_low, "max": from_high},
            "year_to": {"default": last, "min": to_low, "max": to_high},
            "budget_factor_pct": {"default": 0, "min": low, "max": high, "step": views.BUDGET_FACTOR_STEP},
            "max_compared": MAX_COMPARED}


@router.post("/scenarios/calculate")
def calculate(body: ScenarioBody, user: User = Depends(planner), ctx: Scope = Depends(scope)) -> ScenarioView:
    """One plan scenario on today's data: verdict, decision per demand, budget, capacity (with the heat map as Plotly
    figure JSON) and, for the value-optimal filling, what it does differently from the rank order."""
    with limits.heavy(user.user_id):
        return _scenario_json(ctx, run_scenario(ctx.visible, _scenario_input(ctx, body)))


class Comparison(BaseModel):
    scenarios: list[ScenarioBody] = Field(min_length=2, max_length=MAX_COMPARED)


@router.post("/scenarios/compare")
def compare_scenarios(body: Comparison, user: User = Depends(planner), ctx: Scope = Depends(scope)) -> ComparisonView:
    """Two or three scenarios side by side."""
    with limits.heavy(user.user_id):
        results = [run_scenario(ctx.visible, _scenario_input(ctx, s)) for s in body.scenarios]
    return {"comparison": records(store.comparison(results)), "scenarios": [_verdict_json(r) for r in results]}


@router.get("/scenarios")
def saved_scenarios(user: User = Depends(plan_reader), ctx: Scope = Depends(scope)) -> SavedScenarios:
    """Saved plan scenarios (latest status each, newest first); drafts can be submitted by the PMO."""
    saved = store.saved_scenarios(ctx.visible)
    if saved.empty:
        return {"scenarios": []}
    shown = saved[["scenario_id", "scenario_name", "year_from", "year_to", "scenario_status", "verdict", "created_by",
                   "created_on"]]
    return {"scenarios": records(shown.assign(submittable=(shown["scenario_status"] == store.DRAFT)
                                              & (user.role == ROLE_PMO)))}


@router.post("/scenarios", status_code=status.HTTP_201_CREATED)
def save_scenario(body: ScenarioBody, user: User = Depends(planner), ctx: Scope = Depends(scope)) -> DraftSaved:
    """Saves the scenario as a draft (input and decision per demand, recalculated on today's data); the approval
    follows separately (POST /planning/scenarios/{id}/submit)."""
    result = run_scenario(ctx.visible, _scenario_input(ctx, body))
    try:
        scenario_id, written = views.save_draft(ctx.visible, result, user.user_id, user.role)
    except (ValueError, DataSourceError) as exc:
        raise _refused(exc, "Plan scenario not saved") from exc
    clear_data_cache()
    audit_best_effort("plan_scenario_saved", role=user.role, user_id=user.user_id, scenario_id=scenario_id,
                      verdict=result.verdict.label, applied=written.applied, snapshot_id=written.snapshot_id)
    return {"message": f"Plan scenario “{result.input.name}” saved as draft {scenario_id} ({written.applied} row(s)).",
            "scenario_id": scenario_id, "applied": written.applied, "verdict": result.verdict.label}


@router.post("/scenarios/{scenario_id}/submit", status_code=status.HTTP_201_CREATED)
def submit_scenario(scenario_id: str, user: User = Depends(planner), ctx: Scope = Depends(scope)) -> ProposalAnswer:
    """Recalculates the draft on today's data and puts the annual plan before the Portfolio Board."""
    saved = store.saved_scenarios(ctx.visible)
    if saved.empty or scenario_id not in set(saved["scenario_id"].astype(str)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scenario not found.")
    try:
        entry = approval.submit_plan(ctx.visible, scenario_id, user.user_id, user.role, get_data_provider())
    except (approval.PlanError, ProposalError, DataSourceError) as exc:
        raise _refused(exc, "Plan not submitted") from exc
    clear_data_cache()
    audit_best_effort("plan_submitted", role=user.role, user_id=user.user_id, proposal_id=entry["id"],
                      scenario_id=scenario_id, verdict=entry["verdict"], changes=len(entry["changes"]))
    log_security_event("Annual plan submitted to the Portfolio Board", role=user.role, user=user.user_id,
                       proposal=entry["id"], changes=len(entry["changes"]))
    return {"message": f"Scenario {scenario_id} submitted to the Portfolio Board (proposal {entry['id']}, verdict "
                       f"{entry['verdict']}).", "proposal": _summary(entry)}


# --------------------------------------------------------------------------- annual plan decision
def _plan_visible(proposal: dict | None, user: User) -> bool:
    """The Board sees every annual plan, the PMO the plans it submitted; nobody else any."""
    if proposal is None or proposal.get("kind") != proposals.PLAN:
        return False
    return user.role == ROLE_BOARD or (user.role == ROLE_PMO and proposal.get("requester") == user.user_id)


def _plan(proposal_id: str, user: User) -> dict:
    entry = proposals.get_proposal(proposal_id)
    if not _plan_visible(entry, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    return entry


@router.get("/plans")
def plans(status_filter: str | None = Query(None, alias="status"), user: User = Depends(plan_reader)) -> PlanList:
    """Annual plans submitted to the Portfolio Board; `?status=submitted` is the Board's inbox."""
    approval_flow.run_deadlines()
    items = [p for p in proposals.list_proposals(status=status_filter) if _plan_visible(p, user)]
    return {"plans": [{**_summary(p), "requires_override": proposals.requires_override(p.get("verdict")),
                       "change_count": len(p.get("changes") or [])} for p in items]}


@router.get("/plans/{proposal_id}")
def plan(proposal_id: str, user: User = Depends(plan_reader)) -> PlanDetail:
    entry = _plan(proposal_id, user)
    return {**_summary(entry), "paper": entry.get("paper"), "verdict_record": entry.get("verdict_record"),
            "requires_override": proposals.requires_override(entry.get("verdict")),
            "change_set": records(views.plan_change_rows(entry))}


class PlanDecision(BaseModel):
    comment: str = Field("", max_length=2000)
    override: bool = False  # approve against a NO-GO / incomplete verdict: needs the reason in `comment`


class PlanRejection(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


def _decided(outcome: approval_flow.Outcome) -> JSONResponse:
    if outcome.written or outcome.ok:
        clear_data_cache()
    code = status.HTTP_200_OK if outcome.kind != "error" else status.HTTP_409_CONFLICT
    return JSONResponse({"kind": outcome.kind, "message": outcome.message, "snapshot_id": outcome.snapshot_id},
                        status_code=code)


@router.post("/plans/{proposal_id}/approve", response_model=DecisionOutcome, responses=REFUSED)
def approve_plan(proposal_id: str, body: PlanDecision, user: User = Depends(board)) -> JSONResponse:
    """The Portfolio Board approves and writes the plan (snapshot first). Never its own; a NO-GO plan only with
    `override` and the reason in `comment`."""
    _plan(proposal_id, user)
    return _decided(views.decide_plan(proposal_id, True, user.user_id, user.role, comment=body.comment.strip(),
                                      override=body.override))


@router.post("/plans/{proposal_id}/reject", response_model=DecisionOutcome, responses=REFUSED)
def reject_plan(proposal_id: str, body: PlanRejection, user: User = Depends(board)) -> JSONResponse:
    """The Portfolio Board sends the plan back to the PMO with a reason."""
    _plan(proposal_id, user)
    if not body.comment.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Sending the plan back needs a comment.")
    return _decided(views.decide_plan(proposal_id, False, user.user_id, user.role, comment=body.comment.strip()))


# --------------------------------------------------------------------------- reports
@router.get("/reports")
def report_options(user: User = Depends(plan_reader), ctx: Scope = Depends(scope)) -> ReportOptions:
    """The plans a board pack can show (approved plan, saved scenarios, base plan); the PMO can also send a scenario
    input of its own."""
    return {"options": list(views.report_options(ctx.visible))}


class ReportRequest(BaseModel):
    option: str | None = Field(None, max_length=300)  # one of GET /planning/reports
    scenario: ScenarioBody | None = None  # PMO: a calculated scenario instead of an option
    compared: list[ScenarioBody] = Field(default_factory=list, max_length=MAX_COMPARED)  # PMO: comparison sheet
    format: Literal["json", "xlsx", "html"] = "json"


@router.post("/reports", response_model=MultiYearReport, responses=BOARD_PACK)
def report(body: ReportRequest, user: User = Depends(plan_reader), ctx: Scope = Depends(scope)) -> Response:
    """The multi-year view of a plan (json) or its board pack: Excel workbook (xlsx) or print-ready page (html)."""
    if (body.scenario is not None or body.compared) and user.role != ROLE_PMO:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the PMO calculates scenarios.")
    if body.scenario is not None:
        spec = _scenario_input(ctx, body.scenario)
    else:
        options = views.report_options(ctx.visible)
        spec = options.get(body.option or "Base plan (today's data)")
        if spec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found.")
    result = run_scenario(ctx.visible, spec)
    stamp = clock.today().isoformat()
    if body.format == "xlsx":
        compared = [run_scenario(ctx.visible, _scenario_input(ctx, s)) for s in body.compared]
        return Response(reports.board_pack_xlsx(ctx.visible, result, compared),
                        media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="board_pack_{stamp}.xlsx"'})
    if body.format == "html":
        return Response(reports.board_pack_html(ctx.visible, result), media_type="text/html",
                        headers={"Content-Disposition": f'attachment; filename="board_pack_{stamp}.html"'})
    return {**_verdict_json(result), "multi_year": records(reports.multi_year(result))}
