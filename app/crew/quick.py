"""Quick calculation for the question boxes: a question that asks for a change or a planning scenario is calculated
right where it is asked – with the same rule-based pipeline as the Steering Console, but without LLM steps (same
verdict, under a second, no cost). The result says where the next step happens: the Steering Console for the full
decision paper and a proposal, the planning scenarios for a plan draft."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.crew.parser import find_projects_in_text, heuristic_plan, is_change_request
from app.data_adapters.dataset import PortfolioDataset

CONSOLE, PLANNING = "console", "planning"
# simulation intents that only ask back – they calculate nothing yet
_NOT_CALCULABLE = ("ask_quantity", "unstructured")


@dataclass
class QuickResult:
    target: str  # CONSOLE | PLANNING: where the next step happens
    request: str  # the question as calculated (with the page's project when it named none)
    mode: str  # "offline" | "planning" | "blocked"
    verdict: Any = None  # Verdict, or None when nothing was decided (blocked, role without planning)
    changes: list[dict[str, Any]] = field(default_factory=list)
    paper: str = ""
    warnings: list[str] = field(default_factory=list)
    planning_input: Any = None  # ScenarioInput for pre-filling the planning scenarios


def with_project(question: str, dataset: PortfolioDataset, project_id: str | None) -> str:
    """A question on a project page that names no project is about that project."""
    if project_id and not find_projects_in_text(question, dataset.projects):
        return f"{question.strip()} ({project_id})"
    return question.strip()


def target_of(question: str, dataset: PortfolioDataset, project_id: str | None = None,
              as_of: date | None = None) -> str | None:
    """CONSOLE or PLANNING when the question asks for something the rules calculate, else None (a plain question)."""
    from app.planning import chat as planning_chat

    request = with_project(question, dataset, project_id)
    found = find_projects_in_text(request, dataset.projects)
    if planning_chat.is_planning_question(request, dataset, found, as_of):
        return PLANNING
    if is_change_request(request):
        return CONSOLE
    plan = heuristic_plan(request, dataset.projects)
    if any(i.intent_type == "simulation" and not i.description.startswith(_NOT_CALCULABLE) for i in plan.sub_intents):
        return CONSOLE
    return None


def calculate(question: str, dataset: PortfolioDataset, role: str, user_id: str | None, *,
              pii_source: PortfolioDataset | None = None, project_id: str | None = None,
              as_of: date | None = None) -> QuickResult | None:
    """Runs a calculable question through the rule-based pipeline; None for a plain question. `dataset` must be the
    reader's RLS-filtered view, as in the Steering Console."""
    from app.crew import pipeline
    from app.planning import chat as planning_chat

    target = target_of(question, dataset, project_id, as_of)
    if target is None:
        return None
    request = with_project(question, dataset, project_id)
    result = pipeline.run_steering_pipeline(request, dataset, role, user_id, pii_source=pii_source, as_of=as_of,
                                            use_llm=False)
    planning_input = None
    if result.mode == "planning" and role in planning_chat.PLANNING_ROLES:
        planning_input = planning_chat.parse_question(request, dataset, as_of)
    return QuickResult(target if result.mode != "planning" else PLANNING, request, result.mode, result.verdict,
                       list(result.proposed_changes), result.decision_paper,
                       [w for w in result.warnings if not w.startswith("Offline mode")], planning_input)
