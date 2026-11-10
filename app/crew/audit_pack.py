"""Audit pack (E7): everything an internal audit needs about one approved decision, as one ZIP. The proposal as it was
submitted and decided, the decision paper, the verdict with the thresholds then in force, every decision-log record
of the proposal and of its write operation, the result of the hash-chain check, the snapshot of the data before the
write with its manifest entry, the source system of each affected project, and a SHA-256 manifest over all files.

Read-only: nothing is written into data/, the pack is built in memory. PMO and System Admin only."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from app.core import clock, logs, proposals, snapshots
from app.core.security import ROLE_ADMIN, ROLE_PMO

ALLOWED_ROLES = (ROLE_PMO, ROLE_ADMIN)
README = """Audit pack of proposal {pid}
Built {built} by {user} ({role}).

proposal.json            the proposal as submitted and decided (requester, decider, timestamps, change set)
decision_paper.md        the decision paper the decider saw
verdict_record.json      the rule-based verdict with its reasons, conditions and the thresholds in force
audit_trail.jsonl        every decision-log record of this proposal or of its write operation
chain_verification.json  the hash-chain check of the whole decision log at build time
snapshot/                the data before the write and its manifest entry (sha256 of the file)
sources.json             the source system of each affected project
manifest.json            SHA-256 of every file above

Check: sha256sum of each file must match manifest.json; snapshot/manifest_entry.json names the sha256 the
snapshot had when it was taken.
"""


class AuditPackError(RuntimeError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def _trail(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        records, _ = logs.read_jsonl(Path(logs.AUDIT_LOG))
    except FileNotFoundError:
        return []
    pid, operation = proposal.get("id"), proposal.get("operation_id")
    return [r for r in records if r.get("proposal_id") == pid or (operation and r.get("operation_id") == operation)]


def _sources(proposal: dict[str, Any], dataset: Any) -> list[dict[str, Any]]:
    projects = getattr(dataset, "projects", None)
    rows = {} if projects is None or projects.empty else {str(r.get("project_number")): r
                                                            for r in projects.to_dict("records")}
    out = []
    for pid in proposal.get("projects") or []:
        row = rows.get(str(pid))
        out.append({"project_id": pid, "project_name": (row or {}).get("project_name"),
                    "source_system": (row or {}).get("source_system") or ("single source" if row else None),
                    "in_current_data": row is not None})
    return out


def build_audit_pack(proposal_id: str, user: str, role: str, dataset: Any = None) -> tuple[str, bytes]:
    """(file name, ZIP bytes) for an approved proposal. Raises PermissionError for other roles and AuditPackError
    when the proposal is unknown or not approved."""
    if role not in ALLOWED_ROLES:
        raise PermissionError("Only the PMO and the System Admin can build an audit pack.")
    proposal = proposals.get_proposal(proposal_id)
    if proposal is None:
        raise AuditPackError(f"Proposal {proposal_id} not found.")
    if proposal.get("status") != proposals.APPROVED:
        raise AuditPackError(f"Proposal {proposal_id} is {proposal.get('status')}, an audit pack needs an approved "
                             "decision.")
    pid = str(proposal["id"])
    files: dict[str, bytes] = {
        "README.txt": README.format(pid=pid, built=clock.stamp(), user=user, role=role).encode("utf-8"),
        "proposal.json": _json({k: v for k, v in proposal.items() if k not in ("paper", "verdict_record")}),
        "decision_paper.md": str(proposal.get("paper") or "").encode("utf-8"),
        "verdict_record.json": _json(proposal.get("verdict_record") or {}),
        "audit_trail.jsonl": "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                     for r in _trail(proposal)).encode("utf-8"),
        "chain_verification.json": _json(logs.verify_chain(Path(logs.AUDIT_LOG))),
        "sources.json": _json(_sources(proposal, dataset)),
    }
    entry = snapshots.get_snapshot(str(proposal.get("snapshot_id") or ""))
    if entry:
        files["snapshot/manifest_entry.json"] = _json(entry)
        files[f"snapshot/{entry['file']}"] = (snapshots.snapshot_dir() / str(entry["file"])).read_bytes()
    else:
        files["snapshot/missing.txt"] = (f"Snapshot {proposal.get('snapshot_id') or '-'} is no longer available "
                                         "(store mode or removed).\n").encode("utf-8")
    manifest = {"proposal_id": pid, "built_at": clock.stamp(), "built_by": user,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as pack:
        for name, data in files.items():
            pack.writestr(name, data)
        pack.writestr("manifest.json", _json(manifest))
    return f"audit_pack_{pid}.zip", buffer.getvalue()
