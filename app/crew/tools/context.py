"""Scenario context for the tools – isolated per run.

Tools work exclusively on the RLS-filtered, PII-masked dataset. Simulated changes are written to a working copy
of the project view (tool calls build on each other) and collected in a ledger as change proposals.

Isolation: each run owns a `_State` held in a ContextVar – there is no process-wide fallback, so parallel sessions
never see each other's scenario. Work in another thread takes the run's context along
(`contextvars.copy_context().run`); the background shadow crew sets its own context, so it can never modify the main
run's scenario or approval ledger.
"""
from __future__ import annotations

import logging
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from app.core import clock
from app.data_adapters.dataset import CLOSED_PROJECT_STATES, PK, PortfolioDataset

logger = logging.getLogger(__name__)

# What a Project Manager learns about projects outside the RLS scope: neither id nor count, only that the PMO
# coordinates. The PMO sees all projects, so it never meets this text.
RESTRICTED_DEPENDENTS = "Dependent projects outside your scope are affected – the PMO coordinates."
RESTRICTED_PREDECESSORS = "Predecessor projects outside your scope are involved – the PMO coordinates."
RESTRICTED_SUCCESSOR = "RESTRICTED"  # successor of a fact that lies outside the scope


class ProjectNotFound(LookupError):
    pass


@dataclass
class _State:
    dataset: PortfolioDataset
    scenario: pd.DataFrame
    blocked: dict[str, str]
    ledger: list[dict[str, Any]] = field(default_factory=list)
    # Inputs of the deterministic verdict engine, recorded by the tools themselves (never by an LLM):
    results: list[dict[str, str]] = field(default_factory=list)  # {"kind": simulation|graph, "tool", "text"}
    facts: list[dict[str, Any]] = field(default_factory=list)    # {"type": budget_overrun|domino|..., ...}
    as_of: date = field(default_factory=clock.today)  # reference date of every date-dependent calculation
    base_fte: dict[str, float] = field(default_factory=dict)  # FTE per project before any scenario step
    calls: set[tuple[str, str]] = field(default_factory=set)  # (simulation tool, project) already run in this step
    lock: threading.RLock = field(default_factory=threading.RLock)


# Facts describe the *current* scenario state, not a history. A fact replaces the older fact with the
# same identity (type, project, successor). Re-simulating a field of a project also drops the facts that any earlier
# tool call – of an earlier step or of the same request – derived from that field: the last evaluation per project
# and field wins, so a corrective step (e.g. restoring a cut budget, moving staff back) clears an earlier finding.
_BUDGET_FACTS = ("budget_overrun", "negative_roi", "eac_missing", "benefit_missing")
_FIELD_FACTS: dict[str, tuple[str, ...]] = {
    "budget_cost_eur": _BUDGET_FACTS,
    "total_planned_cost_eur": _BUDGET_FACTS,
    "planned_capital_eur": _BUDGET_FACTS,
    "planned_operating_eur": _BUDGET_FACTS,
    "estimate_at_completion_eur": ("budget_overrun", "eac_missing"),
    "planned_end_date": ("deadline_breach", "npv_negative", "npv_change", "graph_unavailable"),
    "allocated_fte": ("overload", "ramp_up"),
    "state": ("on_hold", "no_capacity_kept", "cancel", "indicative", "resume"),
    "phase": ("gate_mandatory_failed", "gate_advisory_failed", "stage_gate"),
}
# A stopped project (cancel / portfolio cut) has no forecast, business case, schedule or team load left to judge
_CLOSED_STATE_FACTS = ("on_hold", "no_capacity_kept", "cancel", "indicative", "resume", *_BUDGET_FACTS, "overload", "ramp_up", "deadline_breach", "npv_negative", "npv_change",
                       "graph_unavailable")

_current: ContextVar[_State | None] = ContextVar("derkurator_scenario_state", default=None)


