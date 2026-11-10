"""System Admin console: environment staging, per-agent model/shadow matrix, model registry, required fields,
database management, audit/shadow/system logs and in-app system tests."""
from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from app.core import cost_tracker, journal, logs
from app.core.business_rules import RULE_LABELS, BusinessRules
from app.core.config import ENVIRONMENTS, endpoint_rows, get_settings
from app.core.env_config import AGENTS, MCP_SERVERS, config_issue, env_config_path, load_mcp_config
from app.core.formatting import field_label, timestamp
from app.core.model_registry import is_thinking_model, model_ids, registry_path
from app.core.required_fields import LOCKED_FIELDS, load_required_fields
from app.core.runtime_config import AGENT_ROLES, get_runtime_config
from app.core.snapshots import MAX_SNAPSHOTS, snapshot_dir
from app.core.system_tests import SystemTestsBusy, junit_path, parse_junit
from app.crew.tools.data_steward_tools import DQ_PROFILE_TAGS
from app.crew.verdict import verdict_rules_md
from app.data_adapters import DataSourceError, get_data_provider
from app.graph.portfolio_graph import current_metadata
from app.ui.boundary import error_boundary
from app.ui.common import (
    confirm_button,
    display_path,
    get_tracing_status,
    graph_status,
    load_dataset,
    notify,
    page_header,
)
from app.ui.shell import is_pmo_head
from app.views import admin as views
from app.views.admin import AGENT_LABELS, NO_SHADOW, key_hint

logger = logging.getLogger("derkurator.ui.admin")


def _flash(outcome: views.Outcome) -> None:
    st.session_state["flash"] = (outcome.kind, outcome.message)


def _clear_data_caches() -> None:
    load_dataset.clear()
    graph_status.clear()


def _user() -> str | None:
    return st.session_state.get("user_id")


def _role() -> str:
    """Session role for the core role checks ("" without a signed-in role is refused there)."""
    return str(st.session_state.get("role") or "")


# =========================================================================== state sync
def sync_admin_state_from_config() -> None:
    """Mirrors the process-wide runtime config into the admin widgets' session state (before widgets render)."""
    cfg = get_runtime_config()
    st.session_state["admin_temperature"] = cfg.temperature
    st.session_state["admin_pii_enabled"] = cfg.pii_masking_enabled
    st.session_state["admin_orchestrator_prompt"] = cfg.orchestrator_prompt
    st.session_state["admin_environment"] = cfg.environment
    for key in ("admin_openai_key", "admin_anthropic_key", "admin_google_key"):
        st.session_state.setdefault(key, "")


def _matrix_rows() -> list[dict[str, str]]:
    return views.matrix_rows()


def _matrix_key() -> str:
    cfg = get_runtime_config()
    return f"model_matrix_{cfg.environment}_{abs(hash(cfg.agent_models)) % 10**8}"


# =========================================================================== callbacks
def save_admin_settings(matrix_key: str) -> None:
    """Form callback: validates, applies, persists to the active environment file and audits (without secrets)."""
    ss = st.session_state
    rows = _matrix_rows()
    for idx, edits in (ss.get(matrix_key) or {}).get("edited_rows", {}).items():
        rows[int(idx)].update({k: v for k, v in edits.items() if k in ("main", "shadow")})
    agents = {a: {"main": r["main"], "shadow": "" if r["shadow"] in (None, NO_SHADOW) else r["shadow"]}
              for a, r in zip(AGENTS, rows, strict=True)}
    changes = {"agents": agents, "temperature": ss["admin_temperature"], "pii_masking_enabled": ss["admin_pii_enabled"],
               "orchestrator_prompt": ss["admin_orchestrator_prompt"]}
    for provider in ("openai", "anthropic", "google"):  # empty field keeps the stored key (never sent to the browser)
        if ss[f"admin_{provider}_key"].strip():
            changes[f"{provider}_api_key"] = ss[f"admin_{provider}_key"].strip()
    outcome = views.save_settings(changes, _user(), _role())
    _flash(outcome)
    if outcome.ok:
        for provider in ("openai", "anthropic", "google"):
            ss[f"admin_{provider}_key"] = ""


def reset_admin_settings() -> None:
    outcome = views.reload_settings(_user())
    for provider in ("openai", "anthropic", "google"):
        st.session_state[f"admin_{provider}_key"] = ""
    _flash(outcome)


def switch_environment_clicked() -> None:
    outcome = views.switch_env(st.session_state["admin_env_select"], _user(), _role())
    if outcome is None:
        return
    if outcome.ok:
        load_dataset.clear()
    _flash(outcome)


def promote_clicked(source: str, target: str) -> None:
    outcome = views.promote(source, target, _user(), _role())
    if outcome.data.get("reloaded"):
        st.session_state.pop("mcp_loaded_env", None)
    _flash(outcome)


def save_required_fields_clicked() -> None:
    outcome = views.save_required(st.session_state["admin_required_fields"], _user(), _role(), is_pmo_head())
    if outcome.ok:
        load_dataset.clear()
    _flash(outcome)


def reset_required_fields_clicked() -> None:
    outcome = views.reset_required(_user(), _role(), is_pmo_head())
    if outcome.ok:
        load_dataset.clear()
    _flash(outcome)


# =========================================================================== console
def render_admin_dashboard() -> None:
    page_header("System Admin Console", "System-wide AI settings and logs. Non-secret settings live in "
                "data/env_<environment>.json; API keys come from .env and are kept in memory only.")
    issue = config_issue(get_runtime_config().environment)
    if issue:
        st.error(f"**Environment config not applied – running on defaults.** {issue}. Fix the file or save the "
                 "settings below to write a valid configuration.")
    # business tasks first, developer tools last; business rules and required fields are the PMO Head's (Rules page)
    areas = st.tabs(["System status", "Operations", "Logs", "Developer"])
    layout = (
        [("system status", render_system_status)],
        [("Snapshots", "snapshots", render_snapshots),
         ("Database", "database management", render_database_management),
         ("Notifications", "notifications", render_notifications),
         ("Environment staging", "environment staging", render_environment_staging)],
        [("Analysis runs", "analysis runs", render_analysis_runs), ("Audit logs", "audit logs", render_audit_logs),
         ("Incidents", "incidents", render_incidents), ("System logs", "system logs", render_system_logs)],
        [("Models, prompt & shadowing", "model settings", render_model_settings_area),
         ("Integrations (MCP)", "MCP integrations", render_mcp_integrations),
         ("FinOps", "FinOps", render_finops), ("Shadow logs", "shadow logs", render_shadow_logs),
         ("System tests", "system tests", render_system_tests)],
    )
    for area, sections in zip(areas, layout, strict=True):
        with area:
            if len(sections) == 1:
                section, render = sections[0]
                with error_boundary(section):  # a broken section never hides the others
                    render()
                continue
            for tab, (_label, section, render) in zip(st.tabs([label for label, _, _ in sections]), sections, strict=True):
                with tab, error_boundary(section):
                    render()


