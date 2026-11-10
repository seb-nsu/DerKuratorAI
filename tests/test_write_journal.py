"""Write journal: every write has an intent and a committed record with the same operation id, an approval reserves
its proposal before the write, and reconcile_operations finds every gap a broken-off approval can leave – after the
intent, after the write and after the decision. Nothing is repaired automatically and nothing is written twice."""
from __future__ import annotations

import json

import pytest

from app.core import journal, logs, proposals
from app.core.proposals import APPROVED, PENDING_APPLY, SUBMITTED, ProposalError
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO
from app.data_adapters import JsonPortfolioAdapter
from app.data_adapters import base as adapter_base

DATA = {
    "Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                  "budget_cost_eur": 800000, "state": "Open", "business_case": "Build"}],
    "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": [],
}
CUT = [{"project_id": "P-1", "field": "budget_cost_eur", "old": 800000, "new": 720000, "source_tool": "test"}]
EDIT = [{"project_id": "P-1", "field": "business_case", "old": "Build", "new": "Build v2", "source_tool": "test"}]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps(DATA), encoding="utf-8")
    return path


def _budget(source) -> float:
    return json.loads(source.read_text(encoding="utf-8"))["Projects"][0]["budget_cost_eur"]


def _writes() -> list[dict]:
    return [r for r in logs.read_jsonl(logs.AUDIT_LOG)[0] if r["event"] == "data_write"]


def _proposal():
    return proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "CONDITIONAL GO", CUT)


def _approve(source, entry, by="PMO-ADMIN", op="op-test0000001"):
    """The order of the approval inbox: reserve, write with the operation id, decide with it."""
    proposals.reserve_apply(entry["id"], by, ROLE_PMO, op)
    result = JsonPortfolioAdapter(source).apply_changes(entry["changes"], user=by, role=ROLE_PMO,
                                                        proposal_id=entry["id"], operation_id=op)
    proposals.decide(entry["id"], APPROVED, by, ROLE_PMO, snapshot_id=result.snapshot_id, operation_id=op)
    return result


class TestIntentAndCommit:
    def test_every_write_has_intent_and_committed_with_one_id(self, source):
        result = JsonPortfolioAdapter(source).apply_changes(EDIT, user="PMO-ADMIN", role=ROLE_PMO)
        intent, committed = _writes()
        assert (intent["phase"], committed["phase"]) == ("intent", "committed")
        assert intent["operation_id"] == committed["operation_id"] == result.operation_id
        assert committed["snapshot_id"] == result.snapshot_id and committed["applied"] == 1
        assert journal.reconcile_operations(source) == []

    def test_a_failed_write_closes_its_intent(self, source, monkeypatch):
        def broken(*_a, **_k):
            raise OSError("disk full")
        monkeypatch.setattr(JsonPortfolioAdapter, "_write_changes", broken)
        with pytest.raises(OSError):
            JsonPortfolioAdapter(source).apply_changes(EDIT, user="PMO-ADMIN", role=ROLE_PMO)
        assert journal.reconcile_operations(source) == []


class TestBrokenOff:
    def test_after_the_intent_before_the_write(self, source, monkeypatch):
        """The process dies between the intent record and the write: the data still equals the snapshot."""
        def killed(*_a, **_k):
            raise KeyboardInterrupt
        monkeypatch.setattr(JsonPortfolioAdapter, "_write_changes", killed)
        with pytest.raises(KeyboardInterrupt):
            JsonPortfolioAdapter(source).apply_changes(EDIT, user="PMO-ADMIN", role=ROLE_PMO)
        [finding] = journal.reconcile_operations(source)
        assert finding["kind"] == "intent_without_commit" and "unchanged" in finding["detail"]
        assert json.loads(source.read_text(encoding="utf-8"))["Projects"][0]["business_case"] == "Build"

    def test_after_the_write_before_the_committed_record(self, source, monkeypatch):
        monkeypatch.setattr(adapter_base, "_record_commit", lambda *_a, **_k: None)
        result = JsonPortfolioAdapter(source).apply_changes(EDIT, user="PMO-ADMIN", role=ROLE_PMO)
        [finding] = journal.reconcile_operations(source)
        assert finding["kind"] == "intent_without_commit" and finding["operation_id"] == result.operation_id
        assert "differs" in finding["detail"] and finding["snapshot_id"] == result.snapshot_id

    def test_after_the_write_before_the_decision(self, source):
        entry = _proposal()
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-crash000001")
        JsonPortfolioAdapter(source).apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO,
                                                   proposal_id=entry["id"], operation_id="op-crash000001")
        [finding] = journal.reconcile_operations(source)
        assert (finding["kind"], finding["proposal_id"]) == ("committed_without_decision", entry["id"])
        assert proposals.get_proposal(entry["id"])["status"] == PENDING_APPLY  # nobody else can approve it

    def test_after_the_decision_nothing_is_open(self, source):
        _approve(source, _proposal())
        assert journal.reconcile_operations(source) == []

    def test_reserved_but_never_written(self, source):
        entry = _proposal()
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-lost0000001")
        [finding] = journal.reconcile_operations(source)
        assert finding["kind"] == "pending_without_commit" and finding["detail"] == "no write started"


