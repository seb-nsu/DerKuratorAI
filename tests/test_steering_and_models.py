"""Guided scenarios, Google Gemini routing, shadow mode and AI interpretation of data quality.
No network: LLMs are replaced by fakes, the graph by a controlled error."""
from __future__ import annotations

import json
import threading

import pytest

from app.core import logs
from app.core.config import detect_provider
from app.core.runtime_config import get_runtime_config, update_runtime_config
from app.core.security import ROLE_PM, ROLE_PMO, register_pii_names
from app.crew import agents, dq_interpretation, pipeline, shadow
from app.crew.pipeline import run_steering_pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools.data_steward_tools import run_all_checks
from app.llm import client as llm_client
from tests.llm_fakes import FakeClient, install


@pytest.fixture
def no_graph(monkeypatch):
    """Graph tools return a controlled error – tests never build the real dependency graph."""
    def unavailable():
        raise graph_tools.GraphUnavailable("Graph im Test deaktiviert")
    monkeypatch.setattr(graph_tools, "get_graph", unavailable)


NAMES = {"P-1001": "Alpha Platform", "P-1002": "Beta Billing", "P-1003": "Gamma Governance"}


# =========================================================================== Guided scenarios
class TestGuidedScenarios:
    def test_capex_cut_runs_exactly_as_configured(self, dataset, no_graph):
        scenario = build_guided_scenario("budget_change", "P-1002", NAMES, value=10, is_perc=True,
                                         direction="decrease", cost_type="capex")
        assert "Cut CapEx of P-1002 (Beta Billing) by 10 %" == scenario.request
        result = run_steering_pipeline(scenario.request, dataset, ROLE_PMO, "PMO", plan=scenario.plan)
        capex = [c for c in result.proposed_changes if c["field"] == "planned_capital_eur"]
        assert capex and capex[0]["new"] == 450000  # €500,000 − 10 %

    def test_structured_parameters_win_over_contradicting_text(self, dataset, no_graph):
        scenario = build_guided_scenario("budget_change", "P-1002", NAMES, value=10, is_perc=True,
                                         direction="decrease", cost_type="capex")
        result = run_steering_pipeline("Increase the budget of P-1002 by 99 %", dataset, ROLE_PMO, "PMO", plan=scenario.plan)
        assert [c["new"] for c in result.proposed_changes if c["field"] == "planned_capital_eur"] == [450000]

    def test_cancel_with_target_transfers_free_budget(self, dataset, no_graph):
        scenario = build_guided_scenario("cancel", "P-1001", NAMES, target="P-1002")
        actions = [i.parameters["action"] for i in scenario.plan.sub_intents]
        assert actions[:3] == ["cancel", "budget_transfer", "task_impact"]
        result = run_steering_pipeline(scenario.request, dataset, ROLE_PMO, "PMO", plan=scenario.plan)
        target_budget = [c["new"] for c in result.proposed_changes if c["project_id"] == "P-1002" and c["field"] == "budget_cost_eur"]
        assert target_budget == [967440]  # €800,000 + free budget €182,000 less 8 % exit costs (€167,440)

    def test_schedule_scenario_uses_sliders(self, dataset, no_graph):
        scenario = build_guided_scenario("schedule", "P-1001", NAMES, months_delta=2, scope_change_pct=0, capacity_change_pct=0)
        result = run_steering_pipeline(scenario.request, dataset, ROLE_PMO, "PMO", plan=scenario.plan)
        assert "[Waterfall] Shift: +2.0 months" in result.decision_paper

    @pytest.mark.parametrize("action,kwargs,message", [
        ("budget_transfer", {"target": "P-1001", "amount": 1000}, "target project"),
        ("resource_transfer", {"target": "P-1002", "fte": 0}, "FTE"),
        ("schedule", {}, "at least"),
        ("budget_change", {"value": 0}, "amount"),
    ])
    def test_invalid_inputs_are_rejected_before_running(self, action, kwargs, message):
        with pytest.raises(ValueError, match=message):
            build_guided_scenario(action, "P-1001", NAMES, **kwargs)


