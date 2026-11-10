"""Planning 1: demands, assessment and rank – deterministic rank, visibility like projects, Data Steward checks for
demands, assessments as appended rows, weight changes only with four eyes."""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core import proposals
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO, apply_rls_to_dataset, is_governed_change
from app.data_adapters import JsonPortfolioAdapter, WriteNotAuthorizedError
from app.data_adapters.dataset import PortfolioDataset
from app.planning import assessment
from app.planning.scoring import ranking, score_breakdown
from app.planning.steward import blocked_demands, check_demands

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)
MODEL = [{"criterion": "Value", "weight": 60, "scale_min": 1, "scale_max": 5, "direction": "higher is better"},
         {"criterion": "Risk", "weight": 40, "scale_min": 1, "scale_max": 5, "direction": "lower is better"}]


def _demand(number, **kw):
    return {"demand_number": number, "demand_name": f"Demand {number}", "demand_owner": "Priya Raman",
            "business_unit": "Finance", "portfolio": "Apps", "demand_type": "New", "demand_status": "Assessed",
            "earliest_start_date": "2027-01-01", "duration_months": 6, "net_present_value_eur": 100, **kw}


def _scores(number, value, risk, on="2026-09-01"):
    return [{"score_number": f"{number}-v-{on}", "demand_number": number, "criterion": "Value", "score": value,
             "assessed_on": on},
            {"score_number": f"{number}-r-{on}", "demand_number": number, "criterion": "Risk", "score": risk,
             "assessed_on": on}]


def _ds(demands, scores, plans=None):
    plans = plans if plans is not None else [{"demand_number": d["demand_number"], "year": 2027, "quarter": 1,
                                              "capex_eur": 100, "opex_eur": 10} for d in demands]
    return PortfolioDataset.from_tables({"Projects": pd.DataFrame([{"project_number": "PRJ-1",
                                                                    "business_unit": "Finance", "portfolio": "Apps"}]),
                                         "Demands": pd.DataFrame(demands), "Scoring_Model": pd.DataFrame(MODEL),
                                         "Demand_Scores": pd.DataFrame(scores),
                                         "Demand_Year_Plans": pd.DataFrame(plans)})


@pytest.fixture(scope="module")
def demo():
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return JsonPortfolioAdapter(SOURCE).load_dataset()


class TestRanking:
    def test_score_is_weighted_normalised_and_turns_lower_is_better(self):
        ds = _ds([_demand("D-1")], _scores("D-1", 5, 1))
        assert ranking(ds).at[0, "score"] == 100.0  # best value, lowest risk
        ds = _ds([_demand("D-1")], _scores("D-1", 3, 5))
        assert ranking(ds).at[0, "score"] == 30.0  # value 0.5 x 60 + risk (worst) 0 x 40

    def test_order_mandatory_score_npv_latest_start_number(self):
        demands = [_demand("D-5"), _demand("D-4", net_present_value_eur=500), _demand("D-3", latest_start_date="2027-03-01"),
                   _demand("D-2", latest_start_date="2027-06-01"), _demand("D-9", demand_type="Mandatory",
                                                                           mandatory_reason="Regulation"),
                   _demand("D-1"), _demand("D-0", demand_status="Submitted")]
        scores = sum((_scores(d, 4, 2) for d in ("D-5", "D-4", "D-3", "D-2", "D-1")), [])
        scores += _scores("D-1", 5, 1, on="2026-09-20")  # a later assessment replaces the earlier one
        ranked = ranking(_ds(demands, scores))
        assert list(ranked["demand_number"]) == ["D-9", "D-1", "D-4", "D-3", "D-2", "D-5", "D-0"]
        assert not ranked.set_index("demand_number").at["D-0", "assessed"]

    def test_same_data_gives_the_same_rank_in_any_row_order(self, demo):
        first = ranking(demo)[["demand_number", "rank"]]
        shuffled = demo.copy()
        shuffled.demands = shuffled.demands.sample(frac=1, random_state=3)
        shuffled.demand_scores = shuffled.demand_scores.sample(frac=1, random_state=5)
        pd.testing.assert_frame_equal(ranking(shuffled)[["demand_number", "rank"]], first)

    def test_demo_ranking(self, demo):
        ranked = ranking(demo)
        assert list(ranked["demand_number"][:4]) == ["DMD-2027-002", "DMD-2027-004", "DMD-2027-016", "DMD-2027-001"]
        parts = score_breakdown(demo)
        one = parts[parts["demand_number"] == "DMD-2027-001"]
        assert one["contribution"].sum() == pytest.approx(ranked.set_index("demand_number").at["DMD-2027-001", "score"],
                                                          abs=0.1)


