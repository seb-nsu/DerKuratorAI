"""The page assistant without its box: the quick calculation of a change or planning question, the next step it
points to, the hand-over hint for a change the rules cannot read, and the audit record of a question (never its
text). The facts per page are built in assist_facts.py."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core import logs
from app.core.security import ROLE_ADMIN, ROLE_BOARD, apply_rls_to_dataset
from app.crew.parser import heuristic_plan, is_change_request
from app.crew.quick import CONSOLE, QuickResult, calculate
from app.data_adapters import PortfolioDataset

logger = logging.getLogger("derkurator.views.assist")
HISTORY = 3  # answers kept per box
EMPTY_HINT = ("Answers come only from what this page shows you. A change or planning question (e.g. “cut the budget by "
              "10 %”) is calculated right here by the rules; the result shows where to continue.")
OFFLINE_HINT = " · No AI available right now: other questions get a rule-based digest of the facts."
AI_NOTE = "AI-generated · answered only from the facts of this page – verdicts stay rule-based"
RULES_NOTE = "Rule-based digest (no AI available)"
CONSOLE_STEP = ("Next step: the Steering Console writes the full decision paper; from there you submit the change for "
                "approval or apply it.")
NO_CONSOLE_STEP = "Change requests are calculated by the PMO in the Steering Console."
PLANNING_STEP = ("Next step: Planning › Plan scenarios – the inputs are filled in; calculate, adjust and save it as a "
                 "draft, then submit it to the Portfolio Board under Saved plan scenarios.")
HANDOFF_HINT = "The rules could not read this change on their own – try it in the Steering Console."


def audit_question(page: str, mode: str, model: str, chars: int, user_id: str | None) -> None:
    """Audits a question by page, mode and length – never its text."""
    try:
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "assistant_question", "page": page, "mode": mode, "model": model,
                                           "chars": chars, "user": user_id})
    except Exception as exc:  # noqa: BLE001 - a question changes nothing; it is never blocked by the trail
        logger.warning("Assistant question not audited (%s)", exc.__class__.__name__)


def quick(question: str, full: PortfolioDataset, role: str | None, user_id: str | None,
          project_id: str | None = None) -> QuickResult | None:
    """The reader's own view, as in the Steering Console; None for a plain question or a reader without data."""
    if not role or role == ROLE_ADMIN:
        return None
    return calculate(question, apply_rls_to_dataset(full, role, user_id), role, user_id, pii_source=full,
                     project_id=project_id)


def has_console(role: str | None) -> bool:
    """The Portfolio Board works on the Planning page only – it has no Steering Console to continue in."""
    return role != ROLE_BOARD


def needs_console(question: str, projects: Callable[[], Any]) -> bool:
    """A change the rules cannot read on their own (no amount, no clear action): the Steering Console's agents can.
    `projects` returns the project table (loaded only when needed)."""
    if is_change_request(question):
        return True
    try:
        plan = heuristic_plan(question, projects())
    except Exception:  # noqa: BLE001 - only decides whether a hand-over is offered
        return False
    return any(i.intent_type == "simulation" and i.description.startswith("unstructured") for i in plan.sub_intents)


def planning_prefill(spec) -> dict[str, Any]:
    """The planning scenario inputs of a calculated question, in the steps the scenario form offers (budget factor in
    5 % steps within ±30 %; moves split into later / earlier)."""
    moves = spec.moves or {}
    return {"name": spec.name, "year_from": spec.year_from, "year_to": spec.year_to,
            "budget_factor_pct": int(max(-30, min(30, 5 * round(spec.budget_factor_pct / 5)))),
            "scope": spec.scope or "", "method": spec.method, "forced_in": list(spec.forced_in or {}),
            "forced_reason": next(iter((spec.forced_in or {}).values()), ""), "excluded": list(spec.excluded or {}),
            "later": [d for d, m in moves.items() if m > 0], "earlier": [d for d, m in moves.items() if m < 0]}


def next_step(result: QuickResult, role: str | None) -> dict[str, Any] | None:
    """Where a calculated question continues: the Steering Console (if the role has one) or the planning scenarios."""
    if result.target == CONSOLE and result.mode != "blocked":
        if not has_console(role):
            return {"target": None, "text": NO_CONSOLE_STEP}
        return {"target": CONSOLE, "text": CONSOLE_STEP, "request": result.request}
    if result.planning_input is not None:
        return {"target": "planning", "text": PLANNING_STEP, "inputs": planning_prefill(result.planning_input)}
    return None


# page -> (box title, subject for the model, example questions); "{id}" is the page's demand
PAGES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "overview": ("Portfolio briefing and questions", "the project portfolio in the reader's scope",
                 ("Brief me on the portfolio in five bullets", "Which projects need attention first?")),
    "data_quality": ("Ask about the data quality", "the data quality findings of the Data Steward",
                     ("Which fixes unblock the most projects?", "What does each rule check?")),
    "report": ("Ask about this report", "a steering report (a simulated change request, its rule-based verdict and "
               "decision paper)", ("Explain the verdict in two sentences", "What must happen to meet the conditions?")),
    "plan": ("Ask about the plan", "the annual plan as it stands (rule-based base plan)",
             ("Where is the plan tightest?", "Which delay would hurt most?", "What drops out in the first year and why?")),
    "demand": ("Ask about this demand", "demand {id} in the annual planning",
               ("What does this demand wait for?", "What happens if it starts a quarter later?")),
    "demands": ("Ask about the demands", "the demands of the annual planning in the reader's scope",
                ("Which demands are not assessed yet?", "Which demands need money in the first year?")),
    "scenario": ("Ask about this plan scenario", "a planning scenario (rule-based annual plan) and its comparison",
                 ("Why does the plan get this verdict?", "What drops out and why?")),
    "project": ("Ask about this project", "project {id}",
                ("Why is this project at risk?", "What is due in the next 90 days?",
                 "What if the budget is cut by 10 %?")),
    "compare": ("Ask about A vs B", "the comparison of two saved what-ifs (A and B)",
                ("Where do A and B differ most?", "Which one has fewer conditions?")),
}
