"""Response models of the admin routes: the exact contract in /api/v1/openapi.json (the React front end generates its
types from it). Free-form log rows stay dict[str, Any]. No model carries an API key, a token or a password hash."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# =========================================================================== actions
class ActionResult(BaseModel):
    """Answer of an admin action. HTTP 200 for success/info/warning; an error answers 409 (or 422 for invalid input)
    with the same body. Action-specific fields are empty on an error."""
    kind: Literal["success", "info", "warning", "error"]
    message: str


class SettingsSaved(ActionResult):
    changed: list[str] = []


class ModelAdded(ActionResult):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str | None = None
    provider: str | None = None
    thinking: bool | None = None
    updated: bool | None = None  # True: an existing catalog entry was updated


class Promoted(ActionResult):
    reloaded: bool = False  # the target was the active environment and was reloaded
    backup: str | None = None  # archived file name of the previous target config


class RequiredFieldsSaved(ActionResult):
    required_fields: list[str] = []
    added: list[str] = []
    removed: list[str] = []


class McpSaved(ActionResult):
    changed: list[str] = []


class RuleChange(BaseModel):
    old: float
    new: float


class RulesSaved(ActionResult):
    changed: dict[str, RuleChange] = {}
    reseeded: bool = False  # the dependency graph was rebuilt (team capacity limit changed)


class GraphRebuilt(ActionResult):
    counts: dict[str, int] = {}


class SnapshotCreated(ActionResult):
    snapshot_id: str | None = None


class SnapshotRestored(ActionResult):
    previous_state_snapshot: str | None = None  # restore this one to undo
    reseeded: bool = False


class DemoReset(ActionResult):
    restored: str | None = None
    previous_state: str | None = None
    archived: list[str] = []
    reseeded: bool = False


class UserPurged(ActionResult):
    pseudonym: str | None = None
    chats: int = 0
    scenarios: int = 0
    proposals: int = 0
    incidents: int = 0
    notifications: int = 0
    archived: int = 0


class LogCleared(ActionResult):
    archive: str | None = None


class ProbeResult(BaseModel):
    ok: bool
    message: str


# =========================================================================== status and settings
class EnvironmentInfo(BaseModel):
    environment: str
    env_file: str
    config_issue: str | None = None  # the environment file is invalid: running on defaults


class GraphStatus(BaseModel):
    ok: bool
    message: str


class TracingInfo(BaseModel):
    enabled: bool
    endpoint: str
    message: str


class RoutingRow(BaseModel):
    agent_role: str
    model: str
    shadow: str


class EndpointRow(BaseModel):
    provider: str
    endpoint: str
    residency: str


class SettingRow(BaseModel):
    setting: str
    value: str


class GraphBackendInfo(BaseModel):
    configured: str  # networkx | neo4j (GRAPH_BACKEND)
    active: str  # the backend that answers now: networkx while Neo4j is not available
    warning: str | None  # Neo4j configured but not used, and why
    rows: list[SettingRow]
    note: str  # where the settings are made


class SystemStatus(EnvironmentInfo):
    data_source: str
    pii_masking_enabled: bool
    llm_available: bool
    missing_keys: list[str]
    shadow_enabled: bool
    shadow_available: bool
    graph: GraphStatus
    graph_backend: GraphBackendInfo
    tracing: TracingInfo
    routing: list[RoutingRow]
    temperature: float
    data_residency: str
    endpoints: list[EndpointRow]
    auth: list[SettingRow]


class AgentRow(BaseModel):
    agent: str
    label: str
    main: str
    shadow: str  # "" = no shadow model


class KeyStatus(BaseModel):
    set: bool
    key_hint: str


class SettingsView(EnvironmentInfo):
    agents: list[AgentRow]
    model_options: list[str]
    thinking_models_in_use: list[str]
    temperature: float
    pii_masking_enabled: bool
    orchestrator_prompt: str
    api_keys: dict[str, KeyStatus]
    missing_shadow_keys: list[str]


class ModelRow(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    provider: str
    thinking: bool


class ModelsView(BaseModel):
    models: list[ModelRow]
    registry_file: str


class PromotionPair(BaseModel):
    source: str
    target: str


class EnvironmentView(EnvironmentInfo):
    environments: list[str]
    demo_accounts_active: bool  # PROD refuses the switch while the demo accounts are active
    promotions: list[PromotionPair]


class DqProfile(BaseModel):
    tag: str
    severity: str
    meaning: str


class RequiredFieldsView(EnvironmentInfo):
    required_fields: list[str]
    options: list[str]
    locked: list[str]
    defaults: list[str]
    missing_calc_fields: list[str]
    data_quality_profiles: list[DqProfile]
    labels: dict[str, str] = {}  # column name -> its label (options, locked and missing fields)


class McpServer(BaseModel):
    name: str
    label: str
    url: str
    token_env: str
    token_set: bool


class McpView(EnvironmentInfo):
    enabled: bool
    servers: list[McpServer]


class RuleRow(BaseModel):
    name: str
    group: str | None
    label: str
    unit: str
    help: str
    integer: bool
    min: float | None
    max: float | None
    default: float
    value: float


class BusinessRulesView(EnvironmentInfo):
    rules: list[RuleRow]
    groups: dict[str, list[str]]
    verdict_rules_md: str


class AdminFiles(BaseModel):
    """Where the console's settings, data and logs live, relative to the project (the console names them as sources)."""
    env_file: str
    model_registry: str
    data_source: str | None
    snapshot_dir: str
    archive_dir: str
    audit_log: str
    business_audit_log: str
    shadow_log: str
    system_log: str
    incidents_log: str
    llm_calls_log: str
    test_report: str


