"""Approvals page: the approval inbox with the decision bar, four-eyes approval, rejection, revert and the proposal
history."""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from app import approval_flow
from app.core.formatting import timestamp
from app.core.proposals import list_proposals
from app.core.security import can_approve
from app.data_adapters import get_data_provider
from app.data_adapters.base import is_manual_change
from app.ui.approval_governance import (
    deputy_banner,
    is_approver,
    render_delegations,
    render_my_proposals,
    run_deadlines,
    sla_columns,
)
from app.ui.common import (
    ACTIONS,
    badge,
    confirm_button,
    load_dataset,
    notify,
    page_header,
    project_names,
)
from app.ui.console import HANDOVER_TEXT, override_controls
from app.ui.report import change_table, export_menu, render_report
from app.views import approvals as approval_views
from app.views.approvals import pmo_inbox  # noqa: F401 - the workspace counts the inbox with it
from app.views.navigation import page_title

logger = logging.getLogger("derkurator.ui.approvals_ui")

# =========================================================================== approvals
def _approve_proposal(proposal: dict, user_id: str, role: str, override: bool = False, reason: str = "") -> None:
    """PMO approval through the shared approval flow; the session role is passed through – the backend re-checks role,
    four-eyes and the stored change set."""
    pid = proposal["id"]
    comment = reason.strip() if override else st.session_state.get(f"inbox_comment_{pid}", "")
    outcome = approval_flow.approve_proposal(pid, user_id, role, comment=comment, override=override)
    if outcome.written:
        load_dataset.clear()
    notify(outcome.kind, outcome.message)


def _reject_proposal(proposal: dict, user_id: str, role: str) -> None:
    pid = proposal["id"]
    outcome = approval_flow.reject_proposal(pid, user_id, role, st.session_state.get(f"inbox_comment_{pid}", ""))
    notify(outcome.kind, outcome.message)


def _proposal_rows(proposals: list[dict], decided: bool = False, names: dict[str, str] | None = None) -> pd.DataFrame:
    names = names or {}
    rows = []
    for p in proposals:
        projects = [f"{pid} {names[pid]}" if pid in names else pid for pid in p.get("projects", [])]
        cells = approval_views.shown_cells(p)
        row = {"Request": p.get("request") or "–", "Projects": ", ".join(projects) or "–",
               "Verdict": cells["verdict_label"], "Budget Δ": cells["budget_delta_label"],
               "Requester": p.get("requester"), "Submitted": cells["submitted_label"], "Proposal": p["id"]}
        if decided:
            row.update({"Status": approval_views.status_label(p, with_override=True), "Decided by": approval_views.decided_by_text(p),
                        "Comment": p.get("comment") or "–"})
        else:
            row.update(sla_columns(p))
        rows.append(row)
    return pd.DataFrame(rows)


def proposal_message(proposal: dict) -> dict:
    return {"content": proposal.get("paper", ""),
            "meta": {"request": proposal.get("request"), "mode": "proposal", "proposed_changes": proposal["changes"],
                     "forced": proposal.get("forced"), "proposal_id": proposal["id"],
                     # the stored verdict record: the inbox shows the same conditions, owners and due date as the console;
                     # an older proposal without a record still shows the verdict the inbox table shows
                     **({"verdict": proposal["verdict_record"]} if isinstance(proposal.get("verdict_record"), dict)
                        else {"verdict": {"label": proposal["verdict"]}} if proposal.get("verdict") else {})}}


