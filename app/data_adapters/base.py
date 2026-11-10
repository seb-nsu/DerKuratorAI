"""Adapter interface, canonical Pydantic models and shared write logic."""
from __future__ import annotations

import json
import logging
import math
import re
from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.data_adapters.dataset import REQUIRED_TABLES, PortfolioDataset

logger = logging.getLogger(__name__)

STATUS_VALUES = ("Green", "Yellow", "Red")
EXECUTION_TYPES = ("Agile", "Waterfall", "Hybrid")

# Fields that "Approve & Apply" may write back to the source (Projects table)
WRITABLE_PROJECT_FIELDS = {
    "budget_cost_eur",
    "total_planned_cost_eur",
    "planned_capital_eur",
    "planned_operating_eur",
    "planned_return_eur",
    "planned_roi_pct",
    "planned_benefit_eur",
    "planned_end_date",
    "state",
    "status",
    # Project detail view (owning Project Manager, see app/data_adapters/project_edit.py)
    "project_name",
    "project_sponsor",
    "phase",
    "execution_type",
    "percent_complete",
    "risk_score",
    "planned_start_date",
    "actual_start_date",
    "actual_cost_eur",
    "estimate_at_completion_eur",
    "business_case",
    # forecast fields that a schedule change moves together with the EAC
    "estimate_to_completion_eur",
    "net_present_value_eur",
    # re-baselining moves the approved end date together with the planned one
    "approved_end_date",
}
# Tables "Approve & Apply" may add rows to (append-only: an existing row is never changed or deleted), with the
# key column of each row
APPENDABLE_TABLES: dict[str, str] = {
    "Baselines": "baseline_number",
    "Approvals": "approval_number",
    "Reserve_Ledger": "entry_number",
    "Benefit_Actuals": "benefit_actual_number",
    "Demand_Scores": "score_number",  # a new assessment of a demand (the latest per criterion counts)
    "Planning_Scenarios": "scenario_record",  # a saved planning scenario, one row per status
    "Planning_Items": "item_record",
    # an approved demand becoming a project (the conversion carries its first baseline, so it is four-eyes)
    "Projects": "project_number", "Cost_Plans": "cost_plan_number", "Resource_Plans": "resource_plan_number",
}
# Field updates of existing rows outside the Projects table: table -> (key column, writable fields)
# A project merge moves these rows of the source project to the target (project_number), with their key column
MERGE_REKEY_TABLES: dict[str, str] = {
    "Project_Tasks": "task_number", "Stories": "story_number", "Resource_Plans": "resource_plan_number",
    "Cost_Plans": "cost_plan_number", "Benefit_Plans": "benefit_plan_number", "Skill_Demand": "skill_demand_number",
    "Risks": "risk_number", "Stakeholders": "stakeholder_number",
}
WRITABLE_ROW_FIELDS: dict[str, tuple[str, frozenset[str]]] = {
    **{table: (key, frozenset({"project_number"})) for table, key in MERGE_REKEY_TABLES.items()},
    "Risks": ("risk_number", frozenset({"risk_status", "mitigation", "probability", "project_number"})),
    "Dependencies": ("dependency_number", frozenset({"predecessor_project_number", "successor_project_number"})),
    "Demands": ("demand_number", frozenset({"demand_status", "converted_project_number"})),
    "Scoring_Model": ("criterion", frozenset({"weight"})),
}


class DataSourceError(RuntimeError):
    """Data source unreachable or structurally unusable."""


class WriteNotAuthorizedError(DataSourceError):
    """The caller's role may not write this change set – checked server-side in apply_changes."""


class DataConflictError(DataSourceError):
    """Compare-and-write: the source no longer holds the values the change set was calculated from."""

    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        detail = "; ".join(f"{c['project_id']} {c['field']}: expected {c['expected']!r}, now {c['current']!r}"
                           for c in conflicts[:5])
        more = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
        super().__init__(f"The data was changed by someone else since this was calculated – nothing was written. "
                         f"{detail}{more}. Please re-run the scenario / reload the project.")


# --------------------------------------------------------------------------- Pydantic models
class _Record(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> Any:
        # pandas 3: missing values can be None, np.nan, pd.NA or pd.NaT
        if v is None or v is pd.NA or v is pd.NaT:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, float) and pd.isna(v):
            return None
        if isinstance(v, pd.Timestamp):
            return None if pd.isna(v) else v.date()
        if isinstance(v, datetime):
            return v.date()
        return v


