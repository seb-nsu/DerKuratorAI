"""Neo4j as the second graph backend (GRAPH_BACKEND=neo4j): a projection of the in-memory graph, queried in Cypher.

Neo4j never holds data of its own. Every rebuild (and pin) of the in-memory graph writes the same graph into Neo4j in
batches (UNWIND); the queries then run there and must answer exactly like the NetworkX backend.

Namespace: every node carries a label with the prefix `DerKurator_` and the key of the graph version it belongs to
(`gk`, a hash over the projected content). Nothing outside these labels is read, written or deleted, so a shared
instance is safe – a database of its own is still preferred (docs/PILOT_OPERATIONS.md). A version is written beside
the previous one and marked complete on its meta node last; then the other versions are deleted, meta node first.
A query checks after reading that its version is still complete, so it never answers from a half-written or
half-deleted projection. Several processes share one projection: a version that is already complete is not written
again, and a uniqueness constraint on the meta node lets only one process write it.

Failure: if Neo4j is unreachable, refuses the login, is too old or a query fails, the in-memory graph answers instead
(same data, so the same result), the reason is logged once and shown in the Admin Console, and a new attempt follows
after RETRY_SECONDS. The password is never logged or shown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

from app.core import clock
from app.core.formatting import timestamp
from app.data_adapters.dataset import CLOSED_TASK_STATES
from app.graph.backends import TASK_FIELDS, GraphBackend, NetworkXBackend, by_hops, distinct
from app.graph.portfolio_graph import PortfolioGraph

logger = logging.getLogger(__name__)

PREFIX = "DerKurator_"
PROJECT, TASK, TEAM, RISK, META = (f"{PREFIX}{n}" for n in ("Project", "Task", "Team", "Risk", "Meta"))
NODE_LABELS = (PROJECT, TASK, TEAM, RISK)
SCHEMA_VERSION = 1  # part of the version key: a changed projection format is written anew
MIN_SERVER_VERSION = (5, 21)  # ANY SHORTEST path patterns
BATCH_SIZE = 1000
RETRY_SECONDS = 30.0  # after a failure, the in-memory graph answers this long before Neo4j is tried again
CLAIM_WAIT_SECONDS = 30.0  # how long a process waits for another one writing the same version
STALE_CLAIM_SECONDS = 600.0  # an unfinished version older than this was abandoned (writer crashed) and is replaced
_CLOSED = sorted(CLOSED_TASK_STATES)

_lock = threading.RLock()
_driver_cache: dict[str, Any] = {"key": None, "driver": None, "schema": False}
_state: dict[str, Any] = {"graph": None, "key": None, "synced_at": None, "sync_seconds": None, "reused": False,
                          "error": None, "failed_graph": None, "failed_at": 0.0}


class Neo4jFailure(RuntimeError):
    """Neo4j could not answer; the in-memory graph takes over."""


# =========================================================================== connection
def _settings():
    from app.core.config import get_settings

    return get_settings()


def server_label(uri: str) -> str:
    """scheme://host:port of the URI – never user info or query parameters."""
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    return f"{parsed.scheme}://{host}{f':{parsed.port}' if parsed.port else ''}" if parsed.scheme else "not set"


def _scrub(exc: BaseException) -> str:
    """First line of the error (the server's own message for a Neo4j error), never the password."""
    message = str(getattr(exc, "message", None) or exc)
    message = (message.splitlines() or [""])[0].rstrip(": ") or "no details"
    password = _settings().neo4j_password
    if password:
        message = message.replace(password, "***")
    return f"{exc.__class__.__name__}: {message}"[:300]


