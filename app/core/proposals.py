"""Change proposals – four-eyes principle for governed changes.

A Project Manager cannot write budget or project-stop changes directly. "Submit proposal" stores the change set with
the decision paper as `submitted`; the PMO reviews it in the Inbox and approves it (a compare-and-write to the data
source) or rejects it. The requester can never decide their own proposal – enforced here, not only in the UI.

Storage: data/pending_changes.jsonl – append-only event log (like the chat store), serialised by a cross-process lock:
    {"type": "submitted", "id": ..., "requester": ..., "changes": [...], "paper": ..., ...}
    {"type": "pending_apply", "id": ..., "by": ..., "operation_id": ...}   reserved before the write (one approver)
    {"type": "released", "id": ..., "operation_id": ..., "reason": ...}     reservation dropped, nothing was written
    {"type": "decided",   "id": ..., "status": "approved|rejected|stale", "by": ..., "comment": ..., "snapshot_id": ...,
     "operation_id": ..., "on_behalf_of": ...}
    {"type": "withdrawn", "id": ..., "by": ..., "reason": ...}                the requester takes an open proposal back
    {"type": "escalated", "id": ..., "level": 1|2}                           open past the approval deadline
    {"type": "expired",   "id": ...}                                         open past the expiry, closed without a write
    {"type": "delegation_granted", "delegation_id": ..., "delegator": ..., "delegate": ..., "start": ..., "end": ...}
    {"type": "delegation_revoked", "delegation_id": ..., "by": ...}
The current state of a proposal is its `submitted` event merged with its latest `pending_apply`, `released`,
`decided`, `withdrawn`, `escalated` or `expired` event. The operation id ties the decision to the intent/committed
records of the data write.

Approval governance (business rules): an open proposal is escalated when it is older than the approval deadline
(level 2 after twice the deadline) and expires after the expiry period – `enforce_deadlines` records both, it runs
whenever the inbox is read and is idempotent. A PMO can delegate their approval right to another PMO for a limited period;
the deputy decides on behalf of the delegator (recorded in the decision), never on a proposal of themselves or of the
delegator.

Notifications (app/core/notifications.py): a submission, a decision, an escalation and an expiry are recorded in the
notification outbox after the proposal log was written – never inside its lock, and never able to undo the event.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import portalocker

from app.core import clock, logs

logger = logging.getLogger("derkurator.proposals")

SUBMITTED, APPROVED, REJECTED, STALE = "submitted", "approved", "rejected", "stale"
PENDING_APPLY = "pending_apply"  # reserved by one approver while the change set is written
WITHDRAWN, EXPIRED = "withdrawn", "expired"  # closed without a decision: by the requester, or by the deadline
DECISIONS = (APPROVED, REJECTED, STALE)
PLAN = "plan"  # kind of a proposal the Portfolio Board decides (the annual plan); every other kind is the PMO's
OPEN = (SUBMITTED, PENDING_APPLY)


def requires_override(verdict: str | None) -> bool:
    """A NO-GO or incomplete analysis is approved only with an explicit, justified override."""
    v = (verdict or "").upper()
    return "NO-GO" in v or "NO GO" in v or "INCOMPLETE" in v


class ProposalError(RuntimeError):
    pass


def proposals_path() -> Path:
    return Path(logs.PROPOSALS_LOG)  # resolved at call time (tests redirect it)


@contextmanager
def _locked() -> Iterator[Path]:
    path = proposals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(path.with_name(path.name + ".lock")), mode="a", timeout=15, check_interval=0.05):
            yield path
    except portalocker.exceptions.LockException as exc:
        raise ProposalError("The approval inbox is busy – please retry.") from exc


def _append(path: Path, event: dict[str, Any]) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        raise ProposalError(f"Proposal could not be saved: {exc}") from exc


# Parsed proposal log per file version: the inbox, badges and the decision bar read it on every rerun. The key
# (path, mtime, size) changes with every append, so a submit or decision is visible on the next read.
_cache: dict[str, Any] = {"key": None, "items": []}
_cache_lock = threading.Lock()


def _read_all(path: Path) -> list[dict[str, Any]]:
    """Merged proposal states in submission order (copies – callers may change them). Never raises."""
    try:
        st = Path(path).stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _cache_lock:
        if key is not None and _cache["key"] == key:
            return [dict(p) for p in _cache["items"]]
    items = _parse(path)
    with _cache_lock:
        _cache["key"], _cache["items"] = key, items
    return [dict(p) for p in items]


def _parse(path: Path) -> list[dict[str, Any]]:
    """Merged proposal states in submission order. Corrupt lines are skipped. Never raises."""
    try:
        records, malformed = logs.read_jsonl(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.error("Proposal log unreadable: %s", exc)
        return []
    if malformed:
        logger.warning("Proposal log: %d corrupt line(s) skipped", malformed)
    proposals: dict[str, dict[str, Any]] = {}
    for event in records:
        pid = str(event.get("id") or "")
        if event.get("type") == "submitted" and pid:
            proposals[pid] = {**event, "status": SUBMITTED}
        elif event.get("type") == "pending_apply" and pid in proposals:
            proposals[pid].update(status=PENDING_APPLY, pending_by=event.get("by"), pending_at=event.get("timestamp"),
                                  operation_id=event.get("operation_id"))
        elif event.get("type") == "released" and pid in proposals and proposals[pid]["status"] == PENDING_APPLY:
            proposals[pid].update(status=SUBMITTED, pending_by=None, operation_id=None)
        elif event.get("type") == "decided" and pid in proposals and event.get("status") in DECISIONS:
            proposals[pid].update(status=event["status"], decided_by=event.get("by"), decided_role=event.get("role"),
                                  decided_at=event.get("timestamp"), comment=event.get("comment", ""),
                                  snapshot_id=event.get("snapshot_id"), override=bool(event.get("override")),
                                  operation_id=event.get("operation_id") or proposals[pid].get("operation_id"),
                                  on_behalf_of=event.get("on_behalf_of"))
        elif event.get("type") in (WITHDRAWN, EXPIRED) and pid in proposals and proposals[pid]["status"] == SUBMITTED:
            proposals[pid].update(status=event["type"], decided_by=event.get("by"), decided_at=event.get("timestamp"),
                                  comment=event.get("reason", ""))
        elif event.get("type") == "escalated" and pid in proposals:
            proposals[pid].update(escalation_level=int(event.get("level") or 1), escalated_at=event.get("timestamp"))
    return list(proposals.values())


def list_proposals(status: str | None = None, requester: str | None = None) -> list[dict[str, Any]]:
    """Newest first; optionally filtered by status and/or requester."""
    items = [p for p in _read_all(proposals_path())
             if (status is None or p["status"] == status) and (requester is None or p.get("requester") == requester)]
    return sorted(items, key=lambda p: str(p.get("timestamp", "")), reverse=True)


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    return next((p for p in _read_all(proposals_path()) if p["id"] == proposal_id), None)


def _notify(event: str, proposal_id: str, level: int | None = None, status: str | None = None) -> None:
    from app.core.notifications import record_event

    record_event(event, proposal_id, level=level, status=status)  # never raises


def submit_proposal(requester: str, requester_role: str, request: str, paper: str, verdict: str | None,
                    changes: list[dict[str, Any]], forced: bool = False,
                    verdict_record: dict[str, Any] | None = None, reverts: str | None = None,
                    kind: str | None = None, justification: str = "") -> dict[str, Any]:
    """Stores a change set for PMO approval. `verdict` is the label (display, filters, override rule);
    `verdict_record` the full rule-based verdict (reasons, conditions, the thresholds in force, as_of), so the decision
    stays reproducible after the business rules change. The change set is stored as its net change per field
    (a multi-step scenario that cuts, raises and cuts again proposes one change). `reverts`: the approved proposal
    this one takes back. `kind` PLAN: the annual plan, decided by the Portfolio Board. `justification`: why the
    requester submits a NO-GO / incomplete result anyway (the console asks for it). Raises ProposalError."""
    from app.data_adapters.base import net_changes

    changes = net_changes(changes)
    if not changes:
        raise ProposalError("A proposal needs at least one change.")
    event = {
        "type": "submitted", "id": uuid.uuid4().hex[:10], "requester": requester, "requester_role": requester_role,
        "request": request, "verdict": verdict, "verdict_record": verdict_record, "paper": paper,
        "changes": changes, "forced": bool(forced),
        "projects": sorted({str(c.get("project_id")) for c in changes}),
        **({"reverts": reverts} if reverts else {}), **({"kind": kind} if kind else {}),
        **({"justification": justification.strip()} if justification.strip() else {}),
    }
    with _locked() as path:
        logs_entry = {"timestamp": clock.stamp(), "environment": logs.current_environment(),
                      **event}
        _append(path, logs_entry)
    logger.info("Proposal %s submitted by %s (%d changes)", event["id"], requester, len(changes))
    _notify("submitted", event["id"])
    return {**logs_entry, "status": SUBMITTED}


def _decision_rights(by: str, role: str, action: str = "decision") -> str | None:
    """Who may decide: the PMO and the Portfolio Board (which proposals each may decide: _check_decision). A PMO
    who deputises for another PMO decides on their behalf (returns the delegator, recorded in the decision).
    Raises ProposalError (logged as a security event) for anyone else."""
    from app.core.security import ROLE_BOARD, can_approve, log_refusal

    if role == ROLE_BOARD:
        return None
    if can_approve(role):
        delegation = active_delegation(by)
        return delegation["delegator"] if delegation else None
    log_refusal("Approval refused", user=by, role=role, reason="not_approver", decision=action)
    raise ProposalError("Only the PMO can decide on proposals.")


def _same_user(a: Any, b: Any) -> bool:
    """Account names compare like sign-in: trimmed and case-insensitive."""
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _acting_for(delegator: str | None, current: dict[str, Any] | None) -> str | None:
    """The delegator a deputy decides for – except on the delegator's own proposal: the deputy (a PMO) decides it
    in their own name, so with two PMO accounts an active delegation never leaves a proposal without a decider."""
    if delegator and current is not None and _same_user(delegator, current.get("requester")):
        return None
    return delegator


def _check_decision(current: dict[str, Any] | None, proposal_id: str, status: str, by: str, role: str, comment: str,
                    override: bool, operation_id: str | None = None, on_behalf_of: str | None = None) -> None:
    """All rules of a PMO decision on one proposal; raises ProposalError (refusals are logged as security events).
    A proposal reserved for a write (pending_apply) is decided only by that write's operation. A deputy decides a
    proposal of the delegator they act for in their own name (_acting_for). The annual plan (kind PLAN) is decided by the Portfolio Board
    only, and the Board decides nothing else."""
    from app.core.security import ROLE_BOARD, log_refusal

    if current is None:
        raise ProposalError(f"Proposal {proposal_id} not found.")
    if current["status"] == PENDING_APPLY:
        if not operation_id or operation_id != current.get("operation_id"):
            raise ProposalError(f"Proposal {proposal_id} is being applied by {current.get('pending_by')} – "
                                "a second approval is not possible.")
    elif current["status"] != SUBMITTED:
        raise ProposalError(f"Proposal {proposal_id} was already {current['status']} by {current.get('decided_by')}.")
    board_role, plan = role == ROLE_BOARD, current.get("kind") == PLAN
    if board_role != plan:
        log_refusal("Approval refused", user=by, role=role, reason="plan_needs_board" if plan else "board_plan_only",
                    proposal=proposal_id)
        raise ProposalError("The annual plan is decided by the Portfolio Board." if plan else
                            "The Portfolio Board decides only the annual plan.")
    own = _same_user(by, current.get("requester"))
    if status == APPROVED and own:
        log_refusal("Approval refused", user=by, role=role, reason="self_approval", proposal=proposal_id,
                    projects=current.get("projects") or [], fields=[c.get("field") for c in current.get("changes") or []])
        raise ProposalError("Four-eyes principle: you cannot approve your own proposal.")
    if status == REJECTED and own:
        log_refusal("Rejection refused", user=by, role=role, reason="self_rejection", proposal=proposal_id)
        raise ProposalError("Four-eyes principle: you cannot reject your own proposal – a second PMO decides.")
    if status == APPROVED and requires_override(current.get("verdict")) and not (override and comment.strip()):
        raise ProposalError(f"The rule-based verdict is {current.get('verdict')} – approve only with an explicit "
                            "override and a reason.")


def validate_decision(proposal_id: str, status: str, by: str, role: str, comment: str = "",
                      override: bool = False) -> None:
    """Checks a decision without recording it – called before data is written, so a decision that would be refused
    afterwards never leaves written data without a decision record. Raises ProposalError."""
    current = get_proposal(proposal_id)
    on_behalf_of = _acting_for(_decision_rights(by, role, status), current)
    _check_decision(current, proposal_id, status, by, role, comment, override, on_behalf_of=on_behalf_of)


def reserve_apply(proposal_id: str, by: str, role: str, operation_id: str, comment: str = "",
                  override: bool = False) -> dict[str, Any]:
    """Reserves an approval before the change set is written (status pending_apply): all decision rules are checked,
    and no second approver can take the same proposal while it is written. Raises ProposalError."""
    delegator = _decision_rights(by, role, APPROVED)
    with _locked() as path:
        current = next((p for p in _read_all(path) if p["id"] == proposal_id), None)
        on_behalf_of = _acting_for(delegator, current)
        _check_decision(current, proposal_id, APPROVED, by, role, comment, override, on_behalf_of=on_behalf_of)
        _append(path, {"timestamp": clock.stamp(), "environment": logs.current_environment(), "type": "pending_apply",
                       "id": proposal_id, "by": by, "role": role, "operation_id": operation_id,
                       **({"on_behalf_of": on_behalf_of} if on_behalf_of else {})})
    return {**current, "status": PENDING_APPLY, "pending_by": by, "operation_id": operation_id}


def release_apply(proposal_id: str, operation_id: str, reason: str) -> None:
    """Drops the reservation of a write that did not happen (the proposal is open again). Only the reserving
    operation can release it."""
    with _locked() as path:
        current = next((p for p in _read_all(path) if p["id"] == proposal_id), None)
        if current is None or current["status"] != PENDING_APPLY or current.get("operation_id") != operation_id:
            raise ProposalError(f"Proposal {proposal_id} is not reserved by operation {operation_id}.")
        _append(path, {"timestamp": clock.stamp(), "environment": logs.current_environment(), "type": "released",
                       "id": proposal_id, "operation_id": operation_id, "reason": reason[:300]})


def decide(proposal_id: str, status: str, by: str, role: str, comment: str = "",
           snapshot_id: str | None = None, override: bool = False, operation_id: str | None = None) -> dict[str, Any]:
    """Records the PMO decision. Enforces: proposal exists and is still open, only the PMO decides, the requester
    never approves or rejects their own proposal, a rejection carries a reason, a NO-GO / incomplete verdict is
    approved only with `override=True` and a reason. A deputy with an active delegation decides on behalf of the
    delegator (recorded), the delegator's own proposals in their own name. Raises ProposalError."""
    if status not in DECISIONS:
        raise ProposalError(f"Unknown decision '{status}'.")
    # authorization refusals are security events; input/state errors below are not
    delegator = _decision_rights(by, role, status)
    if status == REJECTED and not comment.strip():
        raise ProposalError("Please give a reason for the rejection.")
    with _locked() as path:
        current = next((p for p in _read_all(path) if p["id"] == proposal_id), None)
        on_behalf_of = _acting_for(delegator, current)
        _check_decision(current, proposal_id, status, by, role, comment, override, operation_id, on_behalf_of)
        overridden = bool(override and status == APPROVED and requires_override(current.get("verdict")))
        event = {"timestamp": clock.stamp(), "environment": logs.current_environment(),
                 "type": "decided", "id": proposal_id, "status": status, "by": by, "role": role,
                 "comment": comment.strip(), "snapshot_id": snapshot_id, "override": overridden,
                 "verdict": current.get("verdict"), "verdict_record": current.get("verdict_record"),
                 "operation_id": operation_id, **({"on_behalf_of": on_behalf_of} if on_behalf_of else {})}
        _append(path, event)
    logger.info("Proposal %s %s by %s%s", proposal_id, status, by, " (override)" if overridden else "")
    _notify("decided", proposal_id, status=status)
    return {**current, "status": status, "decided_by": by, "comment": comment.strip(), "snapshot_id": snapshot_id,
            "override": overridden, "operation_id": operation_id, "on_behalf_of": on_behalf_of}


