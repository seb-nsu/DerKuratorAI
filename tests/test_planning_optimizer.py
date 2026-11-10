"""Value-optimal filling of a planning scenario: the second proposal next to the rank order. Same limits, same
bound money, same verdict rules – only the choice of the free demands differs, and the comparison says what the
optimizer does differently and why."""
from __future__ import annotations

import io
import random
import shutil

import pandas as pd
import pytest

from app.core.security import ROLE_PMO
from app.data_adapters import JsonPortfolioAdapter
from app.planning import optimizer, reports, store
from app.planning.scenario import ScenarioInput, compare, run_scenario
from tests.test_planning_scenario import AS_OF, CASES, SOURCE, _demand, _ds, _plan, _run


def _value(ds, **spec):
    return _run(ds, method="value", **spec)


def _score_sum(result):
    placed = result.items[result.items["decision"] != "Out"]
    return float(pd.to_numeric(placed["score"], errors="coerce").fillna(0).sum())


def _two_small_beat_one_large():
    # D-1 ranks first (value 5 = score 100) but takes 700 of 1000; D-2 and D-3 (score 75 each) fit only together
    return _ds([_demand("D-1", 5, latest_start_date="2027-01-01"), _demand("D-2", 4, latest_start_date="2027-01-01"),
                _demand("D-3", 4, latest_start_date="2027-01-01")],
               [_plan("D-1", 700), _plan("D-2", 500), _plan("D-3", 500)])


