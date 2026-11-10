"""Every verdict rule, every guided scenario type and every approval outcome has at least one demo case in
evals/cases.jsonl that shows it on the demo portfolio – so each case of the engine can be demonstrated live."""
from __future__ import annotations

import pytest

from app.crew.scenarios import SCENARIOS
from scripts import run_evals
from tests.test_verdict_golden import ALL_RULES, rules_of

# Infrastructure faults cannot be produced from portfolio data; the golden set and their own tests cover them.
NOT_FROM_DATA = {"graph_unavailable", "internal_error"}
# A missing EAC is blocked by the required-field check on the demo data; its case runs as a pytest case on its own data.
# No demo project is in Planning with all mandatory gate criteria met, and none has two re-baselines in the last year:
# both run as golden cases.
# No demo project has reported actual benefits yet: a shortfall and an unreported tranche run as golden cases too.
VIA_PYTEST = {"eac_missing": "E12", "gate_advisory": "PT1", "rebaseline_count": "PT2", "benefit_shortfall": "PT3",
              "benefit_unreported": "PT4"}


@pytest.fixture(scope="module")
def demo_rules(offline_evaluation) -> dict[str, list[str]]:
    """Rule -> demo cases showing it, from the session's evaluation run (each case with its exact plan and as free
    text)."""
    _, verdicts = offline_evaluation
    ids, planning = {}, set()
    for case in run_evals.load_cases():
        ids.setdefault(case["request"], case["id"])
        if case["category"] == "planning":  # planning verdict rules have their golden cases in the planning tests
            planning.add(case["request"])
    seen: dict[str, list[str]] = {}
    for request, verdict in verdicts:
        for rule in rules_of(verdict) if verdict is not None and request not in planning else ():
            seen.setdefault(rule, []).append(ids.get(request, "?"))
    return seen


def test_every_rule_has_a_demo_case(demo_rules):
    assert sorted(ALL_RULES - NOT_FROM_DATA - set(VIA_PYTEST) - set(demo_rules)) == []


def test_rules_outside_the_demo_data_have_their_own_case():
    ids = {c["id"] for c in run_evals.load_cases() if c.get("run_via") == "pytest"}
    assert set(VIA_PYTEST.values()) <= ids


def test_every_scenario_type_has_a_demo_case():
    actions = set()
    for case in run_evals.load_cases():
        plan = case["expected_plan"] or []
        actions |= {step["action"] for step in (plan if isinstance(plan, list) else [plan])}
    assert sorted(set(SCENARIOS) - actions) == []


def test_every_approval_outcome_has_a_demo_case():
    cases = [c for c in run_evals.load_cases() if c["category"] == "approval"]
    assert {c["expected_verdict"] for c in cases} >= {"APPROVED", "REJECTED", "REFUSED"}
    assert {c.get("expected_log_reason") for c in cases} >= {"self_approval", "self_rejection", "not_approver"}
    assert all(c["test"].startswith("tests/test_demo_approvals.py::") for c in cases)
