"""Regression tests: append-only chat, per-request FinOps, key hints, agile fallback, cycle-safe Cypher,
bounded portfolio context, LLM client cache."""
from __future__ import annotations

import json
import threading

import pandas as pd
import pytest

from app.core import chat_store, cost_tracker, logs
from app.core.runtime_config import update_runtime_config
from app.crew import agents
from app.crew.tasks import PORTFOLIO_CONTEXT_MAX_ROWS, portfolio_markdown
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import simulator_tools as sim
from app.graph import portfolio_graph


# =========================================================================== append-only chat
class TestAppendOnlyChat:
    def test_two_tabs_never_lose_each_others_messages(self):
        tab_a = [{"role": "user", "content": "A1"}]
        tab_b = [{"role": "user", "content": "B1"}]
        chat_store.append_messages("PMO", "PMO-ADMIN", tab_a)
        chat_store.append_messages("PMO", "PMO-ADMIN", tab_b)  # tab B never saw A1 – used to overwrite it
        chat_store.append_messages("PMO", "PMO-ADMIN", tab_a)  # re-saving persisted messages appends nothing
        assert [m["content"] for m in chat_store.load_chat("PMO", "PMO-ADMIN")] == ["A1", "B1"]

    def test_clear_is_not_undone_by_a_stale_tab(self):
        stale = [{"role": "user", "content": "old"}]
        chat_store.append_messages("PMO", "PMO-ADMIN", stale)
        chat_store.clear_chat("PMO", "PMO-ADMIN")
        chat_store.append_messages("PMO", "PMO-ADMIN", stale + [{"role": "user", "content": "new"}])
        assert [m["content"] for m in chat_store.load_chat("PMO", "PMO-ADMIN")] == ["new"]

    def test_update_applies_meta_to_persisted_message(self):
        msgs = [{"role": "assistant", "content": "paper", "meta": {"applied": False, "mode": "offline"}}]
        chat_store.append_messages("PMO", "PMO-ADMIN", msgs)
        chat_store.update_message("PMO", "PMO-ADMIN", msgs[0]["id"], {"applied": True, "applied_at": "2026-09-27 18:00"})
        loaded = chat_store.load_chat("PMO", "PMO-ADMIN")[0]
        assert loaded["meta"] == {"applied": False, "mode": "offline"} | {"applied": True, "applied_at": "2026-09-27 18:00"}

    def test_concurrent_appends_from_threads_are_all_kept(self):
        def worker(i):
            chat_store.append_messages("PMO", "PMO-ADMIN", [{"role": "user", "content": f"m{i}"}])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert len(chat_store.load_chat("PMO", "PMO-ADMIN")) == 20

    def test_compaction_keeps_live_messages_and_timestamps(self, monkeypatch):
        monkeypatch.setattr(chat_store, "COMPACT_ABOVE_BYTES", 0)
        chat_store.append_messages("PMO", "PMO-ADMIN", [{"role": "user", "content": "keep"}])
        chat_store.clear_chat("PMO", "PMO-ADMIN")
        chat_store.append_messages("PMO", "PMO-ADMIN", [{"role": "user", "content": "after clear"}])
        lines = chat_store.chat_path("PMO", "PMO-ADMIN").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["message"]["content"] == "after clear"


# =========================================================================== per-request FinOps
@pytest.fixture
def priced_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "DATA_DIR", tmp_path)
    (tmp_path / "supported_models.json").write_text(json.dumps({"models": [
        {"model_id": "gpt-4o", "provider": "openai", "input_cost_per_1m": 2.5, "output_cost_per_1m": 10.0}]}), encoding="utf-8")