# =========================================================================== Google Gemini
class TestGeminiRouting:
    @pytest.mark.parametrize("model", ["gemini/gemini-2.5-pro", "gemini-2.5-flash", "google/gemini-2.5-pro"])
    def test_gemini_models_route_to_google(self, model):
        assert detect_provider(model) == "google"

    def test_missing_google_key_is_reported(self):
        update_runtime_config(agents={"analyst": {"main": "gemini/gemini-2.5-pro"}})
        assert "GOOGLE_API_KEY" in get_runtime_config().missing_keys_for_models()
        with pytest.raises(agents.LLMConfigError, match="GOOGLE_API_KEY"):
            agents.build_analyst_agent()

    def test_gemini_goes_through_its_openai_compatible_endpoint_with_the_google_key(self):
        from app.llm.client import GEMINI_OPENAI_URL, OpenAIClient, client_for

        update_runtime_config(google_api_key="test-google-key", agents={"simulator": {"main": "gemini/gemini-2.5-flash"}}, temperature=0.3)
        client = client_for(agents.build_simulator_agent().model)
        assert isinstance(client, OpenAIClient) and client.name == "gemini-2.5-flash" and client.temperature == 0.3
        assert str(client._sdk.base_url) == GEMINI_OPENAI_URL and client._sdk.api_key == "test-google-key"


# =========================================================================== per-agent shadow mode
class TestShadowMode:
    @pytest.fixture(autouse=True)
    def shadow_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs, "SHADOW_LOG", tmp_path / "shadow.jsonl")
        return tmp_path / "shadow.jsonl"

    def _fake_crew(self, calls: list, paper: str = "# Executive Decision Paper\n**Recommendation:** GO\n"):
        def fake(request, portfolio_md, steward_md, role, projects, plan_override=None, variant="main", forced=None):
            calls.append(variant)
            ctx.record_change("P-1002", "budget_cost_eur", 1.0, "shadow_tool")  # must stay in the shadow context
            ctx.record_tool_result("simulation", "shadow_tool", "RESULT simulate_budget_change | P-1002")  # like a real tool
            return paper, None, [], "RESULT ..."
        return fake

    def test_shadow_crew_runs_with_shadow_variant_and_logs_verdicts(self, dataset, monkeypatch, shadow_log):
        calls: list = []
        monkeypatch.setattr(pipeline, "_run_agents", self._fake_crew(calls))
        primary = {"models": {"all": "rule-based (offline)"}, "duration_s": 1.2, "result": "**Recommendation:** GO"}
        models = {"orchestrator": "gpt-4o-mini", "analyst": "gemini/gemini-2.5-pro"}
        shadow.run_shadow_crew("Stop P-1001", dataset, {}, "md", "steward", ROLE_PMO, None, primary, models)
        entry = json.loads(shadow_log.read_text(encoding="utf-8").splitlines()[-1])
        assert calls == ["shadow"]
        assert entry["shadow"]["models"] == models and entry["shadow"]["error"] is None
        assert entry["verdict_match"] is True and entry["verdict_shadow"] == "GO"

    def test_shadow_context_is_isolated_from_the_main_ledger(self, dataset, monkeypatch):
        ctx.set_portfolio_context(dataset, {})  # main run context
        ctx.record_change("P-1001", "status", "Red", "main_tool")
        monkeypatch.setattr(pipeline, "_run_agents", self._fake_crew([]))
        worker = threading.Thread(target=shadow.run_shadow_crew,
                                  args=("x", dataset, {}, "md", "steward", ROLE_PMO, None, {"result": ""}, {}))
        worker.start()
        worker.join(timeout=10)
        assert [c["source_tool"] for c in ctx.get_ledger()] == ["main_tool"]  # shadow change never reaches main

    def test_shadow_failure_is_logged_not_raised(self, dataset, monkeypatch, shadow_log):
        def boom(*args, **kwargs):
            raise RuntimeError("Provider unreachable")
        monkeypatch.setattr(pipeline, "_run_agents", boom)
        result = shadow.run_shadow_crew("x", dataset, {}, "", "", ROLE_PMO, None, {"result": ""}, {})
        assert "Provider unreachable" in result["error"]
        assert json.loads(shadow_log.read_text(encoding="utf-8"))["shadow"]["error"]

    def test_shadow_stays_off_without_model_or_key(self, dataset):
        assert shadow.start_shadow_run("x", dataset, {}, "", "", ROLE_PMO, None, {}) is None
        update_runtime_config(agents={"analyst": {"shadow": "gemini/gemini-2.5-pro"}})  # Google key missing
        assert get_runtime_config().shadow_enabled and not get_runtime_config().shadow_available
        assert shadow.start_shadow_run("x", dataset, {}, "", "", ROLE_PMO, None, {}) is None

    def test_per_agent_shadow_model_falls_back_to_main(self):
        update_runtime_config(agents={"analyst": {"main": "gpt-4o", "shadow": "gemini/gemini-2.5-pro"}})
        cfg = get_runtime_config()
        assert cfg.model_for_role("analyst", "shadow") == "gemini/gemini-2.5-pro"
        assert cfg.model_for_role("simulator", "shadow") == cfg.model_for_role("simulator")

    def test_pipeline_triggers_shadow_crew_in_background(self, dataset, no_graph, monkeypatch):
        update_runtime_config(openai_api_key="t", anthropic_api_key="t", google_api_key="t",
                              agents={"analyst": {"shadow": "gemini/gemini-2.5-flash"}})
        monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: False))  # main stays offline
        called, captured = threading.Event(), {}

        def fake_run(*args):
            captured["args"] = args
            called.set()

        monkeypatch.setattr(shadow, "run_shadow_crew", fake_run)
        result = run_steering_pipeline("Stop Alpha Platform", dataset, ROLE_PMO, "PMO")
        assert called.wait(timeout=5)
        _request, _ds, _blocked, _md, _steward, _role, _plan, primary, models, *_ = captured["args"]
        assert models["analyst"] == "gemini/gemini-2.5-flash" and primary["result"] == result.decision_paper
        assert result.proposed_changes  # shadow never changes the main results


