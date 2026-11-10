"""The page assistant (the question box under a page). A change or planning question is calculated by the rule-based
pipeline (verdict and changes, no LLM) and names the next step; every other question is answered from the facts of
the page, which the server builds from the caller's scope – never from text the client sends. The LLM only explains;
verdicts stay rule-based. A question is audited without its text."""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api import limits, runs
from app.api.deps import Scope, data_user, project_view, scope
from app.api.routes.planning import ScenarioBody, _scenario_input
from app.api.routes.proposals import _visible as proposal_visible
from app.api.routes.steering import chat_run
from app.api.routes.whatifs import _find as find_what_if
from app.core.auth import User
from app.core.proposals import get_proposal
from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_BOARD, ROLE_PM, ROLE_PMO
from app.crew.ask import MAX_QUESTION_CHARS, ask, llm_ready
from app.crew.verdict import lead_reason
from app.data_adapters.dataset import DEMAND_KEY
from app.views import assist, assist_facts
from app.views.exports import change_rows, change_table, project_names, proposal_message, run_message
from app.views.overview import team_overload
from app.views.steerco_pack import pack_data

router = APIRouter(tags=["assistant"])
logger = logging.getLogger("derkurator.api.assist")
Page = Literal["overview", "data_quality", "report", "plan", "demand", "project", "compare", "demands", "scenario"]
WORKSPACE = (ROLE_PMO, ROLE_PM)  # overview, data quality, steering reports, project detail, what-if comparison
PLANNERS = (ROLE_PMO, ROLE_BOARD)  # the base plan and a demand's place in it
MAX_SCENARIOS = 3  # calculated plan scenarios side by side, as on the page


class AssistQuestion(BaseModel):
    page: Page
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    run_id: str | None = Field(None, max_length=64)  # page "report": a what-if of the caller
    proposal_id: str | None = Field(None, max_length=64)  # page "report": a proposal the caller may see
    demand_id: str | None = Field(None, max_length=64)  # page "demand"
    project_id: str | None = Field(None, max_length=64)  # page "project"; a change question naming none is about it
    a: str | None = Field(None, max_length=32)  # page "compare": saved what-if A
    b: str | None = Field(None, max_length=32)  # page "compare": saved what-if B
    business_unit: list[str] = Field(default_factory=list, max_length=50)  # page "demands": the table's filters
    status: list[str] = Field(default_factory=list, max_length=50)
    year: str | None = Field(None, max_length=8)
    scenarios: list[ScenarioBody] = Field(default_factory=list, max_length=MAX_SCENARIOS)  # page "scenario": the
    # calculated scenarios of the page (client state); recalculated here on today's data
    scenario: str | None = Field(None, max_length=120)  # page "scenario": the name of the scenario shown


class AssistPage(BaseModel):
    page: Page
    title: str
    examples: list[str]


class AssistPages(BaseModel):
    ai_available: bool  # False: other questions get a rule-based digest of the facts
    hint: str
    pages: list[AssistPage]


class AssistCondition(BaseModel):
    condition: str
    owner_role: str
    due: str | None


class AssistVerdict(BaseModel):
    """The rule-based verdict of the calculation (never the AI's)."""
    decision: str
    label: str
    lead_reason: str  # the deciding rule and its first finding in one line (the callout's text)
    rule: str
    reasons: list[str]
    conditions: list[str]
    condition_details: list[AssistCondition]
    rules: dict[str, Any]  # the business-rule thresholds in force when the verdict was computed
    as_of: str | None
    due: str | None


class AssistChange(BaseModel):
    project_id: str
    table: str | None = None
    field: str
    old: Any = None
    new: Any = None
    note: str | None = None


class AssistChangeRow(BaseModel):
    """A proposed change in business columns."""
    project: str
    change: str
    current: str
    proposed: str
    delta: str
    approval: str
    note: str


class AssistPlanningInputs(BaseModel):
    """The planning scenario form, pre-filled with the calculated question."""
    name: str
    year_from: int | None
    year_to: int | None
    budget_factor_pct: int
    scope: str
    method: str
    forced_in: list[str]
    forced_reason: str
    excluded: list[str]
    later: list[str]
    earlier: list[str]


