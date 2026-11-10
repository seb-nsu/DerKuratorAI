"""The own LLM call log: one line per call with caller, model, duration, tokens, cost and outcome – never the prompt
or the answer – and the optional OpenTelemetry export, which is off without an endpoint and carries the same fields."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core import logs
from app.llm.client import OpenAIClient, complete
from app.observability import llm_calls, tracer

SECRET_PROMPT = "Cut the budget of Project Phoenix-Secret by 20 %"


def _client(monkeypatch, usage=None, fail=False) -> OpenAIClient:
    client = OpenAIClient("gpt-4o", "test-key", base_url="http://localhost:9/v1")

    def create(**kwargs):
        if fail:
            raise TimeoutError("provider unreachable")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="The answer text.",
                                                                                tool_calls=None))], usage=usage)

    client._sdk.chat.completions.create = create
    return client


def _lines() -> list[dict]:
    return [json.loads(line) for line in logs.LLM_CALLS_LOG.read_text(encoding="utf-8").splitlines()]


def test_a_call_is_logged_with_figures_and_without_content(monkeypatch):
    client = _client(monkeypatch, usage={"prompt_tokens": 1000, "completion_tokens": 200})
    with llm_calls.caller("simulator"):
        client.chat("system rules", [client.user(SECRET_PROMPT)])
    (entry,) = _lines()
    assert entry["caller"] == "simulator" and entry["model"] == "gpt-4o" and entry["provider"] == "openai"
    assert (entry["prompt_tokens"], entry["completion_tokens"], entry["outcome"]) == (1000, 200, "text")
    assert entry["cost_usd"] == pytest.approx((1000 * 2.5 + 200 * 10) / 1_000_000)
    assert entry["duration_s"] >= 0 and entry["environment"]
    raw = logs.LLM_CALLS_LOG.read_text(encoding="utf-8")
    assert "Phoenix-Secret" not in raw and "answer text" not in raw and "system rules" not in raw
    assert llm_calls.recent(1)[0] == entry


def test_a_failed_call_is_logged_and_still_raised(monkeypatch):
    client = _client(monkeypatch, fail=True)
    with pytest.raises(TimeoutError):
        client.chat("s", [client.user("q")])
    assert _lines()[0]["outcome"] == "error:TimeoutError" and _lines()[0]["caller"] == "unnamed"


def test_single_calls_are_named_by_their_purpose(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr("app.llm.client.client_for", lambda model, purpose="": client)
    assert complete("gpt-4o", "s", "q", purpose="assistant: project P-1") == "The answer text."
    assert _lines()[0]["caller"] == "assistant"


def test_summary_groups_by_caller_and_model(monkeypatch):
    client = _client(monkeypatch, usage={"prompt_tokens": 10, "completion_tokens": 5})
    with llm_calls.caller("analyst"):
        client.chat("s", [client.user("q")])
        client.chat("s", [client.user("q")])
    failing = _client(monkeypatch, fail=True)
    with llm_calls.caller("analyst"), pytest.raises(TimeoutError):
        failing.chat("s", [failing.user("q")])
    (row,) = llm_calls.summary()
    assert row["Caller"] == "analyst" and row["Calls"] == 3 and row["Errors"] == 1 and row["Tokens"] == 30


def test_agents_name_their_calls(monkeypatch):
    from app.llm.agent import Agent, Task, run_task
    from tests.llm_fakes import FakeClient

    seen = []

    class Recording(FakeClient):
        def chat(self, *args, **kwargs):
            seen.append(llm_calls.current_caller())
            return super().chat(*args, **kwargs)

    monkeypatch.setattr("app.llm.client.client_for", lambda model, purpose="": Recording(["done"]))
    run_task(Task("x", "y", Agent("Tester", "g", "b", "test-model", name="simulator.shadow")))
    assert seen == ["simulator.shadow"]


class TestOpenTelemetryExport:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            monkeypatch.delenv(key, raising=False)
        tracer.reset_tracing()
        yield
        tracer.reset_tracing()

    def test_off_without_an_endpoint(self):
        status = tracer.init_tracing()
        assert not status.enabled and "OTEL_EXPORTER_OTLP_ENDPOINT" in status.message
        tracer.record_llm_call({"caller": "x"})  # nothing to export, never raises

    def test_a_broken_exporter_never_stops_the_app(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com:4318")
        import builtins

        real_import = builtins.__import__

        def no_exporter(name, *args, **kwargs):
            if name.startswith("opentelemetry.exporter"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_exporter)
        status = tracer.init_tracing()
        assert not status.enabled and status.endpoint == "collector.example.com:4318"
        assert "ImportError" in status.message

    def test_metrics_carry_figures_and_labels_only(self, monkeypatch):
        recorded = []

        class Instrument:
            def __init__(self, name):
                self.name = name

            def add(self, value, labels):
                recorded.append((self.name, value, labels))

            record = add

        monkeypatch.setattr(tracer, "_status", tracer.TracingStatus(True, "collector", "on"))
        monkeypatch.setattr(tracer, "_metrics", {n: Instrument(n) for n in ("tokens", "calls", "duration", "cost")})
        entry = {"caller": "advisory", "model": "gpt-4o", "provider": "openai", "kind": "stream", "outcome": "text",
                 "duration_s": 1.5, "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.001, "tool_calls": 0}
        tracer.record_llm_call(entry)
        labels = {"caller": "advisory", "model": "gpt-4o", "provider": "openai"}
        assert ("tokens", 100, {**labels, "direction": "input"}) in recorded
        assert ("calls", 1, {**labels, "outcome": "text"}) in recorded and ("duration", 1.5, labels) in recorded
        assert ("cost", 0.001, labels) in recorded


def test_a_full_log_rotates_and_keeps_its_older_generations(monkeypatch, tmp_path):
    monkeypatch.setattr(logs, "LLM_CALLS_LOG", tmp_path / "llm_calls.jsonl")
    monkeypatch.setattr(llm_calls, "MAX_BYTES", 10)
    for n in range(llm_calls.GENERATIONS + 3):
        llm_calls.record(f"model-{n}", "openai", "chat", 0.1, 1, 1, "text")
    rotated = sorted(p.name for p in tmp_path.glob("llm_calls.jsonl*"))
    assert rotated == ["llm_calls.jsonl", *[f"llm_calls.jsonl.{n}" for n in range(1, llm_calls.GENERATIONS + 1)]]
    newest_rotated = json.loads((tmp_path / "llm_calls.jsonl.1").read_text(encoding="utf-8"))
    assert newest_rotated["model"] == f"model-{llm_calls.GENERATIONS + 1}"
