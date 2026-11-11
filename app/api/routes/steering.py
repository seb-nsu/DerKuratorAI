"""Steering Console: one what-if through data quality, simulation, dependency check, rule-based verdict and decision
paper – from free text or from a guided scenario. The result stays on
the server under its run id; a proposal, a direct apply or a saved what-if names that id, never a change set.

The chat history is kept per user and role: every run is added to it, so a reload of either front end shows the
same analyses."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app import services
from app.analysis_runs import log_analysis_run, run_mode
from app.api import limits, runs
from app.api.deps import Scope, clear_data_cache, requester, scope
from app.approval_flow import audit
from app.core import clock, cost_tracker
from app.core.auth import User
from app.core.business_rules import business_assumptions_md
from app.core.chat_store import append_messages, clear_chat, load_chat, update_message
from app.core.proposals import requires_override
from app.core.scenario_store import ScenarioStoreError
from app.core.security import ROLE_PMO, apply_rls_to_dataset, approval_mode, log_security_event
from app.crew.scenarios import build_guided_scenario
from app.data_adapters.base import is_manual_change, net_changes
from app.views import console as views

router = APIRouter(tags=["steering"])
logger = logging.getLogger("derkurator.api.steering")

HIDDEN_META = ("cost_usd", "tokens", "scenario_state")  # LLM costs stay in the Admin Console; the state stays here
def _set_latest(user_id: str, run_id: str | None) -> None:
    runs.set_latest(user_id, run_id)


def _is_latest(user_id: str, run_id: str) -> bool:
    return runs.is_latest(user_id, run_id)


class GuidedPlan(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    project: str | None = Field(None, max_length=64)
    target: str | None = Field(None, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class SteeringRequest(BaseModel):
    request: str = Field("", max_length=4000)  # free text; leave empty with `guided`
    guided: GuidedPlan | None = None  # a guided scenario: the plan is built server-side, no free-text parsing
    force: bool = False  # PMO only: run despite blocking data-quality findings (audited)
    base_run_id: str | None = None  # continue that what-if: the request builds on its changed data


class ApplyRequest(BaseModel):
    override: bool = False  # apply against a NO-GO / incomplete verdict: needs the reason in `comment`
    comment: str = Field("", max_length=2000)


class SaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


# --------------------------------------------------------------------------- answers
class Change(BaseModel):
    """One proposed change (old and new value of a field; `table` and `key` for rows of other tables)."""
    model_config = ConfigDict(extra="allow")
    project_id: str
    field: str | None = None
    old: Any = None
    new: Any = None
    table: str | None = None


class ScenarioInfo(BaseModel):
    action: str
    label: str
    project: Literal["none", "required"]
    project_label: str
    target: Literal["none", "optional", "required"]
    target_label: str | None = None
    note: str | None = None


class GuidedProject(BaseModel):
    project_number: str
    project_name: str
    blocked: bool


class GuidedCatalog(BaseModel):
    scenarios: list[ScenarioInfo]
    examples: list[str]  # start examples of the free text, from projects in the caller's scope
    projects: list[GuidedProject]
    can_force: bool  # PMO while the Data Steward blocks a project
    rules_md: str  # the business rules the verdict is computed from


class ParamOption(BaseModel):
    value: bool | str
    label: str


class UnitDefault(BaseModel):
    default: float
    step: float


class ScenarioParam(BaseModel):
    name: str
    label: str
    kind: Literal["number", "integer", "choice", "date", "text"]
    default: bool | int | float | str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[ParamOption] | None = None
    required: bool = False
    max_length: int | None = None
    help: str | None = None
    zero_is_none: bool = False  # 0 means "not set"
    unit_defaults: dict[str, UnitDefault] | None = None  # default and step per unit ("%" / "€")


class GuidedForm(ScenarioInfo):
    params: list[ScenarioParam]
    notes: list[str]


class ApplyResult(BaseModel):
    kind: Literal["success", "warning"]  # warning: written, but the audit record failed
    message: str
    closed_as: Literal["applied", "handed_over"]
    applied: int
    manual: int
    snapshot_id: str | None
    override: bool
    applied_at: str | None


class SavedWhatIf(BaseModel):
    id: str
    name: str
    budget_impact: dict[str, float]
    changes: int
    steps: int


class RunBadge(BaseModel):
    label: str
    tone: Literal["success", "warning", "danger", "info", "neutral"]
    icon: str  # Material Symbols name


class RunStatus(BaseModel):
    state: Literal["applied", "handed_over", "proposal", "outdated", "draft", "analysis"]
    badge: RunBadge | None = None
    actionable: bool = False  # latest step, still held on the server: apply, save or submit it by its run_id
    override: bool | None = None
    applied_at: str | None = None
    proposal_id: str | None = None
    proposal_status: str | None = None
    decided_by: str | None = None
    comment: str | None = None


class ChatFinding(BaseModel):
    Severity: str | None = None
    Project: str | None = None
    Rule: str | None = None
    Field: str | None = None
    Finding: str | None = None


class ChatMeta(BaseModel):
    request: str | None = None
    mode: str | None = None
    duration_s: float | None = None
    warnings: list[str] = []
    steward_md: str | None = None
    dq_findings: list[ChatFinding] = []
    plan: dict[str, Any] | None = None
    proposed_changes: list[Change] = []
    verdict: dict[str, Any] | None = None
    forced: bool = False
    continued: bool = False
    turns: list[str] = []
    run_id: str | None = None
    applied: bool = False
    handed_over: bool = False
    applied_at: str | None = None
    override: bool = False
    proposal_id: str | None = None
    proposal_status: str | None = None
    saved_as: str | None = None


class ChatMessage(BaseModel):
    id: str | None = None
    role: Literal["user", "assistant"]
    content: str = ""
    ts: str | None = None
    meta: ChatMeta | None = None
    status: RunStatus | None = None


class Continuation(BaseModel):
    available: bool  # the next request can build on the latest step (send base_run_id)
    base_run_id: str | None
    step: int | None
    turns: list[str]  # the last three steps so far
    previous_verdict_blocks: Literal["NO-GO", "INCOMPLETE"] | None
    default_continue: bool  # the console's default: off – a new what-if starts from the data unless the user opts in


class ChatHistory(BaseModel):
    messages: list[ChatMessage]
    continuation: Continuation


def _held_run(run_id: str, user: User) -> dict:
    run = runs.get(run_id, user.user_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The what-if was not found or has expired – run it again.")
    return run


def latest_only(run_id: str, user: User) -> None:
    """Only the latest step of a what-if can be applied, saved or submitted (as in the console)."""
    if not _is_latest(user.user_id, run_id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Outdated step – only the latest step of this what-if can be approved or saved.")


def _blocked(ctx: Scope) -> set[str]:
    from app.crew.tools.data_steward_tools import run_all_checks

    return set(run_all_checks(ctx.visible).blocked_project_ids)


# --------------------------------------------------------------------------- guided scenarios
@router.get("/steering/guided")
def guided_catalog(user: User = Depends(requester), ctx: Scope = Depends(scope)) -> GuidedCatalog:
    """The guided scenarios and the projects of the caller's scope to pick from (blocked ones flagged)."""
    blocked = _blocked(ctx)
    projects = ctx.visible.projects
    names = dict(zip(projects["project_number"].astype(str), projects["project_name"].astype(str), strict=True)) \
        if not projects.empty else {}
    return {"scenarios": views.scenario_catalog(),
            "projects": [{"project_number": pid, "project_name": names[pid], "blocked": pid in blocked}
                         for pid in sorted(names)],
            "examples": views.example_prompts(ctx.visible),
            "can_force": user.role == ROLE_PMO and bool(blocked),
            "rules_md": business_assumptions_md()}