class TestPerRequestCosts:
    def test_request_sees_only_its_own_calls(self, priced_registry):
        with cost_tracker.track_request() as usage:
            cost_tracker.record("gpt-4o", 1000, 100)
            cost_tracker.reset()  # admin reset mid-request must not produce negative numbers
            cost_tracker.record("gpt-4o", 1000, 100)
        assert usage.as_dict()["calls"] == 2 and usage.as_dict()["cost_usd"] == pytest.approx(2 * (0.0025 + 0.001))

    def test_background_thread_does_not_pollute_the_request(self, priced_registry):
        done = threading.Event()

        def shadow():
            cost_tracker.record("gpt-4o", 5000, 5000)  # fresh thread = empty context, like the shadow crew
            done.set()

        with cost_tracker.track_request() as usage:
            threading.Thread(target=shadow).start()
            assert done.wait(5)
            cost_tracker.record("gpt-4o", 10, 10)
        assert usage.total_tokens == 20 and cost_tracker.snapshot()["total_tokens"] == 10020

    def test_context_is_inherited_by_copied_contexts(self, priced_registry):
        import contextvars

        with cost_tracker.track_request() as usage:
            t = threading.Thread(target=contextvars.copy_context().run, args=(cost_tracker.record, "gpt-4o", 7, 3))
            t.start()
            t.join(5)
        assert usage.total_tokens == 10  # CrewAI's tool pool and event bus run handlers on copied contexts

    def test_no_request_context_is_harmless(self, priced_registry):
        cost_tracker.record("gpt-4o", 1, 1)
        assert cost_tracker._request_usage.get() is None


# =========================================================================== secrets hygiene
class TestKeyHints:
    def test_no_key_fragment_reaches_the_ui(self):
        from app.views.admin import key_hint

        assert "…" not in key_hint(True) and key_hint(True) == "Currently set. Leave empty to keep."


