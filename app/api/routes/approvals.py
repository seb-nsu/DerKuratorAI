"""Approval governance of the Approvals page: the page overview (views with their counts, the deputy banner, deadline
events) and the delegation of the approval right to a deputy (PMO only)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import approval_flow
from app.api.deps import approver, data_user
from app.core import clock
from app.core.auth import User
from app.core.business_rules import current_rules
from app.core.proposals import list_delegations
from app.core.security import can_approve
from app.views import approvals as views

router = APIRouter(tags=["approvals"])


class DelegationRequest(BaseModel):
    deputy: str = Field(min_length=1, max_length=200)
    start: date
    end: date | None = None  # empty: a one-day delegation
    reason: str = Field(min_length=1, max_length=300)


class ApprovalRules(BaseModel):
    approval_sla_days: int = Field(description="An open proposal is escalated after this (level 2 after twice)")
    approval_expiry_days: int = Field(description="An open proposal expires after this (0 = never)")
    delegation_max_days: int


class DeputyOf(BaseModel):
    delegator: str
    start: str
    end: str


class DeadlineEvents(BaseModel):
    escalated: int
    expired: int


class Overview(BaseModel):
    approver: bool
    views: list[str] = Field(description="pending, decided, mine, delegations – who is no approver has only mine")
    counts: dict[str, int]
    deputy_of: DeputyOf | None = Field(None, description="The delegation the caller deputises under today")
    deadline_events: DeadlineEvents = Field(description="Escalations and expiries recorded by this view")
    rules: ApprovalRules


class Delegation(BaseModel):
    delegation_id: str
    delegator: str | None = None
    deputy: str | None = None
    start: str | None = None
    end: str | None = None
    reason: str | None = None
    status: str = Field(description="active | revoked (an active one may lie in the past)")
    status_label: str = Field(description="Active | Planned | Ended | Revoked")
    revocable: bool


class DelegationList(BaseModel):
    delegations: list[Delegation]
    deputy_candidates: list[str]
    today: str
    rules: ApprovalRules


class ActionResult(BaseModel):
    kind: str
    message: str


class DelegationResult(ActionResult):
    delegation: Delegation


def _rules() -> ApprovalRules:
    rules = current_rules()
    return ApprovalRules(approval_sla_days=rules.approval_sla_days, approval_expiry_days=rules.approval_expiry_days,
                         delegation_max_days=rules.delegation_max_days)


@router.get("/approvals/overview")
def overview(user: User = Depends(data_user)) -> Overview:
    """What the Approvals page shows above its tables: the views with their counts (who is no approver has only
    'mine'), the active delegation the caller deputises under, and the escalations and expiries this view recorded."""
    events = approval_flow.run_deadlines()
    lists = views.queues(user.user_id, user.role)
    approver_role = can_approve(user.role)
    delegation = views.deputy_info(user.user_id, user.role)
    return Overview(approver=approver_role, views=list(lists) + (["delegations"] if approver_role else []),
                    counts={k: len(v) for k, v in lists.items()},
                    deputy_of=DeputyOf(delegator=delegation["delegator"], start=delegation["start"],
                                       end=delegation["end"]) if delegation else None,
                    deadline_events=DeadlineEvents(escalated=sum(1 for e in events if e["type"] == "escalated"),
                                                   expired=sum(1 for e in events if e["type"] == "expired")),
                    rules=_rules())


def _delegation(d: dict, today: date) -> Delegation:
    return Delegation(delegation_id=d.get("delegation_id"), delegator=d.get("delegator"), deputy=d.get("delegate"),
                      start=d.get("start"), end=d.get("end"), reason=d.get("reason") or None, status=d.get("status"),
                      status_label=views.delegation_status(d, today),
                      revocable=bool(views.open_delegations([d], today)))


@router.get("/delegations")
def delegations(user: User = Depends(approver)) -> DelegationList:
    """Every delegation (newest first) with its state, which can still be ended early, and the possible deputies
    (the other PMO accounts; empty when the deputy has to be typed in)."""
    today = clock.today()
    return DelegationList(delegations=[_delegation(d, today) for d in list_delegations()],
                          deputy_candidates=views.deputy_candidates(user.user_id), today=today.isoformat(),
                          rules=_rules())


@router.post("/delegations", status_code=status.HTTP_201_CREATED)
def grant(body: DelegationRequest, user: User = Depends(approver)) -> DelegationResult:
    result = views.grant(user.user_id, user.role, body.deputy, body.start, body.end, body.reason)
    if not result.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, result.message)
    return DelegationResult(kind=result.kind, message=result.message,
                            delegation=_delegation(result.entry, clock.today()))


@router.post("/delegations/{delegation_id}/revoke")
def revoke(delegation_id: str, user: User = Depends(approver)) -> ActionResult:
    result = views.revoke(delegation_id, user.user_id, user.role)
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.message)
    return ActionResult(kind=result.kind, message=result.message)
