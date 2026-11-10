"""System Admin console logic without Streamlit: every action validates, writes, audits and answers an Outcome; every
reader returns plain rows. The Streamlit console (app/ui/admin_ui.py) and the admin API routes (app/api/routes/admin*)
both call these functions, so an action is audited the same way from either front end. Caches of a front end (the
Streamlit data cache, the API data cache) are cleared by the caller."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.approval_flow import audit
from app.core import cost_tracker, journal, logs
from app.core.business_rules import RULE_LABELS, BusinessRules
from app.core.env_config import (
    AGENTS,
    MCP_SERVERS,
    EnvConfigError,
    env_config_path,
    load_mcp_config,
    promote_env_config,
    save_mcp_config,
)
from app.core.model_registry import add_model, get_model, is_thinking_model, load_models, model_ids
from app.core.required_fields import LOCKED_FIELDS, load_required_fields, reset_required_fields, save_required_fields
from app.core.runtime_config import (
    get_runtime_config,
    persist_runtime_config,
    reset_runtime_config,
    switch_environment,
    update_runtime_config,
)
from app.core.security import ROLE_ADMIN, ROLE_PMO, log_security_event
from app.core.snapshots import SnapshotError, create_snapshot, list_snapshots, restore_snapshot
from app.data_adapters import DataSourceError, get_data_provider, servicenow_connectors, sync_servicenow
from app.graph.portfolio_graph import GraphUnavailable, rebuild

logger = logging.getLogger("derkurator.ui.admin")

AGENT_LABELS = {"orchestrator": "Orchestrator", "data_steward": "Data Steward", "analyst": "Analyst",
                "simulator": "Simulator", "advisory": "Advisory"}
NO_SHADOW = "— none —"
PROVIDERS = ("openai", "anthropic", "google")
CALC_FIELDS = ("budget_cost_eur", "actual_cost_eur", "planned_capital_eur", "planned_operating_eur")
PROMOTIONS = (("DEV", "TEST"), ("TEST", "PROD"))
LOG_FILTER_LEVELS = ("ALL", "INFO", "WARNING", "ERROR")
RULE_GROUPS: dict[str, tuple[str, ...]] = {
    "Simulation assumptions": ("fte_monthly_cost_eur", "exit_cost_rate_pct", "sprint_length_days",
                               "delay_cost_factor_pct"),
    "Value, hold & prioritisation": ("default_discount_rate_pct", "hold_retention_pct", "hold_restart_ramp_months",
                                     "max_hold_months", "cut_protected_max_priority"),
    "Guardrails & capacity": ("max_budget_change_pct", "max_timeline_shift_months", "team_capacity_limit_pct",
                              "overload_alert_pct"),
    "Verdict engine": ("domino_conditional_threshold", "eac_overrun_nogo_pct", "board_approval_threshold_eur",
                       "cancel_conditional_sunk_pct"),
    "Verdict tolerances & conditions": ("overrun_tolerance_eur", "condition_due_days"),
    "Re-baselining & management reserve": ("rebaseline_board_threshold_pct", "max_rebaselines_per_year",
                                           "management_reserve_eur", "reserve_floor_pct"),
    "Risk scale – probability": ("risk_probability_low_pct", "risk_probability_moderate_pct",
                                 "risk_probability_high_pct", "risk_response_min_benefit_ratio"),
    "Risk scale – impact": ("risk_impact_low_eur", "risk_impact_moderate_eur", "risk_impact_high_eur"),
    "Benefit realisation": ("benefit_realization_min_pct", "benefit_realization_nogo_pct",
                            "benefit_report_grace_days"),
    "Approval governance": ("approval_sla_days", "approval_expiry_days", "delegation_max_days"),
    "Status check of the sources": ("status_cost_yellow_pct", "status_cost_red_pct", "status_burn_gap_yellow_pct",
                                    "status_burn_gap_red_pct", "status_slip_yellow_days", "status_slip_red_days",
                                    "status_report_max_age_days"),
    "Forecast ranges": ("simulation_runs", "confidence_level_pct", "reference_class_min_projects"),
}
INT_RULES = {name for name, f in BusinessRules.model_fields.items() if f.annotation is int}


@dataclass
class Outcome:
    """Result of an admin action. kind: success, info, warning or error; `invalid` marks an error caused by the
    input (the API answers 422 instead of 409)."""
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    invalid: bool = False

    @property
    def ok(self) -> bool:
        return self.kind != "error"


def key_hint(has_key: bool) -> str:
    """Status only – no part of a key (not even the last characters) is ever sent to the browser."""
    return "Currently set. Leave empty to keep." if has_key else "Currently not set."


def source_path() -> Path | None:
    """The data source file (None without a file source, e.g. mapped sources)."""
    try:
        return get_data_provider().source_path
    except Exception:  # noqa: BLE001 - without a file source there is no hash to compare
        return None


# =========================================================================== system status
def routing_rows() -> list[dict[str, str]]:
    from app.core.runtime_config import AGENT_ROLES

    cfg = get_runtime_config()
    return [{"Agent role": r, "Model": cfg.model_for_role(r), "Shadow": cfg.shadow_model_for(r) or "–"}
            for r in AGENT_ROLES]


def auth_rows() -> list[dict[str, str]]:
    auth = get_runtime_config().auth
    return [
        {"Setting": "Failed attempts before an account is locked", "Value": f"{auth.max_failed_attempts}"},
        {"Setting": "Lockout duration", "Value": f"{auth.lockout_minutes:g} min"},
        {"Setting": "Session timeout without activity", "Value": f"{auth.session_timeout_minutes:g} min"},
        {"Setting": "Sign-in attempts per minute (all accounts)", "Value": f"{auth.max_attempts_per_minute}"},
    ]


def servicenow_rows() -> list[dict[str, Any]] | None:
    """Last sync per ServiceNow source and table; None without a ServiceNow source. Raises DataSourceError."""
    from app.core.formatting import timestamp

    connectors = servicenow_connectors()
    if not connectors:
        return None
    rows = []
    for connector in connectors:
        state = connector.state()
        for table in connector.specs:
            known = state.get("tables", {}).get(table, {})
            rows.append({"Instance": state.get("instance") or "–", "Table": table, "Rows": known.get("rows"),
                         "Changed up to": known.get("watermark") or "–", "Last sync": timestamp(state.get("synced_at"))
                         if state.get("synced_at") else "never"})
    return rows


def run_servicenow_sync(full: bool, user: str | None, role: str) -> Outcome:
    try:
        reports = sync_servicenow(full=full)
    except DataSourceError as exc:
        audit("servicenow_sync_failed", role=role, user_id=user, error=str(exc)[:300])
        return Outcome("error", f"ServiceNow sync failed: {exc}")
    summary = " ".join(r.summary() for r in reports)
    if not all(r.ok for r in reports):  # a failed source left its snapshot unchanged
        audit("servicenow_sync_failed", role=role, user_id=user, full=full, error=summary[:1000])
        return Outcome("error", summary)
    audit("servicenow_sync", role=role, user_id=user, full=full, summary=summary[:1000])
    return Outcome("warning" if any(r.warnings for r in reports) else "success", summary)


def journal_findings() -> list[dict[str, Any]]:
    return journal.reconcile_operations(source_path())


def resolve_journal_finding(operation_id: str, kind: str, user: str, role: str, comment: str) -> Outcome:
    finding = next((f for f in journal_findings() if f["operation_id"] == operation_id and f["kind"] == kind), None)
    if finding is None:
        return Outcome("error", "Finding not found – it may have been resolved already.", {"not_found": True})
    try:
        journal.mark_resolved(finding, user, role, comment)
    except ValueError as exc:
        return Outcome("error", str(exc), invalid=True)
    except logs.AuditWriteError as exc:
        return Outcome("error", str(exc))
    return Outcome("success", "Finding marked as resolved.")


# =========================================================================== model settings
def matrix_rows() -> list[dict[str, str]]:
    cfg = get_runtime_config()
    return [{"agent": AGENT_LABELS[a], "main": cfg.model_for_role(a), "shadow": cfg.shadow_model_for(a) or NO_SHADOW}
            for a in AGENTS]


def model_options() -> list[str]:
    cfg = get_runtime_config()
    return sorted(set(model_ids()) | {cfg.model_for_role(a) for a in AGENTS}
                  | {cfg.shadow_model_for(a) for a in AGENTS if cfg.shadow_model_for(a)})


def thinking_models_in_use() -> list[str]:
    cfg = get_runtime_config()
    return sorted({cfg.model_for_role(a) for a in AGENTS if is_thinking_model(cfg.model_for_role(a))})


def unknown_models(agents: dict[str, dict[str, str]]) -> list[str]:
    """Models of a matrix change that are neither in the catalog nor in use (the console offers only these)."""
    known = set(model_options())
    return sorted({str(m).strip() for entry in agents.values() for k, m in entry.items()
                   if k in ("main", "shadow") and str(m or "").strip() and str(m).strip() not in known})


def save_settings(changes: dict[str, Any], user: str | None, role: str) -> Outcome:
    """Validates, applies, persists to the active environment file and audits (without secrets). `changes` holds the
    runtime settings to change; an API key is only included when a new one was entered."""
    before = get_runtime_config()
    try:
        update_runtime_config(**changes, role=role)
        persist_runtime_config(updated_by=user)
    except ValueError as exc:
        return Outcome("error", f"Settings not applied: {exc}", invalid=True)
    except OSError as exc:
        return Outcome("error", f"Settings applied in memory but could not be saved to the environment file: {exc}")
    after = get_runtime_config()
    changed = [k for k in ("temperature", "pii_masking_enabled", "orchestrator_prompt") if getattr(before, k) != getattr(after, k)]
    changed += [f"{a}.{v}" for a in AGENTS for v in ("main", "shadow")
                if before.agent_map[a][v] != after.agent_map[a][v]]
    changed += [k for k in ("openai_api_key", "anthropic_api_key", "google_api_key") if k in changes]
    audit("admin_settings_changed", role=ROLE_ADMIN, user_id=user, environment=after.environment,
          changed={k: ("***" if k.endswith("api_key") else str(getattr(after, k, after.agent_map.get(k.split(".")[0], {}).get(k.split(".")[-1], ""))))
                   for k in changed})
    log_security_event("Admin settings changed", user=user, env=after.environment, fields=",".join(changed) or "none")
    if "pii_masking_enabled" in changed:
        log_security_event("PII masking " + ("enabled" if after.pii_masking_enabled else "DISABLED") + " via Admin Console",
                           level=logging.INFO if after.pii_masking_enabled else logging.WARNING, user=user)
    return Outcome("success", f"Settings saved to {env_config_path(after.environment).name} ({len(changed)} change(s)). "
                              "They apply immediately to all new analyses.", {"changed": changed})


def reload_settings(user: str | None) -> Outcome:
    from app.services import clear_llm_caches

    reset_runtime_config()
    clear_llm_caches()  # cached clients of old keys are dropped
    audit("admin_settings_reloaded", role=ROLE_ADMIN, user_id=user)
    log_security_event("Admin settings reloaded from .env and environment file", user=user)
    return Outcome("success", "Settings reloaded from .env (keys) and the environment file.")


def clear_caches(user: str | None) -> Outcome:
    """Drops the cached model clients (e.g. after a key was rotated on the host)."""
    from app.services import clear_llm_caches

    clear_llm_caches()
    log_security_event("LLM client caches cleared", user=user)
    return Outcome("success", "Cached model clients cleared – the next call opens new ones.")


def model_rows() -> list[dict[str, Any]]:
    return [{"model_id": m.model_id, "provider": m.provider, "thinking": m.is_thinking_model} for m in load_models()]


def test_model(model_id: str, thinking: bool) -> Outcome:
    """One live test call without touching the registry."""
    from app.services import test_model_connection

    model_id = (model_id or "").strip()
    ok, message = test_model_connection(model_id, thinking)
    if not ok:
        logger.warning("Model validation failed for %s: %s", model_id, message)
        return Outcome("error", message)
    return Outcome("success", message)


test_model.__test__ = False  # not a pytest test despite its name


def test_and_add_model(model_id: str, thinking: bool, user: str | None) -> Outcome:
    """One live test call, then the model goes into the registry (or is updated there)."""
    from app.services import test_model_connection

    model_id = (model_id or "").strip()
    ok, message = test_model_connection(model_id, thinking)
    if not ok:
        logger.warning("Model validation failed for %s: %s", model_id, message)
        return Outcome("error", message)
    existed = get_model(model_id) is not None
    try:
        spec = add_model(model_id, thinking)
    except (OSError, ValueError) as exc:
        return Outcome("error", f"Test succeeded, but the registry could not be updated: {exc}")
    audit("model_added", role=ROLE_ADMIN, user_id=user, model_id=spec.model_id, provider=spec.provider,
          is_thinking_model=spec.is_thinking_model, updated=existed)
    log_security_event("Model registry updated", user=user, model=spec.model_id, thinking=spec.is_thinking_model)
    return Outcome("success", f"{message} `{spec.model_id}` {'updated' if existed else 'added'} – it is now available "
                              "in the model dropdowns.", {"model_id": spec.model_id, "provider": spec.provider,
                                                          "thinking": spec.is_thinking_model, "updated": existed})


test_and_add_model.__test__ = False  # not a pytest test despite its name


# =========================================================================== FinOps
def finops() -> dict[str, Any]:
    """Estimated LLM costs since app start / last reset, per model with the thinking flag and its cost share."""
    usage = cost_tracker.snapshot()
    by_model = sorted(({"model": m, "thinking": is_thinking_model(m), **{k: v for k, v in values.items() if k != "priced"}}
                       for m, values in usage["by_model"].items()), key=lambda r: r.get("cost_usd") or 0, reverse=True)
    thinking_cost = sum(r.get("cost_usd") or 0 for r in by_model if r["thinking"])
    return {**{k: v for k, v in usage.items() if k != "by_model"}, "by_model": by_model,
            "thinking_share": thinking_cost / usage["cost_usd"] if usage["cost_usd"] else 0.0}


def reset_costs(user: str | None) -> Outcome:
    cost_tracker.reset()
    audit("finops_counters_reset", role=ROLE_ADMIN, user_id=user)
    return Outcome("success", "FinOps counters reset.")


# =========================================================================== notifications
def notification_status() -> dict[str, Any]:
    """On/off, the configured channels (no secret, no address) and the outbox in counts."""
    from app.core import notifications

    status = notifications.status()
    smtp = status["smtp"]
    mail = (f"{smtp['host']}:{smtp['port']} · from {smtp['sender']} · STARTTLS {'on' if smtp['starttls'] else 'off'} · "
            f"login {'set' if smtp['login'] else 'not set'}" if smtp["configured"] else "not configured")
    status["channels"] = [
        {"channel": "Mail (SMTP)", "status": mail},
        {"channel": "Teams · PMO", "status": "webhook set" if status["teams_pmo"] else "not set"},
        {"channel": "Teams · Portfolio Board", "status": "webhook set" if status["teams_board"] else "not set"},
        {"channel": "PMO mailbox (NOTIFY_PMO_TO)", "status": f"{status['pmo_mailboxes']} address(es)"},
        {"channel": "Board mailbox (NOTIFY_BOARD_TO)", "status": f"{status['board_mailboxes']} address(es)"},
        {"channel": "Accounts with a mail address", "status": f"{status['accounts_with_email']} (user file)"},
        {"channel": "Links into the app (APP_BASE_URL)",
         "status": status["app_base_url"] or "not set – messages without a link"},
    ]
    return status


def send_test_notification(to: str, user: str | None) -> Outcome:
    """One test message per configured channel, sent right away; audited without the address."""
    from app.core import notifications

    try:
        result = notifications.send_test(to)
    except notifications.NotificationError as exc:
        audit("notification_test_failed", role=ROLE_ADMIN, user_id=user, error=str(exc)[:200])
        return Outcome("error", str(exc), invalid=isinstance(exc, notifications.InvalidAddress))
    audit("notification_test_sent", role=ROLE_ADMIN, user_id=user, channels=result["channels"], sent=result["sent"],
          failed=result["failed"])
    if not result["failed"]:
        return Outcome("success", f"Test notification sent ({result['sent']} message(s)).", result)
    # sent but not delivered: a warning, so the console shows the error in the outbox status right away
    return Outcome("warning", f"{result['sent']} test message(s) sent, {result['failed']} failed: "
                              + "; ".join(result["errors"]), result)


# =========================================================================== environment
def switch_env(target: str, user: str | None, role: str) -> Outcome | None:
    """Loads the target environment's file. None when it is already active."""
    from app.core import auth

    before = get_runtime_config().environment
    if target == before:
        return None
    if not auth.demo_allowed(target) and auth.using_demo_users():
        # PROD refuses the demo accounts – switching would lock every new session out until the app restarts
        log_security_event("Environment switch refused – demo accounts active", level=logging.WARNING,
                           source=before, target=target, user=user)
        return Outcome("error", f"{target} needs real accounts: set AUTH_USERS_FILE on the host first. "
                                f"The environment stays {before}.")
    try:
        cfg = switch_environment(target, role=role)
    except (ValueError, OSError) as exc:
        return Outcome("error", f"Environment switch failed: {exc}", invalid=isinstance(exc, ValueError))
    audit("environment_switched", role=ROLE_ADMIN, user_id=user, source=before, target=target)
    log_security_event("Environment switched", level=logging.WARNING, source=before, target=target, user=user)
    return Outcome("success", f"Environment switched to {cfg.environment} – loaded {env_config_path(cfg.environment).name}.")