def _confirm_inbox_approve(proposal: dict, role: str, user_id: str) -> None:
    """Writing to the data source is irreversible for the user, so it requires the same explicit confirmation as in the
    Steering Console."""
    pid = proposal["id"]
    pending_key = f"inbox_pending_{pid}"

    manual = all(is_manual_change(c) for c in proposal["changes"])

    @st.dialog(f"Confirm: {ACTIONS['handover' if manual else 'apply'][0].lower()}",
               on_dismiss=lambda: st.session_state.pop(pending_key, None))
    def dialog() -> None:
        changes = proposal["changes"]
        if manual:
            st.info(f"Approve proposal **{pid}** of {proposal.get('requester')}. {HANDOVER_TEXT}",
                    icon=":material/assignment_turned_in:")
        else:
            st.warning(f"Approve proposal **{pid}** of {proposal.get('requester')} and write **{len(changes)} change(s)** "
                       "to the portfolio data source now? A snapshot is taken first; the System Admin can restore it.",
                       icon=":material/warning:")
        st.dataframe(change_table(changes, None), hide_index=True, width="stretch")
        override = override_controls(f"inbox_{pid}", proposal.get("verdict"))
        ready = override is None or (override[0] and bool(override[1].strip()))
        yes, no = st.columns(2)
        if no.button("Cancel", key=f"inbox_approve_cancel_{pid}", width="stretch"):
            st.session_state.pop(pending_key, None)
            st.rerun()
        label, icon = ACTIONS["handover" if manual else "apply"]
        if yes.button(f"Confirm: {label.lower()}", icon=icon,
                      key=f"inbox_approve_confirm_{pid}", type="primary", disabled=not ready, width="stretch"):
            st.session_state.pop(pending_key, None)
            _approve_proposal(proposal, user_id, role, override=override is not None, reason=override[1] if override else "")
            st.rerun()

    dialog()


def _revert_clicked(proposal_id: str, role: str, user_id: str) -> None:
    result = approval_views.revert(proposal_id, user_id, role, get_data_provider())
    notify(result.kind, result.message)


def render_revert_action(proposal: dict, role: str, user_id: str) -> None:
    """'Revert this approval' for an applied proposal: proposes its inverse change set (four-eyes again)."""
    pid = proposal["id"]
    state = approval_views.revert_state(proposal)
    if state["existing"]:
        st.caption(f":material/undo: {state['message']}")
        return
    if not state["possible"]:
        st.caption(state["message"])
        return
    label, icon = ACTIONS["revert"]
    confirm_button(f"{label} this approval", f"revert_{pid}", _revert_clicked, args=(pid, role, user_id), icon=icon,
                   confirm_label="Propose the revert",
                   message=f"Propose the values before proposal **{pid}** as a new proposal? A second PMO approves "
                           "it (four-eyes principle). Fields changed since then block the revert.",
                   help="Proposes the values before this approval as a new proposal. Fields changed since then block "
                        "the revert; the Admin restore stays the emergency path.")


def render_decision_bar(proposal: dict, role: str, user_id: str) -> None:
    pid = proposal["id"]
    hints = approval_views.decision_hints(proposal, user_id, role)
    # a deputy decides a proposal of the delegator they act for in their own name (not on their behalf)
    own, proxy_own, blocked = hints["own"], hints["proxy_own"], hints["blocked"]
    pending_key = f"inbox_pending_{pid}"
    with st.container(border=True):
        st.markdown(f"**Decision on proposal {pid}**")
        comment, reject, approve = st.columns([3, 1, 1], vertical_alignment="bottom")
        text = comment.text_input("Comment (required for a rejection)", key=f"inbox_comment_{pid}")
        reject.button(ACTIONS["reject"][0], icon=ACTIONS["reject"][1], key=f"inbox_reject_{pid}",
                      on_click=_reject_proposal, args=(proposal, user_id, role), width="stretch",
                      disabled=blocked or not text.strip(),
                      help="Four-eyes principle: another PMO decides on your own proposal." if own
                      else "Enter a comment first – a rejection needs a reason." if not text.strip() else None)
        label, icon = ACTIONS["handover" if hints["manual"] else "apply"]
        if approve.button(f"{label} …", icon=icon, key=f"inbox_approve_{pid}", type="primary",
                          disabled=blocked, width="stretch",
                          help="Four-eyes principle: you cannot approve your own proposal." if own
                          else "Opens a confirmation with the change set."):
            st.session_state[pending_key] = True
        if own:
            st.info("Four-eyes principle: you cannot approve or reject your own proposal – a second PMO decides.",
                    icon=":material/lock:")
        elif proxy_own:
            st.caption(f":material/badge: This is a proposal of {hints['delegator']}, whom you deputise for – "
                       "you decide it in your own name.")
        if not own and hints["needs_override"]:
            st.caption(f":material/gpp_maybe: Rule-based verdict {proposal.get('verdict')} – approving needs an "
                       "explicit override with a reason.")
    if st.session_state.get(pending_key) and not blocked:
        _confirm_inbox_approve(proposal, role, user_id)