@router.get("/steering/guided/{action}")
def guided_form(action: str, project: str | None = Query(None, max_length=64), force: bool = False,
                user: User = Depends(requester), ctx: Scope = Depends(scope)) -> GuidedForm:
    """The inputs of one guided scenario for `project`: defaults, bounds (e.g. the lowest schedule shift) and options
    (teams, skills, risks, phases), plus the hints the builder shows."""
    catalog = {s["action"]: s for s in views.scenario_catalog()}
    if action not in catalog:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown scenario.")
    if catalog[action]["project"] == "none":
        project = None
    elif project is not None and project not in set(ctx.visible.project_numbers):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.")
    blocked = _blocked(ctx)
    can_force = user.role == ROLE_PMO and bool(blocked)
    return {**catalog[action], "params": views.scenario_params(action, ctx.visible, project),
            "notes": views.scenario_notes(action, ctx.visible, project, blocked, can_force, force and can_force)}


# --------------------------------------------------------------------------- runs
class Finding(BaseModel):
    severity: str
    project: str
    rule: str
    field: str
    message: str


class RunAnswer(BaseModel):
    run_id: str
    request: str
    mode: str  # LLM | LLM (parser fallback) | offline | fallback | blocked | planning
    verdict: dict[str, Any] | None  # rule-based: decision, label, rule, reasons, conditions, condition_details, …
    decision_paper: str
    proposed_changes: list[dict[str, Any]]
    change_set: list[dict[str, Any]]  # net change per field – what a proposal, apply or save uses
    approval_mode: str | None  # apply | propose | none; null without changes
    handover: bool
    warnings: list[str]
    duration_s: float
    verdict_s: float | None
    data_quality_findings: list[Finding]
    plan: dict[str, Any] | None
    continued: bool
    step: int
    turns: list[str]
    forced: bool
    chat_message_id: str | None


