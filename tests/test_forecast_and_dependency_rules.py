"""A stopped project is not checked against its old forecast; "depend on" is a graph question; a domino only raises a
condition for a simulated change – a pure dependency question needs no decision."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset
from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import graph_tools
from app.data_adapters import JsonPortfolioAdapter
from app.graph.portfolio_graph import build_graph

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
ERP, BILLING, CLOUD = "PRJ-DEMO-1002", "PRJ-DEMO-1007", "PRJ-DEMO-1001"
AS_OF = date(2026, 10, 1)  # reference date of the demo portfolio's expected values (scripts/run_evals.EVAL_AS_OF)
DEPENDENCY_QUESTION = "Which projects depend on General Ledger Consolidation?"


@pytest.fixture(scope="module")
def portfolio(tmp_path_factory):
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    dataset = JsonPortfolioAdapter(SOURCE).load_dataset()
    return dataset, build_graph(dataset)


@pytest.fixture
def pmo(portfolio, monkeypatch):
    dataset, graph = portfolio
    monkeypatch.setattr(graph_tools, "get_graph", lambda: graph)
    return apply_rls_to_dataset(dataset, ROLE_PMO, "PMO-ADMIN")


def _names(view):
    return dict(zip(view.projects["project_number"], view.projects["project_name"], strict=True))


def _guided(view, action, project, target=None, **params):
    scenario = build_guided_scenario(action, project, _names(view), target=target, **params)
    return pipeline.run_steering_pipeline(scenario.request, view, ROLE_PMO, "PMO-ADMIN", plan=scenario.plan,
                                          as_of=AS_OF)


class TestStoppedProjectForecast:
    def test_stop_and_transfer_free_budget_is_not_checked_against_old_forecast(self, pmo):
        result = _guided(pmo, "cancel", BILLING, target=ERP)  # the Steering Console "stop + transfer" flow (case M2)
        assert result.verdict.decision == "CONDITIONAL GO"
        overruns = [f for f in result.scenario_state["facts"] if f["type"] in ("budget_overrun", "eac_missing")
                    and f["project"] == BILLING]
        assert overruns == []
        assert "Forecast not applicable – project stopped." in result.decision_paper

    def test_eval_case_m2_matches_its_expected_verdict(self, pmo):
        case = next(json.loads(line) for line in (ROOT / "evals" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
                    if json.loads(line)["id"] == "M2")
        plan = case["expected_plan"]
        assert _guided(pmo, "cancel", plan["project"], target=plan["target"]).verdict.decision == case["expected_verdict"]

    def test_transfer_from_a_running_project_is_still_checked(self, pmo):
        result = _guided(pmo, "budget_transfer", BILLING, target=ERP, amount=400000)  # 1.32M -> 0.92M, EAC 1.32M
        assert result.verdict.label == "NO-GO"
        assert BILLING in result.verdict.reasons[1]

    @pytest.mark.parametrize("pct, label", [(20, "NO-GO"), (10, "CONDITIONAL GO")])
    def test_cuts_on_running_projects_unchanged(self, portfolio, monkeypatch, pct, label):
        dataset, graph = portfolio
        monkeypatch.setattr(graph_tools, "get_graph", lambda: graph)
        view = apply_rls_to_dataset(dataset, ROLE_PM, "Priya Raman")
        scenario = build_guided_scenario("budget_change", ERP, _names(view), value=pct, is_perc=True,
                                         direction="decrease")
        result = pipeline.run_steering_pipeline(scenario.request, view, ROLE_PM, "Priya Raman", plan=scenario.plan)
        assert result.verdict.label == label


class TestDependencyQuestions:
    @pytest.mark.parametrize("question", [DEPENDENCY_QUESTION, "What depends on General Ledger Consolidation?"])
    def test_depend_on_is_a_graph_question(self, pmo, question):
        plan = pipeline.heuristic_plan(question, pmo.projects)
        assert not plan.requires_simulation and plan.requires_graph_analysis

    def test_pure_dependency_question_needs_no_decision(self, pmo):
        result = pipeline.run_steering_pipeline(DEPENDENCY_QUESTION, pmo, ROLE_PMO, "PMO-ADMIN")
        assert result.verdict.label == "NO DECISION REQUIRED"
        assert "[AI ESTIMATE]" not in result.decision_paper
        assert BILLING in result.decision_paper  # the dependents are still listed

    def test_domino_still_conditions_a_simulated_change(self, pmo):
        result = _guided(pmo, "budget_change", ERP, value=10, is_perc=True, direction="decrease")  # eval case E3
        assert result.verdict.decision == "CONDITIONAL GO"
        assert any("2 dependent project(s)" in c for c in result.verdict.conditions)

    def test_successor_lateness_unchanged(self, pmo):
        result = _guided(pmo, "schedule", CLOUD, months_delta=3)  # eval case E5
        # the cost of delay raises the EAC (€432,600 to €746,189 vs. a budget of €420,000, 77.7 %), hence NO-GO;
        # the successor lateness is still found and reported
        assert result.verdict.label == "NO-GO"
        assert "77.7 %" in result.verdict.reasons[1]
        breach = next(f for f in result.scenario_state["facts"] if f["type"] == "deadline_breach")
        assert (breach["days_late_before"], breach["days_late"]) == (224, 315)
