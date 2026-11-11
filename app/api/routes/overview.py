"""Overview page: scope line, KPI ribbon and portfolio charts, the top-risk alert bar, a Project Manager's waiting
proposals and the PMO's management reserve. One route per page section, so a failing section never takes the others
down. Everything is computed from the caller's scope; a Project Manager
never learns the size of the whole portfolio."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import Scope, project_view, records, requester, scope
from app.api.routes.proposals import _summary as proposal_summary
from app.core.auth import User
from app.core.security import ROLE_PM
from app.views import overview
from app.views.charts import bubble_chart, figure_json, skill_heatmap, team_bar_chart

router = APIRouter(tags=["overview"])
NO_PROJECTS = "No projects are released for your role."
Figure = dict[str, Any]  # Plotly figure JSON, drawn as is by every front end


class OverviewKpis(BaseModel):
    projects: int
    active_projects: int
    show_active: bool  # False: the card would only repeat the scope ("10 of 10 active") and is left out
    total_budget_eur: float | None
    total_eac_eur: float | None
    critical_projects: int
    critical_share_pct: float
    max_team_utilisation_pct: float | None
    top_team: str | None
    capacity_limit_pct: float
    data_quality_index: float | None = None
    blocked_projects: int | None = None


class RiskValuePoint(BaseModel):
    project_number: str
    project_name: str | None = None
    status: str | None = None
    execution_type: str | None = None
    risk_score: float
    planned_roi_pct: float
    budget_cost_eur: float


class RiskValue(BaseModel):
    points: list[RiskValuePoint]
    figure: Figure | None


class OverloadedTeam(BaseModel):
    team: str
    peak: float
    months: int
    projects: int


class OverloadedTeams(BaseModel):
    teams: list[OverloadedTeam]
    figure: Figure | None


class OverviewPage(BaseModel):
    scope_line: str
    projects: int
    own_view: bool  # a Project Manager: "Your open items" instead of the PMO's management reserve
    message: str | None = None  # set when no project is released for the caller
    kpis: OverviewKpis | None = None
    risk_value: RiskValue | None = None
    overloaded_teams: OverloadedTeams | None = None


class TopRisk(BaseModel):
    kind: Literal["blocked", "at_risk", "cluster"]
    title: str
    project: str | None
    name: str | None
    detail: str
    metric_label: str
    metric_value: str
    severity: Literal["critical", "warning"]
    can_open: bool  # the project is in the caller's scope (a cluster has none)


class TopRisks(BaseModel):
    available: bool
    message: str | None = None
    risks: list[TopRisk]


class WaitingProposal(BaseModel):
    id: str
    request: str | None
    timestamp: str | None
    status: str
    deadline: str  # on time | overdue | overdue x2 | expires | closed | unknown
    due: str | None
    deadline_label: str  # the Deadline column as the Approvals tables show it
    due_date: str | None  # the Due column (date only, while the proposal is open)
    submitted_label: str  # the Submitted column: "2026-10-02 18:40"


class OpenItems(BaseModel):
    proposals: list[WaitingProposal]


class SkillUtilisation(BaseModel):
    skill: str
    month: str
    capacity_hours: float | None
    demand_hours: float | None
    utilisation_pct: float | None


class ManagementReserve(BaseModel):
    balance_eur: float
    floor_eur: float
    below_floor: bool
    overload_alert_pct: float
    skills: list[SkillUtilisation]
    figure: Figure | None


def _overview_scope(user: User = Depends(requester), ctx: Scope = Depends(scope)) -> Scope:
    """PMO and Project Manager (the Overview is not part of the Portfolio Board's workspace)."""
    return ctx


@router.get("/overview")
def overview_page(ctx: Scope = Depends(_overview_scope)) -> OverviewPage:
    visible = ctx.visible
    head = {"scope_line": overview.scope_line(ctx.user.role, len(visible.projects), len(ctx.full.projects)),
            "projects": len(visible.projects), "own_view": ctx.user.role == ROLE_PM}
    if visible.projects.empty:
        return OverviewPage(**head, message=NO_PROJECTS)
    view, steward = project_view(visible)
    teams = overview.team_overload(visible)
    return OverviewPage(
        **head, kpis=OverviewKpis(**overview.kpis(view, visible, teams, steward)),
        risk_value=RiskValue(points=records(overview.risk_value_points(view)), figure=figure_json(bubble_chart(view))),
        overloaded_teams=OverloadedTeams(teams=records(teams),
                                         figure=figure_json(team_bar_chart(teams)) if not teams.empty else None))


@router.get("/overview/top-risks")
def top_risks(ctx: Scope = Depends(_overview_scope)) -> TopRisks:
    """Blocked projects and systemic risk clusters in the caller's scope. A predecessor outside the scope appears only
    as "projects outside your scope" – no id, no name, no count."""
    allowed = set(ctx.visible.project_numbers)
    risks, error = overview.top_risks(allowed)
    if error:  # the detail stays in the system log
        return TopRisks(available=False, message=overview.TOP_RISKS_UNAVAILABLE, risks=[])
    return TopRisks(available=True, risks=[TopRisk(**r, can_open=r.get("project") in allowed) for r in risks])


@router.get("/overview/open-items")
def open_items(ctx: Scope = Depends(_overview_scope)) -> OpenItems:
    """The caller's own proposals still waiting for the PMO, with their deadline state."""
    return OpenItems(proposals=[WaitingProposal(**proposal_summary(p))
                                for p in overview.waiting_proposals(ctx.user.user_id)])


@router.get("/overview/reserve")
def management_reserve(ctx: Scope = Depends(_overview_scope)) -> ManagementReserve:
    """Management reserve against its floor and the skill heatmap – the PMO's lever, not a Project Manager's."""
    if ctx.user.role == ROLE_PM:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The management reserve is shown to the PMO only.")
    reserve = overview.management_reserve(ctx.visible)
    skills = reserve.pop("skills")
    return ManagementReserve(**reserve, skills=records(skills), figure=figure_json(skill_heatmap(skills)))