# =========================================================================== withdraw, deadlines, delegation
def withdraw_proposal(proposal_id: str, by: str, role: str, reason: str = "") -> dict[str, Any]:
    """The requester takes back their own open proposal (not while it is being applied). Raises ProposalError."""
    from app.core.security import log_refusal, log_security_event

    with _locked() as path:
        current = next((p for p in _read_all(path) if p["id"] == proposal_id), None)
        if current is None:
            raise ProposalError(f"Proposal {proposal_id} not found.")
        if str(current.get("requester")) != str(by):
            log_refusal("Withdrawal refused", user=by, role=role, reason="not_requester", proposal=proposal_id)
            raise ProposalError("Only the requester can withdraw a proposal.")
        if current["status"] != SUBMITTED:
            raise ProposalError(f"Proposal {proposal_id} is {current['status']} – only an open proposal can be "
                                "withdrawn.")
        _append(path, {"timestamp": clock.stamp(), "environment": logs.current_environment(), "type": WITHDRAWN,
                       "id": proposal_id, "by": by, "role": role, "reason": reason.strip()[:300]})
    log_security_event("Change proposal withdrawn", role=role, user=by, proposal=proposal_id)
    return {**current, "status": WITHDRAWN, "decided_by": by, "comment": reason.strip()[:300]}


