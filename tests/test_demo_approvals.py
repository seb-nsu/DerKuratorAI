"""The four-eyes approval flows on the demo portfolio, as they are shown in a demo: a project manager's proposal
approved by the PMO and written, a PMO proposal approved by the second PMO, a NO-GO approved only with override and
reason, a rejection with reason, a revert of an approval, and the refused attempts (self-approval, self-rejection,
a project manager deciding). The change sets come from the offline pipeline on the demo data, not from fixtures."""
from __future__ import annotations

import json
import logging

import pytest

from app.core import proposals
from app.core.proposals import APPROVED, REJECTED, ProposalError
from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset
from app.crew.pipeline import run_steering_pipeline
from app.data_adapters import JsonPortfolioAdapter
from scripts import run_evals

PM, PMO, DEPUTY = "Priya Raman", "PMO-ADMIN", "PMO-DEPUTY"  # demo accounts (data/demo_users.json)
ERP = "PRJ-DEMO-1002"  # General Ledger Consolidation, managed by Priya Raman


@pytest.fixture
def demo():
    if not run_evals.PORTFOLIO.exists():
        pytest.skip("Demo portfolio not available")
    with run_evals.isolated_environment() as env:
        yield env


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.INFO, logger="derkurator.security")
    return lambda: [r.getMessage() for r in caplog.records if r.getMessage().startswith("SECURITY | ")]


def _budget(env, pid=ERP) -> float:
    projects = json.loads(env["source"].read_text(encoding="utf-8"))["Projects"]
    return next(p for p in projects if p["project_number"] == pid)["budget_cost_eur"]


def _propose(env, user, role, plan, request):
    """Runs the guided scenario on the demo data (RLS view of `user`) and submits its change set as a proposal."""
    ds = env["dataset"]
    names = dict(zip(ds.projects["project_number"], ds.projects["project_name"], strict=True))
    result = run_steering_pipeline(request, apply_rls_to_dataset(ds, role, user), role, user,
                                   plan=run_evals._guided_plan(plan, names), as_of=run_evals.EVAL_AS_OF)
    assert result.proposed_changes, result.warnings
    return proposals.submit_proposal(user, role, request, result.decision_paper, result.verdict.label,
                                     result.proposed_changes, verdict_record=result.verdict.to_dict())


def _cut(pct):
    return {"action": "budget_change", "project": ERP, "delta": -pct, "is_perc": True, "cost_type": "total"}


def _approve(env, entry, by, comment="", override=False):
    """The order of the approval inbox: validate the decision, compare-and-write with a snapshot, record it."""
    proposals.validate_decision(entry["id"], APPROVED, by, ROLE_PMO, comment=comment, override=override)
    written = JsonPortfolioAdapter(env["source"]).apply_changes(entry["changes"], user=by, role=ROLE_PMO,
                                                                proposal_id=entry["id"])
    return proposals.decide(entry["id"], APPROVED, by, ROLE_PMO, comment=comment, snapshot_id=written.snapshot_id,
                            override=override)


class TestApproved:
    def test_pm_proposal_is_approved_by_the_pmo_and_written(self, demo):
        before = _budget(demo)
        entry = _propose(demo, PM, ROLE_PM, _cut(10), "Cut General Ledger Consolidation by 10 %")
        assert entry["verdict"].startswith("CONDITIONAL GO")
        assert _budget(demo) == before  # a proposal writes nothing
        decided = _approve(demo, entry, PMO)
        assert decided["status"] == APPROVED and decided["decided_by"] == PMO and decided["override"] is False
        assert _budget(demo) == pytest.approx(before * 0.9)

    def test_a_pmo_proposal_is_approved_by_the_second_pmo(self, demo, events):
        entry = _propose(demo, DEPUTY, ROLE_PMO, _cut(10), "Cut General Ledger Consolidation by 10 %")
        with pytest.raises(ProposalError, match="Four-eyes"):
            _approve(demo, entry, DEPUTY)  # the requester never approves their own proposal
        assert any("reason=self_approval" in e for e in events())
        assert _approve(demo, entry, PMO)["status"] == APPROVED

    def test_no_go_is_approved_only_with_override_and_reason(self, demo):
        before = _budget(demo)
        entry = _propose(demo, PM, ROLE_PM, _cut(20), "Cut General Ledger Consolidation by 20 %")
        assert entry["verdict"] == "NO-GO"
        with pytest.raises(ProposalError, match="explicit override"):
            _approve(demo, entry, PMO)
        with pytest.raises(ProposalError, match="explicit override"):
            _approve(demo, entry, PMO, override=True)  # an override needs a reason
        assert _budget(demo) == before  # refused up front: nothing written
        decided = _approve(demo, entry, PMO, comment="Board decided on 2026-10-01", override=True)
        assert decided["override"] is True and decided["comment"] == "Board decided on 2026-10-01"
        assert _budget(demo) == pytest.approx(before * 0.8)


class TestDeclined:
    def test_pmo_rejects_with_a_reason_and_nothing_is_written(self, demo):
        before = _budget(demo)
        entry = _propose(demo, PM, ROLE_PM, _cut(10), "Cut General Ledger Consolidation by 10 %")
        with pytest.raises(ProposalError, match="reason"):
            proposals.decide(entry["id"], REJECTED, PMO, ROLE_PMO)
        decided = proposals.decide(entry["id"], REJECTED, PMO, ROLE_PMO, comment="Budget freeze until Q1")
        assert decided["status"] == REJECTED and proposals.get_proposal(entry["id"])["comment"] == "Budget freeze until Q1"
        assert _budget(demo) == before
        with pytest.raises(ProposalError, match="already rejected"):
            _approve(demo, entry, DEPUTY)  # a decided proposal stays decided

    def test_the_requester_cannot_reject_their_own_proposal(self, demo, events):
        entry = _propose(demo, DEPUTY, ROLE_PMO, _cut(10), "Cut General Ledger Consolidation by 10 %")
        with pytest.raises(ProposalError, match="a second PMO decides"):
            proposals.decide(entry["id"], REJECTED, DEPUTY, ROLE_PMO, comment="changed my mind")
        assert any("reason=self_rejection" in e for e in events())

    def test_a_project_manager_cannot_decide(self, demo, events):
        entry = _propose(demo, PM, ROLE_PM, _cut(10), "Cut General Ledger Consolidation by 10 %")
        for status, comment in ((APPROVED, ""), (REJECTED, "no")):
            with pytest.raises(ProposalError, match="Only the PMO"):
                proposals.decide(entry["id"], status, PM, ROLE_PM, comment=comment)
        assert sum("reason=not_approver" in e for e in events()) == 2
        assert proposals.get_proposal(entry["id"])["status"] == proposals.SUBMITTED


class TestReverted:
    def test_an_approval_is_taken_back_as_its_own_four_eyes_proposal(self, demo):
        before = _budget(demo)
        original = _approve(demo, _propose(demo, PM, ROLE_PM, _cut(10), "Cut General Ledger Consolidation by 10 %"), PMO)
        adapter = JsonPortfolioAdapter(demo["source"])
        revert = proposals.submit_revert(original["id"], DEPUTY, ROLE_PMO, adapter)
        assert revert["reverts"] == original["id"]
        with pytest.raises(ProposalError, match="Four-eyes"):
            _approve(demo, revert, DEPUTY)
        _approve(demo, revert, PMO)
        assert _budget(demo) == pytest.approx(before)
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.submit_revert(original["id"], PM, ROLE_PM, adapter)