def _driver():
    s = _settings()
    if not s.neo4j_uri:
        raise Neo4jFailure("NEO4J_URI is not set")
    key = hashlib.sha256(f"{s.neo4j_uri}\0{s.neo4j_user}\0{s.neo4j_password}".encode()).hexdigest()
    with _lock:
        if _driver_cache["key"] != key:
            import neo4j

            old, _driver_cache["driver"] = _driver_cache["driver"], None
            if old is not None:
                old.close()
            _state.update(graph=None, key=None)  # another server or account: its projection is checked anew
            _driver_cache.update(key=key, schema=False, driver=neo4j.GraphDatabase.driver(
                s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password), connection_timeout=5.0,
                connection_acquisition_timeout=10.0, max_transaction_retry_time=3.0,
                notifications_min_severity="OFF"))
        return _driver_cache["driver"]


def close() -> None:
    """Closes the driver and forgets the projection state (tests, settings reload)."""
    with _lock:
        driver = _driver_cache["driver"]
        _driver_cache.update(key=None, driver=None, schema=False)
        _state.update(graph=None, key=None, synced_at=None, sync_seconds=None, reused=False, error=None,
                      failed_graph=None, failed_at=0.0)
    if driver is not None:
        driver.close()


def _run(cypher: str, *, write: bool = False, **params: Any) -> list[dict[str, Any]]:
    """One statement in its own managed transaction on the configured database."""
    driver = _driver()
    with driver.session(database=_settings().neo4j_database) as session:
        work = session.execute_write if write else session.execute_read
        return work(lambda tx: tx.run(cypher, **params).data())


def _server_version() -> tuple[tuple[int, ...], str, str]:
    """(major, minor), version and edition. One attempt without retries, so an unreachable server fails fast."""
    with _driver().session(database=_settings().neo4j_database) as session:
        row = session.run("CALL dbms.components() YIELD versions, edition "
                          "RETURN versions[0] AS version, edition").single()
    numbers = tuple(int(p) for p in str(row["version"]).split("-")[0].split(".")[:2] if p.isdigit())
    return numbers, str(row["version"]), str(row["edition"])


def _ensure_schema() -> None:
    """Indexes for the lookups and the uniqueness constraint of the meta node (once per driver). Without the right
    to create them the projection still works, only slower and without the guard against two writers."""
    if _driver_cache["schema"]:
        return
    statements = [f"CREATE CONSTRAINT derkurator_meta_gk IF NOT EXISTS FOR (m:{META}) REQUIRE m.gk IS UNIQUE"]
    statements += [f"CREATE INDEX derkurator_{label.lower()}_key IF NOT EXISTS FOR (n:{label}) ON (n.gk, {prop})"
                   for label, prop in ((PROJECT, "n.id"), (TASK, "n.id"), (TEAM, "n.name"), (RISK, "n.seq"))]
    for statement in statements:
        try:
            _run(statement, write=True)
        except Exception as exc:  # noqa: BLE001 - missing schema rights are a warning, not a failure
            logger.warning("Neo4j: could not create index or constraint (%s)", _scrub(exc))
    _driver_cache["schema"] = True


# =========================================================================== projection
def _payload(graph: PortfolioGraph) -> dict[str, list[dict[str, Any]]]:
    """The graph as rows for the batch writes; the seq fields keep the order of the in-memory graph."""
    def props(row: dict[str, Any], *drop: str) -> dict[str, Any]:
        return {k: v for k, v in row.items() if v is not None and k not in drop}

    team_seq = {team: i for i, team in enumerate(graph.teams)}
    return {
        "projects": [{"id": pid, "seq": i, "props": props(p)} for i, (pid, p) in enumerate(graph.projects.items())],
        "teams": [{"name": team, "seq": i} for team, i in team_seq.items()],
        "tasks": [{"id": tid, "seq": i, "props": props(t)} for i, (tid, t) in enumerate(graph.tasks.items())],
        "risks": [{"seq": i, "project": r["project"], "props": props(r)} for i, r in enumerate(graph.risks)],
        "belongs": [{"task": tid, "project": pid} for pid, tids in graph.tasks_by_project.items() for tid in tids],
        "handled": [{"task": tid, "team": t["team"]} for tid, t in graph.tasks.items()
                    if t.get("team") and t["team"] in team_seq],
        "blocks": [{"a": a, "b": b} for a, b in graph.task_chain.edges()],
        "allocations": [{"team": team, "project": pid, "seq": i, "props": props(a, "team", "project")}
                        for i, ((team, pid), a) in enumerate(graph.allocations.items())],
        "depends": [{"s": s, "p": p, "seq": i, "props": props(e)}
                    for i, (s, p, e) in enumerate(graph.dependencies.edges(data=True))],
    }


