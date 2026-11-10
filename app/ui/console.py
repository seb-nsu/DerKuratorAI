"""Steering Console: chat and guided scenario builder, the run of a request, history, report viewer and the actions
on a result (save as scenario, apply, submit for approval)."""
from __future__ import annotations

import logging
import uuid

import streamlit as st

from app.analysis_runs import log_analysis_run, plan_outline, run_mode  # noqa: F401 - re-exported for the pages
from app.approval_flow import record_incident
from app.core import clock, cost_tracker
from app.core.business_rules import business_assumptions_md
from app.core.chat_store import append_messages, clear_chat, update_message
from app.core.proposals import (
    APPROVED,
    REJECTED,
    SUBMITTED,
    ProposalError,
    get_proposal,
    requires_override,
    submit_proposal,
)
from app.core.runtime_config import get_runtime_config
from app.core.scenario_store import ScenarioStoreError
from app.core.security import ROLE_PMO, apply_rls_to_dataset, approval_mode, log_security_event
from app.crew.scenarios import (
    COST_TYPE_LABELS,
    CUT_METHOD_LABELS,
    PORTFOLIO_ACTIONS,
    RISK_RESPONSES,
    SCENARIOS,
    STRESS_COST_TYPES,
    build_guided_scenario,
)
from app.crew.shadow import extract_verdict
from app.crew.tools.data_steward_tools import DataQualityReport
from app.data_adapters import PortfolioDataset
from app.data_adapters.base import is_manual_change, net_changes
from app.ui.common import (
    ACTIONS,
    audit,
    badge,
    business_changes,
    load_dataset,
    notify,
    page_header,
    project_names,
    verdict_badge,
)
from app.ui.report import export_menu, render_report
from app.ui.shell import go_to
from app.views import console as views
from app.views.console import (  # noqa: F401 - shared with the API
    EXAMPLE_PROMPTS,
    TARGET_REQUIRED,
    apply_change_set,
    assistant_message,
    example_prompts,
    save_what_if,
    user_message,
)
from app.views.console import min_schedule_months as _min_schedule_months  # noqa: F401 - kept for the tests
from app.views.navigation import scope_text

logger = logging.getLogger("derkurator.ui.console")

# =========================================================================== steering console
def persist_chat(updated: dict | None = None, meta: dict | None = None) -> None:
    """Append-only: new messages are appended; a changed message (e.g. applied) is recorded as an update."""
    identity = st.session_state.get("identity")
    if not identity:
        return
    append_messages(identity[0], identity[1], st.session_state["messages"])
    if updated is not None and meta:
        update_message(identity[0], identity[1], updated.get("id"), meta)


HANDOVER_TEXT = "Nothing is written; the resource change is recorded for manual execution."


def _changes_allowed(changes: list[dict], role: str, user_id: str) -> bool:
    """Server-side RLS re-check of every project in a change set (sets a flash message when denied)."""
    allowed = set(apply_rls_to_dataset(load_dataset(), role, user_id).project_numbers)
    forbidden = sorted({c["project_id"] for c in changes if c["project_id"] not in allowed})
    if forbidden:
        notify("error", f"No permission for: {', '.join(forbidden)}")
    return not forbidden


def _submit_proposal(msg: dict, changes: list[dict], role: str, user_id: str, justification: str = "") -> None:
    meta = msg.setdefault("meta", {})
    if _changes_allowed(changes, role, user_id):
        try:
            record = meta.get("verdict") if isinstance(meta.get("verdict"), dict) else None
            entry = submit_proposal(user_id, role, meta.get("request", ""), str(msg.get("content", "")),
                                    _verdict_label(meta) or extract_verdict(str(msg.get("content", ""))), changes,
                                    forced=meta.get("forced", False), verdict_record=record,
                                    justification=justification)
        except ProposalError as exc:
            notify("error", f"Proposal not submitted: {exc}")
        else:
            meta.update(proposal_id=entry["id"], proposal_status=SUBMITTED)
            audit("proposal_submitted", role=role, user_id=user_id, proposal_id=entry["id"], request=meta.get("request"),
                  changes=changes, run_id=meta.get("run_id"))
            log_security_event("Change proposal submitted", role=role, user=user_id, proposal=entry["id"],
                               changes=len(changes))
            persist_chat(updated=msg, meta={"proposal_id": entry["id"], "proposal_status": SUBMITTED})
            notify("success", f"Proposal {entry['id']} submitted – the PMO will review it in the "
                                                    "approval inbox. You can follow its status under Approvals.")
    st.rerun()


