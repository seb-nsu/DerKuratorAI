"""Projects in the caller's scope (row-level security); a project outside it answers 404, never 403, so its
existence stays hidden. The detail answers what the project detail page shows; the master-data form saves the way the
page does: non-governed fields directly, a change set with a governed field as one proposal to the PMO."""
from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Scope, clear_data_cache, project_view, records, requester, scope
from app.core import clock
from app.core.auth import User
from app.core.business_rules import current_rules
from app.core.formatting import field_label, rule_label
from app.data_adapters.dataset import BASE_CURRENCY
from app.data_adapters.project_edit import EDITABLE_FIELDS, access_level, missing_required
from app.views import projects as views

router = APIRouter(tags=["projects"])


class MasterDataEdit(BaseModel):
    section: Literal["direct", "governed"]  # the page's two buttons: saved directly / submitted for approval
    values: dict[str, Any] = Field(description="Changed fields of the section only; omitted fields keep their value")


class BenefitActual(BaseModel):
    amount: float = Field(gt=0, description="Actual benefit in `currency`")
    plan: str = Field("", max_length=64, description="Benefit tranche (empty: not assigned)")
    currency: str = Field(BASE_CURRENCY, max_length=8)
    month: date | None = None  # stored as the first of the month; empty: this month
    comment: str = Field("", max_length=500)


Row = dict[str, Any]  # a project row: its columns come from the data source (mapping layer)


class ProjectList(BaseModel):
    projects: list[Row]
    total: int


class Finding(BaseModel):
    severity: str
    rule: str
    field: str | None = None
    message: str
    rule_label: str = Field(description="The rule as the page shows it")
    field_label: str | None = Field(None, description="The field as the page shows it")


class KeyFigures(BaseModel):
    budget: float | None = None
    actual: float | None = None
    eac: float | None = None
    progress: float
    planned_end: date | None = None
    actual_pct_of_budget: float | None = None
    eac_vs_budget: float | None = Field(None, description="EAC minus budget (positive: over budget)")
    days_to_end: int | None = Field(None, description="Negative: overdue")
    data_quality: str = Field(description="ok | warnings | blocked (blocked for simulation)")
    rag: str | None = None
    state: str | None = None
    execution_type: str | None = None
    phase: str | None = None


class _Shown(BaseModel):
    """A table as the page shows it (formatted cells); the keys are the page's column titles."""
    model_config = ConfigDict(populate_by_name=True)


class MasterDataRow(_Shown):
    field: str = Field(alias="Field")
    value: str = Field(alias="Value")


class MasterData(BaseModel):
    direct: list[MasterDataRow]
    governed: list[MasterDataRow] = Field(description="Changes need PMO approval")
    business_case: str | None = None


class CostPoint(_Shown):
    month: str | None = None
    planned: float | None = Field(None, alias="Planned (cumulative)")
    actual: float | None = Field(None, alias="Actual (cumulative)")


class BaselineRow(_Shown):
    version: int = Field(alias="Version")
    created: str | None = Field(None, alias="Created")
    budget: str = Field(alias="Budget")
    end: str | None = Field(None, alias="End")
    budget_delta: str = Field(alias="Δ budget vs. v1")
    reason: str | None = Field(None, alias="Reason")
    approved_by: str | None = Field(None, alias="Approved by")


class Costs(BaseModel):
    curve: list[CostPoint]
    caption: str | None = None
    figure: dict[str, Any] | None = Field(None, description="The page's Plotly chart (figure JSON)")
    baselines: list[BaselineRow]
    planned_vs_approved_end_days: int | None = None


class TrancheRow(_Shown):
    tranche: str = Field(alias="Tranche")
    type: str | None = Field(None, alias="Type")
    due: str = Field(alias="Due")
    planned: str = Field(alias="Planned")
    actual: str = Field(alias="Actual")
    status: str = Field(alias="Status")


class Tranche(BaseModel):
    model_config = ConfigDict(extra="allow")
    plan: str | None = None
    type: str | None = None
    due: date
    planned: float
    actual: float
    is_due: bool
    days_due: int


class Realisation(BaseModel):
    tranches: list[Tranche]
    planned_due: float
    actual: float
    unassigned: float
    pct: float | None = None
    early: float
    planned_total: float


class Benefits(BaseModel):
    tranches: list[TrancheRow]
    realisation: Realisation
    colour: str | None = Field(None, description="red (below the NO-GO threshold) | orange (below the minimum) | green")
    min_pct: float
    nogo_pct: float | None = None
    currency: str
    fx_rate: float | None = None
    can_report: bool
    plan_options: list[str] = Field(description="Tranches to report against ('' = not assigned)")
    currencies: list[str]
    default_month: date | None = Field(None, description="The month an actual is booked to when none is given: the "
                                                          "first of the current month")


class DeadlineRow(BaseModel):
    date: str
    days: int
    type: str
    item: str
    status: str | None = None


