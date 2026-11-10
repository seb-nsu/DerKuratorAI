"""Quick calculation in the question boxes: a change or planning question is calculated by the rule-based pipeline
right where it is asked – the same verdict as the Steering Console's offline path, no LLM step – and says where the
next step happens; a plain question is left to the assistant."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset
from app.crew import pipeline, quick
from app.data_adapters import JsonPortfolioAdapter

SOURCE = Path(__file__).resolve().parents[1] / "data" / "DerKurator_Demo_Portfolio.json"
ERP = "PRJ-DEMO-1002"


@pytest.fixture(scope="module")
def full():
    return JsonPortfolioAdapter(SOURCE).load_dataset()


@pytest.fixture
def pmo(full):
    return apply_rls_to_dataset(full, ROLE_PMO, "PMO-ADMIN")


@pytest.mark.parametrize("question, project, target", [
    ("What is the budget?", ERP, None),
    ("Which projects are at risk?", None, None),
    ("What if the budget is cut by 20 %?", ERP, quick.CONSOLE),
    ("What if it is delayed by 3 months?", ERP, quick.CONSOLE),
    ("Save €2M across the portfolio", None, quick.CONSOLE),
    ("What drops out in 2027 at -10 %?", None, quick.PLANNING),
])
def test_target(pmo, question, project, target):
    assert quick.target_of(question, pmo, project) == target


def test_a_change_gets_the_consoles_offline_verdict_without_any_llm(full, pmo, monkeypatch):
    monkeypatch.setattr(type(get_runtime_config()), "llm_available", property(lambda self: True))
    monkeypatch.setattr(pipeline, "_run_agents", lambda *a, **k: pytest.fail("no LLM step in a quick calculation"))
    result = quick.calculate("What if the budget is cut by 20 %?", pmo, ROLE_PMO, "PMO-ADMIN", pii_source=full,
                             project_id=ERP)
    assert result.request.endswith(f"({ERP})") and result.mode == "offline" and result.target == quick.CONSOLE
    assert result.verdict.decision == "NO-GO" and result.changes and not result.warnings


def test_the_page_project_is_not_added_when_the_question_names_one(pmo):
    assert quick.with_project("Cut the budget of PRJ-DEMO-1001 by 5 %", pmo, ERP).endswith("by 5 %")


def test_a_planning_question_gives_the_scenario_input(full, pmo):
    result = quick.calculate("What drops out in 2027 at -10 %?", pmo, ROLE_PMO, "PMO-ADMIN", pii_source=full)
    assert result.target == quick.PLANNING and result.verdict is not None
    assert result.planning_input.budget_factor_pct == -10 and result.planning_input.year_from == 2027


def test_a_pm_gets_no_planning_scenario(full):
    view = apply_rls_to_dataset(full, ROLE_PM, "Priya Raman")
    result = quick.calculate("What drops out in 2027 at -10 %?", view, ROLE_PM, "Priya Raman", pii_source=full)
    assert result.verdict is None and result.planning_input is None and "PMO" in result.paper


def test_a_plain_question_is_not_calculated(full, pmo):
    assert quick.calculate("Who owns it?", pmo, ROLE_PMO, "PMO-ADMIN", pii_source=full, project_id=ERP) is None