def _save_scenario(idx: int, msg: dict, name: str) -> None:
    meta = msg.get("meta") or {}
    role, user_id = st.session_state.get("identity") or ("", "")
    try:
        entry = save_what_if(name, role, user_id, meta.get("request", ""), meta.get("scenario_state"),
                             meta.get("verdict"), meta.get("proposed_changes") or [], load_dataset().projects)
    except ScenarioStoreError as exc:
        notify("error", f"What-if not saved: {exc}")
    else:
        meta["saved_as"] = entry["name"]
        persist_chat(updated=msg, meta={"saved_as": entry["name"]})
        notify("success", f"What-if '{entry['name']}' saved – compare it under What-if comparison.")
    st.rerun()


def render_save_scenario(idx: int, msg: dict) -> None:
    """'Save as what-if' popover (kept for the A/B comparison on the What-if comparison page)."""
    meta = msg.get("meta") or {}
    if meta.get("saved_as"):
        st.caption(f":material/bookmark: Saved as **{meta['saved_as']}**")
        return
    with st.popover("Save as what-if", icon=":material/bookmark_add:"):
        name = st.text_input("What-if name", key=f"save_name_{idx}", placeholder="e.g. A – hard cut")
        if st.button("Save what-if", icon=":material/bookmark_add:", key=f"save_scenario_{idx}", type="primary",
                     disabled=not name.strip()):
            _save_scenario(idx, msg, name)


def _verdict_label(meta: dict) -> str | None:
    return views.verdict_label(meta.get("verdict"))


def _apply_now(idx: int, msg: dict, changes: list[dict], current_role: str, current_user: str,
               override_reason: str = "") -> None:
    meta = msg.setdefault("meta", {})
    overridden = requires_override(_verdict_label(meta))
    if overridden and not override_reason.strip():  # defence in depth: the dialog already requires it
        notify("error", "A NO-GO / incomplete verdict is applied only with an explicit override "
                                              "and a reason – nothing was written.")
        st.rerun()
    st.session_state.pop(f"approve_pending_{idx}", None)
    if not _changes_allowed(changes, current_role, current_user):
        st.rerun()
    outcome = apply_change_set(changes, current_role, current_user, request=meta.get("request"),
                               verdict=meta.get("verdict"), forced=meta.get("forced", False), run_id=meta.get("run_id"),
                               override_reason=override_reason)
    if not outcome.done:
        notify("error", outcome.message)
        st.rerun()
    meta[outcome.closed_as] = True
    meta["applied_at"] = outcome.applied_at
    if outcome.override:
        meta["override"] = True
    if outcome.kind != "success":  # written, but the record behind it failed
        notify("warning", outcome.message)
        st.rerun()
    load_dataset.clear()
    notify("success", outcome.message)
    persist_chat(updated=msg, meta={outcome.closed_as: True, "applied_at": meta["applied_at"],
                                    **({"override": True} if outcome.override else {})})
    st.rerun()


def _confirm_apply(idx: int, msg: dict, changes: list[dict], mode: str, role: str, user_id: str) -> None:
    """Writing to the data source is irreversible for the user, so it requires explicit confirmation in a dialog."""
    pending_key = f"approve_pending_{idx}"
    manual = all(is_manual_change(c) for c in changes)

    @st.dialog(f"Confirm: {ACTIONS['handover' if manual else 'apply'][0].lower()}",
               on_dismiss=lambda: st.session_state.pop(pending_key, None))
    def dialog() -> None:
        if manual:
            st.info(HANDOVER_TEXT, icon=":material/assignment_turned_in:")
        else:
            st.warning(f"Write **{len(changes)} change(s)** to the portfolio data source now? A snapshot is taken "
                       "first; the System Admin can restore it.", icon=":material/warning:")
        st.dataframe(business_changes(changes), hide_index=True, width="stretch")
        override = override_controls(f"console_{idx}", _verdict_label(msg.get("meta") or {}))
        ready = override is None or (override[0] and bool(override[1].strip()))
        yes, no = st.columns(2)
        if no.button("Cancel", key=f"approve_cancel_{idx}", width="stretch"):
            st.session_state.pop(pending_key, None)
            st.rerun()
        label, icon = ACTIONS["handover" if manual else "apply"]
        if yes.button(f"Confirm: {label.lower()}", icon=icon,
                      key=f"approve_confirm_{idx}", type="primary", disabled=mode != "apply" or not ready,
                      width="stretch"):
            _apply_now(idx, msg, changes, role, user_id, override_reason=override[1] if override else "")

    dialog()


_TONE_COLOURS = {"success": "green", "warning": "orange", "danger": "red", "info": "blue", "neutral": "gray"}


