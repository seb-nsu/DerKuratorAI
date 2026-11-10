"""Business rules a steering committee would challenge: cut list selection, project stop, stop + transfer, chained
schedule steps, guardrails for euro amounts, hold figures, FTE from a stopped project, configurable assumptions,
input errors, conditions with owner and due date, all NO-GO reasons, the CapEx clamp and the demo data itself."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core.business_rules import BusinessRules
from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_PMO, apply_rls_to_dataset, mask_dataset_pii
from app.crew.agents import DataStewardAgent
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import portfolio_tools as pt
from app.crew.tools import simulator_tools as sim
from app.crew.tools.data_steward_tools import check_budget_overrun
from app.crew.verdict import compute_verdict, verdict_from_context
from app.data_adapters import JsonPortfolioAdapter
from app.graph.portfolio_graph import build_graph
from scripts import build_demo_portfolio

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)
ERP, BILLING, MARKETPLACE, DEVICES = "PRJ-DEMO-1002", "PRJ-DEMO-1007", "PRJ-DEMO-1009", "PRJ-DEMO-1008"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    dataset = JsonPortfolioAdapter(SOURCE).load_dataset()
    return dataset, build_graph(dataset)


@pytest.fixture
def scenario(demo, monkeypatch):
    """The demo portfolio as the PMO sees it, with the Data Steward's quality gate, at the demo reference date."""
    dataset, graph = demo
    monkeypatch.setattr(graph_tools, "get_graph", lambda: graph)
    view = apply_rls_to_dataset(dataset, ROLE_PMO, "PMO-ADMIN")
    steward = DataStewardAgent().run(view, AS_OF)
    ctx.set_portfolio_context(mask_dataset_pii(view), steward.blocked_reasons, as_of=AS_OF)
    return view


def _project(pid):
    return ctx.get_project(pid)


def _facts(kind):
    return [f for f in ctx.get_facts() if f["type"] == kind]


# =========================================================================== portfolio cut
class TestCutList:
    def test_smallest_combination_spares_the_flagship(self, scenario):
        text = pt.portfolio_cut(500_000)
        assert _facts("portfolio_cut")[0]["projects"] == [MARKETPLACE]  # 34 % complete, not ERP (64 %, €980k sunk)
        assert "net release €667,000" in text and "Surplus over the target: €167,000" in text
        assert ERP not in {c["project_id"] for c in ctx.get_ledger()}

    def test_fewest_stops_then_lowest_ranked(self, dataset_factory, project_factory):
        cheap = dict(budget_cost_eur=300_000, actual_cost_eur=0, total_planned_cost_eur=300_000,
                     planned_capital_eur=100_000, planned_operating_eur=200_000, estimate_at_completion_eur=300_000)
        ctx.set_portfolio_context(dataset_factory([
            project_factory(project_number="P-1", priority=4, value_score=1, **cheap),
            project_factory(project_number="P-2", priority=4, value_score=1, **cheap),
            project_factory(project_number="P-3", priority=2, value_score=5, budget_cost_eur=1_000_000, actual_cost_eur=0,
                            total_planned_cost_eur=1_000_000, planned_capital_eur=400_000, planned_operating_eur=600_000,
                            estimate_at_completion_eur=1_000_000),
        ]), {})
        pt.portfolio_cut(500_000)  # P-1 + P-2 = €552k reach it with two stops of the lowest priority
        assert _facts("portfolio_cut")[0]["projects"] == ["P-3"]  # but one stop is enough
        ctx.set_portfolio_context(ctx.get_dataset(), {})
        pt.portfolio_cut(250_000)
        assert _facts("portfolio_cut")[0]["projects"] == ["P-1"]  # one stop each: the lowest ranked wins


# =========================================================================== project stop
class TestProjectStop:
    def test_stop_with_sunk_costs_and_hard_dependent_is_conditional(self, scenario):
        text = sim.project_cancellation(BILLING)
        cancel = _facts("cancel")[0]
        assert cancel["sunk_pct"] == 52.3 and cancel["hard_dependents"] == 1
        v = verdict_from_context()
        assert v.decision == "CONDITIONAL GO" and "Sunk costs" in text
        assert any(c.startswith(f"Confirm the stop of {BILLING} with the sponsor and book the impairment of €")
                   for c in v.conditions)

    def test_early_stop_without_hard_dependents_is_go(self, dataset_factory, project_factory):
        ctx.set_portfolio_context(dataset_factory([project_factory(actual_cost_eur=40_000)]), {})
        sim.project_cancellation("P-1001")  # 9.5 % sunk, no dependencies
        assert verdict_from_context().decision == "GO"
        update_runtime_config(business_rules={"cancel_conditional_sunk_pct": 5})
        assert verdict_from_context().decision == "CONDITIONAL GO"

    def test_blocked_project_can_be_stopped_with_indicative_figures(self, dataset):
        ctx.set_portfolio_context(dataset, {"P-1003": "planning_incomplete"})
        assert sim.project_cancellation("P-1003").startswith("RESULT")
        assert any("figures indicative" in c for c in verdict_from_context().conditions)


