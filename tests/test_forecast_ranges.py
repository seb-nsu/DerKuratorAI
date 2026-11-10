"""Forecast ranges (E3, E4): reference classes from the first baselines with widening and percentiles, the buffer
at the confidence level, demands, and the Monte Carlo run of the open portfolio – reproducible, risks by their
response, closed projects left out, one hand-checked case."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.business_rules import BusinessRules
from app.crew.tools.forecast_ranges import (
    demand_reference,
    history,
    project_reference,
    reference_class,
    simulate_portfolio,
)
from app.data_adapters import JsonPortfolioAdapter

TODAY = date(2026, 9, 25)
RULES = BusinessRules()


def _project(pid, eac, *, budget=100.0, kind="Agile", portfolio="A", state="Work in Progress", actual=50.0,
             end="2027-06-30", approved="2027-06-30"):
    return {"project_number": pid, "project_name": pid, "execution_type": kind, "portfolio": portfolio,
            "state": state, "budget_cost_eur": budget, "estimate_at_completion_eur": eac, "actual_cost_eur": actual,
            "planned_start_date": "2026-01-01", "approved_end_date": approved, "planned_end_date": end}


def _dataset(projects, risks=None, baselines=None, demands=None, plans=None):
    return SimpleNamespace(projects=pd.DataFrame(projects), risks=pd.DataFrame(risks or [],
                           columns=["risk_number", "project_number", "probability", "impact", "risk_status"]),
                           baselines=pd.DataFrame(baselines or []), demands=pd.DataFrame(demands or []),
                           demand_year_plans=pd.DataFrame(plans or []))


def test_the_cost_ratio_uses_the_first_baseline_and_the_actual_cost_of_closed_projects():
    projects = pd.DataFrame([_project("P1", 130.0), _project("P2", 999.0, state="Closed", actual=90.0)])
    baselines = pd.DataFrame([{"project_number": "P1", "version": 2, "baseline_budget_eur": 120.0},
                              {"project_number": "P1", "version": 1, "baseline_budget_eur": 100.0}])
    rows = {r.project_id: r for r in history(projects, baselines)}
    assert rows["P1"].cost_ratio == 1.3 and rows["P2"].cost_ratio == 0.9 and rows["P2"].closed


def test_a_small_class_widens_and_says_so():
    rows = history(pd.DataFrame([_project(f"A{i}", 100 + i * 10) for i in range(3)]
                                + [_project(f"B{i}", 100.0, kind="Waterfall") for i in range(4)]), None)
    cls = reference_class("X", "Agile", "A", rows, min_size=5)
    assert cls.basis == "whole portfolio" and cls.widened and len(cls.members) == 7
    cls = reference_class("X", "Agile", "A", rows, min_size=3)
    assert cls.basis == "Agile, A" and not cls.widened


def test_golden_buffer_at_the_confidence_level():
    """Ten projects 1.00 … 1.45 over budget: P80 of the ratios (numpy, linear) is 1.36 -> buffer 36 % of 200."""
    ds = _dataset([_project(f"P{i}", 100 + 5 * i) for i in range(10)] + [_project("NEW", 200.0, budget=200.0)])
    out = project_reference(ds, "NEW", RULES)
    assert out["size"] == 10 and out["cost_ratio"]["p_conf"] == 1.36
    assert out["buffer_eur"] == 72.0 and out["budget_at_confidence_eur"] == 272.0
    assert out["headline"].startswith("80 % of 10 comparable projects (Agile, A) stayed within +36 %")


def test_a_demand_is_compared_with_the_projects_of_its_kind():
    ds = _dataset([_project(f"P{i}", 110.0) for i in range(6)],
                  demands=[{"demand_number": "D1", "execution_type": "Agile", "portfolio": "A", "duration_months": 6}],
                  plans=[{"demand_number": "D1", "capex_eur": 600.0, "opex_eur": 400.0}])
    out = demand_reference(ds, "D1", RULES)
    assert out["cost_ratio"]["p_conf"] == 1.1 and out["buffer_eur"] == 100.0
    with pytest.raises(KeyError):
        demand_reference(ds, "D9", RULES)


def test_the_monte_carlo_run_is_reproducible_and_leaves_closed_projects_out():
    ds = _dataset([_project(f"P{i}", 100 + 5 * i) for i in range(8)] + [_project("C", 500.0, state="Closed")])
    first, second = simulate_portfolio(ds, TODAY, RULES, runs=500), simulate_portfolio(ds, TODAY, RULES, runs=500)
    assert first == second and first["summary"]["projects"] == 8
    assert "C" not in {p["project_id"] for p in first["projects"]}
    for p in first["projects"]:
        assert 0 <= p["probability_within_budget_pct"] <= 100 and p["p50_cost_eur"] <= p["p_conf_cost_eur"]


def test_golden_a_certain_risk_adds_its_impact_and_a_mitigated_one_nothing():
    """One project without spread (all ratios equal): forecast 100, an accepted High/High risk at 80 % adds 250,000
    in 80 % of the runs – P50 = 250,100, within budget in about 20 % of the runs."""
    projects = [_project("P1", 100.0, budget=100.0)] + [_project(f"Q{i}", 100.0, kind="Waterfall") for i in range(5)]
    risk = {"risk_number": "R1", "project_number": "P1", "probability": "High", "impact": "High",
            "risk_status": "Accept"}
    out = {p["project_id"]: p for p in simulate_portfolio(_dataset(projects, [risk]), TODAY, RULES)["projects"]}
    assert out["P1"]["p50_cost_eur"] == 250_100.0
    assert 17 <= out["P1"]["probability_within_budget_pct"] <= 23
    mitigated = simulate_portfolio(_dataset(projects, [{**risk, "risk_status": "Mitigate"}]), TODAY, RULES)
    assert next(p for p in mitigated["projects"] if p["project_id"] == "P1")["p50_cost_eur"] == 100.0


def test_the_portfolio_summary_adds_up():
    ds = _dataset([_project(f"P{i}", 100.0) for i in range(6)])
    summary = simulate_portfolio(ds, TODAY, RULES)["summary"]
    assert summary["budget_eur"] == 600.0 and summary["p50_cost_eur"] == 600.0
    assert summary["probability_within_budget_pct"] == 100.0 and summary["reserve_needed_eur"] == 0


def test_the_demo_portfolio_runs_in_well_under_a_second():
    import time

    ds = JsonPortfolioAdapter("data/DerKurator_Demo_Portfolio.json").load_dataset()
    start = time.perf_counter()
    out = simulate_portfolio(ds, TODAY)
    assert time.perf_counter() - start < 2 and out["summary"]["projects"] == 11
    assert out["summary"]["p50_cost_eur"] >= ds.projects["estimate_at_completion_eur"].sum() * 0.9
