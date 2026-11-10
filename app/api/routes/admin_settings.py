"""Admin Console – system status and configuration: models per agent, model catalog, environment staging, required
fields, MCP integrations and business rules. System Admin only; required fields and business rules are read-only here
(the PMO Head changes them, see governance.py); API keys are write-only (the answers say only whether
a key is set)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api import admin_schemas as schemas
from app.api.admin_common import Confirmation, renamed, require_confirm, respond
from app.api.deps import admin, full_dataset
from app.core.auth import User, using_demo_users
from app.core.config import ENVIRONMENTS, endpoint_rows, get_settings
from app.core.env_config import AGENTS, MCP_SERVERS, config_issue, env_config_path
from app.core.formatting import field_label
from app.core.model_registry import registry_path
from app.core.required_fields import DEFAULT_REQUIRED_FIELDS, LOCKED_FIELDS, load_required_fields
from app.core.runtime_config import get_runtime_config
from app.views import admin as views

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin)])


def _env() -> dict:
    env = get_runtime_config().environment
    return {"environment": env, "env_file": env_config_path(env).name, "config_issue": config_issue(env)}


# =========================================================================== system status
@router.get("/status")
def system_status() -> schemas.SystemStatus:
    from app import services
    from app.observability.tracer import init_tracing

    settings, cfg = get_settings(), get_runtime_config()
    try:
        graph_ok, graph_message = services.graph_connection()
    except Exception as exc:  # noqa: BLE001 - the status page must answer without the graph
        graph_ok, graph_message = False, str(exc)
    try:
        backend = services.graph_backend_status()
    except Exception as exc:  # noqa: BLE001 - the status page must answer without the graph backend
        backend = {"configured": settings.graph_backend, "active": "networkx", "warning": str(exc), "rows": [],
                   "note": ""}
    tracing = init_tracing()
    return schemas.SystemStatus(
        **_env(), data_source=settings.data_source, pii_masking_enabled=cfg.pii_masking_enabled,
        llm_available=cfg.llm_available, missing_keys=cfg.missing_keys_for_models(),
        shadow_enabled=cfg.shadow_enabled, shadow_available=cfg.shadow_available,
        graph=schemas.GraphStatus(ok=graph_ok, message=graph_message),
        graph_backend=schemas.GraphBackendInfo(**{**backend, "rows": renamed(backend["rows"], {"Setting": "setting",
                                                                                              "Value": "value"})}),
        tracing=schemas.TracingInfo(enabled=tracing.enabled, endpoint=tracing.endpoint, message=tracing.message),
        routing=renamed(views.routing_rows(), {"Agent role": "agent_role", "Model": "model", "Shadow": "shadow"}),
        temperature=cfg.temperature, data_residency=settings.llm_data_residency,
        endpoints=renamed(endpoint_rows(), {"Provider": "provider", "Endpoint": "endpoint",
                                            "Under the residency setting": "residency"}),
        auth=renamed(views.auth_rows(), {"Setting": "setting", "Value": "value"}),
    )


def _shown(path) -> str:
    """A path relative to the project, or as configured when it lies outside (e.g. a separate log folder)."""
    from pathlib import Path

    from app.core.config import PROJECT_ROOT

    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


@router.get("/files")
def files() -> schemas.AdminFiles:
    from app.core import logs
    from app.core.snapshots import snapshot_dir

    source = views.source_path()
    return schemas.AdminFiles(
        env_file=_shown(env_config_path(get_runtime_config().environment)), model_registry=_shown(registry_path()),
        data_source=_shown(source) if source else None, snapshot_dir=_shown(snapshot_dir()),
        archive_dir=_shown(logs.ARCHIVE_DIR), audit_log=_shown(logs.AUDIT_LOG),
        business_audit_log=_shown(logs.BUSINESS_AUDIT_LOG), shadow_log=_shown(logs.SHADOW_LOG),
        system_log=_shown(logs.APP_LOG), incidents_log=_shown(logs.INCIDENTS_LOG),
        llm_calls_log=_shown(logs.LLM_CALLS_LOG), test_report=_shown(logs.TEST_REPORT))


# =========================================================================== models, prompt & shadowing
class AgentModels(BaseModel):
    main: str | None = Field(None, max_length=200)
    shadow: str | None = Field(None, max_length=200)  # "" = no shadow model


class SettingsUpdate(BaseModel):
    """Only the fields sent are changed. API keys are write-only: an empty or missing key keeps the stored one."""
    agents: dict[str, AgentModels] | None = None
    temperature: float | None = None
    pii_masking_enabled: bool | None = None
    orchestrator_prompt: str | None = Field(None, max_length=20000)
    openai_api_key: str | None = Field(None, max_length=1000)
    anthropic_api_key: str | None = Field(None, max_length=1000)
    google_api_key: str | None = Field(None, max_length=1000)


@router.get("/settings")
def settings_view() -> schemas.SettingsView:
    cfg = get_runtime_config()
    return schemas.SettingsView(
        **_env(),
        agents=[schemas.AgentRow(agent=a, label=views.AGENT_LABELS[a], main=cfg.model_for_role(a),
                                 shadow=cfg.shadow_model_for(a)) for a in AGENTS],
        model_options=views.model_options(), thinking_models_in_use=views.thinking_models_in_use(),
        temperature=cfg.temperature, pii_masking_enabled=cfg.pii_masking_enabled,
        orchestrator_prompt=cfg.orchestrator_prompt,
        api_keys={p: schemas.KeyStatus(set=cfg.has_key_for(p), key_hint=views.key_hint(cfg.has_key_for(p)))
                  for p in views.PROVIDERS},
        missing_shadow_keys=cfg.missing_keys_for_models("shadow") if cfg.shadow_enabled and not cfg.shadow_available
        else [],
    )


@router.put("/settings")
def save_settings(body: SettingsUpdate, response: Response, user: User = Depends(admin)) -> schemas.SettingsSaved:
    changes = body.model_dump(exclude_none=True, exclude={"agents", *(f"{p}_api_key" for p in views.PROVIDERS)})
    if body.agents is not None:
        agents = {a: m.model_dump(exclude_none=True) for a, m in body.agents.items()}
        if set(agents) - set(AGENTS):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown agent – allowed: {', '.join(AGENTS)}.")
        unknown = views.unknown_models(agents)
        if unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"Not in the model catalog: {', '.join(unknown)} – add the model first.")
        changes["agents"] = agents
    for provider in views.PROVIDERS:
        key = (getattr(body, f"{provider}_api_key") or "").strip()
        if key:
            changes[f"{provider}_api_key"] = key
    return respond(views.save_settings(changes, user.user_id, user.role), response, schemas.SettingsSaved)


@router.post("/settings/reload")
def reload_settings(body: Confirmation, response: Response, user: User = Depends(admin)) -> schemas.ActionResult:
    require_confirm(body)
    return respond(views.reload_settings(user.user_id), response)


@router.post("/caches/clear")
def clear_caches(response: Response, user: User = Depends(admin)) -> schemas.ActionResult:
    return respond(views.clear_caches(user.user_id), response, data_changed=True)


# =========================================================================== model catalog
class ModelRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    is_thinking_model: bool = False


@router.get("/models")
def models() -> schemas.ModelsView:
    return schemas.ModelsView(models=views.model_rows(), registry_file=registry_path().name)


@router.post("/models/test")
def test_model(body: ModelRequest, response: Response) -> schemas.ActionResult:
    return respond(views.test_model(body.model_id, body.is_thinking_model), response)


@router.post("/models")
def add_model(body: ModelRequest, response: Response, user: User = Depends(admin)) -> schemas.ModelAdded:
    return respond(views.test_and_add_model(body.model_id, body.is_thinking_model, user.user_id), response,
                   schemas.ModelAdded)


# =========================================================================== environment staging
class EnvironmentSwitch(Confirmation):
    environment: str = Field(min_length=1, max_length=10)


class Promotion(Confirmation):
    source: str = Field(min_length=1, max_length=10)
    target: str = Field(min_length=1, max_length=10)


@router.get("/environment")
def environment_view() -> schemas.EnvironmentView:
    return schemas.EnvironmentView(**_env(), environments=list(ENVIRONMENTS), demo_accounts_active=using_demo_users(),
                                   promotions=[schemas.PromotionPair(source=s, target=t) for s, t in views.PROMOTIONS])


@router.post("/environment")
def switch_environment(body: EnvironmentSwitch, response: Response,
                       user: User = Depends(admin)) -> schemas.ActionResult:
    """Tokens are bound to the environment: after a switch every caller (this one too) signs in again."""
    require_confirm(body)
    target = body.environment.strip().upper()
    if target not in ENVIRONMENTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown environment – allowed: {', '.join(ENVIRONMENTS)}.")
    outcome = views.switch_env(target, user.user_id, user.role)
    if outcome is None:
        outcome = views.Outcome("info", f"{target} is already the active environment.")
    return respond(outcome, response, data_changed=True)


@router.post("/environment/promote")
def promote(body: Promotion, response: Response, user: User = Depends(admin)) -> schemas.Promoted:
    require_confirm(body)
    pair = (body.source.strip().upper(), body.target.strip().upper())
    if pair not in views.PROMOTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "Allowed promotions: " + ", ".join(f"{s} to {t}" for s, t in views.PROMOTIONS) + ".")
    return respond(views.promote(*pair, user.user_id, user.role), response, schemas.Promoted, data_changed=True)


# =========================================================================== required fields
@router.get("/required-fields")
def required_fields() -> schemas.RequiredFieldsView:
    from app.crew.tools.data_steward_tools import DQ_PROFILE_TAGS

    current = load_required_fields()
    try:
        columns = list(full_dataset().projects.columns)  # column names only, never project content
    except Exception:  # noqa: BLE001 - settings must stay reachable without a readable data source
        columns = []
    return schemas.RequiredFieldsView(
        **_env(), required_fields=current, options=views.required_field_options(columns, current),
        locked=list(LOCKED_FIELDS), defaults=list(DEFAULT_REQUIRED_FIELDS),
        missing_calc_fields=views.missing_calc_fields(current),
        labels={f: field_label(f) for f in {*views.required_field_options(columns, current), *LOCKED_FIELDS,
                                             *views.missing_calc_fields(current)}},
        data_quality_profiles=[schemas.DqProfile(tag=tag, severity=sev, meaning=text)
                               for tag, (sev, text) in DQ_PROFILE_TAGS.items()])


# =========================================================================== MCP integrations
class McpSettings(BaseModel):
    enabled: bool
    servers: dict[str, str] = Field(default_factory=dict)  # server name -> URL; a server left out keeps its URL


@router.get("/mcp")
def mcp() -> schemas.McpView:
    return schemas.McpView(**_env(), **views.mcp_status(get_runtime_config().environment))


@router.put("/mcp")
def save_mcp(body: McpSettings, response: Response, user: User = Depends(admin)) -> schemas.McpSaved:
    if set(body.servers) - set(MCP_SERVERS):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown server – allowed: {', '.join(MCP_SERVERS)}.")
    stored = {s["name"]: s["url"] for s in views.mcp_status(get_runtime_config().environment)["servers"]}
    return respond(views.save_mcp(body.enabled, {**stored, **body.servers}, user.user_id), response, schemas.McpSaved)


@router.post("/mcp/{name}/probe")
def probe_mcp(name: str, user: User = Depends(admin)) -> schemas.ProbeResult:
    if name not in MCP_SERVERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown MCP server.")
    ok, message = views.probe_mcp(name, user.user_id)
    return schemas.ProbeResult(ok=ok, message=message)


# =========================================================================== business rules
@router.get("/business-rules")
def business_rules() -> schemas.BusinessRulesView:
    from app.crew.verdict import verdict_rules_md

    return schemas.BusinessRulesView(**_env(), rules=views.rule_rows(),
                                     groups={k: list(v) for k, v in views.RULE_GROUPS.items()},
                                     verdict_rules_md=verdict_rules_md())