def promote(source: str, target: str, user: str | None, role: str) -> Outcome:
    """Copies the source environment's settings into the target's file; reloads it when the target is active."""
    try:
        backup = promote_env_config(source, target, updated_by=user)
    except (EnvConfigError, OSError) as exc:
        return Outcome("error", f"Promotion failed: {exc}")
    reloaded = get_runtime_config().environment == target
    if reloaded:  # the active environment was overwritten, so reload it
        switch_environment(target, role=role)
    audit("config_promoted", role=ROLE_ADMIN, user_id=user, source=source, target=target,
          backup=str(backup) if backup else None)
    log_security_event("Config promoted", level=logging.WARNING, source=source, target=target, user=user)
    return Outcome("success", f"{source} configuration promoted to {target}."
                              + (f" Previous {target} config archived: {backup.name}" if backup else ""),
                   {"reloaded": reloaded, "backup": backup.name if backup else None})


# =========================================================================== required fields
def required_field_options(columns: list[str], current: list[str]) -> list[str]:
    from app.core.required_fields import DEFAULT_REQUIRED_FIELDS

    return sorted(set(columns) | set(current) | set(DEFAULT_REQUIRED_FIELDS))


def missing_calc_fields(current: list[str]) -> list[str]:
    return [f for f in CALC_FIELDS if f not in current]


