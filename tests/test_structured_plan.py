"""The orchestrator's structured plan: typed action and values that the code runs deterministically – only with the
values the request states, sign and unit from the request – while the Simulator still assesses the result (same
agents). And the check of the Advisory's text: figures that no input contains and foreign verdict words are reported."""
from __future__ import annotations

import pytest

from app.crew import guard_rails
from app.crew.advisory_check import check_advisory, foreign_verdict_words, unknown_figures
from app.crew.tasks import SubIntent
from app.crew.verdict import Verdict
from tests.test_llm_guard_rails import ERP, full, llm, plan, portfolio  # noqa: F401 - fixtures used by name

TARGET = "PRJ-DEMO-1006"


def structured(action: str, *ids: str, **values) -> SubIntent:
    return SubIntent(intent_type="simulation", description=f"{action} of {', '.join(ids)}", project_ids=list(ids),
                     action=action, **values)


def params(intent: SubIntent, request: str):
    return guard_rails.structured_parameters(intent, list(intent.project_ids), request)


class TestStructuredParameters:
    def test_a_stated_percentage_cut(self):
        p = params(structured("budget_change", ERP, percent=10, cost_type="capex"), "Cut the CapEx of ERP by 10 %")
        assert p == {"action": "budget_change", "source": guard_rails.STRUCTURED, "delta": -10.0, "is_perc": True,
                     "cost_type": "capex"}

    def test_the_sign_comes_from_the_request_not_from_the_model(self):
        p = params(structured("budget_change", ERP, percent=-15), "Increase the budget of ERP by 15 %")
        assert p["delta"] == 15.0
        p = params(structured("budget_change", ERP, amount_eur=370000), "Take €370k out of the ledger project")
        assert p["delta"] == -370000.0 and p["is_perc"] is False

    @pytest.mark.parametrize("intent, request_text", [
        (structured("budget_change", ERP, amount_eur=185000), "Cut ERP by 10 %"),  # computed, not stated
        (structured("budget_change", ERP, percent=20), "Trim ERP's budget by a fifth"),  # no number in the request
        (structured("budget_change", ERP, percent=10), "Change the ERP budget by 10 %"),  # no direction
        (structured("budget_change", ERP, percent=10, amount_eur=5000), "Cut ERP by 10 %"),  # two values
        (structured("budget_change", ERP, amount_eur=10), "Cut ERP by 10 %"),  # a percentage read as an amount
        (structured("budget_transfer", ERP, amount_eur=1000), "Move €1,000 from ERP"),  # no target
        (structured("schedule", ERP, months=3), "Delay ERP by 3 months and cut capacity by 10 %"),  # stays agentic
        (structured("schedule", ERP, months=4), "Delay ERP by 3 months"),
        (structured("cancel", ERP), "What does ERP depend on?"),
        (structured("", ERP), "Cut ERP by 10 %"),
    ])
    def test_anything_unsafe_goes_to_the_simulator(self, intent, request_text):
        assert params(intent, request_text) is None

    def test_schedule_hold_transfer_and_stop(self):
        assert params(structured("schedule", ERP, months=3), "Delay ERP by 3 months")["months_delta"] == 3
        assert params(structured("schedule", ERP, months=2), "Accelerate ERP by 2 months")["months_delta"] == -2
        assert params(structured("on_hold", ERP, months=6), "Put ERP on hold for 6 months")["months"] == 6
        moved = params(structured("budget_transfer", ERP, TARGET, amount_eur=200000),
                       "Move €200k from ERP to the billing project")
        assert moved["amount"] == 200000 and moved["cost_type"] == "total"
        assert params(structured("cancel", ERP), "Stop ERP")["action"] == "cancel"


class TestPipeline:
    def test_a_structured_plan_runs_in_code_and_the_simulator_assesses_it(self, llm):  # noqa: F811
        # not certain for the parser (a German phrasing), complete in the structured plan
        result, sims = llm("Kannst du beim Ledger-Projekt 20 % vom Budget streichen?",
                           plan(structured("budget_change", ERP, percent=20)))
        assert sims == 0 and result.verdict.decision == "NO-GO"
        assert {c["project_id"] for c in result.proposed_changes} == {ERP}
        assert "Simulation run deterministically from the AI's structured plan" in result.decision_paper

    def test_an_incomplete_structured_plan_keeps_the_simulator_path(self, llm):  # noqa: F811
        from app.crew.tools import simulator_tools

        result, sims = llm("Trim General Ledger Consolidation's budget by a fifth",
                           plan(structured("budget_change", ERP, percent=20)),
                           simulator=lambda: simulator_tools.budget_change(ERP, -20, True))
        assert sims == 1 and result.verdict.decision == "NO-GO"


# --------------------------------------------------------------------------- advisory check
SOURCES = "| ERP | €1,850,000 | 12.5 % |\nsimulate_budget_change: budget 1,850,000 -> 1,480,000 (-20 %)"


class TestAdvisoryCheck:
    def test_figures_from_the_inputs_pass_in_any_shown_precision(self):
        text = "- The budget falls from €1.85 M to €1,480,000 (−20 %), ROI 12.5 %."
        assert unknown_figures(text, SOURCES) == []

    def test_a_figure_no_input_contains_is_reported(self):
        assert unknown_figures("Savings of €2.3 M and a 7 % risk buffer.", SOURCES) == ["€2.3 M", "7 %"]

    def test_verdict_words_outside_the_recommendation_line(self):
        verdict = Verdict("NO-GO", "NO-GO (guardrail violated)")
        text = "**Recommendation:** NO-GO\n## 5. Recommendation\nWith a smaller cut this would be a GO."
        assert foreign_verdict_words(text, verdict) == ["GO"]
        assert foreign_verdict_words("**Recommendation:** NO-GO\nThe NO-GO stands.", verdict) == []

    def test_findings_read_as_hints_for_the_reader(self):
        verdict = Verdict("CONDITIONAL GO", "CONDITIONAL GO")
        notes = check_advisory("Savings of €2.3 M.\nOtherwise NO-GO.", SOURCES, verdict)
        assert len(notes) == 2 and "€2.3 M" in notes[0] and "NO-GO" in notes[1]
        assert check_advisory("Budget €1.85 M.", SOURCES, verdict) == []

    def test_the_crew_run_reports_findings_as_warnings(self, llm):  # noqa: F811
        result, _ = llm("Cut the budget of General Ledger Consolidation by 20 %",
                        plan(structured("budget_change", ERP, percent=20)))
        # the fake Advisory writes only the recommendation line – nothing to report
        assert not [w for w in result.warnings if w.startswith("Advisory check")]
