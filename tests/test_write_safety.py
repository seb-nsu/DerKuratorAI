"""Regression tests: compare-and-write, snapshots, content-based staleness, reader/writer lock, empty resource
plans, internal errors, zero budget."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta

import pandas as pd
import pytest

from app.core import clock, proposals, snapshots
from app.core.security import ROLE_PM, ROLE_PMO
from app.crew import pipeline
from app.crew.tasks import IntentPlan
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import simulator_tools as sim
from app.crew.tools.data_steward_tools import run_all_checks
from app.data_adapters import DataConflictError, JsonPortfolioAdapter


# =========================================================================== edge cases
class TestEmptyResourcePlans:
    def test_current_allocation_keeps_its_columns(self, dataset):
        dataset.resource_plans = pd.DataFrame([])
        alloc = dataset.current_allocation()
        assert alloc.empty and {"project_number", "assignment_group", "allocated_fte"} <= set(alloc.columns)

    def test_tools_work_without_resource_plans(self, dataset):
        dataset.resource_plans = pd.DataFrame([])
        ctx.set_portfolio_context(dataset, {})
        # no plans -> 0 FTE: a clean business answer instead of the former KeyError crash
        assert sim.resource_transfer("P-1001", "P-1002", 1.0).startswith("ERROR: Insufficient resources")
        cancel = sim.project_cancellation("P-1001")
        assert cancel.startswith("RESULT") and "from n/a" in cancel


class TestInternalErrors:
    def _paper(self, dataset, sim_results):
        ctx.set_portfolio_context(dataset, {})
        return pipeline._render_offline_paper("req", IntentPlan(summary="s"), sim_results, [], run_all_checks(dataset), "PMO", [])

    def test_tool_crash_is_reported_as_internal_error(self, dataset, monkeypatch):
        ctx.set_portfolio_context(dataset, {})
        monkeypatch.setattr(sim, "_budget_delta", lambda *a, **k: 1 / 0)
        assert sim.budget_change("P-1001", -10, is_perc=True).startswith("INTERNAL ERROR:")

    def test_internal_error_gives_analysis_incomplete_not_no_go(self, dataset):
        paper = self._paper(dataset, ["INTERNAL ERROR: calculation defect in budget_change (ZeroDivisionError)"])
        decision_part = paper.split("## 6.", 1)[0]  # section 6 lists the business assumptions (incl. rule names)
        assert "**Recommendation:** ANALYSIS INCOMPLETE" in paper and "NO-GO" not in decision_part
        assert "internal error(s)" in paper

    def test_guardrail_error_still_gives_no_go(self, dataset):
        assert "**Recommendation:** NO-GO" in self._paper(dataset, ["ERROR: Budget undercut – x"])
        # "ERROR:" on a later line (e.g. method-aware forecast) is still a guardrail
        assert "NO-GO" in self._paper(dataset, ["RESULT x\n- Agile component not calculable\nERROR: No forecast possible."])

    def test_ai_estimate_internal_error_is_not_a_guardrail(self, dataset):
        paper = self._paper(dataset, [f"{sim.KI_PREFIX} INTERNAL ERROR: estimate not possible"])
        assert "ANALYSIS INCOMPLETE" in paper


class TestZeroBudget:
    def test_zero_budget_is_treated_as_missing(self, dataset_factory, project_factory):
        ctx.set_portfolio_context(dataset_factory([project_factory(budget_cost_eur=0)]), {})
        result = sim.budget_change("P-1001", -10, is_perc=True)
        assert result.startswith("ERROR: Data quality") and "budget_cost_eur" in result
        assert ctx.get_ledger() == []  # no fictitious budget proposed

    def test_zero_actual_cost_stays_legitimate(self, dataset_factory, project_factory):
        ctx.set_portfolio_context(dataset_factory([project_factory(actual_cost_eur=0, estimate_to_completion_eur=420000)]), {})
        assert sim.budget_change("P-1001", -10, is_perc=True).startswith("RESULT")


# =========================================================================== data source writes
@pytest.fixture
def json_source(tmp_path):
    path = tmp_path / "portfolio.json"
    data = {"Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                          "budget_cost_eur": 800000, "planned_end_date": "2030-06-30", "state": "Open"}],
            "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _change(field, old, new, pid="P-1"):
    return {"project_id": pid, "table": "Projects", "field": field, "old": old, "new": new, "source_tool": "test"}


def _project(path):
    return json.loads(path.read_text(encoding="utf-8"))["Projects"][0]


def _approve(adapter, changes, user="PMO-A", **kwargs):
    """Governed fields are written only as the PMO approval of another person's proposal (its stored net change set)."""
    entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "test", "", None, changes)
    return adapter.apply_changes(entry["changes"], user=user, role=ROLE_PMO, proposal_id=entry["id"], **kwargs)


