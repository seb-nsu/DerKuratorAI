"""Configurable required fields of the Data Steward – stored per environment in data/env_<env>.json.

- The System Admin selects required fields in the Admin Console; the Data Steward validates dynamically against them.
- Identity fields (including the row-level security column) are always required and cannot be deselected.
- A missing or corrupt environment file falls back to the default required fields (fail-safe).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("derkurator.config")

LOCKED_FIELDS: tuple[str, ...] = ("project_number", "project_name", "project_manager")
DEFAULT_CORE_FIELDS: tuple[str, ...] = (
    "project_number", "project_name", "project_manager", "status", "state", "execution_type", "percent_complete",
    "planned_end_date", "budget_cost_eur", "actual_cost_eur", "estimate_at_completion_eur",
    "total_planned_cost_eur", "planned_capital_eur", "planned_operating_eur",
)
GOVERNANCE_FIELDS: tuple[str, ...] = ("project_sponsor", "business_case", "planned_benefit_eur")
DEFAULT_REQUIRED_FIELDS: tuple[str, ...] = DEFAULT_CORE_FIELDS + GOVERNANCE_FIELDS


def normalize(fields: Iterable[str]) -> list[str]:
    """Drops duplicates/blanks, adds locked identity fields, sorts stably (default order first)."""
    chosen = {str(f).strip() for f in fields if isinstance(f, str) and str(f).strip()} | set(LOCKED_FIELDS)
    order = {name: i for i, name in enumerate(DEFAULT_REQUIRED_FIELDS)}
    return sorted(chosen, key=lambda f: (order.get(f, len(order)), f))


def config_path() -> Path:
    """File that stores the required fields of the active environment."""
    from app.core.env_config import env_config_path
    from app.core.runtime_config import get_runtime_config

    return env_config_path(get_runtime_config().environment)


def load_required_fields() -> list[str]:
    from app.core.runtime_config import get_runtime_config

    fields = get_runtime_config().required_fields
    return normalize(fields) if fields else list(DEFAULT_REQUIRED_FIELDS)


def save_required_fields(fields: Iterable[str], updated_by: str | None = None, role: str | None = None,
                         pmo_head: bool = False) -> list[str]:
    """Applies the selection and persists it to the active environment file. Raises OSError on write failure."""
    from app.core.runtime_config import persist_runtime_config, update_runtime_config

    final = normalize(fields)
    update_runtime_config(required_fields=final, role=role, pmo_head=pmo_head)
    persist_runtime_config(updated_by=updated_by)
    logger.info("Required fields saved (%d fields) by %s", len(final), updated_by)
    return final


def reset_required_fields(updated_by: str | None = None, role: str | None = None, pmo_head: bool = False) -> list[str]:
    return save_required_fields(DEFAULT_REQUIRED_FIELDS, updated_by=updated_by, role=role, pmo_head=pmo_head)