class TestVisibility:
    def test_pm_sees_own_demands_pmo_all_admin_none(self, demo):
        pm = apply_rls_to_dataset(demo, ROLE_PM, "Priya Raman")
        assert set(pm.demands["demand_owner"]) == {"Priya Raman"}
        assert set(pm.demand_year_plans["demand_number"]) <= set(pm.demands["demand_number"])
        assert set(pm.demand_scores["demand_number"]) <= set(pm.demands["demand_number"])
        assert len(pm.scoring_model) == len(demo.scoring_model)  # the model holds no demand data
        assert len(apply_rls_to_dataset(demo, ROLE_PMO, "PMO-ADMIN").demands) == len(demo.demands)
        assert apply_rls_to_dataset(demo, ROLE_ADMIN, "SYSTEM-ADMIN").demands.empty


class TestDemandSteward:
    def test_demo_demands_have_no_errors(self, demo):
        assert blocked_demands(check_demands(demo, AS_OF)) == {}

    def test_findings(self):
        demands = [_demand("D-1", demand_type="Mandatory"), _demand("D-2", depends_on="D-404"),
                   _demand("D-3", latest_start_date="2026-01-01", earliest_start_date="2027-01-01"),
                   _demand("D-4", business_unit="Atlantis")]
        ds = _ds(demands, _scores("D-2", 9, 1), plans=[{"demand_number": "D-2", "year": 2027, "quarter": 5,
                                                        "capex_eur": -1, "opex_eur": 0}])
        issues = {(i.project_id, i.column, i.severity) for i in check_demands(ds, AS_OF)}
        assert ("D-1", "mandatory_reason", "ERROR") in issues
        assert ("D-1", "Demand_Year_Plans", "ERROR") in issues
        assert ("D-2", "depends_on", "WARNING") in issues
        assert ("D-2", "quarter", "ERROR") in issues and ("D-2", "capex_eur", "ERROR") in issues
        assert ("D-2", "score", "ERROR") in issues
        assert ("D-3", "latest_start_date", "ERROR") in issues
        assert ("D-4", "business_unit", "WARNING") in issues

    def test_a_project_managers_view_names_no_foreign_predecessor(self):
        """In a PM's view the other projects are filtered out – the reference check would name them."""
        ds = _ds([_demand("D-2", depends_on="PRJ-OTHER-9")], _scores("D-2", 9, 1))
        assert any(i.column == "depends_on" for i in check_demands(ds, AS_OF))
        assert not any(i.column == "depends_on" for i in check_demands(ds, AS_OF, references=False))


@pytest.fixture
def source(tmp_path):
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    path = tmp_path / "portfolio.json"
    shutil.copy(SOURCE, path)
    return path


class TestAssessmentWrites:
    def test_assessment_appends_scores_and_sets_assessed(self, source):
        adapter = JsonPortfolioAdapter(source)
        ds = adapter.load_dataset()
        scores = {"Strategic contribution": 4, "Economic value": 5, "Risk of not acting": 3, "Delivery risk": 2,
                  "Urgency": 3}
        changes = assessment.score_changes(ds, "DMD-2027-017", scores, "PMO-ADMIN")
        assert changes[-1]["field"] == "demand_status" and changes[-1]["new"] == "Assessed"
        adapter.apply_changes(changes, user="PMO-ADMIN", reason="demand_assessment", role=ROLE_PMO)
        after = adapter.load_dataset()
        row = ranking(after).set_index("demand_number").loc["DMD-2027-017"]
        assert row["assessed"] and row["demand_status"] == "Assessed"
        assert len(after.demand_scores) == len(ds.demand_scores) + 5

    def test_scale_is_checked(self, demo):
        with pytest.raises(ValueError, match="outside the scale"):
            assessment.score_changes(demo, "DMD-2027-017", {"Urgency": 9}, "PMO-ADMIN")

    def test_pm_cannot_assess_a_foreign_demand(self, source):
        adapter = JsonPortfolioAdapter(source)
        changes = assessment.score_changes(adapter.load_dataset(), "DMD-2027-017", {"Urgency": 3}, "Priya Raman")
        with pytest.raises(WriteNotAuthorizedError):
            adapter.apply_changes(changes, user="Priya Raman", role=ROLE_PM)

    def test_weight_change_needs_a_second_pmo(self, source):
        adapter = JsonPortfolioAdapter(source)
        changes = assessment.weight_changes(adapter.load_dataset(), {"Urgency": 20})
        assert changes and all(is_governed_change(c) for c in changes)
        with pytest.raises(WriteNotAuthorizedError):
            adapter.apply_changes(changes, user="PMO-ADMIN", role=ROLE_PMO)
        entry = proposals.submit_proposal("PMO-ADMIN", ROLE_PMO, "weights", "", None, changes)
        with pytest.raises(WriteNotAuthorizedError):  # never the requester
            adapter.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"])
        result = adapter.apply_changes(entry["changes"], user="PMO-DEPUTY", role=ROLE_PMO, proposal_id=entry["id"])
        assert result.applied == 1 and result.operation_id
        model = json.loads(source.read_text(encoding="utf-8"))["Scoring_Model"]
        assert next(m for m in model if m["criterion"] == "Urgency")["weight"] == 20