class TestCompareAndWrite:
    def test_matching_old_values_are_written_with_snapshot(self, json_source):
        result = _approve(JsonPortfolioAdapter(json_source),
            [_change("budget_cost_eur", 800000.0, 720000.0), _change("planned_end_date", "2030-06-30T00:00:00", "2030-09-30")],
            reason="approve_apply")
        assert result.applied == 2 and result.snapshot_id
        assert _project(json_source)["budget_cost_eur"] == 720000.0
        assert snapshots.get_snapshot(result.snapshot_id)["user"] == "PMO-A"

    def test_second_pmo_with_stale_values_is_rejected_and_nothing_is_written(self, json_source):
        adapter = JsonPortfolioAdapter(json_source)
        _approve(adapter, [_change("budget_cost_eur", 800000, 720000)], user="PMO-A")
        before = json_source.read_text(encoding="utf-8")
        with pytest.raises(DataConflictError) as exc:  # PMO B simulated on the old 800k
            _approve(adapter, [_change("budget_cost_eur", 800000, 900000)], user="PMO-B")
        assert exc.value.conflicts[0]["current"] == 720000.0 and "nothing was written" in str(exc.value)
        assert json_source.read_text(encoding="utf-8") == before

    def test_first_old_value_per_field_is_the_reference(self, json_source):
        chained = [_change("budget_cost_eur", 800000, 750000), _change("budget_cost_eur", 750000, 700000)]
        assert _approve(JsonPortfolioAdapter(json_source), chained).applied == 1
        assert _project(json_source)["budget_cost_eur"] == 700000.0

    def test_missing_project_is_a_conflict(self, json_source):
        with pytest.raises(DataConflictError):
            _approve(JsonPortfolioAdapter(json_source), [_change("state", "Open", "Canceled", pid="P-404")])

    def test_concurrent_writers_never_lose_an_update(self, json_source):
        adapter, outcomes = JsonPortfolioAdapter(json_source), []

        def write(new):
            try:
                _approve(adapter, [_change("budget_cost_eur", 800000, new)])
                outcomes.append("ok")
            except DataConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=write, args=(v,)) for v in (700000, 900000)]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        assert sorted(outcomes) == ["conflict", "ok"]  # exactly one wins, the other is told – no silent overwrite


class TestSnapshots:
    def test_restore_brings_back_the_old_state_and_is_itself_undoable(self, json_source):
        original = json_source.read_text(encoding="utf-8")
        result = _approve(JsonPortfolioAdapter(json_source), [_change("budget_cost_eur", 800000, 1)])
        restored = snapshots.restore_snapshot(result.snapshot_id, json_source, user="admin")
        assert json_source.read_text(encoding="utf-8") == original
        assert _project(json_source)["budget_cost_eur"] == 800000
        undo = snapshots.get_snapshot(restored["previous_state"]["id"])
        assert undo and undo["reason"].startswith("pre-restore")

    def test_manifest_records_change_set_and_inverse(self, json_source):
        result = _approve(JsonPortfolioAdapter(json_source), [_change("state", "Open", "Canceled")], user="u")
        entry = snapshots.get_snapshot(result.snapshot_id)
        assert entry["inverse"] == [{"project_id": "P-1", "table": "Projects", "field": "state", "new": "Open"}]
        assert entry["sha256"] and entry["source"] == "portfolio.json"

    def test_corrupt_snapshot_is_never_restored(self, json_source):
        entry = snapshots.create_snapshot(json_source, "manual")
        (snapshots.snapshot_dir() / entry["file"]).write_text("{tampered", encoding="utf-8")
        with pytest.raises(snapshots.SnapshotError, match="checksum"):
            snapshots.restore_snapshot(entry["id"], json_source)

    def test_snapshot_of_other_source_is_rejected(self, json_source, tmp_path):
        other = tmp_path / "other.json"
        other.write_text("{}", encoding="utf-8")
        entry = snapshots.create_snapshot(other, "manual")
        with pytest.raises(snapshots.SnapshotError, match="belongs to"):
            snapshots.restore_snapshot(entry["id"], json_source)

    def test_retention_prunes_oldest_files(self, json_source, monkeypatch):
        monkeypatch.setattr(snapshots, "MAX_SNAPSHOTS", 3)
        start, ids = clock.now(), []
        for i in range(5):  # frozen clock, one second apart: created_at has second resolution
            monkeypatch.setattr(clock, "now", lambda i=i: start + timedelta(seconds=i))
            ids.append(snapshots.create_snapshot(json_source, "manual")["id"])
        snapshots.prune(keep=3)
        # the newest 3 plus the first one (the state the instance started with, kept for "Reset demo data")
        assert [s["id"] for s in snapshots.list_snapshots()] == ids[::-1][:3] + [ids[0]]


# =========================================================================== staleness by content
class TestContentStaleness:
    @pytest.fixture
    def source(self, tmp_path, monkeypatch, dataset):
        import app.data_adapters as adapters
        from app.graph import portfolio_graph

        path = tmp_path / "portfolio.json"
        path.write_text('{"x": 1}', encoding="utf-8")
        loads = []

        class Provider:
            def load_dataset(self):
                loads.append(1)
                return dataset

        monkeypatch.setattr(portfolio_graph, "_source", lambda: ("json", path))
        monkeypatch.setattr(adapters, "get_data_provider", lambda *a, **k: Provider())
        return path, loads

    def test_touch_or_future_mtime_does_not_trigger_a_rebuild(self, source):
        from app.graph import portfolio_graph

        path, loads = source
        first = portfolio_graph.get_graph()
        future = time.time() + 3600
        os.utime(path, (future, future))  # clock skew / timestamp-preserving copy
        assert portfolio_graph.get_graph() is first and len(loads) == 1
        path.write_text('{"x": 2}', encoding="utf-8")  # real content change
        assert portfolio_graph.get_graph() is not first and len(loads) == 2

    def test_failed_build_is_not_retried_for_unchanged_source(self, source, monkeypatch):
        import app.data_adapters as adapters
        from app.graph import portfolio_graph

        path, loads = source

        class BrokenProvider:
            def load_dataset(self):
                loads.append(1)
                raise RuntimeError("Found duplicated primary key value P1")

        monkeypatch.setattr(adapters, "get_data_provider", lambda *a, **k: BrokenProvider())
        for _ in range(3):
            with pytest.raises(graph_tools.GraphUnavailable):
                portfolio_graph.get_graph()
        assert len(loads) == 1  # no retry storm
        path.write_text('{"fixed": true}', encoding="utf-8")  # new content -> one new attempt
        with pytest.raises(graph_tools.GraphUnavailable):
            portfolio_graph.get_graph()
        assert len(loads) == 2