def save_required(fields: list[str], user: str | None, role: str, pmo_head: bool = False) -> Outcome:
    """Governance rule: only the PMO Head may change it (core check in update_runtime_config)."""
    before = load_required_fields()
    try:
        final = save_required_fields(fields, updated_by=user, role=role, pmo_head=pmo_head)
    except PermissionError:  # an OSError too: never reported as a failed write
        raise
    except OSError as exc:
        return Outcome("error", f"Required fields could not be saved: {exc}")
    added, removed = sorted(set(final) - set(before)), sorted(set(before) - set(final))
    audit("required_fields_changed", role=role or ROLE_PMO, user_id=user, environment=get_runtime_config().environment,
          added=added, removed=removed, required_fields=final)
    log_security_event("Data Steward required fields changed", user=user, added=",".join(added) or "-",
                       removed=",".join(removed) or "-")
    locked = f" Identity fields always remain required: {', '.join(LOCKED_FIELDS)}." if set(LOCKED_FIELDS) - set(fields) else ""
    return Outcome("success", f"{len(final)} required fields saved – the Data Steward validates against them now.{locked}",
                   {"required_fields": final, "added": added, "removed": removed})


def reset_required(user: str | None, role: str, pmo_head: bool = False) -> Outcome:
    try:
        reset_required_fields(updated_by=user, role=role, pmo_head=pmo_head)
    except PermissionError:  # an OSError too: never reported as a failed write
        raise
    except OSError as exc:
        return Outcome("error", f"Required fields could not be reset: {exc}")
    audit("required_fields_reset", role=role or ROLE_PMO, user_id=user)
    log_security_event("Required fields reset to default", user=user)
    return Outcome("success", "Required fields reset to the default.")


