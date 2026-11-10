"""Critical paths that only run when something goes wrong: a failing LLM crew, a thread without its own scenario
context, failing RLS filters and PII masking, an unavailable dependency graph, and Force Simulation requested by a PM.
(The Approve & Apply write path is covered as an AppTest in test_admin_console.py.)"""
from __future__ import annotations

import contextvars
import logging
import threading

import pandas as pd
import pytest

from app.core import security
from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_PM, ROLE_PMO, apply_row_level_security, mask_pii_data
from app.crew import pipeline
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools


@pytest.fixture
def no_graph(monkeypatch):
    def unavailable():
        raise graph_tools.GraphUnavailable("Graph disabled in test")

    monkeypatch.setattr(graph_tools, "get_graph", unavailable)


class TestCrewFailure:
    def test_a_failing_crew_falls_back_to_the_offline_verdict(self, dataset, no_graph, monkeypatch):
        offline = pipeline.run_steering_pipeline("Cut the budget of Alpha Platform by 20 %", dataset, ROLE_PMO, "PMO")

        def crew_down(*args, **kwargs):
            raise RuntimeError("provider said: invalid key sk-ant-secret")

        monkeypatch.setattr(pipeline, "_run_agents", crew_down)
        monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))
        result = pipeline.run_steering_pipeline("Cut the budget of Alpha Platform by 20 %", dataset, ROLE_PMO, "PMO")
        assert result.mode == "offline"
        assert "LLM pipeline failed (RuntimeError) – offline fallback active." in result.warnings
        assert not any("sk-ant" in w for w in result.warnings)  # the exception text stays in the system log
        assert result.verdict.to_dict() == offline.verdict.to_dict()
        assert result.evidence and result.evidence == offline.evidence  # the same evidence as a plain offline run


class TestContextPerThread:
    def test_two_threads_keep_their_own_context(self, dataset_factory, project_factory):
        seen: dict[str, list[str]] = {}
        ready = threading.Barrier(2)

        def run(name: str, pid: str) -> None:
            ctx.set_portfolio_context(dataset_factory([project_factory(project_number=pid)]), {})
            ready.wait(5)  # both contexts are set before either thread reads
            ctx.record_change(pid, "status", "Red", name)
            seen[name] = [c["project_id"] for c in ctx.get_ledger()]

        threads = [threading.Thread(target=run, args=(n, p)) for n, p in (("a", "P-A"), ("b", "P-B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert seen == {"a": ["P-A"], "b": ["P-B"]}

    def test_a_thread_without_context_never_sees_another_sessions_scenario(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        errors: list = []

        def read() -> None:
            try:
                ctx.get_dataset()
            except ctx.ProjectNotFound as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=read)
        worker.start()
        worker.join(10)
        assert errors == ["ERROR: No portfolio context loaded."]
        carried = threading.Thread(target=contextvars.copy_context().run, args=(read,))  # taken along on purpose
        carried.start()
        carried.join(10)
        assert len(errors) == 1


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot render")


class TestFailClosed:
    def test_rls_error_returns_no_rows(self, caplog):
        df = pd.DataFrame({"project_number": ["P-1"], "project_manager": [_Unprintable()]})
        with caplog.at_level(logging.ERROR):
            assert apply_row_level_security(df, ROLE_PM, "Priya Raman").empty
        assert "RLS filtering failed" in caplog.text

    def test_rls_unknown_role_returns_no_rows(self):
        df = pd.DataFrame({"project_number": ["P-1"], "project_manager": ["Priya Raman"]})
        assert apply_row_level_security(df, "Guest", "Priya Raman").empty

    def test_pii_masking_error_withholds_the_text(self, monkeypatch):
        def broken():
            raise RuntimeError("registry corrupt")

        monkeypatch.setattr(security, "registered_pii", broken)
        assert mask_pii_data("Priya Raman cuts the budget") == "[TEXT_WITHHELD_PII_MASKING_ERROR]"


class TestForceSimulation:
    def test_force_by_a_project_manager_is_downgraded(self, dataset, no_graph):
        result = pipeline.run_steering_pipeline("Cut the budget of Gamma Governance by 10 %", dataset, ROLE_PM,
                                                "Priya Raman", force=True)
        assert ("'Force simulation' is reserved for the PMO role – request executed with the quality gate active."
                in result.warnings)
        # the quality gate stays active: the incomplete project is blocked instead of being simulated
        assert list(result.verdict.conditions) == ["Fix the master data of the blocked project(s), then re-run the scenario."]