def graph_key(payload: dict[str, Any]) -> str:
    """Content key of a projection: the same graph gets the same key in every process."""
    text = json.dumps([SCHEMA_VERSION, payload], sort_keys=True, default=str, allow_nan=True)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


_WRITES = {
    "projects": f"UNWIND $rows AS row CREATE (n:{PROJECT}) SET n = row.props, n.gk = $gk, n.id = row.id, "
                "n.seq = row.seq",
    "teams": f"UNWIND $rows AS row CREATE (n:{TEAM} {{gk: $gk, name: row.name, seq: row.seq}})",
    "tasks": f"UNWIND $rows AS row CREATE (n:{TASK}) SET n = row.props, n.gk = $gk, n.id = row.id, n.seq = row.seq",
    "risks": f"UNWIND $rows AS row MATCH (p:{PROJECT} {{gk: $gk, id: row.project}}) "
             f"CREATE (p)-[:HAS_RISK]->(r:{RISK}) SET r = row.props, r.gk = $gk, r.seq = row.seq",
    "belongs": f"UNWIND $rows AS row MATCH (t:{TASK} {{gk: $gk, id: row.task}}) "
               f"MATCH (p:{PROJECT} {{gk: $gk, id: row.project}}) CREATE (t)-[:BELONGS_TO]->(p)",
    "handled": f"UNWIND $rows AS row MATCH (t:{TASK} {{gk: $gk, id: row.task}}) "
               f"MATCH (m:{TEAM} {{gk: $gk, name: row.team}}) CREATE (t)-[:HANDLED_BY]->(m)",
    "blocks": f"UNWIND $rows AS row MATCH (a:{TASK} {{gk: $gk, id: row.a}}) MATCH (b:{TASK} {{gk: $gk, id: row.b}}) "
              "CREATE (a)-[:BLOCKS]->(b)",
    "allocations": f"UNWIND $rows AS row MATCH (m:{TEAM} {{gk: $gk, name: row.team}}) "
                   f"MATCH (p:{PROJECT} {{gk: $gk, id: row.project}}) "
                   "CREATE (m)-[a:ALLOCATED_TO]->(p) SET a = row.props, a.seq = row.seq",
    "depends": f"UNWIND $rows AS row MATCH (s:{PROJECT} {{gk: $gk, id: row.s}}) "
               f"MATCH (p:{PROJECT} {{gk: $gk, id: row.p}}) CREATE (s)-[e:DEPENDS_ON]->(p) SET e = row.props, "
               "e.seq = row.seq",
}


def _delete_versions(op: str, gk: str) -> int:
    """Deletes the meta nodes, then the nodes (with their relationships) of the versions whose key is `op` ("=" or
    "<>") `gk`, in batches. Only DerKurator labels are touched."""
    if op not in ("=", "<>"):
        raise ValueError(op)
    _run(f"MATCH (m:{META}) WHERE m.gk {op} $gk DELETE m", write=True, gk=gk)
    labels = "|".join(NODE_LABELS)
    deleted = 0
    while True:
        n = _run(f"MATCH (n:{labels}) WHERE n.gk {op} $gk WITH n LIMIT {BATCH_SIZE} DETACH DELETE n "
                 "RETURN count(*) AS n", write=True, gk=gk)[0]["n"]
        deleted += n
        if n < BATCH_SIZE:
            return deleted


def _meta(gk: str) -> dict[str, Any] | None:
    rows = _run(f"MATCH (m:{META} {{gk: $gk}}) RETURN m.complete AS complete, m.claimed_at AS claimed_at", gk=gk)
    return rows[0] if rows else None