def _render_llm_calls() -> None:
    """The own LLM call log: per agent and model, from the calls of this process (no prompt or answer text)."""
    from app.observability import llm_calls

    st.markdown(f"**LLM calls** · last {llm_calls.RECENT} calls of this process (log: "
                f"`{display_path(logs.LLM_CALLS_LOG)}`, no prompt or answer text)")
    rows = llm_calls.summary()
    if not rows:
        st.caption("No LLM call yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with st.expander("Last calls"):
        st.dataframe(pd.DataFrame(llm_calls.recent(50)), hide_index=True, width="stretch")

def render_system_status() -> None:
    """Technical status: environment, data source, PII masking, LLM routing, graph database and tracing."""
    settings = get_settings()
    cfg = get_runtime_config()
    ok, graph_msg = graph_status()
    tracing = get_tracing_status()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Environment", cfg.environment, help=f"Data source: {settings.data_source}", border=True)
    c1.caption(f"Data source: **{settings.data_source}**")
    c2.metric("PII masking", "On" if cfg.pii_masking_enabled else "OFF", border=True)
    c3.metric("LLM agents", "Active" if cfg.llm_available else "Offline", help="Offline = deterministic rule-based parser",
              border=True)
    c4.metric("Dependency graph", "Ready" if ok else "Unavailable", help=graph_msg, border=True)
    c5.metric("OpenTelemetry export", "Active" if tracing.enabled else "Off", help=tracing.message, border=True)
    if tracing.endpoint:
        c5.caption(f"Collector: `{tracing.endpoint}`")
    (st.success if ok else st.warning)(f"Dependency graph: {graph_msg}")
    (st.success if tracing.enabled else st.info)(tracing.message)
    if cfg.llm_available:
        st.success("LLM agents: active")
    else:
        missing = cfg.missing_keys_for_models()
        st.warning("LLM agents: offline mode" + (f" – missing keys or EU endpoints for the selected models: "
                                                 f"{', '.join(missing)}"
                                                 if missing else ""))
    if not cfg.pii_masking_enabled:
        st.error("PII masking is disabled: real names (PM, sponsor, assignees, stakeholders) are sent to LLM providers.")
    st.markdown("**LLM routing**")
    st.dataframe(pd.DataFrame([{"Agent role": r, "Model": cfg.model_for_role(r), "Shadow": cfg.shadow_model_for(r) or "–"}
                               for r in AGENT_ROLES]), hide_index=True, width="stretch")
    st.caption(f"Temperature {cfg.temperature:.2f} · Data Steward checks → deterministic")
    _render_llm_calls()
    st.markdown(f"**Data residency** · `LLM_DATA_RESIDENCY={settings.llm_data_residency}`")
    st.dataframe(pd.DataFrame(endpoint_rows()), hide_index=True, width="stretch")
    st.caption("Set in `.env` or the secrets: `LLM_DATA_RESIDENCY=eu` calls only providers with an own endpoint "
               "(`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` – EU gateway, Azure OpenAI or a local model server); the "
               "app cannot check an endpoint's region, the setting states it. See docs/DATA_PROTECTION.md.")
    auth = cfg.auth
    st.markdown("**Sign-in**")
    st.dataframe(pd.DataFrame([
        {"Setting": "Failed attempts before an account is locked", "Value": f"{auth.max_failed_attempts}"},
        {"Setting": "Lockout duration", "Value": f"{auth.lockout_minutes:g} min"},
        {"Setting": "Session timeout without activity", "Value": f"{auth.session_timeout_minutes:g} min"},
        {"Setting": "Sign-in attempts per minute (all accounts)", "Value": f"{auth.max_attempts_per_minute}"},
    ]), hide_index=True, width="stretch")
    st.caption(f"Section \"auth\" of `{display_path(env_config_path(cfg.environment))}` (read-only here).")
    render_graph_backend()
    render_servicenow_sync()
    render_write_journal()


def render_graph_backend() -> None:
    """Configured and active graph backend (NetworkX or Neo4j) with its health, read-only."""
    from app import services

    try:
        backend = services.graph_backend_status()
    except Exception as exc:  # noqa: BLE001 - the status page must render without the graph backend
        backend = {"warning": str(exc), "rows": [], "note": ""}
    st.markdown("**Graph backend**")
    if backend["warning"]:
        st.warning(backend["warning"])
    st.dataframe(pd.DataFrame(backend["rows"]), hide_index=True, width="stretch")
    st.caption(backend["note"])


def _sync_servicenow(full: bool) -> None:
    outcome = views.run_servicenow_sync(full, _user(), _role())
    if outcome.ok:
        load_dataset.clear()
    _flash(outcome)


def render_servicenow_sync() -> None:
    """Last sync per ServiceNow source and the buttons to read the changes (or everything) now."""
    try:
        rows = views.servicenow_rows()
    except DataSourceError as exc:
        st.error(f"ServiceNow: {exc}")
        return
    if rows is None:
        return
    st.markdown("**ServiceNow**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    c1.button("Sync ServiceNow (changes)", icon=":material/sync:", key="sn_sync", on_click=_sync_servicenow,
              args=(False,), width="stretch", help="Reads only the records changed since the last sync.")
    with c2:
        confirm_button("Full sync", "sn_sync_full", _sync_servicenow, args=(True,), icon=":material/cloud_download:",
                       message="Reads every ServiceNow table completely and replaces the local snapshot. This takes "
                               "much longer than a change sync and counts against the instance's API limits.",
                       width="stretch", help="Reads every table completely (after a mapping change).")
    st.caption("Read-only: approved changes stay in DerKurator's own store and are never written to ServiceNow. "
               "Credentials come from the secrets SERVICENOW_INSTANCE, SERVICENOW_USER/SERVICENOW_PASSWORD or "
               "SERVICENOW_TOKEN.")


def _journal_source():
    return views.source_path()


def render_write_journal() -> None:
    """Gaps between the intent and committed records of data writes and the proposal decisions (red rows). Nothing
    is repaired automatically; the admin checks and marks a finding as resolved with a comment (audited)."""
    st.markdown("**Write journal**")
    findings = journal.reconcile_operations(_journal_source())
    if not findings:
        st.success("Every data write has its intent and committed record, and every written proposal its decision.")
        return
    st.error(f"{len(findings)} write journal finding(s) – check the data against the snapshot, then mark as resolved.")
    for f in findings:
        with st.container(border=True):
            st.markdown(f":red[:material/error: **{journal.KINDS.get(f['kind'], f['kind'])}**] · operation "
                        f"`{f['operation_id']}` · snapshot `{f.get('snapshot_id') or '–'}` · proposal "
                        f"`{f.get('proposal_id') or '–'}` · {f.get('user_id') or '–'} · {timestamp(f.get('timestamp'))}")
            st.caption(f.get("detail") or "")
            note, button = st.columns([4, 1], vertical_alignment="bottom")
            comment = note.text_input("Comment (required)", key=f"journal_comment_{f['operation_id']}_{f['kind']}")
            if button.button("Mark as resolved", key=f"journal_resolve_{f['operation_id']}_{f['kind']}",
                             disabled=not comment.strip()):
                try:
                    journal.mark_resolved(f, _user() or "", _role(), comment)
                except (ValueError, logs.AuditWriteError) as exc:
                    st.error(str(exc))
                else:
                    st.rerun()


def render_model_settings_area() -> None:
    render_model_settings()
    render_add_model()


def _reset_costs() -> None:
    views.reset_costs(_user())


def render_finops() -> None:
    usage = cost_tracker.snapshot()
    with st.container(border=True):
        st.subheader("FinOps – estimated LLM costs")
        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Estimated costs (USD)", f"${usage['cost_usd']:.4f}", border=True,
                  help="Since app start / last reset, all users incl. background shadow runs. List prices from "
                       "supported_models.json (input/output_cost_per_1m) – an estimate, not an invoice.")
        f2.metric("Tokens", f"{usage['total_tokens']:,}", help=f"{usage['prompt_tokens']:,} in · {usage['completion_tokens']:,} out",
                  border=True)
        f3.metric("LLM calls", usage["calls"], border=True)
        with f4:
            confirm_button("Reset counters", "reset_costs", _reset_costs, icon=":material/restart_alt:",
                           message="Sets the cost, token and call counters back to zero. The per-call log file stays.")
        if usage["unpriced_models"]:
            st.warning(f"No price configured for: {', '.join(usage['unpriced_models'])} – their costs are not included.")
        if usage["by_model"]:
            table = pd.DataFrame([{"model": m, **v} for m, v in usage["by_model"].items()]).sort_values("cost_usd", ascending=False)
            thinking = table["model"].map(is_thinking_model)
            table.insert(1, "thinking", thinking.map({True: "yes", False: ""}))
            st.dataframe(table.drop(columns=["priced"]), hide_index=True, width="stretch",
                         column_config={"cost_usd": st.column_config.NumberColumn("Cost (USD)", format="$%.4f")})
            share = table.loc[thinking, "cost_usd"].sum() / usage["cost_usd"] if usage["cost_usd"] else 0
            if share >= 0.5:
                st.caption(f":material/psychology: Thinking models cause {share:.0%} of the estimated costs – consider a cheaper main model "
                           "for routine agents (e.g. Orchestrator).")