class TestStopAndTransfer:
    def test_transfer_moves_the_net_release_and_needs_one_board_decision(self, scenario):
        from app.crew.pipeline import _execute_intents
        from app.crew.scenarios import build_guided_scenario

        plan = build_guided_scenario("cancel", BILLING, {}, target=ERP).plan
        _execute_intents("", plan.sub_intents)
        target = [c["new"] for c in ctx.get_ledger() if c["project_id"] == ERP and c["field"] == "budget_cost_eur"]
        assert target == [1_850_000 + 579_600]  # (€1,320,000 − €690,000) × (1 − 8 % exit costs)
        board = [c for c in verdict_from_context().conditions if c.startswith("Obtain board approval")]
        assert board == [f"Obtain board approval for the budget transfer from {BILLING} to {ERP}."]


# =========================================================================== chained schedule steps
class TestScheduleSteps:
    def _state(self):
        p = _project(ERP)
        return (str(p["planned_end_date"])[:10], *(float(p[c]) for c in (
            "estimate_at_completion_eur", "estimate_to_completion_eur", "net_present_value_eur")))

    def test_two_steps_equal_one(self, scenario, demo):
        sim.timeline_shift(ERP, 2)
        sim.timeline_shift(ERP, 2)
        two = self._state()
        ctx.set_portfolio_context(ctx.get_dataset(), {}, as_of=AS_OF)
        sim.timeline_shift(ERP, 4)
        one = self._state()
        assert two[0] == one[0] and all(abs(a - b) <= 1 for a, b in zip(two[1:], one[1:], strict=True))

    def test_a_reverse_step_restores_the_forecast(self, scenario):
        before = self._state()
        sim.timeline_shift(ERP, 2)
        text = sim.timeline_shift(ERP, -2)
        assert "Earlier delay undone" in text and "acceleration cost" not in text
        after = self._state()
        assert after[0] == before[0] and all(abs(a - b) <= 1 for a, b in zip(after[1:], before[1:], strict=True))
        assert verdict_from_context().decision == "GO"

    def test_delay_cost_factor(self, scenario):
        full = sim.timeline_shift(ERP, 1)
        update_runtime_config(business_rules={"delay_cost_factor_pct": 50})
        ctx.set_portfolio_context(ctx.get_dataset(), {}, as_of=AS_OF)
        half = sim.timeline_shift(ERP, 1)
        assert "× 50 %" in half
        cost = [float(t.split("Cost of delay")[1].split("): €")[1].split("\n")[0].replace(",", "")) for t in (full, half)]
        assert abs(cost[1] - cost[0] / 2) <= 1


# =========================================================================== guardrails and figures
class TestGuardrails:
    def test_euro_amount_is_held_to_the_percentage_limit(self, scenario):
        assert sim.budget_change(ERP, 50_000_000).startswith("ERROR: A change of €50,000,000 on PRJ-DEMO-1002")
        update_runtime_config(business_rules={"max_budget_change_pct": 20})
        assert sim.budget_transfer(BILLING, DEVICES, 100_000).startswith(f"ERROR: A change of €100,000 on {DEVICES}")

    def test_input_errors_are_an_incomplete_analysis(self, scenario):
        sim.project_cancellation(BILLING)
        text = sim.project_cancellation(BILLING)
        assert text.startswith("INPUT ERROR: Project PRJ-DEMO-1007 is already 'Canceled'")
        assert verdict_from_context().decision == "ANALYSIS INCOMPLETE"

    def test_hold_defers_at_most_the_open_work(self, scenario):
        text = pt.on_hold(DEVICES, 3)  # ends in two weeks, ETC €58,000
        assert "Spend deferred (not saved): ≈ €58,000" in text
        assert "remobilisation cost ≈ €58,000" in text  # one month of burn = ETC / max(remaining, 1 month)

    def test_hold_limit_is_a_business_rule(self, scenario):
        update_runtime_config(business_rules={"max_hold_months": 6})
        assert pt.on_hold(DEVICES, 7).startswith("ERROR: A hold of 7 months exceeds 6 months")

    def test_fte_from_a_stopped_project_overloads_nobody_there(self, scenario):
        sim.project_cancellation(BILLING)
        text = sim.resource_transfer(BILLING, ERP, 2)
        assert "no remaining demand" in text
        assert BILLING not in {f["project"] for f in _facts("overload")}

    def test_capex_clamp_is_explained(self, dataset):
        ctx.set_portfolio_context(dataset, {})  # P-1001: CapEx plan €155,000, already capitalised €152,320
        text = sim.budget_change("P-1001", -21_000)
        assert "Only €2,680 CapEx headroom" in text and "€5,070 of the cut moved to OpEx" in text


