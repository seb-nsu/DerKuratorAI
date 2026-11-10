"""Decision review and rule lab (E6): decided values against today's data, review stages after six and twelve
months, overdue conditions, the hit rate per verdict, and candidate rules that apply to one run only."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.business_rules import BusinessRules, candidate_rules, current_rules
from app.crew.decision_review import review_decisions

TODAY = date(2026, 9, 25)
PROJECTS = pd.DataFrame([
    {"project_number": "P1", "estimate_at_completion_eur": 95_000, "planned_end_date": "2027-01-31"},
    {"project_number": "P2", "estimate_at_completion_eur": 130_000, "planned_end_date": "2027-05-31"},
])


def _proposal(pid, field, new, decided_at, verdict="GO", status="approved", conditions=(), override=False):
    return {"id": f"{pid}-{field}", "status": status, "decided_at": decided_at, "projects": [pid],
            "override": override,
            "changes": [{"project_id": pid, "table": "Projects", "field": field, "old": None, "new": new}],
            "verdict_record": {"decision": verdict, "rule": "", "condition_details": list(conditions)}}


def test_decided_budget_and_end_date_against_today():
    proposals = [_proposal("P1", "budget_cost_eur", 100_000, "2026-03-01T10:00:00+01:00"),
                 _proposal("P2", "budget_cost_eur", 100_000, "2025-08-01T10:00:00+02:00", verdict="CONDITIONAL GO"),
                 _proposal("P2", "planned_end_date", "2027-03-31", "2026-08-01T10:00:00+02:00")]
    out = review_decisions(proposals, PROJECTS, TODAY, BusinessRules())
    by_id = {d["proposal_id"]: d for d in out["decisions"]}
    p1 = by_id["P1-budget_cost_eur"]
    assert p1["held"] is True and p1["review_stage"] == "6 months" and p1["age_days"] == 208
    p2 = by_id["P2-budget_cost_eur"]
    assert p2["review_stage"] == "12 months" and p2["held"] is False
    assert p2["outcomes"][0]["deviation_pct"] == 30.0
    assert p2["outcomes"][0]["text"] == "forecast €130,000 is 30.0 % over the decided budget €100,000"
    late = by_id["P2-planned_end_date"]
    assert late["review_stage"] is None and late["next_review"] == "2027-01-28"
    assert late["outcomes"][0]["deviation_days"] == 61 and late["held"] is False
    rates = {r["verdict"]: r for r in out["hit_rate"]}
    assert rates["GO"]["decisions"] == 2 and rates["GO"]["hit_rate_pct"] == 50.0
    assert rates["CONDITIONAL GO"]["hit_rate_pct"] == 0.0 and out["due_for_review"] == 2


def test_overdue_conditions_overrides_and_open_proposals():
    proposals = [_proposal("P1", "budget_cost_eur", 100_000, "2026-09-01T10:00:00+02:00", verdict="NO-GO",
                           override=True, conditions=[{"condition": "Re-plan", "owner_role": "PM", "due": "2026-09-15"},
                                                      {"condition": "Later", "owner_role": "PM", "due": "2026-12-01"}]),
                 _proposal("P2", "budget_cost_eur", 1, "2026-09-01T10:00:00+02:00", status="submitted")]
    out = review_decisions(proposals, PROJECTS, TODAY, BusinessRules())
    assert len(out["decisions"]) == 1
    decision = out["decisions"][0]
    assert decision["verdict"] == "NO-GO (overridden)"
    assert decision["conditions_to_check"] == [{"condition": "Re-plan", "owner_role": "PM", "due": "2026-09-15"}]
    assert out["conditions_overdue"] == 1


def test_a_project_that_left_the_data_is_reported_not_scored():
    out = review_decisions([_proposal("P9", "budget_cost_eur", 1, "2025-01-01T10:00:00+01:00")], PROJECTS, TODAY)
    decision = out["decisions"][0]
    assert decision["outcomes"][0]["status"] == "gone" and decision["held"] is None and out["hit_rate"] == []


def test_candidate_rules_apply_inside_the_block_only():
    saved = current_rules().eac_overrun_nogo_pct
    with candidate_rules(current_rules().merged({"eac_overrun_nogo_pct": 42})):
        assert current_rules().eac_overrun_nogo_pct == 42
    assert current_rules().eac_overrun_nogo_pct == saved


def test_the_rule_lab_lists_changed_verdicts_and_flags_wrong_gos():
    from scripts.rule_lab import run_lab

    loose = run_lab({"eac_overrun_nogo_pct": 60}, ["E2"])
    assert loose.cases == 1 and loose.changed == [
        {"id": "E2", "category": "clear", "expected": "NO-GO", "before": "NO-GO", "after": "CONDITIONAL GO"}]
    assert loose.wrong_gos == [] and "1 of 1 reference verdicts change, no wrong GO" in loose.headline
    assert run_lab({"eac_overrun_nogo_pct": 15}, ["E2"]).changed == []


def test_the_rule_lab_refuses_invalid_rules():
    from pydantic import ValidationError

    from scripts.rule_lab import main, run_lab

    with pytest.raises(ValidationError):
        run_lab({"status_cost_yellow_pct": 50, "status_cost_red_pct": 10}, ["E1"])
    assert main(["status_cost_yellow_pct=50", "--cases", "E1"]) == 2