NOTIFY_TEXT = ("Mail (SMTP) and Microsoft Teams messages about the approval inbox: a proposal waiting for a decision, "
               "an overdue or expired proposal, the decision back to the requester. Set in `.env` or the secrets "
               "(`NOTIFY_*`, docs/PILOT_OPERATIONS.md); secrets are never shown here, and the outbox only in counts.")


def _send_test_notification() -> None:
    _flash(views.send_test_notification(st.session_state.get("notify_test_to", ""), _user()))


def render_notifications() -> None:
    """On/off, the configured channels, the outbox in counts and a test message (audited)."""
    view = views.notification_status()
    with st.container(border=True):
        st.subheader("Notifications")
        st.caption(NOTIFY_TEXT)
        n1, n2, n3, n4 = st.columns(4)
        n1.metric("Notifications", "On" if view["enabled"] else "Off", border=True)
        n2.metric("Sent", view["sent"], border=True,
                  help=f"Last sent {timestamp(view['last_sent_at'])}" if view["last_sent_at"] else None)
        n3.metric("Waiting", view["queued"], border=True,
                  help=f"{view['retrying']} retrying · {view['events_waiting']} event(s) not resolved yet")
        n4.metric("Failed", view["failed"], border=True, help="Given up after the last attempt")
        if not view["enabled"]:
            st.info("Notifications are off – set `NOTIFY_ENABLED=true` and at least one channel.")
        error = view["last_error"]
        if error:
            st.warning(f"Last delivery error ({timestamp(error['at'])}, {error['channel']}): {error['error']}")
        st.dataframe(pd.DataFrame(view["channels"]).rename(columns={"channel": "Channel", "status": "Status"}),
                     hide_index=True, width="stretch")
        address, button = st.columns([3, 1], vertical_alignment="bottom")
        address.text_input("Test recipient (mail)", key="notify_test_to", placeholder="empty: the role mailboxes")
        button.button("Send test notification", icon=":material/send:", key="notify_test",
                      on_click=_send_test_notification, width="stretch")
        st.caption("Without SSO the addresses come from the `email` of the accounts in the user file and the role "
                   "mailboxes; SSO will supply them later.")


def render_environment_staging() -> None:
    cfg = get_runtime_config()
    with st.container(border=True):
        st.subheader("Environment staging")
        left, right = st.columns([1, 2])
        with left:
            st.session_state["admin_env_select"] = cfg.environment
            confirm_env = st.checkbox("Confirm environment switch", key="confirm_env_switch")
            st.selectbox("System Environment (Staging)", ENVIRONMENTS, key="admin_env_select", on_change=switch_environment_clicked,
                         disabled=not confirm_env,
                         help="Switching loads data/env_<environment>.json (models, shadowing, required fields). "
                              "PROD needs real accounts (AUTH_USERS_FILE).")
            st.caption(f"Active file: `{display_path(env_config_path(cfg.environment))}`")
        with right:
            st.markdown("**Promotion**")
            p1, p2 = st.columns(2)
            confirm_test = p1.checkbox("Confirm promotion to TEST", key="confirm_promote_test")
            p1.button("Promote config to TEST", icon=":material/upgrade:", key="promote_dev_test", on_click=promote_clicked, args=("DEV", "TEST"),
                      disabled=not confirm_test, help="Copies all DEV settings into env_test.json (previous TEST config is archived).")
            confirm = p2.checkbox("Confirm promotion to PROD", key="confirm_promote_prod")
            p2.button("Promote TEST to PROD", icon=":material/upgrade:", key="promote_test_prod", on_click=promote_clicked, args=("TEST", "PROD"),
                      disabled=not confirm, help="Copies all TEST settings into env_prod.json (previous PROD config is archived).")


