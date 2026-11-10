"""Role-based access control with row-level security, and PII pseudonymization."""
from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, Iterable

import pandas as pd

from app.core.runtime_config import pii_masking_enabled

if TYPE_CHECKING:
    from app.data_adapters.dataset import PortfolioDataset

logger = logging.getLogger("derkurator.security")
_last_pii_state: bool | None = None
_last_rls_scope: tuple | None = None


LOG_VALUE_MAX = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def log_safe(value: object) -> str:
    """A value as one log line: control characters (line breaks, ANSI escapes) replaced, capped at LOG_VALUE_MAX –
    a user id or request text cannot forge a second record."""
    text = _CONTROL_RE.sub("?", str(value))
    return text if len(text) <= LOG_VALUE_MAX else text[:LOG_VALUE_MAX - 1] + "…"


def log_security_event(event: str, level: int = logging.INFO, **details) -> None:
    """Uniform format for security events in data/app.log (greppable via 'SECURITY'). Every value is sanitised."""
    detail = ", ".join(f"{k}={log_safe(v)}" for k, v in details.items())
    logger.log(level, "SECURITY | %s%s", log_safe(event), f" | {detail}" if detail else "")


def log_refusal(event: str, *, user: str | None, role: str | None, reason: str,
                projects: Iterable[str] = (), fields: Iterable[str] = (), **extra) -> None:
    """One WARNING security event per refused write/approval attempt. Only the acting user, role, project IDs, field
    names and a short reason code are logged – never values, amounts or other people's names. Never raises: a failing
    log write must not turn a refusal into a different error (the refusal itself stays blocking)."""
    try:
        log_security_event(event, level=logging.WARNING, user=user or "-", role=role or "-",
                           projects=";".join(sorted({str(p) for p in projects})) or "-",
                           fields=";".join(sorted({str(f) for f in fields})) or "-", reason=reason, **extra)
    except Exception:  # noqa: BLE001, S110 - logging must never weaken or change the refusal
        pass


def _pii_enabled_logged() -> bool:
    """Returns the PII status and logs every state change exactly once."""
    global _last_pii_state
    enabled = pii_masking_enabled()
    if enabled != _last_pii_state:
        log_security_event("PII masking enabled" if enabled else "PII masking DISABLED – real names are sent to LLMs",
                           level=logging.INFO if enabled else logging.WARNING)
        _last_pii_state = enabled
    return enabled

ROLE_ADMIN = "System Admin"
ROLE_PMO = "PMO (Full Access)"
ROLE_PM = "Project Manager (Restricted)"
ROLE_BOARD = "Portfolio Board"  # reads the whole portfolio, approves only the annual plan
AVAILABLE_ROLES = (ROLE_PMO, ROLE_PM, ROLE_BOARD)  # roles with portfolio data access; the System Admin sees none
RLS_COLUMN = "project_manager"
DEMAND_RLS_COLUMN = "demand_owner"  # a Project Manager sees the demands they own
# Distinct PMO identities (sign-in label to user_id), so a PMO change can be approved by a second PMO
PMO_IDENTITIES: dict[str, str] = {ROLE_PMO: "PMO-ADMIN", "PMO (Full Access) – Deputy": "PMO-DEPUTY"}

# PII field to placeholder. The dict order is the priority when a person has several roles
# (e.g. PM on one project and task assignee on another is masked as [PM_MASKED]).
PII_FIELDS: dict[str, str] = {
    "project_manager": "[PM_MASKED]",
    "project_sponsor": "[SPONSOR_MASKED]",
    "additional_assignee": "[ASSIGNEE_MASKED]",
    "assigned_to": "[ASSIGNEE_MASKED]",
    "user": "[STAKEHOLDER_MASKED]",
    "owner": "[OWNER_MASKED]",  # Benefit_Plans.owner
    "demand_owner": "[PM_MASKED]",
}
EMAIL_PLACEHOLDER = "[EMAIL_MASKED]"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_pii_lock = threading.Lock()
_pii_registry: dict[str, str] = {}  # real name to placeholder
# Words of project names (casefolded): a surname that is also such a word ("Cloud", "Data") is only masked as part of
# the full name, never alone – otherwise project names like "Hybrid Hosting Platform" would be corrupted.
_protected_words: set[str] = set()
_WORD_RE = re.compile(r"[^\W\d_]+")


