"""Graph tools for the Portfolio Analyst ("Der Stratege") on the dependency graph.

Graph model (see app/graph/portfolio_graph.py):
  project s depends on project p (s waits for p), with type, status and required-by date per dependency
  tasks belong to a project and are handled by a team; task a blocks task b (a must finish before b)
  teams are allocated to projects (peak / average utilisation, overloaded months, current FTE); projects have risks
The graph contains no person names. Projects outside the RLS permission never appear with id, name, status, budget,
load or count: the tools report only visible rows and one neutral line that the PMO coordinates the rest.
Zero-setup: the graph is built on first access and rebuilt when the source data changes.
The queries go through a GraphBackend (app/graph/backends.py): the in-memory NetworkX graph by default, or its
projection in Neo4j with GRAPH_BACKEND=neo4j – same rows, so the same answers. Scope filter, aggregation and wording
stay here, for both backends.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.business_rules import current_rules
from app.core.config import get_settings
from app.crew.tools.context import (
    RESTRICTED_DEPENDENTS,
    RESTRICTED_PREDECESSORS,
    ProjectNotFound,
    allowed_project_ids,
    clear_facts,
    record_fact,
    record_tool_result,
    resolve_project_id,
)
from app.graph.backends import GraphBackend, backend_for
from app.graph.portfolio_graph import GraphUnavailable, get_graph
from app.llm.tools import tool

logger = logging.getLogger(__name__)

MAX_DEPTH = 5  # levels of indirect project dependencies
MAX_TASK_CHAIN = 15  # levels of a task's blocking chain
HIGH_IMPACT = ["High", "Very High", "Critical"]

__all__ = ["GraphUnavailable", "ANALYST_TOOLS"]


def check_connection() -> tuple[bool, str]:
    try:
        graph = get_graph()
        backend = backend_for(graph)
        where = "in Neo4j" if backend.name == "neo4j" else "in memory"
        note = ""
        if backend.name != "neo4j" and get_settings().graph_backend == "neo4j":
            note = " (Neo4j configured but not available – NetworkX answers, see Admin Console)"
        return True, f"{where} · {len(graph.projects)} projects{note}"
    except GraphUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"not available ({exc.__class__.__name__})"


# =========================================================================== graph queries
def _backend() -> GraphBackend:
    """The configured backend for the current graph (falls back to the in-memory graph when Neo4j fails)."""
    return backend_for(get_graph())


def _by_start(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (r[key] is None, r[key] or ""))


# =========================================================================== helpers
def _label(pid: str, name: str | None) -> str:
    return f"{pid} ({name})"


def _split(rows: list[dict[str, Any]], allowed: set[str]) -> tuple[list[dict[str, Any]], bool]:
    """Rows inside the scope, and whether any row lay outside it."""
    visible = [r for r in rows if r["id"] in allowed]
    return visible, len(visible) < len(rows)


def _eur(v: float | None) -> str:
    return f"€{(v or 0):,.0f}"


def _joined(values: list | None) -> str:
    """Distinct edge properties as text; empty values dropped."""
    return "/".join(sorted(str(v) for v in values or [] if v not in (None, "")))


def _guard(fn):
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            clear_facts("graph_unavailable", tool=fn.__name__)  # answered: an earlier outage of this tool is over
        except ProjectNotFound as exc:
            result = str(exc)
        except GraphUnavailable as exc:
            result = f"ERROR: {exc}"
            record_fact("graph_unavailable", tool=fn.__name__)
        except Exception as exc:
            logger.exception("Graph tool %s failed", fn.__name__)
            result = f"INTERNAL ERROR: graph tool defect in {fn.__name__} ({exc.__class__.__name__}: {exc}) – not a business rule."
        record_tool_result("graph", fn.__name__, result)  # verdict engine input
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# =========================================================================== core logic
@_guard
def downstream_impact(project_id: str) -> str:
    pid = resolve_project_id(project_id)
    rows = _backend().dependents(pid, MAX_DEPTH)  # the project itself (cycles) is never its own dependent
    visible, hidden = _split(rows, allowed_project_ids())
    # the threshold counts every dependent; `restricted` tells the verdict to name none of the hidden ones
    record_fact("domino", project=pid, affected=len(rows), direct=sum(1 for r in rows if r["hops"] == 1),
                **({"restricted": True} if hidden else {}))
    if not rows:
        return f"RESULT get_downstream_impact | {pid}: No dependent projects – no domino effect."
    scope = " in your scope" if hidden else ""
    lines = [f"RESULT get_downstream_impact | {pid}: {len(visible)} projects{scope} affected (domino effect)" if visible
             else f"RESULT get_downstream_impact | {pid}: domino effect outside your scope"]
    for r in visible:
        kind = (f"direct, {_joined(r['dep_types'])} dependency ({_joined(r['dep_statuses'])})" if r["hops"] == 1
                else f"indirect ({r['hops']} levels)")
        lines.append(f"- {_label(r['id'], r['name'])} – {kind}, RAG {r['status']}")
    if hidden:
        lines.append(f"- {RESTRICTED_DEPENDENTS}")
    if visible:
        lines.append(f"- Budget volume affected{scope}: {_eur(sum(r['budget'] or 0 for r in visible))}")
    return "\n".join(lines)


@_guard
def upstream_bottlenecks(project_id: str) -> str:
    pid = resolve_project_id(project_id)
    rows = _backend().predecessors(pid, MAX_DEPTH)
    if not rows:
        return f"RESULT get_upstream_bottlenecks | {pid}: No predecessor projects – not blocked."
    visible, hidden = _split(rows, allowed_project_ids())
    critical = [r for r in visible if r["status"] == "Red" or "At Risk" in (r["dep_statuses"] or [])]
    scope = " in your scope" if hidden else ""
    lines = [f"RESULT get_upstream_bottlenecks | {pid} waits for {len(visible)} projects{scope}, "
             f"{len(critical)} of them critical" if visible
             else f"RESULT get_upstream_bottlenecks | {pid} waits for projects outside your scope"]
    for r in visible:
        flag = " ⚠ BOTTLENECK" if r in critical else ""
        dep = (f"{_joined(r['dep_types'])}, {_joined(r['dep_statuses'])}, required by {_joined(r['required_by'])}"
               if _joined(r["dep_types"]) else f"{r['hops']} levels")
        lines.append(f"- {_label(r['id'], r['name'])} – {dep}, RAG {r['status']}{flag}")
    if hidden:
        lines.append(f"- {RESTRICTED_PREDECESSORS}")
    return "\n".join(lines)


@_guard
def shared_resources(project_id_a: str, project_id_b: str) -> str:
    a, b = resolve_project_id(project_id_a), resolve_project_id(project_id_b)
    backend = _backend()
    rows = backend.shared_teams(a, b)
    a_on_b, b_on_a = (next(iter(backend.edge_types(x, y)), None) for x, y in ((a, b), (b, a)))
    direct = [{"a_on_b": a_on_b, "b_on_a": b_on_a}] if {a, b} <= backend.known_projects([a, b]) else []
    lines = [f"RESULT find_shared_resources | {a} ↔ {b}: {len(rows)} shared teams"]
    for r in rows:
        lines.append(f"- {r['team']}: peak utilisation {a} {r['util_a']:.0f} % / {b} {r['util_b']:.0f} %, "
                     f"currently {r['fte_a'] or 0:g} / {r['fte_b'] or 0:g} FTE")
    if direct and direct[0]["a_on_b"]:
        lines.append(f"- Direct {direct[0]['a_on_b']} dependency: {a} waits for {b}")
    if direct and direct[0]["b_on_a"]:
        lines.append(f"- Direct {direct[0]['b_on_a']} dependency: {b} waits for {a}")
    if not rows:
        lines.append("- No shared teams – transfers are decoupled on the resource side.")
    return "\n".join(lines)


@_guard
def cluster_risk(keyword: str) -> str:
    kw = str(keyword or "").strip().lower()
    # one row per risk; scope filter and aggregation in Python, so counts, budgets and examples only ever come from
    # visible projects
    rows = _backend().risks(kw)
    allowed = allowed_project_ids()
    clusters: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["id"] not in allowed:
            continue
        c = clusters.setdefault(r["cluster"] or "Unclassified", {"risks": 0, "high": 0, "examples": [], "projects": {}})
        c["risks"] += 1
        c["high"] += int(r["impact"] in HIGH_IMPACT)
        if r["example"] and r["example"] not in c["examples"]:
            c["examples"].append(r["example"])
        c["projects"][r["id"]] = r
    if not clusters:
        return f"RESULT detect_cluster_risk | '{kw or '*'}': No matching risks in your project scope."
    ranked = sorted(clusters.items(), key=lambda kv: (-kv[1]["high"], -kv[1]["risks"], kv[0]))[:8]
    lines = [f"RESULT detect_cluster_risk | search term '{kw or '*'}' (systemic risk clusters)"]
    for cluster, c in ranked:
        members = list(c["projects"].values())
        red = sum(1 for m in members if m["status"] == "Red")
        budget = sum(m["budget"] or 0 for m in members)
        level = "HIGH" if c["high"] >= 5 or red >= 3 else "MEDIUM" if c["high"] >= 2 else "LOW"
        visible = sorted(_label(m["id"], m["name"]) for m in members)
        lines.append(
            f"- Cluster '{cluster}': concentration risk {level} – {c['risks']} risks ({c['high']} high impact) in "
            f"{len(members)} projects ({red} red), budget {_eur(budget)}\n  Examples: {'; '.join(c['examples'][:3])}\n"
            f"  Projects: {', '.join(visible)}"
        )
    return "\n".join(lines)


@_guard
def team_bottlenecks() -> str:
    limit = current_rules().team_capacity_limit_pct  # governance config; overloaded_months use it at seed time
    backend = _backend()
    rows = backend.overloaded_allocations(limit)
    by_month: dict[tuple[str, str], list[str]] = {}
    for m in backend.overloaded_months():
        by_month.setdefault((m["team"], m["month"]), []).append(m["id"])
    parallel = [{"team": team, "month": month, "ids": ids} for (team, month), ids in sorted(by_month.items())
                if len(ids) >= 2]
    if not rows:
        return f"RESULT identify_team_bottlenecks | No team above {limit:g} % utilisation."
    teams: dict[str, list[dict]] = {}
    for r in rows:
        teams.setdefault(r["team"], []).append(r)
    allowed = allowed_project_ids()
    hidden = {t for t, ps in teams.items() if any(p["id"] not in allowed for p in ps)}
    # only teams related to the scope, and only their visible allocations (no peak or months of hidden projects)
    teams = {t: [p for p in ps if p["id"] in allowed] for t, ps in teams.items()}
    teams = {t: ps for t, ps in teams.items() if ps}
    if not teams:
        return "RESULT identify_team_bottlenecks | No overloaded teams in your project scope."
    par_by_team: dict[str, list[dict]] = {}
    for p in parallel:
        par_by_team.setdefault(p["team"], []).append(p)
    lines = [f"RESULT identify_team_bottlenecks | {len(teams)} overloaded teams (utilization_pct > {limit:g} %)"]
    for team, loaded in sorted(teams.items(), key=lambda kv: -max(p["peak"] for p in kv[1])):
        projects = sorted(loaded, key=lambda x: (-x["peak"], x["id"]))
        detail = ", ".join(f"{_label(p['id'], p['name'])} up to {p['peak']:.0f} % ({p['months']} months)" for p in projects)
        outside = "; also loaded by projects outside your scope (the PMO coordinates)" if team in hidden else ""
        lines.append(f"- {team}: peak {projects[0]['peak']:.1f} % across {len(projects)} projects in parallel – "
                     f"{detail}{outside}")
        par = [dict(x, ids=[i for i in x["ids"] if i in allowed]) for x in par_by_team.get(team, [])]
        par = [x for x in par if len(x["ids"]) >= 2]
        if par:
            worst = max(par, key=lambda x: len(x["ids"]))
            lines.append(f"  ⚠ Parallel overload in {len(par)} months; max. {len(worst['ids'])} projects at once ({worst['month']})")
    return "\n".join(lines)


@_guard
def task_level_impact(project_number: str) -> str:
    pid = resolve_project_id(project_number)
    backend = _backend()
    own = _by_start([{"task": t["task"], "descr": t["short_description"], "state": t["state"],
                      "pct": t["percent_complete"], "hours": t["remaining_hours"],
                      "planned_start": t["planned_start"], "team": t["team"], "blocks_n": t["blocks_n"]}
                     for t in backend.open_tasks(pid, MAX_TASK_CHAIN)], "planned_start")
    downstream = []
    for d in backend.dependents(pid, MAX_DEPTH):
        tasks = _by_start([{"id": d["id"], "name": d["name"], "hops": d["hops"], "dep_types": d["dep_types"],
                            "task": t["task"], "descr": t["short_description"], "hours": t["remaining_hours"],
                            "start": t["planned_start"]}
                           for t in backend.open_tasks(d["id"])], "start")
        downstream += tasks
    allowed = allowed_project_ids()
    own_hours = sum(r["hours"] or 0 for r in own)
    lines = [f"RESULT get_task_level_impact | Stopping {pid}: {len(own)} open tasks ({own_hours:.0f} h remaining effort) are orphaned"]
    for r in own:
        chain = f", blocks {r['blocks_n']} follow-up tasks" if r["blocks_n"] else ""
        lines.append(f"- {r['task']} '{r['descr']}' [{r['state']}, {r['pct'] or 0:.0f} %, {r['team'] or 'n/a'}]{chain}")

    groups: dict[str, dict[str, Any]] = {}
    for r in downstream:
        g = groups.setdefault(r["id"], {"name": r["name"], "hops": r["hops"], "dep_types": [t for t in r["dep_types"] or [] if t],
                                        "tasks": [], "hours": 0.0, "seen": set()})
        if r["task"] in g["seen"]:  # defensive de-duplication
            continue
        g["seen"].add(r["task"])
        g["tasks"].append(f"{r['task']} {r['descr']}")
        g["hours"] += r["hours"] or 0
    hidden = any(sid not in allowed for sid in groups)
    groups = {sid: g for sid, g in groups.items() if sid in allowed}
    if groups or hidden:
        total_n = sum(len(g["tasks"]) for g in groups.values())
        scope = " in your scope" if hidden else ""
        if groups:
            lines.append(f"Dependent projects: {total_n} open tasks in {len(groups)} projects{scope} at risk")
        for sid, g in groups.items():
            severity = "AT RISK" if "Hard" in g["dep_types"] or g["hops"] > 1 else "MONITOR (soft)"
            dep = f"{_joined(g['dep_types'])} dependency" if g["dep_types"] else f"indirect ({g['hops']} levels)"
            lines.append(f"- {_label(sid, g['name'])}: {severity}, {dep}, {len(g['tasks'])} tasks / "
                         f"{g['hours']:.0f} h – next up: {'; '.join(g['tasks'][:4])}")
        if hidden:
            lines.append(f"- {RESTRICTED_DEPENDENTS}")
    else:
        lines.append("No dependent projects – no task domino outside the project.")
    return "\n".join(lines)


def successor_deadlines(project_id: str) -> list[dict[str, Any]]:
    """Direct successors of a project with the required-by date of each dependency (deadline check).
    One row per DEPENDS_ON edge (two edges may carry two deadlines). Raises GraphUnavailable."""
    rows = _backend().successor_edges(project_id)
    return sorted(rows, key=lambda r: (r["required_by"] is None, r["required_by"] or "", r["id"]))


def top_portfolio_risks(allowed: set[str], limit: int = 3) -> list[dict[str, Any]]:
    """Top risks for the dashboard alert bar (no scenario context needed; RLS via `allowed`).
    1) Blocked projects: waiting (DEPENDS_ON) for red predecessors or on 'At Risk' dependencies.
    2) Filled up with the systemic risk clusters that carry the most high-impact risks in the visible scope.
    Raises GraphUnavailable if the graph cannot be queried."""
    risks: list[dict[str, Any]] = []
    backend = _backend()
    groups: dict[str, dict[str, Any]] = {}
    for e in backend.blocking_edges():
        row = groups.setdefault(e["id"], {"id": e["id"], "name": e["name"], "status": e["status"],
                                          "budget": e["budget"], "blockers": [], "types": [], "n": 0})
        row["blockers"].append(e["blocker"])
        row["types"].append(e["type"])
        row["n"] += 1
    blocked = sorted(groups.values(), key=lambda r: (-r["n"], r["budget"] is None, -(r["budget"] or 0)))
    for r in blocked:
        if r["id"] not in allowed:
            continue
        hard = sum(1 for t in r["types"] if t == "Hard")
        shown = sorted({b for b in r["blockers"] if b in allowed})
        if len(shown) < len(set(r["blockers"])):  # no id and no count of predecessors outside the scope
            blockers = ", ".join([*shown, "projects outside your scope"])
            detail = f"Waits for {blockers}{' (hard dependency)' if hard else ''} – the PMO coordinates"
        elif hard:
            detail = f"Waits for {', '.join(shown)} ({hard} hard dependenc{'y' if hard == 1 else 'ies'})"
        else:  # soft only: the project can proceed – at risk, not blocked
            detail = f"Waits for {', '.join(shown)} (soft dependency)"
        risks.append({  # "blocked" only behind a hard dependency; soft ones can proceed and are "at risk"
            "kind": "blocked" if hard else "at_risk", "title": f"{r['id']} {'blocked' if hard else 'at risk'}",
            "project": r["id"], "name": r["name"],
            "detail": detail,
            "metric_label": "Budget at risk", "metric_value": _eur(r["budget"]), "severity": "critical" if hard else "warning",
        })
        if len(risks) >= limit:
            return risks
    clusters = backend.risks()
    per_cluster: dict[str, dict[str, Any]] = {}
    for r in clusters:
        if r["id"] not in allowed:
            continue
        c = per_cluster.setdefault(r["cluster"] or "Unclassified", {"high": 0, "projects": set()})
        c["high"] += int(r["impact"] in HIGH_IMPACT)
        c["projects"].add(r["id"])
    # clusters with the same count on the same projects are one card ("Systemic risks: Capacity, Data"), not three
    merged: dict[tuple[int, frozenset], list[str]] = {}
    for cluster, c in per_cluster.items():
        merged.setdefault((c["high"], frozenset(c["projects"])), []).append(cluster)
    ordered = sorted(merged.items(), key=lambda kv: (-kv[0][0], -len(kv[0][1]), kv[1]))
    for (high, projects), names in ordered[: limit - len(risks)]:
        label = ", ".join(sorted(names))
        risks.append({
            "kind": "cluster", "title": f"Systemic risk{'s' if len(names) > 1 else ''}: {label}", "project": None,
            "name": label, "detail": f"{high} high-impact risks across {len(projects)} projects",
            "metric_label": "High-impact risks", "metric_value": str(high), "severity": "critical" if high >= 5 else "warning",
        })
    return risks


# =========================================================================== agent tools
@tool("get_downstream_impact")
def get_downstream_impact(project_id: str) -> str:
    """Project-level domino effect: which projects depend (directly/indirectly) on project_id?"""
    return downstream_impact(project_id)


@tool("get_upstream_bottlenecks")
def get_upstream_bottlenecks(project_id: str) -> str:
    """Which predecessor projects does project_id wait for? Flags red or 'At Risk' dependencies as bottlenecks."""
    return upstream_bottlenecks(project_id)


@tool("find_shared_resources")
def find_shared_resources(project_id_a: str, project_id_b: str) -> str:
    """Teams (assignment groups) shared by two projects incl. utilisation and direct dependencies."""
    return shared_resources(project_id_a, project_id_b)


@tool("detect_cluster_risk")
def detect_cluster_risk(keyword: str) -> str:
    """Systemic concentration risks via risk nodes, grouped by systemic_risk_cluster (Capacity, Supplier,
    Data, Governance, Requirements, Scope). keyword filters cluster/description; '' = all."""
    return cluster_risk(keyword)


@tool("identify_team_bottlenecks")
def identify_team_bottlenecks() -> str:
    """Finds all teams (assignment groups) above the organisation's capacity limit (utilization_pct) and shows on
    which projects they are overloaded in parallel (incl. months with simultaneous overload)."""
    return team_bottlenecks()


@tool("get_task_level_impact")
def get_task_level_impact(project_number: str) -> str:
    """Task domino on project stop: the project's open tasks, their BLOCKS chains and endangered tasks in
    dependent projects (hard = at risk, soft = monitor)."""
    return task_level_impact(project_number)


ANALYST_TOOLS = [
    get_downstream_impact,
    get_upstream_bottlenecks,
    find_shared_resources,
    detect_cluster_risk,
    identify_team_bottlenecks,
    get_task_level_impact,
]