def render_model_settings() -> None:
    cfg = get_runtime_config()
    registry = model_ids()
    options = views.model_options()
    matrix_key = _matrix_key()
    with st.container(border=True):
        st.subheader("Models, shadowing & behaviour")
        if not registry:
            st.error(f"No models available – check `{display_path(registry_path())}`.")
        with st.form("admin_settings"):
            st.markdown("**API keys**")
            key_cols = st.columns(3)
            for col, (provider, label, placeholder) in zip(key_cols, (
                ("openai", "OpenAI API key", "sk-…"), ("anthropic", "Anthropic API key", "sk-ant-…"),
                ("google", "Google API key (Gemini)", "AIza…"),
            ), strict=True):
                hint = key_hint(cfg.has_key_for(provider))
                col.text_input(label, type="password", key=f"admin_{provider}_key", placeholder=placeholder, help=hint)
                col.caption(hint)

            st.markdown("**Per-agent model matrix** – main LLM runs the pipeline; the shadow LLM runs the same agents "
                        "in the background for comparison (Shadow Logs). The Data Steward's checks stay deterministic; "
                        "its model writes the executive data-quality interpretation.")
            st.data_editor(
                pd.DataFrame(_matrix_rows()), key=matrix_key, hide_index=True, width="stretch", num_rows="fixed",
                disabled=["agent"],
                column_config={
                    "agent": st.column_config.TextColumn("Agent"),
                    "main": st.column_config.SelectboxColumn("Main LLM", options=options, required=True),
                    "shadow": st.column_config.SelectboxColumn("Shadow LLM", options=[NO_SHADOW, *options], required=True),
                },
            )
            b1, b2 = st.columns([1, 2])
            with b1:
                st.toggle("Enable PII masking", key="admin_pii_enabled",
                          help="Pseudonymises person names (PM, sponsor, assignees, stakeholders, benefit and demand owners) "
                               "and e-mail addresses before every LLM call.")
                st.slider("Agent creativity (temperature)", 0.0, 1.0, step=0.05, key="admin_temperature",
                          help="0 = deterministic wording, 1 = creative. Applies to all agents. Thinking/reasoning models "
                               "(is_thinking_model in supported_models.json) ignore it and always run with 1.0. "
                               "Numbers are calculated by the deterministic tools anyway.")
                st.caption("Not applied to Claude models (not supported by the provider).")
                thinking = views.thinking_models_in_use()
                if thinking:
                    st.caption(f":material/psychology: Overridden to 1.0 for thinking models: {', '.join(thinking)}")
            with b2:
                st.text_area("Orchestrator system prompt", key="admin_orchestrator_prompt", height=150,
                             help="Backstory of the orchestrator agent (intent split and routing). Empty = default prompt.")
            st.form_submit_button("Save settings", icon=":material/save:", type="primary", on_click=save_admin_settings, args=(matrix_key,))
        confirm_button("Reload from .env and environment file", "reload_settings", reset_admin_settings,
                       icon=":material/restart_alt:", confirm_label="Discard and reload",
                       message="Discards every setting changed here that is not yet in the .env or environment file.")
        if cfg.shadow_enabled and not cfg.shadow_available:
            st.warning(f"Shadowing is configured but keys are missing: {', '.join(cfg.missing_keys_for_models('shadow'))}")


def render_add_model() -> None:
    with st.expander("Add new LLM"):
        st.caption(f"Adds a model to `{display_path(registry_path())}` after a successful live test call.")
        st.text_input("Model-ID (optionally with provider prefix)", key="byom_model_id", placeholder="e.g. gemini/gemini-2.5-pro or gpt-4.1")
        st.checkbox("Is a reasoning/thinking model", key="byom_thinking",
                    help="Thinking models run with temperature 1.0 and without a separate system prompt.")
        if st.button("Test & add model", key="byom_add", type="primary"):
            model_id = st.session_state.get("byom_model_id", "").strip()
            thinking = bool(st.session_state.get("byom_thinking"))
            with st.spinner(f"Testing {model_id} ..."):
                outcome = views.test_and_add_model(model_id, thinking, _user())
            if not outcome.ok:
                st.error(outcome.message)
                return
            st.success(outcome.message)
        with st.container(height=180):
            st.dataframe(pd.DataFrame(views.model_rows()), hide_index=True, width="stretch")


def render_required_fields_settings(editable: bool = True) -> None:
    """Data Steward required fields; `editable` only for the PMO Head (the core checks it again)."""
    current = load_required_fields()
    try:
        columns = list(load_dataset().projects.columns)
    except Exception:  # noqa: BLE001 - settings must stay reachable without a readable data source
        columns = []
    options = views.required_field_options(columns, current)
    st.session_state["admin_required_fields"] = current  # sync with the saved config (a form avoids a rerun per click)
    with st.container(border=True):
        st.subheader("Data Steward required fields")
        with st.form("required_fields_form") if editable else st.container():
            st.multiselect("Required fields (columns of the Projects table)", options, key="admin_required_fields",
                           format_func=field_label, disabled=not editable,
                           help="Empty values in these fields are reported as ERROR and block the project for simulation "
                                f"(PMO can force). Always required: {', '.join(map(field_label, LOCKED_FIELDS))}. "
                                "If the estimate at completion (EAC) is required, a missing EAC blocks the project "
                                "already here – before the verdict's own EAC-missing rule can apply.")
            if editable:
                st.form_submit_button("Save required fields", icon=":material/save:", type="primary",
                                      on_click=save_required_fields_clicked)
        if editable:
            confirm_button("Restore default required fields", "reset_required_fields", reset_required_fields_clicked,
                           icon=":material/restart_alt:", confirm_label="Restore defaults",
                           message="Replaces the required-field list of this environment with the defaults. Your own "
                                   "selection is lost.")
        missing_calc = views.missing_calc_fields(current)
        st.caption(f"Stored per environment in `{display_path(env_config_path(get_runtime_config().environment))}` · "
                   f"{len(current)} fields active."
                   + (f" Note: {', '.join(missing_calc)} are not required – budget simulations still reject projects with "
                      "missing values." if missing_calc else ""))
        with st.expander("Other Data Steward rules (deterministic, read-only)"):
            st.markdown(
                "| data_quality_profile | Severity | Meaning |\n|---|---|---|\n"
                + "\n".join(f"| `{tag}` | {sev} | {text} |" for tag, (sev, text) in DQ_PROFILE_TAGS.items())
                + "\n\nAlso: logic check (progress without actual start), past due, budget overrun (EAC > budget), "
                  "CapEx/OpEx split, resource overload, task integrity."
            )