# --------------------------------------------------------------------------- RBAC
def apply_row_level_security(df: pd.DataFrame, role: str, user_id: str | None, column: str = RLS_COLUMN) -> pd.DataFrame:
    """The PMO and the Portfolio Board see all projects, a Project Manager only those with project_manager == user_id; deny by default."""
    if df is None or df.empty:
        return pd.DataFrame(columns=getattr(df, "columns", []))
    try:
        if role in (ROLE_PMO, ROLE_BOARD):
            return df.copy()
        if role == ROLE_PM:
            if not user_id or column not in df.columns:
                logger.warning("RLS: PM without user_id or column '%s' missing -> empty result", column)
                return df.iloc[0:0].copy()
            mask = df[column].astype(str).str.strip().str.casefold() == str(user_id).strip().casefold()
            return df.loc[mask].copy()
    except Exception:  # security errors must never lead to data leaks
        logger.exception("RLS filtering failed -> empty result")
        return df.iloc[0:0].copy()
    logger.warning("RLS: unknown role '%s' -> empty result", role)
    return df.iloc[0:0].copy()


def apply_rls_to_dataset(dataset: "PortfolioDataset", role: str, user_id: str | None) -> "PortfolioDataset":
    """Filters all linked tables (tasks, resources, risks, ...) to the visible projects."""
    global _last_rls_scope
    visible = apply_row_level_security(dataset.projects, role, user_id)
    scope = (role, user_id, len(visible), len(dataset.projects))
    if scope != _last_rls_scope:  # only log scope changes, not every Streamlit rerun
        log_security_event("RLS applied", role=role, user=user_id, visible=f"{len(visible)}/{len(dataset.projects)}")
        _last_rls_scope = scope
    demands = apply_row_level_security(dataset.demands, role, user_id, column=DEMAND_RLS_COLUMN) \
        if not dataset.demands.empty else dataset.demands
    return dataset.filter_projects(visible["project_number"].astype(str) if not visible.empty else [],
                                   demands["demand_number"].astype(str) if not demands.empty else [])


# Separation of duties: changes to these fields (budgets, business case figures, project stop/state)
# need a second person. Anyone proposes them, a PMO approves – never the same person.
# The schedule baseline and cost forecast/actuals are governed too – they drive the verdict rules.
GOVERNED_CHANGE_FIELDS = frozenset({
    "budget_cost_eur", "total_planned_cost_eur", "planned_capital_eur", "planned_operating_eur",
    "planned_benefit_eur", "planned_return_eur", "planned_roi_pct", "state",
    "planned_end_date", "estimate_at_completion_eur", "actual_cost_eur", "estimate_to_completion_eur",
    "net_present_value_eur", "phase", "approved_end_date",
})
# New rows of these tables are decisions themselves (a new baseline, a stage-gate approval, a reserve booking) –
# always four-eyes, like budgets, dates, state and phase
GOVERNED_TABLES = frozenset({"Baselines", "Approvals", "Reserve_Ledger"})
# Moving a row to another project (a merge) re-assigns plans and dependencies – four-eyes in every table
GOVERNED_ROW_FIELDS = frozenset({"project_number", "predecessor_project_number", "successor_project_number"})
# Criterion weights decide which demands get a budget – a weight change is four-eyes and in the audit trail
GOVERNED_WEIGHT_TABLES = frozenset({"Scoring_Model"})


# The annual plan: an approved scenario and the demands it approves are decided by the Portfolio Board only
PLAN_TABLES = frozenset({"Planning_Scenarios", "Planning_Items"})
PLAN_APPROVED = "Approved"


