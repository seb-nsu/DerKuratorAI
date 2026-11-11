"""Scenario memory (continued scenarios), saved scenarios / A/B comparison helpers, project cockpit data."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core import scenario_store
from app.core.scenario_store import ScenarioStoreError, save_scenario
from app.core.security import ROLE_PM, ROLE_PMO
from app.crew.pipeline import run_steering_pipeline, scenario_memory_md
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.views import projects, whatifs

NAMES = {"P-1001": "Alpha Platform", "P-1002": "Beta Billing", "P-1003": "Gamma Governance"}


@pytest.fixture
def no_graph(monkeypatch):
    """Graph tools fail in a controlled way – these tests never build the real dependency graph."""
    from app.crew.tools import graph_tools

    def unavailable():
        raise graph_tools.GraphUnavailable("Graph disabled in test")

    monkeypatch.setattr(graph_tools, "get_graph", unavailable)


def _cut(pid, pct):
    return build_guided_scenario("budget_change", pid, NAMES, value=pct, is_perc=True, direction="decrease", cost_type="total")


# =========================================================================== scenario memory
class TestScenarioMemory:
    def test_second_step_builds_on_the_manipulated_data(self, dataset, no_graph):
        step1 = run_steering_pipeline(_cut("P-1002", 10).request, dataset, ROLE_PMO, "PMO", plan=_cut("P-1002", 10).plan)
        assert step1.scenario_state["turns"] == [_cut("P-1002", 10).request]
        budget_after_step1 = next(c["new"] for c in step1.proposed_changes if c["field"] == "budget_cost_eur")
        assert budget_after_step1 == 720000.0

        # "... and now also cut P-1002 by another 10 %" -> must start from 720k, not from the source's 800k
        step2 = run_steering_pipeline(_cut("P-1002", 10).request, dataset, ROLE_PMO, "PMO", plan=_cut("P-1002", 10).plan,
                                      base_state=step1.scenario_state)
        budgets = [c for c in step2.proposed_changes if c["field"] == "budget_cost_eur"]
        assert [c["new"] for c in budgets] == [720000.0, 648000.0]  # carried over + new step
        assert budgets[1]["old"] == 720000.0
        assert len(step2.scenario_state["turns"]) == 2
        assert any("Step 2 of a what-if" in w for w in step2.warnings)

    def test_verdict_covers_the_whole_scenario(self, dataset, no_graph):
        # step 1 pushes P-1002 into a forecast overrun (NO-GO); step 2 is harmless – the verdict must stay NO-GO
        step1 = run_steering_pipeline(_cut("P-1002", 20).request, dataset, ROLE_PMO, "PMO", plan=_cut("P-1002", 20).plan)
        assert step1.verdict.decision == "NO-GO"
        step2 = run_steering_pipeline(_cut("P-1001", 1).request, dataset, ROLE_PMO, "PMO", plan=_cut("P-1001", 1).plan,
                                      base_state=step1.scenario_state)
        assert step2.verdict.decision == "NO-GO" and "P-1002" in step2.verdict.reasons[1]
        assert {c["project_id"] for c in step2.proposed_changes} == {"P-1001", "P-1002"}

    def test_fresh_request_without_base_state_starts_over(self, dataset, no_graph):
        step1 = run_steering_pipeline(_cut("P-1002", 10).request, dataset, ROLE_PMO, "PMO", plan=_cut("P-1002", 10).plan)
        fresh = run_steering_pipeline(_cut("P-1002", 10).request, dataset, ROLE_PMO, "PMO", plan=_cut("P-1002", 10).plan)
        assert fresh.proposed_changes[0]["old"] == step1.proposed_changes[0]["old"] == 800000

    def test_memory_text_for_the_agents(self):
        md = scenario_memory_md(["Cut A by 10 %"], [{"project_id": "P-1", "field": "budget_cost_eur", "old": 1, "new": 2,
                                                     "source_tool": "t"}])
        assert "SCENARIO SO FAR" in md and "1. Cut A by 10 %" in md and "P-1 budget_cost_eur: 1 -> 2" in md

    def test_replay_restores_fte_into_scenario_column(self, dataset):
        base = {"changes": [{"project_id": "P-1001", "table": "Resource_Plans", "field": "allocated_fte", "old": 2.0,
                             "new": 1.0, "source_tool": "simulate_resource_transfer", "scenario_field": "current_fte"}],
                "results": [], "facts": [], "turns": ["move 1 FTE"]}
        ctx.set_portfolio_context(dataset, {}, base_state=base)
        assert ctx.get_project("P-1001")["current_fte"] == 1.0
        assert ctx.get_ledger()[0]["field"] == "allocated_fte" and "carried_over" not in ctx.get_ledger()[0]


# =========================================================================== saved scenarios
CHANGES_A = [{"project_id": "P-1002", "table": "Projects", "field": "budget_cost_eur", "old": 800000, "new": 720000,
              "source_tool": "simulate_budget_change"}]
CHANGES_B = [{"project_id": "P-1001", "table": "Projects", "field": "state", "old": "Work in Progress", "new": "Canceled",
              "source_tool": "simulate_project_cancellation"}]


class TestScenarioStore:
    def test_save_list_visibility_and_delete(self):
        a = save_scenario("Scenario A – soft cut", "PMO-ADMIN", ROLE_PMO, ["cut"], {"label": "GO"}, CHANGES_A, {"P-1002": -80000})
        b = save_scenario("Scenario B – stop", "Priya Raman", ROLE_PM, ["stop"], {"label": "CONDITIONAL GO"}, CHANGES_B, {"P-1001": -182000})
        assert [s["id"] for s in scenario_store.list_scenarios(ROLE_PMO, "PMO-ADMIN")] == [b["id"], a["id"]]  # PMO sees all
        assert [s["id"] for s in scenario_store.list_scenarios(ROLE_PM, "Priya Raman")] == [b["id"]]  # PM: own only
        with pytest.raises(ScenarioStoreError, match="Only the owner or the PMO"):
            scenario_store.delete_scenario(a["id"], "Priya Raman", ROLE_PM)
        scenario_store.delete_scenario(a["id"], "PMO-ADMIN", ROLE_PMO)
        assert scenario_store.get_scenario(a["id"]) is None

    def test_validation(self):
        with pytest.raises(ScenarioStoreError, match="name"):
            save_scenario("  ", "u", ROLE_PMO, [], None, CHANGES_A, {})
        with pytest.raises(ScenarioStoreError, match="proposed changes"):
            save_scenario("Empty", "u", ROLE_PMO, [], None, [], {})
        save_scenario("Twice", "u", ROLE_PMO, [], None, CHANGES_A, {})
        with pytest.raises(ScenarioStoreError, match="already saved"):
            save_scenario("twice", "u", ROLE_PMO, [], None, CHANGES_A, {})


# =========================================================================== executive helpers
class TestExecutiveHelpers:
    def test_budget_impact_from_changes_and_stops(self, dataset):
        impact = whatifs.budget_impact(CHANGES_A + CHANGES_B + [
            {"project_id": "P-1002", "field": "budget_cost_eur", "old": 720000, "new": 700000},  # chained step
            {"project_id": "P-1003", "field": "state", "old": "Work in Progress", "new": "On Hold"},  # no budget effect
        ], dataset.projects)
        assert impact == {"P-1002": -100000.0, "P-1001": -182000.0}  # 800k -> 700k net; stop releases 420k − 238k

    def test_summary_compare_and_waterfall(self):
        a = {"name": "A", "verdict": {"label": "GO"}, "changes": CHANGES_A, "budget_impact": {"P-1002": -80000}, "turns": ["x"]}
        b = {"name": "B", "verdict": {"label": "NO-GO"}, "changes": CHANGES_A + CHANGES_B,
             "budget_impact": {"P-1002": -80000, "P-1001": -182000}, "turns": ["x", "y"]}
        sa, sb = whatifs.scenario_figures(a), whatifs.scenario_figures(b)
        assert sa["net_budget_impact"] == -80000 and sb["projects_stopped"] == 1 and sb["steps"] == 2
        frame = whatifs.compare_frame(a, b)
        assert frame[frame["project"] == "P-1001"]["differs"].tolist() == ["≠"]
        assert frame[frame["project"] == "P-1002"]["differs"].tolist() == [""]
        fig = whatifs.waterfall_figure(b, "B")
        assert fig is not None and list(fig.data[0].x) == ["P-1001", "P-1002", "Net impact"]
        assert whatifs.waterfall_figure({"budget_impact": {}}, "x") is None

    def test_cockpit_frames(self, dataset):
        dataset.dependencies = pd.DataFrame([
            {"successor_project_number": "P-1002", "predecessor_project_number": "P-1001", "dependency_type": "Hard",
             "status": "At Risk", "required_by_date": pd.Timestamp("2030-05-01"), "description": "API"},
            {"successor_project_number": "P-1001", "predecessor_project_number": "P-9999", "dependency_type": "Soft",
             "status": "On Track", "required_by_date": pd.Timestamp("2029-01-01"), "description": "secret"},
        ])
        project = dataset.project_view().iloc[0].to_dict()
        deadlines = projects.cockpit_deadlines("P-1001", project, dataset, today=pd.Timestamp("2030-06-01").date())
        assert deadlines["type"].tolist() == ["Needs from", "Delivers to", "Project end"]
        assert deadlines.iloc[0]["item"] == "Restricted project" and deadlines.iloc[1]["days"] == -31
        incoming, outgoing = projects.cockpit_dependencies("P-1001", dataset)
        assert incoming.iloc[0]["description"] == projects.RESTRICTED_NOTE and outgoing.iloc[0]["project"].startswith("P-1002")
        assert incoming.iloc[0]["type"] == "–" and "P-9999" not in incoming.to_string()
        curve = projects.cockpit_cost_curve("P-1001", dataset)
        assert list(curve.columns) == ["month", "Planned (cumulative)", "Actual (cumulative)"] and len(curve) == 1
        assert curve.iloc[0]["Planned (cumulative)"] == 420000 and curve.iloc[0]["Actual (cumulative)"] == 238000