def _claim(gk: str) -> bool:
    """True if this process may write version `gk`; False if it is complete already (written by another process).
    Waits while another process writes it."""
    import neo4j

    deadline = time.monotonic() + CLAIM_WAIT_SECONDS
    while True:
        meta = _meta(gk)
        if meta and meta["complete"]:
            return False
        if meta and time.time() - (meta["claimed_at"] or 0) / 1000 > STALE_CLAIM_SECONDS:
            _delete_versions("=", gk)  # abandoned by a crashed writer
            meta = None
        if meta is None:
            try:
                _run(f"CREATE (m:{META} {{gk: $gk, complete: false, schema: $schema, claimed_at: timestamp()}})",
                     write=True, gk=gk, schema=SCHEMA_VERSION)
                return True
            except neo4j.exceptions.ConstraintError:
                pass  # another process claimed it a moment ago
        if time.monotonic() > deadline:
            raise Neo4jFailure("another process is still writing this graph version into Neo4j")
        time.sleep(0.5)


def _write(gk: str, payload: dict[str, list[dict[str, Any]]]) -> None:
    for part, cypher in _WRITES.items():
        rows = payload[part]
        for start in range(0, len(rows), BATCH_SIZE):
            _run(cypher, write=True, gk=gk, rows=rows[start:start + BATCH_SIZE])
    counts = {k: len(v) for k, v in payload.items()}
    done = _run(f"MATCH (m:{META} {{gk: $gk}}) SET m.complete = true, m.synced_at = $at, m.counts = $counts "
                "RETURN count(m) AS n", write=True, gk=gk, at=clock.stamp(), counts=json.dumps(counts))[0]["n"]
    if not done:
        raise Neo4jFailure("the graph version was removed while it was written (another process replaced it)")


def sync(graph: PortfolioGraph) -> bool:
    """Writes `graph` into Neo4j unless that version is there already, and removes every other DerKurator version.
    True when Neo4j answers for `graph` afterwards; on a failure the reason is recorded and False returned."""
    with _lock:
        started = time.perf_counter()
        try:
            payload = _payload(graph)
            gk = graph_key(payload)
            numbers, version, _edition = _server_version()
            if numbers < MIN_SERVER_VERSION:
                raise Neo4jFailure(f"Neo4j {version} is too old – {'.'.join(map(str, MIN_SERVER_VERSION))} or later "
                                   "is required")
            _ensure_schema()
            written = _claim(gk)
            if written:
                try:
                    _write(gk, payload)
                except Exception:
                    _delete_versions("=", gk)  # no half-written version is left behind
                    raise
            removed = _delete_versions("<>", gk)
        except Exception as exc:  # noqa: BLE001 - any Neo4j problem leaves the in-memory graph in charge
            _failed(graph, exc)
            return False
        seconds = round(time.perf_counter() - started, 3)
        was_failing = _state["error"] is not None
        _state.update(graph=graph, key=gk, synced_at=clock.stamp(), sync_seconds=seconds, reused=not written,
                      error=None, failed_graph=None, failed_at=0.0)
    if was_failing:
        logger.warning("Neo4j graph backend available again – queries run in Neo4j")
    logger.info("Dependency graph %s Neo4j in %.2f s (version %s, %d old nodes removed)",
                "written into" if written else "already in", seconds, gk[:8], removed)
    return True


def _failed(graph: PortfolioGraph | None, exc: BaseException) -> None:
    message = _scrub(exc)
    with _lock:
        first = _state["error"] is None
        _state.update(error=message, failed_graph=graph, failed_at=time.monotonic())
        if graph is None or _state["graph"] is graph:
            _state.update(graph=None)
    if first:  # once per outage, not per query
        logger.warning("Neo4j graph backend not available – the in-memory graph (NetworkX) answers instead: %s",
                       message)


