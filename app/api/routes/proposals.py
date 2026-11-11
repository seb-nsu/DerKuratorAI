"""Change proposals: submit a what-if run, follow its status, the PMO decision (four-eyes, shared flow in
app/approval_flow.py), withdrawing an own proposal and reverting an approval. An open annual plan (kind `plan`) belongs to
the Portfolio Board and is decided elsewhere; the PMO sees it here only once it is closed (the Decided view), its
requester always."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import approval_flow
from app.api import runs
from app.api.deps import Scope, approver, clear_data_cache, data_user, requester, scope
from app.api.routes import steering
from app.approval_flow import audit
from app.core.auth import User
from app.core.chat_store import update_message
from app.core.proposals import (
    OPEN,
    PLAN,
    SUBMITTED,
    ProposalError,
    get_proposal,
    list_proposals,
    requires_override,
    sla_state,
    submit_proposal,
)
from app.core.security import apply_rls_to_dataset, approval_mode, can_approve, log_security_event
from app.data_adapters import get_data_provider
from app.views import approvals as views
from app.views.exports import change_rows
from app.views.projects import project_names

router = APIRouter(tags=["proposals"])

SUMMARY_FIELDS = ("id", "timestamp", "requester", "requester_role", "status", "verdict", "request", "projects",
                  "changes", "forced", "justification", "comment", "decided_by", "decided_at", "escalation_level",
                  "snapshot_id", "reverts", "override", "on_behalf_of", "kind")


class Change(BaseModel):
    """One field change of a change set (an appended row carries `op` and `row`)."""
    model_config = ConfigDict(extra="allow")
    project_id: str | None = None
    table: str | None = None
    field: str | None = None
    old: Any = None
    new: Any = None
    key: Any = None
    op: str | None = None
    row: dict[str, Any] | None = None
    source_tool: str | None = None
    note: str | None = None


class ProposalSummary(BaseModel):
    id: str
    timestamp: str | None = None
    requester: str | None = None
    requester_role: str | None = None
    status: str
    verdict: str | None = None
    request: str | None = None
    projects: list[str] | None = None
    changes: list[Change] | None = None
    forced: bool | None = None
    justification: str | None = None
    comment: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    escalation_level: int | None = None
    snapshot_id: str | None = None
    reverts: str | None = None
    override: bool | None = None
    on_behalf_of: str | None = None
    kind: str | None = None
    deadline: str = Field(description="on time | overdue | overdue x2 | expires | closed | unknown")
    due: str | None = None
    expires: str | None = None
    days_open: int | None = None
    deadline_label: str = Field(description="The Deadline column of the Approvals tables")
    due_date: str | None = Field(None, description="The Due column: only while the proposal waits for approval")
    status_label: str
    decided_by_label: str | None = Field(None, description="Who decided; a deputy with the delegator")
    budget_delta_eur: float
    verdict_label: str = Field(description="The Verdict cell: shape symbol + verdict, \"No verdict\" without one")
    budget_delta_label: str = Field(description="The Budget Δ cell: signed whole euros, \"–\" without a change")
    submitted_label: str = Field(description="The Submitted cell: \"2026-10-02 18:40\"")


class ProposalList(BaseModel):
    proposals: list[ProposalSummary]
    project_names: dict[str, str] = Field(description="Names of the listed projects in the caller's scope")


class DecisionHints(BaseModel):
    own: bool = Field(description="The caller submitted it – four-eyes: they never decide it")
    approver: bool
    blocked: bool
    proxy_own: bool = Field(description="A proposal of the delegator the caller deputises for: decided in the "
                                        "caller's own name")
    delegator: str | None = None
    acting_for: str | None = Field(None, description="The delegator a decision of the caller is recorded for")
    needs_override: bool = Field(description="NO-GO / incomplete: approving needs override plus a reason")
    manual: bool = Field(description="Only hand-over changes: the approval records a hand-over")


class ExistingRevert(BaseModel):
    id: str
    status: str


class RevertState(BaseModel):
    possible: bool
    existing: ExistingRevert | None = None
    message: str | None = None


class ProposalDetail(ProposalSummary):
    paper: str | None = None
    verdict_record: dict[str, Any] | None = None
    project_names: dict[str, str]
    change_rows: list[dict[str, str]] = Field(description="The change set as the page's change table (Project, Change, "
                                                          "Current, Proposed, Δ, Approval, Note); project names only "
                                                          "within the caller's scope")
    decision: DecisionHints
    can_decide: bool
    can_withdraw: bool
    revert: RevertState | None = Field(None, description="PMO only")


class DecisionOutcome(BaseModel):
    kind: str = Field(description="success | warning | error")
    message: str
    snapshot_id: str | None = None


class ActionResult(BaseModel):
    kind: str
    message: str


class RevertResult(ActionResult):
    proposal: ProposalSummary


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _summary(proposal: dict) -> dict:
    """A proposal as the Approvals tables show it: deadline state and columns, status words, budget change and who
    decided (a deputy with the delegator they acted for)."""
    sla = sla_state(proposal)
    columns = views.sla_columns(proposal)
    return {**{k: proposal.get(k) for k in SUMMARY_FIELDS},
            "deadline": sla["state"], "due": _iso(sla.get("due")), "expires": _iso(sla.get("expires")),
            "days_open": sla.get("days_open"), "deadline_label": columns["Deadline"],
            "due_date": None if columns["Due"] == "–" else columns["Due"],
            "status_label": views.status_label(proposal, with_override=True),
            "decided_by_label": views.decided_by_text(proposal) if proposal.get("decided_by") else None,
            "budget_delta_eur": views.budget_delta(proposal), **views.shown_cells(proposal)}


def _visible(proposal: dict | None, user: User) -> bool:
    """The requester always sees their own proposal. The PMO sees every proposal of its own kind, and an annual plan
    once it is closed (the Decided view); everybody else only their own."""
    if proposal is None:
        return False
    if proposal.get("requester") == user.user_id:
        return True
    if proposal.get("kind") == PLAN:
        return can_approve(user.role) and proposal.get("status") not in OPEN
    return can_approve(user.role)


def _names(proposals: list[dict], user: User) -> dict[str, str]:
    """Names of the projects of these proposals – only of projects in the caller's scope."""
    from app.api.deps import full_dataset

    ids = {str(pid) for p in proposals for pid in p.get("projects") or []}
    if not ids:
        return {}
    visible = apply_rls_to_dataset(full_dataset(), user.role, user.user_id)
    return {pid: name for pid, name in project_names(visible).items() if pid in ids}


