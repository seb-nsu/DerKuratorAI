"""Write journal: reconciles the intent and committed records of every data write with the proposal decisions.

Every write (`PortfolioDataProvider.apply_changes`) leaves a `data_write` audit record with `phase: "intent"` before
it writes and one with `phase: "committed"` after it, both with the same `operation_id`. An approval reserves its
proposal as `pending_apply` with that id before the write and records its decision with it afterwards. A step that
breaks off in between leaves a gap, which `reconcile_operations` finds:

- `intent_without_commit`: the write started, but neither a committed nor a failure record follows. The source is
  compared with the snapshot taken before the write (sha256) to say whether the data changed since.
- `committed_without_decision`: a proposal's change set was written, but the proposal carries no decision.
- `pending_without_commit`: a proposal is reserved for a write that never committed.

Nothing is repaired automatically. The System Admin marks a finding as resolved with a mandatory comment
(`mark_resolved`, audited); resolving a `pending_without_commit` finding also releases the reservation, so the
proposal is back in the inbox.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.core import logs

INTENT, COMMITTED = "intent", "committed"
KINDS = {
    "intent_without_commit": "Write started without a committed record",
    "committed_without_decision": "Proposal written without a decision",
    "pending_without_commit": "Proposal reserved for a write that never committed",
}


def _records() -> list[dict[str, Any]]:
    try:
        records, _ = logs.read_jsonl(logs.AUDIT_LOG)
    except FileNotFoundError:
        return []
    return records


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _source_state(snapshot_id: str | None, source: Path | None) -> str:
    """Whether the source still equals the snapshot taken before the write."""
    from app.core.snapshots import get_snapshot

    snap = get_snapshot(snapshot_id) if snapshot_id else None
    if snap is None or source is None:
        return "no snapshot to compare"
    current = _sha256(source)
    if current is None:
        return "source unreadable"
    return "data unchanged since the snapshot (write did not happen)" if current == snap.get("sha256") \
        else "data differs from the snapshot (write or a later change happened)"


def reconcile_operations(source: Path | None = None) -> list[dict[str, Any]]:
    """Open findings of the write journal, oldest first. `source` is the data source file the snapshots were taken
    of (None: no hash comparison). Resolved findings are left out."""
    from app.core.proposals import APPROVED, PENDING_APPLY, REJECTED, STALE, list_proposals

    records = _records()
    resolved = {r.get("operation_id") for r in records if r.get("event") == "operation_resolved"}
    intents: dict[str, dict[str, Any]] = {}
    closed: set[str] = set()
    committed: dict[str, dict[str, Any]] = {}
    for r in records:
        op = r.get("operation_id")
        if not op:
            continue
        if r.get("event") == "data_write" and r.get("phase") == INTENT:
            intents[op] = r
        elif r.get("event") == "data_write" and r.get("phase") == COMMITTED:
            committed[op] = r
            closed.add(op)
        elif r.get("event") == "data_write_failed":
            closed.add(op)

    findings: list[dict[str, Any]] = []
    for op, r in intents.items():
        if op not in closed and op not in resolved:
            findings.append({"kind": "intent_without_commit", "operation_id": op, "snapshot_id": r.get("snapshot_id"),
                             "proposal_id": r.get("proposal_id"), "user_id": r.get("user_id"),
                             "timestamp": r.get("timestamp"), "detail": _source_state(r.get("snapshot_id"), source)})
    proposals = {p["id"]: p for p in list_proposals()}
    for op, r in committed.items():
        pid = r.get("proposal_id")
        if not pid or op in resolved or pid not in proposals:
            continue
        p = proposals[pid]
        if p["status"] not in (APPROVED, REJECTED, STALE) or p.get("operation_id") != op:
            findings.append({"kind": "committed_without_decision", "operation_id": op, "snapshot_id": r.get("snapshot_id"),
                             "proposal_id": pid, "user_id": r.get("user_id"), "timestamp": r.get("timestamp"),
                             "detail": f"proposal status: {p['status']}"})
    for p in proposals.values():
        op = p.get("operation_id")
        if p["status"] == PENDING_APPLY and op not in committed and op not in resolved \
                and not any(f["operation_id"] == op for f in findings):
            findings.append({"kind": "pending_without_commit", "operation_id": op, "snapshot_id": None,
                             "proposal_id": p["id"], "user_id": p.get("pending_by"), "timestamp": p.get("pending_at"),
                             "detail": "intent recorded" if op in intents else "no write started"})
    return sorted(findings, key=lambda f: str(f.get("timestamp") or ""))


def mark_resolved(finding: dict[str, Any], by: str, role: str, comment: str) -> None:
    """Records that a person checked a finding (mandatory comment, audited). A reserved proposal whose write never
    committed is released again. Raises ValueError without a comment; logs.AuditWriteError if the trail fails."""
    from app.core.proposals import ProposalError, release_apply
    from app.core.security import log_security_event

    if not comment.strip():
        raise ValueError("Please describe what was checked before marking the finding as resolved.")
    logs.append_jsonl(logs.AUDIT_LOG, {"event": "operation_resolved", "operation_id": finding["operation_id"],
                                       "kind": finding["kind"], "proposal_id": finding.get("proposal_id"),
                                       "snapshot_id": finding.get("snapshot_id"), "user_id": by, "role": role,
                                       "comment": comment.strip()})
    if finding["kind"] == "pending_without_commit" and finding.get("proposal_id"):
        try:
            release_apply(finding["proposal_id"], finding["operation_id"], f"resolved by {by}: {comment.strip()}")
        except ProposalError:
            pass  # already decided or released in the meantime – the resolution record stands
    log_security_event("Write journal finding resolved", user=by, role=role, operation=finding["operation_id"],
                       kind=finding["kind"], proposal=finding.get("proposal_id"))