def run_status(meta: dict, is_last: bool) -> tuple[str, str, str]:
    """(label, badge colour, icon) of an analysis: applied / handed over / proposal status / outdated / draft."""
    shown = views.run_badge(meta, views.run_state(meta, is_last))
    return shown["label"], _TONE_COLOURS[shown["tone"]], f":material/{shown['icon']}:"


def render_console_actions(idx: int, msg: dict, user_msg: dict | None, ds: PortfolioDataset, is_last: bool) -> None:
    """Action bar of the selected report: save, export and the one primary action (propose or approve & apply)."""
    meta = msg.get("meta") or {}
    changes = net_changes(meta.get("proposed_changes"))  # one change per field: the net effect of all steps
    proposal = get_proposal(meta["proposal_id"]) if meta.get("proposal_id") else None
    secondary, primary = st.columns(2)
    with secondary:
        export_menu(str(idx), msg, user_msg, ds)
        decided = bool(proposal and proposal.get("status") in (APPROVED, REJECTED))
        if changes and is_last and meta.get("scenario_state") and not (meta.get("applied") or meta.get("handed_over")
                                                                       or decided):
            render_save_scenario(idx, msg)  # keep this scenario for the A/B comparison
        elif meta.get("saved_as"):
            st.caption(f":material/bookmark: Saved as **{meta['saved_as']}**")
    with primary:
        if not changes or meta.get("applied") or meta.get("handed_over"):
            return
        if meta.get("proposal_id"):
            if proposal and proposal.get("comment"):
                st.caption(f"PMO comment: {proposal['comment']}")
            return
        if not is_last:
            st.caption("Outdated step – only the latest step of this what-if can be approved.")
            return
        current_role, current_user = st.session_state["identity"]
        mode = approval_mode(current_role, changes)
        if mode == "propose":  # four-eyes principle: governed changes go to the PMO inbox – also those of a PMO
            verdict = _verdict_label(meta)
            label, icon = ACTIONS["submit"]
            if requires_override(verdict):  # a NO-GO is no recommendation to submit: secondary, with a reason
                reason = st.text_input(f"Why submit despite {verdict}? (required)", key=f"propose_reason_{idx}",
                                       placeholder="e.g. Board asked for the cut anyway; risks accepted")
                if st.button(f"{label} anyway", icon=icon, key=f"propose_{idx}", width="stretch",
                             disabled=not reason.strip(),
                             help=None if reason.strip() else "Give the reason first – the PMO sees it with the proposal."):
                    _submit_proposal(msg, changes, current_role, current_user, justification=reason)
            elif st.button(label, icon=icon, key=f"propose_{idx}", type="primary", width="stretch"):
                _submit_proposal(msg, changes, current_role, current_user)
            st.caption(":material/lock: Budget, schedule, phase and stop changes need the approval of a PMO other than "
                       "the requester (four-eyes principle).")
            return
        pending_key = f"approve_pending_{idx}"
        manual = all(is_manual_change(c) for c in changes)
        label, icon = ACTIONS["handover" if manual else "apply"]
        if st.button(f"{label} …", icon=icon, key=f"approve_{idx}", type="primary", disabled=mode != "apply",
                     width="stretch",
                     help="Nothing is written – the change is recorded for manual execution." if manual
                     else "Opens a confirmation with the change set." if mode == "apply"
                     else "Your role can only submit proposals for this change."):
            st.session_state[pending_key] = True
        if st.session_state.get(pending_key):
            _confirm_apply(idx, msg, changes, mode, current_role, current_user)


def previous_verdict_blocks() -> str | None:
    """'NO-GO' / 'INCOMPLETE' if the latest result was rejected: the next step then starts from the original data by
    default (a NO-GO step is rarely the base of the next idea, e.g. −20 % NO-GO, then −10 % on its own)."""
    return views.previous_verdict_blocks(st.session_state.get("messages", []))


def last_scenario_state() -> dict | None:
    """Scenario memory: the cumulative state of the latest assistant message that can still be continued
    (not applied, not submitted as a proposal)."""
    return views.last_scenario_state(st.session_state.get("messages", []))