def render_database_management() -> None:
    settings = get_settings()
    source = {"json": "JSON", "excel": "Excel", "mapped": "the mapped sources"}.get(settings.data_source,
                                                                                settings.data_source)
    with st.container(border=True):
        st.subheader("Database management")
        st.caption(f"Dependency graph (NetworkX, in memory) · source: {source}. The app builds it on first use and "
                   "rebuilds it automatically when the source data changes – the button forces an immediate rebuild.")
        meta = current_metadata()
        if meta is None:
            st.caption("The graph has not been built in this process yet – it is built on the first analysis.")
        else:
            counts = meta.get("counts", {})
            st.caption(f":material/check_circle: Built {timestamp(meta['built_at'])} in {meta['build_seconds']:g} s "
                       f"(source: {meta.get('source') or '?'}) · {counts.get('Project', 0)} projects, "
                       f"{counts.get('Task', 0)} tasks, {counts.get('Team', 0)} teams, {counts.get('Risk', 0)} risks, "
                       f"{counts.get('DEPENDS_ON', 0)} project dependencies")
        _render_import_report()
        if st.button("Rebuild dependency graph", key="rebuild_graph", icon=":material/restart_alt:"):
            logger.info("UI | Graph rebuild requested | user=%s", _user())
            with st.spinner("Rebuilding the dependency graph ..."):
                outcome = views.rebuild_graph(_user(), settings.data_source)
            if not outcome.ok:
                st.error(outcome.message)
                return
            graph_status.clear()
            counts = outcome.data["counts"]
            st.success(f"Dependency graph built from {source}: {counts.get('Project', 0)} projects, "
                       f"{counts.get('Task', 0)} tasks, {counts.get('Team', 0)} teams, {counts.get('Risk', 0)} risks, "
                       f"{sum(v for k, v in counts.items() if k.isupper())} relationships.")


def _render_import_report() -> None:
    """Rows read, loaded and skipped per table with the notes of the last load (no project content)."""
    try:
        report = views.import_report()
    except DataSourceError as exc:
        st.error(f"The data source could not be loaded: {exc}")
        return
    if not report:
        return
    findings = len(report.get("findings", []))
    with st.expander(f"Import report · {report['mapping']} · {findings} finding(s)", icon=":material/fact_check:"):
        rows = views.import_report_rows(report)
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        for table, t in report["tables"].items():
            for note in t["warnings"]:
                st.caption(f"{table}: {note}")
        st.caption("Findings also appear in the Data Steward report. Mapping files: docs/DATA_MAPPING.md")


# =========================================================================== integrations (MCP)
def _sync_mcp_state(env: str) -> None:
    """Loads the environment's MCP settings into the widgets once per environment (keeps unsaved input on errors)."""
    ss = st.session_state
    if ss.get("mcp_loaded_env") == env:
        return
    mcp = load_mcp_config(env)
    ss["mcp_enabled"] = mcp["enabled"]
    for name in MCP_SERVERS:
        ss[f"mcp_url_{name}"] = mcp["servers"].get(name, "")
    ss["mcp_loaded_env"] = env


def save_mcp_clicked() -> None:
    ss = st.session_state
    _flash(views.save_mcp(ss["mcp_enabled"], {n: ss[f"mcp_url_{n}"] for n in MCP_SERVERS}, _user()))


def probe_mcp_clicked(name: str) -> None:
    st.session_state[f"mcp_probe_{name}"] = views.probe_mcp(name, _user())


def render_mcp_integrations() -> None:
    env = get_runtime_config().environment
    _sync_mcp_state(env)
    stored = load_mcp_config(env)
    st.subheader("Integrations (MCP)")
    st.caption("External enterprise data via the Model Context Protocol. Settings are stored per environment in "
               f"`{display_path(env_config_path(env))}` (promoted with the rest of the config). Access tokens are secrets: "
               "set them as environment variables (e.g. `SERVICENOW_MCP_TOKEN`) – they are never stored or shown.")
    with st.container(border=True):
        with st.form("mcp_settings"):
            st.toggle("Enable MCP enterprise data (overrides the local DB)", key="mcp_enabled",
                      help="Master switch for a future release: agents load their data tools from the MCP servers "
                           "instead of the in-memory dependency graph.")
            for name, label in MCP_SERVERS.items():
                st.text_input(label, key=f"mcp_url_{name}", placeholder=f"https://{name}.example.com/mcp")
            st.form_submit_button("Save integrations", icon=":material/save:", type="primary", on_click=save_mcp_clicked)
    if stored["enabled"]:
        st.warning("MCP is enabled in this environment's config. Note: the connector is an architecture building block – "
                   "the agents still use the in-memory dependency graph until it is wired in (see below).")
    else:
        st.info("MCP disabled – agents use the in-memory dependency graph (default).")

    for name, label in MCP_SERVERS.items():
        url = stored["servers"].get(name, "")
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            token_env = f"{name.upper()}_MCP_TOKEN"
            c1.markdown(f"**{label.replace(' URL', '')}** · `{url or 'not configured'}`  \n"
                        f"Token (`{token_env}`): {':material/check: set' if os.getenv(token_env) else '— not set'}")
            c2.button("Test connection", icon=":material/network_check:", key=f"mcp_test_{name}", disabled=not url, on_click=probe_mcp_clicked, args=(name,))
            result = st.session_state.get(f"mcp_probe_{name}")
            if result:
                (st.success if result[0] else st.error)(result[1])

    with st.expander("Architecture – how the connector plugs into the agents"):
        st.markdown("`app/crew/tools/mcp_client.py` → `EnterpriseMCPConnector` turns these settings into agent "
                    "tools (Streamable HTTP, or SSE for `…/sse` URLs). Wiring it in is one line per agent:")
        st.code("connector = EnterpriseMCPConnector.from_environment()\n"
                "agent = Agent(**connector.agent_kwargs(agent_kwargs))  # no-op while MCP is disabled", language="python")
        st.caption("Before enabling in PROD: route MCP results through the same RLS filter and PII masking as local data.")


# =========================================================================== business rules
_INT_RULES = views.INT_RULES


def _rule_bounds(name: str) -> tuple[float | None, float | None]:
    return views.rule_bounds(name)


def _sync_rules_state(force: bool = False) -> None:
    ss, cfg = st.session_state, get_runtime_config()
    if force or ss.get("rules_loaded_env") != cfg.environment:
        for name in RULE_LABELS:
            ss[f"rule_{name}"] = getattr(cfg.business_rules, name)
        ss["rules_loaded_env"] = cfg.environment


def _apply_business_rules(values: dict, event: str) -> None:
    outcome = views.apply_business_rules(values, event, _user(), _role(), is_pmo_head())
    if outcome.data.get("reseeded"):
        _clear_data_caches()
    if outcome.ok:
        _sync_rules_state(force=True)
    _flash(outcome)


def save_business_rules_clicked() -> None:
    _apply_business_rules({name: st.session_state[f"rule_{name}"] for name in RULE_LABELS}, "business_rules_changed")


def reset_business_rules_clicked() -> None:
    _apply_business_rules(BusinessRules().model_dump(), "business_rules_reset")