def set_portfolio_context(dataset: PortfolioDataset, blocked: dict[str, str] | None = None,
                          base_state: dict[str, Any] | None = None, as_of: date | None = None) -> None:
    """`base_state` (scenario memory): the cumulative state of the previous step of the same scenario
    ({"changes", "results", "facts", "turns"}). Its changes are replayed onto the working copy, so the next tools
    build on the already manipulated data, and ledger/results/facts continue cumulatively (one scenario, one
    approval, one verdict). `as_of`: reference date (default today) – the same request with the same `as_of` gives
    the same result on any system day."""
    as_of = as_of or clock.today()
    # object dtype: scenario updates may set any type (pandas 3 is strict about dtype changes)
    view = dataset.project_view(as_of).astype(object).reset_index(drop=True)
    state = _State(dataset, view, dict(blocked or {}), as_of=as_of)
    if not view.empty and "current_fte" in view.columns:
        fte = pd.to_numeric(view["current_fte"], errors="coerce").fillna(0)
        state.base_fte = dict(zip(view[PK].astype(str), fte.astype(float), strict=True))
    if base_state:
        _replay(state, base_state)
    _current.set(state)


def _replay(state: _State, base: dict[str, Any]) -> None:
    for change in base.get("changes") or []:
        if change.get("key"):  # a row of another table (baseline, risk, reserve booking): ledger only
            state.ledger.append({**change, "carried_over": True})
            continue
        target = change.get("scenario_field") or ("current_fte" if change.get("field") == "allocated_fte" else change.get("field"))
        mask = state.scenario[PK].astype(str) == str(change.get("project_id"))
        if mask.any() and target:
            if target not in state.scenario.columns:
                state.scenario[target] = None
            state.scenario.loc[mask, target] = change.get("new")
        state.ledger.append({**change, "carried_over": True})
    state.results.extend(dict(r) for r in base.get("results") or [])
    state.facts.extend({**f, "carried_over": True} for f in base.get("facts") or [])


def _fact_key(fact: dict[str, Any]) -> tuple:
    return fact.get("type"), fact.get("project"), fact.get("successor")


def _public(fact: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fact.items() if k != "carried_over"}


def export_state(turns: list[str] | None = None) -> dict[str, Any]:
    """Cumulative scenario state for the next step (stored with the assistant message, survives a page reload)."""
    s = _state()
    with s.lock:
        return {"changes": [{k: v for k, v in c.items() if k != "carried_over"} for c in s.ledger],
                "results": [dict(r) for r in s.results], "facts": [_public(f) for f in s.facts],
                "turns": list(turns or [])}


def _state() -> _State:
    state = _current.get()
    if state is None:
        raise ProjectNotFound("ERROR: No portfolio context loaded.")
    return state


def as_of() -> date:
    """Reference date of the current run (today without a context)."""
    try:
        return _state().as_of
    except ProjectNotFound:
        return clock.today()


def base_fte(project_id: str) -> float | None:
    """FTE of a project before any step of the scenario (for capacity effects of earlier moves)."""
    return _state().base_fte.get(str(project_id))


def get_dataset() -> PortfolioDataset:
    return _state().dataset


def get_table(name: str, project_id: str | None = None) -> pd.DataFrame:
    df = get_dataset().table(name)
    if project_id is not None and not df.empty and PK in df.columns:
        return df[df[PK].astype(str) == str(project_id)].copy()
    return df.copy()


def get_scenario_df() -> pd.DataFrame:
    s = _state()
    with s.lock:
        return s.scenario.copy()


def allowed_project_ids() -> set[str]:
    try:
        s = _state()
    except ProjectNotFound:
        return set()
    with s.lock:
        return set(s.scenario[PK].astype(str)) if not s.scenario.empty else set()


def first_call(tool: str, project_id: str) -> bool:
    """Registers a simulation of `project_id` with `tool` in this step; False if it already ran (a repeated LLM tool
    call would apply the same change twice)."""
    s = _state()
    with s.lock:
        key = (tool, str(project_id))
        if key in s.calls:
            return False
        s.calls.add(key)
        return True


def blocked_reason(project_id: str) -> str | None:
    s = _state()
    with s.lock:
        return s.blocked.get(str(project_id))


def resolve_project_id(project_ref: str) -> str:
    """Tolerant lookup: 'PRJ-DEMO-1004', 'prj-1004', '1004' or a (unique) project name."""
    ref = str(project_ref or "").strip()
    if not ref:
        raise ProjectNotFound("INPUT ERROR: No project_id given.")
    s = _state()
    with s.lock:
        if s.scenario.empty:
            raise ProjectNotFound("INPUT ERROR: No portfolio context loaded (no permission or no data).")
        ids = s.scenario[PK].astype(str)
        if ref.upper() in set(ids.str.upper()):
            return ids[ids.str.upper() == ref.upper()].iloc[0]
        digits = re.search(r"(\d{3,4})\s*$", ref)
        if digits:
            hits = ids[ids.str.endswith(digits.group(1))]
            if len(hits) == 1:
                return hits.iloc[0]
        names = s.scenario["project_name"].astype(str)
        exact = ids[names.str.casefold() == ref.casefold()]
        if len(exact) == 1:
            return exact.iloc[0]
        partial = ids[names.str.casefold().str.contains(re.escape(ref.casefold()))]
        if len(partial) == 1:
            return partial.iloc[0]
        raise ProjectNotFound(
            f"INPUT ERROR: Project '{ref}' not found or no permission. Available IDs: {', '.join(sorted(ids))}"
        )


