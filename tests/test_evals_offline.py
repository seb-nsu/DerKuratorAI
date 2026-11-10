"""The evaluation case set through the deterministic offline parser (no network, no keys). Fails on any mismatch of the
reference and access cases (verdict and plan), on any wrong adversarial verdict and on any wrong GO in the whole set.
The other categories (paraphrase, German, vague, multi-part, multi-step) measure the language layer and are reported,
not enforced – see scripts/run_evals.py and docs/evals/EVAL_REPORT.md. The LLM mode needs API keys and stays out of
CI."""
from __future__ import annotations

import pytest

from scripts import run_evals

ENFORCED = ("clear", "access", "demo", "pmo", "planning")  # verdict and plan
# adversarial: the verdict must hold (safety). The plan is reported only – e.g. X2 reads the injected "50 %" as the
# cut amount and still ends at NO-GO for the wrong reason, which the report lists as a parser weakness.
VERDICT_ONLY = ("adversarial",)


@pytest.fixture(scope="module")
def offline(offline_evaluation):
    return offline_evaluation[0].runs


def test_every_case_was_evaluated(offline):
    expected = [c["id"] for c in run_evals.load_cases() if c.get("run_via") != "pytest"]
    assert [r.id for r in offline] == expected


def test_no_wrong_go_in_the_whole_set(offline):
    assert [r.id for r in offline if r.wrong_go] == []


@pytest.mark.parametrize("category", ENFORCED)
def test_enforced_categories_match(offline, category):
    runs = [r for r in offline if r.category == category]
    assert runs
    mismatches = [(r.id, r.expected, r.actual, r.plan_correct) for r in runs
                  if not r.verdict_correct or r.plan_correct is False]
    assert mismatches == []


@pytest.mark.parametrize("category", VERDICT_ONLY)
def test_verdict_holds(offline, category):
    runs = [r for r in offline if r.category == category]
    assert runs and [(r.id, r.actual) for r in runs if not r.verdict_correct] == []


def test_self_approval_leaves_its_security_event(offline):
    e8 = next(r for r in offline if r.id == "E8")
    assert e8.log_ok is True


def test_report_renders_without_names_or_keys(offline):
    report = run_evals.render_report(run_evals.Evaluation(cases=run_evals.load_cases(), runs=offline))
    assert "## Summary" in report and "Wrong GOs" in report
    assert "Priya Raman" not in report and "api_key" not in report.lower()


def test_stale_expectation_stops_the_run():
    case = dict(next(c for c in run_evals.load_cases() if c["id"] == "E1"), expected_verdict="NO-GO")
    with run_evals.isolated_environment() as env, pytest.raises(run_evals.StaleExpectation, match="E1: expected NO-GO"):
        run_evals.ground_truth([case], env["dataset"])


def test_conditions_are_compared_without_figures():
    assert run_evals.condition_kinds(["Close the forecast gap of €185,000 on PRJ-DEMO-1002 (scope)."]) == \
        run_evals.condition_kinds(["Close the forecast gap of €190,500 on PRJ-DEMO-1002 (scope)."])
