"""Benefit realisation and multi-currency: rates by valid-from date, amounts converted to EUR when the dataset is
built, written back in the project's own currency, a missing rate blocks the project, and actual benefits are
reported as appended rows."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from app.core.security import ROLE_PM, ROLE_PMO
from app.crew.tools.benefit_tools import realisation
from app.crew.tools.data_steward_tools import check_currency
from app.data_adapters import JsonPortfolioAdapter
from app.data_adapters.dataset import PortfolioDataset, exchange_rates, to_base_currency

RATES = pd.DataFrame([{"currency": "USD", "rate_to_eur": 0.8, "valid_from": "2020-01-01"},
                      {"currency": "usd", "rate_to_eur": 0.9, "valid_from": "2026-01-01"},
                      {"currency": "USD", "rate_to_eur": 1.5, "valid_from": "2099-01-01"},
                      {"currency": "CHF", "rate_to_eur": 1.05, "valid_from": "2020-01-01"}])
DATA = {"Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                      "budget_cost_eur": 1000000, "state": "Open", "currency": "USD"},
                     {"project_number": "P-2", "project_name": "Beta", "project_manager": "Priya Raman",
                      "budget_cost_eur": 500000, "state": "Open"}],
        "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": [],
        "Exchange_Rates": RATES.to_dict(orient="records")}


class TestRates:
    def test_the_latest_valid_rate_wins_and_eur_is_one(self):
        assert exchange_rates(RATES, date(2026, 10, 5)) == {"EUR": 1.0, "USD": 0.9, "CHF": 1.05}
        assert exchange_rates(RATES, date(2025, 6, 1))["USD"] == 0.8
        assert exchange_rates(pd.DataFrame()) == {"EUR": 1.0}

    def test_rows_in_another_currency_are_converted(self):
        df = pd.DataFrame([{"project_number": "P-1", "budget_cost_eur": 100.0, "planned_roi_pct": 10.0, "currency": "USD"},
                           {"project_number": "P-2", "budget_cost_eur": 100.0, "planned_roi_pct": 10.0, "currency": None},
                           {"project_number": "P-3", "budget_cost_eur": 100.0, "planned_roi_pct": 10.0, "currency": "JPY"}])
        out = to_base_currency(df, {"EUR": 1.0, "USD": 0.9})
        assert out["budget_cost_eur"].tolist()[:2] == [90.0, 100.0]
        assert out["budget_cost_eur"].iloc[2] == 100.0 and pd.isna(out["fx_rate"].iloc[2])  # no rate: unconverted
        assert out["planned_roi_pct"].tolist() == [10.0, 10.0, 10.0]  # only money columns
        assert out["currency"].tolist() == ["USD", "EUR", "JPY"]

    def test_a_table_without_currency_column_is_untouched(self):
        df = pd.DataFrame([{"project_number": "P-1", "budget_cost_eur": 100.0}])
        assert to_base_currency(df, {"EUR": 1.0}).equals(df)

    def test_a_missing_rate_blocks_the_project(self):
        ds = PortfolioDataset.from_tables({"Projects": pd.DataFrame([{"project_number": "P-9", "budget_cost_eur": 1,
                                                                       "currency": "JPY"}])})
        [issue] = check_currency(ds)
        assert (issue.severity, issue.rule, issue.project_id) == ("ERROR", "Exchange Rate", "P-9")


class TestWriteBack:
    @pytest.fixture
    def source(self, tmp_path):
        path = tmp_path / "portfolio.json"
        path.write_text(json.dumps(DATA), encoding="utf-8")
        return path

    def test_amounts_are_shown_in_eur_and_written_in_the_source_currency(self, source):
        adapter = JsonPortfolioAdapter(source)
        projects = adapter.load_dataset().projects.set_index("project_number")
        assert projects.at["P-1", "budget_cost_eur"] == 900000.0  # 1,000,000 USD at 0.9
        change = [{"project_id": "P-1", "field": "business_case", "old": None, "new": "x"}]
        adapter.apply_changes(change, user="Priya Raman", role=ROLE_PM)
        _approve_budget(adapter, "P-1", 900000.0, 810000.0)
        stored = json.loads(source.read_text(encoding="utf-8"))["Projects"][0]
        assert stored["budget_cost_eur"] == 900000.0 and stored["currency"] == "USD"  # 810,000 EUR / 0.9
        assert adapter.load_dataset().projects.set_index("project_number").at["P-1", "budget_cost_eur"] == 810000.0

    def test_a_eur_project_is_written_unchanged(self, source):
        adapter = JsonPortfolioAdapter(source)
        _approve_budget(adapter, "P-2", 500000.0, 450000.0)
        assert json.loads(source.read_text(encoding="utf-8"))["Projects"][1]["budget_cost_eur"] == 450000.0

    def test_an_actual_benefit_is_an_appended_row_the_pm_writes_directly(self, source):
        row = {"benefit_actual_number": "BAC-P-2-001", "project_number": "P-2", "benefit_plan_number": None,
               "month": "2029-03-01", "actual_benefit_eur": 1000, "currency": "CHF", "reported_by": "Priya Raman"}
        JsonPortfolioAdapter(source).apply_changes(
            [{"op": "append", "table": "Benefit_Actuals", "project_id": "P-2", "field": "Benefit_Actuals", "row": row}],
            user="Priya Raman", role=ROLE_PM)
        actuals = JsonPortfolioAdapter(source).load_dataset().benefit_actuals
        assert actuals["actual_benefit_eur"].tolist() == [1050.0]  # converted on load, stored as entered


def _approve_budget(adapter, pid, old, new):
    from app.core import proposals

    entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO",
                                      [{"project_id": pid, "field": "budget_cost_eur", "old": old, "new": new}])
    proposals.reserve_apply(entry["id"], "PMO-ADMIN", ROLE_PMO, f"op-{pid}-fx00001")
    adapter.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"],
                          operation_id=f"op-{pid}-fx00001")


class TestRealisation:
    PLANS = pd.DataFrame([{"benefit_plan_number": "B1", "planned_benefit_eur": 100.0, "realization_start_date": "2029-01-01"},
                          {"benefit_plan_number": "B2", "planned_benefit_eur": 100.0, "realization_start_date": "2030-01-01"}])

    def test_due_tranches_against_actuals_including_unassigned(self):
        actuals = pd.DataFrame([{"benefit_plan_number": "B1", "actual_benefit_eur": 40.0},
                                {"benefit_plan_number": None, "actual_benefit_eur": 20.0}])
        r = realisation(self.PLANS, actuals, {}, date(2029, 6, 30))
        assert (r["planned_due"], r["actual"], r["unassigned"], r["pct"]) == (100.0, 60.0, 20.0, 60.0)
        assert [t["is_due"] for t in r["tranches"]] == [True, False]

    def test_actuals_of_tranches_not_yet_due_never_hide_a_shortfall(self):
        """Due 100 with 40 realised, not due 100 with 150 already realised: 40 %, not 190 %."""
        actuals = pd.DataFrame([{"benefit_plan_number": "B1", "actual_benefit_eur": 40.0},
                                {"benefit_plan_number": "B2", "actual_benefit_eur": 150.0}])
        r = realisation(self.PLANS, actuals, {}, date(2029, 6, 30))
        assert (r["planned_due"], r["actual"], r["early"], r["pct"]) == (100.0, 40.0, 150.0, 40.0)

    def test_nothing_due_has_no_rate(self):
        assert realisation(self.PLANS, pd.DataFrame(), {}, date(2028, 1, 1))["pct"] is None

    def test_without_plans_the_project_benefit_is_one_tranche_at_the_end(self):
        r = realisation(pd.DataFrame(), pd.DataFrame(), {"planned_end_date": "2028-06-30", "planned_benefit_eur": 500},
                        date(2029, 1, 1))
        assert r["planned_due"] == 500 and r["tranches"][0]["type"] == "planned benefit (project)"