def page_approvals(ctx: dict) -> None:
    role, user_id, ds = ctx["role"], ctx["user_id"], ctx["ds"]
    run_deadlines()
    approver = is_approver(role)
    if not approver:
        page_header(page_title(role, "approvals", "Approvals"), "Your proposals and the PMO's decisions")
        render_my_proposals(list_proposals(requester=user_id), user_id, role)
        return

    lists = approval_views.queues(user_id, role)
    pending, decided, mine = lists["pending"], lists["decided"], lists["mine"]
    page_header("Approvals", "Four-eyes principle · a proposal is written only after a second PMO approves it")
    deputy_banner(user_id, role)
    counts = {"pending": len(pending), "decided": len(decided), "mine": len(mine)}
    labels = {"pending": "Pending", "decided": "Decided", "mine": "Mine", "delegations": "Delegations"}
    views = ["pending", "decided", "mine"] + (["delegations"] if can_approve(role) else [])
    # stable option keys: the counts change after an action, the selected view must not
    view = st.segmented_control("View", views, default="pending", key="approvals_view", label_visibility="collapsed",
                                required=True, format_func=lambda k: f"{labels[k]} ({counts[k]})" if k in counts
                                else labels[k]) or "pending"
    names = project_names(ds)
    if view == "mine":
        render_my_proposals(mine, user_id, role)
        return
    if view == "delegations":
        render_delegations(user_id, role)
        return
    if view == "decided":
        st.subheader("Decided proposals")
        if not decided:
            st.caption("No decided proposals yet.")
            return
        event = st.dataframe(_proposal_rows(decided, decided=True, names=names), hide_index=True, width="stretch",
                             column_config={"Proposal": None}, selection_mode="single-row", on_select="rerun",
                             key="decided_grid")
        rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
        if rows and rows[0] < len(decided) and can_approve(role):
            render_revert_action(decided[rows[0]], role, user_id)
        elif can_approve(role):
            st.caption("Select an approved proposal to take its approval back.")
        return
    st.subheader(f"Pending approvals ({len(pending)})")
    if not pending:
        st.caption(":material/check_circle: No proposals waiting for approval – the inbox is empty.")
        return
    event = st.dataframe(_proposal_rows(pending, names=names), hide_index=True, width="stretch",
                         column_config={"Proposal": None},  # the id is for links, not for reading
                         selection_mode="single-row", on_select="rerun", key="inbox_grid")
    rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    # a link to one proposal (?proposal=… from a notification, or a hand-over from another page) opens it directly
    picked = st.query_params.get("proposal") or st.session_state.get("inbox_pick")
    if rows and rows[0] < len(pending):
        proposal = pending[rows[0]]
    elif picked and any(p["id"] == picked for p in pending):
        proposal = next(p for p in pending if p["id"] == picked)
    else:  # never decide a proposal nobody picked
        st.info("Select a proposal in the table to review and decide it.", icon=":material/touch_app:")
        return
    eyebrow = f"Proposal {proposal['id']} · from {proposal.get('requester')} · {timestamp(proposal.get('timestamp'))}"
    level = int(proposal.get("escalation_level") or 0)
    status_md = (badge(f"Escalated{' twice' if level > 1 else ''} – overdue", "red", ":material/schedule:") if level
                 else badge("Waiting for your decision", "blue", ":material/pending_actions:"))
    render_report(f"inbox_{proposal['id']}", proposal_message(proposal), ds, request=proposal.get("request") or "–",
                  eyebrow=eyebrow, status_md=status_md,
                  requester=proposal.get("requester"),
                  actions=lambda: export_menu(f"inbox_{proposal['id']}", proposal_message(proposal), None, ds,
                                              requester=proposal.get("requester")))
    render_decision_bar(proposal, role, user_id)
