"""The query interface of the dependency graph and its two implementations.

The graph tools (`app/crew/tools/graph_tools.py`) ask a `GraphBackend` for plain rows and do the scope filter,
aggregation and wording themselves, so both backends give the same answer:

  NetworkXBackend  the in-memory graph of `app/graph/portfolio_graph.py` (the default)
  Neo4jBackend     the same graph projected into Neo4j and queried in Cypher (`app/graph/neo4j_backend.py`),
                   switched on with GRAPH_BACKEND=neo4j

Every method returns its rows in a defined order (the order of the source data where the tools depend on it), so a
tie in a later sort falls the same way on both backends. The in-memory graph is always built: Neo4j is only a
projection of it, and when Neo4j cannot answer, the NetworkX backend answers instead (same data, same result).
"""
from __future__ import annotations

from typing import Any

import networkx as nx

from app.data_adapters.dataset import CLOSED_TASK_STATES
from app.graph.portfolio_graph import PortfolioGraph

TASK_FIELDS = ("short_description", "state", "percent_complete", "remaining_hours", "planned_start", "team")


def is_open(task: dict[str, Any]) -> bool:
    """An open task: a known state that is not closed (a task without a state counts as neither)."""
    return task.get("state") is not None and task["state"] not in CLOSED_TASK_STATES


def distinct(values) -> list:
    return list(dict.fromkeys(v for v in values if v is not None))


def by_hops(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (r["hops"], r["id"]))


class GraphBackend:
    """The queries the graph tools need. Project ids are canonical project numbers."""

    name = "abstract"

    def known_projects(self, ids: list[str]) -> set[str]:
        """The ids of `ids` that are projects of the graph."""
        raise NotImplementedError

    def dependents(self, pid: str, max_depth: int) -> list[dict[str, Any]]:
        """Projects that wait for `pid` directly or indirectly (up to max_depth levels), sorted by (hops, id): id,
        name, status, budget, hops and the distinct types and statuses of the direct dependencies on `pid`."""
        raise NotImplementedError

    def predecessors(self, pid: str, max_depth: int) -> list[dict[str, Any]]:
        """Projects `pid` waits for directly or indirectly, sorted by (hops, id), with the distinct types, statuses and
        required-by dates of the direct dependencies."""
        raise NotImplementedError

    def edge_types(self, a: str, b: str) -> list[str | None]:
        """The type of every dependency a -> b (a waits for b), in source order."""
        raise NotImplementedError

    def shared_teams(self, a: str, b: str) -> list[dict[str, Any]]:
        """Teams allocated to both projects, by team name: team, util_a, util_b, fte_a, fte_b."""
        raise NotImplementedError

    def risks(self, keyword: str = "") -> list[dict[str, Any]]:
        """Risks whose cluster or description contains `keyword` (lower case; '' = all), in graph order, with their
        project: id, name, status, budget, cluster, impact, example."""
        raise NotImplementedError

    def overloaded_allocations(self, limit: float) -> list[dict[str, Any]]:
        """Allocations with a peak utilisation above `limit`, in allocation order: team, id, name, peak, months."""
        raise NotImplementedError

    def overloaded_months(self) -> list[dict[str, Any]]:
        """Every overloaded month of every allocation, in allocation order: team, month, id."""
        raise NotImplementedError

    def open_tasks(self, pid: str, chain_depth: int = 0) -> list[dict[str, Any]]:
        """Open tasks of a project in source order (task plus TASK_FIELDS); with chain_depth > 0 also `blocks_n`, the
        open tasks in the blocking chain after each task (up to chain_depth levels)."""
        raise NotImplementedError

    def successor_edges(self, pid: str) -> list[dict[str, Any]]:
        """Every direct dependency on `pid` from another project (parallel ones in source order; the caller sorts):
        id, name, type, status, required_by, dependency."""
        raise NotImplementedError

    def blocking_edges(self) -> list[dict[str, Any]]:
        """Dependencies that are 'At Risk' or wait for a red project, by successor in project order: id, name,
        status, budget (of the successor), blocker, type."""
        raise NotImplementedError