# =========================================================================== verdict engine
class TestVerdictDetails:
    def test_small_gap_is_within_tolerance(self):
        gap = {"type": "budget_overrun", "project": "P-1", "eac": 800_500, "new_budget": 800_000, "overrun": 500,
               "overrun_pct": 0.1}
        assert compute_verdict(["RESULT"], [], [gap]).decision == "GO"
        assert compute_verdict(["RESULT"], [], [gap], rules=BusinessRules(overrun_tolerance_eur=0)).decision == \
            "CONDITIONAL GO"

    def test_conditions_carry_owner_and_due_date(self):
        facts = [{"type": "negative_roi", "project": "P-1", "new_roi": -3.0},
                 {"type": "overload", "project": "P-2", "utilisation_pct": 130.0}]
        v = compute_verdict(["RESULT"], [], facts, as_of="2026-10-01")
        assert [d["owner_role"] for d in v.condition_details()] == ["Sponsor", "PMO"]
        assert v.due == "2026-10-15" and v.to_dict()["condition_details"][0]["due"] == "2026-10-15"
        assert "(owner: Sponsor · due 2026-10-15)" in v.to_markdown()

    def test_every_hard_overrun_is_named(self):
        facts = [{"type": "budget_overrun", "project": p, "eac": 1_300_000, "new_budget": 1_000_000, "overrun": 300_000,
                  "overrun_pct": pct} for p, pct in (("P-1", 20.0), ("P-2", 30.0))]
        v = compute_verdict(["RESULT"], [], facts)
        assert v.decision == "NO-GO" and [r.split(":")[0] for r in v.reasons[1:]] == ["P-2", "P-1"]

    def test_every_guardrail_violation_is_named(self):
        v = compute_verdict(["ERROR: Budget undercut – A.", "ERROR: Insufficient resources – B."], [], [])
        assert v.reasons[1] == "2 guardrail violation(s): Budget undercut – A." and v.reasons[2] == "Insufficient resources – B."


# =========================================================================== demo data
class TestDemoData:
    def test_builder_has_nothing_left_to_change(self):
        data = json.loads(SOURCE.read_text(encoding="utf-8"))
        assert build_demo_portfolio.build(data) == data

    def test_data_is_consistent(self):
        data = json.loads(SOURCE.read_text(encoding="utf-8"))
        ends = {p["project_number"]: p["planned_end_date"] for p in data["Projects"]}
        after_end = [d for d in data["Dependencies"] if d["required_by_date"] > ends[d["predecessor_project_number"]]]
        assert len(after_end) >= 3
        assert all(float(p["estimate_at_completion_eur"]) >= float(p["actual_cost_eur"]) for p in data["Projects"])
        assert len({tuple(s["story_points"] for s in data["Stories"] if s["project_number"] == p["project_number"])
                    for p in data["Projects"]}) == len(data["Projects"])

    def test_eac_below_actual_is_a_quality_error(self, dataset_factory, project_factory):
        issues = check_budget_overrun(dataset_factory([project_factory(estimate_at_completion_eur=200_000)]))
        assert [(i.severity, i.rule) for i in issues if "below actual" in i.message] == [("ERROR", "Forecast Logic")]


def test_rule_limits_must_fit_together():
    """A hold (duration + restart ramp) never moves a project further than a direct schedule shift may."""
    import pytest
    from pydantic import ValidationError

    from app.core.business_rules import BusinessRules

    with pytest.raises(ValidationError, match="must not exceed the max. schedule shift"):
        BusinessRules().merged({"max_timeline_shift_months": 24})
    with pytest.raises(ValidationError, match="NO-GO threshold"):
        BusinessRules().merged({"benefit_realization_nogo_pct": 90})
    assert BusinessRules().merged({"max_hold_months": 12, "max_timeline_shift_months": 13}).max_hold_months == 12
