"""Project merge: the target takes over money, dates, plan rows and dependencies of the source, the source is closed,
and the whole change set is governed (four-eyes) down to the re-keyed rows."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core import proposals
from app.core.security import ROLE_PM, ROLE_PMO, is_governed_change
from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools.merge_tools import merge_projects
from app.crew.verdict import verdict_from_context
from app.data_adapters import JsonPortfolioAdapter
from app.data_adapters.base import WriteNotAuthorizedError
from tests.conftest import _project


@pytest.fixture
def merged(dataset):
    ctx.set_portfolio_context(dataset, {})
    return merge_projects("P-1002", "P-1001")


def _ledger(table="Projects"):
    return [c for c in ctx.get_ledger() if c.get("table", "Projects") == table]


class TestMergeTool:
    def test_money_moves_to_the_target_and_the_source_closes(self, merged):
        assert merged.startswith("RESULT simulate_merge | P-1002 (Beta Billing) is merged into P-1001")
        target = {c["field"]: c["new"] for c in _ledger() if c["project_id"] == "P-1001"}
        source = {c["field"]: c["new"] for c in _ledger() if c["project_id"] == "P-1002"}
        assert target["budget_cost_eur"] == 1_220_000 and target["actual_cost_eur"] == 538_000
        assert target["planned_benefit_eur"] == 1_710_000
        assert source["budget_cost_eur"] == 0.0 and source["state"] == "Closed Incomplete" and source["phase"] == "Closed"

    def test_rows_of_the_source_are_re_keyed(self, merged):
        moved = {(c["table"], c["key"]) for c in ctx.get_ledger() if c.get("field") == "project_number"}
        assert moved == {("Project_Tasks", "T-3"), ("Resource_Plans", "R-1"), ("Stakeholders", "S-2")}
        assert all(c["old"] == "P-1002" and c["new"] == "P-1001" for c in ctx.get_ledger()
                   if c.get("field") == "project_number")
        assert "Moved to P-1001: 1 tasks, 1 resource plan rows, 1 stakeholders" in merged

    def test_verdict_needs_a_re_baseline_and_the_board(self, merged):
        verdict = verdict_from_context()
        assert verdict.label == "CONDITIONAL GO"
        assert any("P-1002 is merged into P-1001 (combined budget €1,220,000" in r for r in verdict.reasons)
        assert any("Re-baseline P-1001 after the merge" in c for c in verdict.conditions)
        assert any("Agree the sponsor of the merged project P-1001" in c for c in verdict.conditions)

    def test_same_project_and_blocked_source_are_refused(self, dataset):
        ctx.set_portfolio_context(dataset, {"P-1003": "planning_incomplete"})
        assert "two different projects" in merge_projects("P-1001", "P-1001")
        assert "Data quality" in merge_projects("P-1003", "P-1001")

    def test_dependencies_are_re_pointed_and_a_link_between_both_stays(self, dataset_factory):
        ds = dataset_factory([_project(), _project(project_number="P-1002", project_name="Beta Billing"),
                              _project(project_number="P-1004", project_name="Delta Data")])
        deps = pd.DataFrame([
            {"dependency_number": "D-1", "predecessor_project_number": "P-1002", "successor_project_number": "P-1004",
             "dependency_type": "Finish-to-Start", "status": "Active", "required_by_date": "2031-01-01"},
            {"dependency_number": "D-2", "predecessor_project_number": "P-1002", "successor_project_number": "P-1001",
             "dependency_type": "Finish-to-Start", "status": "Active", "required_by_date": "2031-01-01"},
        ])
        import dataclasses

        ctx.set_portfolio_context(dataclasses.replace(ds, dependencies=deps), {})
        text = merge_projects("P-1002", "P-1001")
        changes = [(c["key"], c["field"], c["new"]) for c in _ledger("Dependencies")]
        assert changes == [("D-1", "predecessor_project_number", "P-1001")]
        assert "Dependency D-2 between P-1002 and P-1001 becomes internal" in text

    def test_cross_unit_merge_is_a_sponsor_condition(self, dataset_factory):
        ds = dataset_factory([_project(business_unit="Finance"),
                              _project(project_number="P-1002", project_name="Beta Billing", business_unit="Sales")])
        ctx.set_portfolio_context(ds, {})
        merge_projects("P-1002", "P-1001")
        assert any("between Finance and Sales" in c for c in verdict_from_context().conditions)


class TestGovernance:
    def test_re_keyed_rows_are_governed(self):
        assert is_governed_change({"table": "Project_Tasks", "key": "T-3", "field": "project_number"})
        assert is_governed_change({"table": "Dependencies", "key": "D-1", "field": "successor_project_number"})
        assert not is_governed_change({"table": "Risks", "key": "RK-1", "field": "mitigation"})

    def test_the_merge_is_written_only_as_an_approved_proposal(self, merged, tmp_path, dataset):
        tables = {"Projects": [_project(), _project(project_number="P-1002", project_name="Beta Billing",
                                                    project_manager="Rowan Ellis", budget_cost_eur=800000,
                                                    actual_cost_eur=300000)],
                  "Project_Tasks": [{"task_number": "T-3", "project_number": "P-1002", "state": "Open"}],
                  "Resource_Plans": [{"resource_plan_number": "R-1", "project_number": "P-1002", "month": "2025-01-01"}],
                  "Stakeholders": [{"stakeholder_number": "S-2", "project_number": "P-1002", "user": "Fatima Okafor"}],
                  "Risks": []}
        source = tmp_path / "portfolio.json"
        source.write_text(json.dumps(tables), encoding="utf-8")
        adapter = JsonPortfolioAdapter(source)
        changes = [c for c in ctx.get_ledger() if c["field"] in ("project_number", "state", "budget_cost_eur")]
        with pytest.raises(WriteNotAuthorizedError):
            adapter.apply_changes(changes, user="Priya Raman", role=ROLE_PM)
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "merge", "", "CONDITIONAL GO", changes)
        proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, "op-merge-0000001")
        adapter.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"],
                              operation_id="op-merge-0000001")
        stored = json.loads(source.read_text(encoding="utf-8"))
        assert [t["project_number"] for t in stored["Project_Tasks"]] == ["P-1001"]
        assert stored["Stakeholders"][0]["project_number"] == "P-1001"
        assert stored["Projects"][1]["state"] == "Closed Incomplete"


class TestParserAndBuilder:
    def test_merge_wording_is_a_merge_plan(self, dataset):
        plan = pipeline.heuristic_plan("Merge Beta Billing into Alpha Platform", dataset.projects)
        assert [i.description for i in plan.sub_intents if i.intent_type == "simulation"] == ["merge:P-1002->P-1001"]
        assert pipeline.is_change_request("Projekt Beta mit Alpha zusammenlegen")

    def test_guided_scenario(self):
        scenario = build_guided_scenario("merge", "P-1002", {"P-1002": "Beta", "P-1001": "Alpha"}, target="P-1001")
        assert scenario.request == "Merge P-1002 (Beta) into P-1001 (Alpha)"
        assert scenario.plan.sub_intents[0].project_ids == ["P-1002", "P-1001"]
        with pytest.raises(ValueError):
            build_guided_scenario("merge", "P-1002", {}, target="P-1002")
