"""The enterprise graph in memory (NetworkX), built from the PortfolioDataset.

Graph model:
  project dependencies   MultiDiGraph successor -> predecessor (s waits for p), one edge per Dependencies row with
                         dependency_number, type, status, description, required_by
  task chain             DiGraph a -> b (a = dependency_task_number of b: a must finish before b)
  tasks                  task -> project, team (assignment group), state, progress, remaining hours, planned dates
  allocations            (team, project) -> peak / average utilisation, overloaded months, demand, capacity, FTE
  risks                  project -> risks with impact, cluster and description

Privacy: person names (PM, sponsor, assigned_to) are deliberately never taken into the graph. Edges to a project that
is not in the Projects table are dropped (as the former graph database did).

The graph is held per process and rebuilt automatically when the source file changes (content fingerprint, with a
size/mtime fast path). Building it takes well under a second for a few hundred projects, so there is no persistent
database, no seed script and no lock across processes. A rebuild swaps the reference atomically; queries in flight
keep the graph they started with. Tests and the evaluation pin a graph built from their own dataset.

With GRAPH_BACKEND=neo4j every rebuild and pin also writes the graph into Neo4j, where the graph tools then query
it (app/graph/backends.py, app/graph/neo4j_backend.py); this graph stays the source and the fallback.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

from app.core import clock
from app.data_adapters.dataset import CLOSED_TASK_STATES, PortfolioDataset

logger = logging.getLogger(__name__)


class GraphUnavailable(RuntimeError):
    """The graph could not be built (source data unreadable or structurally unusable)."""


@dataclass(frozen=True)
class PortfolioGraph:
    projects: dict[str, dict[str, Any]]
    dependencies: nx.MultiDiGraph
    tasks: dict[str, dict[str, Any]]
    task_chain: nx.DiGraph
    allocations: dict[tuple[str, str], dict[str, Any]]
    teams: tuple[str, ...]
    risks: tuple[dict[str, Any], ...]
    built_at: str = ""
    source: str = ""
    build_seconds: float = 0.0
    tasks_by_project: dict[str, list[str]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "Project": len(self.projects), "Task": len(self.tasks), "Team": len(self.teams), "Risk": len(self.risks),
            "BELONGS_TO": sum(1 for t in self.tasks.values() if t.get("project") in self.projects),
            "BLOCKS": self.task_chain.number_of_edges(),
            "HANDLED_BY": sum(1 for t in self.tasks.values() if t.get("team")),
            "ALLOCATED_TO": len(self.allocations), "HAS_RISK": len(self.risks),
            "DEPENDS_ON": self.dependencies.number_of_edges(),
        }


# =========================================================================== build
def _clean(value: Any, kind: str = "str") -> Any:
    """NaN/NaT to None, timestamps to ISO dates, numpy scalars to Python, numbers to float/int."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.date().isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    try:
        if kind == "float":
            return float(value)
        if kind == "int":
            return int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return str(value)


