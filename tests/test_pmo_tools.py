"""The PMO tools on the golden portfolio: the inputs they refuse and the parser's reading of free-text PMO requests.
The verdict of each tool's normal and rule cases is pinned in tests/golden/verdict_cases.json (GC-41 to GC-58)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.security import ROLE_PMO
from app.crew import pipeline
from app.crew.scenarios import build_guided_scenario
from tests.test_verdict_golden import golden_dataset, golden_graph, run_case  # noqa: F401 - pytest fixtures

AS_OF = "2029-06-30"


def _case(action, project=None, target=None, rules=None, **params):
    return {"scenario": {"action": action, "project": project, "target": target, "params": params}, "as_of": AS_OF,
            **({"rules": rules} if rules else {})}


@pytest.mark.parametrize(("case", "verdict", "reason"), [
    (_case("resume", "G-02"), "ANALYSIS INCOMPLETE", "not on hold"),
    (_case("resume", "G-13", restart_date="2029-01-01"), "ANALYSIS INCOMPLETE", "lies before the reference date"),
    (_case("cost_stress", pct=5, cost_type="all", scope="Nowhere"), "ANALYSIS INCOMPLETE", "matches no portfolio"),
    (_case("cost_stress", pct=500, cost_type="all"), "NO-GO", "exceeds the limit"),
    (_case("stage_gate", "G-14", target_phase="Closing"), "ANALYSIS INCOMPLETE", ""),
    (_case("stage_gate", "G-02"), "ANALYSIS INCOMPLETE", ""),
    (_case("reserve_draw", "G-02", amount=2_000_000, reason="too much"), "NO-GO", ""),
    (_case("skill_transfer", "G-11", "G-02", skill="Unknown Skill", fte=1, months=1), "ANALYSIS INCOMPLETE",
     "not found"),
    (_case("skill_transfer", "G-11", "G-02", skill="Golden Skill", fte=9, months=1), "NO-GO",
     "Insufficient skill capacity"),
    (_case("risk_response", "G-02", risk_number="GK-404", response="Mitigate"), "ANALYSIS INCOMPLETE", "not found"),
], ids=["resume-not-paused", "resume-in-the-past", "stress-unknown-scope", "stress-above-limit", "gate-skips-a-phase",
        "gate-without-phase", "reserve-insufficient", "skill-unknown", "skill-more-than-available", "risk-unknown"])
def test_refused_inputs(run_case, case, verdict, reason):  # noqa: F811
    result = run_case(case)
    assert result.verdict.decision == verdict, result.verdict.to_markdown()
    assert not result.proposed_changes
    assert any(reason in r for r in result.verdict.reasons), result.verdict.to_markdown()


def test_a_stress_test_writes_nothing(run_case):  # noqa: F811
    assert run_case(_case("cost_stress", pct=20, cost_type="opex")).proposed_changes == []


def test_a_hold_and_a_resume_in_one_scenario(run_case, golden_dataset):  # noqa: F811
    """Resume after a hold of the same scenario: the hold already paid the ramp-up and the state returns."""
    hold = run_case(_case("on_hold", "G-02", months=2, retention_pct=50))
    names = dict(zip(golden_dataset.projects["project_number"], golden_dataset.projects["project_name"], strict=True))
    guided = build_guided_scenario("resume", "G-02", names, None)
    resumed = pipeline.run_steering_pipeline(guided.request, golden_dataset, ROLE_PMO, "PMO", plan=guided.plan,
                                             base_state=hold.scenario_state, as_of=date.fromisoformat(AS_OF))
    states = [(c["field"], c["new"]) for c in resumed.proposed_changes if c["field"] == "state"]
    assert states[-1] == ("state", "Work in Progress")
    assert "already costed in the hold" in resumed.evidence


@pytest.mark.parametrize(("request_text", "family"), [
    ("Resume Paused Service", "resume"),
    ("Paused Service wieder aufnehmen", "resume"),
    ("Stress test: all costs +8 %", "cost_stress"),
    ("Was passiert bei einer Kostensteigerung von 10 %?", "cost_stress"),
    ("Re-baseline Healthy Ops to EUR 550k because of the scope", "rebaseline"),
    ("Neue Baseline für Healthy Ops mit 550.000 €", "rebaseline"),
    ("Check the stage gate of Gate Ready", "stage_gate"),
    ("Is Gate Ready ready for the next phase?", "stage_gate"),
    ("Draw EUR 50k from the management reserve for Healthy Ops", "reserve_draw"),
    ("Healthy Ops gibt 50.000 € an die Managementreserve zurück", "reserve_return"),
    ("Move 0.5 FTE Golden Skill from Large Team to Healthy Ops", "skill_transfer"),
    ("Accept risk GK-2", None),  # no RSK- number: the parser does not guess a risk
])
def test_parser_reads_pmo_requests(golden_dataset, request_text, family):  # noqa: F811
    plan = pipeline.heuristic_plan(request_text, golden_dataset.projects)
    sims = [i.description.split(":", 1)[0] for i in plan.sub_intents if i.intent_type == "simulation"]
    if family is None:
        assert "risk_response" not in sims
    else:
        assert sims[:1] == [family], sims


def test_data_steward_checks_the_pmo_data(dataset_factory):
    from app.crew.tools.data_steward_tools import check_pmo_tables

    ds = dataset_factory()
    ds.projects["phase"] = "Build"
    ds.projects.loc[ds.projects.index[0], "phase"] = "Warp drive"
    ds.risks = pd.DataFrame([{"risk_number": "R-1", "project_number": ds.project_numbers[0], "probability": "Huge",
                              "impact": "High"}])
    ds.baselines = pd.DataFrame([{"project_number": ds.project_numbers[0], "version": 1},
                                 {"project_number": ds.project_numbers[0], "version": 1}])
    ds.team_skills = pd.DataFrame([{"assignment_group": "Team A", "skill": "SAP FI", "fte": 1}])
    ds.skill_demand = pd.DataFrame([{"project_number": ds.project_numbers[0], "assignment_group": "Team A",
                                     "skill": "Cobol", "month": "2029-06-01", "demand_hours": 10}])
    rules = sorted({i.rule for i in check_pmo_tables(ds)})
    assert rules == ["Baseline History", "Phase Model", "Risk Scale", "Skill Consistency"]
    assert [i.column for i in check_pmo_tables(ds) if i.rule == "Phase Model"] == ["phase"]


def test_every_stage_gate_criterion_of_the_model_has_its_check():
    from app.core.stage_gates import CRITERION_NAMES, DEFAULT_STAGE_GATES, validate_stage_gates
    from app.crew.tools.gate_checks import CRITERIA

    assert tuple(CRITERIA) == CRITERION_NAMES
    assert validate_stage_gates(DEFAULT_STAGE_GATES) == DEFAULT_STAGE_GATES
    with pytest.raises(ValueError, match="unknown criterion"):
        validate_stage_gates({"Planning -> Executing": [{"criterion": "magic", "kind": "mandatory"}]})
