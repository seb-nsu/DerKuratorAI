"""Strategy score from the weighted goal contributions and the value-optimal portfolio cut (min-cover knapsack)
next to the ranked cut list."""
from __future__ import annotations

import dataclasses
import random
from itertools import combinations

import pandas as pd
import pytest

from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools import portfolio_tools as pt
from app.crew.tools.data_steward_tools import check_strategy
from app.data_adapters.dataset import strategy_scores

GOALS = pd.DataFrame([{"goal_id": "G1", "goal_name": "Growth", "weight": 3},
                      {"goal_id": "G2", "goal_name": "Efficiency", "weight": 1},
                      {"goal_id": "G0", "goal_name": "Retired", "weight": 0}])
CONTRIBUTIONS = pd.DataFrame([{"project_number": "P-B", "goal_id": "G1", "contribution": 5},
                              {"project_number": "P-C", "goal_id": "G2", "contribution": 2},
                              {"project_number": "P-C", "goal_id": "G0", "contribution": 5}])


@pytest.fixture
def portfolio(dataset_factory, project_factory):
    ds = dataset_factory([
        project_factory(project_number="P-A", project_name="Mandatory", priority=1, value_score=5),
        project_factory(project_number="P-B", project_name="Low prio", priority=3, value_score=2,
                        budget_cost_eur=1_000_000, actual_cost_eur=100_000, total_planned_cost_eur=1_000_000,
                        planned_capital_eur=400_000, planned_operating_eur=600_000, estimate_at_completion_eur=1_000_000),
        project_factory(project_number="P-C", project_name="Mid prio", priority=2, value_score=3,
                        budget_cost_eur=900_000, actual_cost_eur=0, total_planned_cost_eur=900_000,
                        planned_capital_eur=400_000, planned_operating_eur=500_000, estimate_at_completion_eur=900_000),
        project_factory(project_number="P-D", project_name="Reporting refresh", priority=2, value_score=2,
                        budget_cost_eur=500_000, actual_cost_eur=0, total_planned_cost_eur=500_000,
                        planned_capital_eur=200_000, planned_operating_eur=300_000, estimate_at_completion_eur=500_000),
    ])
    return dataclasses.replace(ds, strategic_goals=GOALS, project_goals=CONTRIBUTIONS)


def _facts(kind):
    return [f for f in ctx.get_facts() if f["type"] == kind]


class TestStrategyScore:
    def test_weighted_contribution_with_value_score_fallback(self, portfolio):
        view = portfolio.project_view().set_index("project_number")
        # P-B: 3 x 5 / (4 x 5) = 75; P-C: 1 x 2 / 20 = 10 (zero-weight goal ignored); P-D: value 2 x 20
        assert view["strategy_score"].to_dict() == {"P-A": 100.0, "P-B": 75.0, "P-C": 10.0, "P-D": 40.0}

    def test_without_goals_the_value_score_counts(self):
        projects = pd.DataFrame([{"project_number": "P-1", "value_score": 3}, {"project_number": "P-2"}])
        assert strategy_scores(projects, pd.DataFrame(), pd.DataFrame()).tolist() == [60.0, 0.0]

    def test_unknown_goal_and_bad_contribution_are_warnings(self, portfolio):
        bad = pd.concat([CONTRIBUTIONS, pd.DataFrame([{"project_number": "P-D", "goal_id": "GX", "contribution": 3},
                                                      {"project_number": "P-D", "goal_id": "G1", "contribution": 9}])])
        issues = check_strategy(dataclasses.replace(portfolio, project_goals=bad))
        assert [(i.severity, i.column) for i in issues] == [("WARNING", "goal_id"), ("WARNING", "contribution")]


class TestValueCut:
    def test_least_strategic_value_lost_against_the_ranked_list(self, portfolio):
        ctx.set_portfolio_context(portfolio, {})
        text = pt.portfolio_cut(800_000, "value")
        assert "value-optimal cut list of 1 project stop(s), net release €828,000, strategic value lost 10" in text
        assert "P-C (Mid prio) – strategy score 10" in text
        assert "ranked cut list (priority first): P-B – net release €828,000, strategic value lost 75 (+65" in text
        cut = _facts("portfolio_cut")[0]
        assert (cut["method"], cut["projects"], cut["ranked_projects"]) == ("value", ["P-C"], ["P-B"])
        assert {c["project_id"] for c in ctx.get_ledger() if c["new"] == "Canceled"} == {"P-C"}

    def test_two_stops_and_the_protected_project_stays(self, portfolio):
        ctx.set_portfolio_context(portfolio, {})
        text = pt.portfolio_cut(1_200_000, "value")
        assert sorted(_facts("portfolio_cut")[0]["projects"]) == ["P-C", "P-D"]  # 50 lost; P-B + P-D would lose 115
        assert "P-A (protected: priority 1)" in text

    def test_unreachable_target_stops_everything_eligible(self, portfolio):
        ctx.set_portfolio_context(portfolio, {})
        text = pt.portfolio_cut(50_000_000, "value")
        assert "⚠ Savings target not reached" in text
        assert sorted(_facts("portfolio_cut")[0]["projects"]) == ["P-B", "P-C", "P-D"]

    def test_unknown_method_is_an_input_error(self, portfolio):
        ctx.set_portfolio_context(portfolio, {})
        assert pt.portfolio_cut(800_000, "random").startswith(pt.INPUT_PREFIX)

    def test_knapsack_matches_brute_force(self):
        rng = random.Random(7)  # noqa: S311 - reproducible test data
        for _ in range(40):
            cands = [{"pid": f"P{i}", "release": rng.randint(50, 900) * 1000.0, "strategy": float(rng.randint(0, 100))}
                     for i in range(rng.randint(1, 9))]
            target = rng.randint(100, 3000) * 1000.0
            got = pt._min_value_cut(cands, target)
            hits = [c for n in range(1, len(cands) + 1) for c in combinations(cands, n)
                    if sum(x["release"] for x in c) >= target]
            if not hits:
                assert got == cands
                continue
            loss = lambda combo: sum(x["strategy"] + pt.STOP_PENALTY for x in combo)  # noqa: E731
            assert sum(x["release"] for x in got) >= target
            assert loss(got) == pytest.approx(min(loss(c) for c in hits))


class TestParserAndBuilder:
    def test_strategic_wording_selects_the_value_method(self, portfolio, monkeypatch):
        seen = {}
        monkeypatch.setattr(pt, "portfolio_cut", lambda target, method="ranked": seen.update(method=method) or "RESULT")
        plan = pipeline.heuristic_plan("Find €1M savings with the least strategic value lost", portfolio.projects)
        assert [i.description for i in plan.sub_intents if i.intent_type == "simulation"] == ["portfolio_cut"]
        assert pipeline._has("find €1m savings, value-optimal", pipeline._VALUE_CUT_KW)
        assert not pipeline._has("find €1m savings", pipeline._VALUE_CUT_KW)

    def test_guided_scenario_carries_the_method(self):
        scenario = build_guided_scenario("portfolio_cut", "", {}, target_eur=1_000_000, method="value")
        assert scenario.plan.sub_intents[0].parameters["method"] == "value"
        assert "least strategic value lost" in scenario.request
        with pytest.raises(ValueError):
            build_guided_scenario("portfolio_cut", "", {}, target_eur=1_000_000, method="magic")