def is_plan_decision(change: dict) -> bool:
    """A change only the Portfolio Board approves: the Approved rows of a plan and a demand set to Approved."""
    table = change.get("table")
    if table in PLAN_TABLES:
        return (change.get("row") or {}).get("scenario_status") == PLAN_APPROVED
    return table == "Demands" and change.get("field") == "demand_status" and change.get("new") == PLAN_APPROVED


def is_plan_change(change: dict) -> bool:
    """What the Portfolio Board may write: rows of the planning tables and the status of demands."""
    return change.get("table") in PLAN_TABLES or (change.get("table") == "Demands"
                                                  and change.get("field") == "demand_status")


def is_governed_change(change: dict) -> bool:
    if is_plan_decision(change):
        return True
    table = change.get("table", "Projects")
    if table == "Projects" and change.get("op") == "append":  # a new project (a converted demand)
        return True
    if table in GOVERNED_TABLES or (table != "Projects" and change.get("field") in GOVERNED_ROW_FIELDS):
        return True
    if table in GOVERNED_WEIGHT_TABLES:
        return True
    return table == "Projects" and change.get("field") in GOVERNED_CHANGE_FIELDS


def can_approve(role: str) -> bool:
    """Only the PMO may approve and write governed changes (four-eyes principle)."""
    return role == ROLE_PMO


def approval_mode(role: str, changes: Iterable[dict]) -> str:
    """How a user may act on a proposed change set:
    'apply'   – write directly (PMO or PM, change set touches no governed field, e.g. an FTE shift),
    'propose' – submit to the PMO inbox (governed changes; a PMO's own changes also need a second PMO),
    'none'    – no write access (System Admin, unknown roles)."""
    if role in (ROLE_PMO, ROLE_PM):
        return "propose" if any(is_governed_change(c) for c in changes) else "apply"
    return "none"


# --------------------------------------------------------------------------- PII
def register_pii_names(names: Iterable[str], placeholder: str) -> None:
    with _pii_lock:
        for name in names:
            if not isinstance(name, str):  # None, np.nan, pd.NA, pd.NaT, numbers: never register as a name
                continue
            clean = name.strip()
            if clean and clean.lower() not in {"nan", "<na>", "none", "nat"}:
                _pii_registry.setdefault(clean, placeholder)  # the first (highest) priority wins


def register_protected_terms(terms: Iterable[str]) -> None:
    """Registers words (e.g. from project names) that must never be masked as a standalone surname."""
    with _pii_lock:
        for term in terms:
            if isinstance(term, str):
                _protected_words.update(w.casefold() for w in _WORD_RE.findall(term))


def register_pii_from_dataset(dataset: "PortfolioDataset") -> None:
    """Registers all person names from the PII fields of all tables."""
    projects = dataset.projects
    if projects is not None and not projects.empty and "project_name" in projects.columns:
        register_protected_terms(projects["project_name"].dropna().astype(str).unique())
    tables = [dataset.projects, dataset.tasks, dataset.risks, dataset.stakeholders, dataset.stories,
              getattr(dataset, "benefit_plans", None), getattr(dataset, "demands", None)]
    for column, placeholder in PII_FIELDS.items():
        for df in tables:
            if df is not None and not df.empty and column in df.columns:
                register_pii_names(df[column].dropna().astype(str).unique(), placeholder)


def registered_pii() -> dict[str, str]:
    with _pii_lock:
        return dict(_pii_registry)


_RuleKey = tuple[tuple[tuple[str, str], ...], frozenset[str]]
_rules_cache: dict[_RuleKey, list[tuple[str, bool, re.Pattern[str], str]]] = {}