class AssistNextStep(BaseModel):
    target: Literal["console", "planning"] | None  # None: the role has no Steering Console (text says who calculates)
    text: str
    request: str | None = None  # target console: the request to run there (POST /steering/runs)
    inputs: AssistPlanningInputs | None = None  # target planning


class AssistCalculation(BaseModel):
    kind: Literal["calculation"] = "calculation"
    question: str
    request: str
    mode: str  # offline | planning | blocked
    verdict: AssistVerdict | None
    changes: list[AssistChange]
    change_rows: list[AssistChangeRow]
    paper: str  # the rule-based paper (Markdown)
    warnings: list[str]
    next_step: AssistNextStep | None


class AssistHandoff(BaseModel):
    text: str
    request: str  # run it in the Steering Console


class AssistAnswer(BaseModel):
    kind: Literal["answer"] = "answer"
    question: str
    text: str  # Markdown; an AI answer starts with the AI-generated label
    mode: Literal["llm", "rules"]
    ai_generated: bool
    note: str
    truncated: bool  # the facts were shortened
    facts: str  # the facts the answer comes from (Markdown, the caller's scope only)
    handoff: AssistHandoff | None


AskAnswer = Annotated[AssistCalculation | AssistAnswer, Field(discriminator="kind")]
_ROW_KEYS = {"Project": "project", "Change": "change", "Current": "current", "Proposed": "proposed", "Δ": "delta",
             "Approval": "approval", "Note": "note"}


def _pages_for(role: str) -> list[str]:
    pages = ["overview", "data_quality", "report", "project", "compare"] if role in WORKSPACE else []
    pages += ["plan"] if role in PLANNERS else []
    pages += ["scenario"] if role == ROLE_PMO else []
    return [*pages, "demands", "demand"]


@router.get("/assist/pages")
def pages(user: User = Depends(data_user)) -> AssistPages:
    """The question boxes of the caller's pages: title, example questions, and whether an AI answers right now."""
    ready, _ = llm_ready()
    return AssistPages(ai_available=ready, hint=assist.EMPTY_HINT + ("" if ready else assist.OFFLINE_HINT),
                       pages=[AssistPage(page=p, title=assist.PAGES[p][0], examples=list(assist.PAGES[p][2]))
                              for p in _pages_for(user.role)])


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