class TestReservation:
    def test_a_second_approver_is_refused_while_the_first_writes(self, source):
        entry = _proposal()
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-first000001")
        with pytest.raises(ProposalError, match="being applied"):
            proposals.reserve_apply(entry["id"], "PMO-DEPUTY", ROLE_PMO, "op-second00001")
        with pytest.raises(ProposalError, match="being applied"):
            proposals.decide(entry["id"], APPROVED, "PMO-DEPUTY", ROLE_PMO)

    def test_a_second_approval_after_the_write_fails_and_writes_nothing(self, source):
        entry = _proposal()
        _approve(source, entry)
        with pytest.raises(ProposalError, match="already approved"):
            proposals.reserve_apply(entry["id"], "PMO-DEPUTY", ROLE_PMO, "op-again000001")
        assert _budget(source) == 720000
        assert len([w for w in _writes() if w["phase"] == "committed"]) == 1

    def test_the_decision_carries_the_operation_id(self, source):
        entry = _proposal()
        result = _approve(source, entry, op="op-trace000001")
        assert proposals.get_proposal(entry["id"])["operation_id"] == "op-trace000001" == result.operation_id

    def test_release_puts_the_proposal_back_in_the_inbox(self, source):
        entry = _proposal()
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-free0000001")
        with pytest.raises(ProposalError):
            proposals.release_apply(entry["id"], "op-other000001", "wrong operation")
        proposals.release_apply(entry["id"], "op-free0000001", "conflict")
        assert proposals.get_proposal(entry["id"])["status"] == SUBMITTED

    def test_the_requester_cannot_reserve_their_own_proposal(self, source):
        entry = proposals.submit_proposal("PMO-DEPUTY", ROLE_PMO, "cut", "", "GO", CUT)
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.reserve_apply(entry["id"], "PMO-DEPUTY", ROLE_PMO, "op-self0000001")


class TestResolve:
    def test_resolving_needs_a_comment_and_is_audited(self, source):
        entry = _proposal()
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-stuck000001")
        [finding] = journal.reconcile_operations(source)
        with pytest.raises(ValueError):
            journal.mark_resolved(finding, "SYSTEM-ADMIN", ROLE_ADMIN, "  ")
        journal.mark_resolved(finding, "SYSTEM-ADMIN", ROLE_ADMIN, "Checked: nothing was written")
        assert journal.reconcile_operations(source) == []
        record = [r for r in logs.read_jsonl(logs.AUDIT_LOG)[0] if r["event"] == "operation_resolved"][-1]
        assert record["operation_id"] == "op-stuck000001" and record["comment"] == "Checked: nothing was written"
        assert proposals.get_proposal(entry["id"])["status"] == SUBMITTED  # released: back in the inbox

    def test_nothing_is_repaired_automatically(self, source, monkeypatch):
        monkeypatch.setattr(adapter_base, "_record_commit", lambda *_a, **_k: None)
        JsonPortfolioAdapter(source).apply_changes(EDIT, user="PMO-ADMIN", role=ROLE_PMO)
        before = source.read_text(encoding="utf-8")
        journal.reconcile_operations(source)
        journal.reconcile_operations(source)
        assert source.read_text(encoding="utf-8") == before