class DependencyRow(BaseModel):
    project: str = Field(description="'<id> – <name>' or the neutral row for projects outside the scope")
    type: str | None = None
    status: str | None = None
    required_by: str | None = None
    description: str | None = None


class Dependencies(BaseModel):
    incoming: list[DependencyRow]
    outgoing: list[DependencyRow]


class ProjectDetail(BaseModel):
    project: Row
    access: str = Field(description="edit (owning Project Manager) | read | none")
    tabs: list[str]
    header: KeyFigures
    missing_required: list[str]
    master_data: MasterData
    status_reports: list[Row]
    costs: Costs
    benefits: Benefits
    deadlines: list[DeadlineRow]
    dependencies: Dependencies
    risks: list[Row] = Field(description="Risk columns of the source plus exposure_eur, highest exposure first")
    total_risk_exposure_eur: float
    data_quality_findings: list[Finding]


class FormField(BaseModel):
    field: str
    label: str
    kind: str = Field(description="text | textarea | number | percent | int | date | select")
    options: list[str] | None = None
    min: int | float | str | None = None
    max: int | float | str | None = None
    step: int | float | None = None
    value: int | float | str | None = Field(None, description="The value the input shows (clamped into its range)")
    stored_out_of_range: int | float | str | None = Field(None, description="The stored value when it lies outside "
                                                                            "the range – flag it, never write the "
                                                                            "shown value back unchanged")
    missing: bool = Field(description="Required and empty")
    editable: bool
    fill_only_while_empty: bool = Field(description="Approved budget and CapEx/OpEx plan")


class EditForm(BaseModel):
    access: str
    sections: dict[str, list[FormField]] = Field(description="direct (saved at once) and governed (a proposal)")
    missing_required: list[str]


class SaveResult(BaseModel):
    kind: str = Field(description="success | info (nothing changed)")
    message: str
    proposal_id: str | None = Field(None, description="Governed changes: the proposal in the PMO inbox")
    applied: int | None = None
    snapshot_id: str | None = None


class BenefitResult(BaseModel):
    kind: str
    message: str
    benefit_actual_number: str
    snapshot_id: str | None = None


@router.get("/projects")
def list_projects(ctx: Scope = Depends(scope)) -> ProjectList:
    if ctx.visible.projects.empty:
        return ProjectList(projects=[], total=0)
    view, _ = project_view(ctx.visible)
    return ProjectList(projects=records(view), total=len(view))


def _project(project_id: str, ctx: Scope):
    """(project row, view, steward report) of a project in the caller's scope; 404 outside it."""
    if project_id not in set(ctx.visible.project_numbers):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    view, steward = project_view(ctx.visible)
    row = view[view["project_number"].astype(str) == project_id]
    return row, steward