@router.post("/steering/runs")
def run_what_if(body: SteeringRequest, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> RunAnswer:
    plan, request = None, body.request.strip()
    if body.guided is not None:
        if request:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Send either a request text or a guided scenario.")
        g = body.guided
        try:
            project, target, params = views.guided_inputs(g.action, g.project, g.target, g.params, ctx.visible)
            projects = ctx.visible.projects
            names = dict(zip(projects["project_number"].astype(str), projects["project_name"].astype(str), strict=True))
            scenario = build_guided_scenario(g.action, project, names, target=target, **params)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found.") from exc
        except ValueError as exc:  # includes GuidedInputError: a message for the user
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        plan, request = scenario.plan, scenario.request
    elif not request:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Describe a change or choose a guided scenario.")
    base_state = None
    if body.base_run_id:
        base = runs.get(body.base_run_id, user.user_id)
        if base is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "The what-if to continue was not found or has expired.")
        base_state = base.get("scenario_state")
    force = False
    if body.force:  # as in the console: PMO only, and only while the Data Steward blocks a project
        if user.role != ROLE_PMO:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the PMO can force a simulation.")
        force = bool(_blocked(ctx))
    if force:
        audit("forced_simulation", role=user.role, user_id=user.user_id, request=request)
        log_security_event("Forced simulation requested (data quality gate overridden)", level=logging.WARNING,
                           role=user.role, user=user.user_id)
    logger.info("API | Steering request started | role=%s, guided=%s, forced=%s, chars=%d", user.role,
                plan is not None, force, len(request))  # never the request text (may contain names)
    stamp = clock.now().strftime("%Y-%m-%d %H:%M")
    with limits.heavy(user.user_id), cost_tracker.track_request():
        result = services.run_steering_pipeline(request, ctx.visible, user.role, user.user_id, pii_source=ctx.full,
                                                plan=plan, force=force, base_state=base_state)
    run_id = uuid.uuid4().hex
    log_analysis_run(user.role, result, run_id, user.user_id)
    verdict = result.verdict.to_dict() if result.verdict else None
    messages = [views.user_message(f"**Scenario:** {request}" if plan is not None else request, base_state, force, stamp),
                views.assistant_message(request, result, run_id, force, base_state)]
    append_messages(user.role, user.user_id, messages)  # the shared chat history (assigns the message ids)
    changes = net_changes(result.proposed_changes)  # one change per field: the net effect of all steps
    runs.put(run_id, {"user_id": user.user_id, "role": user.role, "request": request,
                      "paper": result.decision_paper, "verdict": verdict, "changes": changes,
                      "proposed_changes": result.proposed_changes, "forced": force,
                      "scenario_state": result.scenario_state, "chat_message_id": messages[1].get("id"),
                      "mode": result.mode, "warnings": result.warnings,  # the report exports read these
                      "plan": result.intent_plan.model_dump() if result.intent_plan else None})
    _set_latest(user.user_id, run_id)
    steward = result.steward_report
    turns = (result.scenario_state or {}).get("turns") or []
    return RunAnswer(**{
        "run_id": run_id, "request": request, "mode": run_mode(result.mode, result.warnings), "verdict": verdict,
        "decision_paper": result.decision_paper, "proposed_changes": result.proposed_changes, "change_set": changes,
        "approval_mode": approval_mode(user.role, changes) if changes else None,
        "handover": bool(changes) and all(is_manual_change(c) for c in changes),
        "warnings": result.warnings, "duration_s": round(result.duration_s or 0, 2),
        "verdict_s": round(result.verdict_s, 2) if result.verdict_s is not None else None,
        "data_quality_findings": [{"severity": getattr(i.severity, "value", i.severity), "project": i.project_id,
                                   "rule": i.rule, "field": i.column, "message": i.message}
                                  for i in steward.issues] if steward else [],
        "plan": result.intent_plan.model_dump() if result.intent_plan else None,
        "continued": base_state is not None, "step": len(turns), "turns": turns, "forced": force,
        "chat_message_id": messages[1].get("id"),  # LLM costs stay in the Admin Console (FinOps), never here
    })