class TestOptimizer:
    def test_two_smaller_demands_with_more_value_beat_one_large(self):
        ds = _two_small_beat_one_large()
        rank, value = _run(ds), _value(ds)
        assert rank.decided("In") == ["D-1"] and sorted(value.decided("In")) == ["D-2", "D-3"]
        assert _score_sum(value) == pytest.approx(150) and _score_sum(rank) == pytest.approx(100)
        reason = value.items.set_index("demand_number").at["D-1", "reason"]
        assert "CapEx 2027 of business unit Finance" in reason and "would be exceeded" in reason
        assert value.verdict.decision == "GO" and value.solved

    def test_the_comparison_says_what_differs_and_why(self):
        ds = _two_small_beat_one_large()
        summary, figures, changes = compare(_run(ds), _value(ds))
        assert list(figures["Filling"]) == ["Rank order", "Value-optimal"]
        assert list(figures["Score sum"]) == [100, 150]
        assert summary[0].startswith("Score sum of the plan 100 → 150 (+50)")
        assert any("Additionally in the plan: D-2 (score 75), D-3 (score 75)" in s for s in summary)
        assert any(s.startswith("Left out instead: D-1 (score 100)") for s in summary)
        row = changes.set_index("Demand").loc["D-1"]
        assert row["Rank order"] == "In 2027-Q1" and row["Value-optimal"] == "Out"
        assert "CapEx 2027" in row["Why (value-optimal)"]

    def test_same_plan_means_no_difference(self):
        ds = _ds([_demand("D-1")], [_plan("D-1", 100)])
        summary, _, changes = compare(_run(ds), _value(ds))
        assert changes.empty and "same demands with the same starts" in summary[0]

    def test_mandatory_demands_come_before_any_score(self):
        ds = _ds([_demand("D-1", 5, latest_start_date="2027-01-01"), _demand("D-2", None, demand_type="Mandatory",
                  mandatory_reason="Regulation", latest_start_date="2027-01-01")],
                 [_plan("D-1", 600), _plan("D-2", 600)])
        value = _value(ds)
        assert value.decided("In") == ["D-2"] and "mandatory_out" not in {f["type"] for f in value.facts}

    def test_a_deferral_is_preferred_to_an_out_and_names_the_limit(self):
        ds = _ds([_demand("D-1", 5, latest_start_date="2027-01-01"), _demand("D-2", 4)],
                 [_plan("D-1", 700), _plan("D-2", 500, quarter=2)])
        items = _value(ds).items.set_index("demand_number")
        assert items.at["D-1", "decision"] == "In"
        assert items.at["D-2", "decision"] == "Deferred" and items.at["D-2", "start"] == "2028-Q2"
        assert items.at["D-2", "reason"].startswith("Deferred to 2028-Q2: CapEx 2027")

    def test_dependencies_hold(self):
        ds = _ds([_demand("D-1", 2), _demand("D-2", 5, depends_on="D-1")],
                 [_plan("D-1", 100, quarter=2), _plan("D-2", 100, quarter=2)])
        items = _value(ds).items.set_index("demand_number")
        assert items.at["D-1", "start"] == "2027-Q2" and items.at["D-2", "start"] == "2028-Q2"
        out = _ds([_demand("D-1", 5, latest_start_date="2027-01-01"), _demand("D-2", 4, depends_on="D-1")],
                  [_plan("D-1", 1200), _plan("D-2", 100)])
        assert _value(out).items.set_index("demand_number").at["D-2", "reason"] == "Predecessor D-1 is out"

    def test_a_successor_never_starts_before_its_chosen_predecessor_ends(self):
        # D-2 is worth more but needs D-1 first; with room for both only when D-2 starts a year later
        ds = _ds([_demand("D-1", 2), _demand("D-2", 5, depends_on="D-1")],
                 [_plan("D-1", 600, quarter=4), _plan("D-2", 600, quarter=4)])
        items = _value(ds).items.set_index("demand_number")
        assert items.at["D-1", "start"] == "2027-Q4" and items.at["D-2", "start"] == "2028-Q4"

    def test_capacity_holds(self):
        cap = [{"year": y, "assignment_group": "Team A", "available_fte": 4, "max_utilization_pct": 75}
               for y in (2027, 2028, 2029)]
        res = [{"demand_number": d, "year": 2027, "quarter": 1, "assignment_group": "Team A", "fte": 2}
               for d in ("D-1", "D-2")]
        r = _value(_ds([_demand("D-1", 5), _demand("D-2", 4)], [_plan("D-1", 10), _plan("D-2", 10)], capacity=cap,
                       resources=res))
        assert r.decided("Deferred") == ["D-2"] and "Team A 2027-Q1" in r.items.set_index("demand_number").at[
            "D-2", "reason"]

    @pytest.mark.parametrize("seed", range(12))
    def test_never_less_value_than_the_rank_order_and_never_over_a_limit(self, seed):
        rng = random.Random(seed)  # noqa: S311 – reproducible test data, no security use
        demands = [_demand(f"D-{i:02d}", rng.randint(1, 5), latest_start_date=rng.choice(["2027-12-31", "2028-12-31"]),
                           depends_on=f"D-{i - 1:02d}" if i and rng.random() < 0.2 else None) for i in range(10)]
        plans = [_plan(f"D-{i:02d}", rng.randint(100, 700), rng.randint(0, 300), quarter=rng.randint(1, 4))
                 for i in range(10)]
        ds = _ds(demands, plans)
        rank, value = _run(ds), _value(ds)
        assert value.solved and _score_sum(value) >= _score_sum(rank) - 1e-6
        assert not value.budget.empty and (value.budget["Free"] >= -1).all()
        assert not {"budget_limit", "capacity_limit", "dependency_violated"} & {f["type"] for f in value.facts}
        again = _value(_ds(list(reversed(demands)), list(reversed(plans))))
        pd.testing.assert_frame_equal(value.items[["demand_number", "decision", "start"]],
                                      again.items[["demand_number", "decision", "start"]])

    def test_without_a_solution_the_rank_order_is_used(self, monkeypatch):
        class NoSolution:
            x = None

        monkeypatch.setattr(optimizer, "milp", lambda *a, **k: NoSolution())
        ds = _two_small_beat_one_large()
        value = _value(ds)
        assert not value.solved and value.decided("In") == _run(ds).decided("In")


@pytest.mark.parametrize(("name", "demands", "plans", "extra", "spec", "decision", "fact"), CASES,
                         ids=[c[0] for c in CASES])
