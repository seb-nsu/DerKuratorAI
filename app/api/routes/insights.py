"""Decision insights (PMO): status check of the sources, the portfolio with probabilities, the reference class of a
project, the review of past decisions, the audit pack of an approved decision and the write-back to ServiceNow. The
same functions as the Streamlit page (app/views/insights.py); deterministic, no LLM."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from app.api.deps import Scope, pmo, scope
from app.core.auth import User
from app.crew.audit_pack import AuditPackError
from app.data_adapters import DataSourceError
from app.views import insights

router = APIRouter(prefix="/insights", tags=["decision insights"])


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")  # the figures of app/views/insights.py, named in their docstrings


class StatusCheckRow(_Open):
    project_id: str
    project_name: str
    reported: str | None
    calculated: str
    mismatch: bool
    label: str
    reasons: list[str]
    stale: bool
    source: str | None = None


class StatusCheckPage(BaseModel):
    checks: list[StatusCheckRow]
    mismatches: int
    stale: int


class PortfolioForecast(BaseModel):
    summary: dict[str, Any]  # runs, seed, confidence_pct, budget_eur, p50/p_conf cost, probability, reserve, headline
    projects: list[dict[str, Any]]


class ReferenceForecast(_Open):
    subject: str
    basis: str
    size: int
    closed: int
    widened: bool
    confidence_pct: float
    cost_ratio: dict[str, float | None]
    schedule_ratio: dict[str, float | None]
    headline: str | None = None


class DecisionReview(BaseModel):
    as_of: str
    decisions: list[dict[str, Any]]
    hit_rate: list[dict[str, Any]]
    due_for_review: int
    conditions_overdue: int


class Transfer(_Open):
    proposal_id: str | None = None
    table: str | None = None
    key: str | None = None
    field: str | None = None
    status: str
    reason: str | None = None


def _pmo_scope(user: User = Depends(pmo), ctx: Scope = Depends(scope)) -> Scope:
    return ctx


@router.get("/status-check")
def status_check(ctx: Scope = Depends(_pmo_scope)) -> StatusCheckPage:
    """Reported against calculated status of every open project ("reported green, calculated red")."""
    return insights.status_check(ctx.visible)


@router.get("/forecast")
def forecast(ctx: Scope = Depends(_pmo_scope)) -> PortfolioForecast:
    """Monte Carlo over the open portfolio: probability to stay within budget and the reserve at the confidence
    level of the business rules (seeded, reproducible)."""
    return insights.portfolio_forecast(ctx.visible)


@router.get("/reference/{project_id}")
def reference(project_id: str, ctx: Scope = Depends(_pmo_scope)) -> ReferenceForecast:
    """Reference class forecast of one project: how comparable projects ended against their first budget."""
    try:
        return insights.reference(ctx.visible, project_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.") from None


@router.get("/decision-review")
def decision_review(ctx: Scope = Depends(_pmo_scope)) -> DecisionReview:
    """Approved decisions six and twelve months on: did the budget and the end date hold, hit rate per verdict."""
    return insights.decision_review(ctx.visible)


@router.get("/audit-pack/{proposal_id}", response_class=Response,
            responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}})
def audit_pack(proposal_id: str, ctx: Scope = Depends(_pmo_scope)) -> Response:
    """One ZIP with everything an internal audit needs about an approved decision (audited)."""
    try:
        name, data = insights.audit_pack(proposal_id, ctx.user.user_id, ctx.user.role, ctx.full)
    except AuditPackError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/servicenow-transfers")
def servicenow_transfers(user: User = Depends(pmo)) -> list[Transfer]:
    """Every write-back attempt with its check against the latest ServiceNow snapshot."""
    return insights.servicenow_transfers()


@router.post("/servicenow-transfer/{proposal_id}")
def servicenow_transfer(proposal_id: str, user: User = Depends(pmo)) -> list[Transfer]:
    """Writes the changes of an approved proposal back to ServiceNow (only when switched on in the mapping and on
    the host); a value changed in ServiceNow meanwhile is a conflict and is not overwritten."""
    try:
        return insights.servicenow_transfer(proposal_id, user.user_id, user.role)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No approved proposal with this id.") from None
    except DataSourceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
