"""Dependency deadlines on schedule shifts, NPV & benefit timing, on hold, portfolio cut."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_PMO
from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import portfolio_tools as pt
from app.crew.tools import simulator_tools as sim
from app.crew.tools.portfolio_tools import rnp_severity
from app.crew.verdict import verdict_from_context
from app.graph import portfolio_graph

NAMES = {"P-1001": "Alpha Platform", "P-1002": "Beta Billing", "P-1003": "Gamma Governance"}


def _facts(kind):
    return [f for f in ctx.get_facts() if f["type"] == kind]


@pytest.fixture
def graph_with_deadlines(tmp_path, dataset, monkeypatch):
    """P-1001 (ends 2030-06-30) is predecessor of P-1002 (required 2030-08-15) and P-1003 (required 2030-01-01)."""
    dataset.dependencies = pd.DataFrame([
        {"dependency_number": "D-1", "successor_project_number": "P-1002", "predecessor_project_number": "P-1001",
         "dependency_type": "Hard", "status": "On Track", "required_by_date": "2030-08-15"},
        {"dependency_number": "D-2", "successor_project_number": "P-1003", "predecessor_project_number": "P-1001",
         "dependency_type": "Soft", "status": "At Risk", "required_by_date": "2030-01-01"},
    ])
    dataset.dependencies["required_by_date"] = pd.to_datetime(dataset.dependencies["required_by_date"])
    portfolio_graph.pin(dataset)
    ctx.set_portfolio_context(dataset, {})
    return dataset


# =========================================================================== 1. dependency deadlines
class TestDeadlineCheck:
    def test_delay_reports_new_and_existing_deadline_breaches(self, graph_with_deadlines):
        text = sim.timeline_shift("P-1001", 3)  # new end ≈ 2030-09-29
        assert "⚠ RISK: P-1002 misses the required-by date 2030-08-15 of its Hard dependency" in text
        assert "⚠ RISK: P-1003 already waits beyond its required-by date 2030-01-01 (Soft)" in text
        breaches = {f["successor"]: f for f in _facts("deadline_breach")}
        assert breaches["P-1002"]["new"] is True and breaches["P-1003"]["new"] is False
        v = verdict_from_context()
        assert v.decision == "CONDITIONAL GO"
        assert any("Agree a new delivery date or a mitigation with P-1002" in c for c in v.conditions)

    def test_short_delay_within_slack_is_fine(self, graph_with_deadlines):
        text = sim.timeline_shift("P-1001", 1)  # ≈ 2030-07-30 < 2030-08-15
        assert "✓ P-1002: required by 2030-08-15 – still met" in text
        assert [f["successor"] for f in _facts("deadline_breach")] == ["P-1003"]  # the already-late one only

    def test_acceleration_does_not_query_deadlines(self, graph_with_deadlines):
        assert "Dependency deadlines" not in sim.timeline_shift("P-1001", -1)

    def test_graph_unavailable_is_reported_not_raised(self, dataset, monkeypatch):
        def down(_pid):
            raise graph_tools.GraphUnavailable("Graph not built: offline")

        monkeypatch.setattr(graph_tools, "successor_deadlines", down)
        ctx.set_portfolio_context(dataset, {})
        assert "Dependency deadline check not possible" in sim.timeline_shift("P-1001", 2)
        assert any("Graph database unavailable" in r for r in verdict_from_context().reasons)

    def test_method_aware_forecast_checks_deadlines_too(self, graph_with_deadlines):
        text = sim.agile_vs_waterfall_shift("P-1001", months_delta=3)  # Waterfall project
        assert "misses the required-by date 2030-08-15" in text


# =========================================================================== 2. NPV & benefit timing
class TestNpvTiming:
    def test_delay_lowers_npv_and_roi_from_benefit_plans(self, dataset):
        dataset.benefit_plans = pd.DataFrame([
            {"benefit_plan_number": "B-1", "project_number": "P-1001", "planned_benefit_eur": 300000.0,
             "realization_start_date": pd.Timestamp("2031-01-01"), "owner": "Marco Bianchi"},
            {"benefit_plan_number": "B-2", "project_number": "P-1001", "planned_benefit_eur": 210000.0,
             "realization_start_date": pd.Timestamp("2031-07-01"), "owner": "Marco Bianchi"},
        ])
        dataset.projects["discount_rate_pct"] = 10.0
        dataset.projects["net_present_value_eur"] = 50000.0
        ctx.set_portfolio_context(dataset, {})
        text = sim.timeline_shift("P-1001", 6)
        assert "10 % discount rate, 2 benefit tranche(s) from Benefit_Plans" in text
        change = _facts("npv_change")[0]
        assert change["delta_pv_benefits"] < 0 and change["delta_npv"] < change["delta_pv_benefits"]  # cost of delay too
        assert "NPV: €50,000 -> " in text and "ROI on the forecast (undiscounted, incl. extra cost): 21.4 % -> " in text

    def test_fallback_to_planned_benefit_and_default_rate(self, dataset):
        update_runtime_config(business_rules={"default_discount_rate_pct": 5})
        ctx.set_portfolio_context(dataset, {})
        text = sim.timeline_shift("P-1001", 3)
        assert "5 % discount rate, planned_benefit_eur realised at the planned end" in text

    def test_npv_turning_negative_is_a_condition(self, dataset):
        dataset.projects["net_present_value_eur"] = 1000.0
        ctx.set_portfolio_context(dataset, {})
        text = sim.timeline_shift("P-1001", 12)
        assert "⚠ NPV turns negative for P-1001" in text
        assert any("NPV below zero" in c for c in verdict_from_context().conditions)


# =========================================================================== 3a. on hold
class TestOnHold:
    def test_hold_has_no_exit_costs_but_standby_and_ramp_up(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        text = pt.on_hold("P-1001", 4, 25)
        assert text.startswith("RESULT simulate_on_hold") and "No exit/contract costs" in text
        assert "Capacity kept (standby, 25 %): 0.5 of 2 FTE" in text  # fixture: 2 FTE
        assert "standby cost €25,000" in text  # 0.5 FTE × €12,500 × 4 months
        ledger = {c["field"]: c["new"] for c in ctx.get_ledger()}
        assert ledger["state"] == "On Hold" and ledger["planned_end_date"] > "2030-06-30"
        v = verdict_from_context()
        assert v.decision == "CONDITIONAL GO" and any("Confirm the restart date" in c for c in v.conditions)

    def test_hold_uses_governance_defaults_and_guards(self, dataset):
        update_runtime_config(business_rules={"hold_retention_pct": 50, "hold_restart_ramp_months": 2})
        ctx.set_portfolio_context(dataset, {})
        text = pt.on_hold("P-1001", 3)
        assert "Capacity kept (standby, 50 %)" in text and "+5 months incl. ramp-up" in text
        assert pt.on_hold("P-1001", 0).startswith("INPUT ERROR")  # invalid input, not a business guardrail
        assert pt.on_hold("P-1001", 30).startswith("ERROR")  # above max_hold_months
        assert pt.on_hold("P-1002", 3).startswith("RESULT")  # another, unblocked project in the same context

    def test_blocked_project_is_paused_with_indicative_figures(self, dataset):
        ctx.set_portfolio_context(dataset, {"P-1003": "planning_incomplete"})
        text = pt.on_hold("P-1003", 3)
        assert text.startswith("RESULT") and "Master data incomplete (planning_incomplete) – figures indicative." in text
        v = verdict_from_context()
        assert v.decision == "CONDITIONAL GO"
        assert any(c.startswith("Master data of P-1003 incomplete – figures indicative") for c in v.conditions)

    def test_hold_is_not_a_cancellation_in_the_parser(self, dataset):
        plan = pipeline.heuristic_plan("Put Alpha Platform on hold for 4 months", dataset.projects)
        kinds = [i.description.split(":")[0] for i in plan.sub_intents if i.intent_type == "simulation"]
        assert kinds == ["on_hold"]


# =========================================================================== 3b. portfolio cut
@pytest.fixture
def portfolio(dataset_factory, project_factory):
    return dataset_factory([
        project_factory(project_number="P-A", project_name="Mandatory", priority=1, value_score=5,
                        risk_of_not_performing="Regulatory fine"),
        project_factory(project_number="P-B", project_name="Low prio", priority=3, value_score=2,
                        risk_of_not_performing="Nice to have", budget_cost_eur=1_000_000, actual_cost_eur=100_000,
                        total_planned_cost_eur=1_000_000, planned_capital_eur=400_000, planned_operating_eur=600_000,
                        estimate_at_completion_eur=1_000_000),
        project_factory(project_number="P-C", project_name="Mid prio", priority=2, value_score=3,
                        risk_of_not_performing="Contractual penalty", budget_cost_eur=900_000, actual_cost_eur=0,
                        total_planned_cost_eur=900_000, planned_capital_eur=400_000, planned_operating_eur=500_000,
                        estimate_at_completion_eur=900_000),
        project_factory(project_number="P-D", project_name="Mid prio low value", priority=2, value_score=2,
                        risk_of_not_performing="", budget_cost_eur=500_000, actual_cost_eur=0,
                        total_planned_cost_eur=500_000, planned_capital_eur=200_000, planned_operating_eur=300_000,
                        estimate_at_completion_eur=500_000),
    ])


class TestPortfolioCut:
    def test_ranking_and_greedy_selection(self, portfolio):
        ctx.set_portfolio_context(portfolio, {})
        text = pt.portfolio_cut(1_200_000)
        ranked = [ln.strip().split(" ")[1] for ln in text.splitlines() if ln.strip()[:2] in ("1.", "2.", "3.")]
        assert ranked == ["P-B", "P-D"]  # priority 3 first, then priority 2 with the lower value score
        assert "P-A (protected: priority 1)" in text and "Next candidates" in text and "P-C" in text
        # P-B: (1,000k − 100k) × 0.92 = 828k; P-D: 500k × 0.92 = 460k; cumulative 1,288k ≥ 1,200k -> stop
        assert "net release €828,000" in text and "cumulative €1,288,000" in text
        assert "cut list of 2 project stop(s), net release €1,288,000" in text
        cut = _facts("portfolio_cut")[0]
        assert cut["projects"] == ["P-B", "P-D"] and cut["achieved"] >= 1_200_000
        assert {c["project_id"] for c in ctx.get_ledger() if c["new"] == "Canceled"} == {"P-B", "P-D"}

    def test_unreachable_target_is_a_condition(self, portfolio):
        ctx.set_portfolio_context(portfolio, {})
        text = pt.portfolio_cut(50_000_000)
        assert "⚠ Savings target not reached" in text
        assert any("Find the missing" in c for c in verdict_from_context().conditions)

    def test_protection_rule_and_quality_gate(self, portfolio):
        update_runtime_config(business_rules={"cut_protected_max_priority": 0})
        ctx.set_portfolio_context(portfolio, {"P-B": "missing_business_case"})
        text = pt.portfolio_cut(10_000_000)
        assert "P-B (quality gate)" in text and "protected" not in text
        assert "P-A (Mandatory)" in text  # no longer protected

    def test_risk_of_not_performing_keywords(self):
        assert rnp_severity("Contractual penalty and customer churn") == 3
        assert rnp_severity("Security exposure grows") == 3
        assert rnp_severity("Service cost grows") == 2 and rnp_severity(None) == 1

    def test_parser_and_guided_scenario(self, portfolio):
        plan = pipeline.heuristic_plan("Find €2M savings in the portfolio", portfolio.projects)
        assert [i.description for i in plan.sub_intents if i.intent_type == "simulation"] == ["portfolio_cut"]
        scenario = build_guided_scenario("portfolio_cut", None, {}, target_eur=1_200_000)
        assert scenario.plan.sub_intents[0].parameters == {"action": "portfolio_cut", "target_eur": 1_200_000.0,
                                                         "method": "ranked"}
        result = pipeline.run_steering_pipeline(scenario.request, portfolio, ROLE_PMO, "PMO", plan=scenario.plan)
        assert "Portfolio cut | " in result.decision_paper and result.verdict.decision == "CONDITIONAL GO"
        assert {c["project_id"] for c in result.proposed_changes} == {"P-B", "P-D"}


# =========================================================================== 4. schedule cost
def _eac_change(project: str = "P-1001"):
    return [c for c in ctx.get_ledger() if c["field"] == "estimate_at_completion_eur" and c["project_id"] == project]


def _short_project_dataset(dataset_factory, project_factory, months_left: float):
    from app.core import clock

    end = sim._add_months(clock.today(), months_left).isoformat()
    ds = dataset_factory([project_factory(planned_end_date=end, current_fte=4.0)])
    ctx.set_portfolio_context(ds, {})
    return ds


class TestScheduleCost:
    def test_capacity_cut_stretches_duration_without_extra_cost(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        text = sim.agile_vs_waterfall_shift("P-1001", capacity_change_pct=-50)
        assert "same effort, longer duration, no extra cost" in text
        assert all(abs(c["new"] - 420000) < 1000 for c in _eac_change())

    def test_capacity_increase_shortens_duration_at_equal_cost(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        sim.agile_vs_waterfall_shift("P-1001", capacity_change_pct=50)
        end = [c for c in ctx.get_ledger() if c["field"] == "planned_end_date"]
        assert end and end[0]["new"] < "2030-06-30"
        assert all(abs(c["new"] - 420000) < 1000 for c in _eac_change())

    def test_scope_increase_adds_share_of_remaining_cost(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        sim.agile_vs_waterfall_shift("P-1001", scope_change_pct=20)
        assert _eac_change()[0]["new"] == pytest.approx(420000 + 0.2 * 182000, abs=1)

    def test_acceleration_beyond_remaining_duration_is_rejected(self, dataset_factory, project_factory):
        _short_project_dataset(dataset_factory, project_factory, 8)
        for text in (sim.agile_vs_waterfall_shift("P-1001", months_delta=-12), sim.timeline_shift("P-1001", -12)):
            assert text.startswith("ERROR: Acceleration by 12 months impossible")
        assert ctx.get_ledger() == []

    def test_acceleration_raises_the_forecast(self, dataset_factory, project_factory):
        _short_project_dataset(dataset_factory, project_factory, 8)
        text = sim.agile_vs_waterfall_shift("P-1001", months_delta=-5)
        assert "acceleration cost" in text
        assert _eac_change()[0]["new"] > 420000

    def test_schedule_slider_stops_at_remaining_duration(self, dataset_factory, project_factory):
        from app.views.console import min_schedule_months as _min_schedule_months

        short = _short_project_dataset(dataset_factory, project_factory, 3)
        assert _min_schedule_months(short, "P-1001") == -2
        long = dataset_factory()
        assert _min_schedule_months(long, None) == -36  # the schedule guardrail (business rule)
        update_runtime_config(business_rules={"max_timeline_shift_months": 6, "max_hold_months": 5})
        assert _min_schedule_months(long, "P-1001") == -6
