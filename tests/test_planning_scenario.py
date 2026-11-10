"""Planning 2: scenarios – what running projects bind, greedy filling in rank order, deferral by whole years, the
reason of every Out, and the deterministic planning verdict. One golden case per verdict rule; no case may end in GO
when a rule says otherwise."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core.security import ROLE_PM, ROLE_PMO
from app.data_adapters import JsonPortfolioAdapter, WriteNotAuthorizedError
from app.data_adapters.dataset import PortfolioDataset
from app.planning import store
from app.planning.committed import committed_capacity, committed_costs
from app.planning.scenario import ScenarioInput, run_scenario

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)  # planning years 2027-2029, binding year 2027
MODEL = [{"criterion": "Value", "weight": 100, "scale_min": 1, "scale_max": 5, "direction": "higher is better"}]


def _demand(number, value=3, **kw):
    row = {"demand_number": number, "demand_name": f"Demand {number}", "demand_owner": "Priya Raman",
           "business_unit": "Finance", "portfolio": "Apps", "demand_type": "New", "demand_status": "Assessed",
           "earliest_start_date": "2027-01-01", "duration_months": 3, "net_present_value_eur": 100, "tshirt_size": "M",
           **kw}
    return row, value


def _ds(demands, plans, envelopes=None, capacity=None, resources=None, projects=None, cost_plans=None,
        assessed_on="2026-09-01"):
    rows = [d for d, _ in demands]
    scores = [{"score_number": f"S-{d['demand_number']}", "demand_number": d["demand_number"], "criterion": "Value",
               "score": v, "assessed_on": assessed_on} for d, v in demands if v is not None]
    envelopes = envelopes if envelopes is not None else [
        {"year": y, "scope_type": "business_unit", "scope": "Finance", "capex_cap_eur": 1000, "opex_cap_eur": 1000,
         "cap_type": "hard"} for y in (2027, 2028, 2029)]
    tables = {"Projects": pd.DataFrame(projects or [{"project_number": "PRJ-1", "business_unit": "Finance",
                                                     "portfolio": "Apps", "state": "Closed"}]),
              "Demands": pd.DataFrame(rows), "Scoring_Model": pd.DataFrame(MODEL),
              "Demand_Scores": pd.DataFrame(scores, columns=["score_number", "demand_number", "criterion", "score",
                                                             "assessed_on"]),
              "Demand_Year_Plans": pd.DataFrame(plans), "Budget_Envelopes": pd.DataFrame(envelopes)}
    if capacity:
        tables["Capacity_Limits"] = pd.DataFrame(capacity)
    if resources:
        tables["Demand_Resource_Plans"] = pd.DataFrame(resources)
    if cost_plans:
        tables["Cost_Plans"] = pd.DataFrame(cost_plans)
    return PortfolioDataset.from_tables(tables)


def _plan(number, capex, opex=0, year=2027, quarter=1, confidence="Medium"):
    return {"demand_number": number, "year": year, "quarter": quarter, "capex_eur": capex, "opex_eur": opex,
            "confidence": confidence}


def _run(ds, **spec):
    return run_scenario(ds, ScenarioInput(**spec), as_of=AS_OF)


def _types(result):
    return {f["type"] for f in result.facts}


class TestFilling:
    def test_everything_fits_is_go(self):
        r = _run(_ds([_demand("D-1")], [_plan("D-1", 500)]))
        assert r.verdict.decision == "GO" and r.decided("In") == ["D-1"]

    def test_rank_order_decides_who_gets_the_money_and_the_out_names_limit_and_amount(self):
        r = _run(_ds([_demand("D-1", 5), _demand("D-2", 2, latest_start_date="2027-03-31")],
                     [_plan("D-1", 700), _plan("D-2", 500)]))
        assert r.decided("In") == ["D-1"] and r.decided("Out") == ["D-2"]
        reason = r.items.set_index("demand_number").at["D-2", "reason"]
        assert "CapEx 2027 of business unit Finance" in reason and "€200" in reason
        assert r.verdict.decision == "GO"  # an Out demand is a planning result, not a breach

    def test_a_demand_is_deferred_by_whole_years_within_its_latest_start(self):
        r = _run(_ds([_demand("D-1", 5), _demand("D-2", 2)], [_plan("D-1", 700), _plan("D-2", 500, quarter=2)]))
        item = r.items.set_index("demand_number").loc["D-2"]
        assert item["decision"] == "Deferred" and item["start"] == "2028-Q2" and item["shift_years"] == 1
        assert item["reason"].startswith("Deferred to 2028-Q2: CapEx 2027")

    def test_a_duplicate_demand_number_is_blocked_not_a_crash(self):
        r = _run(_ds([_demand("D-1", 5), _demand("D-1", 2), _demand("D-2")],
                     [_plan("D-1", 100), _plan("D-2", 100)]))
        assert list(r.items["demand_number"]).count("D-1") == 1
        assert r.items.set_index("demand_number").at["D-1", "decision"] != "In"  # the Data Steward blocks it
        assert "D-2" in r.decided("In")

    def test_a_free_demand_is_never_planned_over_a_soft_limit(self):
        env = [{"year": y, "scope_type": "business_unit", "scope": "Finance", "capex_cap_eur": 1000,
                "opex_cap_eur": 1000, "cap_type": "soft"} for y in (2027, 2028, 2029)]
        r = _run(_ds([_demand("D-1", 5), _demand("D-2", 2, latest_start_date="2027-03-31")],
                     [_plan("D-1", 700), _plan("D-2", 500)], envelopes=env))
        assert r.decided("Out") == ["D-2"] and "soft_limit" not in _types(r)

    def test_reserve_and_budget_factor_shrink_the_limit(self):
        env = [{"year": 2027, "scope_type": "business_unit", "scope": "Finance", "capex_cap_eur": 1000,
                "opex_cap_eur": 1000, "reserve_pct": 10, "cap_type": "hard"}]
        ds = _ds([_demand("D-1", latest_start_date="2027-01-01")], [_plan("D-1", 850)], envelopes=env)
        assert _run(ds).decided("In") == ["D-1"]
        assert _run(ds, budget_factor_pct=-10).decided("Out") == ["D-1"]  # 1000 x 0.9 x 0.9 = 810

    def test_running_projects_bind_money_first(self):
        projects = [{"project_number": "PRJ-1", "business_unit": "Finance", "portfolio": "Apps", "state": "Active",
                     "planned_end_date": "2027-06-30", "estimate_to_completion_eur": 400}]
        cost = [{"project_number": "PRJ-1", "month": "2027-02-01", "cost_type": "CapEx", "planned_cost_eur": 300}]
        ds = _ds([_demand("D-1", latest_start_date="2027-01-01")], [_plan("D-1", 800)], projects=projects,
                 cost_plans=cost)
        committed = committed_costs(ds, AS_OF)
        assert committed["capex_eur"].sum() == pytest.approx(300 + 100 * 0)  # no capital share: the rest is OpEx
        assert committed.loc[committed["estimated"], "opex_eur"].sum() == pytest.approx(100)
        assert _run(ds).decided("Out") == ["D-1"]  # 300 + 800 > 1000

    def test_predecessor_first_and_successor_after_its_end(self):
        r = _run(_ds([_demand("D-1", 2), _demand("D-2", 5, depends_on="D-1")],
                     [_plan("D-1", 100, quarter=2), _plan("D-2", 100, quarter=2)]))
        items = r.items.set_index("demand_number")
        assert items.at["D-2", "decision"] == "Deferred" and items.at["D-2", "start"] == "2028-Q2"

    def test_successor_of_an_out_demand_is_out(self):
        r = _run(_ds([_demand("D-1", 5, latest_start_date="2027-01-01"), _demand("D-2", 4, depends_on="D-1")],
                     [_plan("D-1", 1200), _plan("D-2", 100)]))
        assert r.items.set_index("demand_number").at["D-2", "reason"] == "Predecessor D-1 is out"

    def test_unassessed_draft_and_rejected_demands(self):
        r = _run(_ds([_demand("D-1", None), _demand("D-2", demand_status="Draft"),
                      _demand("D-3", demand_status="Rejected")], [_plan(d, 10) for d in ("D-1", "D-2", "D-3")]))
        assert r.items.set_index("demand_number").at["D-1", "reason"] == "Not assessed yet"
        assert set(r.items["demand_number"]) == {"D-1"}

    def test_pmo_exclude_and_move(self):
        ds = _ds([_demand("D-1"), _demand("D-2")], [_plan("D-1", 100), _plan("D-2", 100)])
        r = _run(ds, excluded={"D-1": "Merged into D-2"}, moves={"D-2": 1})
        items = r.items.set_index("demand_number")
        assert items.at["D-1", "reason"] == "Excluded by the PMO: Merged into D-2"
        assert items.at["D-2", "decision"] == "Deferred" and items.at["D-2", "start"] == "2028-Q1"

    def test_capacity_defers_a_demand(self):
        cap = [{"year": y, "assignment_group": "Team A", "available_fte": 4, "max_utilization_pct": 75}
               for y in (2027, 2028, 2029)]
        res = [{"demand_number": d, "year": 2027, "quarter": 1, "assignment_group": "Team A", "fte": 2}
               for d in ("D-1", "D-2")]
        r = _run(_ds([_demand("D-1", 5), _demand("D-2", 4)], [_plan("D-1", 10), _plan("D-2", 10)], capacity=cap,
                     resources=res))
        assert r.decided("Deferred") == ["D-2"]
        assert "Team A 2027-Q1 would need 4.0 FTE of 3.0 available" in r.items.set_index("demand_number").at[
            "D-2", "reason"]

    def test_scope_limits_the_scenario(self):
        r = _run(_ds([_demand("D-1"), _demand("D-2", business_unit="Sales")], [_plan("D-1", 10), _plan("D-2", 10)]),
                 scope="business_unit:Finance")
        assert list(r.items["demand_number"]) == ["D-1"]
        assert set(r.budget["Scope"]) == {"Finance"}

    def test_same_data_same_plan_in_any_row_order(self):
        demands = [_demand(f"D-{i}", i % 5 + 1) for i in range(8)]
        plans = [_plan(f"D-{i}", 150 + 10 * i, quarter=i % 4 + 1) for i in range(8)]
        first = _run(_ds(demands, plans)).items[["demand_number", "decision", "start"]]
        again = _run(_ds(list(reversed(demands)), list(reversed(plans)))).items[["demand_number", "decision", "start"]]
        pd.testing.assert_frame_equal(first, again)


# one case per verdict rule: (name, demands, plans, extra dataset args, scenario, expected verdict, expected fact)
SOFT = [{"year": 2027, "scope_type": "portfolio", "scope": "Apps", "capex_cap_eur": 500, "opex_cap_eur": 500,
         "cap_type": "soft"}]
CAP = [{"year": 2027, "assignment_group": "Team A", "available_fte": 2, "max_utilization_pct": 100}]
RES = [{"demand_number": "D-1", "year": 2027, "quarter": 1, "assignment_group": "Team A", "fte": 3}]
CASES = [
    ("hard limit of the binding year", [_demand("D-1")], [_plan("D-1", 1500)], {}, {"forced_in": {"D-1": "Board"}},
     "NO-GO", "budget_limit"),
    ("capacity of the binding year", [_demand("D-1")], [_plan("D-1", 10)], {"capacity": CAP, "resources": RES},
     {"forced_in": {"D-1": "Board"}}, "NO-GO", "capacity_limit"),
    ("mandatory demand does not fit", [_demand("D-1", None, demand_type="Mandatory", mandatory_reason="Law",
                                               latest_start_date="2027-01-01")],
     [_plan("D-1", 1500)], {}, {}, "NO-GO", "mandatory_out"),
    ("mandatory demand excluded", [_demand("D-1", None, demand_type="Mandatory", mandatory_reason="Law")],
     [_plan("D-1", 10)], {}, {"excluded": {"D-1": "later"}}, "NO-GO", "mandatory_out"),
    ("forced before its predecessor ends", [_demand("D-1", 2), _demand("D-2", 5, depends_on="D-1")],
     [_plan("D-1", 10, quarter=2), _plan("D-2", 10, quarter=1)], {}, {"forced_in": {"D-2": "Board"}}, "NO-GO",
     "dependency_violated"),
    ("forced demand without assessment", [_demand("D-1", None)], [_plan("D-1", 10)], {},
     {"forced_in": {"D-1": "CEO"}}, "ANALYSIS INCOMPLETE", "incomplete"),
    ("circular dependency", [_demand("D-1", depends_on="D-2"), _demand("D-2", depends_on="D-1")],
     [_plan("D-1", 10), _plan("D-2", 10)], {}, {}, "ANALYSIS INCOMPLETE", "incomplete"),
    ("soft limit", [_demand("D-1")], [_plan("D-1", 600)], {"envelopes": SOFT}, {"forced_in": {"D-1": "Board"}},
     "CONDITIONAL GO", "soft_limit"),
    ("preview year over its hard limit", [_demand("D-1")], [_plan("D-1", 1500, year=2028)], {},
     {"forced_in": {"D-1": "Board"}}, "CONDITIONAL GO", "preview_over"),
    ("preview year at the alert share", [_demand("D-1")], [_plan("D-1", 950, year=2028)], {}, {},
     "CONDITIONAL GO", "preview_alert"),
    ("forced over a better demand", [_demand("D-1", 5, latest_start_date="2027-01-01"), _demand("D-2", 1)],
     [_plan("D-1", 600), _plan("D-2", 600)], {}, {"forced_in": {"D-2": "Sponsor"}}, "CONDITIONAL GO",
     "forced_over_better"),
    ("run cost after go-live", [_demand("D-1", run_cost_after_go_live_eur=200)], [_plan("D-1", 100)], {}, {},
     "CONDITIONAL GO", "run_cost"),
    ("stale assessment", [_demand("D-1")], [_plan("D-1", 100)], {"assessed_on": "2025-06-01"}, {},
     "CONDITIONAL GO", "stale_assessment"),
    ("rough estimate of a large demand", [_demand("D-1", tshirt_size="XL")], [_plan("D-1", 100, confidence="Rough")],
     {}, {}, "CONDITIONAL GO", "rough_estimate"),
    ("rough estimate of a small demand", [_demand("D-1", tshirt_size="S")], [_plan("D-1", 100, confidence="Rough")],
     {}, {}, "GO", None),
]


@pytest.mark.parametrize(("name", "demands", "plans", "extra", "spec", "decision", "fact"), CASES,
                         ids=[c[0] for c in CASES])
def test_verdict_rules(name, demands, plans, extra, spec, decision, fact):
    r = _run(_ds(demands, plans, **extra), **spec)
    assert r.verdict.decision == decision, (name, r.facts)
    if fact:
        assert fact in _types(r)
    if decision != "GO":
        assert r.verdict.reasons or r.verdict.conditions


def test_no_case_is_a_wrong_go():
    wrong = [c[0] for c in CASES if c[5] != "GO" and _run(_ds(c[1], c[2], **c[3]), **c[4]).verdict.decision == "GO"]
    assert wrong == []


@pytest.fixture(scope="module")
def demo():
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return JsonPortfolioAdapter(SOURCE).load_dataset()


class TestDemoPortfolio:
    def test_base_plan(self, demo):
        r = run_scenario(demo, as_of=AS_OF)
        assert r.years == (2027, 2028, 2029)
        assert {"DMD-2027-002", "DMD-2027-004", "DMD-2027-016"} <= set(r.decided("In"))  # mandatory first
        assert r.decided("Out") and r.decided("Deferred")
        assert r.verdict.decision == "CONDITIONAL GO"
        assert not committed_capacity(demo, AS_OF).empty and not committed_costs(demo, AS_OF).empty

    def test_forcing_the_data_mesh_needs_a_board_resolution(self, demo):
        r = run_scenario(demo, ScenarioInput(forced_in={"DMD-2027-005": "Platform for 006 and 012"}), as_of=AS_OF)
        assert "soft_limit" in _types(r) and r.verdict.decision == "CONDITIONAL GO"

    def test_a_much_smaller_budget_drops_a_mandatory_demand(self, demo):
        r = run_scenario(demo, ScenarioInput(budget_factor_pct=-40), as_of=AS_OF)
        assert r.verdict.decision == "NO-GO" and "mandatory_out" in _types(r)


class TestSavedScenarios:
    @pytest.fixture
    def source(self, tmp_path):
        if not SOURCE.exists():
            pytest.skip("Test portfolio JSON not available")
        path = tmp_path / "portfolio.json"
        shutil.copy(SOURCE, path)
        return path

    def test_draft_is_appended_and_recalculates_to_the_same_plan(self, source):
        adapter = JsonPortfolioAdapter(source)
        ds = adapter.load_dataset()
        spec = ScenarioInput(name="Cut", budget_factor_pct=-10, forced_in={"DMD-2027-005": "Platform"},
                             excluded={"DMD-2027-015": ""}, moves={"DMD-2027-003": 1})
        result = run_scenario(ds, spec, as_of=AS_OF)
        changes = store.save_changes(ds, result, "PMO-ADMIN")
        with pytest.raises(WriteNotAuthorizedError):  # a plan is not a PM's row
            adapter.apply_changes(changes, user="Priya Raman", role=ROLE_PM)
        adapter.apply_changes(changes, user="PMO-ADMIN", role=ROLE_PMO, reason="planning_scenario")
        after = adapter.load_dataset()
        saved = store.saved_scenarios(after)
        assert list(saved["scenario_id"]) == ["SCN-2027-001"] and saved.at[0, "scenario_status"] == "Draft"
        assert store.input_of(saved.iloc[0]) == ScenarioInput(name="Cut", year_from=2027, year_to=2029,
                                                              budget_factor_pct=-10, scope=None,
                                                              forced_in={"DMD-2027-005": "Platform"},
                                                              excluded={"DMD-2027-015": ""},
                                                              moves={"DMD-2027-003": 1})
        items = store.items_of(after, "SCN-2027-001", "Draft")
        assert set(items["item_number"]) == set(result.items["demand_number"])
        again = run_scenario(after, store.input_of(saved.iloc[0]), as_of=AS_OF)
        pd.testing.assert_frame_equal(again.items[["demand_number", "decision"]],
                                      result.items[["demand_number", "decision"]])
        assert store.next_scenario_id(after, 2027) == "SCN-2027-002"
        assert store.approved_plan(after) is None

    def test_comparison(self, demo):
        base = run_scenario(demo, as_of=AS_OF)
        cut = run_scenario(demo, ScenarioInput(name="Cut", budget_factor_pct=-20), as_of=AS_OF)
        frame = store.comparison([base, cut]).set_index("Scenario")
        assert frame.at["Cut", "Out"] > frame.at["Base plan", "Out"]
        assert frame.at["Cut", "NPV planned"] < frame.at["Base plan", "NPV planned"]


class TestBoardPack:
    def test_excel_and_print_page(self, demo):
        import io

        from openpyxl import load_workbook

        from app.planning import reports

        base = run_scenario(demo, as_of=AS_OF)
        cut = run_scenario(demo, ScenarioInput(name="Cut <10 %>", budget_factor_pct=-10), as_of=AS_OF)
        book = load_workbook(io.BytesIO(reports.board_pack_xlsx(demo, cut, [base, cut])))
        assert book.sheetnames == ["Summary", "Ranking", "Decisions", "Budget", "Capacity", "Multi-year", "Comparison"]
        summary = {r[0]: r[1] for r in book["Summary"].iter_rows(min_row=2, values_only=True)}
        assert summary["Verdict (rule-based)"] == "NO-GO"
        page = reports.board_pack_html(demo, cut)
        assert "Cut &lt;10 %&gt;" in page and "<script" not in page and "NO-GO" in page
        totals = reports.multi_year(base).set_index("Business unit").loc["Total"]
        placed = base.items[base.items["decision"] != "Out"]
        assert totals["CapEx 2027"] == pytest.approx(placed["CapEx 2027"].sum())
