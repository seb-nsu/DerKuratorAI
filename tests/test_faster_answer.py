"""Verdict first, decision paper streamed, figures written by the code – proven offline with a fake crew and a
fake stream (no network, no keys). The verdict, the changes and every number are the same as without the callbacks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.formatting import field_label
from app.core.runtime_config import get_runtime_config, update_runtime_config
from app.core.security import ROLE_PMO
from app.crew import paper, pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tasks import IntentPlan
from app.crew.tools import simulator_tools as sim

NAMES = {"P-1001": "Alpha Platform", "P-1002": "Beta Billing", "P-1003": "Gamma Governance"}
LLM_PAPER = ("# Executive Decision Paper\n**Request:** x | **Date:** today | **Recommendation:** NO-GO\n\n"
             "## 1. Management Summary\n- The cut leaves a gap.\n\n"
             "## 3. Scenario Analysis (quantitative)\n| invented | table |\n\n"
             "## 5. Recommendation & Conditions\n- Close the gap (PMO, next week).\n")


@pytest.fixture(autouse=True)
def no_graph(monkeypatch):
    """Graph tools fail in a controlled way – these tests never build the real dependency graph."""
    from app.crew.tools import graph_tools

    def unavailable():
        raise graph_tools.GraphUnavailable("Graph disabled in test")

    monkeypatch.setattr(graph_tools, "get_graph", unavailable)


def _cut(value: int = 20):
    return build_guided_scenario("budget_change", "P-1002", NAMES, value=value, is_perc=True, direction="decrease")


@pytest.fixture
def crew(monkeypatch):
    """LLM agent mode with a fake crew: the Simulator calls the real tool, the Advisory returns LLM_PAPER."""
    from app.crew import agents, tasks
    from app.llm import agent as llm_agent

    log: list[str] = []
    captured: dict = {}

    class FakeCrew:
        def __init__(self, agents=None, tasks=None, **kwargs):
            self.tasks = tasks

        def kickoff(self):
            if self.tasks[0]["kind"] == "advisory":
                log.append("advisory")
                return SimpleNamespace(raw=LLM_PAPER)
            if self.tasks[0]["kind"] != "assessment":  # a guided plan already ran deterministically
                sim.budget_change("P-1002", -20, is_perc=True)  # the Simulator's tool call (recorded in the context)
            return SimpleNamespace(raw="budget_change: gap above threshold.",
                                   tasks_output=[SimpleNamespace(raw="budget_change: gap above threshold.")])

    def fake_advisory(agent, request, plan, specialist_results, steward_md, role, verdict):
        captured["evidence"] = specialist_results
        return {"kind": "advisory"}

    monkeypatch.setattr(llm_agent, "Crew", FakeCrew)
    for name in ("build_orchestrator_agent", "build_simulator_agent", "build_analyst_agent", "build_advisory_agent"):
        monkeypatch.setattr(agents, name, lambda *a, **k: object())
    monkeypatch.setattr(tasks, "create_simulation_task", lambda *a, **k: {"kind": "simulation"})
    monkeypatch.setattr(tasks, "create_assessment_task", lambda *a, **k: {"kind": "assessment"})
    monkeypatch.setattr(tasks, "create_analysis_task", lambda *a, **k: {"kind": "graph"})
    monkeypatch.setattr(tasks, "create_advisory_task", fake_advisory)
    monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))
    return SimpleNamespace(log=log, captured=captured)


class TestVerdictFirst:
    def test_offline_callback_once_with_the_final_verdict(self, dataset):
        previews = []
        result = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan,
                                                on_verdict=previews.append)
        assert len(previews) == 1 and previews[0]["mode"] == "offline"
        assert previews[0]["verdict"].label == result.verdict.label
        assert previews[0]["proposed_changes"] == result.proposed_changes
        assert result.verdict_s is not None and result.verdict_s <= result.duration_s

    def test_without_callback_the_result_is_unchanged(self, dataset):
        plain = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan)
        with_cb = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan,
                                                 on_verdict=lambda p: None)
        def changes(result):  # the ledger timestamp is the wall clock of each run, not part of the result
            return [{k: v for k, v in c.items() if k != "timestamp"} for c in result.proposed_changes]

        assert plain.verdict.label == with_cb.verdict.label and changes(plain) == changes(with_cb)
        assert plain.decision_paper == with_cb.decision_paper

    def test_agent_callback_comes_before_the_advisory(self, dataset, crew):
        def on_verdict(preview):
            crew.log.append(f"verdict:{preview['verdict'].label}")

        result = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan,
                                                on_verdict=on_verdict)
        assert result.mode == "agents"
        assert crew.log == [f"verdict:{result.verdict.label}", "advisory"]

    def test_a_failing_display_never_stops_the_analysis(self, dataset):
        def broken(preview):
            raise RuntimeError("UI gone")

        result = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan,
                                                on_verdict=broken)
        assert result.verdict.decision == "NO-GO"


class TestFiguresFromTheCode:
    def test_paper_has_all_sections_in_order_and_code_figures(self, dataset, crew):
        result = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan)
        paper = result.decision_paper
        positions = [paper.index(f"## {n}.") for n in (1, 3, 5, 7)]
        assert positions == sorted(positions)
        assert "| invented | table |" not in paper  # the LLM's own figures are replaced
        assert "| Project | Change | Before | After | Δ |" in paper
        assert all(f"| {c['project_id']} | {field_label(c['field'])} |" in paper for c in result.proposed_changes)
        assert f"**Recommendation:** {result.verdict.label}" in paper

    def test_advisory_gets_the_tool_results_verbatim(self, dataset, crew):
        pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan)
        evidence = crew.captured["evidence"]
        assert "RESULT simulate_budget_change" in evidence  # from the context, not from the Simulator's prose
        assert "Specialist assessment:" in evidence

    def test_paper_without_sections_keeps_its_text(self):
        paper = pipeline.assemble_paper("# Paper\n**Recommendation:** GO\nfree text", "## 3. S\nx", "## 7. N\ny")
        assert paper.startswith("# Paper\n**Recommendation:** GO\nfree text")
        assert paper.index("## 3. S") < paper.index("## 7. N")

    def test_scenario_section_lists_before_after_delta(self):
        md = pipeline.scenario_section_md([{"project_id": "P-1", "field": "budget_cost_eur", "old": 1_850_000,
                                            "new": 1_480_000}], ["RESULT simulate_budget_change: cut\nmore"])
        assert "| P-1 | Approved budget (€) | €1,850,000 | €1,480,000 | −€370,000 |" in md
        assert "- RESULT simulate_budget_change: cut" in md and "more" not in md


class TestStreaming:
    def test_a_split_name_is_never_shown_unmasked(self, monkeypatch):
        from tests.llm_fakes import FakeClient, install

        chunks = ["## 1. Summary\nOwner: Pri", "ya Raman approves", " the cut.\nDone"]
        install(monkeypatch, FakeClient(chunks=chunks))
        monkeypatch.setattr(paper, "mask_pii_data", lambda text: text.replace("Priya Raman", "[PM_MASKED]"))
        shown: list[str] = []
        raw = paper.stream_advisory("describe", "main", shown.append)
        assert raw == "".join(chunks)
        assert shown and all("Priya Raman" not in s and "Tay" not in s.replace("[PM_MASKED]", "") for s in shown)
        assert shown[-1].endswith("Done") and "[PM_MASKED] approves the cut." in shown[-1]

    def test_streamed_paper_is_assembled_like_the_task_paper(self, dataset, crew, monkeypatch):
        seen: list[str] = []

        def fake_stream(description, variant, on_paper):
            assert "FINAL VERDICT" in description
            on_paper("## 1. Management Summary\n")
            return LLM_PAPER

        monkeypatch.setattr(pipeline, "stream_advisory", fake_stream)
        result = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan,
                                                on_paper=seen.append)
        assert seen == ["## 1. Management Summary\n"] and "advisory" not in crew.log  # no second, non-streamed call
        assert "| Project | Change | Before | After | Δ |" in result.decision_paper
        assert result.verdict.decision == "NO-GO"

    def test_stream_error_falls_back_to_the_task(self, dataset, crew, monkeypatch):
        seen: list[str] = []

        def broken_stream(description, variant, on_paper):
            on_paper("partial")
            raise ConnectionError("stream dropped")

        monkeypatch.setattr(pipeline, "stream_advisory", broken_stream)
        result = pipeline.run_steering_pipeline(_cut().request, dataset, ROLE_PMO, "PMO", plan=_cut().plan,
                                                on_paper=seen.append)
        assert seen == ["partial", ""] and crew.log == ["advisory"]
        assert result.mode == "agents" and result.verdict.decision == "NO-GO"


class TestAgentLimitsAndOutputs:
    def test_every_agent_has_a_time_limit(self):
        from app.crew import agents

        update_runtime_config(openai_api_key="sk-test",
                              agents={a: {"main": "gpt-4o"} for a in ("orchestrator", "analyst", "simulator", "advisory")})
        for role, build in (("orchestrator", agents.build_orchestrator_agent), ("simulator", agents.build_simulator_agent),
                            ("analyst", agents.build_analyst_agent), ("advisory", agents.build_advisory_agent)):
            assert build().max_execution_time == agents.AGENT_TIME_LIMITS_S[role]
        assert agents.build_simulator_agent().max_iter == 10  # step limit unchanged (multi-part requests)

    def test_specialists_do_not_repeat_tool_results(self):
        from app.crew.tasks import create_analysis_task, create_simulation_task

        plan = IntentPlan(summary="s")
        for task in (create_simulation_task(None, "req", plan, "table"), create_analysis_task(None, "req", plan, "table")):
            assert "Do not repeat the tool results" in task.expected_output

    def test_advisory_writes_only_the_explanation(self):
        from app.crew.tasks import advisory_task_description
        from app.crew.verdict import compute_verdict

        text = advisory_task_description("req", IntentPlan(summary="s"), "RESULT", "steward", "PMO",
                                         compute_verdict(["RESULT"], [], []))
        assert "## 3." not in text and "## 7." not in text and "## 5. Recommendation & Conditions" in text
        assert "do not write them" in text