def render_business_rules(editable: bool = True) -> None:
    """Business rules with their bounds; `editable` only for the PMO Head (the core checks it again)."""
    _sync_rules_state()
    cfg = get_runtime_config()
    st.subheader("Business rules")
    st.caption("The organisation's business assumptions and verdict thresholds, owned by the PMO Head. "
               f"Stored per environment in `{display_path(env_config_path(cfg.environment))}` and promoted with it. "
               "Used by the calculation tools, the Data Steward and the deterministic verdict engine, and given to "
               "the agents as 'business assumptions'.")
    groups = views.RULE_GROUPS
    with st.form("business_rules_form") if editable else st.container():
        for title, names in groups.items():
            st.markdown(f"**{title}**")
            cols = st.columns(len(names))
            for col, name in zip(cols, names, strict=True):
                label, unit, help_text = RULE_LABELS[name]
                low, high = _rule_bounds(name)
                if name in _INT_RULES:
                    col.number_input(f"{label} ({unit})", min_value=int(low), max_value=int(high), step=1,
                                     key=f"rule_{name}", help=help_text, disabled=not editable)
                else:
                    col.number_input(f"{label} ({unit})", min_value=float(low), max_value=float(high),
                                     step=100.0 if unit.startswith("€") else 0.5, key=f"rule_{name}", help=help_text,
                                     disabled=not editable)
        if editable:
            st.caption("Changing the team capacity limit rebuilds the graph database (overloaded months are "
                       "pre-computed).")
            st.form_submit_button("Save business rules", icon=":material/save:", type="primary",
                                  on_click=save_business_rules_clicked)
    if editable:
        confirm_button("Reset to defaults", "reset_business_rules", reset_business_rules_clicked,
                       icon=":material/restart_alt:", confirm_label="Reset business rules",
                       message="Replaces every business rule threshold with its default. Verdicts from now on use the "
                               "defaults; your values are lost (the audit trail keeps the change).")
    with st.expander("How the verdict engine decides"):
        st.markdown(verdict_rules_md())


# =========================================================================== snapshots
def create_snapshot_clicked() -> None:
    _flash(views.take_snapshot(_user()))


def restore_snapshot_clicked() -> None:
    ss = st.session_state
    outcome = views.restore(ss.get("snapshot_select"), _user(), _role())
    if outcome.data.get("reseeded"):
        _clear_data_caches()
        ss["confirm_restore"] = False
    _flash(outcome)


def render_snapshots() -> None:
    st.subheader("Snapshots")
    source = get_data_provider().source_path
    st.caption(f"A copy of the data source is saved to `{display_path(snapshot_dir())}` before every write "
               f"(Approve & Apply, project edits, restores). The newest {MAX_SNAPSHOTS} are kept. "
               f"Active source: `{display_path(source) if source else 'n/a'}`.")
    st.button("Create snapshot now", icon=":material/photo_camera:", key="create_snapshot", on_click=create_snapshot_clicked, disabled=source is None)
    snaps = views.snapshots_of_source()
    if not snaps:
        st.info("No snapshots yet – the first one is created with the next data change.")
        render_data_lifecycle()
        return
    table = pd.DataFrame([{"id": s["id"], "created": timestamp(s.get("created_at")),
                           "reason": s.get("reason"), "user": s.get("user") or "–", "env": s.get("environment"),
                           "changes": len(s.get("changes") or []), "size_kb": round((s.get("size") or 0) / 1024, 1)}
                          for s in snaps])
    st.dataframe(table, hide_index=True, width="stretch")
    labels = {s["id"]: f"{str(s.get('created_at', '')).replace('T', ' ')} · {s.get('reason')} · {s.get('user') or '–'} ({s['id']})"
              for s in snaps}
    with st.container(border=True):
        st.markdown("**Restore**")
        snap_id = st.selectbox("Snapshot", list(labels), format_func=labels.get, key="snapshot_select")
        selected = next(s for s in snaps if s["id"] == snap_id)
        if selected.get("changes"):
            with st.expander(f"Changes written after this snapshot ({len(selected['changes'])})"):
                cols = ["project_id", "table", "field", "old", "new", "source_tool"]
                st.dataframe(pd.DataFrame(selected["changes"]).reindex(columns=cols).astype(str), hide_index=True,
                             width="stretch")
        st.warning("Restoring replaces the complete data source with this snapshot – all later changes are undone. "
                   "The current state is saved as a new snapshot first; the graph database is rebuilt.")
        confirm = st.checkbox("I want to restore this snapshot", key="confirm_restore")
        st.button("Restore snapshot", icon=":material/restore:", key="restore_snapshot", type="primary", disabled=not confirm,
                  on_click=restore_snapshot_clicked)
    render_data_lifecycle()


def reset_demo_clicked() -> None:
    outcome = views.reset_demo(_user())
    if outcome.data.get("reseeded"):
        _clear_data_caches()
    _flash(outcome)


def render_demo_reset() -> None:
    if not views.demo_reset_available():
        return  # never offered in PROD
    st.subheader("Reset demo data")
    st.caption("Puts the portfolio back to the state this instance started with and archives open proposals, chats "
               "and saved what-ifs (data/archive/). The current state is saved as a snapshot first.")
    confirm = st.checkbox("I want to reset the demo data", key="confirm_demo_reset")
    st.button("Reset demo data", icon=":material/settings_backup_restore:", key="reset_demo_data", disabled=not confirm,
              on_click=reset_demo_clicked)


def purge_user_clicked() -> None:
    outcome = views.purge(st.session_state.get("purge_user_id"), _user(), _role())
    if outcome.ok:
        st.session_state["purge_user_id"], st.session_state["confirm_purge_user"] = "", False
    _flash(outcome)


def render_purge_user() -> None:
    st.subheader("Purge user")
    st.caption("Erasure request: removes the user's chats and saved what-ifs and replaces the user id in proposals "
               "and archives by a pseudonym. The decision log and the snapshots stay – they are approval evidence.")
    st.text_input("User id", key="purge_user_id", placeholder="exactly as in the user file")
    confirm = st.checkbox("I want to purge this user's personal data", key="confirm_purge_user")
    st.button("Purge user", icon=":material/person_remove:", key="purge_user", disabled=not confirm,
              on_click=purge_user_clicked)


def render_data_lifecycle() -> None:
    render_demo_reset()
    render_purge_user()


