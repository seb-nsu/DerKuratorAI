"""Page assistant: answers only from the facts the code collects, masked before they leave, a rule-based digest without
a model, the reader's scope respected; plus the planning analysis it reads (chain, delay, envelopes, hot spots)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core import logs
from app.core.security import register_pii_names
from app.crew import ask as ask_mod
from app.data_adapters import JsonPortfolioAdapter
from app.planning import analysis
from app.planning.scenario import ScenarioInput, run_scenario
from app.ui import assist_facts
from tests.llm_fakes import FakeClient, install
from tests.test_admin_console import _app, app_sandbox  # noqa: F401 - the fixture is used by name

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)
FACTS = "## Project P-1\n- state: Work in Progress\n- owner: Robin Example\n- budget_cost_eur: 100,000\n- risk: supplier late"


@pytest.fixture
def llm(monkeypatch):
    fake = install(monkeypatch, FakeClient(["The budget is 100,000 according to the facts."]))
    monkeypatch.setattr(ask_mod, "llm_ready", lambda: (True, "test-model"))
    ask_mod.clear_cache()
    yield fake
    ask_mod.clear_cache()


class TestAsk:
    def test_without_a_model_the_answer_is_the_matching_facts(self, monkeypatch):
        monkeypatch.setattr(ask_mod, "llm_ready", lambda: (False, "test-model"))
        answer = ask_mod.ask("What is the budget?", FACTS, "project P-1")
        assert answer.mode == "rules" and "budget_cost_eur: 100,000" in answer.text
        assert "supplier" not in answer.text  # only the lines that share a word with the question

    def test_the_model_gets_masked_facts_and_rules_and_the_answer_is_labelled(self, llm):
        register_pii_names(["Robin Example"], "[PM_MASKED]")
        answer = ask_mod.ask("Who owns it and what is the budget?", FACTS, "project P-1")
        system, human = llm.calls[0]["system"], llm.prompts[0]
        assert "Robin Example" not in human and "[PM_MASKED]" in human
        assert "ONLY the facts" in system and "GO / NO-GO" in system
        assert answer.mode == "llm" and answer.text.startswith(ask_mod.AI_LABEL) and answer.facts == FACTS

    def test_the_same_question_is_answered_from_the_cache(self, llm):
        ask_mod.ask("What is the budget?", FACTS, "project P-1")
        ask_mod.ask("What is the budget?", FACTS, "project P-1")
        assert len(llm.calls) == 1

    def test_a_failing_model_falls_back_to_the_digest(self, llm):
        llm.fail = True
        answer = ask_mod.ask("What is the budget?", FACTS, "project P-1")
        assert answer.mode == "rules" and "100,000" in answer.text

    def test_long_facts_are_cut_and_marked(self, monkeypatch):
        monkeypatch.setattr(ask_mod, "llm_ready", lambda: (False, ""))
        answer = ask_mod.ask("anything", "x\n" * ask_mod.MAX_FACTS_CHARS, "s")
        assert answer.truncated and len(answer.facts) == ask_mod.MAX_FACTS_CHARS

    def test_offline_mode_never_calls_a_model(self, monkeypatch):
        monkeypatch.setenv("FORCE_OFFLINE_MODE", "true")
        from app.core.config import get_settings

        get_settings.cache_clear()
        try:
            assert ask_mod.llm_ready()[0] is False
        finally:
            get_settings.cache_clear()


def test_table_lists_rows_and_counts_the_rest():
    text = assist_facts.table(pd.DataFrame({"a": range(5), "b": ["x"] * 5}), "T", limit=2)
    assert text.splitlines() == ["## T", "- a: 0 · b: x", "- a: 1 · b: x", "- … 3 more row(s) not listed"]
    assert assist_facts.table(pd.DataFrame(), "Empty") == "## Empty\nnone"


# --------------------------------------------------------------------------- planning analysis
@pytest.fixture(scope="module")
def demo():
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return JsonPortfolioAdapter(SOURCE).load_dataset()


@pytest.fixture(scope="module")
def plan(demo):
    return run_scenario(demo, ScenarioInput(), AS_OF)


class TestPlanningAnalysis:
    def test_the_chain_names_successors_with_their_dates(self, demo, plan):
        chain = analysis.dependency_chain(demo, "DMD-2027-005", plan)
        row = chain[chain["Item"] == "DMD-2027-006"].iloc[0]
        assert row["Direction"] == "Needed by" and row["Steps"] == 1 and row["Start"].startswith("20")

    def test_a_delay_moves_the_successor_just_enough(self, demo, plan):
        impact = analysis.slip_impact(demo, "DMD-2027-005", 2, plan)
        row = impact[impact["Item"] == "DMD-2027-006"].iloc[0]
        assert row["Moves by (quarters)"] >= 1 and row["Still fits"] in ("yes", "no – drops out")
        assert analysis.slip_impact(demo, "DMD-2027-005", 0, plan).empty

    def test_a_chain_of_three_moves_down_the_line(self):
        from app.data_adapters.dataset import PortfolioDataset

        demands = pd.DataFrame([
            {"demand_number": "A", "demand_name": "A", "earliest_start_date": "2027-01-01", "duration_months": 3},
            {"demand_number": "B", "demand_name": "B", "earliest_start_date": "2027-04-01", "duration_months": 3,
             "depends_on": "A", "latest_start_date": "2027-06-30"},
            {"demand_number": "C", "demand_name": "C", "earliest_start_date": "2027-07-01", "duration_months": 3,
             "depends_on": "B"}])
        ds = PortfolioDataset(projects=pd.DataFrame(), demands=demands)
        impact = analysis.slip_impact(ds, "A", 1, None).set_index("Item")
        assert impact.at["B", "Start after the delay"] == "2027-Q3" and impact.at["B", "Still fits"] == "no – drops out"
        assert impact.at["C", "Start after the delay"] == "2027-Q4"

    def test_a_scoped_view_never_names_what_lies_outside(self, demo):
        from app.data_adapters.dataset import PortfolioDataset

        scoped = PortfolioDataset(projects=demo.projects.head(0),
                                  demands=demo.demands[demo.demands["demand_number"] == "DMD-2027-006"])
        assert analysis.dependency_chain(scoped, "DMD-2027-006", None).empty
        assert analysis.hidden_links(scoped, "DMD-2027-006")
        facts = assist_facts.demand_facts(scoped, "DMD-2027-006", None, full_view=False)
        assert "DMD-2027-005" not in facts and "outside the reader's scope" in facts
        assert "Budget envelopes" not in facts and "Decision in the base plan" not in facts

    def test_budget_and_capacity_position(self, demo, plan):
        budget = analysis.budget_position(demo, "DMD-2027-005", plan)
        assert set(budget["Scope type"]) == {"business unit", "portfolio"} and budget["This demand"].sum() > 0
        capacity = analysis.capacity_position(demo, "DMD-2027-005", plan)
        assert not capacity.empty and capacity["This demand FTE"].gt(0).all()

    def test_hot_spots_and_critical_chains(self, demo, plan):
        budget, _ = analysis.hot_spots(plan)
        assert (pd.to_numeric(budget["Used %"]) >= analysis.HOT_SHARE).all()
        chains = analysis.critical_chains(demo, plan)
        assert "DMD-2027-005" in set(chains["Demand"])

    def test_plan_facts_carry_the_rule_based_verdict(self, demo, plan):
        facts = assist_facts.plan_facts(demo, plan)
        assert plan.verdict.label in facts and "Decision per demand" in facts and "critical chains" in facts


# --------------------------------------------------------------------------- pages (AppTest, no model)
def _audit_events(tmp) -> list[dict]:
    path = Path(logs.AUDIT_LOG)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class TestPages:
    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        monkeypatch.setenv("FORCE_OFFLINE_MODE", "true")

    def test_planning_analysis_answers_and_audits_without_the_question(self, app_sandbox):  # noqa: F811
        at = _app("planning")
        assert "Analysis" in {t.label for t in at.tabs}
        at.button(key="ask_ex_plan_analysis_0").click().run()
        assert not at.exception, at.exception
        assert any("No AI answer available" in m.value for m in at.markdown)
        events = [e for e in _audit_events(app_sandbox) if e.get("event") == "assistant_question"]
        assert events and events[-1]["page"] == "plan_analysis" and events[-1]["mode"] == "rules"
        assert "tightest" not in json.dumps(events[-1])

    def test_pm_demand_detail_has_no_plan_tab(self, app_sandbox):  # noqa: F811
        at = _app("planning", login="Priya Raman", plan_detail="DMD-2027-001")
        labels = {t.label for t in at.tabs}
        assert "Dependencies" in labels and "In the plan" not in labels and "Analysis" not in labels

    def test_pmo_demand_detail_shows_the_plan_position(self, app_sandbox):  # noqa: F811
        at = _app("planning", plan_detail="DMD-2027-005")
        assert "In the plan" in {t.label for t in at.tabs}
        assert any("Base plan (rank order" in m.value for m in at.markdown)

    def test_a_change_question_is_calculated_in_the_box_and_points_to_the_console(self, app_sandbox):  # noqa: F811
        pid = "PRJ-DEMO-1002"
        at = _app("projects", detail_pid=pid)
        at.text_input(key=f"ask_q_project_{pid}").set_value("What if the budget is cut by 20 %?")
        at.button(key=f"FormSubmitter:ask_form_project_{pid}-Ask").click().run()
        assert not at.exception, at.exception
        assert any("Recommendation: NO-GO" in e.value for e in at.error)
        assert any(b.key == f"ask_console_project_{pid}_0" for b in at.button)
        events = [e for e in _audit_events(app_sandbox) if e.get("event") == "assistant_question"]
        assert events[-1]["mode"] == "calculation"
        at.button(key=f"ask_console_project_{pid}_0").click().run()
        assert not at.exception, at.exception
        assert "cut by 20 %? (PRJ-DEMO-1002)" in at.session_state["messages"][0]["content"]

    def test_a_planning_question_is_calculated_and_pre_fills_the_scenarios(self, app_sandbox):  # noqa: F811
        at = _app("overview")
        at.text_input(key="ask_q_overview").set_value("What drops out in 2027 at -10 %?")
        at.button(key="FormSubmitter:ask_form_overview-Ask").click().run()
        assert not at.exception, at.exception
        assert any("Recommendation:" in e.value for e in [*at.error, *at.warning, *at.success])
        at.button(key="ask_plan_overview_0").click().run()
        assert not at.exception, at.exception
        assert at.slider(key="scn_factor").value == -10 and at.number_input(key="scn_from").value == 2027

    def test_a_plain_question_is_answered_from_the_facts(self, app_sandbox):  # noqa: F811
        pid = "PRJ-DEMO-1002"
        at = _app("projects", detail_pid=pid)
        at.button(key=f"ask_ex_project_{pid}_0").click().run()
        assert not at.exception, at.exception
        assert not any(b.key.startswith(("ask_console_", "ask_handoff_")) for b in at.button)

    def test_overview_briefing(self, app_sandbox):  # noqa: F811
        at = _app("overview")
        at.button(key="ask_ex_overview_0").click().run()
        assert not at.exception, at.exception
        assert any("Portfolio in your scope" in m.value for m in [*at.markdown, *at.code])