# =========================================================================== agile fallback
class TestAgileFallback:
    def test_months_shift_on_agile_project_without_stories(self, dataset):
        ctx.set_portfolio_context(dataset, {})  # P-1002 is Agile and has no Stories
        result = sim.agile_vs_waterfall_shift("P-1002", months_delta=3)
        assert result.startswith("RESULT") and "ERROR" not in result and "[Fallback]" in result
        assert "+3.0 months" in result
        assert ctx.get_ledger()[0]["field"] == "planned_end_date"

    def test_capacity_change_on_agile_project_uses_time_based_forecast(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        result = sim.agile_vs_waterfall_shift("P-1002", capacity_change_pct=-20)
        assert "Time-based forecast" in result and "ERROR" not in result and "Shift: +" in result

    def test_agile_with_velocity_still_uses_story_points(self, dataset):
        dataset.stories = pd.DataFrame([
            {"story_number": "S-1", "project_number": "P-1002", "story_points": 8, "state": "Complete"},
            {"story_number": "S-2", "project_number": "P-1002", "story_points": 13, "state": "Open"},
        ])
        ctx.set_portfolio_context(dataset, {})
        result = sim.agile_vs_waterfall_shift("P-1002", months_delta=1)
        assert "[Agile] Velocity" in result and "[Fallback]" not in result


# =========================================================================== cycle-safe graph queries
class TestCycleSafeQueries:
    @pytest.fixture
    def cyclic_graph(self, tmp_path, dataset, monkeypatch):
        dataset.dependencies = pd.DataFrame([
            {"dependency_number": "D-1", "successor_project_number": "P-1001", "predecessor_project_number": "P-1002",
             "dependency_type": "Hard", "status": "On Track"},
            {"dependency_number": "D-2", "successor_project_number": "P-1002", "predecessor_project_number": "P-1001",
             "dependency_type": "Soft", "status": "At Risk"},  # cycle A -> B -> A
            {"dependency_number": "D-3", "successor_project_number": "P-1003", "predecessor_project_number": "P-1001",
             "dependency_type": "Hard", "status": "On Track"},
            {"dependency_number": "D-4", "successor_project_number": "P-1003", "predecessor_project_number": "P-1001",
             "dependency_type": "Soft", "status": "On Track"},  # duplicate direct edge
        ])
        portfolio_graph.pin(dataset)
        ctx.set_portfolio_context(dataset, {})
        return dataset

    def test_downstream_never_lists_the_project_itself_and_counts_budget_once(self, cyclic_graph):
        text = graph_tools.downstream_impact("P-1001")
        assert "2 projects affected" in text
        assert text.count("P-1003") == 1 and "P-1001 (" not in text.split("\n", 1)[1]
        assert "€1,220,000" in text  # 800k (P-1002) + 420k (P-1003), not doubled
        assert "Hard/Soft dependency" in text

    def test_upstream_is_cycle_safe(self, cyclic_graph):
        text = graph_tools.upstream_bottlenecks("P-1001")
        assert "waits for 1 projects" in text and "P-1001 (" not in text.split("\n", 1)[1]
        assert "BOTTLENECK" not in text  # P-1002's edge towards P-1001 is 'On Track'

    def test_task_impact_groups_each_dependent_project_once(self, cyclic_graph):
        text = graph_tools.task_level_impact("P-1001")
        dependent = text.split("Dependent projects:", 1)[1]
        assert dependent.count("P-1002") == 1 and "P-1001 (" not in dependent


# =========================================================================== bounded context
class TestBoundedPortfolioContext:
    def _view(self, n):
        return pd.DataFrame({
            "project_number": [f"P-{i:04d}" for i in range(n)], "project_name": [f"Project {i}" for i in range(n)],
            "status": ["Red" if i % 50 == 0 else "Green" for i in range(n)], "risk_score": [i % 5 + 1 for i in range(n)],
            "budget_cost_eur": [float(i * 1000) for i in range(n)],
        })

    def test_large_portfolio_is_capped_with_named_projects_kept(self):
        md = portfolio_markdown(self._view(500), focus_ids=["P-0007"])
        rows = [ln for ln in md.splitlines() if ln.startswith("| P-")]
        assert len(rows) == PORTFOLIO_CONTEXT_MAX_ROWS
        assert any(ln.startswith("| P-0007 ") for ln in rows)  # named project stays although low risk/budget
        assert any(ln.startswith("| P-0000 ") for ln in rows)  # red project ranks first
        assert "Context limited to 25 of 500 projects" in md

    def test_small_portfolio_is_unchanged(self):
        md = portfolio_markdown(self._view(11))
        assert len([ln for ln in md.splitlines() if ln.startswith("| P-")]) == 11 and "Context limited" not in md


# =========================================================================== client cache
class TestLLMCache:
    def test_same_model_key_and_temperature_share_one_client(self):
        from app.llm.client import client_for

        update_runtime_config(openai_api_key="sk-test", temperature=0.2, agents={"analyst": {"main": "gpt-4o"}, "simulator": {"main": "gpt-4o"}})
        agents.clear_llm_caches()
        a = client_for(agents.build_analyst_agent().model)
        b = client_for(agents.build_simulator_agent().model)
        assert a is b and a.model == "gpt-4o"

    def test_key_or_temperature_change_gives_a_new_client(self):
        from app.llm import client as llm_client

        update_runtime_config(openai_api_key="sk-test", temperature=0.2, agents={"analyst": {"main": "gpt-4o"}})
        agents.clear_llm_caches()
        first = llm_client.client_for("gpt-4o")
        update_runtime_config(temperature=0.5)
        second = llm_client.client_for("gpt-4o")
        update_runtime_config(openai_api_key="sk-other")
        third = llm_client.client_for("gpt-4o")
        assert first is not second and second is not third and third._sdk.api_key == "sk-other"
        assert not any("sk-other" in key for key in llm_client._clients)  # the key itself is never a cache key

    def test_missing_key_is_reported_before_caching(self):
        update_runtime_config(openai_api_key="", agents={"analyst": {"main": "gpt-4o"}})
        with pytest.raises(agents.LLMConfigError, match="OPENAI_API_KEY"):
            agents.build_analyst_agent()
