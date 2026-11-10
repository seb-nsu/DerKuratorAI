"""Governance configuration (business rules) and the deterministic verdict engine."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.business_rules import BusinessRules
from app.core.env_config import config_issue, env_config_path
from app.core.runtime_config import (
    get_runtime_config,
    persist_runtime_config,
    reset_runtime_config,
    update_runtime_config,
)
from app.core.security import ROLE_PMO
from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import simulator_tools as sim
from app.crew.verdict import compute_verdict, enforce_verdict, verdict_from_context
from app.graph import portfolio_graph

NAMES = {"P-1001": "Alpha Platform", "P-1002": "Beta Billing", "P-1003": "Gamma Governance"}


# =========================================================================== governance configuration
class TestBusinessRulesConfig:
    def test_defaults_equal_former_constants(self):
        rules = get_runtime_config().business_rules
        assert (rules.fte_monthly_cost_eur, rules.exit_cost_rate_pct, rules.sprint_length_days,
                rules.max_budget_change_pct, rules.max_timeline_shift_months, rules.overload_alert_pct,
                rules.team_capacity_limit_pct) == (12500, 8, 14, 100, 36, 110, 100)

    def test_rules_are_persisted_per_environment_and_partial_files_merge(self):
        update_runtime_config(business_rules={"fte_monthly_cost_eur": 9800})
        persist_runtime_config("pmo")
        stored = json.loads(env_config_path("DEV").read_text(encoding="utf-8"))["business_rules"]
        assert stored["fte_monthly_cost_eur"] == 9800 and stored["exit_cost_rate_pct"] == 8
        env_config_path("DEV").write_text(json.dumps({"business_rules": {"exit_cost_rate_pct": 5}}), encoding="utf-8")
        rules = reset_runtime_config().business_rules
        assert rules.exit_cost_rate_pct == 5 and rules.fte_monthly_cost_eur == 12500

    def test_out_of_bounds_rules_are_rejected(self):
        with pytest.raises(ValueError, match="exit_cost_rate_pct"):
            update_runtime_config(business_rules={"exit_cost_rate_pct": 250})
        env_config_path("DEV").write_text(json.dumps({"business_rules": {"sprint_length_days": 0}}), encoding="utf-8")
        assert reset_runtime_config().business_rules == BusinessRules()  # invalid file: defaults and admin banner
        assert "business_rules" in config_issue("DEV")

    def test_assumptions_are_injected_into_agent_prompts(self):
        from app.crew import agents

        update_runtime_config(openai_api_key="sk-test", business_rules={"fte_monthly_cost_eur": 9800},
                              agents={a: {"main": "gpt-4o"} for a in ("orchestrator", "analyst", "simulator", "advisory")})
        for build in (agents.build_orchestrator_agent, agents.build_simulator_agent, agents.build_analyst_agent,
                      agents.build_advisory_agent):
            assert "Blended cost per FTE-month: €9,800" in build().backstory
        assert "never change it" in agents.build_advisory_agent().backstory


class TestToolsUseRules:
    def test_rates_and_limits_come_from_config(self, dataset):
        update_runtime_config(business_rules={"fte_monthly_cost_eur": 10000, "exit_cost_rate_pct": 5,
                                              "max_budget_change_pct": 20})
        ctx.set_portfolio_context(dataset, {})
        assert "burn rate +€10,000/month" in sim.resource_transfer("P-1002", "P-1001", 1.0)
        cancel = sim.project_cancellation("P-1001")
        assert "exit/contract costs (5%)" in cancel and "≈ €" in cancel
        assert "exceeds the limit of ±20%" in sim.budget_change("P-1002", -25, is_perc=True)

    def test_team_capacity_limit_drives_graph_tool(self, tmp_path, dataset, monkeypatch):
        portfolio_graph.pin(dataset)
        ctx.set_portfolio_context(dataset, {})
        assert "Team Harbor" in graph_tools.team_bottlenecks()  # 125 % > 100 %
        update_runtime_config(business_rules={"team_capacity_limit_pct": 130})
        assert graph_tools.team_bottlenecks() == "RESULT identify_team_bottlenecks | No team above 130 % utilisation."


# =========================================================================== verdict engine
def _fact(kind, **data):
    return {"type": kind, **data}


class TestVerdictRules:
    def test_ai_estimate_and_heuristic_placeholder_are_conditions(self):
        ai = compute_verdict(["⚠️ [AI ESTIMATE] supplier risk"], [], [])
        heuristic = compute_verdict(["⚠️ [HEURISTIC PLACEHOLDER – no AI] supplier risk"], [], [])
        assert ai.decision == heuristic.decision == "CONDITIONAL GO"
        assert "qualitative AI estimate" in ai.reasons[0] and "heuristic placeholder" in heuristic.reasons[0]

    def test_rule_order(self):
        assert compute_verdict(["INTERNAL ERROR: x", "ERROR: Budget undercut"], [], []).decision == "ANALYSIS INCOMPLETE"
        assert compute_verdict(["ERROR: Data quality – P-1 blocked"], [], []).label == "NO-GO"
        assert compute_verdict(["ERROR: Budget undercut – x"], [], []).label == "NO-GO"
        assert compute_verdict(["RESULT ok"], [], []).label == "GO"
        assert compute_verdict([], ["RESULT graph"], []).label == "NO DECISION REQUIRED"

    def test_budget_overrun_threshold_separates_nogo_and_conditional(self):
        big = _fact("budget_overrun", project="P-1", eac=1_000_000, new_budget=800_000, overrun=200_000, overrun_pct=25.0)
        small = _fact("budget_overrun", project="P-1", eac=880_000, new_budget=800_000, overrun=80_000, overrun_pct=10.0)
        assert compute_verdict(["RESULT"], [], [big]).label == "NO-GO"
        v = compute_verdict(["RESULT"], [], [small])
        assert v.decision == "CONDITIONAL GO" and any("Close the forecast gap of €80,000" in c for c in v.conditions)
        assert compute_verdict(["RESULT"], [], [big], rules=BusinessRules(eac_overrun_nogo_pct=30)).decision == "CONDITIONAL GO"

    def test_domino_board_and_forced_conditions(self):
        facts = [_fact("domino", project="P-1", affected=3, direct=2),
                 _fact("budget_change", project="P-1", delta=-600000, new_budget=1000000)]
        v = compute_verdict(["RESULT"], ["RESULT"], facts, forced=["P-9"])
        assert v.decision == "CONDITIONAL GO"
        assert any("board approval" in c for c in v.conditions) and any("3 dependent project" in c for c in v.conditions)
        assert any("P-9" in r for r in v.reasons)
        assert compute_verdict(["RESULT"], [], facts[:1], rules=BusinessRules(domino_conditional_threshold=5)).decision == "GO"

    def test_uncovered_tool_warnings_still_make_it_conditional(self):
        warning = "Ramp-up > 50 % – plan 1–2 months of onboarding loss."
        v = compute_verdict(["RESULT x"], [], [_fact("ramp_up", project="P-1", warning=warning)])
        assert v.decision == "CONDITIONAL GO" and v.reasons[0] == f"Tool warning: {warning}"

    def test_warning_text_in_a_tool_result_is_never_parsed(self):
        # only structured facts count – a "⚠" line in free text (e.g. an echoed LLM answer) changes nothing
        assert compute_verdict(["RESULT x\n⚠ Ramp-up > 50 % – plan onboarding."], [], []).decision == "GO"

    def test_tools_record_facts_for_the_engine(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        sim.budget_change("P-1002", -20, is_perc=True)  # 640k new budget vs 800k EAC -> 25 % overrun
        v = verdict_from_context()
        assert v.label == "NO-GO" and "P-1002" in v.reasons[1]


class TestEnforcement:
    def test_llm_verdict_is_overwritten(self):
        v = compute_verdict(["ERROR: Budget undercut"], [], [])
        paper, deviated = enforce_verdict("# Paper\n**Request:** x | **Recommendation:** GO | more\ntext", v)
        assert deviated and "**Recommendation:** NO-GO | more" in paper
        assert paper.count("Verdict (rule-based, not AI-generated)") == 1
        again, deviated_again = enforce_verdict(paper, v)
        assert again == paper and not deviated_again  # idempotent

    def test_missing_recommendation_line_is_inserted(self):
        paper, deviated = enforce_verdict("# Executive Decision Paper\nBody", compute_verdict(["RESULT"], [], []))
        assert deviated and paper.startswith("# Executive Decision Paper\n**Recommendation:** GO\nBody")


# =========================================================================== LLM agent mode with a fake crew
class TestAgentModeUsesEngine:
    def test_advisory_gets_the_verdict_and_cannot_change_it(self, dataset, monkeypatch):
        from app.crew import agents, tasks
        from app.llm import agent as llm_agent

        captured = {}

        class FakeCrew:
            def __init__(self, agents=None, tasks=None, **kwargs):
                self.tasks = tasks

            def kickoff(self):
                if self.tasks[0]["kind"] == "advisory":  # the AI "decides" GO – must be overruled
                    return SimpleNamespace(raw="# Executive Decision Paper\n**Request:** x | **Recommendation:** GO\n")
                captured["simulator_task"] = self.tasks[0]["kind"]  # guided plan: the tool already ran
                return SimpleNamespace(raw="assessed", tasks_output=[SimpleNamespace(raw="assessed")])

        def fake_advisory(agent, request, plan, specialist_results, steward_md, role, verdict):
            captured["verdict"] = verdict
            return {"kind": "advisory"}

        monkeypatch.setattr(llm_agent, "Crew", FakeCrew)
        for name in ("build_orchestrator_agent", "build_simulator_agent", "build_analyst_agent", "build_advisory_agent"):
            monkeypatch.setattr(agents, name, lambda *a, **k: object())
        monkeypatch.setattr(tasks, "create_simulation_task", lambda *a, **k: {"kind": "simulation"})
        monkeypatch.setattr(tasks, "create_assessment_task", lambda *a, **k: {"kind": "assessment"})
        monkeypatch.setattr(tasks, "create_analysis_task", lambda *a, **k: {"kind": "graph"})
        monkeypatch.setattr(tasks, "create_advisory_task", fake_advisory)
        monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))

        scenario = build_guided_scenario("budget_change", "P-1002", NAMES, value=20, is_perc=True, direction="decrease")
        result = pipeline.run_steering_pipeline(scenario.request, dataset, ROLE_PMO, "PMO", plan=scenario.plan)

        assert result.mode == "agents"
        assert captured["simulator_task"] == "assessment"  # the Simulator assessed, it did not re-type the values
        assert [c["new"] for c in result.proposed_changes if c["field"] == "budget_cost_eur"] == [640000]
        assert captured["verdict"].label == "NO-GO"  # injected before writing
        assert "**Recommendation:** NO-GO" in result.decision_paper
        assert result.verdict.label == captured["verdict"].label
        assert any("deviated from the rule-based verdict" in w for w in result.warnings)

    def test_advisory_prompt_contains_verdict_and_conditions(self):
        from app.crew.tasks import IntentPlan, create_advisory_task

        v = compute_verdict(["RESULT"], [], [_fact("domino", project="P-1", affected=4, direct=1)])
        task = create_advisory_task(None, "req", IntentPlan(summary="s"), "RESULT {x}", "steward", "PMO", v)
        assert "FINAL VERDICT" in task.description and "must not change it" in task.description
        assert "CONDITIONAL GO" in task.description and "Align the 4 dependent project(s) of P-1" in task.description
        assert "{" not in task.description  # tool output braces neutralised (CrewAI template placeholders)
