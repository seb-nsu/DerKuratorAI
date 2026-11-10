"""Project detail editing: field catalogue, RBAC check, validation and change detection (UI-independent).

Rules:
- Only the Project Manager who owns the project may edit it; the PMO has read-only access; all other roles have none.
- Identity and row-level security fields (project_number, project_manager) are never editable here.
- Governed budget fields (approved budget, CapEx/OpEx plan) can only be filled in when empty – changes to an
  existing budget are simulated in the Steering Console and submitted as a proposal to the PMO inbox.
- A change to a governed field (security.GOVERNED_CHANGE_FIELDS – state, planned end,
  benefit, EAC, actual cost, budgets) is never saved directly; it becomes a proposal for the PMO (`split_governed`).
- Required fields (active environment config) must not be cleared.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.core.required_fields import load_required_fields
from app.core.security import ROLE_PM, ROLE_PMO, is_governed_change
from app.data_adapters.dataset import PHASES


@dataclass(frozen=True)
class FieldSpec:
    label: str
    kind: str  # text | textarea | number | percent | int | date | select
    options: tuple[str, ...] = ()
    governed: bool = False  # budget field: editable only while empty


EDITABLE_FIELDS: dict[str, FieldSpec] = {
    "project_name": FieldSpec("Project name", "text"),
    "project_sponsor": FieldSpec("Sponsor", "text"),
    "status": FieldSpec("RAG status", "select", ("Green", "Yellow", "Red")),
    "state": FieldSpec("State", "select", ("Pending", "Open", "Work in Progress", "On Hold", "Closed Complete",
                                           "Closed Incomplete", "Canceled")),
    # a phase change is a stage gate: governed, so it becomes a proposal (or runs as the stage-gate scenario)
    "phase": FieldSpec("Phase", "select", PHASES),
    "execution_type": FieldSpec("Execution type", "select", ("Agile", "Waterfall", "Hybrid")),
    "percent_complete": FieldSpec("Progress (%)", "percent"),
    "risk_score": FieldSpec("Risk score (1–5)", "int"),
    "planned_start_date": FieldSpec("Planned start", "date"),
    "actual_start_date": FieldSpec("Actual start", "date"),
    "planned_end_date": FieldSpec("Planned end", "date"),
    "budget_cost_eur": FieldSpec("Approved budget (€)", "number", governed=True),
    "planned_capital_eur": FieldSpec("Planned CapEx (€)", "number", governed=True),
    "planned_operating_eur": FieldSpec("Planned OpEx (€)", "number", governed=True),
    "actual_cost_eur": FieldSpec("Actual cost (€)", "number"),
    "estimate_at_completion_eur": FieldSpec("Estimate at completion (€)", "number"),
    "planned_benefit_eur": FieldSpec("Planned benefit (€)", "number"),
    "business_case": FieldSpec("Business case", "textarea"),
}


def is_missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def can_edit(role: str, user_id: str | None, project: dict[str, Any]) -> bool:
    owner = str(project.get("project_manager") or "").strip().casefold()
    return role == ROLE_PM and bool(user_id) and owner == str(user_id).strip().casefold()


def access_level(role: str, user_id: str | None, project: dict[str, Any]) -> str:
    if can_edit(role, user_id, project):
        return "edit"
    return "read" if role in (ROLE_PMO, ROLE_PM) else "none"


def field_editable(field: str, project: dict[str, Any], role: str, user_id: str | None) -> bool:
    spec = EDITABLE_FIELDS.get(field)
    if spec is None or not can_edit(role, user_id, project):
        return False
    return not spec.governed or is_missing(project.get(field))


def missing_required(project: dict[str, Any]) -> list[str]:
    return [f for f in load_required_fields() if is_missing(project.get(f))]


def _normalize(field: str, value: Any) -> Any:
    spec = EDITABLE_FIELDS[field]
    if is_missing(value):
        return None
    if spec.kind in ("number", "percent"):
        return float(value)
    if spec.kind == "int":
        return int(round(float(value)))
    if spec.kind == "date":
        if isinstance(value, (pd.Timestamp, datetime)):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        ts = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(ts) else ts.date().isoformat()
    return str(value).strip()


def split_governed(changes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(direct, governed): direct changes may be saved by the owner; governed ones need a PMO-approved proposal."""
    return [c for c in changes if not is_governed_change(c)], [c for c in changes if is_governed_change(c)]


def diff_and_validate(project: dict[str, Any], edits: dict[str, Any], role: str, user_id: str | None
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (changes in the Approve & Apply ledger format, validation errors). Nothing is written here."""
    if not can_edit(role, user_id, project):
        return [], ["Read-only: only the owning Project Manager can edit this project."]
    required = set(load_required_fields())
    changes, errors = [], []
    pid = str(project.get("project_number"))
    for field, raw in edits.items():
        spec = EDITABLE_FIELDS.get(field)
        if spec is None:
            errors.append(f"{field}: not an editable field.")
            continue
        try:
            new, old = _normalize(field, raw), _normalize(field, project.get(field))
        except (TypeError, ValueError):
            errors.append(f"{spec.label}: invalid value '{raw}'.")
            continue
        if new == old:
            continue
        if spec.governed and not is_missing(project.get(field)):
            errors.append(f"{spec.label}: an approved budget is changed via a budget scenario in the Steering Console, "
                          "which is submitted as a proposal to the PMO inbox.")
            continue
        if new is None and field in required:
            errors.append(f"{spec.label}: required field must not be empty.")
            continue
        if spec.kind in ("number", "int") and new is not None and new < 0:
            errors.append(f"{spec.label}: must not be negative.")
            continue
        if spec.kind == "percent" and new is not None and not 0 <= new <= 100:
            errors.append(f"{spec.label}: must be between 0 and 100.")
            continue
        if field == "risk_score" and new is not None and not 1 <= new <= 5:
            errors.append(f"{spec.label}: must be between 1 and 5.")
            continue
        if spec.kind == "select" and new is not None and new not in spec.options:
            errors.append(f"{spec.label}: '{new}' is not allowed.")
            continue
        changes.append({"project_id": pid, "table": "Projects", "field": field, "old": old, "new": new,
                        "source_tool": "project_detail_edit", "note": f"edited by {user_id}"})
    return changes, errors
