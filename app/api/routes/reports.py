"""The report viewer: one decision report as the Steering Console and the Approvals page show it – verdict, key
figures, change count, the tabs (Summary, Decision paper, Changes, Impact, Dependencies, Data quality) with their
charts as Plotly figure JSON – of a what-if in the caller's history or of a proposal the caller may see. A what-if
report also carries the console's actions on it (save, submit, apply or hand over), as the console offers them. The
history exports (all analyses as Markdown, all proposed changes as CSV) live here too."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.api import runs
from app.api.deps import Scope, requester, scope
from app.api.routes.exports import FILE_RESPONSES
from app.api.routes.proposals import _visible as proposal_visible
from app.api.routes.steering import RunBadge, chat_run, latest_only
from app.approval_flow import audit
from app.core import clock
from app.core.auth import User
from app.core.chat_store import load_chat
from app.core.proposals import get_proposal, requires_override
from app.core.runtime_config import get_runtime_config
from app.core.security import approval_mode
from app.data_adapters.base import is_manual_change, net_changes
from app.views import console as console_views
from app.views.exports import build_report_markdown, changes_csv, proposal_message, run_message
from app.views.reports import business_change_rows, report_view

router = APIRouter(tags=["reports"])


class ReportVerdict(BaseModel):
    label: str
    key: Literal["GO", "CONDITIONAL", "NO-GO", "INCOMPLETE", "NONE"]
    lead: str  # the first reason in one line (the callout)


class ReportMetric(BaseModel):
    label: str
    value: str
    detail: str


class ReportChanges(BaseModel):
    decisions: int
    fields: int
    projects: int
    governed: int
    referenced: int
    caption: str
    help: str


class ReportSummary(BaseModel):
    warnings: list[str]
    steps: list[str]  # "Step n of a what-if" notes: context, not a problem
    forced: bool
    offline: bool  # written by the rule engine (no AI agents)
    as_of: str | None
    reasons: list[str]
    conditions: list[str]
    flags: list[str]
    rules: list[dict[str, str]]  # the thresholds stored with the verdict: Rule, Value


class ReportTab(BaseModel):
    id: Literal["summary", "paper", "changes", "impact", "dependencies", "data_quality"]
    label: str


class PaperSection(BaseModel):
    title: str
    body: str


class ReportImpact(BaseModel):
    figures: list[dict[str, Any]]
    caption: str


class DependencyLevel(BaseModel):
    figure: dict[str, Any] | None
    rows: list[dict[str, str]]  # Project (waits), Depends on, Type, Status


class ReportDependencies(BaseModel):
    count: int
    direct_count: int
    caption: str
    toggle_label: str | None  # "Show all levels (n links)" when there is more than the direct links
    direct: DependencyLevel
    all: DependencyLevel | None


class ReportDataQuality(BaseModel):
    caption: str
    rows: list[dict[str, str]]  # Severity, Project, Rule, Field, Finding


class ReportView(BaseModel):
    title: str
    verdict: ReportVerdict
    metrics: list[ReportMetric]
    changes: ReportChanges
    summary: ReportSummary
    tabs: list[ReportTab]
    paper: str
    paper_sections: list[PaperSection]  # empty: the paper has no sections, show it whole
    change_table: list[dict[str, str]]  # Project, Change, Current, Proposed, Δ, Approval, Note
    impact: ReportImpact
    dependencies: ReportDependencies | None
    data_quality: ReportDataQuality | None


class RunActions(BaseModel):
    """The console's action bar of one analysis. `primary`: submit (governed changes, four-eyes), apply or handover
    (needs no approval); none while closed, outdated, submitted or no longer held. `note` is the text shown instead."""
    held: bool  # the server still holds the run: it can be exported, saved, submitted or applied
    can_save: bool
    saved_as: str | None
    primary: Literal["submit", "apply", "handover"] | None
    primary_enabled: bool
    primary_help: str | None
    note: str | None
    needs_override: bool  # NO-GO / incomplete: submit needs a reason, apply an override plus a reason
    verdict_label: str | None
    change_count: int
    confirm_rows: list[dict[str, str]]  # the change set of the confirmation: Project, Change, Current, Proposed, Note


class RunReport(ReportView):
    run_id: str
    message_id: str | None
    ts: str | None
    request: str
    eyebrow: str
    status: RunBadge
    actions: RunActions


def _run_actions(meta: dict, run: dict | None, is_last: bool, user: User) -> dict:
    changes = net_changes(meta.get("proposed_changes"))  # one change per field: the net effect of all steps
    closed = bool(meta.get("applied") or meta.get("handed_over"))
    label = console_views.verdict_label(meta.get("verdict"))
    proposal = get_proposal(meta["proposal_id"]) if meta.get("proposal_id") else None
    decided = bool(proposal and proposal.get("status") in ("approved", "rejected"))
    out = {"held": run is not None, "saved_as": meta.get("saved_as") or (run or {}).get("saved_as"),
           "can_save": bool(run is not None and changes and is_last and run.get("scenario_state") and not closed
                            and not decided and not run.get("saved_as") and not meta.get("saved_as")),
           "primary": None, "primary_enabled": False, "primary_help": None, "note": None,
           "needs_override": requires_override(label), "verdict_label": label, "change_count": len(changes),
           "confirm_rows": business_change_rows(changes)}
    if not changes or closed:
        return out
    if meta.get("proposal_id"):
        if proposal and proposal.get("comment"):
            out["note"] = f"PMO comment: {proposal['comment']}"
        return out
    if not is_last:
        out["note"] = "Outdated step – only the latest step of this what-if can be approved."
        return out
    if run is None:
        out["note"] = "This what-if is no longer held on the server – run it again to submit or apply it."
        return out
    mode = approval_mode(user.role, changes)
    if mode == "propose":  # four-eyes principle: governed changes go to the PMO inbox – also those of a PMO
        out.update(primary="submit", primary_enabled=True)
        return out
    manual = all(is_manual_change(c) for c in changes)
    out.update(primary="handover" if manual else "apply", primary_enabled=mode == "apply",
               primary_help="Nothing is written – the change is recorded for manual execution." if manual
               else "Opens a confirmation with the change set." if mode == "apply"
               else "Your role can only submit proposals for this change.")
    return out


def _held_latest(run_id: str, user: User) -> dict | None:
    run = runs.get(run_id, user.user_id)
    if run is None:
        return None
    try:
        latest_only(run_id, user)
    except HTTPException:
        return None
    return run


@router.get("/steering/runs/{run_id}/report")
def run_report(run_id: str, user: User = Depends(requester), ctx: Scope = Depends(scope)) -> RunReport:
    """One analysis of the caller's history (or a run still held) as the report viewer shows it, with its status and
    the actions the console offers on it."""
    asked, message, is_last = chat_run(user, run_id)
    held = runs.get(run_id, user.user_id)
    if message is None:
        if held is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "The what-if was not found or has expired – run it again.")
        message, is_last = run_message(held), True
        message["meta"].update(run_id=run_id, scenario_state=held.get("scenario_state"),
                               saved_as=held.get("saved_as"))
    meta = message.get("meta") or {}
    environment = get_runtime_config().environment
    turns = len((meta.get("scenario_state") or {}).get("turns") or [])
    eyebrow = " · ".join(x for x in ("Decision report", message.get("ts") or "", user.user_id, environment,
                                     f"step {turns}" if meta.get("continued") and turns else "",
                                     "forced simulation" if meta.get("forced") else "") if x)
    state = console_views.run_state(meta, is_last)
    run = _held_latest(run_id, user) if is_last else None
    return RunReport(**report_view(message, ctx.visible, requester=user.user_id, environment=environment),
                     run_id=run_id, message_id=message.get("id"), ts=message.get("ts"),
                     request=str(meta.get("request") or (asked or {}).get("content") or "Analysis")
                     .replace("**Scenario:** ", ""),
                     eyebrow=eyebrow, status=console_views.run_badge(meta, state),
                     actions=_run_actions(meta, run, is_last, user))


@router.get("/proposals/{proposal_id}/report")
def proposal_report(proposal_id: str, ctx: Scope = Depends(scope)) -> ReportView:
    """A proposal the caller may see (PMO: every one, others: their own) as the report viewer shows it, read in the
    caller's scope."""
    proposal = get_proposal(proposal_id)
    if not proposal_visible(proposal, ctx.user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    return ReportView(**report_view(proposal_message(proposal), ctx.visible, requester=proposal.get("requester") or "",
                                    environment=get_runtime_config().environment))


@router.get("/steering/chat/export/{kind}", response_class=Response, responses=FILE_RESPONSES)
def export_history(kind: Literal["markdown", "csv"], user: User = Depends(requester),
                   ctx: Scope = Depends(scope)) -> Response:
    """The whole analysis history: the simulation report (Markdown) or every proposed change (CSV); audited."""
    messages = load_chat(user.role, user.user_id)
    stamp = clock.now().strftime("%Y%m%d_%H%M")
    if kind == "markdown":
        body = build_report_markdown(messages, ctx.visible, user.role, user.user_id, get_runtime_config().environment)
        media, name = "text/markdown", f"derkurator_simulation_report_{stamp}.md"
    else:
        body, media, name = changes_csv(messages), "text/csv", f"derkurator_changes_{stamp}.csv"
    audit("report_exported", role=user.role, user_id=user.user_id, format=kind, messages=len(messages))
    return Response(body, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