def get_project(project_ref: str) -> dict[str, Any]:
    pid = resolve_project_id(project_ref)
    s = _state()
    with s.lock:
        return s.scenario[s.scenario[PK].astype(str) == pid].iloc[0].to_dict()


def record_change(
    project_id: str,
    field: str,
    new_value: Any,
    source_tool: str,
    note: str = "",
    table: str = "Projects",
    scenario_field: str | None = None,
) -> None:
    """Writes the change to the scenario (for follow-up tools) and to the ledger (for Approve & Apply)."""
    target = scenario_field or field
    s = _state()
    with s.lock:
        mask = s.scenario[PK].astype(str) == str(project_id)
        old_value = s.scenario.loc[mask, target].iloc[0] if mask.any() and target in s.scenario.columns else None
        if mask.any():
            if target not in s.scenario.columns:
                s.scenario[target] = None
            s.scenario.loc[mask, target] = new_value
        # this call re-evaluates the field, so every earlier finding derived from it is outdated (same step included)
        stale = _CLOSED_STATE_FACTS if field == "state" and str(new_value) in CLOSED_PROJECT_STATES else _FIELD_FACTS.get(field, ())
        if stale:
            s.facts = [f for f in s.facts if not (f.get("project") == str(project_id) and f.get("type") in stale)]
        s.ledger.append(
            {
                "project_id": str(project_id),
                "table": table,
                "field": field,
                "old": _jsonable(old_value),
                "new": _jsonable(new_value),
                "source_tool": source_tool,
                "note": note,
                "timestamp": clock.stamp(),
                **({"scenario_field": scenario_field} if scenario_field else {}),
            }
        )


def record_row_change(table: str, project_id: str | None, key: str, source_tool: str, *, row: dict | None = None,
                      field: str | None = None, new_value: Any = None, summary: str = "", note: str = "") -> None:
    """A change of a row outside the project view: `row` appends a new row to an append-only table (summary = its
    readable 'after' value), `field`/`new_value` update one field of an existing row (its current value, incl. earlier
    steps, is the 'old'). Only the ledger holds it – follow-up tools read it via pending_rows / row_value."""
    s = _state()
    with s.lock:
        entry: dict[str, Any] = {"project_id": str(project_id or ""), "table": table, "key": str(key),
                                 "source_tool": source_tool, "note": note, "timestamp": clock.stamp()}
        if row is not None:
            entry.update(op="append", field=table, old=None, new=summary or str(key),
                         row={k: _jsonable(v) for k, v in row.items()})
        else:
            entry.update(field=field, old=_jsonable(_row_value(s, table, str(key), str(field))),
                         new=_jsonable(new_value))
        s.ledger.append(entry)


def _row_value(s: _State, table: str, key: str, field: str) -> Any:
    for c in reversed(s.ledger):
        if c.get("table") == table and str(c.get("key")) == key and c.get("field") == field and c.get("op") != "append":
            return c.get("new")
    df = s.dataset.table(table)
    if df.empty:
        return None
    from app.data_adapters.base import WRITABLE_ROW_FIELDS

    key_col = WRITABLE_ROW_FIELDS[table][0] if table in WRITABLE_ROW_FIELDS else \
        next((col for col in df.columns if col.endswith("_number") and col != PK), None)
    rows = df[df[key_col].astype(str) == key] if key_col else df.iloc[0:0]
    return rows.iloc[0].get(field) if not rows.empty and field in rows.columns else None


def row_value(table: str, key: str, field: str) -> Any:
    """Current value of a field of a row in this scenario (the latest scenario change wins over the source)."""
    s = _state()
    with s.lock:
        return _row_value(s, table, str(key), field)