# =========================================================================== logs
def log_toolbar(name: str, on_clear, clear_help: str) -> None:
    """Uniform refresh/clear bar. Clearing requires confirmation."""
    c1, c2, c3 = st.columns([1, 1, 4])
    if c1.button("Refresh", icon=":material/refresh:", key=f"refresh_{name}"):
        logger.info("UI | Log view refreshed | log=%s", name)
        st.rerun()
    confirm = c3.checkbox("Confirm clearing", key=f"confirm_clear_{name}", help=clear_help)
    c2.button("Clear", icon=":material/delete:", key=f"clear_{name}", disabled=not confirm, on_click=_clear_log,
              args=(name, on_clear))


def _clear_log(name: str, on_clear) -> None:
    try:
        on_clear()
    except OSError as exc:
        notify("error", f"Clearing failed: {exc}")
    st.session_state[f"confirm_clear_{name}"] = False  # the next clear needs a fresh confirmation


def _clear_audit() -> None:
    _flash(views.clear_audit_log(_user()))


def _clear_shadow() -> None:
    _flash(views.clear_shadow_log(_user()))


def _clear_system() -> None:
    _flash(views.clear_system_log(_user()))


def render_business_audit() -> None:
    with st.expander("Business audit trail (project master-data edits)", expanded=False):
        st.caption(f"Source: `{display_path(logs.BUSINESS_AUDIT_LOG)}` – one line per changed field.")
        try:
            lines = logs.tail_lines(logs.BUSINESS_AUDIT_LOG, 200)
        except FileNotFoundError:
            lines = []
        except OSError as exc:
            st.error(f"Business audit log unreadable: {exc}")
            return
        if lines:
            st.code("\n".join(lines), language=None)  # newest first
        else:
            st.info("No entries")


def render_analysis_runs(limit: int = 200) -> None:
    """Technical details of the latest analyses – for the System Admin only (business users never see run mode or
    runtime). No names, no request text, no project data beyond IDs."""
    st.subheader("Analysis runs")
    st.caption(f"Source: `{display_path(logs.AUDIT_LOG)}` (event `analysis_run`) – role, run mode, duration, rule-based "
               "verdict and the plan the pipeline executed.")
    rows = views.analysis_runs(limit)
    if not rows:
        st.info("No analyses recorded yet.")
        return
    table = pd.DataFrame(rows)
    modes = sorted(table["Mode"].dropna().astype(str).unique())
    picked = st.multiselect("Mode", modes, default=modes, key="runs_mode_filter")
    st.dataframe(table[table["Mode"].astype(str).isin(picked)], hide_index=True, width="stretch",
                 column_config={"Plan": st.column_config.TextColumn("Plan (JSON)", width="large")})
    durations = pd.to_numeric(table["Duration (s)"], errors="coerce").dropna()
    if not durations.empty:
        st.caption(f"{len(table)} run(s) · median {durations.median():.1f} s · p95 {durations.quantile(0.95):.1f} s")


def render_audit_chain() -> None:
    """Tamper evidence: every record of the decision log carries the hash of its predecessor."""
    if not st.button("Verify the hash chain", icon=":material/link:", key="audit_verify"):
        return
    check = views.verify_audit_chain(_user())
    legacy = f" · {check['unchained']} older record(s) from before the chain" if check["unchained"] else ""
    if check["ok"]:
        st.success(f"Hash chain intact: {check['chained']} chained record(s){legacy}.", icon=":material/verified:")
    else:
        st.error(f"Hash chain broken at line {check['broken_at']}: {check['reason']}. Compare with the latest archive "
                 "or snapshot before trusting later records.", icon=":material/link_off:")


