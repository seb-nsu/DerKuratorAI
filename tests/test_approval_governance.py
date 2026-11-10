"""Approval governance: withdrawing an own proposal, the approval deadline with escalation and expiry, and the
delegation of the approval right to another PMO – who decides on behalf of the delegator, never their own proposal,
and the delegator's own proposals in their own name (two PMO accounts never block each other). Only PMO accounts
deputise."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core import clock, proposals
from app.core.proposals import APPROVED, EXPIRED, REJECTED, SUBMITTED, WITHDRAWN, ProposalError
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO
from app.data_adapters import JsonPortfolioAdapter
from app.data_adapters.base import WriteNotAuthorizedError

CUT = [{"project_id": "P-1", "table": "Projects", "field": "budget_cost_eur", "old": 800000, "new": 720000}]
T0 = datetime(2029, 6, 1, 9, 0, tzinfo=timezone.utc)
DATA = {"Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                      "budget_cost_eur": 800000, "state": "Open"}],
        "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": []}


@pytest.fixture
def at(monkeypatch):
    """Sets the app clock: at(days) = T0 + days."""
    def set_time(days: float = 0) -> datetime:
        moment = T0 + timedelta(days=days)
        monkeypatch.setattr(clock, "now", lambda: moment)
        return moment
    set_time(0)
    return set_time


def _submit(requester="Priya Raman", role=ROLE_PM):
    return proposals.submit_proposal(requester, role, "Cut", "", "GO", CUT)


class TestWithdraw:
    def test_the_requester_withdraws_an_open_proposal(self):
        entry = _submit()
        proposals.withdraw_proposal(entry["id"], "Priya Raman", ROLE_PM, "not needed any more")
        stored = proposals.get_proposal(entry["id"])
        assert stored["status"] == WITHDRAWN and stored["comment"] == "not needed any more"
        with pytest.raises(ProposalError, match="already withdrawn"):
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)

    def test_nobody_else_withdraws_it(self):
        entry = _submit()
        with pytest.raises(ProposalError, match="Only the requester"):
            proposals.withdraw_proposal(entry["id"], "PMO-ADMIN", ROLE_PMO)

    def test_a_decided_or_reserved_proposal_cannot_be_withdrawn(self):
        decided, reserved = _submit(), _submit()
        proposals.decide(decided["id"], REJECTED, "PMO-ADMIN", ROLE_PMO, comment="no")
        proposals.reserve_apply(reserved["id"], "PMO-ADMIN", ROLE_PMO, "op-withdraw001")
        for entry in (decided, reserved):
            with pytest.raises(ProposalError, match="only an open proposal"):
                proposals.withdraw_proposal(entry["id"], "Priya Raman", ROLE_PM)


class TestDeadlines:
    def test_on_time_then_escalated_then_expired(self, at):
        entry = _submit()  # rules: deadline 5 days, expiry 30 days
        assert proposals.sla_state(entry)["state"] == "on time"
        at(5)
        [event] = proposals.enforce_deadlines()
        assert (event["type"], event["level"]) == ("escalated", 1)
        assert proposals.enforce_deadlines() == []  # idempotent
        at(10)
        assert proposals.enforce_deadlines()[0]["level"] == 2
        at(30)
        [event] = proposals.enforce_deadlines()
        assert event["type"] == EXPIRED
        stored = proposals.get_proposal(entry["id"])
        assert stored["status"] == EXPIRED and stored["decided_by"] == "system" and stored["escalation_level"] == 2
        with pytest.raises(ProposalError, match="already expired"):
            proposals.decide(stored["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)

    def test_a_proposal_being_applied_never_expires(self, at):
        entry = _submit()
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-slow0000001")
        at(40)
        assert proposals.enforce_deadlines() == []
        assert proposals.get_proposal(entry["id"])["status"] == proposals.PENDING_APPLY

    def test_expiry_can_be_switched_off(self, at, monkeypatch):
        from app.core.business_rules import BusinessRules

        monkeypatch.setattr(proposals, "_rules", lambda: BusinessRules(approval_expiry_days=0))
        _submit()
        at(400)
        [event] = proposals.enforce_deadlines()
        assert event["type"] == "escalated"


class TestDelegation:
    def _delegate(self, delegate="PMO-DEPUTY", days=7, start=None):
        start = start or clock.today()
        return proposals.grant_delegation("PMO-ADMIN", ROLE_PMO, delegate, start, start + timedelta(days=days - 1),
                                          "vacation")

    def test_rules_of_a_delegation(self, at):
        today = clock.today()
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.grant_delegation("Priya Raman", ROLE_PM, "PMO-DEPUTY", today, today, "x")
        with pytest.raises(ProposalError, match="other than yourself"):
            proposals.grant_delegation("PMO-ADMIN", ROLE_PMO, "pmo-admin", today, today, "x")
        with pytest.raises(ProposalError, match="reason"):
            proposals.grant_delegation("PMO-ADMIN", ROLE_PMO, "PMO-DEPUTY", today, today, " ")
        with pytest.raises(ProposalError, match="at most 30 days"):
            self._delegate(days=31)
        with pytest.raises(ProposalError, match="in the past"):
            self._delegate(start=today - timedelta(days=1))
        self._delegate()
        with pytest.raises(ProposalError, match="already deputises"):
            self._delegate(days=3)

    @pytest.mark.parametrize("deputy", ["Rowan Ellis", "BOARD-CHAIR", "nobody-here"])
    def test_only_a_pmo_account_deputises(self, at, deputy):
        """A deputy sees the whole inbox – a project manager, the Board or an unknown account never gets it."""
        with pytest.raises(ProposalError, match="not a PMO account"):
            self._delegate(delegate=deputy)

    def test_an_older_delegation_to_a_project_manager_gives_no_rights(self, at):
        # an own MonkeyPatch: undoing the test's monkeypatch would also undo the isolation of the data folder
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(proposals, "_account_role", lambda user: ROLE_PMO)
            self._delegate(delegate="Rowan Ellis")
        at(0)
        assert proposals.active_delegation("Rowan Ellis") is None
        entry = _submit(requester="Mara Lenz")
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.decide(entry["id"], REJECTED, "Rowan Ellis", ROLE_PM, comment="x")

    def test_the_deputy_decides_on_behalf_of_the_delegator(self, at):
        self._delegate()
        entry = _submit(requester="Mara Lenz")
        decided = proposals.decide(entry["id"], REJECTED, "PMO-DEPUTY", ROLE_PMO, comment="too early")
        assert decided["on_behalf_of"] == "PMO-ADMIN"
        assert proposals.get_proposal(entry["id"])["on_behalf_of"] == "PMO-ADMIN"

    def test_the_deputy_never_decides_their_own_proposal(self, at):
        self._delegate()
        own = _submit(requester="PMO-DEPUTY", role=ROLE_PMO)
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.decide(own["id"], APPROVED, "PMO-DEPUTY", ROLE_PMO)

    def test_with_two_pmo_accounts_the_deputy_decides_the_delegators_proposal_in_their_own_name(self, at):
        """Delegator and deputy are the only PMOs: the delegator's own proposal must still find a decider."""
        self._delegate()
        delegators = _submit(requester="PMO-ADMIN", role=ROLE_PMO)
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.decide(delegators["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)
        decided = proposals.decide(delegators["id"], APPROVED, "PMO-DEPUTY", ROLE_PMO)
        assert decided["decided_by"] == "PMO-DEPUTY" and not decided.get("on_behalf_of")

    def test_the_own_proposal_check_ignores_upper_and_lower_case(self, at):
        entry = _submit(requester="pmo-admin", role=ROLE_PMO)
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)

    def test_outside_the_period_or_after_a_revoke_the_pmo_decides_for_themselves(self, at):
        entry = _submit(requester="Mara Lenz")
        granted = self._delegate(days=2)
        at(0)
        with pytest.raises(ProposalError, match="Only the delegator"):
            proposals.revoke_delegation(granted["delegation_id"], "Mara Lenz", ROLE_PM)
        proposals.revoke_delegation(granted["delegation_id"], "PMO-ADMIN", ROLE_PMO)
        assert proposals.active_delegation("PMO-DEPUTY") is None
        decided = proposals.decide(entry["id"], REJECTED, "PMO-DEPUTY", ROLE_PMO, comment="x")
        assert not decided.get("on_behalf_of")

    def test_a_project_manager_never_decides(self, at):
        entry = _submit(requester="Mara Lenz")
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.decide(entry["id"], REJECTED, "Rowan Ellis", ROLE_PM, comment="x")

    def test_the_admin_never_gets_approval_rights(self, at):
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.grant_delegation("SYSTEM-ADMIN", ROLE_ADMIN, "PMO-DEPUTY", clock.today(), clock.today(), "x")


class TestDeputyWrites:
    @pytest.fixture
    def source(self, tmp_path):
        path = tmp_path / "portfolio.json"
        path.write_text(json.dumps(DATA), encoding="utf-8")
        return path

    def test_the_deputy_writes_the_reserved_change_set(self, source, at):
        proposals.grant_delegation("PMO-ADMIN", ROLE_PMO, "PMO-DEPUTY", date(2029, 6, 1), date(2029, 6, 7), "vacation")
        entry = _submit()
        proposals.reserve_apply(entry["id"], "PMO-DEPUTY", ROLE_PMO, "op-deputy00001")
        result = JsonPortfolioAdapter(source).apply_changes(entry["changes"], user="PMO-DEPUTY", role=ROLE_PMO,
                                                            proposal_id=entry["id"], operation_id="op-deputy00001")
        proposals.decide(entry["id"], APPROVED, "PMO-DEPUTY", ROLE_PMO, snapshot_id=result.snapshot_id,
                         operation_id="op-deputy00001")
        assert json.loads(source.read_text(encoding="utf-8"))["Projects"][0]["budget_cost_eur"] == 720000
        assert proposals.get_proposal(entry["id"])["on_behalf_of"] == "PMO-ADMIN"

    def test_a_project_manager_never_writes_a_foreign_proposal(self, source, at):
        entry = _submit()
        with pytest.raises(WriteNotAuthorizedError):
            JsonPortfolioAdapter(source).apply_changes(entry["changes"], user="Rowan Ellis", role=ROLE_PM,
                                                       proposal_id=entry["id"])
        assert proposals.get_proposal(entry["id"])["status"] == SUBMITTED
