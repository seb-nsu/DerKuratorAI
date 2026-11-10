"""The PMO decision on a change proposal, shared by every front end (Streamlit pages and the HTTP API).

Approve = validate the decision, reserve the proposal (no second approver), compare-and-write the change set with a
snapshot, record the decision, audit it. Reject = record the decision with its reason, audit it. Each step keeps the
backend checks (role, four-eyes, stored change set); the caller only shows the returned outcome.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.core import logs
from app.core.proposals import (
    APPROVED,
    REJECTED,
    STALE,
    ProposalError,
    decide,
    enforce_deadlines,
    get_proposal,
    release_apply,
    requires_override,
    reserve_apply,
    validate_decision,
)
from app.core.security import log_security_event
from app.data_adapters import DataConflictError, DataSourceError, get_data_provider
from app.data_adapters.base import new_operation_id

logger = logging.getLogger("derkurator.approval_flow")


@dataclass(frozen=True)
class Outcome:
    """`kind`: success | warning | error. `written`: the data source changed (caches must be refreshed)."""
    kind: str
    message: str
    written: bool = False
    snapshot_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "success"


def audit(event: str, **payload) -> None:
    """Appends an audit entry. Blocking: raises logs.AuditWriteError if the trail cannot be written."""
    logs.append_jsonl(logs.AUDIT_LOG, {"event": event, **payload})


def audit_best_effort(event: str, **payload) -> None:
    try:
        audit(event, **payload)
    except Exception:  # the caller already reports the original problem
        logger.exception("Audit event %s could not be written", event)


def record_incident(event: str, incident: str, **fields) -> None:
    """System errors go to the incident log (data/incidents.jsonl), never to the decision log; best effort – the
    system log already holds the traceback."""
    try:
        logs.append_jsonl(logs.INCIDENTS_LOG, {"event": event, "incident_id": incident, **fields})
    except OSError:
        logger.exception("Incident %s could not be recorded", incident)


def written_audit_failed(applied: int, snapshot_id: str | None) -> str:
    incident = uuid.uuid4().hex[:8].upper()
    logger.exception("Changes written but the audit record failed (incident %s)", incident)
    record_incident("audit_write_failed", incident, fields=applied, snapshot_id=snapshot_id)
    log_security_event("Write without decision record", level=logging.ERROR, incident=incident, fields=applied,
                       snapshot=snapshot_id)
    return (f"{applied} field change(s) written (snapshot {snapshot_id}), but the audit record failed (incident "
            f"{incident}). Please contact your System Admin.")


def _release_quietly(proposal_id: str, operation: str, reason: str) -> None:
    """Nothing was written: the proposal goes back to the inbox. If even that fails, the write journal reports it."""
    try:
        release_apply(proposal_id, operation, reason)
    except ProposalError as exc:
        logger.warning("Reservation of proposal %s could not be released: %s", proposal_id, exc)


def approve_proposal(proposal_id: str, user_id: str, role: str, comment: str = "", override: bool = False) -> Outcome:
    """PMO approval: compare-and-write of the stored change set (with a snapshot), then the decision record. The
    decision is validated before anything is written (e.g. a NO-GO without override is refused up front)."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        return Outcome("error", f"Proposal {proposal_id} not found.")
    pid = proposal["id"]
    operation = new_operation_id()  # intent, committed, decision, audit and security event share it
    try:
        validate_decision(pid, APPROVED, user_id, role, comment=comment, override=override)
        reserve_apply(pid, user_id, role, operation, comment=comment, override=override)  # no second approver
    except ProposalError as exc:
        return Outcome("error", str(exc))
    try:
        result = get_data_provider().apply_changes(proposal["changes"], user=user_id, reason=f"proposal_{pid}",
                                                   role=role, proposal_id=pid, operation_id=operation)
    except DataConflictError as exc:  # data changed since the simulation, so the proposal is outdated
        try:
            decide(pid, STALE, user_id, role, comment=f"Data changed since submission: {exc}", operation_id=operation)
        except ProposalError as stale_exc:  # the outdated proposal stays open – say so instead of hiding it
            logger.warning("Proposal %s could not be marked stale: %s", pid, stale_exc)
            return Outcome("error", f"Proposal {pid} is outdated – nothing was written. It could not be marked as "
                                    f"outdated: {stale_exc}")
        return Outcome("error", f"Proposal {pid} is outdated – nothing was written. {exc}")
    except DataSourceError as exc:
        _release_quietly(pid, operation, str(exc))
        return Outcome("error", f"Proposal {pid} could not be applied: {exc}")
    except Exception as exc:  # refused or broken before the write: the proposal goes back to the inbox
        _release_quietly(pid, operation, exc.__class__.__name__)
        raise
    try:
        decide(pid, APPROVED, user_id, role, comment=comment, snapshot_id=result.snapshot_id, override=override,
               operation_id=operation)
    except ProposalError as exc:  # data is written; the decision record could not be stored, so inform the PMO
        logger.error("Proposal %s applied but decision not recorded: %s", pid, exc)
        audit_best_effort("proposal_applied_undecided", role=role, user_id=user_id, proposal_id=pid,
                          snapshot_id=result.snapshot_id, applied=result.applied, error=str(exc)[:200],
                          operation_id=operation)
        return Outcome("warning", f"Changes applied (snapshot {result.snapshot_id}), but the decision could not be "
                                  f"recorded: {exc}", written=True, snapshot_id=result.snapshot_id)
    overridden = bool(override and requires_override(proposal.get("verdict")))
    try:
        audit("proposal_approved", role=role, user_id=user_id, proposal_id=pid, requester=proposal.get("requester"),
              changes=proposal["changes"], applied=result.applied, manual=result.manual, snapshot_id=result.snapshot_id,
              override=overridden, verdict=proposal.get("verdict"), verdict_record=proposal.get("verdict_record"),
              comment=comment, operation_id=operation)
    except Exception:  # noqa: BLE001 - written and decided: say so, never "nothing was confirmed"
        return Outcome("warning", written_audit_failed(result.applied, result.snapshot_id), written=True,
                       snapshot_id=result.snapshot_id)
    log_security_event("Change proposal approved with verdict override (four-eyes)" if overridden else
                       "Change proposal approved (four-eyes)", role=role, user=user_id, proposal=pid,
                       requester=proposal.get("requester"), fields=result.applied, override=overridden,
                       operation=operation)
    return Outcome("success", f"Proposal {pid} of {proposal.get('requester')} approved"
                              f"{' with override' if overridden else ''} – {result.applied} field change(s) written "
                              f"(snapshot {result.snapshot_id}).", written=True, snapshot_id=result.snapshot_id)