@router.post("/steering/runs/{run_id}/apply")
def apply_run(run_id: str, body: ApplyRequest, user: User = Depends(requester),
              ctx: Scope = Depends(scope)) -> ApplyResult:
    """Approve & apply a change set without governed fields (approval mode 'apply'), or record the hand-over of
    manual changes (e.g. resource plans). Governed changes go through POST /proposals (four-eyes)."""
    run = _held_run(run_id, user)
    latest_only(run_id, user)
    changes = run.get("changes") or []
    if not changes:
        raise HTTPException(status.HTTP_409_CONFLICT, "This what-if has no change to apply.")
    mode = approval_mode(user.role, changes)
    if mode == "propose":
        raise HTTPException(status.HTTP_409_CONFLICT, "These changes need the approval of a PMO other than the requester "
                                                      "– submit them as a proposal.")
    if mode != "apply":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot apply changes.")
    label = (run.get("verdict") or {}).get("label")
    overridden = requires_override(label)
    if overridden and not (body.override and body.comment.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"The verdict is {label}: applying needs `override` and the reason in `comment`.")
    allowed = set(apply_rls_to_dataset(ctx.full, user.role, user.user_id).project_numbers)  # server-side re-check
    if any(c["project_id"] not in allowed for c in changes):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No permission for a project in this change set.")
    outcome = views.apply_change_set(changes, user.role, user.user_id, request=run.get("request"),
                                     verdict=run.get("verdict"), forced=run.get("forced", False), run_id=run_id,
                                     override_reason=body.comment if overridden else "")
    if not outcome.done:
        if outcome.system_error:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, outcome.message)
        raise HTTPException(status.HTTP_409_CONFLICT, outcome.message)
    clear_data_cache()
    runs.pop(run_id)  # closed: no second apply, no proposal, no continuation
    _set_latest(user.user_id, None)
    update_message(user.role, user.user_id, run.get("chat_message_id"),
                   {outcome.closed_as: True, "applied_at": outcome.applied_at,
                    **({"override": True} if outcome.override else {})})
    return {"kind": outcome.kind, "message": outcome.message, "closed_as": outcome.closed_as,
            "applied": outcome.applied, "manual": outcome.manual, "snapshot_id": outcome.snapshot_id,
            "override": outcome.override, "applied_at": outcome.applied_at}


