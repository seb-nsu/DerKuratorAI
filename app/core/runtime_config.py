"""Runtime configuration: API keys from .env (memory only), all other settings from the active environment file
(data/env_<env>.json). Changes via the Admin Console apply immediately and process-wide.

Process-wide and thread-safe on purpose (instead of st.session_state): the shadow crew and background jobs run in worker threads
and the backend must run without Streamlit. The Admin UI mirrors these values in its session state.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

from app.core.auth_settings import AuthSettings
from app.core.business_rules import BusinessRules, ValidationError
from app.core.config import ENVIRONMENTS, PROVIDER_KEY_NAMES, detect_provider, get_settings, residency_block
from app.core.env_config import (
    AGENTS,
    CREW_AGENTS,
    DEFAULT_ORCHESTRATOR_PROMPT,
    env_config_path,
    load_env_config,
    save_env_config,
)
from app.core.stage_gates import validate_stage_gates

_PLACEHOLDER_KEYS = {"", "sk-...", "sk-ant-...", "aiza...", "changeme"}
AGENT_ROLES = CREW_AGENTS  # agents that form the LLM crew (the Data Steward itself is deterministic)

__all__ = ["AGENTS", "AGENT_ROLES", "DEFAULT_ORCHESTRATOR_PROMPT", "RuntimeConfig", "get_runtime_config",
           "update_runtime_config", "reset_runtime_config", "switch_environment", "persist_runtime_config"]


@dataclass(frozen=True)
class RuntimeConfig:
    openai_api_key: str
    anthropic_api_key: str
    google_api_key: str = ""
    environment: str = "DEV"
    # (agent, main model, shadow model); an empty shadow means no shadow model for this agent
    agent_models: tuple[tuple[str, str, str], ...] = ()
    temperature: float = 0.1
    pii_masking_enabled: bool = True
    orchestrator_prompt: str = DEFAULT_ORCHESTRATOR_PROMPT
    required_fields: tuple[str, ...] = field(default_factory=tuple)
    business_rules: BusinessRules = field(default_factory=BusinessRules)  # governance config (business rules)
    auth: AuthSettings = field(default_factory=AuthSettings)  # lockout and session timeout of the sign-in
    stage_gates: dict[str, Any] = field(default_factory=dict)  # gate criteria per phase transition (A4)

    # ------------------------------------------------------------------ keys
    def api_key_for(self, provider: str) -> str:
        return {"openai": self.openai_api_key, "anthropic": self.anthropic_api_key, "google": self.google_api_key}[provider]

    def has_key_for(self, provider: str) -> bool:
        return self.api_key_for(provider).strip().lower() not in _PLACEHOLDER_KEYS

    def missing_key_for_model(self, model: str) -> str | None:
        provider = detect_provider(model)
        if not self.has_key_for(provider):
            return PROVIDER_KEY_NAMES[provider]
        return residency_block(provider)  # data residency "eu": a provider without an EU endpoint counts as missing

    # ------------------------------------------------------------------ models
    @property
    def agent_map(self) -> dict[str, dict[str, str]]:
        return {a: {"main": m, "shadow": s} for a, m, s in self.agent_models}

    def model_for_role(self, role: str, variant: str = "main") -> str:
        if role not in AGENTS:
            raise ValueError(f"Unknown agent role: {role}")
        entry = self.agent_map.get(role, {"main": "", "shadow": ""})
        if variant == "shadow":
            return entry["shadow"] or entry["main"]  # agents without a shadow model reuse their main model
        return entry["main"]

    def shadow_model_for(self, role: str) -> str:
        return self.agent_map.get(role, {}).get("shadow", "")

    def missing_keys_for_models(self, variant: str = "main") -> list[str]:
        return sorted({k for role in CREW_AGENTS if (k := self.missing_key_for_model(self.model_for_role(role, variant)))})

    @property
    def llm_available(self) -> bool:
        return not get_settings().force_offline_mode and not self.missing_keys_for_models()

    @property
    def shadow_enabled(self) -> bool:
        return any(self.shadow_model_for(a) for a in CREW_AGENTS)

    @property
    def shadow_available(self) -> bool:
        return self.shadow_enabled and not self.missing_keys_for_models("shadow")

    # ------------------------------------------------------------------ backwards-compatible accessors
    @property
    def orchestrator_model(self) -> str:
        return self.model_for_role("orchestrator")

    @property
    def analyst_model(self) -> str:
        return self.model_for_role("analyst")

    @property
    def simulator_model(self) -> str:
        return self.model_for_role("simulator")

    @property
    def advisory_model(self) -> str:
        return self.model_for_role("advisory")

    @property
    def data_steward_model(self) -> str:
        return self.model_for_role("data_steward")

    def to_env_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "agents": self.agent_map,
            "temperature": self.temperature,
            "pii_masking_enabled": self.pii_masking_enabled,
            "orchestrator_prompt": self.orchestrator_prompt,
            "required_fields": list(self.required_fields),
            "business_rules": self.business_rules.model_dump(),
            "auth": self.auth.model_dump(),
            "stage_gates": dict(self.stage_gates),
        }


def _agent_tuple(agents: dict[str, dict[str, str]]) -> tuple[tuple[str, str, str], ...]:
    return tuple((a, str(agents[a]["main"]).strip(), str(agents[a].get("shadow", "")).strip()) for a in AGENTS)


def _build(env: str, keys: dict[str, str]) -> RuntimeConfig:
    from app.core.required_fields import normalize

    data = load_env_config(env)
    return RuntimeConfig(
        **keys,
        environment=env,
        agent_models=_agent_tuple(data["agents"]),
        temperature=float(data["temperature"]),
        pii_masking_enabled=bool(data["pii_masking_enabled"]),
        orchestrator_prompt=str(data["orchestrator_prompt"]).strip() or DEFAULT_ORCHESTRATOR_PROMPT,
        required_fields=tuple(normalize(data["required_fields"])),
        business_rules=BusinessRules.model_validate(data.get("business_rules") or {}),
        auth=AuthSettings.model_validate(data.get("auth") or {}),
        stage_gates=dict(data.get("stage_gates") or {}),
    )


def _env_keys() -> dict[str, str]:
    s = get_settings()
    return {"openai_api_key": s.openai_api_key, "anthropic_api_key": s.anthropic_api_key, "google_api_key": s.google_api_key}


_lock = threading.RLock()
_config: RuntimeConfig | None = None
# Several worker processes share the settings through the environment file: a worker notices a file saved by another
# one (stamp = modification time and size) and reloads it, keeping its API keys. Checked at most once per second.
_stamp: tuple[int, int] | None = None
_checked_at = 0.0
CHECK_INTERVAL_S = 1.0


def _file_stamp(env: str) -> tuple[int, int] | None:
    try:
        stat = env_config_path(env).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _set(config: RuntimeConfig) -> RuntimeConfig:
    global _config, _stamp, _checked_at
    _config, _stamp, _checked_at = config, _file_stamp(config.environment), time.monotonic()
    return config


def get_runtime_config() -> RuntimeConfig:
    global _checked_at
    with _lock:
        if _config is None:
            return _set(_build(get_settings().environment, _env_keys()))
        if time.monotonic() - _checked_at >= CHECK_INTERVAL_S:
            _checked_at = time.monotonic()
            if _file_stamp(_config.environment) != _stamp:  # saved by another worker
                keys = {k: getattr(_config, k) for k in ("openai_api_key", "anthropic_api_key", "google_api_key")}
                return _set(_build(_config.environment, keys))
        return _config


def require_admin(role: str | None, action: str) -> None:
    """Core-side role check for configuration and restore actions. `role=None` is an in-process caller without a
    user (start-up, scripts, tests); every user-triggered call passes the session role, and only the System Admin
    passes. Raises PermissionError."""
    from app.core.security import ROLE_ADMIN, log_security_event  # local: security reads this module at import

    if role is not None and role != ROLE_ADMIN:
        log_security_event("Admin action refused", role=role or "-", action=action)
        raise PermissionError(f"{action} is reserved for the System Admin.")


GOVERNANCE_SETTINGS = frozenset({"business_rules", "required_fields"})  # owned by the PMO Head, not the System Admin


def require_pmo_head(role: str | None, pmo_head: bool, action: str) -> None:
    """Core-side check for the governance rules (business rules, required fields): only the PMO account flagged as
    PMO Head may change them. `role=None` is an in-process caller, as with require_admin. Raises PermissionError."""
    from app.core.security import ROLE_PMO, log_security_event  # local: security reads this module at import

    if role is not None and not (role == ROLE_PMO and pmo_head):
        log_security_event("PMO Head action refused", role=role or "-", action=action)
        raise PermissionError(f"{action} is reserved for the PMO Head.")


def update_runtime_config(*, role: str | None = None, pmo_head: bool = False, **changes: Any) -> RuntimeConfig:
    """Validates and applies changes atomically (in memory). Use persist_runtime_config() to save them.
    Model changes: `agents={"analyst": {"main": "...", "shadow": "..."}}` (partial updates allowed)."""
    global _config
    if not changes or set(changes) - GOVERNANCE_SETTINGS:
        require_admin(role, "Changing the settings")
    if set(changes) & GOVERNANCE_SETTINGS:
        require_pmo_head(role, pmo_head, "Changing the business rules")
    with _lock:
        current = get_runtime_config()
        agents = changes.pop("agents", None)
        unknown = set(changes) - set(RuntimeConfig.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        if "temperature" in changes:
            temp = float(changes["temperature"])
            if not 0.0 <= temp <= 1.0:
                raise ValueError("Temperature must be between 0.0 and 1.0.")
            changes["temperature"] = temp
        if "orchestrator_prompt" in changes and not str(changes["orchestrator_prompt"]).strip():
            changes["orchestrator_prompt"] = DEFAULT_ORCHESTRATOR_PROMPT
        if "environment" in changes:
            env = str(changes["environment"]).strip().upper()
            if env not in ENVIRONMENTS:
                raise ValueError(f"Unknown environment '{changes['environment']}' – allowed: {', '.join(ENVIRONMENTS)}")
            changes["environment"] = env
        if "required_fields" in changes:
            from app.core.required_fields import normalize

            changes["required_fields"] = tuple(normalize(changes["required_fields"]))
        if "business_rules" in changes:  # partial updates allowed; bounds are validated
            rules = changes["business_rules"]
            try:
                changes["business_rules"] = rules if isinstance(rules, BusinessRules) else current.business_rules.merged(dict(rules))
            except ValidationError as exc:
                raise ValueError("Invalid business rules: " + "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])) from None
        if "auth" in changes:  # partial updates allowed; bounds are validated
            auth = changes["auth"]
            try:
                changes["auth"] = auth if isinstance(auth, AuthSettings) else AuthSettings.model_validate(
                    {**current.auth.model_dump(), **dict(auth)})
            except ValidationError as exc:
                raise ValueError("Invalid sign-in settings: " + "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])) from None
        if "stage_gates" in changes:
            changes["stage_gates"] = dict(validate_stage_gates(changes["stage_gates"]))
        if agents:
            merged = current.agent_map
            for agent, entry in agents.items():
                if agent not in AGENTS:
                    raise ValueError(f"Unknown agent: {agent}")
                if "main" in entry and not str(entry["main"]).strip():
                    raise ValueError(f"Main model for {agent} must not be empty.")
                merged[agent] = {**merged[agent], **{k: str(v or "").strip() for k, v in entry.items() if k in ("main", "shadow")}}
            changes["agent_models"] = _agent_tuple(merged)
        _config = replace(current, **changes)  # in memory until persisted (the file stamp stays the old one)
        return _config


def persist_runtime_config(updated_by: str | None = None) -> None:
    """Writes the non-secret settings to the active environment file. Raises OSError on failure."""
    cfg = get_runtime_config()
    with _lock:
        save_env_config(cfg.environment, cfg.to_env_dict(), updated_by=updated_by)
        _set(cfg)


def switch_environment(env: str, role: str | None = None) -> RuntimeConfig:
    """Loads data/env_<env>.json; in-memory API keys are kept."""
    global _config
    require_admin(role, "Switching the environment")
    env = env.strip().upper()
    if env not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment '{env}'")
    with _lock:
        current = get_runtime_config()
        keys = {k: getattr(current, k) for k in ("openai_api_key", "anthropic_api_key", "google_api_key")}
        return _set(_build(env, keys))


def reset_runtime_config() -> RuntimeConfig:
    """Re-reads .env (keys, environment) and the settings from the environment file named there."""
    from app.core.config import reload_env

    reload_env()
    with _lock:
        return _set(_build(get_settings().environment, _env_keys()))


def pii_masking_enabled() -> bool:
    return get_runtime_config().pii_masking_enabled


def current_environment() -> str:
    """Lock-free read (used by the log filter; must never contend with the config lock)."""
    cfg = _config
    return cfg.environment if cfg is not None else get_settings().environment