def reject_proposal(proposal_id: str, user_id: str, role: str, comment: str) -> Outcome:
    proposal = get_proposal(proposal_id)
    if proposal is None:
        return Outcome("error", f"Proposal {proposal_id} not found.")
    try:
        decide(proposal_id, REJECTED, user_id, role, comment=comment)
    except ProposalError as exc:
        return Outcome("error", str(exc))
    audit("proposal_rejected", role=role, user_id=user_id, proposal_id=proposal_id,
          requester=proposal.get("requester"), comment=comment)
    log_security_event("Change proposal rejected", role=role, user=user_id, proposal=proposal_id)
    return Outcome("success", f"Proposal {proposal_id} of {proposal.get('requester')} rejected.")


def run_deadlines() -> list[dict]:
    """Records due escalations and expiries (each with an audit entry) and returns the events, so the caller can say
    what changed. Never raises: a deadline check that cannot run is retried on the next view."""
    try:
        events = enforce_deadlines()
    except ProposalError as exc:
        logger.warning("Approval deadlines not checked: %s", exc)
        return []
    for event in events:
        try:
            audit("proposal_expired" if event["type"] == "expired" else "proposal_escalated", role="system",
                  user_id="system", proposal_id=event["id"], requester=event.get("requester"),
                  level=event.get("level"))
        except Exception:  # noqa: BLE001 - the proposal log already holds the event
            logger.warning("Business audit for deadline event of %s failed", event["id"])
    return events