def test_the_verdict_rules_apply_to_the_value_optimal_plan_too(name, demands, plans, extra, spec, decision, fact):
    r = _value(_ds(demands, plans, **extra), **spec)
    if decision != "GO":
        assert r.verdict.decision != "GO", (name, r.facts)


@pytest.fixture(scope="module")
def demo():
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return JsonPortfolioAdapter(SOURCE).load_dataset()


class TestDemoPortfolio:
    def test_more_budget_brings_the_data_platform_in(self, demo):
        rank = run_scenario(demo, ScenarioInput(budget_factor_pct=10), as_of=AS_OF)
        value = run_scenario(demo, ScenarioInput(budget_factor_pct=10, method="value"), as_of=AS_OF)
        assert {"DMD-2027-005", "DMD-2027-006"} <= set(value.decided("In")) - set(rank.decided("In"))
        assert _score_sum(value) > _score_sum(rank)
        assert {"DMD-2027-002", "DMD-2027-004", "DMD-2027-016"} <= set(value.decided("In"))  # mandatory stay

    def test_saved_value_scenario_recalculates_with_the_same_filling(self, demo, tmp_path):
        path = tmp_path / "portfolio.json"
        shutil.copy(SOURCE, path)
        adapter = JsonPortfolioAdapter(path)
        result = run_scenario(demo, ScenarioInput(name="Optimised", budget_factor_pct=10, method="value"), as_of=AS_OF)
        adapter.apply_changes(store.save_changes(demo, result, "PMO-ADMIN"), user="PMO-ADMIN", role=ROLE_PMO,
                              reason="planning_scenario")
        saved = store.saved_scenarios(adapter.load_dataset()).iloc[0]
        assert saved["filling"] == "value" and store.input_of(saved).method == "value"
        assert "value-optimal" in store.spec_text(result.input)

    def test_board_pack_shows_the_optimizer(self, demo):
        from openpyxl import load_workbook

        value = run_scenario(demo, ScenarioInput(name="Optimised", budget_factor_pct=10, method="value"), as_of=AS_OF)
        book = load_workbook(io.BytesIO(reports.board_pack_xlsx(demo, value)))
        assert book.sheetnames[-1] == "Optimizer"
        cells = [c for row in book["Optimizer"].iter_rows(values_only=True) for c in row if c]
        assert "Value-optimal" in cells and "DMD-2027-005" in cells
        page = reports.board_pack_html(demo, value)
        assert "Value-optimal choice compared with the rank order" in page and "<script" not in page
        rank_page = reports.board_pack_html(demo, run_scenario(demo, as_of=AS_OF))
        assert "Value-optimal choice" not in rank_page


def test_several_hundred_demands_are_solved_well_within_the_time_limit():
    """Load test: 300 demands in three business units with predecessors – the exact solution, not the fallback."""
    import time

    rng = random.Random(7)  # noqa: S311 - reproducible test data
    units = ("Finance", "HR", "Sales")
    demands, plans = [], []
    for i in range(300):
        number = f"D-{i:04d}"
        demands.append(_demand(number, rng.randint(1, 5), business_unit=units[i % 3], latest_start_date="2028-12-31",
                               depends_on=f"D-{i - 7:04d}" if i % 11 == 0 and i > 7 else None))
        plans.append(_plan(number, rng.randint(50, 400), rng.randint(0, 100), quarter=rng.randint(1, 4)))
    envelopes = [{"year": y, "scope_type": "business_unit", "scope": u, "capex_cap_eur": 12000, "opex_cap_eur": 3000,
                  "cap_type": "hard"} for y in (2027, 2028, 2029) for u in units]
    ds = _ds(demands, plans, envelopes=envelopes)
    started = time.perf_counter()
    value = _value(ds)
    assert value.solved and time.perf_counter() - started < optimizer.TIME_LIMIT_S
    assert _score_sum(value) >= _score_sum(_run(ds))
