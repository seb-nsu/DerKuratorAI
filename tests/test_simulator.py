"""Guardrails and calculation logic of the deterministic simulation tools."""
from __future__ import annotations

import pytest

from app.crew.tools import context as ctx
from app.crew.tools import simulator_tools as sim
from app.crew.tools.data_steward_tools import run_all_checks


@pytest.fixture
def scenario(dataset):
    """Scenario context as in the pipeline: quality gate from the Data Steward."""
    ctx.set_portfolio_context(dataset, run_all_checks(dataset).blocked_reasons)
    return dataset


def _ledger(field: str, project: str = "P-1001"):
    return [c for c in ctx.get_ledger() if c["field"] == field and c["project_id"] == project]


class TestBudgetGuardrails:
    def test_capex_cut_below_capitalised_actuals_is_rejected(self, scenario):
        # planned CapEx €155,000, already capitalised €152,320: a cut of €50,000 is not allowed
        result = sim.budget_change("P-1001", -50000, cost_type="capex")
        assert result.startswith("ERROR: CapEx undercut")
        assert ctx.get_ledger() == []

    def test_budget_cut_below_actual_costs_is_rejected(self, scenario):
        result = sim.budget_change("P-1001", -200000)
        assert result.startswith("ERROR:") and "undercut" in result
        assert ctx.get_ledger() == []

    def test_percentage_change_above_limit_is_rejected(self, scenario):
        assert "exceeds the limit" in sim.budget_change("P-1001", 150, is_perc=True)

    def test_invalid_cost_type_is_rejected(self, scenario):
        assert "cost_type" in sim.budget_change("P-1001", -1000, cost_type="bitcoin")

    def test_cut_below_forecast_does_not_improve_roi(self, scenario):
        # the forecast (EAC €420,000) stays the expected cost, so a cut budget cannot raise ROI or return
        result = sim.budget_change("P-1001", -2000, cost_type="capex")
        assert result.startswith("RESULT simulate_budget_change")
        assert _ledger("planned_capital_eur")[0]["new"] == 153000
        assert _ledger("planned_roi_pct") == []
        assert _ledger("planned_return_eur") == []

    def test_budget_increase_recalculates_roi_on_expected_cost(self, scenario):
        sim.budget_change("P-1001", 10000)
        # ROI = (benefit − cost) / cost = (510,000 − 430,000) / 430,000
        assert _ledger("planned_roi_pct")[0]["new"] == pytest.approx(18.6, abs=0.05)
        assert _ledger("planned_return_eur")[0]["new"] == pytest.approx(80000)

    def test_total_cut_uses_bucket_headroom_before_failing(self, scenario):
        # a proportional cut would push CapEx below the actual costs, so the remainder must come from OpEx
        result = sim.budget_change("P-1001", -100000)
        assert result.startswith("RESULT")
        assert _ledger("planned_capital_eur")[0]["new"] >= 152320


class TestTransfersAndCapacity:
    def test_transfer_exceeding_free_budget_is_rejected(self, scenario):
        result = sim.budget_transfer("P-1001", "P-1002", 500000)
        assert result.startswith("ERROR: Budget undercut")

    def test_transfer_to_itself_is_rejected(self, scenario):
        assert sim.budget_transfer("P-1001", "P-1001", 1000) == "INPUT ERROR: Source and target are identical."

    def test_resource_transfer_exceeding_allocation_is_rejected(self, scenario):
        assert sim.resource_transfer("P-1001", "P-1002", 5).startswith("ERROR: Insufficient resources")

    def test_cancellation_then_transfer_is_chained_in_scenario(self, scenario):
        assert sim.project_cancellation("P-1001").startswith("RESULT")
        assert sim.budget_transfer("P-1001", "P-1002", 182000).startswith("RESULT")
        assert _ledger("state")[0]["new"] == "Canceled"
        assert _ledger("budget_cost_eur", "P-1002")[0]["new"] == 982000


class TestConfigurableRequiredFieldsGuard:
    def test_budget_tool_rejects_missing_value_even_if_field_is_not_required(self, dataset_factory, project_factory):
        from app.core.required_fields import DEFAULT_REQUIRED_FIELDS, save_required_fields

        save_required_fields([f for f in DEFAULT_REQUIRED_FIELDS if f != "planned_capital_eur"])
        ds = dataset_factory([project_factory(planned_capital_eur=None)])
        ctx.set_portfolio_context(ds, run_all_checks(ds).blocked_reasons)  # Project is NOT locked
        result = sim.budget_change("P-1001", -1000, cost_type="capex")
        assert result.startswith("ERROR: Data quality") and "planned_capital_eur" in result


class TestQualityGateAndFallbacks:
    def test_blocked_project_is_rejected_by_quality_gate(self, scenario):
        assert sim.budget_change("P-1003", 1000).startswith("ERROR: Data quality")

    def test_unknown_project_is_reported(self, scenario):
        # An unknown project is an input error (ANALYSIS INCOMPLETE), not a guardrail violation
        assert sim.budget_change("P-9999", 1000).startswith("INPUT ERROR: Project 'P-9999' not found")

    def test_unstructured_scenario_without_llm_is_a_heuristic_placeholder(self, scenario):
        # without API keys (fixture) no model runs, so the fallback is labelled a heuristic placeholder, never an AI estimate
        assert sim.unstructured_scenario("P-1003", "Lieferant fällt aus").startswith(sim.HEURISTIC_PREFIX)

    def test_waterfall_delay_shifts_end_date(self, scenario):
        result = sim.agile_vs_waterfall_shift("P-1001", months_delta=2)
        assert "[Waterfall] Shift: +2.0 months" in result
        assert _ledger("planned_end_date")[0]["new"] > "2030-06-30"


class TestBurnRate:
    """Monthly burn rate behind the cost of delay: remaining estimate over the remaining time – but never for a project
    already past its planned end, whose remaining time is no basis."""
    PROJECT = {"planned_start_date": "2026-01-01", "planned_end_date": "2027-01-01",
               "estimate_to_completion_eur": 1_200_000, "budget_cost_eur": 2_400_000}

    def _burn(self, monkeypatch, as_of, **fields):
        from datetime import date

        monkeypatch.setattr(sim, "as_of", lambda: date.fromisoformat(as_of))
        return sim._monthly_burn({**self.PROJECT, **fields})

    def test_a_running_project_burns_its_remaining_estimate_over_the_remaining_time(self, monkeypatch):
        assert 190_000 < self._burn(monkeypatch, "2026-07-01", current_fte=4) < 210_000  # 1.2 M over ~6 months

    def test_an_overdue_project_burns_its_team_cost_not_the_whole_estimate(self, monkeypatch):
        from app.core.business_rules import current_rules

        burn = self._burn(monkeypatch, "2027-06-01", current_fte=4)
        assert burn == pytest.approx(4 * current_rules().fte_monthly_cost_eur)

    def test_an_overdue_project_without_a_team_spreads_its_budget_over_the_planned_duration(self, monkeypatch):
        burn = self._burn(monkeypatch, "2027-06-01", current_fte=0)
        assert burn == pytest.approx(2_400_000 / 12, rel=0.02)  # never the whole 1.2 M estimate per month