def _rules():
    from app.core.business_rules import current_rules

    return current_rules()


def sla_state(proposal: dict[str, Any], now: datetime | None = None, rules: Any = None) -> dict[str, Any]:
    """Deadline view of a proposal: due and expiry time, days open, and the state 'on time', 'overdue' (escalation
    level 1), 'overdue x2' (level 2), 'expires' (past the expiry, not yet recorded) or 'closed'."""
    rules = rules or _rules()
    now = now or clock.now()
    try:
        submitted = clock.parse(proposal.get("timestamp"))
    except (TypeError, ValueError):
        return {"state": "unknown", "level": 0, "due": None, "expires": None, "days_open": None}
    due = submitted + timedelta(days=rules.approval_sla_days)
    expires = submitted + timedelta(days=rules.approval_expiry_days) if rules.approval_expiry_days else None
    days_open = max(0, (now - submitted).days)
    base = {"due": due, "expires": expires, "days_open": days_open}
    if proposal.get("status") not in OPEN:
        return {**base, "state": "closed", "level": 0}
    if expires is not None and now >= expires:
        return {**base, "state": "expires", "level": 2}
    if now >= due + timedelta(days=rules.approval_sla_days):
        return {**base, "state": "overdue x2", "level": 2}
    if now >= due:
        return {**base, "state": "overdue", "level": 1}
    return {**base, "state": "on time", "level": 0}