def backend_for(graph: PortfolioGraph) -> GraphBackend:
    """The Neo4j backend if the projection of `graph` is in place (written now if needed), else the in-memory one."""
    with _lock:
        synced = _state["graph"] is graph
        recent_failure = (_state["failed_graph"] is graph
                          and time.monotonic() - _state["failed_at"] < RETRY_SECONDS)
    if not synced and (recent_failure or not sync(graph)):
        return NetworkXBackend(graph)
    with _lock:
        if _state["graph"] is not graph:  # replaced in between
            return NetworkXBackend(graph)
        return Neo4jBackend(graph, _state["key"])


# =========================================================================== queries
class Neo4jBackend(GraphBackend):
    """The graph queries in Cypher on the projection of one graph version. Every query falls back to the in-memory
    graph when Neo4j fails or the version is gone."""

    name = "neo4j"

    def __init__(self, graph: PortfolioGraph, gk: str) -> None:
        self.gk = gk
        self.fallback = NetworkXBackend(graph)

    def _read(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        def work(tx):
            rows = tx.run(cypher, gk=self.gk, **params).data()
            # checked AFTER reading: a version is marked complete last and its meta node deleted first
            complete = tx.run(f"MATCH (m:{META} {{gk: $gk}}) WHERE m.complete RETURN count(m) AS n",
                              gk=self.gk).single()["n"]
            return rows, complete

        with _driver().session(database=_settings().neo4j_database) as session:
            rows, complete = session.execute_read(work)
        if not complete:
            raise Neo4jFailure("the graph version in Neo4j was replaced or removed")
        return rows

    def _query(self, method: str, *args: Any, cypher: str, convert=None, **params: Any) -> Any:
        try:
            rows = self._read(cypher, **params)
            return convert(rows) if convert else rows
        except Exception as exc:  # noqa: BLE001 - same data in memory: the answer stays the same
            _failed(self.fallback.graph, exc)
            return getattr(self.fallback, method)(*args)

    def known_projects(self, ids: list[str]) -> set[str]:
        return self._query("known_projects", ids, ids=list(ids), convert=lambda rows: {r["id"] for r in rows},
                           cypher=f"MATCH (p:{PROJECT} {{gk: $gk}}) WHERE p.id IN $ids RETURN p.id AS id")

    @staticmethod
    def _direct(rows: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
        """Direct edges ([seq, values ...]) to distinct values in source order, sorted by (hops, id)."""
        out = []
        for r in rows:
            edges = sorted(r.pop("edges"), key=lambda e: e[0])
            out.append({**r, **{f: distinct(e[i + 1] for e in edges) for i, f in enumerate(fields)}})
        return by_hops(out)

    def dependents(self, pid: str, max_depth: int) -> list[dict[str, Any]]:
        return self._query(
            "dependents", pid, max_depth, pid=pid,
            convert=lambda rows: self._direct(rows, "dep_types", "dep_statuses"),
            cypher=f"""
            MATCH (s:{PROJECT} {{gk: $gk, id: $pid}})
            MATCH path = ANY SHORTEST (d:{PROJECT})-[:DEPENDS_ON]->{{1,{int(max_depth)}}}(s)
            WHERE d <> s
            RETURN d.id AS id, d.name AS name, d.status AS status, d.budget AS budget, length(path) AS hops,
                   [(d)-[e:DEPENDS_ON]->(s) | [e.seq, e.type, e.status]] AS edges
            """)

    def predecessors(self, pid: str, max_depth: int) -> list[dict[str, Any]]:
        return self._query(
            "predecessors", pid, max_depth, pid=pid,
            convert=lambda rows: self._direct(rows, "dep_types", "dep_statuses", "required_by"),
            cypher=f"""
            MATCH (s:{PROJECT} {{gk: $gk, id: $pid}})
            MATCH path = ANY SHORTEST (s)-[:DEPENDS_ON]->{{1,{int(max_depth)}}}(u:{PROJECT})
            WHERE u <> s
            RETURN u.id AS id, u.name AS name, u.status AS status, length(path) AS hops,
                   [(s)-[e:DEPENDS_ON]->(u) | [e.seq, e.type, e.status, e.required_by]] AS edges
            """)

    def edge_types(self, a: str, b: str) -> list[str | None]:
        return self._query(
            "edge_types", a, b, a=a, b=b, convert=lambda rows: [r["type"] for r in rows],
            cypher=f"MATCH (:{PROJECT} {{gk: $gk, id: $a}})-[e:DEPENDS_ON]->(:{PROJECT} {{gk: $gk, id: $b}}) "
                   "RETURN e.type AS type ORDER BY e.seq")

    def shared_teams(self, a: str, b: str) -> list[dict[str, Any]]:
        # two MATCH clauses: with a == b both allocations are the same relationship (as in the in-memory graph)
        return self._query(
            "shared_teams", a, b, a=a, b=b,
            cypher=f"""
            MATCH (t:{TEAM} {{gk: $gk}})-[x:ALLOCATED_TO]->(:{PROJECT} {{gk: $gk, id: $a}})
            MATCH (t)-[y:ALLOCATED_TO]->(:{PROJECT} {{gk: $gk, id: $b}})
            RETURN t.name AS team, x.utilization_pct AS util_a, y.utilization_pct AS util_b,
                   x.current_fte AS fte_a, y.current_fte AS fte_b
            ORDER BY t.seq
            """)

    def risks(self, keyword: str = "") -> list[dict[str, Any]]:
        return self._query(
            "risks", keyword, kw=keyword,
            cypher=f"""
            MATCH (p:{PROJECT} {{gk: $gk}})-[:HAS_RISK]->(r:{RISK})
            WHERE $kw = '' OR toLower(coalesce(r.cluster, '')) CONTAINS $kw
               OR toLower(coalesce(r.short_description, '')) CONTAINS $kw
            RETURN p.id AS id, p.name AS name, p.status AS status, p.budget AS budget, r.cluster AS cluster,
                   r.impact AS impact, r.short_description AS example
            ORDER BY r.seq
            """)

    def overloaded_allocations(self, limit: float) -> list[dict[str, Any]]:
        return self._query(
            "overloaded_allocations", limit, limit=float(limit),
            cypher=f"""
            MATCH (t:{TEAM} {{gk: $gk}})-[a:ALLOCATED_TO]->(p:{PROJECT})
            WHERE a.utilization_pct > $limit
            RETURN t.name AS team, p.id AS id, p.name AS name, a.utilization_pct AS peak,
                   size(a.overloaded_months) AS months
            ORDER BY a.seq
            """)

    def overloaded_months(self) -> list[dict[str, Any]]:
        return self._query(
            "overloaded_months",
            cypher=f"""
            MATCH (t:{TEAM} {{gk: $gk}})-[a:ALLOCATED_TO]->(p:{PROJECT})
            UNWIND range(0, size(a.overloaded_months) - 1) AS i
            RETURN t.name AS team, a.overloaded_months[i] AS month, p.id AS id
            ORDER BY a.seq, i
            """)

    def open_tasks(self, pid: str, chain_depth: int = 0) -> list[dict[str, Any]]:
        fields = ", ".join(f"t.{f} AS {f}" for f in TASK_FIELDS)
        blocked = (f""", COUNT {{
                       MATCH ANY SHORTEST (t)-[:BLOCKS]->{{1,{int(chain_depth)}}}(n:{TASK})
                       WHERE n.state IS NOT NULL AND NOT n.state IN $closed
                   }} AS blocks_n""" if chain_depth else "")
        return self._query(
            "open_tasks", pid, chain_depth, pid=pid, closed=_CLOSED,
            cypher=f"""
            MATCH (t:{TASK})-[:BELONGS_TO]->(:{PROJECT} {{gk: $gk, id: $pid}})
            WHERE t.state IS NOT NULL AND NOT t.state IN $closed
            RETURN t.id AS task, {fields}{blocked}
            ORDER BY t.seq
            """)

    def successor_edges(self, pid: str) -> list[dict[str, Any]]:
        return self._query(
            "successor_edges", pid, pid=pid,
            cypher=f"""
            MATCH (s:{PROJECT} {{gk: $gk}})-[e:DEPENDS_ON]->(p:{PROJECT} {{gk: $gk, id: $pid}})
            WHERE s <> p
            RETURN s.id AS id, s.name AS name, e.type AS type, e.status AS status, e.required_by AS required_by,
                   e.dependency_number AS dependency
            ORDER BY e.seq
            """)

    def blocking_edges(self) -> list[dict[str, Any]]:
        return self._query(
            "blocking_edges",
            cypher=f"""
            MATCH (s:{PROJECT} {{gk: $gk}})-[e:DEPENDS_ON]->(p:{PROJECT})
            WHERE e.status = 'At Risk' OR p.status = 'Red'
            RETURN s.id AS id, s.name AS name, s.status AS status, s.budget AS budget, p.id AS blocker,
                   e.type AS type
            ORDER BY e.seq
            """)


# =========================================================================== status
def _live_counts(gk: str) -> tuple[int, int]:
    nodes = edges = 0
    for label in NODE_LABELS:
        row = _run(f"MATCH (n:{label} {{gk: $gk}}) RETURN count(n) AS nodes, sum(COUNT {{ (n)-->() }}) AS edges",
                   gk=gk)[0]
        nodes, edges = nodes + row["nodes"], edges + (row["edges"] or 0)
    return nodes, edges


def status() -> dict[str, Any]:
    """Health of the Neo4j backend for the Admin Console: reachable, node and relationship counts of the current
    version, last sync. Read-only: nothing is written or synced here."""
    s = _settings()
    with _lock:
        state = dict(_state)
    rows = [{"Setting": "Graph backend", "Value": "Neo4j (projection of the in-memory graph)"},
            {"Setting": "Server", "Value": f"{server_label(s.neo4j_uri)} · database {s.neo4j_database} · user "
                                           f"{s.neo4j_user}"}]
    reachable, detail = False, ""
    try:
        _numbers, version, edition = _server_version()
        reachable, detail = True, f"yes · Neo4j {version} {edition}"
    except Exception as exc:  # noqa: BLE001 - the status page reports, it does not fail
        detail = f"no – {_scrub(exc)}"
    rows.append({"Setting": "Reachable", "Value": detail})
    active = "neo4j" if state["graph"] is not None and state["error"] is None else "networkx"
    if reachable and state["key"]:
        try:
            nodes, edges = _live_counts(state["key"])
            graph = state["graph"]
            expected = ""
            if graph is not None:
                counts = graph.counts()
                want_nodes = sum(v for k, v in counts.items() if not k.isupper())
                want_edges = sum(v for k, v in counts.items() if k.isupper())
                expected = (" (same as the in-memory graph)" if (nodes, edges) == (want_nodes, want_edges)
                            else f" (in-memory graph: {want_nodes} nodes, {want_edges} relationships)")
            rows.append({"Setting": "Nodes / relationships", "Value": f"{nodes} / {edges}{expected}"})
        except Exception as exc:  # noqa: BLE001
            rows.append({"Setting": "Nodes / relationships", "Value": f"not readable – {_scrub(exc)}"})
    if state["synced_at"]:
        how = "version was already in Neo4j" if state["reused"] else "written"
        rows.append({"Setting": "Last sync",
                     "Value": f"{timestamp(state['synced_at'])} · {state['sync_seconds']:g} s · {how}"})
    else:
        rows.append({"Setting": "Last sync", "Value": "none in this process yet (written on the next graph build)"})
    rows.append({"Setting": "Queries answered by", "Value": "Neo4j" if active == "neo4j"
                 else "NetworkX (in memory) – fallback"})
    warning = None
    if state["error"]:
        warning = (f"Neo4j is configured but not used ({state['error']}). The in-memory graph (NetworkX) answers "
                   "instead – same data, same results; Neo4j is tried again automatically.")
    elif not reachable:
        warning = f"Neo4j is configured but not reachable ({detail.removeprefix('no – ')})."
    return {"configured": "neo4j", "active": active, "warning": warning, "rows": rows}
