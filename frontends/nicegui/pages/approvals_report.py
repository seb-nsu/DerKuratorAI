"""The report of one proposal in the PMO inbox: the shared report viewer on `GET /proposals/{id}/report` with the
proposal's eyebrow and status badge, the export menu (SteerCo Pack, Markdown, CSV) and the report's question box –
as the React inbox shows it. The decision bar stays on the Approvals page."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api, report_viewer
from frontends.nicegui.components import error_box

CHANGE_COLUMNS = ["Project", "Change", "Current", "Proposed", "Δ", "Approval", "Note"]


async def proposal_report(proposal: dict, status: tuple[str, str, str] | None = None) -> None:
    """`proposal`: the answer of GET /proposals/{id}; `status`: (text, tone, icon) of the badge next to the verdict."""
    pid = proposal["id"]
    try:
        report = await api.get(f"/proposals/{pid}/report")
    except api.ApiError as exc:
        error_box(exc.message)
        return
    card = report_viewer.render(
        report, actions=lambda: report_viewer.export_menu(f"/proposals/{pid}"),
        eyebrow=f"Proposal {pid} · from {proposal.get('requester')} · {proposal['submitted_label']}",
        status=dict(zip(("label", "tone", "icon"), status, strict=True)) if status else None,
        title=proposal.get("request") or "–", key=f"inbox_{pid}")
    if proposal.get("justification"):
        with card:
            ui.label(f"Reason given by the requester: {proposal['justification']}").classes("q-mt-sm")
    await report_viewer.ask_about(card, proposal_id=pid)