class ProjectRecord(_Record):
    project_number: str
    project_name: str
    project_manager: str
    project_sponsor: Optional[str] = None
    status: Literal["Green", "Yellow", "Red"]
    state: str
    execution_type: Literal["Agile", "Waterfall", "Hybrid"]
    percent_complete: float = Field(ge=0, le=100)
    planned_start_date: Optional[date] = None
    actual_start_date: Optional[date] = None
    planned_end_date: Optional[date] = None
    budget_cost_eur: float = Field(ge=0)
    actual_cost_eur: float = Field(ge=0)
    estimate_at_completion_eur: float = Field(ge=0)
    total_planned_cost_eur: float = Field(ge=0)
    planned_capital_eur: float = Field(ge=0)
    planned_operating_eur: float = Field(ge=0)
    planned_benefit_eur: Optional[float] = None
    planned_roi_pct: Optional[float] = None
    business_case: Optional[str] = None
    data_quality_profile: str = "valid"


class TaskRecord(_Record):
    task_number: str
    project_number: str
    short_description: str
    dependency_task_number: Optional[str] = None
    assigned_to: Optional[str] = None
    assignment_group: Optional[str] = None
    state: str
    percent_complete: float = Field(ge=0, le=100)
    planned_effort_hours: Optional[float] = Field(default=None, ge=0)


class ResourcePlanRecord(_Record):
    resource_plan_number: str
    project_number: str
    month: date
    assignment_group: str
    capacity_hours: float = Field(ge=0)
    demand_hours: float = Field(ge=0)
    utilization_pct: float = Field(ge=0)
    allocated_fte: float = Field(ge=0)


class StakeholderRecord(_Record):
    stakeholder_number: str
    project_number: str
    user: str
    function: Optional[str] = None


class RiskRecord(_Record):
    risk_number: str
    project_number: str
    short_description: str
    probability: Optional[str] = None
    impact: Optional[str] = None
    risk_status: Optional[str] = None
    systemic_risk_cluster: Optional[str] = None


TABLE_MODELS: dict[str, tuple[str, type[_Record]]] = {
    "Projects": ("project_number", ProjectRecord),
    "Project_Tasks": ("task_number", TaskRecord),
    "Resource_Plans": ("resource_plan_number", ResourcePlanRecord),
    "Stakeholders": ("stakeholder_number", StakeholderRecord),
    "Risks": ("risk_number", RiskRecord),
}


@dataclass
class ApplyResult:
    applied: int = 0
    manual: list[dict[str, Any]] = field(default_factory=list)  # not automatically persistable
    skipped: list[str] = field(default_factory=list)
    snapshot_id: str | None = None  # snapshot of the source taken right before the write
    operation_id: str | None = None  # intent and committed record of the write journal carry it


def new_operation_id() -> str:
    import uuid

    return f"op-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- Interface