def run_request(request: str, display: str, ds: PortfolioDataset, full_ds: PortfolioDataset, role: str, user_id: str,
                plan=None, force: bool = False, base_state: dict | None = None) -> None:
    """Shared execution path for free text and guided scenarios. Verdict first (report header), the decision paper
    streams below it. `base_state`: continue the previous scenario – the request builds on the manipulated data."""
    messages = st.session_state["messages"]
    stamp = clock.now().strftime("%Y-%m-%d %H:%M")
    messages.append(user_message(display, base_state, force, stamp))
    if force:
        audit("forced_simulation", role=role, user_id=user_id, request=request)
        log_security_event("Forced simulation requested (data quality gate overridden)", level=logging.WARNING,
                           role=role, user=user_id)
    # FinOps: usage is collected per request via a ContextVar – never as a delta of the process counter
    with cost_tracker.track_request() as usage, st.status("Analysing the what-if ...", expanded=True) as status:
        try:
            from app.services import run_steering_pipeline

            st.write("Checking data quality (deterministic rules) ...")
            st.write("Calculating the change · checking dependencies · writing the decision paper ...")
            preview_box, paper_box = st.container(), st.empty()

            def show_verdict(preview: dict) -> None:  # verdict first – the paper follows
                card = {"role": "assistant", "content": "", "meta": {
                    "request": request, "mode": preview["mode"], "forced": force,
                    "verdict": preview["verdict"].to_dict(), "proposed_changes": preview["proposed_changes"]}}
                with preview_box:
                    render_report("preview", card, ds, eyebrow=f"Decision report · {stamp} · {user_id}", preview=True)
                status.update(label="Verdict ready – writing the decision paper ...")

            # never log the prompt text (may contain names) – metadata only
            logger.info("UI | Steering request started | role=%s, guided=%s, forced=%s, chars=%d", role, plan is not None, force, len(request))
            result = run_steering_pipeline(request, ds, role, user_id, pii_source=full_ds, plan=plan, force=force,
                                           base_state=base_state, on_verdict=show_verdict,
                                           on_paper=paper_box.markdown)
            logger.info("UI | Steering request finished | mode=%s, duration=%.1fs, changes=%d",
                        result.mode, result.duration_s, len(result.proposed_changes))
            status.update(label="Analysis complete", state="complete", expanded=False)
            run_id = uuid.uuid4().hex
            messages.append(assistant_message(request, result, run_id, force, base_state))
            log_analysis_run(role, result, run_id, user_id)
        except Exception:  # details go to the system log under an incident ID
            incident = uuid.uuid4().hex[:8].upper()
            logger.exception("Pipeline failed (incident %s)", incident)
            record_incident("analysis_failed", incident, role=role, user_id=user_id)
            status.update(label="Analysis failed", state="error")
            messages.append({"role": "assistant", "meta": {}, "ts": clock.now().strftime("%Y-%m-%d %H:%M"),
                             "content": f"The analysis could not be completed due to a system error (Incident ID: "
                                        f"**{incident}**). Please contact your System Admin."})
    # only this request's own calls; the background shadow crew runs in a fresh context (process total only)
    spent = usage.as_dict()
    session = st.session_state.setdefault("session_costs", {"calls": 0, "total_tokens": 0, "cost_usd": 0.0})
    for key in session:
        session[key] += spent[key]
    if messages[-1]["role"] == "assistant" and messages[-1].get("meta") is not None and spent["total_tokens"]:
        messages[-1]["meta"].update(cost_usd=spent["cost_usd"], tokens=spent["total_tokens"])
    st.session_state["selected_run"] = len(messages) - 1  # the new report opens in the viewer
    persist_chat()
    st.rerun()