def _clean(value: Any) -> Any:
    """JSON-safe: dates as ISO text, missing numbers (NaN) as null."""
    value = jsonable_encoder(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return None if isinstance(value, float) and math.isnan(value) else value


def _figure(fig) -> dict | None:
    return json.loads(fig.to_json()) if fig is not None else None


@router.get("/projects/{project_id}")
def project_detail(project_id: str, ctx: Scope = Depends(scope)) -> ProjectDetail:
    row, steward = _project(project_id, ctx)
    project = row.iloc[0].to_dict()
    user, ds = ctx.user, ctx.visible
    access = access_level(user.role, user.user_id, project)
    findings = [{"severity": getattr(i.severity, "value", i.severity), "rule": i.rule, "field": i.column,
                 "message": i.message, "rule_label": rule_label(i.rule),
                 "field_label": field_label(i.column) if i.column else None}
                for i in steward.issues if str(i.project_id) == project_id]
    figures = views.key_figures(project, steward.project_status().get(project_id))
    free, governed = views.master_data_frames(project)
    curve = views.cockpit_cost_curve(project_id, ds)
    risks = views.project_risks(project_id, ds)
    frame, realisation = views.benefit_frame(project_id, project, ds)
    rules = current_rules()
    incoming, outgoing = views.cockpit_dependencies(project_id, ds)
    approved_end, end = views.to_day(project.get("approved_end_date")), figures["planned_end"]
    currency = str(_clean(project.get("currency")) or BASE_CURRENCY)
    return ProjectDetail(**{
        "project": records(row)[0],
        "access": access,
        "tabs": ["Master data", "Status history", "Costs & baselines", "Benefits", "Schedule & dependencies", "Risks",
                 "Data quality", *(["Edit"] if access == "edit" else [])],
        "header": _clean(figures),
        "missing_required": [EDITABLE_FIELDS[f].label if f in EDITABLE_FIELDS else f
                             for f in missing_required(project)],
        "master_data": {"direct": records(free), "governed": records(governed),
                        "business_case": str(_clean(project.get("business_case")) or "").strip() or None},
        "status_reports": records(views.status_history(project_id, ds)),
        "costs": {"curve": records(curve), "caption": views.cost_curve_caption(curve) if not curve.empty else None,
                  "figure": _figure(views.cost_curve_figure(curve, figures["budget"], figures["eac"]))
                  if not curve.empty else None,
                  "baselines": records(views.baseline_frame(project_id, project, ds)),
                  "planned_vs_approved_end_days": (end - approved_end).days if approved_end and end else None},
        "benefits": {"tranches": records(frame), "realisation": _clean(realisation),
                     "colour": views.realisation_colour(realisation["pct"], rules)
                     if realisation["pct"] is not None else None,
                     "min_pct": rules.benefit_realization_min_pct, "nogo_pct": rules.benefit_realization_nogo_pct,
                     "currency": currency, "fx_rate": views.to_number(project.get("fx_rate")) if currency != BASE_CURRENCY else None,
                     "can_report": views.can_report_benefit(user.role, user.user_id, project),
                     "plan_options": views.benefit_plan_options(project_id, ds),
                     "currencies": views.benefit_currencies(ds),
                     "default_month": clock.today().replace(day=1)},
        "deadlines": records(views.cockpit_deadlines(project_id, project, ds)),
        "dependencies": {"incoming": records(incoming), "outgoing": records(outgoing)},
        "risks": records(risks),
        "total_risk_exposure_eur": float(risks["exposure_eur"].sum()) if not risks.empty else 0.0,
        "data_quality_findings": findings,
    })


@router.get("/projects/{project_id}/edit-form")
def edit_form(project_id: str, ctx: Scope = Depends(scope)) -> EditForm:
    """The master-data form: its two sections with every field's input (kind, options, range, value shown, a stored
    value outside the range), required/missing, and whether the caller may change it (only the owning Project
    Manager edits; approved budget and CapEx/OpEx plan only while empty)."""
    row, _ = _project(project_id, ctx)
    return EditForm(**views.edit_form(row.iloc[0].to_dict(), ctx.user.role, ctx.user.user_id))


@router.post("/projects/{project_id}/master-data")
def save_master_data(project_id: str, body: MasterDataEdit, user: User = Depends(requester),
                     ctx: Scope = Depends(scope)) -> SaveResult:
    """Saves one section of the form: 'direct' is written at once (snapshot, audit), 'governed' is submitted to the
    PMO inbox as one proposal and written after a second person approves it."""
    row, _ = _project(project_id, ctx)
    project = row.iloc[0].to_dict()
    if access_level(user.role, user.user_id, project) != "edit":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only: only the owning Project Manager can edit this "
                                                       "project.")
    edits, errors = views.section_edits(project, body.section, body.values)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            {"message": "Changes not saved:\n\n" + "\n".join(f"- {e}" for e in errors),
                             "errors": errors})
    outcome = views.save_master_data(project_id, project, edits, user.role, user.user_id, ctx.full)
    if outcome.written:
        clear_data_cache()
    if outcome.kind == "error":
        code = {"validation": status.HTTP_422_UNPROCESSABLE_CONTENT, "permission": status.HTTP_404_NOT_FOUND,
                "system": status.HTTP_500_INTERNAL_SERVER_ERROR}.get(outcome.reason, status.HTTP_409_CONFLICT)
        raise HTTPException(code, {"message": outcome.message, "errors": outcome.errors} if outcome.errors
                            else outcome.message)
    return SaveResult(kind=outcome.kind, message=outcome.message, proposal_id=outcome.proposal_id,
                      applied=outcome.applied, snapshot_id=outcome.snapshot_id)


@router.post("/projects/{project_id}/benefit-actuals", status_code=status.HTTP_201_CREATED)
def report_benefit(project_id: str, body: BenefitActual, ctx: Scope = Depends(scope)) -> BenefitResult:
    """Reports an actual benefit (an appended Benefit_Actuals row, no proposal – like a status report). The owning
    Project Manager and the PMO report."""
    row, _ = _project(project_id, ctx)
    project, user = row.iloc[0].to_dict(), ctx.user
    if not views.can_report_benefit(user.role, user.user_id, project):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owning Project Manager and the PMO report benefits.")
    if body.plan not in views.benefit_plan_options(project_id, ctx.visible):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown benefit tranche for this project.")
    if body.currency.upper() not in views.benefit_currencies(ctx.visible):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown currency '{body.currency}'.")
    change = views.benefit_actual_change(project_id, ctx.visible, user.user_id, body.plan, body.month, body.amount,
                                         body.currency.upper(), body.comment)
    kind, message, result = views.report_benefit(change, user.role, user.user_id)
    if result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, message)
    clear_data_cache()
    return BenefitResult(kind=kind, message=message, benefit_actual_number=change["row"]["benefit_actual_number"],
                         snapshot_id=result.snapshot_id)
