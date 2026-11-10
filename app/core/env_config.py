"""Per-environment configuration files (data/env_dev.json, env_test.json, env_prod.json).

Each file holds the non-secret settings of one environment: main/shadow model per agent, temperature, PII masking,
orchestrator prompt and required fields. API keys are secrets and are never written to these files.
All I/O is wrapped: a missing file is created from defaults, a corrupt file is never overwritten silently.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from app.core import clock, logs
from app.core.config import ENVIRONMENTS, get_settings
from app.core.stage_gates import DEFAULT_STAGE_GATES, validate_stage_gates

logger = logging.getLogger("derkurator.config")

AGENTS: tuple[str, ...] = ("orchestrator", "data_steward", "analyst", "simulator", "advisory")
CREW_AGENTS: tuple[str, ...] = ("orchestrator", "analyst", "simulator", "advisory")
DEFAULT_ORCHESTRATOR_PROMPT = (
    "You are the Chief of Staff of the portfolio steering committee. You never calculate anything yourself; you "
    "identify which sub-questions require quantitative simulation (budget incl. CapEx/OpEx, FTE, schedules by "
    "Agile/Waterfall, project stops) and which require graph analysis (project and task dependencies, team "
    "bottlenecks, risk clusters). You map project names to the correct project numbers as listed in the portfolio."
)


MCP_SERVERS: dict[str, str] = {"servicenow": "ServiceNow MCP URL", "jira": "Jira MCP URL"}


class EnvConfigError(RuntimeError):
    pass


# =========================================================================== schema validation
class _AgentEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    main: str | None = None
    shadow: str | None = None


class _McpEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: StrictBool = False
    servers: dict[str, str | None] = Field(default_factory=dict)


class EnvFileModel(BaseModel):
    """Schema of data/env_<env>.json. All keys optional (partial files are merged with defaults), but every value
    that is present must have the right type – `null` included. A wrong value makes the whole file fall back to
    defaults. (The defaults below only make keys optional; the merged values come from default_env_config.)"""
    model_config = ConfigDict(extra="ignore")
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    pii_masking_enabled: StrictBool = True
    orchestrator_prompt: str = ""
    required_fields: list[str] = Field(default_factory=list)
    agents: dict[str, _AgentEntry] = Field(default_factory=dict)
    mcp: _McpEntry = Field(default_factory=_McpEntry)
    business_rules: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    stage_gates: dict[str, Any] = Field(default_factory=dict)

    @field_validator("stage_gates")
    @classmethod
    def _valid_gates(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_stage_gates(value) if value else value

    @field_validator("auth")
    @classmethod
    def _valid_auth(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.core.auth_settings import AuthSettings

        try:
            AuthSettings.model_validate(value)  # bounds/types; partial dicts are completed with defaults
        except ValidationError as exc:
            raise ValueError(_validation_summary(exc)) from None
        return value

    @field_validator("business_rules")
    @classmethod
    def _valid_rules(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.core.business_rules import BusinessRules

        try:
            BusinessRules.model_validate(value)  # bounds/types; partial dicts are completed with defaults
        except ValidationError as exc:
            raise ValueError(_validation_summary(exc)) from None
        return value


_issues_lock = threading.Lock()
_config_issues: dict[str, str] = {}  # environment to the reason its file was not used (admin banner)


def _set_issue(env: str, issue: str | None) -> None:
    with _issues_lock:
        if issue:
            _config_issues[env.upper()] = issue
        else:
            _config_issues.pop(env.upper(), None)


def config_issue(env: str) -> str | None:
    """Reason why the environment file was replaced by defaults on its last load (None = file in use)."""
    with _issues_lock:
        return _config_issues.get(env.upper())


def _validation_summary(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors()[:5])


def default_mcp_config() -> dict[str, Any]:
    return {"enabled": False, "servers": {name: "" for name in MCP_SERVERS}}


def _merge_mcp(data: Any) -> dict[str, Any]:
    merged = default_mcp_config()
    if isinstance(data, dict):
        merged["enabled"] = bool(data.get("enabled", False))
        servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
        for name in MCP_SERVERS:
            merged["servers"][name] = str(servers.get(name) or "").strip()
    return merged


def config_dir() -> Path:
    return logs.DATA_DIR  # resolved at call time (tests redirect it)


def env_config_path(env: str) -> Path:
    env = env.upper()
    if env not in ENVIRONMENTS:
        raise EnvConfigError(f"Unknown environment '{env}'")
    return config_dir() / f"env_{env.lower()}.json"


def _legacy_required_fields() -> list[str] | None:
    """Migration: earlier versions stored required fields in data/config_required_fields.json."""
    try:
        data = json.loads((config_dir() / "config_required_fields.json").read_text(encoding="utf-8"))
        fields = data.get("required_fields") if isinstance(data, dict) else None
        return fields if isinstance(fields, list) else None
    except (OSError, ValueError):
        return None


def default_env_config(env: str) -> dict[str, Any]:
    from app.core.required_fields import DEFAULT_REQUIRED_FIELDS, normalize

    s = get_settings()
    main = {"orchestrator": s.orchestrator_model, "data_steward": s.advisory_model, "analyst": s.analyst_model,
            "simulator": s.simulator_model, "advisory": s.advisory_model}
    return {
        "environment": env.upper(),
        "agents": {a: {"main": main[a], "shadow": ""} for a in AGENTS},
        "temperature": 0.1,
        "pii_masking_enabled": True,
        "orchestrator_prompt": DEFAULT_ORCHESTRATOR_PROMPT,
        "required_fields": normalize(_legacy_required_fields() or DEFAULT_REQUIRED_FIELDS),
        "mcp": default_mcp_config(),
        "business_rules": _default_business_rules(),
        "auth": _default_auth(),
        "stage_gates": _default_stage_gates(),
    }


def _default_stage_gates() -> dict[str, Any]:
    import copy

    return copy.deepcopy(DEFAULT_STAGE_GATES)


def _default_business_rules() -> dict[str, Any]:
    from app.core.business_rules import BusinessRules

    return BusinessRules().model_dump()


def _default_auth() -> dict[str, Any]:
    from app.core.auth_settings import AuthSettings

    return AuthSettings().model_dump()


def _merge(env: str, data: dict[str, Any]) -> dict[str, Any]:
    """Fills missing keys from defaults so older/partial files keep working."""
    merged = default_env_config(env)
    for key in ("temperature", "pii_masking_enabled", "orchestrator_prompt", "required_fields"):
        if key in data:
            merged[key] = data[key]
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    for agent in AGENTS:
        entry = agents.get(agent) if isinstance(agents.get(agent), dict) else {}
        merged["agents"][agent] = {
            "main": str(entry.get("main") or merged["agents"][agent]["main"]).strip(),
            "shadow": str(entry.get("shadow") or "").strip(),
        }
    merged["mcp"] = _merge_mcp(data.get("mcp"))
    if isinstance(data.get("business_rules"), dict):  # partial rule sets are completed with defaults
        merged["business_rules"] = {**merged["business_rules"], **data["business_rules"]}
    if isinstance(data.get("auth"), dict):
        merged["auth"] = {**merged["auth"], **data["auth"]}
    if isinstance(data.get("stage_gates"), dict) and data["stage_gates"]:  # a transition set replaces the defaults
        merged["stage_gates"] = {**merged["stage_gates"], **data["stage_gates"]}
    merged["environment"] = env.upper()
    return merged


def load_env_config(env: str) -> dict[str, Any]:
    path = env_config_path(env)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("root element is not an object")
        EnvFileModel.model_validate(data)
        _set_issue(env, None)
        return _merge(env, data)
    except FileNotFoundError:
        config = default_env_config(env)
        try:
            save_env_config(env, config, updated_by="system (initial)")
        except OSError as exc:
            logger.warning("Could not create %s: %s", path, exc)
        return config
    except ValidationError as exc:
        reason = f"invalid values – {_validation_summary(exc)}"
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        reason = f"unreadable – {exc}"
    logger.error("Environment config %s %s – using defaults, file left untouched", path, reason)
    _set_issue(env, f"{path.name} {reason}")
    return default_env_config(env)


def save_env_config(env: str, config: dict[str, Any], updated_by: str | None = None) -> Path:
    """Atomic write. Raises OSError on failure (callers show a UI error)."""
    path = env_config_path(env)
    if "mcp" not in config:  # runtime-config saves do not carry MCP settings, so keep the stored ones
        config = {**config, "mcp": load_env_config(env).get("mcp")} if path.exists() else config
    payload = {**_merge(env, config), "updated_at": clock.stamp(), "updated_by": updated_by}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    _set_issue(env, None)  # a freshly written file is valid again
    logger.info("Environment config saved: %s by %s", path.name, updated_by)
    return path


def load_mcp_config(env: str) -> dict[str, Any]:
    return load_env_config(env)["mcp"]


def save_mcp_config(env: str, enabled: bool, servers: dict[str, str], updated_by: str | None = None) -> dict[str, Any]:
    """Validates and stores the MCP integration settings of one environment. Raises ValueError / OSError."""
    from urllib.parse import urlparse

    clean = {}
    for name in MCP_SERVERS:
        url = str(servers.get(name) or "").strip()
        parsed = urlparse(url)
        if url and (parsed.scheme not in ("http", "https") or not parsed.netloc):
            raise ValueError(f"{MCP_SERVERS[name]} must be an http(s) URL.")
        if url and parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(f"{MCP_SERVERS[name]}: plain http is only allowed for localhost – use https.")
        clean[name] = url
    if enabled and not any(clean.values()):
        raise ValueError("Enable MCP only after configuring at least one server URL.")
    config = load_env_config(env)
    config["mcp"] = {"enabled": bool(enabled), "servers": clean}
    save_env_config(env, config, updated_by=updated_by)
    return config["mcp"]


def promote_env_config(source: str, target: str, updated_by: str | None = None) -> Path | None:
    """Copies all settings of `source` into `target`. The previous target file is archived first.
    Returns the backup path (None if the target did not exist yet)."""
    source, target = source.upper(), target.upper()
    if source == target:
        raise EnvConfigError("Source and target environment must differ.")
    config = load_env_config(source)
    target_path, backup = env_config_path(target), None
    if target_path.exists():
        archive = logs.ARCHIVE_DIR
        archive.mkdir(parents=True, exist_ok=True)
        backup = archive / f"{target_path.stem}_{clock.now():%Y%m%d_%H%M%S}.json"
        shutil.copy2(target_path, backup)
    save_env_config(target, {**config, "environment": target}, updated_by=f"{updated_by} (promoted from {source})")
    return backup