def _facts(body: AssistQuestion, ctx: Scope) -> tuple[str, str]:
    """(subject, facts) of the page, from the caller's scope only."""
    _, subject, _ = assist.PAGES[body.page]
    visible, user = ctx.visible, ctx.user
    if body.page == "overview":
        if visible.projects.empty:
            return subject, assist_facts.section("Portfolio in your scope", {"Projects": 0})
        view, steward = project_view(visible)
        return subject, assist_facts.overview_facts(view, visible, team_overload(visible), steward)
    if body.page == "data_quality":
        from app.crew.tools.data_steward_tools import run_all_checks

        return subject, assist_facts.data_quality_facts(run_all_checks(visible))
    if body.page == "report":
        if body.run_id:  # an analysis of the caller's history, or a run the server still holds
            _, message, _ = chat_run(user, body.run_id)
            if message is None:
                run = runs.get(body.run_id, user.user_id)
                if run is None:
                    raise _not_found("What-if")
                message = run_message(run)
            by = user.user_id
        else:
            proposal = get_proposal(body.proposal_id or "")
            if not proposal_visible(proposal, user):
                raise _not_found("Proposal")
            message, by = proposal_message(proposal), proposal.get("requester")
        data = pack_data(message, requester=by, environment=get_runtime_config().environment)
        changes = message["meta"].get("proposed_changes") or []
        return subject, assist_facts.report_facts(message, data, change_table(changes, visible) if changes else None)
    if body.page == "project":
        pid = body.project_id or ""
        if pid not in set(visible.project_numbers):
            raise _not_found("Project")
        view, steward = project_view(visible)
        project = view[view["project_number"].astype(str) == pid].iloc[0].to_dict()
        issues = [i for i in steward.issues if str(i.project_id) == pid]
        return subject.format(id=pid), assist_facts.project_facts(pid, project, visible, issues)
    if body.page == "compare":
        if not body.a or not body.b or body.a == body.b:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose two different what-ifs.")
        return subject, assist_facts.compare_facts(find_what_if(ctx, body.a), find_what_if(ctx, body.b))
    from app.planning import analysis

    if body.page == "demands":
        from app.views import planning as planning_views

        table = planning_views.demand_table(visible, user.role)
        shown = planning_views.filter_demands(table, body.business_unit, body.status, body.year) \
            if not table.empty else table
        return subject, assist_facts.demands_facts(visible, shown, planning_views.demand_issues(visible, user.role))
    if body.page == "scenario":
        from app.planning.scenario import run_scenario

        if not body.scenarios:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Calculate a plan scenario first.")
        results = [run_scenario(visible, _scenario_input(ctx, s)) for s in body.scenarios]
        shown = next((r for r in results if r.input.name == body.scenario), results[-1])
        return subject, assist_facts.scenario_facts(visible, shown, results)
    if body.page == "plan":
        return subject, assist_facts.plan_facts(visible, analysis.base_plan(visible))
    demand = body.demand_id or ""
    if visible.demands.empty or demand not in set(visible.demands[DEMAND_KEY].astype(str)):
        raise _not_found("Demand")
    full = user.role in PLANNERS
    plan = analysis.base_plan(visible) if full else None
    return subject.format(id=demand), assist_facts.demand_facts(visible, demand, plan, full)


@router.post("/assist/ask")
def ask_question(body: AssistQuestion, ctx: Scope = Depends(scope)) -> AskAnswer:
    user = ctx.user
    if body.page not in _pages_for(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This page is not part of your workspace.")
    if body.page == "report" and not (body.run_id or body.proposal_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Name the report: run_id or proposal_id.")
    question = body.question.strip()
    if not question:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Please enter a question.")
    with limits.heavy(user.user_id):  # a quick calculation or a plan scenario behind the answer
        return _answer(body, ctx, question)


def _answer(body: AssistQuestion, ctx: Scope, question: str) -> AskAnswer:
    user = ctx.user
    subject, facts = _facts(body, ctx)  # also checks the report or demand is the caller's to see
    result = None
    try:
        result = assist.quick(question, ctx.full, user.role, user.user_id,
                              project_id=body.project_id if body.page == "project" else None)
    except Exception as exc:  # noqa: BLE001 - the question is still answered from the facts
        logger.warning("Quick calculation failed for %s (%s)", body.page, exc.__class__.__name__)
    if result is not None:
        assist.audit_question(body.page, "calculation", "", len(question), user.user_id)
        verdict = result.verdict
        rows = [{_ROW_KEYS[k]: str(v) for k, v in r.items()} for r in change_rows(result.changes,
                                                                                   project_names(ctx.visible))]
        return AssistCalculation(question=question, request=result.request, mode=result.mode,
                           verdict={**verdict.to_dict(), "lead_reason": lead_reason(verdict.reasons)} if verdict is not None else None, changes=result.changes,
                           change_rows=rows, paper=result.paper, warnings=result.warnings,
                           next_step=assist.next_step(result, user.role))
    answer = ask(question, facts, subject)
    assist.audit_question(body.page, answer.mode, answer.model, len(question), user.user_id)
    handoff = assist.has_console(user.role) and assist.needs_console(question, lambda: ctx.visible.projects)
    return AssistAnswer(question=question, text=answer.text, mode=answer.mode, ai_generated=answer.mode == "llm",
                       note=assist.AI_NOTE if answer.mode == "llm" else assist.RULES_NOTE, truncated=answer.truncated,
                       facts=answer.facts, handoff=AssistHandoff(text=assist.HANDOFF_HINT, request=question) if handoff else None)
