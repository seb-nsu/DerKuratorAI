"""Planning questions in the chat: the deterministic parser turns them into a scenario input, the planning engine
answers. No change request to a project may be read as a planning question (the whole evaluation set is checked)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core.security import ROLE_PM, ROLE_PMO
from app.crew.pipeline import find_projects_in_text, run_steering_pipeline
from app.data_adapters import JsonPortfolioAdapter
from app.planning.chat import is_planning_question, parse_question

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)

# question -> expected input fields (anything not named: the default)
QUESTIONS = [
    ("What drops out in 2027 at -10 %?", {"year_from": 2027, "budget_factor_pct": -10}),
    ("Was fällt 2027 raus bei −10 %?", {"year_from": 2027, "budget_factor_pct": -10}),
    ("Which demands fall out if we cut the envelopes by 15 percent?", {"budget_factor_pct": -15}),
    ("Show the annual plan 2027-2029", {"year_from": 2027, "year_to": 2029}),
    ("Annual plan with 5 % more budget", {"budget_factor_pct": 5}),
    ("Jahresplanung Shared Services mit 20 % weniger Budget",
     {"budget_factor_pct": -20, "scope": "business_unit:Shared Services"}),
    ("Planning scenario for the portfolio Data & Insights", {"scope": "portfolio:Data & Insights"}),
    ("Which demands fit into 2027 if Data Catalogue Foundation is forced in?",
     {"year_from": 2027, "forced_in": {"DMD-2027-005": "named in the question"}}),
    ("Annual plan without DMD-2027-003 and DMD-2027-001 later",
     {"excluded": {"DMD-2027-003": "named in the question"}, "moves": {"DMD-2027-001": 1}}),
    ("Jahresplan: DMD-2027-014 vorziehen", {"moves": {"DMD-2027-014": -1}}),
    ("Value-optimal annual plan with 10 % more budget", {"budget_factor_pct": 10, "method": "value"}),
    ("Jahresplanung 2027 mit dem höchsten Wert", {"year_from": 2027, "method": "value"}),
]
NOT_PLANNING = ["Cut the budget of General Ledger Consolidation by 20 %", "Delay PRJ-DEMO-1001 by 3 months",
                "What drops out of PRJ-DEMO-1002 in 2027?", "Which projects are blocked by the supplier?",
                # everyday words of the planning vocabulary without a planning year or a percentage
                "Which teams have the highest skill demand?", "Welche Vorhaben hängen am Lieferanten?",
                "Is the demand for SAP consultants covered?"]


@pytest.fixture(scope="module")
def demo():
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return JsonPortfolioAdapter(SOURCE).load_dataset()


def _is_planning(text, ds):
    return is_planning_question(text, ds, find_projects_in_text(text, ds.projects), AS_OF)


@pytest.mark.parametrize(("question", "expected"), QUESTIONS, ids=[q for q, _ in QUESTIONS])
def test_planning_questions_are_understood(demo, question, expected):
    assert _is_planning(question, demo)
    spec = parse_question(question, demo, AS_OF)
    defaults = {"year_from": None, "year_to": None, "budget_factor_pct": 0, "scope": None, "forced_in": {},
                "excluded": {}, "moves": {}, "method": "rank"}
    assert {k: getattr(spec, k) for k in defaults} == {**defaults, **expected}


@pytest.mark.parametrize("request_text", NOT_PLANNING)
def test_change_requests_stay_change_requests(demo, request_text):
    assert not _is_planning(request_text, demo)


def test_only_the_planning_evaluation_cases_are_read_as_planning_questions(demo):
    cases = [json.loads(line) for line in (ROOT / "evals" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    golden = json.loads((ROOT / "tests" / "golden" / "verdict_cases.json").read_text(encoding="utf-8"))["cases"]
    planning = {c["request"] for c in cases if c["category"] == "planning" and c["expected_verdict"] != "NO DECISION REQUIRED"}
    requests = [c["request"] for c in cases] + [c["request"] for c in golden if c.get("request")]
    assert planning and {r for r in requests if _is_planning(r, demo)} == planning


def test_pipeline_answers_with_the_planning_engine(demo):
    seen = {}
    result = run_steering_pipeline("What drops out in 2027 at -10 %?", demo, ROLE_PMO, "PMO-ADMIN", as_of=AS_OF,
                                   on_verdict=seen.update)
    assert result.mode == "planning" and result.verdict.decision in ("NO-GO", "CONDITIONAL GO", "GO")
    assert seen["verdict"] is result.verdict and result.proposed_changes == []
    assert "### Out of the plan" in result.decision_paper and "budget envelopes -10 %" in result.decision_paper
    assert "rule-based" in result.decision_paper


def test_a_project_manager_gets_no_portfolio_plan(demo):
    result = run_steering_pipeline("Show the annual plan 2027", demo, ROLE_PM, "Priya Raman", as_of=AS_OF)
    assert result.mode == "planning" and result.verdict is None
    assert "PMO" in result.decision_paper and "DMD-" not in result.decision_paper


def test_a_value_optimal_question_explains_the_difference_to_the_rank_order(demo):
    result = run_steering_pipeline("Optimise the annual plan with 10 % more budget", demo, ROLE_PMO, "PMO-ADMIN",
                                   as_of=AS_OF)
    paper = result.decision_paper
    assert result.mode == "planning" and "value-optimal choice of the demands" in paper
    assert "### What the optimizer does differently from the rank order" in paper
    assert "DMD-2027-005" in paper and "Score sum of the plan" in paper