def enforce_deadlines(now: datetime | None = None) -> list[dict[str, Any]]:
    """Records the escalations and expiries that are due (idempotent: each level and each expiry is recorded once).
    A proposal that is being applied is neither escalated nor expired. Returns the new events."""
    from app.core.security import log_security_event

    rules, now = _rules(), now or clock.now()
    events: list[dict[str, Any]] = []
    with _locked() as path:
        for p in _read_all(path):
            if p["status"] != SUBMITTED:
                continue
            state = sla_state(p, now, rules)
            base = {"timestamp": clock.stamp(), "environment": logs.current_environment(), "id": p["id"]}
            if state["state"] == "expires":
                events.append({**base, "type": EXPIRED, "by": "system", "requester": p.get("requester"),
                               "reason": f"open for {state['days_open']} days – expired without a decision"})
            elif state["level"] > int(p.get("escalation_level") or 0):
                events.append({**base, "type": "escalated", "level": state["level"], "requester": p.get("requester"),
                               "days_open": state["days_open"]})
        for event in events:
            _append(path, event)
    for event in events:
        log_security_event("Change proposal expired" if event["type"] == EXPIRED else
                           f"Change proposal escalated (level {event['level']})", user="system", role="system",
                           proposal=event["id"], requester=event.get("requester"))
        _notify(event["type"], event["id"], level=event.get("level"))
    return events


