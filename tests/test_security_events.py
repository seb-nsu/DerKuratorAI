"""Refused write and approval attempts leave exactly one security event (IDs, field names and a reason code only);
the refusal itself is unchanged, and allowed operations or non-authorization errors are not logged."""
from __future__ import annotations

import json
import logging

import pytest

from app.core import proposals, security
from app.core.proposals import APPROVED, REJECTED, ProposalError
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO
from app.data_adapters import DataConflictError, JsonPortfolioAdapter, WriteNotAuthorizedError

OTHER_PERSON = "Rowan Ellis"  # manager of the foreign project P-2 – must never appear in a log line


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "portfolio.json"
    data = {"Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                          "budget_cost_eur": 800000, "state": "Open", "business_case": "Build"},
                         {"project_number": "P-2", "project_name": "Beta", "project_manager": OTHER_PERSON,
                          "budget_cost_eur": 123456, "state": "Open", "business_case": "Build"}],
            "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def events(caplog):
    """Returns the SECURITY refusal lines logged so far."""
    caplog.set_level(logging.INFO, logger="derkurator.security")
    return lambda: [r.getMessage() for r in caplog.records
                    if r.getMessage().startswith("SECURITY | ") and " refused" in r.getMessage()]


def _c(field, old, new, pid="P-1"):
    return {"project_id": pid, "table": "Projects", "field": field, "old": old, "new": new, "source_tool": "test"}


def _reason(line: str) -> str:
    return line.split("reason=", 1)[1].split(",", 1)[0].strip()


class TestRefusedWrites:
    def test_pm_governed_field(self, source, events):
        with pytest.raises(WriteNotAuthorizedError, match="Governed fields"):
            JsonPortfolioAdapter(source).apply_changes([_c("budget_cost_eur", 800000, 1)], user="Priya Raman",
                                                       role=ROLE_PM)
        [line] = events()
        assert _reason(line) == "governed_without_proposal"
        assert "Write refused" in line and "user=Priya Raman" in line and "projects=P-1" in line
        assert "fields=budget_cost_eur" in line
        assert not any(ch.isdigit() for ch in line.split("fields=", 1)[1])  # no old/new values or amounts

    def test_pm_foreign_project(self, source, events):
        with pytest.raises(WriteNotAuthorizedError, match="No permission for: P-2"):
            JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "Test", pid="P-2")], user="Priya Raman",
                                                       role=ROLE_PM)
        [line] = events()
        assert _reason(line) == "foreign_project" and "projects=P-2" in line
        assert OTHER_PERSON not in line and "Beta" not in line and "123456" not in line

    def test_system_admin(self, source, events):
        with pytest.raises(WriteNotAuthorizedError, match="no write access"):
            JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "x")], user="SYSTEM-ADMIN",
                                                       role=ROLE_ADMIN)
        [line] = events()
        assert _reason(line) == "no_write_role"

    def test_self_approval_via_authorize(self, source, events):
        changes = [_c("budget_cost_eur", 800000, 700000)]
        entry = proposals.submit_proposal("PMO-ADMIN", ROLE_PMO, "cut", "", "GO", changes)
        with pytest.raises(WriteNotAuthorizedError, match="Four-eyes"):
            JsonPortfolioAdapter(source).apply_changes(changes, user="PMO-ADMIN", role=ROLE_PMO,
                                                       proposal_id=entry["id"])
        [line] = events()
        assert _reason(line) == "self_approval" and f"proposal={entry['id']}" in line
        assert "700000" not in line

    def test_proposal_not_open_and_changeset_mismatch(self, source, events):
        changes = [_c("budget_cost_eur", 800000, 700000)]
        adapter = JsonPortfolioAdapter(source)
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO", changes)
        with pytest.raises(WriteNotAuthorizedError, match="differs"):
            adapter.apply_changes([_c("budget_cost_eur", 800000, 1)], user="PMO-ADMIN", role=ROLE_PMO,
                                  proposal_id=entry["id"])
        proposals.decide(entry["id"], REJECTED, "PMO-ADMIN", ROLE_PMO, comment="no")
        with pytest.raises(WriteNotAuthorizedError, match="already rejected"):
            adapter.apply_changes(changes, user="PMO-DEPUTY", role=ROLE_PMO, proposal_id=entry["id"])
        assert [_reason(line) for line in events()] == ["changeset_mismatch", "proposal_not_open"]

    def test_pmo_governed_write_without_proposal(self, source, events):
        with pytest.raises(WriteNotAuthorizedError, match="approved proposal"):
            JsonPortfolioAdapter(source).apply_changes([_c("state", "Open", "Canceled")], user="PMO-ADMIN",
                                                       role=ROLE_PMO)
        assert [_reason(line) for line in events()] == ["governed_without_proposal"]


class TestRefusedApprovals:
    def test_self_approval_via_decide(self, events):
        entry = proposals.submit_proposal("PMO-ADMIN", ROLE_PMO, "cut", "", "GO", [_c("state", "Open", "Canceled")])
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)
        [line] = events()
        assert "Approval refused" in line and _reason(line) == "self_approval"
        assert "projects=P-1" in line and "fields=state" in line and "Canceled" not in line

    def test_pm_cannot_decide(self, events):
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO", [_c("state", "Open", "Canceled")])
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.decide(entry["id"], APPROVED, OTHER_PERSON, ROLE_PM)
        [line] = events()
        assert _reason(line) == "not_approver" and f"user={OTHER_PERSON}" in line  # acting user only


class TestNotLogged:
    def test_allowed_write(self, source, events):
        JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "Test")], user="Priya Raman", role=ROLE_PM)
        assert events() == []

    def test_non_authorization_errors(self, source, events):
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO", [_c("state", "Open", "Canceled")])
        with pytest.raises(ProposalError, match="reason"):  # input error, not a security refusal
            proposals.decide(entry["id"], REJECTED, "PMO-ADMIN", ROLE_PMO)
        with pytest.raises(DataConflictError):  # data conflict, not a security refusal
            JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Changed meanwhile", "Test")], user="Priya Raman",
                                                       role=ROLE_PM)
        assert events() == []


def test_failing_log_write_never_changes_the_refusal(source, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("log volume full")

    monkeypatch.setattr(security, "log_security_event", broken)
    with pytest.raises(WriteNotAuthorizedError, match="Governed fields"):  # same type and message as without logging
        JsonPortfolioAdapter(source).apply_changes([_c("budget_cost_eur", 800000, 1)], user="Priya Raman", role=ROLE_PM)