def _mask_rules(mapping: dict[str, str]) -> list[tuple[str, bool, re.Pattern[str], str]]:
    """The compiled replacement rules for the registered names, longest first: (needle for a quick containment check,
    case-insensitive?, pattern, placeholder). Cached per registry state – compiling them per text was the bulk of the
    masking time."""
    with _pii_lock:
        protected = frozenset(_protected_words)
    key = (tuple(sorted(mapping.items())), protected)
    rules = _rules_cache.get(key)
    if rules is not None:
        return rules
    tokens: dict[str, str] = {}
    for full_name, placeholder in mapping.items():
        tokens.setdefault(full_name, placeholder)
        parts = full_name.split()
        # also mask the surname alone ("Frau Demir"), unless it is a word of a project name
        if len(parts) > 1 and len(parts[-1]) >= 3 and parts[-1].casefold() not in protected:
            tokens.setdefault(parts[-1], placeholder)
    rules = []
    for token in sorted(tokens, key=len, reverse=True):
        # surnames are matched case-sensitively (proper nouns), full names case-insensitively
        ignore_case = " " in token
        pattern = re.compile(rf"(?<![\w\[]){re.escape(token)}(?![\w\]])", re.IGNORECASE if ignore_case else 0)
        rules.append((token.lower() if ignore_case else token, ignore_case, pattern, tokens[token]))
    if len(_rules_cache) > 32:  # the registry grows during a session; old states are not needed again
        _rules_cache.clear()
    _rules_cache[key] = rules
    return rules


def _mask_text(text: str, rules: list[tuple[str, bool, re.Pattern[str], str]]) -> str:
    result = _EMAIL_RE.sub(EMAIL_PLACEHOLDER, text) if "@" in text else text
    lowered = result.lower()
    for needle, ignore_case, pattern, placeholder in rules:
        # a regex runs only where the name can occur at all (non-ASCII names always run: case rules differ)
        if needle.isascii() and needle not in (lowered if ignore_case else result):
            continue
        replaced = pattern.sub(placeholder, result)
        if replaced != result:
            result, lowered = replaced, replaced.lower()
    return result


def mask_pii_data(text: str, names: dict[str, str] | None = None) -> str:
    """Pseudonymizes real names (full and last name) and e-mails before text goes to an LLM.
    Example: 'Priya Raman' -> '[PM_MASKED]'. Only active if PII masking is enabled in the Admin Console (default: on)."""
    if not text:
        return text or ""
    if not _pii_enabled_logged():
        return str(text)
    try:
        return _mask_text(str(text), _mask_rules(names if names is not None else registered_pii()))
    except Exception:
        logger.exception("PII masking failed – text withheld")
        return "[TEXT_WITHHELD_PII_MASKING_ERROR]"


def mask_dataframe_pii(df: pd.DataFrame) -> pd.DataFrame:
    """Fully replaces PII columns and masks names in free-text columns (only when PII masking is enabled)."""
    if df is None or df.empty or not _pii_enabled_logged():
        return df
    masked = df.copy()
    for column, placeholder in PII_FIELDS.items():
        if column in masked.columns:
            masked[column] = masked[column].astype(object).where(masked[column].isna(), placeholder)
    rules = _mask_rules(registered_pii())
    seen: dict[str, str] = {}  # the same text (a status, a team name) is masked once

    def mask(value):
        if not isinstance(value, str) or not value:
            return value
        out = seen.get(value)
        if out is None:
            try:
                out = _mask_text(value, rules)
            except Exception:
                logger.exception("PII masking failed – text withheld")
                out = "[TEXT_WITHHELD_PII_MASKING_ERROR]"
            seen[value] = out
        return out

    for column in masked.columns:
        if column in PII_FIELDS:
            continue
        if masked[column].dtype == object or pd.api.types.is_string_dtype(masked[column]):
            masked[column] = masked[column].map(mask)
    return masked


def mask_dataset_pii(dataset: "PortfolioDataset") -> "PortfolioDataset":
    from app.data_adapters.dataset import TABLES

    out = dataset.copy()
    for attr in TABLES:
        setattr(out, attr, mask_dataframe_pii(getattr(out, attr)))
    return out