def _delegations(path: Path) -> list[dict[str, Any]]:
    try:
        records, _ = logs.read_jsonl(path)
    except (FileNotFoundError, OSError):
        return []
    items: dict[str, dict[str, Any]] = {}
    for event in records:
        did = str(event.get("delegation_id") or "")
        if event.get("type") == "delegation_granted" and did:
            items[did] = {**event, "status": "active"}
        elif event.get("type") == "delegation_revoked" and did in items:
            items[did].update(status="revoked", revoked_by=event.get("by"), revoked_at=event.get("timestamp"))
    return list(items.values())


def list_delegations() -> list[dict[str, Any]]:
    """All delegations, newest first, with status 'active' or 'revoked' (an active one may lie in the past)."""
    return sorted(_delegations(proposals_path()), key=lambda d: str(d.get("timestamp", "")), reverse=True)


def active_delegation(user: str | None, on: date | None = None) -> dict[str, Any] | None:
    """The delegation under which `user` deputises today (or on `on`), if any. Only a PMO account deputises; a
    delegation to any other account (an older record) counts for nothing."""
    if not user or _account_role(user) != _pmo_role():
        return None
    on = on or clock.today()
    key = str(user).strip().casefold()
    return next((d for d in list_delegations() if d["status"] == "active"
                 and str(d.get("delegate", "")).strip().casefold() == key
                 and date.fromisoformat(d["start"]) <= on <= date.fromisoformat(d["end"])), None)