# =========================================================================== database
def import_report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows read, loaded and skipped per table (no project content)."""
    return [{"Table": table, "Source": t["source"], "Read": t["read"], "Loaded": t["loaded"],
             "Skipped": t["dropped"], "Notes": len(t["warnings"])} for table, t in report["tables"].items()]


def import_report() -> dict[str, Any] | None:
    """The import report of the last load. Raises DataSourceError."""
    return get_data_provider().load_dataset().metadata.get("import_report")


def rebuild_graph(user: str | None, data_source: str) -> Outcome:
    try:
        counts = rebuild().counts()
    except (GraphUnavailable, DataSourceError) as exc:
        logger.error("Graph rebuild failed: %s", exc)
        return Outcome("error", f"Rebuild failed: {exc}")
    audit("graph_rebuilt", role=ROLE_ADMIN, user_id=user, counts=counts, source=data_source)
    log_security_event("Dependency graph rebuilt", user=user, source=data_source,
                       projects=counts.get("Project"), tasks=counts.get("Task"))
    source = {"json": "JSON", "excel": "Excel", "mapped": "the mapped sources"}.get(data_source, data_source)
    return Outcome("success", f"Dependency graph built from {source}: {counts.get('Project', 0)} projects, "
                              f"{counts.get('Task', 0)} tasks, {counts.get('Team', 0)} teams, "
                              f"{counts.get('Risk', 0)} risks, {sum(v for k, v in counts.items() if k.isupper())} "
                              "relationships.", {"counts": counts})


def reseed_graph() -> str:
    """Forces a rebuild of the dependency graph from the (restored) source. Returns a note for the message; the
    caller clears its data caches."""
    try:
        counts = rebuild().counts()
        return f"Dependency graph rebuilt ({counts.get('Project', 0)} projects)."
    except Exception as exc:  # noqa: BLE001 - data is restored; the graph rebuilds itself on next access
        logger.error("Graph rebuild after snapshot restore failed: %s", exc)
        return f"Graph rebuild failed ({exc}) – it will be retried automatically on next access."


# =========================================================================== MCP integrations
def mcp_status(env: str) -> dict[str, Any]:
    """Stored MCP settings and per server whether its token is set (never the token)."""
    stored = load_mcp_config(env)
    return {"enabled": stored["enabled"], "servers": [
        {"name": name, "label": label, "url": stored["servers"].get(name, ""), "token_env": f"{name.upper()}_MCP_TOKEN",
         "token_set": bool(os.getenv(f"{name.upper()}_MCP_TOKEN"))} for name, label in MCP_SERVERS.items()]}


def save_mcp(enabled: bool, servers: dict[str, str], user: str | None) -> Outcome:
    env = get_runtime_config().environment
    before = load_mcp_config(env)
    try:
        after = save_mcp_config(env, enabled, servers, updated_by=user)
    except ValueError as exc:
        return Outcome("error", f"Integrations not saved: {exc}", invalid=True)
    except OSError as exc:
        return Outcome("error", f"Integrations could not be written to the environment file: {exc}")
    changed = [k for k in ("enabled",) if before[k] != after[k]] + [f"servers.{n}" for n in MCP_SERVERS
                                                                    if before["servers"][n] != after["servers"][n]]
    audit("mcp_settings_changed", role=ROLE_ADMIN, user_id=user, environment=env, changed=changed, enabled=after["enabled"])
    log_security_event("MCP integration settings changed", level=logging.WARNING if after["enabled"] else logging.INFO,
                       user=user, env=env, enabled=after["enabled"], fields=",".join(changed) or "none")
    return Outcome("success", f"Integration settings saved to {env_config_path(env).name} ({len(changed)} change(s)).",
                   {"changed": changed})


def probe_mcp(name: str, user: str | None) -> tuple[bool, str]:
    from app.crew.tools.mcp_client import EnterpriseMCPConnector

    connector = EnterpriseMCPConnector.from_environment(get_runtime_config().environment)
    ok, message = connector.probe(name)
    audit("mcp_connection_tested", role=ROLE_ADMIN, user_id=user, server=name, ok=ok)
    return ok, message


# =========================================================================== business rules
def rule_bounds(name: str) -> tuple[float | None, float | None]:
    low = high = None
    for meta in BusinessRules.model_fields[name].metadata:
        low, high = getattr(meta, "ge", low), getattr(meta, "le", high)
    return low, high


def rule_rows() -> list[dict[str, Any]]:
    """Every business rule with its group, label, unit, help, bounds, default and current value."""
    current, defaults = get_runtime_config().business_rules, BusinessRules()
    group_of = {name: title for title, names in RULE_GROUPS.items() for name in names}
    rows = []
    for name, (label, unit, help_text) in RULE_LABELS.items():
        low, high = rule_bounds(name)
        rows.append({"name": name, "group": group_of.get(name), "label": label, "unit": unit, "help": help_text,
                     "integer": name in INT_RULES, "min": low, "max": high, "default": getattr(defaults, name),
                     "value": getattr(current, name)})
    return rows


def apply_business_rules(values: dict[str, Any], event: str, user: str | None, role: str,
                         pmo_head: bool = False) -> Outcome:
    """Validates (bounds), applies, persists and audits; only the PMO Head may change the rules. A changed team capacity limit rebuilds the graph (overloaded
    months are materialised there); data["reseeded"] tells the caller to clear its data caches."""
    before = get_runtime_config().business_rules
    try:
        after = update_runtime_config(business_rules=values, role=role, pmo_head=pmo_head).business_rules
        persist_runtime_config(updated_by=user)
    except ValueError as exc:
        return Outcome("error", f"Business rules not saved: {exc}", invalid=True)
    except PermissionError:  # an OSError too: never reported as a failed write
        raise
    except OSError as exc:
        return Outcome("error", f"Business rules applied in memory but not saved to the environment file: {exc}")
    changed = {k: {"old": getattr(before, k), "new": getattr(after, k)} for k in RULE_LABELS
               if getattr(before, k) != getattr(after, k)}
    audit(event, role=role or ROLE_PMO, user_id=user, environment=get_runtime_config().environment, changed=changed)
    log_security_event("Business rules changed", user=user, fields=",".join(changed) or "none")
    note = ""
    reseeded = "team_capacity_limit_pct" in changed
    if reseeded:
        note = " " + reseed_graph()
    return Outcome("success", f"{len(changed)} business rule(s) saved to {env_config_path(get_runtime_config().environment).name}. "
                              f"They apply to all new analyses and verdicts.{note}",
                   {"changed": changed, "reseeded": reseeded})


def save_business_rules(values: dict[str, Any], user: str | None, role: str, pmo_head: bool = False) -> Outcome:
    return apply_business_rules(values, "business_rules_changed", user, role, pmo_head)


def reset_business_rules(user: str | None, role: str, pmo_head: bool = False) -> Outcome:
    return apply_business_rules(BusinessRules().model_dump(), "business_rules_reset", user, role, pmo_head)


# =========================================================================== snapshots and data lifecycle
def snapshots_of_source() -> list[dict[str, Any]]:
    source = source_path()
    return [s for s in list_snapshots() if source is None or s.get("source") == source.name]


def take_snapshot(user: str | None) -> Outcome:
    try:
        entry = create_snapshot(get_data_provider().source_path, "manual", user)
    except (SnapshotError, TypeError) as exc:
        return Outcome("error", f"Snapshot failed: {exc}")
    audit("snapshot_created", role=ROLE_ADMIN, user_id=user, snapshot_id=entry["id"], source=entry["source"])
    return Outcome("success", f"Snapshot {entry['id']} of {entry['source']} created.", {"snapshot_id": entry["id"]})


def restore(snap_id: str, user: str | None, role: str) -> Outcome:
    """Restores a snapshot over the data source (the state before is snapshotted first) and reseeds the graph."""
    source = get_data_provider().source_path
    try:
        result = restore_snapshot(snap_id, source, user=user, role=role)
    except (SnapshotError, OSError) as exc:
        return Outcome("error", f"Restore failed: {exc}")
    note = reseed_graph()  # forced reseed: the graph must reflect the restored data immediately
    previous = result["previous_state"]["id"]
    audit("snapshot_restored", role=ROLE_ADMIN, user_id=user, snapshot_id=snap_id, source=source.name,
          previous_state_snapshot=previous)
    log_security_event("Data source restored from snapshot", level=logging.WARNING, user=user, snapshot=snap_id,
                       source=source.name, previous_state=previous)
    return Outcome("success", f"Snapshot {snap_id} restored over {source.name}. The state before the restore was saved as "
                              f"snapshot {previous} (restore it to undo). {note}",
                   {"previous_state_snapshot": previous, "reseeded": True})


def demo_reset_available() -> bool:
    from app.core.demo_reset import RESET_ENVIRONMENTS

    return get_runtime_config().environment in RESET_ENVIRONMENTS  # never offered in PROD


def reset_demo(user: str | None) -> Outcome:
    from app.core.demo_reset import DemoResetError, reset_demo_data

    env = get_runtime_config().environment
    try:
        result = reset_demo_data(get_data_provider().source_path, env, user)
    except (DemoResetError, OSError) as exc:
        return Outcome("error", f"Reset failed: {exc}")
    note = reseed_graph() if result["restored"] else ""
    audit("demo_data_reset", role=ROLE_ADMIN, user_id=user, environment=env, snapshot_id=result["restored"],
          previous_state_snapshot=result["previous_state"], archived=result["archived"])
    log_security_event("Demo data reset", level=logging.WARNING, user=user, environment=env)
    return Outcome("success", "Demo data reset to the shipped state"
                              + (f" (current state saved as snapshot {result['previous_state']})."
                                 if result["previous_state"] else " (portfolio data was unchanged).")
                              + (" Proposals, chats and saved what-ifs archived." if result["archived"] else "")
                              + (f" {note}" if note else ""),
                   {**result, "reseeded": bool(result["restored"])})


def purge(target: str, user: str | None, role: str) -> Outcome:
    """Erasure request for one user; only the pseudonym is logged and audited, never the user id."""
    from app.core.purge import PurgeError, pseudonym, purge_user

    target = str(target or "").strip()
    try:
        result = purge_user(target, user, role)
    except (PurgeError, OSError) as exc:
        return Outcome("error", f"Purge failed: {exc}", invalid=isinstance(exc, PurgeError))
    alias = pseudonym(target)
    audit("user_purged", role=ROLE_ADMIN, user_id=user, purged=alias, **result)
    log_security_event("User purged", level=logging.WARNING, user=user, purged=alias)
    return Outcome("success", f"User purged as {alias}: {result['chats']} chat(s) and {result['scenarios']} "
                              f"saved what-if(s) removed, {result['proposals']} proposal record(s) and "
                              f"{result['archived']} archived record(s) pseudonymised.", {"pseudonym": alias, **result})


# =========================================================================== logs
def _read(path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        return logs.read_jsonl(path)
    except FileNotFoundError:
        return [], 0


def business_audit_lines(n: int = 200) -> list[str]:
    """Newest first; raises OSError if the file is unreadable."""
    try:
        return logs.tail_lines(logs.BUSINESS_AUDIT_LOG, n)
    except FileNotFoundError:
        return []


def analysis_runs(limit: int = 200) -> list[dict[str, Any]]:
    """Technical details of the latest analyses, newest first – no names, no request text."""
    records, _ = _read(logs.AUDIT_LOG)
    runs = [r for r in records if r.get("event") == "analysis_run"][-limit:][::-1]
    return [{"Time": r.get("timestamp"), "Environment": r.get("environment"), "Role": r.get("role"),
             "Mode": r.get("mode"), "Duration (s)": r.get("duration_s"), "Verdict": r.get("verdict") or "–",
             "Plan": json.dumps(r.get("plan") or [], ensure_ascii=False)} for r in runs]


def audit_records() -> tuple[list[dict[str, Any]], int]:
    """The decision log, newest first, with the number of corrupt lines skipped. Raises FileNotFoundError."""
    records, malformed = logs.read_jsonl(logs.AUDIT_LOG)
    if records and all("timestamp" in r for r in records):
        records = sorted(records, key=lambda r: str(r["timestamp"]), reverse=True)
    else:
        records = records[::-1]
    return records, malformed


def verify_audit_chain(user: str | None) -> dict[str, Any]:
    """Tamper evidence: every record of the decision log carries the hash of its predecessor."""
    check = logs.verify_chain(logs.AUDIT_LOG)
    if not check["ok"]:
        log_security_event("Audit hash chain broken", level=logging.WARNING, user=user, line=check["broken_at"])
    audit("audit_chain_verified", role=ROLE_ADMIN, user_id=user, ok=check["ok"], records=check["records"])
    return check


def incidents(limit: int = 200) -> tuple[list[dict[str, Any]], int]:
    records, malformed = _read(logs.INCIDENTS_LOG)
    return records[-limit:][::-1], malformed


def shadow_entries() -> tuple[list[dict[str, Any]], int]:
    records, malformed = _read(logs.SHADOW_LOG)
    return sorted(records, key=lambda r: str(r.get("timestamp", "")), reverse=True), malformed


def system_log_lines(level: str = "ALL", n: int = 100) -> list[str]:
    """The last lines of the system log, newest first, optionally of one level."""
    try:
        lines = logs.tail_lines(logs.APP_LOG, n)
    except FileNotFoundError:
        return []
    if level and level != "ALL":
        lines = [line for line in lines if f"| {level}" in line]
    return lines


def llm_call_log(n: int = 50) -> dict[str, Any]:
    """Per agent and model, and the last calls of this process (no prompt or answer text)."""
    from app.observability import llm_calls

    return {"summary": llm_calls.summary(), "recent": llm_calls.recent(n), "window": llm_calls.RECENT}


def clear_audit_log(user: str | None) -> Outcome:
    archived = logs.archive_and_clear(logs.AUDIT_LOG)
    log_security_event("Audit log cleared", level=logging.WARNING, user=user, archive=archived)
    return Outcome("success", f"Audit log cleared. Archive: {archived.name}" if archived else "Audit log was already empty.",
                   {"archive": archived.name if archived else None})


def clear_shadow_log(user: str | None) -> Outcome:
    archived = logs.archive_and_clear(logs.SHADOW_LOG)
    log_security_event("Shadow log cleared", user=user, archive=archived)
    return Outcome("success", "Shadow log cleared (archived)." if archived else "Shadow log was already empty.",
                   {"archive": archived.name if archived else None})


def clear_system_log(user: str | None) -> Outcome:
    archived = logs.clear_app_log()  # archives first; on OSError nothing is cleared
    log_security_event("System log cleared", level=logging.WARNING, user=user, archive=archived)
    return Outcome("success", f"System log cleared. Archive: {archived.name}" if archived
                   else "System log was already empty.", {"archive": archived.name if archived else None})


CLEARABLE_LOGS = {"audit": clear_audit_log, "shadow": clear_shadow_log, "system": clear_system_log}


def clear_log(name: str, user: str | None) -> Outcome:
    try:
        return CLEARABLE_LOGS[name](user)
    except OSError as exc:
        return Outcome("error", f"Clearing failed: {exc}")


# =========================================================================== system tests
def run_tests(user: str | None):
    """Runs the pytest suite (SystemTestRun) and audits it. Raises SystemTestsBusy while another run is active."""
    from app.core.system_tests import run_system_tests

    run = run_system_tests()
    audit("system_tests_run", role=ROLE_ADMIN, user_id=user, returncode=run.returncode, counts=run.counts,
          duration_s=round(run.duration_s, 1))
    return run