class Submission(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    justification: str = Field("", max_length=2000)  # required when the verdict is NO-GO or incomplete


class Decision(BaseModel):
    comment: str = Field("", max_length=2000)
    override: bool = False  # approve against a NO-GO: needs the reason in `comment`


class Rejection(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


class Withdrawal(BaseModel):
    reason: str = Field("", max_length=300)


@router.get("/proposals")
def proposals(mine: bool = Query(False, description="Only the caller's own proposals"),
              status_filter: str | None = Query(None, alias="status"),
              view: str | None = Query(None, pattern="^(pending|decided|mine)$",
                                       description="A view of the Approvals page: pending (the PMO inbox, most "
                                                   "urgent first), decided (PMO) or mine"),
              user: User = Depends(data_user)) -> ProposalList:
    approval_flow.run_deadlines()
    if view is not None:
        lists = views.queues(user.user_id, user.role)
        if view not in lists:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the PMO sees the inbox and the decided proposals.")
        items = [p for p in lists[view] if _visible(p, user)]
    else:
        items = list_proposals(status=status_filter,
                               requester=user.user_id if mine or not can_approve(user.role) else None)
        items = [p for p in items if _visible(p, user)]
        if can_approve(user.role) and status_filter == SUBMITTED and not mine:
            # the inbox: escalated first, then oldest; an open annual plan waits for the Board, not the PMO
            items = views.sort_by_urgency([p for p in items if p.get("kind") != PLAN])
    return ProposalList(proposals=[_summary(p) for p in items], project_names=_names(items, user))


@router.get("/proposals/{proposal_id}")
def proposal(proposal_id: str, user: User = Depends(data_user)) -> ProposalDetail:
    """One proposal with its paper, the decision hints of the decision bar (own proposal, acting for a delegator,
    override needed, hand-over only), whether the caller can withdraw it and whether its approval can be reverted."""
    entry = get_proposal(proposal_id)
    if not _visible(entry, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    hints = views.decision_hints(entry, user.user_id, user.role)
    names = _names([entry], user)
    return ProposalDetail(**_summary(entry), paper=entry.get("paper"), verdict_record=entry.get("verdict_record"),
                          project_names=names, decision=hints,
                          change_rows=[{k: str(v) for k, v in row.items()}
                                       for row in change_rows(entry.get("changes") or [], names)],
                          can_decide=not hints["blocked"] and entry.get("status") == SUBMITTED
                          and entry.get("kind") != PLAN,
                          can_withdraw=hints["own"] and entry.get("status") == SUBMITTED,
                          revert=views.revert_state(entry) if can_approve(user.role) else None)


@router.post("/proposals", status_code=status.HTTP_201_CREATED)
def submit(body: Submission, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> ProposalSummary:
    run = runs.get(body.run_id, user.user_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The what-if was not found or has expired – run it again.")
    steering.latest_only(body.run_id, user)  # as in the console: only the latest step of a what-if
    changes = run["changes"] or []
    mode = approval_mode(user.role, changes) if changes else "none"
    if mode != "propose":
        raise HTTPException(status.HTTP_409_CONFLICT, "This what-if has no change that needs a PMO approval.")
    allowed = set(apply_rls_to_dataset(ctx.full, user.role, user.user_id).project_numbers)  # server-side re-check
    if any(c["project_id"] not in allowed for c in changes):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No permission for a project in this change set.")
    label = (run.get("verdict") or {}).get("label")
    if requires_override(label) and not body.justification.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"Give the reason for submitting despite {label} – the PMO sees it with the proposal.")
    try:
        entry = submit_proposal(user.user_id, user.role, run["request"], run["paper"], label, changes,
                                forced=run.get("forced", False), verdict_record=run.get("verdict"),
                                justification=body.justification)
    except ProposalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Proposal not submitted: {exc}") from exc
    runs.pop(body.run_id)  # one proposal per run
    if run.get("chat_message_id"):  # the shared chat shows it as submitted, so no front end offers it again
        update_message(user.role, user.user_id, run["chat_message_id"],
                       {"proposal_id": entry["id"], "proposal_status": SUBMITTED})
    audit("proposal_submitted", role=user.role, user_id=user.user_id, proposal_id=entry["id"], request=run["request"],
          changes=changes, run_id=body.run_id)
    log_security_event("Change proposal submitted", role=user.role, user=user.user_id, proposal=entry["id"],
                       changes=len(changes))
    return ProposalSummary(**_summary(entry))


def _outcome(outcome: approval_flow.Outcome) -> JSONResponse:
    if outcome.written:
        clear_data_cache()
    code = status.HTTP_200_OK if outcome.kind != "error" else status.HTTP_409_CONFLICT
    return JSONResponse({"kind": outcome.kind, "message": outcome.message, "snapshot_id": outcome.snapshot_id},
                        status_code=code)


@router.post("/proposals/{proposal_id}/approve", response_model=DecisionOutcome)
def approve(proposal_id: str, body: Decision, user: User = Depends(approver)) -> JSONResponse:
    if not _visible(get_proposal(proposal_id), user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    return _outcome(approval_flow.approve_proposal(proposal_id, user.user_id, user.role, comment=body.comment.strip(),
                                                   override=body.override))


@router.post("/proposals/{proposal_id}/reject", response_model=DecisionOutcome)
def reject(proposal_id: str, body: Rejection, user: User = Depends(approver)) -> JSONResponse:
    if not _visible(get_proposal(proposal_id), user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    if not body.comment.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A rejection needs a comment.")
    return _outcome(approval_flow.reject_proposal(proposal_id, user.user_id, user.role, body.comment.strip()))


@router.post("/proposals/{proposal_id}/withdraw")
def withdraw(proposal_id: str, body: Withdrawal, user: User = Depends(data_user)) -> ActionResult:
    """The requester takes back their own open proposal; nothing is written."""
    entry = get_proposal(proposal_id)
    if not _visible(entry, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    result = views.withdraw(proposal_id, user.user_id, user.role, body.reason)
    if not result.ok:  # the core re-checks (and logs a refusal) – the requester alone may withdraw
        own = entry.get("requester") == user.user_id
        raise HTTPException(status.HTTP_409_CONFLICT if own else status.HTTP_403_FORBIDDEN, result.message)
    return ActionResult(kind=result.kind, message=result.message)


@router.post("/proposals/{proposal_id}/revert", status_code=status.HTTP_201_CREATED)
def revert(proposal_id: str, user: User = Depends(approver)) -> RevertResult:
    """Proposes the values before an approval as a new proposal; a second PMO approves it (four-eyes). Fields changed
    since the approval block the revert."""
    if not _visible(get_proposal(proposal_id), user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    result = views.revert(proposal_id, user.user_id, user.role, get_data_provider())
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.message)
    return RevertResult(kind=result.kind, message=result.message, proposal=_summary(result.entry))
