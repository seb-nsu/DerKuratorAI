"""Audit pack (E7): an approved proposal becomes one ZIP with its paper, verdict, decision-log records, chain check,
snapshot and a SHA-256 manifest; only the PMO and the System Admin can build it, and only for an approved decision."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from app import approval_flow
from app.core import logs, proposals
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO
from app.crew.audit_pack import AuditPackError, build_audit_pack
from tests import test_api
from tests.conftest import make_dataset

source = test_api.source
PM, PMO, ERP = test_api.PM, test_api.PMO, test_api.ERP


def approved(source) -> dict:
    old = test_api.budget(source)
    entry = proposals.submit_proposal(PM, ROLE_PM, "Raise the ERP budget", "# Decision paper\nRaise it.", "GO",
                                      [{"project_id": ERP, "field": "budget_cost_eur", "old": old, "new": old + 10_000}],
                                      verdict_record={"decision": "GO", "rules": {"eac_overrun_nogo_pct": 30}})
    outcome = approval_flow.approve_proposal(entry["id"], PMO, ROLE_PMO, comment="ok")
    assert outcome.ok, outcome.message
    return proposals.get_proposal(entry["id"])


def unpack(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as pack:
        return {name: pack.read(name) for name in pack.namelist()}


def test_pack_holds_the_whole_decision_and_its_manifest(source):
    proposal = approved(source)
    ds = make_dataset([{"project_number": ERP, "project_name": "General Ledger Consolidation",
                        "source_system": "ServiceNow"}])
    name, data = build_audit_pack(proposal["id"], PMO, ROLE_PMO, ds)
    files = unpack(data)
    assert name == f"audit_pack_{proposal['id']}.zip"
    assert files["decision_paper.md"].decode().startswith("# Decision paper")
    assert json.loads(files["verdict_record.json"])["rules"] == {"eac_overrun_nogo_pct": 30}
    record = json.loads(files["proposal.json"])
    assert record["decided_by"] == PMO and record["requester"] == PM and "paper" not in record
    events = [json.loads(line)["event"] for line in files["audit_trail.jsonl"].decode().splitlines()]
    assert "proposal_approved" in events
    assert json.loads(files["chain_verification.json"])["ok"] is True
    entry = json.loads(files["snapshot/manifest_entry.json"])
    snap = files[f"snapshot/{entry['file']}"]
    assert hashlib.sha256(snap).hexdigest() == entry["sha256"]  # the data before the write, unchanged
    assert json.loads(files["sources.json"]) == [{"project_id": ERP, "project_name": "General Ledger Consolidation",
                                                  "source_system": "ServiceNow", "in_current_data": True}]
    manifest = json.loads(files["manifest.json"])
    assert set(manifest["files"]) == set(files) - {"manifest.json"}
    assert all(hashlib.sha256(files[n]).hexdigest() == h for n, h in manifest["files"].items())


def test_pack_shows_a_broken_chain(source):
    proposal = approved(source)
    lines = logs.AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["comment"] = "changed afterwards"
    logs.AUDIT_LOG.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8")
    check = json.loads(unpack(build_audit_pack(proposal["id"], PMO, ROLE_ADMIN)[1])["chain_verification.json"])
    assert check["ok"] is False and check["broken_at"] == 1


def test_only_pmo_and_admin_and_only_approved(source):
    proposal = approved(source)
    with pytest.raises(PermissionError):
        build_audit_pack(proposal["id"], PM, ROLE_PM)
    with pytest.raises(AuditPackError, match="not found"):
        build_audit_pack("nope", PMO, ROLE_PMO)
    old = test_api.budget(source)
    open_one = proposals.submit_proposal(PM, ROLE_PM, "Another", "paper", "GO",
                                         [{"project_id": ERP, "field": "budget_cost_eur", "old": old, "new": old + 1}])
    with pytest.raises(AuditPackError, match="submitted"):
        build_audit_pack(open_one["id"], PMO, ROLE_PMO)


def test_pack_says_when_the_snapshot_is_gone(source):
    proposal = approved(source)
    for path in logs.SNAPSHOT_DIR.glob("*"):
        if path.name != "manifest.jsonl":
            path.unlink()
    files = unpack(build_audit_pack(proposal["id"], PMO, ROLE_PMO)[1])
    assert "no longer available" in files["snapshot/missing.txt"].decode()
    assert json.loads(files["sources.json"])[0]["in_current_data"] is False  # no dataset given