class PortfolioDataProvider(ABC):
    """Every data source (JSON, Excel, ServiceNow API, ...) delivers the same relational tables."""

    source_name: str = "abstract"

    def __init__(self) -> None:
        self.last_validation_errors: dict[str, list[str]] = {}

    @abstractmethod
    def load_tables(self) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
        """Raw tables keyed by table name, plus metadata."""

    @abstractmethod
    def _write_project_fields(self, updates: dict[str, dict[str, Any]]) -> None:
        """Writes {project_number: {field: value}} to the source (the snapshot is taken beforehand by apply_changes)."""

    def _write_rows(self, appends: dict[str, list[dict[str, Any]]],
                    row_updates: dict[str, dict[str, dict[str, Any]]]) -> None:
        """Appends rows ({table: [row, ...]}) and updates fields of existing rows ({table: {key: {field: value}}})."""
        raise DataSourceError(f"The {self.source_name} source cannot write rows of other tables.")

    def _write_changes(self, updates: dict[str, dict[str, Any]], appends: dict[str, list[dict[str, Any]]],
                       row_updates: dict[str, dict[str, dict[str, Any]]]) -> None:
        """One write of everything a change set holds. Adapters that can write all of it at once override this."""
        if updates:
            self._write_project_fields(updates)
        if appends or row_updates:
            self._write_rows(appends, row_updates)

    # ------------------------------------------------------------------ read
    def load_dataset(self) -> PortfolioDataset:
        tables, metadata = self.load_tables()
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise DataSourceError(f"Required tables missing in {self.source_name}: {', '.join(missing)}")
        dataset = PortfolioDataset.from_tables(tables, metadata)
        self.last_validation_errors = self.validate(dataset)
        return dataset

    def validate(self, dataset: PortfolioDataset) -> dict[str, list[str]]:
        """Pydantic validation of all core tables; errors are collected, not raised."""
        errors: dict[str, list[str]] = {}
        for table, (key, model) in TABLE_MODELS.items():
            df = dataset.table(table)
            for idx, row in enumerate(df.to_dict(orient="records")):
                try:
                    model.model_validate(row)
                except ValidationError as exc:
                    rid = row.get(key) or f"Row {idx + 1}"
                    errors.setdefault(table, []).append(
                        f"{rid}: " + "; ".join(f"{'.'.join(map(str, e['loc']))} – {e['msg']}" for e in exc.errors())
                    )
        return errors

    # ------------------------------------------------------------------ write
    @property
    def source_path(self) -> Path | None:
        path = getattr(self, "path", None)
        return Path(path) if path is not None else None

    def _in_source_currency(self, updates: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Project amounts are simulated in EUR; a project kept in another currency gets them back in its own
        currency (the rate it was converted with when loaded), so the source never mixes currencies."""
        projects = self.load_dataset().projects
        if not updates or projects.empty or "fx_rate" not in projects.columns:
            return updates
        rates = dict(zip(projects[PK_COL].astype(str).str.strip(), pd.to_numeric(projects["fx_rate"], errors="coerce"),
                         strict=True))
        out: dict[str, dict[str, Any]] = {}
        for pid, fields_ in updates.items():
            rate = rates.get(pid)
            if rate is None or pd.isna(rate) or rate == 1.0:
                out[pid] = fields_
                continue
            out[pid] = {f: (round(float(v) / rate, 2) if f.endswith("_eur") and isinstance(v, (int, float))
                            and not isinstance(v, bool) else v) for f, v in fields_.items()}
        return out

    def find_conflicts(self, expected: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
        """Compares the expected 'old' values with the current source state."""
        projects = self.load_dataset().projects
        current = projects.assign(**{PK_COL: projects[PK_COL].astype(str).str.strip()}).set_index(PK_COL) \
            if not projects.empty and PK_COL in projects.columns else pd.DataFrame()
        conflicts = []
        for (pid, fld), old in expected.items():
            if pid not in current.index:
                conflicts.append({"project_id": pid, "field": fld, "expected": old, "current": "(project missing)"})
                continue
            now = current.at[pid, fld] if fld in current.columns else None
            if _comparable(now) != _comparable(old):
                conflicts.append({"project_id": pid, "field": fld, "expected": _comparable(old), "current": _comparable(now)})
        return conflicts

    def find_row_conflicts(self, expected: dict[tuple[str, str, str], Any],
                           new_keys: dict[str, set[str]]) -> list[dict[str, Any]]:
        """Compare-and-write for rows outside Projects: an appended row's key must not exist yet, an updated field must
        still hold its expected old value. `expected`: {(table, key, field): old}; `new_keys`: {table: {key}}."""
        dataset = self.load_dataset()
        conflicts = []
        for table, keys in new_keys.items():
            df = dataset.table(table)
            col = APPENDABLE_TABLES[table]
            existing = set(df[col].astype(str).str.strip()) if not df.empty and col in df.columns else set()
            for key in sorted(keys & existing):
                conflicts.append({"project_id": key, "field": table, "expected": "(new row)", "current": "(already exists)"})
        for (table, key, fld), old in expected.items():
            df = dataset.table(table)
            col = WRITABLE_ROW_FIELDS[table][0]
            rows = df[df[col].astype(str).str.strip() == key] if not df.empty and col in df.columns else df
            if rows.empty:
                conflicts.append({"project_id": key, "field": fld, "expected": old, "current": "(row missing)"})
                continue
            now = rows.iloc[0].get(fld)
            if _comparable(now) != _comparable(old):
                conflicts.append({"project_id": key, "field": fld, "expected": _comparable(old), "current": _comparable(now)})
        return conflicts

    def authorize(self, changes: list[dict[str, Any]], role: str, user: str | None,
                  proposal_id: str | None = None, operation_id: str | None = None) -> None:
        """Server-side RBAC for every write – never trust the UI. Raises WriteNotAuthorizedError.
        - System Admin / unknown roles: no writes.
        - Project Manager: only own projects, never governed fields (those go through a proposal).
        - PMO: governed fields only as the approval of an open proposal of *another* person, whose stored change
          set is exactly the one written (four-eyes principle, also between PMOs).
        - Deputy (another PMO with an active approval delegation): the same rights and four-eyes checks as the PMO;
          the decision is recorded on behalf of the delegator (the delegator's own proposals in the deputy's name).
        - Portfolio Board: only the change set of an annual plan it approves (planning rows, demand status); an
          approved plan is never written by anyone else."""
        from app.core import proposals
        from app.core.security import (
            ROLE_BOARD,
            ROLE_PM,
            ROLE_PMO,
            is_governed_change,
            is_plan_change,
            is_plan_decision,
            log_refusal,
        )

        def refuse(reason: str, message: str, projects=None, fields=None) -> WriteNotAuthorizedError:
            """Logs the refusal as one security event (IDs and field names only) and returns the error to raise."""
            log_refusal("Write refused", user=user, role=role, reason=reason,
                        projects=projects if projects is not None else [c.get("project_id") for c in changes],
                        fields=fields if fields is not None else [c.get("field") for c in changes],
                        **({"proposal": proposal_id} if proposal_id else {}))
            return WriteNotAuthorizedError(message)

        if role not in (ROLE_PM, ROLE_PMO, ROLE_BOARD):
            raise refuse("no_write_role", f"Role '{role}' has no write access to portfolio data.")
        if role == ROLE_BOARD and (proposal_id is None or not all(is_plan_change(c) for c in changes)):
            raise refuse("board_plan_only", "The Portfolio Board writes only the annual plan it approves.")
        if role != ROLE_BOARD and any(is_plan_decision(c) for c in changes):
            raise refuse("plan_needs_board", "The annual plan is approved by the Portfolio Board (four-eyes).")
        governed = [c for c in changes if is_governed_change(c)]
        if role == ROLE_PM and any(not str(c.get("project_id") or "").strip() for c in changes):
            raise refuse("portfolio_row", "Rows without a project (e.g. the management reserve) are written by the PMO only.")
        if role == ROLE_PM:
            dataset = self.load_dataset()
            projects, demands = dataset.projects, dataset.demands
            owners = dict(zip(projects[PK_COL].astype(str).str.strip(), projects["project_manager"].astype(str), strict=True))
            if not demands.empty and {"demand_number", "demand_owner"} <= set(demands.columns):  # own demands too
                owners.update(zip(demands["demand_number"].astype(str).str.strip(),
                                  demands["demand_owner"].astype(str), strict=True))
            foreign = sorted({str(c.get("project_id")) for c in changes
                              if owners.get(str(c.get("project_id")).strip(), "").strip().casefold()
                              != str(user or "").strip().casefold()})
            if foreign:
                raise refuse("foreign_project", f"No permission for: {', '.join(foreign)}", projects=foreign)
            if governed:
                fields = sorted({c['field'] for c in governed})
                raise refuse("governed_without_proposal", "Governed fields (" + ", ".join(fields)
                             + ") need a PMO-approved proposal (four-eyes principle).", fields=fields)
            return
        if not governed and role != ROLE_BOARD:
            return
        proposal = proposals.get_proposal(proposal_id) if proposal_id else None
        if proposal is None:
            raise refuse("governed_without_proposal",
                         "Governed changes need an approved proposal – submit it to the PMO inbox.")
        reserved = (proposal["status"] == proposals.PENDING_APPLY and operation_id is not None
                    and proposal.get("operation_id") == operation_id and proposal.get("pending_by") == user)
        if proposal["status"] != proposals.SUBMITTED and not reserved:  # a reservation is written only by its operation
            raise refuse("proposal_not_open", f"Proposal {proposal_id} is already {proposal['status']}.")
        if str(proposal.get("requester")) == str(user):  # PMO here: only the requester can fail
            raise refuse("self_approval", "Four-eyes principle: you cannot approve your own proposal.")
        if _fingerprint(proposal.get("changes")) != _fingerprint(changes):
            raise refuse("changeset_mismatch",
                         f"The change set differs from proposal {proposal_id} – nothing was written.")

    def apply_changes(self, changes: list[dict[str, Any]], user: str | None = None, reason: str = "data_change",
                      check_conflicts: bool = True, *, role: str, proposal_id: str | None = None,
                      operation_id: str | None = None) -> ApplyResult:
        """Compare-and-write with a snapshot before the write, all under a cross-process lock:
        0. server-side RBAC (`authorize`), 1. every change's 'old' value must still match the source (the first
        'old' per field is the starting state), 2. snapshot of the source file, 3. blocking audit record (phase
        "intent"), 4. write, 5. audit record (phase "committed") with the same operation id.
        A conflict raises DataConflictError and writes nothing; a failed audit write aborts the write. A missing
        committed record after a successful write is reported by app.core.journal.reconcile_operations."""
        from app.core import logs
        from app.core.snapshots import SnapshotError, create_snapshot, source_write_lock

        self.authorize(changes or [], role, user, proposal_id, operation_id)

        result = ApplyResult(operation_id=operation_id or new_operation_id())
        updates: dict[str, dict[str, Any]] = {}
        expected: dict[tuple[str, str], Any] = {}
        appends: dict[str, list[dict[str, Any]]] = {}
        new_keys: dict[str, set[str]] = {}
        row_updates: dict[str, dict[str, dict[str, Any]]] = {}
        expected_rows: dict[tuple[str, str, str], Any] = {}
        applicable: list[dict[str, Any]] = []
        for change in changes or []:
            fld = change.get("field")
            if is_manual_change(change):
                result.manual.append(change)
                continue
            if is_append(change):
                table = change["table"]
                row = _approved_row({k: _serializable(v) for k, v in change["row"].items()}, user, proposal_id)
                key = str(row.get(APPENDABLE_TABLES[table]) or "").strip()
                if not key or key in new_keys.get(table, set()):
                    result.skipped.append(f"Row without a unique {APPENDABLE_TABLES[table]}: {table}")
                    continue
                appends.setdefault(table, []).append(row)
                new_keys.setdefault(table, set()).add(key)
                applicable.append(change)
                continue
            if is_row_update(change):
                table, key = change["table"], str(change["key"]).strip()
                row_updates.setdefault(table, {}).setdefault(key, {})[fld] = _serializable(change.get("new"))
                if "old" in change:
                    expected_rows.setdefault((table, key, fld), change.get("old"))
                applicable.append(change)
                continue
            pid = str(change.get("project_id", "")).strip()
            if not pid:
                result.skipped.append(f"Change without project_id: {change}")
                continue
            updates.setdefault(pid, {})[fld] = _serializable(change.get("new"))
            if "old" in change:
                expected.setdefault((pid, fld), change.get("old"))
            applicable.append(change)
        if not (updates or appends or row_updates):
            return result
        source = self.source_path
        try:
            with source_write_lock(source) if source is not None else nullcontext():
                if check_conflicts:
                    conflicts = self.find_conflicts(expected) if expected else []
                    if appends or expected_rows:
                        conflicts += self.find_row_conflicts(expected_rows, new_keys)
                    if conflicts:
                        raise DataConflictError(conflicts)
                if source is not None:
                    result.snapshot_id = create_snapshot(source, reason, user, applicable)["id"]
                logs.append_jsonl(logs.AUDIT_LOG, {"event": "data_write", "phase": "intent",
                                                   "operation_id": result.operation_id, "user_id": user, "role": role,
                                                   "reason": reason, "proposal_id": proposal_id,
                                                   "snapshot_id": result.snapshot_id, "changes": applicable})
                try:
                    self._write_changes(self._in_source_currency(updates), appends, row_updates)
                except Exception as exc:  # the data_write record above must not stand as a successful write
                    _record_write_failure(user, role, reason, proposal_id, result.snapshot_id, exc, result.operation_id)
                    raise
                result.applied = (sum(len(v) for v in updates.values()) + sum(len(v) for v in appends.values())
                                  + sum(len(f) for rows in row_updates.values() for f in rows.values()))
                _record_commit(user, role, reason, proposal_id, result)
        except SnapshotError as exc:
            raise DataSourceError(str(exc)) from exc
        except logs.AuditWriteError as exc:
            raise DataSourceError(f"Audit trail could not be written – nothing was changed. ({exc})") from exc
        return result


def _approved_row(row: dict[str, Any], user: str | None, proposal_id: str | None) -> dict[str, Any]:
    """An appended row records who approved it and when (empty approval fields are filled at the write)."""
    from app.core import clock

    stamp = {"approved_by": user, "approval_date": clock.today().isoformat(), "proposal_id": proposal_id}
    return {**row, **{k: v for k, v in stamp.items() if k in row and row[k] in (None, "") and v}}


def _record_commit(user, role, reason, proposal_id, result: ApplyResult) -> None:
    """The `committed` record of the write journal. The data is written at this point, so a failed record never
    raises (the write is not undone); the open intent is what reconcile_operations reports."""
    from app.core import logs

    try:
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "data_write", "phase": "committed",
                                           "operation_id": result.operation_id, "user_id": user, "role": role,
                                           "reason": reason, "proposal_id": proposal_id,
                                           "snapshot_id": result.snapshot_id, "applied": result.applied})
    except logs.AuditWriteError:
        logging.getLogger(__name__).error("Write %s applied, but its committed record is missing",
                                          result.operation_id)


def _record_write_failure(user, role, reason, proposal_id, snapshot_id, exc: Exception,
                          operation_id: str | None = None) -> None:
    """`data_write_failed` next to the `data_write` record: the trail shows the write did not happen (best effort –
    the original error is what the caller sees)."""
    from app.core import logs

    try:
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "data_write_failed", "user_id": user, "role": role, "reason": reason,
                                           "proposal_id": proposal_id, "snapshot_id": snapshot_id,
                                           "operation_id": operation_id, "error": exc.__class__.__name__})
    except Exception:  # never mask the write error
        logging.getLogger(__name__).exception("data_write_failed could not be recorded")


PK_COL = "project_number"


def is_append(change: dict[str, Any]) -> bool:
    """A new row for one of the append-only tables."""
    return (change.get("op") == "append" and change.get("table") in APPENDABLE_TABLES
            and isinstance(change.get("row"), dict))


def is_row_update(change: dict[str, Any]) -> bool:
    """A field update of an existing row outside Projects (e.g. a risk's status), addressed by its key."""
    spec = WRITABLE_ROW_FIELDS.get(str(change.get("table")))
    return spec is not None and change.get("op") != "append" and bool(change.get("key")) and change.get("field") in spec[1]


def is_manual_change(change: dict[str, Any]) -> bool:
    """A change the data source cannot write (resource plans, non-writable fields) – executed by hand."""
    if is_append(change) or is_row_update(change):
        return False
    return change.get("table", "Projects") != "Projects" or change.get("field") not in WRITABLE_PROJECT_FIELDS


def net_changes(changes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One change per (project, table, field, row key): the first 'old' (the state before the scenario) and the latest 'new'.
    Fields that end where they started drop out, so a proposal holds the net change of a multi-step scenario."""
    merged: dict[tuple, dict[str, Any]] = {}
    for c in changes or []:
        key = (str(c.get("project_id")), c.get("table", "Projects"), c.get("field"), c.get("key"))
        if key in merged:
            merged[key].update({k: v for k, v in c.items() if k not in ("old", "carried_over")})
        else:
            merged[key] = {k: v for k, v in c.items() if k != "carried_over"}
    return [c for c in merged.values() if _comparable(c.get("old")) != _comparable(c.get("new"))]


def _comparable(value: Any) -> Any:
    """Normalises values from the ledger (JSON round-trips, ISO strings) and from the source (floats, Timestamps)
    to one comparable form: None | bool | float (2 decimals) | ISO date | stripped string."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).date().isoformat()
    if isinstance(value, bool):
        return value
    if hasattr(value, "item") and not isinstance(value, str):
        value = value.item()
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else round(float(value), 2)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+)?", text):
        return text[:10]
    try:
        return round(float(text), 2)
    except ValueError:
        return text


def _fingerprint(changes: Any) -> str:
    """Order-preserving, JSON-normalised form of a change set (a proposal round-trips through JSONL)."""
    return json.dumps(changes or [], sort_keys=True, default=str)


def _serializable(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, float):
        return round(value, 2)
    return value