def _pmo_builder_inputs(action: str, ds: PortfolioDataset, project: str | None, params: dict, cols) -> None:
    """Inputs of the PMO scenarios (resume, stress test, re-baseline, stage gate, reserve, skills, risk response)."""
    p1, p2, p3, p4 = cols
    current = views.project_row(ds, project)
    if action == "resume":
        if str(current.get("state")) != "On Hold":
            p1.info("This project is not on hold. Resume works for a paused project – or after a hold simulated "
                    "earlier in this what-if (\"Builds on the previous step\").", icon=":material/info:")
        restart = p2.date_input("Restart date", value=None, key="sb_restart", help="Empty = today.")
        params["restart_date"] = restart.isoformat() if restart else None
        fte = p3.number_input("Returning FTE (0 = the team before the hold)", min_value=0.0, value=0.0, step=0.5,
                              key="sb_resume_fte")
        params["fte"] = fte or None
    elif action == "cost_stress":
        params["pct"] = p1.number_input("Cost increase (%)", min_value=0.0, value=8.0, step=1.0, key="sb_stress_pct")
        params["cost_type"] = p2.radio("Applies to", list(STRESS_COST_TYPES), format_func=STRESS_COST_TYPES.get,
                                       key="sb_stress_type", horizontal=True)
        scopes = views.stress_scopes(ds)
        params["scope"] = p3.selectbox("Scope (optional)", ["", *scopes], format_func=lambda s: s or "whole scope",
                                       key="sb_stress_scope")
    elif action == "rebaseline":
        params["new_budget_eur"] = p1.number_input("New budget (€, 0 = unchanged)", min_value=0.0, value=0.0,
                                                   step=50_000.0, key="sb_rb_budget")
        end = p2.date_input("New end date (empty = unchanged)", value=None, key="sb_rb_end")
        params["new_end_date"] = end.isoformat() if end else None
        params["reason"] = p3.text_input("Reason (mandatory)", key="sb_rb_reason")
    elif action == "stage_gate":
        phase, following = views.gate_phases(ds, project)
        p1.caption(f"Current phase: {phase or 'unknown'}")
        params["target_phase"] = p2.selectbox("Target phase", following or [""], key="sb_gate_phase",
                                              format_func=lambda x: x or "next phase")
    elif action == "reserve_draw":
        params["direction"] = p1.radio("Direction", ["draw", "return"], key="sb_res_dir", horizontal=True,
                                       format_func=lambda d: "Draw" if d == "draw" else "Return")
        params["amount"] = p2.number_input("Amount (€)", min_value=0.0, value=200_000.0, step=10_000.0,
                                           key="sb_res_amount")
        params["reason"] = p3.text_input("Reason", key="sb_res_reason")
    elif action == "skill_transfer":
        skills = views.skill_names(ds)
        params["skill"] = p1.selectbox("Skill", skills or [""], key="sb_skill")
        params["fte"] = p2.number_input("FTE", min_value=0.0, value=1.0, step=0.5, key="sb_skill_fte")
        params["months"] = p3.slider("Months", 1, 12, 3, key="sb_skill_months")
    elif action == "merge":
        p1.caption("The target takes over budget, actuals, forecast, benefit, plans, risks and dependencies of the "
                   "source project; the source is closed. Needs PMO approval.")
    elif action == "benefit_review":
        p1.caption("Compares the benefit tranches already due with the actual benefits reported – writes nothing. "
                   "Actuals are reported in the project's Benefits tab.")
    elif action == "risk_response":
        labels = views.risk_labels(ds, project)
        params["risk_number"] = p1.selectbox("Risk", list(labels) or [""], format_func=lambda k: labels.get(k, "no risks"),
                                             key="sb_risk")
        params["response"] = p2.selectbox("Response", RISK_RESPONSES, key="sb_risk_response")
        params["cost_eur"] = p3.number_input("Cost (€)", min_value=0.0, value=0.0, step=5_000.0, key="sb_risk_cost")
        params["funding"] = p4.radio("Funded from", ["project", "reserve"], key="sb_risk_funding", horizontal=True,
                                     format_func=lambda f: "Project budget" if f == "project" else "Reserve")