@router.post("/steering/runs/{run_id}/save", status_code=status.HTTP_201_CREATED)
def save_run(run_id: str, body: SaveRequest, user: User = Depends(requester),
             ctx: Scope = Depends(scope)) -> SavedWhatIf:
    """Saves the latest step of a what-if for the A/B comparison (What-if comparison page)."""
    run = _held_run(run_id, user)
    latest_only(run_id, user)
    if run.get("saved_as"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already saved as '{run['saved_as']}'.")
    if not run.get("changes") or not run.get("scenario_state"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only what-ifs with proposed changes can be saved for a comparison.")
    try:
        entry = views.save_what_if(body.name, user.role, user.user_id, run.get("request", ""), run.get("scenario_state"),
                                   run.get("verdict"), run.get("proposed_changes") or [], ctx.full.projects)
    except ScenarioStoreError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"What-if not saved: {exc}") from exc
    run["saved_as"] = entry["name"]
    update_message(user.role, user.user_id, run.get("chat_message_id"), {"saved_as": entry["name"]})
    return {"id": entry["id"], "name": entry["name"], "budget_impact": entry["budget_impact"],
            "changes": len(entry["changes"]), "steps": len(entry["turns"])}


# --------------------------------------------------------------------------- chat history
def chat_run(user: User, run_id: str) -> tuple[dict | None, dict | None, bool]:
    """(user message, assistant message, is the latest analysis) of a run in the caller's chat history; (None, None,
    False) when the history has no such run."""
    messages = load_chat(user.role, user.user_id)
    last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and (m.get("meta") or {}).get("run_id") == run_id:
            asked = messages[i - 1] if i > 0 and messages[i - 1].get("role") == "user" else None
            return asked, m, i == last
    return None, None, False


def _public(message: dict, state: dict | None) -> dict:
    meta = message.get("meta")
    out = {k: v for k, v in message.items() if k != "meta"}
    if meta is not None:
        out["meta"] = {k: v for k, v in meta.items() if k not in HIDDEN_META}
        out["meta"]["turns"] = (meta.get("scenario_state") or {}).get("turns") or []
    if state is not None:
        out["status"] = state
    return out


@router.get("/steering/chat")
def chat_history(user: User = Depends(requester)) -> ChatHistory:
    """The caller's analysis history (the same one every front end restores after a reload), each analysis with
    its status, and whether the next request can build on the latest step."""
    messages = load_chat(user.role, user.user_id)
    last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    out = []
    for i, m in enumerate(messages):
        state = None
        if m.get("role") == "assistant" and (m.get("meta") or {}).get("mode"):
            state = views.run_state(m["meta"], i == last)
            run_id = m["meta"].get("run_id")
            state["actionable"] = bool(run_id and i == last and state["state"] == "draft"
                                       and runs.get(run_id, user.user_id) is not None)
            state["badge"] = views.run_badge(m["meta"], state)
        out.append(_public(m, state))
    base = views.last_scenario_message(messages)
    base_run_id = (base.get("meta") or {}).get("run_id") if base else None
    held = bool(base_run_id) and runs.get(base_run_id, user.user_id) is not None
    blocks = views.previous_verdict_blocks(messages)
    turns = (base["meta"]["scenario_state"].get("turns") or []) if base else []
    return {"messages": out, "continuation": {
        "available": held, "base_run_id": base_run_id if held else None, "step": len(turns) + 1 if held else None,
        "turns": turns[-3:] if held else [], "previous_verdict_blocks": blocks, "default_continue": False}}


@router.delete("/steering/chat", status_code=status.HTTP_204_NO_CONTENT)
def clear_history(user: User = Depends(requester)) -> Response:
    """Clears the caller's history including all unsaved what-ifs (saved ones stay on the What-if comparison page)."""
    clear_chat(user.role, user.user_id)
    _set_latest(user.user_id, None)  # the held runs can no longer be applied or saved
    return Response(status_code=status.HTTP_204_NO_CONTENT)
