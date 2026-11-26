"""Red trigger (recovery recommendations): the list and detail of the recommendations, the alert band of the overview,
the recommendations of one project, 'Analyse now' / 'Recalculate', 'Adopt' and 'Dismiss', and the scan log for the
System Admin. Logic in app/views/recovery.py and app/recovery/. A recommendation is never a proposal: adopting it runs
the option as a what-if in the caller's name; the caller submits it from the Steering Console, a second PMO approves."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app import services
from app.api import limits, runs
from app.api.deps import Scope, admin, requester, scope
from app.approval_flow import audit
from app.core import clock, cost_tracker
from app.core.auth import User
from app.core.chat_store import append_messages
from app.core.security import ROLE_PMO, approval_mode, log_security_event
from app.crew.scenarios import build_guided_scenario
from app.data_adapters.base import net_changes
from app.recovery import hooks, store, watch
from app.recovery.engine import RecoveryError
from app.views import console as console_views
from app.views import recovery as views

router = APIRouter(prefix="/recovery", tags=["recovery"])
logger = logging.getLogger("derkurator.api.recovery")


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")  # the figures of app/recovery, named in their docstrings


class RecommendationSummary(BaseModel):
    id: str
    project_id: str
    project_name: str | None = None
    status: str
    trigger: str | None = None
    trigger_label: str | None = None
    reported: str | None = None
    calculated: str | None = None
    as_of: str | None = None
    created_at: str | None = None
    causes: list[str]
    gap_eur: float
    gap_days: int
    options: int
    recommended: str | None = None
    headline: str


class RecommendationList(BaseModel):
    enabled: bool = Field(description="The business rule recovery_enabled")
    recommendations: list[RecommendationSummary]


class Recommendation(_Open):
    id: str
    project_id: str
    project_name: str | None = None
    status: str
    causes: list[dict[str, Any]]
    impact: dict[str, Any]
    options: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    not_possible: list[dict[str, Any]]
    explanation: dict[str, Any]
    recommended: str | None = None
    proposals: list[str] = []


class AnalyseRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)


class AdoptRequest(BaseModel):
    option: str = Field(min_length=1, max_length=2)


class AdoptResult(BaseModel):
    recommendation_id: str
    option: str
    run_id: str
    request: str
    verdict: dict[str, Any] | None
    change_set: list[dict[str, Any]]
    approval_mode: str | None
    steps: int
    chat_message_id: str | None


class DismissRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ScanResult(_Open):
    enabled: bool


class ScanLog(BaseModel):
    scans: list[dict[str, Any]]
    chain: dict[str, Any]


def _enabled() -> bool:
    return hooks.enabled()


@router.get("/recommendations")
def recommendations(status_filter: str | None = Query(None, alias="status", pattern="^(open|adopted|dismissed|"
                                                                                    "superseded|expired)$"),
                    user: User = Depends(requester), ctx: Scope = Depends(scope)) -> RecommendationList:
    """PMO: every recommendation; a Project Manager: those of their own projects (other projects anonymised)."""
    return RecommendationList(enabled=_enabled(), recommendations=views.list_recommendations(
        ctx.full, user.role, user.user_id, status_filter))


@router.get("/alerts")
def alerts(user: User = Depends(requester), ctx: Scope = Depends(scope)) -> RecommendationList:
    """The alert band of the overview. Opening it also starts the (idempotent) scan in the background."""
    hooks.scan_in_background("overview")
    return RecommendationList(enabled=_enabled(), recommendations=views.alerts(ctx.full, user.role, user.user_id))


@router.get("/recommendations/{rec_id}")
def recommendation(rec_id: str, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> Recommendation:
    try:
        return Recommendation(**views.get(rec_id, ctx.full, user.role, user.user_id))
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found.") from None


@router.get("/projects/{project_id}")
def project_recommendations(project_id: str, user: User = Depends(requester),
                            ctx: Scope = Depends(scope)) -> RecommendationList:
    """The recommendations of one project (project detail, tab Recommendations), newest first."""
    try:
        items = views.for_project(project_id, ctx.full, user.role, user.user_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.") from None
    return RecommendationList(enabled=_enabled(), recommendations=items)


@router.post("/analyse", status_code=status.HTTP_201_CREATED)
def analyse(body: AnalyseRequest, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> Recommendation:
    """'Analyse now' / 'Recalculate' (PMO): works for a project that is red already, also with the trigger off."""
    try:
        with limits.heavy(user.user_id):
            rec = views.analyse(body.project_id, user.user_id, user.role, full=ctx.full)
    except views.RecoveryAccessError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except RecoveryError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except store.RecoveryStoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return Recommendation(**views.public(rec, None, ctx.full))


@router.post("/recommendations/{rec_id}/adopt")
def adopt(rec_id: str, body: AdoptRequest, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> AdoptResult:
    """Runs the option as a what-if in the caller's name (the Steering Console shows it, ready to submit). The PMO
    runs it with the quality gate overridden for the red project when the Data Steward holds it (audited)."""
    rec = store.get_recommendation(rec_id)
    allowed = views.visible_ids(ctx.full, user.role, user.user_id)
    if rec is None or (allowed is not None and str(rec.get("project_id")) not in allowed):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found.")
    try:
        option = views.check_adopt(rec, body.option.upper(), user.role, user.user_id, ctx.full)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Option not found.") from None
    except views.RecoveryAccessError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except store.RecoveryStoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    projects = ctx.visible.projects
    names = dict(zip(projects["project_number"].astype(str), projects["project_name"].astype(str), strict=True))
    forced = set(rec.get("forced") or []) if user.role == ROLE_PMO else set()
    if forced:
        audit("forced_simulation", role=user.role, user_id=user.user_id, request=f"recovery {rec_id} {option['key']}")
        log_security_event("Forced simulation requested (recovery option)", level=logging.WARNING, role=user.role,
                           user=user.user_id)
    state, result, request, messages, stamp = None, None, "", [], clock.now().strftime("%Y-%m-%d %H:%M")
    with limits.heavy(user.user_id), cost_tracker.track_request():
        for step in option["steps"]:
            try:
                guided = build_guided_scenario(step["action"], step["project"], names, target=step.get("target"),
                                               **(step.get("params") or {}))
            except ValueError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
            result = services.run_steering_pipeline(guided.request, ctx.visible, user.role, user.user_id,
                                                    pii_source=ctx.full, plan=guided.plan, force=bool(forced),
                                                    force_projects=forced or None, base_state=state, use_llm=False)
            run_id = uuid.uuid4().hex
            messages += [console_views.user_message(f"**Recovery option {option['key']}:** {guided.request}", state,
                                                    bool(forced), stamp),
                         console_views.assistant_message(guided.request, result, run_id, bool(forced), state)]
            state, request = result.scenario_state, guided.request
    append_messages(user.role, user.user_id, messages)
    verdict = result.verdict.to_dict() if result.verdict else None
    changes = net_changes(result.proposed_changes)
    runs.put(run_id, {"user_id": user.user_id, "role": user.role, "request": request, "paper": result.decision_paper,
                      "verdict": verdict, "changes": changes, "proposed_changes": result.proposed_changes,
                      "forced": bool(forced), "scenario_state": result.scenario_state,
                      "chat_message_id": messages[-1].get("id"), "mode": result.mode, "warnings": result.warnings,
                      "plan": result.intent_plan.model_dump() if result.intent_plan else None,
                      "recommendation_id": rec_id})
    runs.set_latest(user.user_id, run_id)
    try:
        store.adopt(rec_id, user.user_id, user.role, option["key"], run_id)
    except store.RecoveryStoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    audit("recovery_adopted", role=user.role, user_id=user.user_id, recommendation_id=rec_id, option=option["key"],
          run_id=run_id)
    return AdoptResult(recommendation_id=rec_id, option=option["key"], run_id=run_id, request=request,
                       verdict=verdict, change_set=changes,
                       approval_mode=approval_mode(user.role, changes) if changes else None,
                       steps=len(option["steps"]), chat_message_id=messages[-1].get("id"))


@router.post("/recommendations/{rec_id}/dismiss")
def dismiss(rec_id: str, body: DismissRequest, user: User = Depends(requester)) -> RecommendationSummary:
    """Dismiss with a mandatory reason (PMO) – the reasons per lever feed the learning loop."""
    try:
        rec = views.dismiss(rec_id, body.reason, user.user_id, user.role)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found.") from None
    except views.RecoveryAccessError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except store.RecoveryStoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return RecommendationSummary(**views.summary(rec))


@router.post("/scan")
def scan(user: User = Depends(requester), ctx: Scope = Depends(scope)) -> ScanResult:
    """Runs the watch now (PMO). Does nothing while the red trigger is off."""
    if user.role != ROLE_PMO:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Reserved for the PMO.")
    from app.recovery.engine import run_scan

    with limits.heavy(user.user_id):
        return ScanResult(**run_scan(dataset=ctx.full, source=f"pmo:{user.user_id}"))


@router.get("/scans")
def scans(user: User = Depends(admin)) -> ScanLog:
    """Admin Console › Logs › Recovery scans: the last scans and the hash chain of the recommendation store."""
    return ScanLog(scans=watch.scan_log(), chain=store.verify())
