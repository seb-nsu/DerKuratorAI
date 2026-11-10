"""Release-candidate features: FinOps cost tracking, business audit trail, chat persistence, MCP settings/connector,
scenario visuals and report export. No network calls, no real data/ files."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core import chat_store, clock, cost_tracker, logs
from app.core.env_config import env_config_path, load_mcp_config, promote_env_config, save_mcp_config
from app.core.runtime_config import persist_runtime_config, update_runtime_config
from app.crew.tools.mcp_client import EnterpriseMCPConnector, MCPConnectorError
from app.ui import visuals


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "DATA_DIR", tmp_path)
    (tmp_path / "supported_models.json").write_text(json.dumps({"models": [
        {"model_id": "gpt-4o", "provider": "openai", "input_cost_per_1m": 2.5, "output_cost_per_1m": 10.0},
        {"model_id": "gemini/gemini-2.5-pro", "provider": "google", "is_thinking_model": True,
         "input_cost_per_1m": 1.25, "output_cost_per_1m": 10.0},
        {"model_id": "gpt-4.1", "provider": "openai"},
    ]}), encoding="utf-8")
    return tmp_path


class TestCostTracking:
    def test_priced_usage_is_estimated_and_bare_names_resolve(self, registry):
        cost_tracker.record("gpt-4o", 1_000_000, 100_000)
        cost_tracker.record("gemini-2.5-pro", 200_000, 0)  # CrewAI/Gemini report the bare model name
        snap = cost_tracker.snapshot()
        assert snap["cost_usd"] == pytest.approx(2.5 + 1.0 + 0.25)
        assert set(snap["by_model"]) == {"gpt-4o", "gemini/gemini-2.5-pro"} and snap["calls"] == 2

    def test_unpriced_models_are_flagged(self, registry):
        cost_tracker.record("gpt-4.1", 1000, 1000)
        assert cost_tracker.snapshot()["unpriced_models"] == ["gpt-4.1"]

    def test_anthropic_usage_of_a_client_call_is_recorded(self, registry):
        from app.llm.client import AnthropicClient

        client = AnthropicClient("gpt-4o", "test-key")  # the registry id prices the call
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                   usage=SimpleNamespace(input_tokens=400, output_tokens=100))
        client._sdk.messages.create = lambda **kwargs: response
        assert client.chat("s", [client.user("q")]).text == "ok"
        assert cost_tracker.snapshot()["total_tokens"] == 500

    def test_openai_usage_is_recorded_and_a_call_without_usage_is_ignored(self, registry):
        from app.llm.client import OpenAIClient

        client = OpenAIClient("gpt-4o", "test-key", base_url="http://localhost:9/v1")
        message = SimpleNamespace(content="ok", tool_calls=None)
        usages = iter([{"prompt_tokens": 10, "completion_tokens": 5}, None])
        client._sdk.chat.completions.create = lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=message)], usage=next(usages))
        client.chat("s", [client.user("q")])
        before = cost_tracker.snapshot()
        client.chat("s", [client.user("q")])
        assert cost_tracker.snapshot()["calls"] == before["calls"] == 1
        assert before["prompt_tokens"] == 10


class TestBusinessAudit:
    def test_entry_format(self):
        logs.append_business_audit("Yusuf Demir", "PRJ-1", "project_sponsor", None, "Helena Brandt")
        logs.append_business_audit("Yusuf Demir", "PRJ-1", "percent_complete", 40.0, 55.0)
        lines = logs.BUSINESS_AUDIT_LOG.read_text(encoding="utf-8").splitlines()
        assert lines[0].endswith("User Yusuf Demir changed field project_sponsor from NULL to 'Helena Brandt' on Project ID PRJ-1")
        assert "from 40.0 to 55.0" in lines[1] and "| DEV |" in lines[1]

    def test_write_errors_are_blocking(self, tmp_path):  # an audit failure is never swallowed
        with pytest.raises(logs.AuditWriteError):
            logs.append_business_audit("x", "P", "f", None, 1, path=tmp_path)  # a directory -> OSError
        with pytest.raises(logs.AuditWriteError):
            logs.append_jsonl(tmp_path, {"event": "x"})


class TestChatPersistence:
    MESSAGES = [{"role": "user", "content": "Cut CapEx"}, {"role": "assistant", "content": "paper",
                                                              "meta": {"proposed_changes": [{"old": 1.5, "new": 2}]}}]

    def test_roundtrip_is_scoped_to_identity(self):
        messages = [dict(m) for m in self.MESSAGES]
        chat_store.append_messages("PMO", "PMO-ADMIN", messages)
        loaded = chat_store.load_chat("PMO", "PMO-ADMIN")
        assert [{k: v for k, v in m.items() if k != "id"} for m in loaded] == self.MESSAGES
        assert [m["id"] for m in loaded] == [m["id"] for m in messages]
        assert chat_store.load_chat("Project Manager", "Rowan Ellis") == []
        assert "PMO" not in chat_store.chat_path("PMO", "PMO-ADMIN").name  # no names in file names

    def test_expired_or_corrupt_history_is_ignored(self):
        path = chat_store.chat_path("PMO", "PMO-ADMIN")
        path.parent.mkdir(parents=True, exist_ok=True)
        old = (clock.now() - timedelta(days=2)).isoformat()
        event = {"type": "message", "id": "a", "ts": old, "identity": ["PMO", "PMO-ADMIN"], "message": self.MESSAGES[0]}
        path.write_text(json.dumps(event) + "\n{broken\n", encoding="utf-8")
        assert chat_store.load_chat("PMO", "PMO-ADMIN") == []
        chat_store.append_messages("PMO", "PMO-ADMIN", [dict(self.MESSAGES[0])])
        assert len(chat_store.load_chat("PMO", "PMO-ADMIN")) == 1  # corrupt line skipped, rest intact
        chat_store.clear_chat("PMO", "PMO-ADMIN")
        assert chat_store.load_chat("PMO", "PMO-ADMIN") == []


class TestMCPSettings:
    def test_defaults_and_save(self):
        assert load_mcp_config("DEV") == {"enabled": False, "servers": {"servicenow": "", "jira": ""}}
        save_mcp_config("DEV", True, {"jira": "https://jira.example.com/mcp"}, updated_by="tester")
        assert load_mcp_config("DEV")["servers"]["jira"] == "https://jira.example.com/mcp"

    @pytest.mark.parametrize("url", ["ftp://x", "not a url", "http://jira.example.com/mcp"])
    def test_invalid_urls_are_rejected(self, url):
        with pytest.raises(ValueError):
            save_mcp_config("DEV", False, {"jira": url})

    def test_enabling_without_server_is_rejected(self):
        with pytest.raises(ValueError):
            save_mcp_config("DEV", True, {})

    def test_runtime_config_save_and_promotion_keep_mcp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs, "ARCHIVE_DIR", tmp_path / "archive")
        save_mcp_config("DEV", True, {"servicenow": "https://sn.example.com/mcp"})
        update_runtime_config(temperature=0.3)
        persist_runtime_config("tester")  # the runtime config carries no MCP block and must not wipe it
        assert json.loads(env_config_path("DEV").read_text(encoding="utf-8"))["mcp"]["enabled"] is True
        promote_env_config("DEV", "TEST", "tester")
        assert load_mcp_config("TEST")["servers"]["servicenow"] == "https://sn.example.com/mcp"


class TestMCPConnector:
    CONFIG = {"enabled": True, "servers": {"servicenow": "https://sn.example.com/mcp", "jira": "https://jira.example.com/sse"}}

    def test_disabled_connector_is_a_no_op(self):
        connector = EnterpriseMCPConnector.from_config({"enabled": False, "servers": self.CONFIG["servers"]})
        assert not connector.active and connector.agent_kwargs({"role": "x"}) == {"role": "x"}

    def test_token_comes_from_env_and_transport_from_the_url(self, monkeypatch):
        monkeypatch.setenv("SERVICENOW_MCP_TOKEN", "secret-token")
        http, sse = EnterpriseMCPConnector.from_config(self.CONFIG).servers
        assert (http.transport, sse.transport) == ("http", "sse")
        assert http.headers() == {"Authorization": "Bearer secret-token"} and sse.headers() is None

    def test_server_tools_become_agent_tools(self, monkeypatch):
        connector = EnterpriseMCPConnector.from_config(self.CONFIG)
        schema = {"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]}
        monkeypatch.setattr(connector, "_list_specs", lambda srv: [
            {"name": "get_incident", "description": "Reads one incident.", "input_schema": schema}])
        calls = []
        monkeypatch.setattr(connector, "_caller", lambda srv, name: lambda args: calls.append((srv.name, name, args))
                            or "INC1: open")
        kwargs = connector.agent_kwargs({"tools": ["local"]}, tool_filter=lambda server, tool: server == "servicenow")
        local, remote = kwargs["tools"]
        assert local == "local" and remote.name == "servicenow_get_incident" and remote.parameters == schema
        assert remote.run({"number": "INC1"}) == "INC1: open" and calls == [("servicenow", "get_incident", {"number": "INC1"})]

    def test_from_environment_reads_env_file(self):
        save_mcp_config("DEV", True, {"jira": "https://jira.example.com/mcp"})
        connector = EnterpriseMCPConnector.from_environment("DEV")
        assert connector.active and [s.name for s in connector.servers] == ["jira"]

    def test_probe_never_raises(self, monkeypatch):
        connector = EnterpriseMCPConnector.from_config(self.CONFIG)

        def boom(name):
            raise ConnectionError("refused")

        monkeypatch.setattr(connector, "list_tools", boom)
        ok, message = connector.probe("servicenow")
        assert not ok and "ConnectionError" in message
        assert EnterpriseMCPConnector.from_config(self.CONFIG).probe("unknown") == (False, "MCP server 'unknown' is not configured")

    def test_plain_http_only_for_localhost(self):
        connector = EnterpriseMCPConnector.from_config({"enabled": True, "servers": {"jira": "http://jira.corp/mcp"}})
        with pytest.raises(MCPConnectorError):
            connector.agent_tools()


class TestScenarioVisuals:
    CHANGES = [
        {"project_id": "P-1002", "table": "Projects", "field": "budget_cost_eur", "old": 500000, "new": 400000},
        {"project_id": "P-1001", "table": "Projects", "field": "budget_cost_eur", "old": "900000.0", "new": "1000000.0"},
        {"project_id": "P-1001", "table": "Projects", "field": "planned_end_date", "old": "2025-01-01", "new": "2025-03-01"},
    ]

    def test_deltas_only_for_numeric_budget_fields(self):
        deltas = visuals.scenario_deltas(self.CHANGES)
        assert list(deltas["delta"]) == [-100000.0, 100000.0]
        assert visuals.delta_chart(deltas, "€") is not None and visuals.delta_chart(deltas, "FTE") is None

    def test_focus_uses_word_boundaries(self):
        meta = {"request": "Stop P-1001", "plan": {"sub_intents": [{"project_ids": ["P-1003"]}]}}
        assert visuals.focus_projects(meta, {"P-100", "P-1001", "P-1003"}) == ["P-1003", "P-1001"]

    def test_dependency_graph_hides_projects_outside_the_scope(self, dataset):
        deps = pd.DataFrame([{"successor_project_number": "P-1002", "predecessor_project_number": "P-1001",
                              "dependency_type": "Technical", "status": "At Risk"},
                             {"successor_project_number": "P-1003", "predecessor_project_number": "P-9999",
                              "dependency_type": "Data", "status": "On Track"}])
        dataset.dependencies = deps
        edges = visuals.dependency_edges(dataset, ["P-1001"])
        assert len(edges) == 1 and edges.iloc[0]["successor"] == "P-1002"
        fig = visuals.dependency_graph_figure(dataset, visuals.dependency_edges(dataset, ["P-1003"]), ["P-1003"])
        text = fig.to_json()
        assert "Restricted project" in text and "P-9999" not in text  # outside the user's scope: neither ID nor name
        nodes = fig.data[-1]
        assert len(nodes.x) == 2 and any("P-1003" in label for label in nodes.text)

    def test_dependency_graph_puts_predecessors_left(self, dataset):
        edges = pd.DataFrame([{"successor": "P-1002", "predecessor": "P-1001", "type": "Hard", "status": "At Risk"},
                              {"successor": "P-1003", "predecessor": "P-1002", "type": "Soft", "status": ""}])
        fig = visuals.dependency_graph_figure(dataset, edges, ["P-1002"])
        x = dict(zip([t.split("<br>")[0].split(" · ")[0] for t in fig.data[-1].text], fig.data[-1].x, strict=True))
        assert any(" · " in t.split("<br>")[0] for t in fig.data[-1].text)  # RAG as a word next to the shape
        assert x["<b>P-1001</b>"] < x["<b>P-1002</b>"] < x["<b>P-1003</b>"]
        assert fig.data[0].line.color == "#d03b3b"  # the at-risk dependency is red

    def test_graph_only_when_analyst_involved(self):
        assert visuals.wants_dependency_graph({"plan": {"requires_graph_analysis": True}})
        assert visuals.wants_dependency_graph({}, "3 projects depend on P-1001")
        assert not visuals.wants_dependency_graph({}, "Budget reduced.")

    def test_report_and_csv_export(self, dataset):
        messages = [{"role": "user", "content": "Transfer budget"},
                    {"role": "assistant", "content": "**Recommendation:** GO",
                     "meta": {"mode": "offline", "duration_s": 1.2, "proposed_changes": self.CHANGES,
                              "request": "=HYPERLINK(\"x\")"}}]
        report = visuals.build_report_markdown(messages, dataset, "PMO", "PMO-ADMIN", "DEV")
        for section in ("# DerKurator AI – Simulation Report", "## 1. Request", "### Proposed changes",
                        "### Before / after (chart data)", "**Recommendation:** GO", "| Project | Change | Current |"):
            assert section in report
        assert "_Mode" not in report and "source_tool" not in report and "runtime" not in report  # no technical fields
        csv = visuals.changes_csv(messages)
        assert "'=HYPERLINK" in csv and len(csv.strip().splitlines()) == 4
