"""Claude models get no sampling parameters (current Claude models reject them); OpenAI and Gemini keep the configured
temperature. The evaluation never counts an offline fallback as an LLM result."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.runtime_config import get_runtime_config, update_runtime_config
from app.crew import agents, pipeline
from app.llm.client import AnthropicClient, OpenAIClient
from scripts import run_evals


@pytest.fixture(autouse=True)
def fresh_llm_caches():
    agents.clear_llm_caches()
    yield
    agents.clear_llm_caches()


class TestSamplingParameters:
    @staticmethod
    def _sent(client, sdk_method: str) -> dict:
        sent: dict = {}

        def create(**kwargs):
            sent.update(kwargs)
            raise RuntimeError("stop")

        target = client._sdk.messages if sdk_method == "anthropic" else client._sdk.chat.completions
        target.create = create
        with pytest.raises(RuntimeError):
            client.chat("system", [client.user("question")])
        return sent

    def test_claude_gets_no_temperature(self):
        client = AnthropicClient("claude-haiku-4-5", "sk-ant-test-key", temperature=0.1)
        sent = self._sent(client, "anthropic")
        assert "temperature" not in sent and sent["model"] == "claude-haiku-4-5" and sent["system"] == "system"

    @pytest.mark.parametrize("name, key", [("gpt-4o-mini", "sk-test-openai-key"),
                                           ("gemini/gemini-2.5-flash", "AIza-test-google-key")])
    def test_openai_and_gemini_keep_the_configured_temperature(self, name, key):
        client = OpenAIClient(name, key, base_url="http://localhost:9/v1", temperature=0.1)
        sent = self._sent(client, "openai")
        assert sent["temperature"] == pytest.approx(0.1) and sent["model"] == name.split("/")[-1]
        assert sent["messages"][0] == {"role": "system", "content": "system"}


def _fail_crew(monkeypatch):
    def crew_down(*args, **kwargs):
        raise TimeoutError("provider unreachable")

    monkeypatch.setattr(pipeline, "_run_agents", crew_down)
    monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))


class TestEvaluationFallbackDetection:
    def test_failed_llm_runs_are_fallbacks_and_not_llm_results(self, monkeypatch):
        if not run_evals.PORTFOLIO.exists():
            pytest.skip("Demo portfolio not available")
        _fail_crew(monkeypatch)
        cases = [c for c in run_evals.load_cases() if c["id"] in ("E2", "E3")]
        runs = run_evals.evaluate("llm", cases=cases, include_pytest_cases=False).runs
        assert [(r.plan_source, r.error) for r in runs] == [("fallback_offline", "TimeoutError")] * 2
        assert run_evals.llm_results(runs) == [] and run_evals.incomplete(runs)
        report = run_evals.render_report(run_evals.Evaluation(cases=cases, runs=runs))
        assert "LLM results incomplete" in report and "LLM calls that fell back to offline" in report
        assert "| LLM calls failed → offline fallback (excluded above) | 2 of 2 |" in report

    def test_orchestrator_fallback_counts_as_offline_plan(self):
        result = SimpleNamespace(mode="agents", warnings=["Orchestrator fallback to rule-based parser (ValidationError)."])
        assert run_evals.plan_source(result, "llm") == ("fallback_offline", "ValidationError")
        assert run_evals.plan_source(SimpleNamespace(mode="agents", warnings=[]), "llm") == ("llm", None)

    def test_preflight_stops_with_a_redacted_message(self, monkeypatch):
        if not run_evals.PORTFOLIO.exists():
            pytest.skip("Demo portfolio not available")

        def crew_down(*args, **kwargs):
            raise PermissionError("invalid key sk-ant-abcdef1234567890")

        monkeypatch.setattr(pipeline, "_run_agents", crew_down)
        monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))
        message = run_evals.preflight()
        assert message and "PermissionError" in message
        assert "sk-ant-abcdef" not in message  # the exception text stays in the system log


def test_offline_mode_is_never_labelled_llm():
    update_runtime_config(openai_api_key="", anthropic_api_key="", google_api_key="")
    assert run_evals.plan_source(SimpleNamespace(mode="offline", warnings=[]), "offline") == ("offline", None)
