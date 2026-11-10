"""A proposal holds the net change of a scenario, a resource-only change set is a hand-over, and an applied approval
can be taken back as its own four-eyes proposal (conflicts with later changes block it)."""
from __future__ import annotations

import json

import pytest

from app.core import proposals
from app.core.proposals import APPROVED, ProposalError
from app.core.security import ROLE_PM, ROLE_PMO
from app.crew.tools import context as ctx
from app.crew.tools import simulator_tools as sim
from app.data_adapters import JsonPortfolioAdapter
from app.data_adapters.base import is_manual_change, net_changes


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "portfolio.json"
    data = {"Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                          "budget_cost_eur": 800000, "planned_end_date": "2030-06-30", "state": "Open"}],
            "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _change(field, old, new, pid="P-1"):
    return {"project_id": pid, "table": "Projects", "field": field, "old": old, "new": new, "source_tool": "test"}


def _budget(path):
    return json.loads(path.read_text(encoding="utf-8"))["Projects"][0]["budget_cost_eur"]


def _approved(adapter, changes, requester="Priya Raman", by="PMO-A"):
    entry = proposals.submit_proposal(requester, ROLE_PM, "Cut", "", "GO", changes)
    result = adapter.apply_changes(entry["changes"], user=by, role=ROLE_PMO, proposal_id=entry["id"])
    proposals.decide(entry["id"], APPROVED, by, ROLE_PMO, snapshot_id=result.snapshot_id)
    return proposals.get_proposal(entry["id"])


class TestNetChange:
    def test_three_steps_give_one_change_per_field(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        for pct in (-20, 25, -10):
            sim.budget_change("P-1001", pct, is_perc=True)
        ledger = ctx.get_ledger()
        net = net_changes(ledger)
        assert len(ledger) > len(net)
        assert len({(c["project_id"], c["field"]) for c in net}) == len(net)
        budget = next(c for c in net if c["field"] == "budget_cost_eur")
        assert budget["old"] == 420000 and budget["new"] == 378000  # 420k × 0.8 × 1.25 × 0.9

    def test_a_field_back_at_its_start_drops_out(self):
        assert net_changes([_change("budget_cost_eur", 800000, 700000), _change("budget_cost_eur", 700000, 800000.0),
                            _change("state", "Open", "On Hold")]) == [_change("state", "Open", "On Hold")]

    def test_proposals_store_the_net_change(self):
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "r", "", "GO",
                                          [_change("budget_cost_eur", 1, 2), _change("budget_cost_eur", 2, 3)])
        assert [(c["old"], c["new"]) for c in entry["changes"]] == [(1, 3)]
        with pytest.raises(ProposalError, match="at least one change"):
            proposals.submit_proposal("Priya Raman", ROLE_PM, "r", "", "GO",
                                      [_change("budget_cost_eur", 1, 2), _change("budget_cost_eur", 2, 1)])

    def test_resource_changes_are_manual(self):
        assert is_manual_change({"table": "Resource_Plans", "field": "allocated_fte"})
        assert is_manual_change({"table": "Projects", "field": "project_manager"})
        assert not is_manual_change(_change("budget_cost_eur", 1, 2))


class TestRevertApproval:
    def test_revert_is_its_own_four_eyes_proposal(self, source):
        adapter = JsonPortfolioAdapter(source)
        original = _approved(adapter, [_change("budget_cost_eur", 800000, 700000)])
        revert = proposals.submit_revert(original["id"], "PMO-B", ROLE_PMO, adapter)
        assert revert["reverts"] == original["id"] and revert["requester"] == "PMO-B"
        assert [(c["field"], c["old"], c["new"]) for c in revert["changes"]] == [("budget_cost_eur", 700000, 800000)]
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.decide(revert["id"], APPROVED, "PMO-B", ROLE_PMO)
        result = adapter.apply_changes(revert["changes"], user="PMO-A", role=ROLE_PMO, proposal_id=revert["id"])
        proposals.decide(revert["id"], APPROVED, "PMO-A", ROLE_PMO, snapshot_id=result.snapshot_id)
        assert _budget(source) == 800000
        with pytest.raises(ProposalError, match="already being reverted"):
            proposals.submit_revert(original["id"], "PMO-C", ROLE_PMO, adapter)

    def test_later_changes_block_the_revert(self, source):
        adapter = JsonPortfolioAdapter(source)
        first = _approved(adapter, [_change("budget_cost_eur", 800000, 700000)])
        _approved(adapter, [_change("budget_cost_eur", 700000, 650000)])
        with pytest.raises(ProposalError, match="Changed since the approval: P-1 budget_cost_eur"):
            proposals.submit_revert(first["id"], "PMO-B", ROLE_PMO, adapter)
        assert proposals.revert_of(first["id"]) is None

    def test_only_an_applied_approval_by_the_pmo(self, source):
        adapter = JsonPortfolioAdapter(source)
        approved = _approved(adapter, [_change("budget_cost_eur", 800000, 700000)])
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.submit_revert(approved["id"], "Priya Raman", ROLE_PM, adapter)
        open_one = proposals.submit_proposal("Priya Raman", ROLE_PM, "r", "", "GO", [_change("state", "Open", "On Hold")])
        with pytest.raises(ProposalError, match="nothing to revert"):
            proposals.submit_revert(open_one["id"], "PMO-B", ROLE_PMO, adapter)