class NetworkXBackend(GraphBackend):
    """Today's queries on the in-memory graph."""

    name = "networkx"

    def __init__(self, graph: PortfolioGraph) -> None:
        self.graph = graph

    def _edges(self, a: str, b: str) -> list[dict[str, Any]]:
        """Attributes of every dependency a -> b (a waits for b), in source order."""
        data = self.graph.dependencies.get_edge_data(a, b) or {}
        return list(data.values())

    def known_projects(self, ids: list[str]) -> set[str]:
        return {i for i in ids if i in self.graph.projects}

    def dependents(self, pid: str, max_depth: int) -> list[dict[str, Any]]:
        g = self.graph
        if pid not in g.dependencies:
            return []
        hops = nx.single_source_shortest_path_length(g.dependencies.reverse(copy=False), pid, cutoff=max_depth)
        rows = []
        for d, n in hops.items():
            if d == pid:
                continue
            direct = self._edges(d, pid)
            p = g.projects[d]
            rows.append({"id": d, "name": p.get("name"), "status": p.get("status"), "budget": p.get("budget"),
                         "hops": n, "dep_types": distinct(e.get("type") for e in direct),
                         "dep_statuses": distinct(e.get("status") for e in direct)})
        return by_hops(rows)

    def predecessors(self, pid: str, max_depth: int) -> list[dict[str, Any]]:
        g = self.graph
        if pid not in g.dependencies:
            return []
        hops = nx.single_source_shortest_path_length(g.dependencies, pid, cutoff=max_depth)
        rows = []
        for u, n in hops.items():
            if u == pid:
                continue
            direct = self._edges(pid, u)
            p = g.projects[u]
            rows.append({"id": u, "name": p.get("name"), "status": p.get("status"), "hops": n,
                         "dep_types": distinct(e.get("type") for e in direct),
                         "dep_statuses": distinct(e.get("status") for e in direct),
                         "required_by": distinct(e.get("required_by") for e in direct)})
        return by_hops(rows)

    def edge_types(self, a: str, b: str) -> list[str | None]:
        return [e.get("type") for e in self._edges(a, b)]

    def shared_teams(self, a: str, b: str) -> list[dict[str, Any]]:
        al = self.graph.allocations
        return [{"team": team, "util_a": x["utilization_pct"], "util_b": al[(team, b)]["utilization_pct"],
                 "fte_a": x["current_fte"], "fte_b": al[(team, b)]["current_fte"]}
                for (team, pid), x in sorted(al.items()) if pid == a and (team, b) in al]

    def risks(self, keyword: str = "") -> list[dict[str, Any]]:
        g = self.graph
        return [{"id": r["project"], "name": g.projects[r["project"]].get("name"),
                 "status": g.projects[r["project"]].get("status"), "budget": g.projects[r["project"]].get("budget"),
                 "cluster": r.get("cluster"), "impact": r.get("impact"), "example": r.get("short_description")}
                for r in g.risks
                if not keyword or keyword in str(r.get("cluster") or "").lower()
                or keyword in str(r.get("short_description") or "").lower()]

    def overloaded_allocations(self, limit: float) -> list[dict[str, Any]]:
        g = self.graph
        return [{"team": a["team"], "id": a["project"], "name": g.projects[a["project"]].get("name"),
                 "peak": a["utilization_pct"], "months": len(a["overloaded_months"])}
                for a in g.allocations.values() if a["utilization_pct"] is not None and a["utilization_pct"] > limit]

    def overloaded_months(self) -> list[dict[str, Any]]:
        return [{"team": a["team"], "month": month, "id": a["project"]}
                for a in self.graph.allocations.values() for month in a["overloaded_months"]]

    def _blocked_count(self, task: str, depth: int) -> int:
        g = self.graph
        reach = nx.single_source_shortest_path_length(g.task_chain, task, cutoff=depth)
        blocked = {n for n, d in reach.items() if d >= 1}
        if any(reach.get(pred, depth) < depth for pred in g.task_chain.predecessors(task)):
            blocked.add(task)  # the chain leads back to the task itself
        return sum(1 for n in blocked if is_open(g.tasks[n]))

    def open_tasks(self, pid: str, chain_depth: int = 0) -> list[dict[str, Any]]:
        g = self.graph
        rows = []
        for t in g.tasks_by_project.get(pid, []):
            if not is_open(g.tasks[t]):
                continue
            row = {"task": t, **{k: g.tasks[t].get(k) for k in TASK_FIELDS}}
            if chain_depth:
                row["blocks_n"] = self._blocked_count(t, chain_depth)
            rows.append(row)
        return rows

    def successor_edges(self, pid: str) -> list[dict[str, Any]]:
        g = self.graph
        if pid not in g.dependencies:
            return []
        return [{"id": succ, "name": g.projects[succ].get("name"), "type": e.get("type"), "status": e.get("status"),
                 "required_by": e.get("required_by"), "dependency": e.get("dependency_number")}
                for succ, _, e in g.dependencies.in_edges(pid, data=True) if succ != pid]

    def blocking_edges(self) -> list[dict[str, Any]]:
        g = self.graph
        rows = []
        for succ, pred, e in g.dependencies.edges(data=True):
            if e.get("status") == "At Risk" or g.projects[pred].get("status") == "Red":
                p = g.projects[succ]
                rows.append({"id": succ, "name": p.get("name"), "status": p.get("status"), "budget": p.get("budget"),
                             "blocker": pred, "type": e.get("type")})
        return rows


def backend_for(graph: PortfolioGraph) -> GraphBackend:
    """The configured backend for `graph`: Neo4j when GRAPH_BACKEND=neo4j and the projection of this graph is in
    place, otherwise the in-memory graph (also the fallback when Neo4j fails – the reason is in backend_status)."""
    from app.core.config import get_settings

    if get_settings().graph_backend != "neo4j":
        return NetworkXBackend(graph)
    from app.graph import neo4j_backend

    return neo4j_backend.backend_for(graph)


def on_rebuild(graph: PortfolioGraph) -> None:
    """Called after every rebuild and pin: writes the new graph into Neo4j when it is the configured backend. Never
    raises – a failure leaves the in-memory graph in charge and is shown in the Admin Console."""
    from app.core.config import get_settings

    if get_settings().graph_backend == "neo4j":
        from app.graph import neo4j_backend

        neo4j_backend.sync(graph)


BACKEND_NOTE = ("Set in `.env` or the secrets: `GRAPH_BACKEND=networkx|neo4j`, `NEO4J_URI`, `NEO4J_USER`, "
                "`NEO4J_PASSWORD`, `NEO4J_DATABASE` (read-only here). Neo4j holds a copy of the in-memory graph; "
                "see docs/PILOT_OPERATIONS.md.")


def backend_status() -> dict[str, Any]:
    """Configured and active backend with its health, for the Admin Console (read-only; never the password)."""
    from app.core.config import get_settings

    if get_settings().graph_backend != "neo4j":
        status = {"configured": "networkx", "active": "networkx", "warning": None,
                  "rows": [{"Setting": "Graph backend", "Value": "NetworkX (in memory, default)"}]}
    else:
        from app.graph import neo4j_backend

        status = neo4j_backend.status()
    return {**status, "note": BACKEND_NOTE}