def render_audit_logs() -> None:
    render_business_audit()
    st.subheader("Audit logs")
    st.caption(f"Source: `{display_path(logs.AUDIT_LOG)}` – approved scenarios, project edits and admin changes (no secrets).")
    log_toolbar("audit", _clear_audit, "The content is copied to data/archive/ before clearing.")
    render_audit_chain()
    try:
        records, malformed = logs.read_jsonl(logs.AUDIT_LOG)
    except FileNotFoundError:
        st.info("No entries")
        return
    if not records:
        st.info("No entries")
        return
    if malformed:
        st.warning(f"{malformed} corrupt line(s) skipped.")
    df = pd.DataFrame(records)
    for col in df.columns:  # make nested fields (changes, changed, manual) readable
        if df[col].map(lambda v: isinstance(v, (dict, list))).any():
            df[col] = df[col].map(lambda v: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
    df = df.sort_values("timestamp", ascending=False) if "timestamp" in df.columns else df.iloc[::-1]
    events = sorted(df["event"].dropna().unique()) if "event" in df.columns else []
    selected = st.multiselect("Event type", events, default=events, key="audit_event_filter") if events else []
    if events:
        df = df[df["event"].isin(selected)]
    front = [c for c in ("timestamp", "environment", "event", "role", "user_id", "request", "applied") if c in df.columns]
    st.dataframe(df[front + [c for c in df.columns if c not in front]].astype(str), hide_index=True, width="stretch")
    st.caption(f"{len(df)} entries")


def render_incidents(limit: int = 200) -> None:
    """System errors with their incident ID (UI errors, failed analyses, failed audit writes) – the traceback is in
    the system log under the same ID. Kept apart from the decision log, which holds decisions only."""
    st.subheader("Incidents")
    st.caption(f"Source: `{display_path(logs.INCIDENTS_LOG)}` – incident ID, section and error type; the traceback is in "
               "the system log under the same ID.")
    try:
        records, malformed = logs.read_jsonl(logs.INCIDENTS_LOG)
    except FileNotFoundError:
        records, malformed = [], 0
    if malformed:
        st.warning(f"{malformed} corrupt line(s) skipped.")
    if not records:
        st.info("No incidents recorded.")
        return
    front = ["timestamp", "incident_id", "event", "section", "error_type", "role", "user_id"]
    df = pd.DataFrame(records[-limit:][::-1])
    st.dataframe(df[[c for c in front if c in df.columns] + [c for c in df.columns if c not in front]].astype(str),
                 hide_index=True, width="stretch")


def _models_table(side: dict) -> pd.DataFrame | None:
    models = side.get("models")
    if isinstance(models, dict):
        return pd.DataFrame([{"agent": AGENT_LABELS.get(a, a), "model": m} for a, m in models.items()])
    return None


def render_shadow_logs() -> None:
    st.subheader("Shadow logs")
    st.caption(f"Source: `{display_path(logs.SHADOW_LOG)}` – main run vs. shadow run (per-agent shadow models) per request.")
    cfg = get_runtime_config()
    with st.expander("Current per-agent shadow configuration"):
        st.dataframe(pd.DataFrame(_matrix_rows()), hide_index=True, width="stretch")
        st.caption("Shadowing active." if cfg.shadow_available else
                   "Shadowing inactive (no shadow model set or keys missing) – configure it in Settings.")
    log_toolbar("shadow", _clear_shadow, "The content is copied to data/archive/ before clearing.")
    try:
        records, malformed = logs.read_jsonl(logs.SHADOW_LOG)
    except FileNotFoundError:
        st.info("No entries")
        return
    if not records:
        st.info("No entries")
        return
    if malformed:
        st.warning(f"{malformed} corrupt line(s) skipped.")

    def side(entry: dict, key: str) -> dict:
        value = entry.get(key)
        return value if isinstance(value, dict) else {"result": value}

    for entry in sorted(records, key=lambda r: str(r.get("timestamp", "")), reverse=True):
        primary, shadow = side(entry, "primary"), side(entry, "shadow")
        p_t, s_t = primary.get("duration_s"), shadow.get("duration_s")
        delta = f" · Δ {s_t - p_t:+.1f}s" if isinstance(p_t, (int, float)) and isinstance(s_t, (int, float)) else ""
        match = entry.get("verdict_match")
        badge = " · :material/check: same verdict" if match is True else " · :material/warning: verdicts differ" if match is False else ""
        title = f"{entry.get('timestamp', '?')} · {str(entry.get('request', '–'))[:80]}{delta}{badge}"
        with st.expander(title):
            st.markdown(f"**Request:** {entry.get('request', '–')}")
            left, right = st.columns(2)
            for col, label, data, verdict in ((left, "Main run", primary, entry.get("verdict_primary")),
                                              (right, "Shadow run", shadow, entry.get("verdict_shadow"))):
                with col:
                    st.markdown(f"**{label}**" + (f" · recommendation: **{verdict}**" if verdict else ""))
                    table = _models_table(data)
                    if table is not None:
                        st.dataframe(table, hide_index=True, width="stretch")
                    else:
                        st.caption(f"Model: `{data.get('model', 'n/a')}`")
                    dur = data.get("duration_s")
                    st.metric("Runtime", f"{dur:.2f} s" if isinstance(dur, (int, float)) else "–")
                    if data.get("error"):
                        st.error(data["error"])
                    with st.container(height=260):
                        st.markdown(str(data.get("result") or "_no result_"))
            if entry.get("data_steward_shadow"):
                with st.container(border=True):
                    ds = entry["data_steward_shadow"]
                    st.markdown(f"**Data Steward shadow interpretation** (`{ds.get('model')}`, {ds.get('source')})")
                    st.markdown(str(ds.get("interpretation", "")))


def render_system_logs() -> None:
    st.subheader("System logs")
    st.caption(f"Source: `{display_path(logs.APP_LOG)}` – last 100 lines, newest first.")
    log_toolbar("system", _clear_system, "Clears data/app.log (the clearing itself is logged).")
    try:
        lines = logs.tail_lines(logs.APP_LOG, 100)
    except FileNotFoundError:
        st.info("No entries")
        return
    if not lines:
        st.info("No entries")
        return
    level = st.segmented_control("Level", ["ALL", "INFO", "WARNING", "ERROR"], default="ALL", key="syslog_level")
    if level and level != "ALL":
        lines = [line for line in lines if f"| {level}" in line]
    st.code("\n".join(lines) or "(no lines for this filter)", language="log")


# =========================================================================== system tests
def _run_tests_clicked() -> None:
    user = _user()
    logger.info("UI | System tests requested | user=%s", user)
    with st.spinner("Running tests..."):
        try:
            run = views.run_tests(user)
        except SystemTestsBusy as exc:
            st.warning(str(exc))
            return
    st.session_state["last_test_run"] = run


def render_test_results(counts: dict[str, int], cases: list[dict], header: str) -> None:
    if not counts:
        return
    st.caption(header)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Passed", counts.get("passed", 0), border=True)
    m2.metric("Failed", counts.get("failed", 0), border=True)
    m3.metric("Errors", counts.get("error", 0), border=True)
    m4.metric("Skipped", counts.get("skipped", 0), border=True)
    if cases:
        order = {"failed": 0, "error": 1, "skipped": 2, "passed": 3}
        df = pd.DataFrame(cases).sort_values(["outcome", "module", "test"],
                                             key=lambda s: s.map(order) if s.name == "outcome" else s)
        st.dataframe(df, hide_index=True, width="stretch", column_config={
            "outcome": st.column_config.TextColumn("Outcome"), "duration_s": st.column_config.NumberColumn("Duration (s)", format="%.3f"),
        })


def render_system_tests() -> None:
    st.subheader("System tests")
    st.caption("Runs the pytest suite (security, simulator guardrails, Data Steward, config, admin console) on a "
               "deterministic mock portfolio – no network access; logs, decisions and proposals of the run go to a "
               "temporary folder, so the live data stays unchanged.")
    offline = get_settings().force_offline_mode
    if offline:
        st.info("System tests are disabled on this instance (FORCE_OFFLINE_MODE, e.g. the hosted demo). "
                "Run them locally with `python run_tests.py`.")
    if st.button("Run system tests now", type="primary", key="run_system_tests", disabled=offline):
        _run_tests_clicked()
    run = st.session_state.get("last_test_run")
    if run is not None:
        (st.success if run.passed else st.error)(
            f"{run.status_text} (exit code {run.returncode}) · duration {run.duration_s:.1f} s · started {run.started_at}"
        )
        render_test_results(run.counts, run.cases, "Results of this run")
        with st.expander("Console output"):
            st.code(run.stdout or "(no output on stdout)", language="text")
            if run.stderr.strip():
                st.markdown("**stderr**")
                st.code(run.stderr, language="text")
    else:
        try:  # results of an earlier run (e.g. via run_tests.py)
            counts, cases = parse_junit(junit_path())
            stamp = datetime.fromtimestamp(junit_path().stat().st_mtime, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")
            render_test_results(counts, cases, f"Last saved run: {stamp}")
        except (FileNotFoundError, OSError, ET.ParseError):
            pass
    st.divider()
    render_test_report()


def render_test_report() -> None:
    st.markdown("##### HTML test report (pytest-html)")
    try:
        html = logs.TEST_REPORT.read_text(encoding="utf-8", errors="replace")
        stamp = datetime.fromtimestamp(logs.TEST_REPORT.stat().st_mtime, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    except FileNotFoundError:
        st.info("No test report yet – click 'Run system tests now' (or run `python run_tests.py`).")
        return
    except OSError as exc:
        st.warning(f"Test report could not be read: {exc}")
        return
    st.caption(f"As of: {stamp} · file: `{display_path(logs.TEST_REPORT)}`")
    st.download_button("Download test report (HTML)", html, icon=":material/download:", file_name="test_report.html", mime="text/html")
    with st.expander("Show test report", expanded=False):
        # st.iframe instead of st.html: the report renders its table via JavaScript (st.html ignores JS)
        # and ships its own CSS that must stay isolated. Content is generated locally by pytest-html.
        st.iframe(html, height=650)