def _rows(df: pd.DataFrame, mapping: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    """Selects source columns, renames them ({source: (property, kind)}) and cleans the values."""
    present = {src: spec for src, spec in mapping.items() if src in df.columns}
    return [{name: _clean(row[src], kind) for src, (name, kind) in present.items()}
            for row in df.to_dict(orient="records")]


PROJECT_PROPERTIES = {
    "project_number": ("project_number", "str"), "project_name": ("name", "str"), "status": ("status", "str"),
    "state": ("state", "str"), "phase": ("phase", "str"), "execution_type": ("execution_type", "str"),
    "percent_complete": ("percent_complete", "float"), "budget_cost_eur": ("budget", "float"),
    "estimate_at_completion_eur": ("eac", "float"), "actual_cost_eur": ("actual_cost", "float"),
    "planned_end_date": ("planned_end", "str"), "risk_score": ("risk_score", "int"),
    "value_score": ("value_score", "int"), "portfolio": ("portfolio", "str"),
    "business_unit": ("business_unit", "str"), "priority": ("priority", "int"),
}


def build_graph(ds: PortfolioDataset, source: str = "") -> PortfolioGraph:
    """Builds the graph from a dataset (the full source, not an RLS view – the tools filter by scope)."""
    started = time.perf_counter()
    projects = {p["project_number"]: p for p in _rows(ds.projects, PROJECT_PROPERTIES) if p.get("project_number")}

    tk = ds.tasks.copy()
    tasks: dict[str, dict[str, Any]] = {}
    chain = nx.DiGraph()
    by_project: dict[str, list[str]] = {}
    if not tk.empty and "task_number" in tk.columns:
        effort = pd.to_numeric(tk.get("planned_effort_hours"), errors="coerce").fillna(0)
        done = pd.to_numeric(tk.get("percent_complete"), errors="coerce").fillna(0)
        tk["remaining_hours"] = effort * (1 - done / 100)
        tk.loc[tk["state"].isin(CLOSED_TASK_STATES), "remaining_hours"] = 0.0
        rows = _rows(tk, {
            "task_number": ("task_number", "str"), "project_number": ("project", "str"),
            "assignment_group": ("team", "str"), "dependency_task_number": ("depends_on", "str"),
            "short_description": ("short_description", "str"), "state": ("state", "str"),
            "percent_complete": ("percent_complete", "float"), "planned_start_date": ("planned_start", "str"),
            "planned_end_date": ("planned_end", "str"), "planned_effort_hours": ("effort_hours", "float"),
            "remaining_hours": ("remaining_hours", "float"),
        })
        for row in rows:
            if row.get("task_number") and row["task_number"] not in tasks:
                tasks[row["task_number"]] = row
                chain.add_node(row["task_number"])
        for row in rows:  # a -> b only between known tasks
            if row.get("depends_on") in tasks and row.get("task_number") in tasks:
                chain.add_edge(row["depends_on"], row["task_number"])
        for number, task in tasks.items():
            if task.get("project") in projects:
                by_project.setdefault(task["project"], []).append(number)

    allocations: dict[tuple[str, str], dict[str, Any]] = {}
    rp = ds.resource_plans.copy()
    if not rp.empty and {"assignment_group", "project_number"} <= set(rp.columns):
        from app.core.business_rules import current_rules

        limit = current_rules().team_capacity_limit_pct  # governance config; a change takes effect on the next build
        rp["utilization_pct"] = pd.to_numeric(rp["utilization_pct"], errors="coerce")
        current = ds.current_allocation()
        fte = current.groupby(["assignment_group", "project_number"])["allocated_fte"].sum() if not current.empty \
            else pd.Series(dtype=float)
        for (team_key, pid_key), grp in rp.groupby(["assignment_group", "project_number"]):
            team, pid = str(team_key), str(pid_key)
            if pid not in projects:
                continue
            over = grp[grp["utilization_pct"] > limit]
            allocations[(team, pid)] = {
                "team": team, "project": pid,
                "utilization_pct": float(grp["utilization_pct"].max()),
                "avg_utilization_pct": round(float(grp["utilization_pct"].mean()), 1),
                "overloaded_months": sorted(m.date().isoformat() for m in over["month"]),
                "months": int(len(grp)),
                "demand_hours": float(pd.to_numeric(grp["demand_hours"], errors="coerce").sum()),
                "capacity_hours": float(pd.to_numeric(grp["capacity_hours"], errors="coerce").sum()),
                "current_fte": float(fte.get((team, pid), 0.0)),
            }
    teams = set(rp["assignment_group"].dropna().astype(str)) if "assignment_group" in rp.columns else set()
    teams |= {t["team"] for t in tasks.values() if t.get("team")}

    order = {pid: i for i, pid in enumerate(projects)}
    risks = tuple(sorted((r for r in _rows(ds.risks, {
        "risk_number": ("risk_number", "str"), "project_number": ("project", "str"),
        "short_description": ("short_description", "str"), "probability": ("probability", "str"),
        "impact": ("impact", "str"), "risk_status": ("risk_status", "str"),
        "systemic_risk_cluster": ("cluster", "str"), "due_date": ("due_date", "str"),
    }) if r.get("project") in projects), key=lambda r: order[r["project"]]))  # per project, in source order

    deps = nx.MultiDiGraph()
    deps.add_nodes_from(projects)
    if not ds.dependencies.empty:
        for row in _rows(ds.dependencies, {
            "successor_project_number": ("successor", "str"), "predecessor_project_number": ("predecessor", "str"),
            "dependency_number": ("dependency_number", "str"), "dependency_type": ("type", "str"),
            "status": ("status", "str"), "description": ("description", "str"),
            "required_by_date": ("required_by", "str"),
        }):
            s, p = row.pop("successor"), row.pop("predecessor")
            if s in projects and p in projects:
                deps.add_edge(s, p, **row)

    seconds = time.perf_counter() - started
    return PortfolioGraph(projects=projects, dependencies=deps, tasks=tasks, task_chain=chain,
                          allocations=allocations, teams=tuple(sorted(teams)), risks=risks, built_at=clock.stamp(),
                          source=source, build_seconds=round(seconds, 3), tasks_by_project=by_project)


# =========================================================================== process-wide graph
_lock = threading.Lock()
_state: dict[str, Any] = {"graph": None, "fingerprint": None, "pinned": False, "failed": None, "version": 0}


def _source() -> tuple[str, Path | list[Path] | None]:
    from app.core.config import get_settings
    from app.data_adapters import source_files

    s = get_settings()
    try:
        files = source_files(s)
    except Exception:  # noqa: BLE001 - an unreadable mapping is reported by the build itself
        return s.data_source, None
    return s.data_source, files[0] if len(files) == 1 else files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(path: Path | list[Path] | None, previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Content fingerprint of the source file(s); an unchanged size and mtime reuse the previous hash. A mapped source
    with several files gets one fingerprint over all of them (missing files, e.g. an unused store, are skipped)."""
    if path is None:
        return None
    if not isinstance(path, Path):
        return _files_fingerprint([p for p in path if p.exists()], previous)
    try:
        stat = path.stat()
    except OSError:
        return None
    if previous and previous.get("size") == stat.st_size and previous.get("mtime_ns") == stat.st_mtime_ns:
        return previous
    return {"sha256": _sha256(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "file": path.name}


def current_source_fingerprint(previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The fingerprint of the configured source now (the API's data cache compares it to notice another worker's
    write)."""
    return source_fingerprint(_source()[1], previous)


def _files_fingerprint(paths: list[Path], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    if not paths:
        return None
    try:
        signature = [[str(p), p.stat().st_size, p.stat().st_mtime_ns] for p in paths]
    except OSError:
        return None
    if previous and previous.get("signature") == signature:
        return previous
    digest = hashlib.sha256("".join(_sha256(p) for p in paths).encode()).hexdigest()
    return {"sha256": digest, "signature": signature, "file": ", ".join(p.name for p in paths)}


def get_graph() -> PortfolioGraph:
    """The current graph; built on first use and rebuilt when the source content changed. Raises GraphUnavailable."""
    with _lock:
        graph, pinned, previous = _state["graph"], _state["pinned"], _state["fingerprint"]
    if pinned and graph is not None:
        return graph
    _, path = _source()
    fingerprint = source_fingerprint(path, previous)
    if graph is not None and (fingerprint is None or (previous and fingerprint["sha256"] == previous["sha256"])):
        if fingerprint is not previous:
            with _lock:
                _state["fingerprint"] = fingerprint
        return graph
    failed = _state["failed"]
    if failed and fingerprint and failed[0] == fingerprint["sha256"]:
        raise GraphUnavailable(f"Graph not built – the unchanged source data failed before: {failed[1]}")
    return rebuild(fingerprint=fingerprint)


def rebuild(dataset: PortfolioDataset | None = None, *, fingerprint: dict[str, Any] | None = None) -> PortfolioGraph:
    """Builds the graph from the configured source (or `dataset`) and makes it the current one."""
    source, path = _source()
    try:
        if dataset is None:
            from app.data_adapters import get_data_provider

            fingerprint = fingerprint or source_fingerprint(path)  # taken BEFORE loading: a later edit is detected
            dataset = get_data_provider().load_dataset()
        graph = build_graph(dataset, source=source)
    except Exception as exc:
        message = f"Building the dependency graph failed: {str(exc).splitlines()[0] if str(exc) else exc!r}"
        if fingerprint:
            _state["failed"] = (fingerprint["sha256"], message[:300])
        raise GraphUnavailable(message) from exc
    with _lock:
        _state.update(graph=graph, fingerprint=fingerprint, pinned=False, failed=None, version=_state["version"] + 1)
    logger.info("Dependency graph built in %.2f s: %s", graph.build_seconds, graph.counts())
    _project(graph)
    return graph


def pin(dataset: PortfolioDataset, source: str = "pinned") -> PortfolioGraph:
    """Uses a graph built from `dataset` until reset() – for tests and the evaluation (no source file)."""
    graph = build_graph(dataset, source=source)
    with _lock:
        _state.update(graph=graph, fingerprint=None, pinned=True, failed=None, version=_state["version"] + 1)
    _project(graph)
    return graph


def _project(graph: PortfolioGraph) -> None:
    """Writes the new graph into the second backend when one is configured (GRAPH_BACKEND=neo4j); never raises."""
    from app.graph.backends import on_rebuild

    on_rebuild(graph)


def reset() -> None:
    """Drops the current graph; the next query builds it from the configured source."""
    with _lock:
        _state.update(graph=None, fingerprint=None, pinned=False, failed=None)


def current_metadata() -> dict[str, Any] | None:
    """Status for the Admin Console without building anything. None if no graph has been built yet."""
    with _lock:
        graph = _state["graph"]
    if graph is None:
        return None
    return {"counts": graph.counts(), "built_at": graph.built_at, "source": graph.source,
            "build_seconds": graph.build_seconds}


def graph_version() -> int:
    """Changes whenever the graph is rebuilt (cache key for the UI)."""
    with _lock:
        return _state["version"]
