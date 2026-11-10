"""Report exports, as the Streamlit export menu offers them: the SteerCo Pack (one-page HTML, print to PDF in the
browser), the analysis as Markdown and its changes as CSV – of a what-if run of the caller or of a proposal the caller
may see. Everything reads the caller's scope (projects outside it appear as one neutral node); each download is
audited."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from app.api import runs
from app.api.deps import Scope, requester, scope
from app.api.routes.proposals import _visible as proposal_visible
from app.approval_flow import audit
from app.core import clock
from app.core.auth import User
from app.core.proposals import get_proposal
from app.core.runtime_config import get_runtime_config
from app.views.exports import build_report_markdown, changes_csv, proposal_message, run_message
from app.views.steerco_pack import pack_data, render_pack_html

router = APIRouter(tags=["exports"])
Kind = Literal["steerco-pack", "markdown", "csv"]
MEDIA = {"steerco-pack": ("text/html", "steerco_pack_{stamp}.html"),
         "markdown": ("text/markdown", "derkurator_analysis_{stamp}.md"),
         "csv": ("text/csv", "derkurator_changes_{stamp}.csv")}
FILE_RESPONSES = {200: {"description": "The export as a download (Content-Disposition: attachment)",
                        "content": {media: {"schema": {"type": "string"}} for media, _ in MEDIA.values()}},
                  404: {"description": "Not found or not in the caller's scope"}}


def _export(kind: str, message: dict, user_message: dict | None, ctx: Scope, requester_id: str | None) -> Response:
    user, environment = ctx.user, get_runtime_config().environment
    if kind == "steerco-pack":
        data = pack_data(message, requester=requester_id or user.user_id, environment=environment)
        body = render_pack_html(data)
        audit("steerco_pack_exported", role=user.role, user_id=user.user_id, verdict=data["verdict"])
    elif kind == "markdown":
        pair = [m for m in (user_message, message) if m]
        body = build_report_markdown(pair, ctx.visible, user.role, user.user_id, environment)
        audit("report_exported", role=user.role, user_id=user.user_id, format="markdown", messages=len(pair))
    else:
        body = changes_csv([message])
        audit("report_exported", role=user.role, user_id=user.user_id, format="csv", messages=1)
    media, name = MEDIA[kind]
    return Response(body, media_type=f"{media}; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{name.format(stamp=clock.now().strftime("%Y%m%d_%H%M"))}"'})


@router.get("/steering/runs/{run_id}/export/{kind}", response_class=Response, responses=FILE_RESPONSES)
def export_run(run_id: str, kind: Kind, user: User = Depends(requester),
               ctx: Scope = Depends(scope)) -> Response:
    """A what-if of the caller – from their analysis history, or still held on the server: the request and the
    analysis."""
    from app.api.routes.steering import chat_run

    asked, message, _ = chat_run(user, run_id)
    if message is None:
        run = runs.get(run_id, user.user_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "The what-if was not found or has expired – run it again.")
        message, asked = run_message(run), {"role": "user", "content": run.get("request") or ""}
    return _export(kind, message, asked, ctx, user.user_id)


@router.get("/proposals/{proposal_id}/export/{kind}", response_class=Response, responses=FILE_RESPONSES)
def export_proposal(proposal_id: str, kind: Kind,
                    ctx: Scope = Depends(scope)) -> Response:
    """A proposal the caller may see (PMO: every one, others: their own); the pack names its requester."""
    proposal = get_proposal(proposal_id)
    if not proposal_visible(proposal, ctx.user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found.")
    return _export(kind, proposal_message(proposal), None, ctx, proposal.get("requester"))