def _pmo_role() -> str:
    from app.core.security import ROLE_PMO

    return ROLE_PMO


def _account_role(user: str) -> str | None:
    """The role of a sign-in account, or None if there is no such account (or no user list)."""
    from app.core.auth import load_users

    try:
        account = load_users().get(str(user).strip().casefold())
    except Exception:  # without a valid user list nobody can be checked, so nobody deputises
        logger.exception("User list could not be read – no account can deputise")
        return None
    return account.role if account else None


def grant_delegation(delegator: str, role: str, delegate: str, start: date, end: date, reason: str) -> dict[str, Any]:
    """A PMO hands their approval right to a deputy for a limited period (at most the business rule's maximum).
    Raises ProposalError."""
    from app.core.security import can_approve, log_refusal, log_security_event

    if not can_approve(role):
        log_refusal("Delegation refused", user=delegator, role=role, reason="not_approver")
        raise ProposalError("Only the PMO can delegate the approval right.")
    delegate = (delegate or "").strip()
    if not delegate or delegate.casefold() == str(delegator).strip().casefold():
        raise ProposalError("Choose a deputy other than yourself.")
    # A deputy sees the whole inbox: only another PMO account may deputise, never a project manager
    if _account_role(delegate) != role:
        log_refusal("Delegation refused", user=delegator, role=role, reason="deputy_not_pmo")
        raise ProposalError(f"{delegate} is not a PMO account – only another PMO can deputise.")
    if not reason.strip():
        raise ProposalError("Please give a reason for the delegation (e.g. vacation).")
    if end < start:
        raise ProposalError("The delegation ends before it starts.")
    if start < clock.today():
        raise ProposalError("A delegation cannot start in the past.")
    max_days = _rules().delegation_max_days
    if (end - start).days + 1 > max_days:
        raise ProposalError(f"A delegation lasts at most {max_days} days.")
    with _locked() as path:
        overlap = next((d for d in _delegations(path) if d["status"] == "active"
                        and str(d.get("delegator")) == str(delegator)
                        and str(d.get("delegate", "")).casefold() == delegate.casefold()
                        and date.fromisoformat(d["start"]) <= end and start <= date.fromisoformat(d["end"])), None)
        if overlap:
            raise ProposalError(f"{delegate} already deputises for you from {overlap['start']} to {overlap['end']}.")
        event = {"timestamp": clock.stamp(), "environment": logs.current_environment(), "type": "delegation_granted",
                 "delegation_id": uuid.uuid4().hex[:8], "delegator": delegator, "delegator_role": role,
                 "delegate": delegate, "start": start.isoformat(), "end": end.isoformat(), "reason": reason.strip()[:300]}
        _append(path, event)
    log_security_event("Approval right delegated", user=delegator, role=role, delegate=delegate,
                       start=event["start"], end=event["end"])
    return {**event, "status": "active"}