def render_scenario_builder(ds: PortfolioDataset, steward: DataQualityReport):
    """Guided composer: pick a scenario, set parameters, trigger the simulator directly."""
    names = project_names(ds)
    blocked = set(steward.blocked_project_ids)
    ids = sorted(names)
    fmt = lambda pid: f"{pid} – {names.get(pid, pid)}{' (blocked)' if pid in blocked else ''}"  # noqa: E731
    if st.session_state.get("sb_project") not in (None, *ids):  # e.g. prefilled for a project outside this scope
        st.session_state.pop("sb_project")

    c1, c2, c3 = st.columns([1.1, 1.4, 1.4])
    action = c1.selectbox("Scenario type", list(SCENARIOS), format_func=SCENARIOS.get, key="sb_action")
    source_label = "Source project" if action in TARGET_REQUIRED else "Project"
    if action in PORTFOLIO_ACTIONS:  # across the whole scope – no single project
        project = None
        c2.caption("Across all projects in your scope – the tool ranks candidates by priority, value score and "
                   "risk of not performing." if action == "portfolio_cut" else
                   "Across all open projects in your scope – nothing is written, the result ranks the projects by "
                   "their forecast overrun after the increase.")
    else:
        project = c2.selectbox(source_label, ids, format_func=fmt, key="sb_project")
        if project and len(fmt(project)) > 36:  # the box cuts long names: the full name below it
            c2.caption(names.get(project, project))
    target, params = None, {}
    if action in TARGET_REQUIRED:
        others = [p for p in ids if p != project]
        target = c3.selectbox("Target project", others, format_func=fmt, key="sb_target") if others else None
    elif action == "cancel":
        others = ["—", *[p for p in ids if p != project]]
        choice = c3.selectbox("Transfer free budget to (optional)", others,
                              format_func=lambda p: "— no transfer —" if p == "—" else fmt(p), key="sb_cancel_target")
        target = None if choice == "—" else choice

    p1, p2, p3, p4 = st.columns(4)
    if action == "budget_change":
        params["cost_type"] = p1.radio("Budget type", list(COST_TYPE_LABELS), format_func=COST_TYPE_LABELS.get, key="sb_cost_type", horizontal=True)
        params["direction"] = p2.radio("Direction", ["decrease", "increase"], key="sb_direction", horizontal=True,
                                       format_func=lambda d: "Cut" if d == "decrease" else "Increase")
        params["is_perc"] = p3.radio("Unit", ["%", "€"], key="sb_unit", horizontal=True) == "%"
        params["value"] = p4.number_input("Value", min_value=0.0, value=10.0 if params["is_perc"] else 50000.0,
                                          step=5.0 if params["is_perc"] else 10000.0, key=f"sb_value_{params['is_perc']}")
    elif action == "budget_transfer":
        params["amount"] = p1.number_input("Amount (€, 0 = entire free budget)", min_value=0.0, value=100000.0, step=10000.0, key="sb_amount")
        params["cost_type"] = p2.radio("Budget type", list(COST_TYPE_LABELS), format_func=COST_TYPE_LABELS.get, key="sb_transfer_cost_type", horizontal=True)
    elif action == "resource_transfer":
        params["fte"] = p1.number_input("FTE", min_value=0.0, value=1.0, step=0.5, key="sb_fte")
        teams = views.project_teams(ds, project)
        params["team"] = p2.selectbox("Team (optional)", ["", *teams], format_func=lambda t: t or "all teams", key="sb_team")
    elif action == "schedule":
        min_months = views.min_schedule_months(ds, project)
        if st.session_state.get("sb_months", 0) < min_months:
            st.session_state["sb_months"] = min_months
        params["months_delta"] = p1.slider("Schedule (months)", min_months,
                                           get_runtime_config().business_rules.max_timeline_shift_months, 0, key="sb_months",
                                           help="Acceleration is limited by the remaining duration of the project.")
        params["scope_change_pct"] = p2.slider("Scope (%)", -50, 100, 0, step=5, key="sb_scope")
        params["capacity_change_pct"] = p3.slider("Capacity (%)", -50, 50, 0, step=5, key="sb_capacity")
    elif action == "cancel":
        p1.caption("Calculates sunk costs, CapEx impairment risk, released budget, FTE and task domino.")
    elif action == "on_hold":
        rules = get_runtime_config().business_rules
        params["months"] = p1.slider("Hold duration (months)", 1, rules.max_hold_months, 3, key="sb_hold_months")
        params["retention_pct"] = p2.slider("Capacity kept on standby (%)", 0, 100, int(rules.hold_retention_pct), step=5,
                                            key="sb_hold_retention")
        p3.caption(f"No exit costs; restart ramp-up {rules.hold_restart_ramp_months:g} month(s). Successor deadlines and "
                   "NPV are checked.")
    elif action == "portfolio_cut":
        params["target_eur"] = p1.number_input("Savings target (€)", min_value=0.0, value=2_000_000.0, step=100_000.0,
                                               key="sb_cut_target")
        params["method"] = p2.radio("Method", list(CUT_METHOD_LABELS), format_func=CUT_METHOD_LABELS.get,
                                    key="sb_cut_method", horizontal=True,
                                    help="Priority ranking: fewest stops, lowest priority first. Least strategic "
                                         "value lost: the stops with the lowest strategy score that reach the target "
                                         "(optimised), compared with the ranked list.")
        p3.caption("Proposes complete project stops (cut list) until the net budget release reaches the target.")
    else:
        _pmo_builder_inputs(action, ds, project, params, (p1, p2, p3, p4))

    run = st.button("Simulate", icon=":material/play_arrow:", type="primary", key="sb_run", disabled=not ids,
                    help=None if ids else "No project in your scope for this scenario type.")
    if project in blocked and action in ("cancel", "on_hold"):
        st.info("The Data Steward blocks this project's master data – a stop or a hold is still calculated, with "
                "indicative figures and a condition to confirm them.", icon=":material/info:")
    elif project in blocked and not st.session_state.get("sb_force"):
        st.info("This project is blocked for quantitative simulation by the Data Steward – the result shows the "
                "quality gate finding and the graph analysis."
                + (" The PMO can force the simulation (Options & rules)." if ids else ""), icon=":material/block:")
    if not run:
        return None
    try:
        return build_guided_scenario(action, project, names, target=target, **params)
    except ValueError as exc:
        st.warning(str(exc))
        return None


def _runs(messages: list[dict]) -> list[tuple[int, dict | None, dict]]:
    """(index, user message, assistant message) per analysis, oldest first."""
    out = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            user = messages[i - 1] if i > 0 and messages[i - 1].get("role") == "user" else None
            out.append((i, user, m))
    return out