# =========================================================================== operations
class ModelUsageRow(BaseModel):
    model: str
    thinking: bool
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class FinOpsView(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    unpriced_models: list[str]
    by_model: list[ModelUsageRow]
    thinking_share: float  # share of the estimated costs caused by thinking models


class SmtpChannel(BaseModel):
    configured: bool  # host and sender set
    host: str | None = None
    port: int
    sender: str | None = None
    starttls: bool
    login: bool  # NOTIFY_SMTP_USER set
    password_set: bool  # the password itself is never sent


class NotificationChannel(BaseModel):
    channel: str
    status: str


class NotificationFailure(BaseModel):
    at: str | None = None
    channel: str | None = None
    error: str | None = None


class NotificationsView(BaseModel):
    """Counts only: the System Admin sees no subjects, recipients or project data."""
    enabled: bool
    smtp: SmtpChannel
    teams_pmo: bool  # a webhook URL is set (the URL is a secret)
    teams_board: bool
    pmo_mailboxes: int
    board_mailboxes: int
    accounts_with_email: int
    app_base_url: str | None = None
    events_waiting: int  # recorded, recipients not resolved yet
    queued: int  # waiting for the first attempt or a retry
    retrying: int
    sent: int
    failed: int  # given up after the last attempt
    last_sent_at: str | None = None
    last_error: NotificationFailure | None = None
    channels: list[NotificationChannel] = Field(description="The console's channel table as shown")


class NotificationTest(ActionResult):
    channels: list[str] = []
    sent: int = 0
    failed: int = 0
    errors: list[str] = []


class ServiceNowRow(BaseModel):
    instance: str
    table: str
    rows: int | None
    changed_up_to: str
    last_sync: str


class ServiceNowView(BaseModel):
    configured: bool
    error: str | None
    tables: list[ServiceNowRow]


class JournalFinding(BaseModel):
    kind: str
    label: str
    operation_id: str | None
    snapshot_id: str | None = None
    proposal_id: str | None = None
    user_id: str | None = None
    timestamp: str | None = None
    detail: str | None = None


class JournalView(BaseModel):
    findings: list[JournalFinding]


class GraphMeta(BaseModel):
    counts: dict[str, int]
    built_at: str
    source: str | None
    build_seconds: float


class ImportRow(BaseModel):
    table: str
    source: str
    read: int
    loaded: int
    skipped: int
    notes: int


class ImportNote(BaseModel):
    table: str
    note: str


class ImportReport(BaseModel):
    mapping: str | None
    findings: int
    tables: list[ImportRow]
    notes: list[ImportNote]


class DatabaseView(BaseModel):
    data_source: str
    graph: GraphMeta | None  # None until the graph was built in this process
    import_report_error: str | None
    import_report: ImportReport | None


class SnapshotChange(BaseModel):
    project_id: str | None
    table: str | None
    field: str | None
    old: Any = None
    new: Any = None
    source_tool: str | None = None


class SnapshotRow(BaseModel):
    id: str
    created_at: str | None
    reason: str | None
    user: str | None
    environment: str | None
    size_kb: float
    changes: list[SnapshotChange]


class SnapshotsView(BaseModel):
    source: str | None
    max_snapshots: int
    demo_reset_available: bool
    snapshots: list[SnapshotRow]


class TestCaseRow(BaseModel):
    module: str
    test: str
    outcome: str
    duration_s: float
    message: str


class SavedTestRun(BaseModel):
    counts: dict[str, int]
    cases: list[TestCaseRow]
    saved_at: str


class SystemTestsView(BaseModel):
    enabled: bool
    last_run: SavedTestRun | None
    report_saved_at: str | None = None  # the HTML report of the last run (None: no report yet)


class SystemTestRunResult(BaseModel):
    passed: bool
    status: str
    returncode: int
    duration_s: float
    started_at: str
    counts: dict[str, int]
    cases: list[TestCaseRow]
    stdout: str
    stderr: str


# =========================================================================== logs
class LinesView(BaseModel):
    lines: list[str]  # newest first


class AnalysisRun(BaseModel):
    time: str | None
    environment: str | None
    role: str | None
    mode: str | None
    duration_s: float | None
    verdict: str
    plan: list[Any]


class AnalysisRunsView(BaseModel):
    runs: list[AnalysisRun]
    modes: list[str]
    total: int = 0  # runs before the mode filter
    median_s: float | None = None  # duration statistics of all listed runs (before the mode filter)
    p95_s: float | None = None


class AuditLogView(BaseModel):
    records: list[dict[str, Any]]  # free-form decision log records, newest first
    total: int
    events: list[str]
    malformed: int


class ChainCheck(BaseModel):
    ok: bool
    records: int
    chained: int
    unchained: int
    broken_at: int | None
    reason: str


class IncidentsView(BaseModel):
    incidents: list[dict[str, Any]]
    malformed: int


class MatrixRow(BaseModel):
    agent: str
    main: str
    shadow: str


class ShadowView(BaseModel):
    entries: list[dict[str, Any]]  # free-form main vs. shadow comparisons, newest first
    total: int
    malformed: int
    matrix: list[MatrixRow]
    shadow_available: bool


class LlmCallSummary(BaseModel):
    caller: str
    model: str
    calls: int
    errors: int
    median_s: float
    tokens: int
    cost_usd: float


class LlmCallsView(BaseModel):
    summary: list[LlmCallSummary]
    recent: list[dict[str, Any]]  # per call: model, caller, tokens, duration, outcome – never prompt or answer text
    window: int
