"""What-if comparison: saved what-ifs (PMO: all, Project Manager: their own), the A/B comparison with its charts
(Plotly figure JSON) and deleting a saved what-if. A saved what-if shows only the projects of
the reader's current scope."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

from app.api.deps import Scope, records, requester, scope
from app.api.routes.steering import Change
from app.approval_flow import audit
from app.core.auth import User
from app.core.formatting import field_label
from app.core.scenario_store import ScenarioStoreError, delete_scenario, list_scenarios
from app.core.security import ROLE_PMO
from app.views import whatifs as views

router = APIRouter(tags=["what-ifs"])


class WhatIfFigures(BaseModel):
    verdict: str | None
    net_budget_impact: float
    budget_released: float
    budget_added: float
    projects_stopped: int
    projects_on_hold: int
    fte_moved: float
    changes: int
    steps: int


class WhatIfSummary(BaseModel):
    id: str
    name: str
    owner: str | None
    timestamp: str | None
    verdict: str | None
    restricted: bool  # changes of projects outside the reader's scope were left out
    can_delete: bool
    figures: WhatIfFigures


class Pair(BaseModel):
    a: str
    b: str


class WhatIfList(BaseModel):
    whatifs: list[WhatIfSummary]
    default_pair: Pair | None  # A = the older, B = the newer of the two latest


class WhatIfDetail(WhatIfSummary):
    turns: list[str]
    changes: list[Change]
    budget_impact: dict[str, float]
    conditions: list[str]
    waterfall_figure: dict[str, Any] | None  # Plotly figure JSON; None without a budget effect


class Difference(BaseModel):
    project: str
    field: str
    field_label: str
    A: str
    B: str
    differs: str  # "≠" where A and B end at different values


class ImpactBar(BaseModel):
    project: str
    scenario: str
    impact: float


class Comparison(BaseModel):
    a: WhatIfDetail
    b: WhatIfDetail
    differences: list[Difference]
    budget_impact: list[ImpactBar]
    budget_impact_figure: dict[str, Any] | None  # Plotly figure JSON; None if neither changes a budget


def _figure(fig) -> dict | None:
    return json.loads(fig.to_json()) if fig is not None else None


def _visible(ctx: Scope) -> list[dict]:
    allowed = set(ctx.visible.project_numbers)
    out = []
    for s in list_scenarios(ctx.user.role, ctx.user.user_id):
        scoped = views.in_scope(s, allowed)
        if scoped["restricted"]:  # conditions may name the projects left out
            scoped["verdict"] = {"label": (s.get("verdict") or {}).get("label")}
        out.append(scoped)
    return out


def _find(ctx: Scope, scenario_id: str) -> dict:
    found = next((s for s in _visible(ctx) if s["id"] == scenario_id), None)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved what-if not found.")
    return found


def _summary(s: dict, user: User) -> dict:
    return {"id": s["id"], "name": s["name"], "owner": s.get("owner"), "timestamp": s.get("timestamp"),
            "verdict": (s.get("verdict") or {}).get("label"), "restricted": s["restricted"],
            "can_delete": s.get("owner") == user.user_id or user.role == ROLE_PMO,
            "figures": views.scenario_figures(s)}


def _detail(s: dict, user: User, tag: str | None = None) -> dict:
    title = f"Budget impact per project – {tag}" if tag else "Budget impact per project"
    return {**_summary(s, user), "turns": s.get("turns") or [], "changes": s.get("changes") or [],
            "budget_impact": s.get("budget_impact") or {},
            "conditions": list((s.get("verdict") or {}).get("conditions") or [])[:4],
            "waterfall_figure": _figure(views.waterfall_figure(s, title))}


@router.get("/whatifs")
def saved_what_ifs(user: User = Depends(requester), ctx: Scope = Depends(scope)) -> WhatIfList:
    """Saved what-ifs, newest first. `default_pair`: A = the older, B = the newer of the two latest (as the page)."""
    saved = _visible(ctx)
    return {"whatifs": [_summary(s, user) for s in saved],
            "default_pair": {"a": saved[1]["id"], "b": saved[0]["id"]} if len(saved) > 1 else None}


@router.get("/whatifs/compare")
def compare(a: str = Query(..., max_length=32), b: str = Query(..., max_length=32), user: User = Depends(requester),
            ctx: Scope = Depends(scope)) -> Comparison:
    """A/B comparison: key figures side by side, the budget impact per project (grouped bars), the change-by-change
    differences and one waterfall per what-if."""
    if a == b:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose two different what-ifs.")
    sa, sb = _find(ctx, a), _find(ctx, b)
    diff = views.compare_frame(sa, sb)
    diff.insert(2, "field_label", diff["field"].map(field_label))
    bars = views.impact_bars(sa, sb)
    return {"a": _detail(sa, user, "A"), "b": _detail(sb, user, "B"), "differences": records(diff),
            "budget_impact": records(bars), "budget_impact_figure": _figure(views.impact_bars_figure(bars))}


@router.get("/whatifs/{scenario_id}")
def saved_what_if(scenario_id: str, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> WhatIfDetail:
    return _detail(_find(ctx, scenario_id), user)


@router.delete("/whatifs/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_what_if(scenario_id: str, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> Response:
    """Deletes a saved what-if: its owner or the PMO."""
    found = _find(ctx, scenario_id)
    if found.get("owner") != user.user_id and user.role != ROLE_PMO:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner or the PMO can delete a saved what-if.")
    try:
        delete_scenario(scenario_id, user.user_id, user.role)
    except ScenarioStoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    audit("scenario_deleted", role=user.role, user_id=user.user_id, scenario_id=scenario_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
