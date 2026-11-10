"""Fact engine: fact superseding, timeline EAC, portfolio-cut domino and the reference date."""
from __future__ import annotations

import datetime as dt
from datetime import date

import pandas as pd
import pytest

from app.core import clock
from app.core.security import ROLE_PMO
from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import portfolio_tools as pt
from app.crew.tools import simulator_tools as sim
from app.crew.verdict import verdict_from_context
from app.graph import portfolio_graph

AS_OF = date(2029, 6, 30)


def _types(kind: str) -> list[dict]:
    return [f for f in ctx.get_facts() if f["type"] == kind]


# =========================================================================== fact superseding
class TestFactSuperseding:
    def test_cut_then_increase_in_one_request_judges_the_net_effect(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        sim.budget_change("P-1002", -20, is_perc=True)  # 640k vs EAC 800k -> 25 % overrun (NO-GO on its own)
        assert verdict_from_context().decision == "NO-GO"
        sim.budget_change("P-1002", 25, is_perc=True)   # 640k × 1.25 = 800k -> EAC covered again
        assert _types("budget_overrun") == []
        assert _types("budget_change")[0]["delta"] == 0
        assert verdict_from_context().decision == "GO"

    def test_overload_of_step_one_disappears_after_a_back_transfer(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        sim.resource_transfer("P-1001", "P-1003", 1)  # P-1001: 300 h demand on 160 h capacity -> 188 %
        assert [f["project"] for f in _types("overload")] == ["P-1001"]
        step_one = ctx.export_state(["move 1 FTE"])

        ctx.set_portfolio_context(dataset, {}, base_state=step_one)
        text = sim.resource_transfer("P-1003", "P-1001", 1)
        assert "Utilisation target: 188 % -> 94 %" in text  # starts from the scenario, not from the original data
        assert _types("overload") == []
        assert not any("utilisation" in r for r in verdict_from_context().reasons)

    def test_a_stopped_project_drops_its_budget_and_schedule_findings(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        sim.budget_change("P-1002", -20, is_perc=True)
        assert _types("budget_overrun")
        sim.project_cancellation("P-1002")
        assert _types("budget_overrun") == [] and _types("negative_roi") == []

    def test_graph_outage_is_cleared_once_the_graph_answers(self, dataset, tmp_path, monkeypatch):
        portfolio_graph.pin(dataset)
        real_graph = graph_tools.get_graph

        def down(*_args, **_kwargs):
            raise graph_tools.GraphUnavailable("Graph not built: offline")

        ctx.set_portfolio_context(dataset, {})
        monkeypatch.setattr(graph_tools, "get_graph", down)
        graph_tools.downstream_impact("P-1001")
        assert _types("graph_unavailable")
        monkeypatch.setattr(graph_tools, "get_graph", real_graph)
        graph_tools.downstream_impact("P-1001")
        assert _types("graph_unavailable") == []
        assert not any("Graph database unavailable" in r for r in verdict_from_context().reasons)


# =========================================================================== timeline EAC
@pytest.fixture
def late_project(dataset_factory, project_factory):
    """Budget = EAC = €100k, €90k still to spend over the last 12 months before the end (2030-06-30)."""
    return dataset_factory([project_factory(
        project_number="P-T", project_name="Tight Timeline", budget_cost_eur=100_000, actual_cost_eur=10_000,
        estimate_at_completion_eur=100_000, estimate_to_completion_eur=90_000, total_planned_cost_eur=100_000,
        planned_capital_eur=40_000, planned_operating_eur=60_000)])


class TestTimelineEac:
    def test_delay_writes_the_new_eac_and_a_big_overrun_is_no_go(self, late_project):
        ctx.set_portfolio_context(late_project, {}, as_of=AS_OF)
        text = sim.timeline_shift("P-T", 6)
        eac = next(c for c in ctx.get_ledger() if c["field"] == "estimate_at_completion_eur")
        assert eac["old"] == 100_000 and eac["new"] > 140_000
        assert "⚠ Budget does not cover the forecast after the schedule change" in text
        v = verdict_from_context()
        assert v.label == "NO-GO"
        assert "above the NO-GO threshold of 15 %" in v.reasons[1]

    def test_small_overrun_is_a_condition_and_a_budget_increase_clears_it(self, late_project):
        ctx.set_portfolio_context(late_project, {}, as_of=AS_OF)
        sim.timeline_shift("P-T", 1)  # ≈ €7.4k cost of delay -> 7 % overrun
        v = verdict_from_context()
        assert v.decision == "CONDITIONAL GO" and any("Close the forecast gap" in c for c in v.conditions)
        sim.budget_change("P-T", 10_000)  # the budget now covers the new EAC
        assert _types("budget_overrun") == []


# =========================================================================== portfolio cut domino
@pytest.fixture
def cut_with_dependents(tmp_path, dataset_factory, project_factory, monkeypatch):
    ds = dataset_factory([
        project_factory(project_number="P-B", project_name="Low prio", priority=3, value_score=2),
        project_factory(project_number="P-S1", project_name="Successor one", priority=1),
        project_factory(project_number="P-S2", project_name="Successor two", priority=1),
    ])
    ds.dependencies = pd.DataFrame([
        {"dependency_number": f"D-{i}", "successor_project_number": succ, "predecessor_project_number": "P-B",
         "dependency_type": "Hard", "status": "On Track", "required_by_date": pd.Timestamp("2030-08-15")}
        for i, succ in enumerate(("P-S1", "P-S2"))
    ])
    portfolio_graph.pin(ds)
    return ds


class TestPortfolioCutDomino:
    def test_every_stopped_project_gets_the_domino_rule(self, cut_with_dependents):
        ctx.set_portfolio_context(cut_with_dependents, {})
        text = pt.portfolio_cut(100_000)
        assert "P-B: 2 projects affected (domino effect)" in text
        assert _types("domino") == [{"type": "domino", "project": "P-B", "affected": 2, "direct": 2}]
        v = verdict_from_context()
        assert v.decision == "CONDITIONAL GO"
        assert "Align the 2 dependent project(s) of P-B before execution." in v.conditions

    def test_guided_cut_runs_with_graph_analysis(self, cut_with_dependents):
        scenario = build_guided_scenario("portfolio_cut", None, {}, target_eur=100_000)
        assert scenario.plan.requires_graph_analysis
        result = pipeline.run_steering_pipeline(scenario.request, cut_with_dependents, ROLE_PMO, "PMO", plan=scenario.plan)
        assert any("dependent project(s) of P-B" in c for c in result.verdict.conditions)


# =========================================================================== reference date
class TestReferenceDate:
    def _run(self, ds, monkeypatch, system_day: date):
        frozen = dt.datetime.combine(system_day, dt.time(12, 0), tzinfo=dt.UTC)
        monkeypatch.setattr(clock, "now", lambda: frozen)
        return pipeline.run_steering_pipeline("Delay Beta Billing by 3 months", ds, ROLE_PMO, "PMO", as_of=AS_OF)

    def test_same_as_of_gives_the_same_verdict_on_any_system_day(self, dataset, monkeypatch):
        first = self._run(dataset, monkeypatch, date(2026, 10, 1))
        second = self._run(dataset, monkeypatch, date(2028, 3, 15))
        assert first.verdict.to_dict() == second.verdict.to_dict()
        assert first.verdict.to_dict()["as_of"] == "2029-06-30"
        assert first.proposed_changes == [{**c, "timestamp": first.proposed_changes[i]["timestamp"]}
                                          for i, c in enumerate(second.proposed_changes)]
        assert "**Date:** 2029-06-30" in first.decision_paper

    def test_default_reference_date_is_today(self, dataset, monkeypatch):
        monkeypatch.setattr(clock, "now", lambda: dt.datetime(2027, 5, 4, 9, 30, tzinfo=dt.UTC))
        ctx.set_portfolio_context(dataset, {})
        assert ctx.as_of() == date(2027, 5, 4)
        assert verdict_from_context().as_of == "2027-05-04"
