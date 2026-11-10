"""Planning 3: the annual plan is submitted by the PMO and approved by the Portfolio Board only (four-eyes, one
operation id in the write journal); an approved demand becomes a project that every existing use case handles, with
the link in both directions."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from app.core import proposals
from app.core.security import ROLE_BOARD, ROLE_PM, ROLE_PMO, apply_rls_to_dataset
from app.crew.pipeline import run_steering_pipeline
from app.crew.tools.data_steward_tools import run_all_checks
from app.data_adapters import JsonPortfolioAdapter, WriteNotAuthorizedError
from app.planning import approval, store
from app.planning.scenario import ScenarioInput, run_scenario

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)


@pytest.fixture
def adapter(tmp_path):
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    path = tmp_path / "portfolio.json"
    shutil.copy(SOURCE, path)
    return JsonPortfolioAdapter(path)


def _draft(adapter, spec=None) -> str:
    ds = adapter.load_dataset()
    changes = store.save_changes(ds, run_scenario(ds, spec or ScenarioInput(), as_of=AS_OF), "PMO-ADMIN")
    adapter.apply_changes(changes, user="PMO-ADMIN", role=ROLE_PMO, reason="planning_scenario")
    return changes[0]["row"]["scenario_id"]


def _submitted(adapter) -> dict:
    scenario = _draft(adapter)
    return approval.submit_plan(adapter.load_dataset(), scenario, "PMO-ADMIN", ROLE_PMO, adapter, as_of=AS_OF)


class TestPlanApproval:
    def test_submit_records_the_scenario_and_waits_for_the_board(self, adapter):
        entry = _submitted(adapter)
        assert entry["kind"] == proposals.PLAN and entry["verdict"] == "CONDITIONAL GO"
        saved = store.saved_scenarios(adapter.load_dataset())
        assert saved.at[0, "scenario_status"] == "Submitted"
        with pytest.raises(approval.PlanError, match="already Submitted"):
            approval.submit_plan(adapter.load_dataset(), saved.at[0, "scenario_id"], "PMO-ADMIN", ROLE_PMO, adapter,
                                 as_of=AS_OF)

    def test_only_the_board_approves_the_plan(self, adapter):
        entry = _submitted(adapter)
        with pytest.raises(proposals.ProposalError, match="Portfolio Board"):
            proposals.validate_decision(entry["id"], proposals.APPROVED, "PMO-DEPUTY", ROLE_PMO)
        with pytest.raises(WriteNotAuthorizedError):  # not even with the proposal id
            adapter.apply_changes(entry["changes"], user="PMO-DEPUTY", role=ROLE_PMO, proposal_id=entry["id"])
        result = approval.approve_plan(entry["id"], "BOARD-CHAIR", ROLE_BOARD, adapter, comment="Approved in session")
        assert result.operation_id and result.applied == len(entry["changes"])
        decided = proposals.get_proposal(entry["id"])
        assert decided["status"] == proposals.APPROVED and decided["operation_id"] == result.operation_id
        after = adapter.load_dataset()
        plan = store.approved_plan(after)
        assert plan["scenario_id"] == "SCN-2027-001" and plan["approved_by"] == "BOARD-CHAIR"
        status = after.demands.set_index("demand_number")["demand_status"]
        assert status["DMD-2027-002"] == "Approved"  # mandatory, starts 2027
        assert status["DMD-2027-001"] == "Planned"  # deferred to 2028
        assert status["DMD-2027-007"] == "Deferred"  # out
        assert not store.items_of(after, "SCN-2027-001", "Approved").empty

    def test_the_board_decides_nothing_else_and_never_writes_directly(self, adapter):
        weights = proposals.submit_proposal("PMO-ADMIN", ROLE_PMO, "weights", "", None,
                                            [{"table": "Scoring_Model", "project_id": "", "key": "Urgency",
                                              "field": "weight", "old": 10.0, "new": 20.0}])
        with pytest.raises(proposals.ProposalError, match="only the annual plan"):
            proposals.validate_decision(weights["id"], proposals.APPROVED, "BOARD-CHAIR", ROLE_BOARD)
        with pytest.raises(WriteNotAuthorizedError):
            adapter.apply_changes([{"table": "Demands", "project_id": "DMD-2027-001", "key": "DMD-2027-001",
                                    "field": "demand_status", "new": "Approved"}], user="BOARD-CHAIR", role=ROLE_BOARD)

    def test_nobody_else_sets_a_demand_to_approved(self, adapter):
        change = {"table": "Demands", "project_id": "DMD-2027-001", "key": "DMD-2027-001", "field": "demand_status",
                  "old": "Assessed", "new": "Approved"}
        for user, role in (("Priya Raman", ROLE_PM), ("PMO-ADMIN", ROLE_PMO)):
            with pytest.raises(WriteNotAuthorizedError):
                adapter.apply_changes([change], user=user, role=role)

    def test_the_requester_board_member_cannot_approve_their_own_plan(self, adapter):
        scenario = _draft(adapter)
        entry = approval.submit_plan(adapter.load_dataset(), scenario, "BOARD-CHAIR", ROLE_PMO, adapter, as_of=AS_OF)
        with pytest.raises(proposals.ProposalError, match="own proposal"):
            approval.approve_plan(entry["id"], "BOARD-CHAIR", ROLE_BOARD, adapter)

    def test_approved_demands_are_committed_in_the_next_round(self, adapter):
        entry = _submitted(adapter)
        approval.approve_plan(entry["id"], "BOARD-CHAIR", ROLE_BOARD, adapter)
        nxt = run_scenario(adapter.load_dataset(), as_of=AS_OF).items.set_index("demand_number")
        assert nxt.at["DMD-2027-002", "reason"] == "Approved in an earlier plan"


class TestConversion:
    @pytest.fixture
    def approved(self, adapter):
        entry = _submitted(adapter)
        approval.approve_plan(entry["id"], "BOARD-CHAIR", ROLE_BOARD, adapter)
        return adapter

    def test_only_an_approved_demand_becomes_a_project(self, approved):
        with pytest.raises(approval.PlanError, match="Only an approved demand"):
            approval.conversion_changes(approved.load_dataset(), "DMD-2027-001", "PMO-ADMIN")

    def test_conversion_with_four_eyes_and_traceability(self, approved):
        ds = approved.load_dataset()
        entry = approval.submit_conversion(ds, "DMD-2027-002", "PMO-ADMIN", ROLE_PMO)
        pid = entry["changes"][0]["key"]
        with pytest.raises(WriteNotAuthorizedError):  # the first baseline is four-eyes
            approved.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO)
        approved.apply_changes(entry["changes"], user="PMO-DEPUTY", role=ROLE_PMO, proposal_id=entry["id"])
        after = approved.load_dataset()
        project = after.projects.set_index("project_number").loc[pid]
        assert project["project_name"] == "Digital Tax Reporting" and project["project_manager"] == "Priya Raman"
        plans = ds.demand_year_plans[ds.demand_year_plans["demand_number"] == "DMD-2027-002"]
        cost = after.cost_plans[after.cost_plans["project_number"] == pid]["planned_cost_eur"].sum()
        assert cost == pytest.approx(plans["capex_eur"].sum() + plans["opex_eur"].sum(), abs=1)
        assert not after.resource_plans[after.resource_plans["project_number"] == pid].empty
        assert not after.baselines[after.baselines["project_number"] == pid].empty
        assert approval.project_of(after, "DMD-2027-002") == pid and approval.demand_of(after, pid) == "DMD-2027-002"
        assert after.demands.set_index("demand_number").at["DMD-2027-002", "demand_status"] == "Converted"
        pm = apply_rls_to_dataset(after, ROLE_PM, "Priya Raman")
        assert pid in set(pm.projects["project_number"])  # the demand owner manages the project
        errors = [i for i in run_all_checks(after).issues if i.project_id == pid and i.severity == "ERROR"]
        assert errors == []
        for request in (f"Cut the budget of {pid} by 10 %", f"Delay {pid} by 2 months"):
            result = run_steering_pipeline(request, pm, ROLE_PM, "Priya Raman", pii_source=after, as_of=AS_OF)
            assert result.mode == "offline" and result.verdict is not None and result.proposed_changes