def revoke_delegation(delegation_id: str, by: str, role: str) -> None:
    """Ends a delegation early – by its delegator or any PMO. Raises ProposalError."""
    from app.core.security import can_approve, log_refusal, log_security_event

    with _locked() as path:
        current = next((d for d in _delegations(path) if d["delegation_id"] == delegation_id), None)
        if current is None or current["status"] != "active":
            raise ProposalError(f"Delegation {delegation_id} is not active.")
        if not can_approve(role) and str(by) != str(current.get("delegator")):
            log_refusal("Delegation revoke refused", user=by, role=role, reason="not_approver")
            raise ProposalError("Only the delegator or the PMO can end a delegation.")
        _append(path, {"timestamp": clock.stamp(), "environment": logs.current_environment(),
                       "type": "delegation_revoked", "delegation_id": delegation_id, "by": by, "role": role})
    log_security_event("Approval delegation revoked", user=by, role=role, delegation=delegation_id)


# =========================================================================== revert an approval
def revert_of(proposal_id: str) -> dict[str, Any] | None:
    """The revert proposal of an approved proposal (open or approved), if any – a proposal is reverted only once."""
    return next((p for p in _read_all(proposals_path())
                 if p.get("reverts") == proposal_id and p["status"] in (SUBMITTED, PENDING_APPLY, APPROVED)), None)


def revert_changes(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """The inverse change set of an applied proposal, from the manifest of the snapshot taken before its write: 'old'
    is what the approval wrote, 'new' the value before. Raises ProposalError."""
    from app.core.snapshots import list_snapshots
    from app.data_adapters.base import is_append, is_manual_change, net_changes

    if proposal.get("status") != APPROVED or not proposal.get("snapshot_id"):
        raise ProposalError(f"Proposal {proposal.get('id')} was not applied – there is nothing to revert.")
    entry = next((s for s in list_snapshots() if s.get("id") == proposal["snapshot_id"]), None)
    if entry is None:
        raise ProposalError(f"The snapshot of proposal {proposal['id']} is no longer available – use the Admin restore.")
    # appended rows (baselines, gate approvals, reserve bookings) are append-only records and stay as history
    written = {(str(c["project_id"]), c.get("table", "Projects"), c["field"], c.get("key")): c
               for c in net_changes(entry.get("changes")) if not is_manual_change(c) and not is_append(c)}
    inverse = []
    for key, change in written.items():
        inverse.append({"project_id": key[0], "table": key[1], "field": key[2], "old": change.get("new"),
                        "new": change.get("old"), "source_tool": "revert_approval",
                        "note": f"revert of proposal {proposal['id']}", **({"key": key[3]} if key[3] else {})})
    if not inverse:
        raise ProposalError(f"Proposal {proposal['id']} wrote no field – there is nothing to revert "
                            "(appended records such as baselines stay as history).")
    return inverse


def submit_revert(proposal_id: str, requester: str, role: str, provider: Any) -> dict[str, Any]:
    """Proposes taking back an applied approval: its inverse change set as a new proposal (four-eyes again). Fields
    changed since the approval are a conflict – then nothing is proposed and the message names them. Raises
    ProposalError."""
    from app.core.security import can_approve

    if not can_approve(role):
        raise ProposalError("Only the PMO can revert an approval.")
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise ProposalError(f"Proposal {proposal_id} not found.")
    existing = revert_of(proposal_id)
    if existing:
        raise ProposalError(f"Proposal {proposal_id} is already being reverted (proposal {existing['id']}).")
    changes = revert_changes(proposal)
    conflicts = provider.find_conflicts({(c["project_id"], c["field"]): c["old"] for c in changes if not c.get("key")})
    rows = {(c["table"], c["key"], c["field"]): c["old"] for c in changes if c.get("key")}
    if rows:
        conflicts += provider.find_row_conflicts(rows, {})
    if conflicts:
        fields = ", ".join(f"{c['project_id']} {c['field']} (now {c['current']})" for c in conflicts)
        raise ProposalError(f"Changed since the approval: {fields} – the revert would overwrite later decisions. "
                            "Revise the fields in a new scenario, or ask the System Admin for a restore.")
    paper = (f"# Revert of proposal {proposal_id}\n\nTakes back the approval of {proposal.get('decided_by')} "
             f"({proposal.get('decided_at')}): every field returns to its value before that write.\n\n"
             f"Original request: {proposal.get('request') or '–'}")
    return submit_proposal(requester, role, f"Revert approval of proposal {proposal_id}", paper, None, changes,
                           reverts=proposal_id)