def _request_text(user: dict | None, msg: dict) -> str:
    text = str((msg.get("meta") or {}).get("request") or (user or {}).get("content") or "Analysis")
    return text.replace("**Scenario:** ", "")


def render_history(runs: list[tuple[int, dict | None, dict]], selected: int, ds: PortfolioDataset) -> None:
    """Left rail: every analysis with verdict and status; each one opens in the viewer (including its graph)."""
    last_idx = runs[-1][0] if runs else -1
    with st.container(border=True):
        st.markdown("**Analyses**")
        if not runs:
            st.caption("No analyses yet.")
        for idx, user, msg in reversed(runs):
            meta = msg.get("meta") or {}
            label, colour, icon = run_status(meta, idx == last_idx)
            turns = len((meta.get("scenario_state") or {}).get("turns") or [])
            step = f"Step {turns} · " if meta.get("continued") and turns else ""
            with st.container(border=True):
                verdict = (meta.get("verdict") or {}).get("label") if isinstance(meta.get("verdict"), dict) else None
                st.markdown((verdict_badge(verdict) if verdict else badge("Analysis", "blue", ":material/insights:"))
                            + f"  \n{step}{_request_text(user, msg)}")
                st.caption(" · ".join(x for x in (msg.get("ts", ""), badge(label, colour, icon)) if x))
                if st.button("Viewing" if idx == selected else "Open", key=f"run_open_{idx}", disabled=idx == selected,
                             width="stretch", type="tertiary"):
                    st.session_state["selected_run"] = idx
                    st.rerun()
        if runs:
            render_history_exports(ds)


def render_history_exports(ds: PortfolioDataset | None) -> None:
    """'Clear history' under the rail (each report exports itself in the viewer's export menu)."""
    role, user_id = st.session_state.get("identity") or ("-", "-")
    if st.button("Clear history", icon=":material/delete:", key="clear_chat", width="stretch", type="tertiary"):
        st.session_state["clear_chat_pending"] = True  # deletes the persisted history, so confirm first
    if st.session_state.get("clear_chat_pending"):
        @st.dialog("Clear analysis history", on_dismiss=lambda: st.session_state.pop("clear_chat_pending", None))
        def confirm() -> None:
            st.warning("Delete the complete history including all unsaved what-ifs? This cannot be undone.",
                       icon=":material/warning:")
            yes, no = st.columns(2)
            if yes.button("Yes, clear history", type="primary", key="clear_chat_confirm", width="stretch"):
                st.session_state.pop("clear_chat_pending", None)
                st.session_state["messages"] = []
                st.session_state.pop("selected_run", None)
                clear_chat(role, user_id)
                st.rerun()
            if no.button("Cancel", key="clear_chat_cancel", width="stretch"):
                st.session_state.pop("clear_chat_pending", None)
                st.rerun()

        confirm()


def render_viewer(runs: list[tuple[int, dict | None, dict]], selected: int, ds: PortfolioDataset) -> None:
    last_idx = runs[-1][0]
    idx, user, msg = next(r for r in runs if r[0] == selected)
    meta = msg.get("meta") or {}
    if not meta.get("mode"):  # e.g. a system-error message with an incident ID
        with st.container(border=True):
            st.markdown(msg.get("content", ""))
        return
    label, colour, icon = run_status(meta, idx == last_idx)
    turns = len((meta.get("scenario_state") or {}).get("turns") or [])
    identity = st.session_state.get("identity") or ("", "")
    eyebrow = " · ".join(x for x in ("Decision report", msg.get("ts", ""), identity[1], get_runtime_config().environment,
                                     f"step {turns}" if meta.get("continued") and turns else "",
                                     "forced simulation" if meta.get("forced") else "") if x)
    with st.container(key="report_viewer"):
        render_report(str(idx), msg, ds, request=_request_text(user, msg), eyebrow=eyebrow,
                      status_md=badge(label, colour, icon),
                      actions=lambda: render_console_actions(idx, msg, user, ds, idx == last_idx))


