"""Golden verdict set: every rule of the verdict engine and every guided scenario type, on synthetic data.

The cases live in tests/golden/verdict_cases.json, the portfolio in tests/golden/portfolio_golden.json (independent
of the demo data in data/). Each case runs through the real pipeline with an exact plan and a fixed reference date –
no parser, no LLM – so a changed rule or formula shows up here as a changed verdict.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_PMO
from app.crew import pipeline
from app.crew.scenarios import SCENARIOS, build_guided_scenario
from app.crew.tasks import IntentPlan, SubIntent
from app.crew.tools import graph_tools
from app.crew.tools import simulator_tools as sim
from app.crew.verdict import compute_verdict
from app.data_adapters.dataset import PortfolioDataset
from app.graph.portfolio_graph import build_graph

GOLDEN = Path(__file__).parent / "golden"
CASES: list[dict[str, Any]] = json.loads((GOLDEN / "verdict_cases.json").read_text(encoding="utf-8"))["cases"]
BY_ID = {c["id"]: c for c in CASES}

# Verdict label -> rule ID (one rule decides the verdict)
_LABEL_RULES = {
    "": "none",
    "budget does not cover the forecast": "budget_overrun_nogo",
    "guardrail violated": "guardrail",
    "data quality – quality gate": "quality_gate",
    "internal error": "internal_error",
    "input not understood": "input_error",
    "analysis": "analysis",
    "gate criteria not met": "gate_mandatory",
    "business case not realised": "benefit_case_failed",
}
# CONDITIONAL GO reason text -> rule ID (several rules can hit at once)
_CONDITION_RULES = {
    "Forced simulation for": "forced",
    "forecast exceeds the new budget by": "budget_overrun",
    "dependent project(s) affected": "domino",
    "outside your scope are affected": "domino",
    "misses required-by dates of dependent projects outside": "deadline_breach",
    "Stopping ": "cancel_review",
    "master data incomplete": "indicative",
    "Budget transfer of": "board_approval",
    "no estimate at completion": "eac_missing",
    "ROI turns negative": "negative_roi",
    "team utilisation after the transfer": "overload",
    "misses the required-by date": "deadline_breach",
    "already waits beyond": "deadline_breach",
    "is paused for": "on_hold",
    "NPV turns negative": "npv_negative",
    "Savings target not reached": "portfolio_cut_missed",
    "Portfolio cut stops": "portfolio_cut",
    "board approval threshold": "board_approval",
    "Graph database unavailable": "graph_unavailable",
    "qualitative AI estimate": "ai_estimate",
    "heuristic placeholder": "ai_estimate",
    "Tool warning:": "tool_warning",
    "' utilisation after the transfer": "overload",
    "restarts with": "resume_team",
    "Portfolio stress:": "cost_stress",
    "re-baseline deviates": "rebaseline_board",
    "within 12 months (limit": "rebaseline_count",
    "advisory gate criterion not met": "gate_advisory",
    "below its floor": "reserve_floor",
    "removes only": "risk_low_benefit",
    "is accepted.": "risk_accept_high",
    "benefit realisation": "benefit_shortfall",
    "without any actual": "benefit_unreported",
    "is merged into": "merge",
    "spans two business units": "merge_cross_unit",
}
ALL_RULES = set(_LABEL_RULES.values()) | set(_CONDITION_RULES.values())


def rules_of(verdict) -> set[str]:
    if verdict.rule or verdict.decision == "GO":
        return {_LABEL_RULES[verdict.rule]}
    return {rule for marker, rule in _CONDITION_RULES.items() if any(marker in r for r in verdict.reasons)}


@pytest.fixture(scope="module")
def golden_dataset() -> PortfolioDataset:
    raw = json.loads((GOLDEN / "portfolio_golden.json").read_text(encoding="utf-8"))
    meta = raw.pop("dataset_metadata", {})
    return PortfolioDataset.from_tables({name: pd.DataFrame(rows) for name, rows in raw.items()}, meta)


@pytest.fixture(scope="module")
def golden_graph(golden_dataset):
    return build_graph(golden_dataset)


@pytest.fixture
def run_case(golden_dataset, golden_graph, monkeypatch):
    monkeypatch.setattr(graph_tools, "get_graph", lambda: golden_graph)
    names = dict(zip(golden_dataset.projects["project_number"], golden_dataset.projects["project_name"], strict=True))

    def run(case: dict[str, Any]):
        if case.get("rules"):
            update_runtime_config(business_rules=case["rules"])
        if case.get("inject") == "graph_down":
            def down(*_args, **_kwargs):
                raise graph_tools.GraphUnavailable("Graph not built: offline")
            monkeypatch.setattr(graph_tools, "get_graph", down)
        elif case.get("inject") == "internal_error":
            def broken(*_args, **_kwargs):
                raise RuntimeError("injected defect")
            monkeypatch.setattr(sim, "_budget_delta", broken)
        if case.get("scenario"):
            s = case["scenario"]
            guided = build_guided_scenario(s["action"], s["project"], names, s["target"], **s["params"])
            request, plan = guided.request, guided.plan
        else:
            intents = [SubIntent(description=case["id"], **i) for i in case["plan"]]
            request = case["id"]
            plan = IntentPlan(summary=request, sub_intents=intents,
                              requires_simulation=any(i.intent_type == "simulation" for i in intents),
                              requires_graph_analysis=any(i.intent_type == "graph_analysis" for i in intents))
        return pipeline.run_steering_pipeline(request, golden_dataset, ROLE_PMO, "PMO", plan=plan,
                                              force=bool(case.get("force")), as_of=date.fromisoformat(case["as_of"]))

    return run


# =========================================================================== the golden cases
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case, run_case):
    verdict = run_case(case).verdict
    assert verdict.decision == case["expected_verdict"], verdict.to_markdown()
    assert case["expected_rule"] in rules_of(verdict), verdict.to_markdown()
    assert any(case["reason_contains"] in r for r in verdict.reasons), verdict.to_markdown()
    assert len(verdict.conditions) == case["expected_conditions"], verdict.to_markdown()
    assert verdict.as_of == case["as_of"]


@pytest.mark.parametrize("case", [c for c in CASES if "expected_facts" in c], ids=lambda c: c["id"])
def test_golden_facts_match_the_hand_calculation(case, run_case):
    """The oracle: EAC, new budget, overrun and ROI calculated by hand (formulas in verdict_cases.json) – not taken
    from an earlier run – and the exact conditions the verdict sets."""
    result = run_case(case)
    checked = {"budget_change", "budget_overrun", "negative_roi", "eac_missing"}
    facts = [{k: v for k, v in f.items() if k != "carried_over"} for f in result.scenario_state["facts"]
             if f["type"] in checked]
    assert facts == case["expected_facts"]
    assert list(result.verdict.conditions) == case["expected_condition_texts"]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("expected_changes")], ids=lambda c: c["id"])
def test_golden_changes(case, run_case):
    changes = [(c["field"], c["old"], c["new"]) for c in run_case(case).proposed_changes]
    for expected in case["expected_changes"]:
        assert (expected["field"], expected["old"], expected["new"]) in changes, changes


def test_same_scenario_flips_with_the_threshold(run_case):
    """GC-36 is GC-10 with the NO-GO threshold raised from 15 % to 30 %: only the rule may change the verdict."""
    tight, loose = BY_ID["GC-10"], BY_ID["GC-36"]
    assert loose["same_as"] == "GC-10" and loose["scenario"] == tight["scenario"]
    assert run_case(tight).verdict.decision == "NO-GO"
    assert run_case(loose).verdict.decision == "CONDITIONAL GO"


# =========================================================================== coverage of the set itself
def test_every_rule_is_covered():
    assert {c["expected_rule"] for c in CASES} == ALL_RULES


def test_every_scenario_type_is_covered():
    actions = {c["scenario"]["action"] for c in CASES if c.get("scenario")}
    assert actions == set(SCENARIOS)
    # a hold, a portfolio cut and a merge always carry a condition (restart date / sponsor sign-off / re-baseline)
    # – never GO by design
    go = {c["scenario"]["action"] for c in CASES if c.get("scenario") and c["expected_verdict"] == "GO"}
    assert go == set(SCENARIOS) - {"on_hold", "portfolio_cut", "merge"}


def test_case_ids_are_unique():
    assert len(BY_ID) == len(CASES)


# =========================================================================== branches no scenario reaches
class TestEngineBranches:
    """Fact combinations the tools never produce on their own, so every branch of compute_verdict is pinned."""

    def test_mixed_guardrail_and_quality_gate_is_a_guardrail_violation(self):
        v = compute_verdict(["ERROR: Data quality – P-1 blocked", "ERROR: Budget change exceeds the limit"], [], [])
        assert v.label == "NO-GO"

    def test_domino_below_the_threshold_is_no_condition(self):
        v = compute_verdict(["ok"], [], [{"type": "domino", "project": "P-1", "affected": 1, "direct": 1}])
        assert v.decision == "GO"

    def test_domino_of_a_pure_dependency_question_needs_no_decision(self):
        v = compute_verdict([], ["graph"], [{"type": "domino", "project": "P-1", "affected": 5, "direct": 5}])
        assert v.decision == "NO DECISION REQUIRED"

    def test_successor_already_late_is_escalated(self):
        fact = {"type": "deadline_breach", "project": "P-1", "successor": "P-2", "required_by": "2030-01-31",
                "dependency_type": "Hard", "new": False, "days_late_before": 10, "days_late": 40}
        v = compute_verdict(["ok"], [], [fact])
        assert "already waits beyond 2030-01-31; lateness grows from 10 to 40 days" in v.reasons[0]
        assert v.conditions == ("Escalate the already late Hard dependency of P-2.",)

    def test_unbounded_overload(self):
        v = compute_verdict(["ok"], [], [{"type": "overload", "project": "P-1", "utilisation_pct": None}])
        assert "team utilisation after the transfer unbounded" in v.reasons[0]

    def test_board_threshold_zero_disables_the_rule(self):
        from app.core.business_rules import BusinessRules

        fact = {"type": "budget_change", "project": "P-1", "delta": 10_000_000}
        assert compute_verdict(["ok"], [], [fact], rules=BusinessRules(board_approval_threshold_eur=0)).decision == "GO"

    def test_warning_fact_without_text_is_ignored(self):
        assert compute_verdict(["ok"], [], [{"type": "ramp_up", "project": "P-1", "warning": ""}]).decision == "GO"

    def test_unknown_fact_is_ignored(self):
        assert compute_verdict(["ok"], [], [{"type": "npv_change", "project": "P-1"}]).decision == "GO"

    def test_duplicate_reasons_are_collapsed(self):
        f = {"type": "negative_roi", "project": "P-1", "new_roi": -5}
        v = compute_verdict(["ok"], [], [f, dict(f)])
        assert len(v.reasons) == 1 and len(v.conditions) == 1

    def test_as_of_is_optional(self):
        assert compute_verdict(["ok"], [], []).as_of is None
