"""Approval governance in the Approvals page: deadlines (SLA, escalation, expiry), withdrawing an own proposal and
delegating the approval right to a deputy. The rules live in app.core.proposals; this module only shows them."""
from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd
import streamlit as st

from app import approval_flow
from app.core import clock
from app.core.business_rules import current_rules
from app.core.formatting import timestamp
from app.core.proposals import SUBMITTED, list_delegations
from app.core.security import can_approve
from app.ui.common import ACTIONS, STATUS_LABELS, confirm_button, notify
from app.views import approvals as views
from app.views.approvals import SLA_LABELS, sla_columns, sort_by_urgency  # noqa: F401 - used by the pages

logger = logging.getLogger("derkurator.ui.approvals")


def is_approver(role: str) -> bool:
    """The PMO decides on proposals (a PMO who deputises for another PMO decides on their behalf)."""
    return can_approve(role)


def run_deadlines() -> None:
    """Records due escalations and expiries once per page view and tells the reader what changed."""
    events = approval_flow.run_deadlines()
    expired = [e["id"] for e in events if e["type"] == "expired"]
    escalated = [e["id"] for e in events if e["type"] == "escalated"]
    if expired or escalated:
        parts = ([f"{len(escalated)} proposal(s) escalated past the approval deadline"] if escalated else []) + \
                ([f"{len(expired)} proposal(s) expired without a decision"] if expired else [])
        st.toast(" · ".join(parts), icon=":material/schedule:")


def deputy_banner(user_id: str, role: str) -> None:
    delegation = views.deputy_info(user_id, role)
    if delegation:
        st.info(f"You are deputy of **{delegation['delegator']}** until {delegation['end']} – you decide proposals "
                "on their behalf, their own proposals in your own name (never your own).", icon=":material/badge:")


def _withdraw_clicked(proposal_id: str, user_id: str, role: str) -> None:
    result = views.withdraw(proposal_id, user_id, role, st.session_state.get(f"withdraw_reason_{proposal_id}", ""))
    notify(result.kind, result.message)


def render_my_proposals(mine: list[dict], user_id: str, role: str) -> None:
    """The requester's proposals with deadline state; an open one can be withdrawn."""
    st.subheader("My proposals")
    if not mine:
        st.caption("You have not submitted a proposal yet. Proposals are created from a Steering Console report, "
                   "governed fields in the project detail, or (PMO) the Planning page and reverts.")
        return
    st.dataframe(pd.DataFrame([{
        "Proposal": p["id"], "Submitted": timestamp(p.get("timestamp")),
        "Status": STATUS_LABELS.get(p["status"], str(p["status"])), **sla_columns(p),
        "Decided by": p.get("decided_by") or "–", "Comment": p.get("comment") or "–",
        "Request": p.get("request") or "–", "Projects": ", ".join(p.get("projects", [])),
    } for p in mine]), hide_index=True, width="stretch")
    open_items = [p for p in mine if p["status"] == SUBMITTED]
    if not open_items:
        return
    with st.container(border=True):
        pick, reason, button = st.columns([2, 2, 1], vertical_alignment="bottom")
        selected = pick.selectbox("Withdraw an open proposal", open_items, key="withdraw_pick", index=None,
                                  placeholder="Pick a proposal …",
                                  format_func=lambda p: f"{p['id']} · {p.get('request') or '–'}"[:80])
        if selected is None:
            return
        reason.text_input("Reason (optional)", key=f"withdraw_reason_{selected['id']}")
        label, icon = ACTIONS["withdraw"]
        with button:
            confirm_button(label, f"withdraw_{selected['id']}", _withdraw_clicked, args=(selected["id"], user_id, role),
                           icon=icon, width="stretch", confirm_label=f"Withdraw {selected['id']}",
                           message=f"Withdraw proposal **{selected['id']}**? The PMO no longer sees it; nothing is "
                                   "written. Submit it again from the report if needed.")


def _grant_clicked(user_id: str, role: str) -> None:
    dates = st.session_state.get("delegation_dates") or ()
    start, end = (tuple(dates) + (None, None))[:2]
    result = views.grant(user_id, role, st.session_state.get("delegation_deputy") or "", start, end,
                         st.session_state.get("delegation_reason", ""))
    notify(result.kind, result.message)


def _revoke_clicked(delegation_id: str, user_id: str, role: str) -> None:
    result = views.revoke(delegation_id, user_id, role)
    notify(result.kind, result.message)


def render_delegations(user_id: str, role: str) -> None:
    """PMO: hand the approval right to a deputy for a period (e.g. vacation) and see all delegations."""
    rules = current_rules()
    st.subheader("Delegation of the approval right")
    st.caption(f"Another PMO decides proposals on your behalf for at most {rules.delegation_max_days} days (only PMO "
               "accounts can deputise – a deputy sees the whole inbox). The four-eyes principle stays: a deputy never "
               "decides their own proposal; yours they decide in their own name. Open proposals are "
               f"escalated after {rules.approval_sla_days} days and expire after "
               f"{rules.approval_expiry_days or 'no'} days (Rules page, set by the PMO Head).")
    with st.form("delegation_form", clear_on_submit=True):
        deputy, dates = st.columns(2)
        candidates = views.deputy_candidates(user_id)
        if candidates:
            deputy.selectbox("Deputy", candidates, key="delegation_deputy")
        else:
            deputy.text_input("Deputy (user id)", key="delegation_deputy")
        today = clock.today()
        dates.date_input("Period", value=(today, today + timedelta(days=6)), min_value=today, key="delegation_dates")
        st.text_input("Reason (required)", key="delegation_reason", placeholder="e.g. vacation")
        st.form_submit_button("Delegate", icon=":material/badge:", type="primary", on_click=_grant_clicked,
                              args=(user_id, role))
    items = list_delegations()
    if not items:
        st.caption("No delegations yet.")
        return
    today = clock.today()
    st.dataframe(pd.DataFrame([{
        "Delegator": d.get("delegator"), "Deputy": d.get("delegate"), "From": d.get("start"), "To": d.get("end"),
        "Status": views.delegation_status(d, today),
        "Reason": d.get("reason") or "–",
    } for d in items]), hide_index=True, width="stretch")
    open_items = views.open_delegations(items, today)
    if open_items:
        pick, button = st.columns([3, 1], vertical_alignment="bottom")
        choice = pick.selectbox("End a delegation early", open_items, key="delegation_revoke_pick",
                                format_func=lambda d: f"{d['delegator']} → {d['delegate']} ({d['start']} – {d['end']})")
        with button:
            confirm_button("End delegation", "delegation_revoke", _revoke_clicked,
                           args=(choice["delegation_id"], user_id, role), icon=":material/event_busy:",
                           width="stretch", message=f"End the delegation {choice['delegator']} → {choice['delegate']} "
                                                    "now? The deputy can no longer decide on your behalf.")