def _composer(ds: PortfolioDataset, steward: DataQualityReport, role: str):
    """Guided scenario builder or free text, plus scenario memory and (PMO) force mode. Returns
    (scenario, prompt, force, base_state)."""
    with st.container(border=True):
        top = st.columns([1.2, 2.2, 1.6], vertical_alignment="center")
        mode = top[0].segmented_control("Input", ["Guided", "Free text"], default="Guided", key="composer_mode",
                                        label_visibility="collapsed", required=True)
        # Scenario memory: "... and now also cut project B by 5 %" builds on the previous result
        previous, base_state = last_scenario_state(), None
        with top[1]:
            if previous:
                turns = previous.get("turns") or []
                rejected = previous_verdict_blocks()
                step_id = len(st.session_state.get("messages", []))
                if st.session_state.get("sb_continue_step") != step_id:  # a new result -> fresh default for the toggle
                    st.session_state["sb_continue_step"] = step_id
                    st.session_state["sb_continue"] = False  # building on a step is an explicit choice
                if st.toggle(f"Builds on the previous step (step {len(turns) + 1})", key="sb_continue",
                             help="The next request builds on the already changed data of this what-if so far: "
                                  + " → ".join(turns[-3:])):
                    base_state = previous
                    st.caption("Building on: " + " → ".join(turns[-3:]))
                elif rejected:
                    st.caption(f"Last step was {rejected} – starting from the original data.")
                else:
                    st.caption("Starting from the original data.")
            else:
                st.caption("New what-if · nothing to build on yet")
        # force mode only matters while the Data Steward blocks a project – then it sits with the rules, not the inputs
        can_force = role == ROLE_PMO and bool(steward.blocked_project_ids)
        with top[2]:
            with st.popover("Options & rules" if can_force else "Rules in use", icon=":material/rule:", width="stretch"):
                if can_force:
                    st.toggle("Force simulation (ignore data warnings)", key="sb_force",
                              help="PMO only: the Data Steward still reports all findings, but blocked projects are "
                                   "simulated anyway (indicative results). Every forced run is audited.")
                    st.divider()
                st.markdown(business_assumptions_md())
                st.caption("Set by the PMO Head on the Rules page; the GO / CONDITIONAL GO / NO-GO verdict is computed "
                           "from these rules – never by the AI.")
        force = can_force and bool(st.session_state.get("sb_force"))
        if force:
            st.warning("Force mode active – quality-gate blocks are ignored; results for blocked projects are "
                       "indicative only.", icon=":material/warning:")
        scenario, prompt = None, st.session_state.pop("pending_prompt", None)
        if mode == "Free text":
            prompt = st.chat_input("Describe a change or ask a what-if …", disabled=ds.projects.empty) or prompt
        else:
            scenario = render_scenario_builder(ds, steward)
    return scenario, prompt, force, base_state


def page_console(ctx: dict) -> None:
    ds, full_ds, role, user_id, steward = ctx["ds"], ctx["full_ds"], ctx["role"], ctx["user_id"], ctx["steward"]
    head, link = st.columns([4, 1], vertical_alignment="bottom")
    with head:
        page_header("Steering Console", f"Scope: {scope_text(role, len(ds.projects), len(full_ds.projects))} · the "
                                        "verdict is rule-based; the AI writes the decision paper")
    if link.button("Compare saved what-ifs", icon=":material/compare_arrows:", key="console_compare", type="tertiary"):
        go_to("scenarios")
    messages = st.session_state["messages"]
    runs = _runs(messages)
    selected = st.session_state.get("selected_run")
    if selected not in [r[0] for r in runs]:
        selected = runs[-1][0] if runs else -1
        st.session_state["selected_run"] = selected
    rail, main = st.columns([1, 3.2])
    with main:
        scenario, prompt, force, base_state = _composer(ds, steward, role)
        if scenario is not None:
            run_request(scenario.request, f"**Scenario:** {scenario.request}", ds, full_ds, role, user_id,
                        plan=scenario.plan, force=force, base_state=base_state)
        elif prompt:
            run_request(prompt, prompt, ds, full_ds, role, user_id, force=force, base_state=base_state)
        if runs:
            render_viewer(runs, selected, ds)
        else:
            with st.container(border=True):
                st.markdown("**No analyses yet.** Use the composer above or start with an example:")
                for i, example in enumerate(example_prompts(ds)):
                    st.button(example, key=f"example_{i}", icon=":material/arrow_forward:", type="tertiary",
                              on_click=lambda text=example: st.session_state.update(pending_prompt=text))
    with rail:
        render_history(runs, selected, ds)


def override_controls(key: str, verdict: str | None) -> tuple[bool, str] | None:
    """Explicit override of a NO-GO / incomplete verdict: checkbox + mandatory reason. None = no override needed."""
    if not requires_override(verdict):
        return None
    st.error(f"The rule-based verdict is **{verdict}**. Approving means overriding it – the override and your reason "
             "are recorded in the audit trail.", icon=":material/gpp_maybe:")
    confirmed = st.checkbox(f"Override the {verdict} verdict", key=f"override_{key}")
    reason = st.text_area("Reason for the override (required)", key=f"override_reason_{key}", height=80)
    return confirmed, reason
