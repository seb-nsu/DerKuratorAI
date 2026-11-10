"""Change requests naming a project the system cannot recognise: ANALYSIS INCOMPLETE with a request to restate
it (incl. the closest visible project names) – never GO, NO-GO or NO DECISION REQUIRED, and nothing is written."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset
from app.crew import pipeline
from app.crew.tasks import IntentPlan, SubIntent
from app.crew.tools import graph_tools
from app.data_adapters import JsonPortfolioAdapter
from app.graph.portfolio_graph import build_graph

SOURCE = Path(__file__).resolve().parents[1] / "data" / "DerKurator_Demo_Portfolio.json"
ASK_BACK = ("No project you have access to was recognised in the request. Please name the project by its name or ID "
            "(e.g. PRJ-DEMO-1002).")
INCOMPLETE = "ANALYSIS INCOMPLETE"
PM = "Priya Raman"  # owns General Ledger Consolidation and Laptop Refresh Programme


@pytest.fixture(scope="module")
def portfolio(tmp_path_factory):
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    dataset = JsonPortfolioAdapter(SOURCE).load_dataset()
    return dataset, build_graph(dataset)


@pytest.fixture
def full(portfolio, monkeypatch):
    dataset, graph = portfolio
    monkeypatch.setattr(graph_tools, "get_graph", lambda: graph)
    return dataset


def _run(dataset, request, role=ROLE_PMO, user="PMO-ADMIN"):
    view = apply_rls_to_dataset(dataset, role, user)
    return pipeline.run_steering_pipeline(request, view, role, user)


def _suggestions(text: str) -> list[str]:
    m = re.search(r"Closest matches: ([^\n]*?)\.(?:\s|$)", text)
    return [s.strip() for s in m.group(1).split(",")] if m else []


class TestUnknownProjectOffline:
    @pytest.mark.parametrize("request_text", ["Cut the budget of Unicorn Project by 5 %", "Stop the Unicorn initiative",
                                              "Put the Unicorn initiative on hold",
                                              "Delay the Unicorn initiative by 3 months"])
    def test_change_request_without_known_project_is_incomplete(self, full, request_text):
        result = _run(full, request_text)
        assert result.verdict.label == INCOMPLETE
        assert ASK_BACK in result.verdict.reasons[1] and ASK_BACK in result.decision_paper
        assert result.proposed_changes == []  # nothing to approve, nothing written

    def test_misspelled_project_is_suggested(self, full):
        result = _run(full, "Cut the budget of Genral Ledgr Konsolidaton by 5 %")
        assert result.verdict.label == INCOMPLETE
        suggestions = _suggestions(result.verdict.reasons[1])
        assert suggestions[0] == "General Ledger Consolidation" and len(suggestions) <= 3

    def test_pm_only_sees_own_project_names_as_suggestions(self, full):
        own = set(apply_rls_to_dataset(full, ROLE_PM, PM).projects["project_name"])
        foreign = set(full.projects["project_name"]) - own
        result = _run(full, "Cut the budget of Unicorn Project by 5 %", ROLE_PM, PM)
        assert result.verdict.label == INCOMPLETE
        assert set(_suggestions(result.verdict.reasons[1])) <= own
        assert not any(name in result.decision_paper for name in foreign)  # no foreign project name leaks

    def test_foreign_project_id_keeps_its_existing_message(self, full):
        result = _run(full, "Cut the budget of PRJ-DEMO-1011 by 5 %", ROLE_PM, PM)
        assert result.verdict.decision == "ANALYSIS INCOMPLETE"
        assert "not found or no permission" in result.verdict.reasons[1] and ASK_BACK not in result.verdict.reasons[1]


class TestUnchangedBehaviour:
    def test_analysis_question_needs_no_decision(self, full):
        assert _run(full, "Where do we have team bottlenecks?").verdict.label == "NO DECISION REQUIRED"

    @pytest.mark.parametrize("request_text", ["Cut the budget of General Ledger Consolidation by 20 %",
                                              "Kürze das Budget von General Ledger Consolidation um 20 %"])
    def test_known_project_still_gets_its_verdict(self, full, request_text):
        assert _run(full, request_text).verdict.label == "NO-GO"

    def test_portfolio_cut_needs_no_project_name(self, full):
        result = _run(full, "Save €500,000")
        assert result.verdict.decision == "CONDITIONAL GO" and ASK_BACK not in result.decision_paper

    @pytest.mark.parametrize("request_text", ["Where do we have team bottlenecks?",
                                              "Which clusters carry systemic risk?"])
    def test_analysis_questions_are_not_change_requests(self, full, request_text):
        assert not pipeline.is_unresolved_change_request(request_text, full.projects)


class TestUnknownProjectLLMMode:
    """The orchestrator's plan is the LLM's – the rule must hold whatever it returns."""

    @pytest.mark.parametrize("llm_plan", [
        IntentPlan(summary="analysis only", sub_intents=[SubIntent(intent_type="graph_analysis",
                                                                   description="cluster_risk")],
                   requires_graph_analysis=True),
        IntentPlan(summary="simulation without project", sub_intents=[SubIntent(intent_type="simulation",
                                                                                description="budget_change")],
                   requires_simulation=True),
    ], ids=["plan-as-analysis", "plan-without-project"])
    def test_unknown_project_is_incomplete_in_agent_mode(self, dataset, monkeypatch, llm_plan):
        from app.crew import agents, tasks
        from app.llm import agent as llm_agent

        captured: dict = {"simulation_tasks": 0}

        class FakeCrew:
            def __init__(self, agents=None, tasks=None, **kwargs):
                self.tasks = tasks

            def kickoff(self):
                kind = self.tasks[0]["kind"]
                if kind == "orchestrator":
                    return SimpleNamespace(pydantic=llm_plan, raw=llm_plan.model_dump_json())
                if kind == "advisory":
                    return SimpleNamespace(raw="# Executive Decision Paper\n**Request:** x | **Recommendation:** GO\n")
                return SimpleNamespace(raw="graph result", tasks_output=[SimpleNamespace(raw="graph result")])

        def fake_simulation_task(*args, **kwargs):
            captured["simulation_tasks"] += 1
            return {"kind": "simulation"}

        def fake_advisory(agent, request, plan, specialist_results, steward_md, role, verdict):
            captured["verdict"] = verdict
            return {"kind": "advisory"}

        monkeypatch.setattr(llm_agent, "Crew", FakeCrew)
        for name in ("build_orchestrator_agent", "build_simulator_agent", "build_analyst_agent", "build_advisory_agent"):
            monkeypatch.setattr(agents, name, lambda *a, **k: object())
        monkeypatch.setattr(tasks, "create_orchestration_task", lambda *a, **k: {"kind": "orchestrator"})
        monkeypatch.setattr(tasks, "create_simulation_task", fake_simulation_task)
        monkeypatch.setattr(tasks, "create_analysis_task", lambda *a, **k: {"kind": "graph"})
        monkeypatch.setattr(tasks, "create_advisory_task", fake_advisory)
        monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))

        result = pipeline.run_steering_pipeline("Cut the budget of Unicorn Project by 5 %", dataset, ROLE_PMO, "PMO")

        assert result.mode == "agents"
        assert captured["verdict"].label == INCOMPLETE and result.verdict.label == INCOMPLETE
        assert ASK_BACK in result.verdict.reasons[1]
        assert captured["simulation_tasks"] == 0  # the Simulator never guesses a project
        assert result.proposed_changes == []