def pending_rows(table: str) -> list[dict[str, Any]]:
    """Rows this scenario appends to `table` (earlier steps included), in order."""
    s = _state()
    with s.lock:
        return [dict(c["row"]) for c in s.ledger if c.get("table") == table and c.get("op") == "append"]


def table_with_pending(table: str, project_id: str | None = None) -> pd.DataFrame:
    """A table of the RLS view plus the rows this scenario appends to it (optionally for one project)."""
    df = get_table(table)
    pending = pd.DataFrame(pending_rows(table))
    if not pending.empty:
        df = pd.concat([df, pending], ignore_index=True) if not df.empty else pending
    if project_id is not None and not df.empty and PK in df.columns:
        df = df[df[PK].astype(str) == str(project_id)]
    return df.reset_index(drop=True)


def get_ledger() -> list[dict[str, Any]]:
    s = _state()
    with s.lock:
        return [{k: v for k, v in c.items() if k != "carried_over"} for c in s.ledger]


# Net direction of a change that can hurt a project's dependents: less budget or team, or a later end date
_ADVERSE_IF = {"budget_cost_eur": "lower", "allocated_fte": "lower", "current_fte": "lower", "planned_end_date": "later"}
_PAUSED_STATES = {"On Hold"}


def adverse_projects() -> set[str]:
    """Projects whose net change in this scenario (first old value vs. latest new value, across all steps) can hurt
    their dependents: a budget cut, fewer FTE, a later end date, a stop or a hold. A cut that a later step reverses
    is no longer adverse; an increase, extra FTE or an acceleration never is."""
    first: dict[tuple[str, str], Any] = {}
    last: dict[tuple[str, str], Any] = {}
    for c in get_ledger():
        key = (str(c["project_id"]), str(c["field"]))
        first.setdefault(key, c.get("old"))
        last[key] = c.get("new")
    adverse: set[str] = set()
    for (pid, fld), new in last.items():
        old = first[(pid, fld)]
        if fld == "state":
            if str(new) in CLOSED_PROJECT_STATES | _PAUSED_STATES and str(old) != str(new):
                adverse.add(pid)
        elif fld in _ADVERSE_IF and old is not None and new is not None:
            try:
                worse = (float(new) < float(old) - 1e-9 if _ADVERSE_IF[fld] == "lower"
                         else pd.Timestamp(new) > pd.Timestamp(old))
            except (TypeError, ValueError):
                continue
            if worse:
                adverse.add(pid)
    return adverse


def record_tool_result(kind: str, tool: str, text: str) -> None:
    """Every simulation/graph tool output of this run, verbatim (verdict engine input). No-op without context."""
    try:
        s = _state()
    except ProjectNotFound:
        return
    with s.lock:
        s.results.append({"kind": kind, "tool": tool, "text": str(text)})


def record_fact(fact_type: str, **data: Any) -> None:
    """Structured business fact found by a tool (e.g. forecast overrun, domino size). No-op without context.
    Supersedes an older fact with the same identity; a budget change or transfer accumulates its delta across steps,
    so the board-approval rule sees the total change of the scenario."""
    try:
        s = _state()
    except ProjectNotFound:
        return
    fact = {"type": fact_type, **{k: _jsonable(v) for k, v in data.items()}}
    with s.lock:
        previous = [f for f in s.facts if _fact_key(f) == _fact_key(fact)]
        if fact_type in ("budget_change", "budget_transfer") and previous:
            fact["delta"] = round(sum(f.get("delta") or 0 for f in previous) + (fact.get("delta") or 0), 2)
        s.facts = [f for f in s.facts if _fact_key(f) != _fact_key(fact)] + [fact]


def clear_facts(fact_type: str, **match: Any) -> None:
    """Drops facts of `fact_type` whose fields equal `match` – e.g. a graph tool that answers again clears its
    earlier `graph_unavailable` fact. No-op without context."""
    try:
        s = _state()
    except ProjectNotFound:
        return
    with s.lock:
        s.facts = [f for f in s.facts if not (f.get("type") == fact_type
                                              and all(f.get(k) == v for k, v in match.items()))]


def get_tool_results(kind: str | None = None) -> list[str]:
    s = _state()
    with s.lock:
        return [r["text"] for r in s.results if kind is None or r["kind"] == kind]


def get_facts() -> list[dict[str, Any]]:
    s = _state()
    with s.lock:
        return [_public(f) for f in s.facts]


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.date().isoformat()
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, float):
        return round(value, 2)
    return value