# =========================================================================== PMO force override
class TestForceSimulation:
    def _cut_blocked(self, dataset, role, force):
        scenario = build_guided_scenario("budget_change", "P-1003", NAMES, value=5, is_perc=True, direction="decrease",
                                         cost_type="total")
        return run_steering_pipeline(scenario.request, dataset, role, "user", plan=scenario.plan, force=force)

    def test_without_force_blocked_project_is_rejected(self, dataset, no_graph):
        result = self._cut_blocked(dataset, ROLE_PMO, force=False)
        assert "ERROR: Data quality" in result.decision_paper and not result.proposed_changes

    def test_pmo_force_simulates_blocked_project_with_warning(self, dataset, no_graph):
        result = self._cut_blocked(dataset, ROLE_PMO, force=True)
        assert "Budget change | " in result.decision_paper and "simulate_budget_change" not in result.decision_paper
        assert "FORCED SIMULATION" in result.decision_paper and any("FORCED SIMULATION" in w for w in result.warnings)
        assert result.steward_report.blocked_project_ids == ["P-1003"]  # findings are still reported
        assert result.proposed_changes

    def test_force_is_ignored_for_project_managers(self, dataset, no_graph):
        result = self._cut_blocked(dataset, ROLE_PM, force=True)
        assert "ERROR: Data quality" in result.decision_paper
        assert any("reserved for the PMO" in w for w in result.warnings)

    def test_force_keeps_value_guards_for_missing_numbers(self, dataset_factory, project_factory, no_graph):
        ds = dataset_factory([project_factory(project_number="P-1003", budget_cost_eur=None)])
        result = self._cut_blocked(ds, ROLE_PMO, force=True)
        assert "values missing for this calculation" in result.decision_paper


# =========================================================================== AI interpretation
class TestDataQualityInterpretation:
    def test_rules_interpretation_without_llm(self, dataset):
        summary = dq_interpretation.summarize(run_all_checks(dataset))
        result = dq_interpretation.interpret(summary)
        assert result.source == "rules" and "67 %" in result.text and "P-1003" in result.text

    def test_llm_interpretation_uses_data_steward_model_and_receives_no_names(self, dataset, monkeypatch):
        register_pii_names(["Priya Raman"], "[PM_MASKED]")
        update_runtime_config(agents={"data_steward": {"main": "gemini/gemini-2.5-flash"}}, google_api_key="test-google-key")
        fake, used = FakeClient(["**Overall picture:** data partially reliable."]), []
        monkeypatch.setattr(llm_client, "client_for", lambda model, purpose="": used.append(model) or fake)
        result = dq_interpretation.interpret(dq_interpretation.summarize(run_all_checks(dataset)))
        assert used == ["gemini/gemini-2.5-flash"]
        assert result.source == "llm" and result.text.startswith(dq_interpretation.AI_LABEL)
        prompt = fake.calls[0]["system"] + " ".join(fake.prompts)
        assert "Priya Raman" not in prompt and "P-1003" in prompt

    def test_llm_failure_falls_back_to_rules(self, dataset, monkeypatch):
        update_runtime_config(google_api_key="test-google-key", agents={"data_steward": {"main": "gemini/gemini-2.5-pro"}})
        install(monkeypatch, FakeClient(fail=True))
        assert dq_interpretation.interpret(dq_interpretation.summarize(run_all_checks(dataset))).source == "rules"
