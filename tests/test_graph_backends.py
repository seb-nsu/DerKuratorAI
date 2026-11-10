"""The two graph backends: the graph tools answer exactly the same on the in-memory graph (NetworkX) and on its
projection in Neo4j, and a Neo4j that fails never changes an answer – the in-memory graph takes over.

The Neo4j tests (marker `neo4j`) read NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD and NEO4J_DATABASE from the environment
and are skipped without NEO4J_URI. CI runs them against a `neo4j:5-community` service container (job `neo4j`):

    NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_PASSWORD=... pytest -m neo4j
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.core.config import get_settings
from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset, mask_dataset_pii
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.data_adapters import JsonPortfolioAdapter
from app.data_adapters.dataset import PortfolioDataset
from app.graph import backends, neo4j_backend, portfolio_graph

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)
NEO4J_ENV = {name: os.environ.get(name, "") for name in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
                                                         "NEO4J_DATABASE")}
UNREACHABLE = "bolt://127.0.0.1:1"  # nothing listens there: the connection is refused at once


@pytest.fixture
def use_backend(monkeypatch):
    """use_backend("networkx" | "neo4j" | "unreachable"): switches GRAPH_BACKEND (and the Neo4j settings)."""
    def use(name: str, **env: str) -> None:
        if name == "neo4j" and not NEO4J_ENV["NEO4J_URI"]:
            pytest.skip("NEO4J_URI is not set – no Neo4j to test against")
        for key, value in NEO4J_ENV.items():
            if value:
                monkeypatch.setenv(key, value)
        if name == "unreachable":
            monkeypatch.setenv("NEO4J_URI", UNREACHABLE)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("GRAPH_BACKEND", "networkx" if name == "networkx" else "neo4j")
        get_settings.cache_clear()
        neo4j_backend.close()

    yield use
    neo4j_backend.close()
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    get_settings.cache_clear()


@pytest.fixture(scope="module", autouse=True)
def _remove_projection():
    """No DerKurator projection stays in the test database after this module."""
    yield
    if NEO4J_ENV["NEO4J_URI"]:
        import neo4j

        auth = (NEO4J_ENV["NEO4J_USER"] or "neo4j", NEO4J_ENV["NEO4J_PASSWORD"])
        with neo4j.GraphDatabase.driver(NEO4J_ENV["NEO4J_URI"], auth=auth) as driver:
            driver.execute_query("MATCH (n) WHERE any(label IN labels(n) WHERE label STARTS WITH $prefix) "
                                 "DETACH DELETE n", prefix=neo4j_backend.PREFIX,
                                 database_=NEO4J_ENV["NEO4J_DATABASE"] or "neo4j")


# =========================================================================== test portfolios
def tricky_dataset(project_factory) -> PortfolioDataset:
    """Everything that could make two implementations drift apart: a dependency cycle, parallel dependencies with
    two deadlines, a self-dependency, a dependency on an unknown project, a cyclic task chain, closed tasks and tasks
    without a state, a duplicated task number, umlauts, missing values and two teams overloaded in the same month."""
    names = {"P-A": "Ärger Analytics", "P-B": "Beta Billing", "P-C": "Cäsar Core", "P-D": "Delta Data",
             "P-E": "Echo ERP", "P-F": "Foxtrot Field"}
    status = {"P-A": "Red", "P-B": "Yellow", "P-C": "Green", "P-D": "Red", "P-E": "Green", "P-F": "Green"}
    managers = {"P-A": "Priya Raman", "P-B": "Rowan Ellis", "P-C": "Priya Raman", "P-D": "Rowan Ellis",
                "P-E": "Priya Raman", "P-F": "Ingrid Solberg"}
    projects = [project_factory(project_number=pid, project_name=name, status=status[pid],
                                project_manager=managers[pid], budget_cost_eur=100000 * (i + 1))
                for i, (pid, name) in enumerate(names.items())]
    projects[5]["budget_cost_eur"] = None

    def dep(n, succ, pred, kind="Hard", state="On Track", required_by=None):
        return {"dependency_number": f"D-{n}", "successor_project_number": succ, "predecessor_project_number": pred,
                "dependency_type": kind, "status": state, "description": f"dependency {n}",
                "required_by_date": required_by}

    dependencies = pd.DataFrame([
        dep(1, "P-B", "P-A", required_by="2030-03-01"), dep(2, "P-B", "P-A", "Soft", "At Risk", "2030-01-15"),
        dep(3, "P-C", "P-B"), dep(4, "P-A", "P-C", "Soft"),  # cycle A <- B <- C <- A
        dep(5, "P-D", "P-D"), dep(6, "P-E", "P-D", "Hard", "At Risk", "2030-02-01"), dep(7, "P-F", "P-E", "Soft"),
        dep(8, "P-F", "P-X"),  # unknown predecessor: dropped
        dep(9, "P-C", "P-A", None, None, None),  # no type, no status
    ])

    def task(n, pid, state="Open", after="", team="Team Orion", start=None, pct=0):
        return {"task_number": n, "project_number": pid, "short_description": f"Task {n} – Übergabe",
                "dependency_task_number": after, "assignment_group": team, "state": state, "percent_complete": pct,
                "planned_effort_hours": 40, "planned_start_date": start, "planned_end_date": None}

    tasks = pd.DataFrame([
        task("T-1", "P-A", start="2026-02-01"), task("T-2", "P-A", after="T-1", start="2026-01-01"),
        task("T-3", "P-A", after="T-2"), task("T-4", "P-A", "Closed Complete", after="T-3", pct=100),
        task("T-5", "P-A", after="T-4", team=None), task("T-1b", "P-A", after="T-5"),
        task("T-6", "P-B", start="2026-03-01", team="Team Harbor"), task("T-7", "P-B", None, after="T-6"),
        task("T-8", "P-C", after="T-8"),  # blocks itself
        task("T-9", "P-C", after="T-10"), task("T-10", "P-C", after="T-9", pct=50),  # two-task cycle
        task("T-11", "P-E", start="2026-04-01", team="Team Harbor"), task("T-12", "P-X", after="T-11"),
        task("T-6", "P-B", after="T-11"),  # duplicated number: the first row wins, its edge stays
    ])

    def plan(n, pid, team, month, util):
        return {"resource_plan_number": f"R-{n}", "project_number": pid, "month": pd.Timestamp(month),
                "assignment_group": team, "capacity_hours": 320, "demand_hours": 320 * util / 100,
                "utilization_pct": util, "requested_fte": 2.0, "allocated_fte": 1.5}

    plans = pd.DataFrame([
        plan(1, "P-A", "Team Orion", "2026-01-01", 120), plan(2, "P-A", "Team Orion", "2026-02-01", 95),
        plan(3, "P-B", "Team Orion", "2026-01-01", 130), plan(4, "P-B", "Team Harbor", "2026-01-01", 70),
        plan(5, "P-C", "Team Orion", "2026-01-01", 105), plan(6, "P-E", "Team Harbor", "2026-01-01", 110),
        plan(7, "P-E", "Team Cirrus", "2026-02-01", float("nan")), plan(8, "P-F", "Team Harbor", "2026-01-01", 140),
        plan(9, "P-X", "Team Orion", "2026-01-01", 150),  # unknown project: no allocation
    ])

    def risk(n, pid, cluster, impact, text):
        return {"risk_number": f"RK-{n}", "project_number": pid, "short_description": text, "probability": "High",
                "impact": impact, "risk_status": "Open", "systemic_risk_cluster": cluster}

    risks = pd.DataFrame([
        risk(1, "P-A", "Supplier", "High", "Lieferant Ärger"), risk(2, "P-B", "Supplier", "Critical", "Supplier late"),
        risk(3, "P-C", "Capacity", "High", "Team overloaded"), risk(4, "P-D", None, "Low", "Unclassified risk"),
        risk(5, "P-A", "Capacity", "Very High", "Übergabe fehlt"), risk(6, "P-E", "Supplier", "High", None),
        risk(7, "P-F", "Data", "High", "Data quality"), risk(8, "P-X", "Data", "High", "Unknown project"),
    ])
    return PortfolioDataset.from_tables({"Projects": pd.DataFrame(projects), "Project_Tasks": tasks,
                                         "Resource_Plans": plans, "Stakeholders": pd.DataFrame(), "Risks": risks,
                                         "Dependencies": dependencies})


def demo_dataset() -> PortfolioDataset:
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return JsonPortfolioAdapter(SOURCE).load_dataset()


def answers(dataset: PortfolioDataset, role: str, user: str) -> dict[str, object]:
    """Every graph tool on every project (and every pair of the first projects) in the scope of `user`, with the
    facts the verdict engine reads."""
    portfolio_graph.pin(dataset, source="parity")
    view = mask_dataset_pii(apply_rls_to_dataset(dataset, role, user))
    ctx.set_portfolio_context(view, {}, as_of=AS_OF)
    pids = list(dict.fromkeys(dataset.projects["project_number"].astype(str)))
    out: dict[str, object] = {}
    for pid in pids:
        out[f"downstream {pid}"] = graph_tools.downstream_impact(pid)
        out[f"upstream {pid}"] = graph_tools.upstream_bottlenecks(pid)
        out[f"tasks {pid}"] = graph_tools.task_level_impact(pid)
        out[f"deadlines {pid}"] = graph_tools.successor_deadlines(pid)
    for a in pids[:6]:
        for b in pids[:6]:
            out[f"shared {a} {b}"] = graph_tools.shared_resources(a, b)
    for keyword in ("", "supp", "ÄRGER", "capacity", "data", "nothing-like-this"):
        out[f"cluster {keyword}"] = graph_tools.cluster_risk(keyword)
    out["teams"] = graph_tools.team_bottlenecks()
    allowed = ctx.allowed_project_ids()
    out["top risks"] = graph_tools.top_portfolio_risks(allowed, limit=10)
    out["top risks 3"] = graph_tools.top_portfolio_risks(allowed)
    out["facts"] = ctx.get_facts()
    return out


CASES = {"demo portfolio, PMO": ("demo", ROLE_PMO, "PMO"),
         "demo portfolio, project manager": ("demo", ROLE_PM, "Priya Raman"),
         "tricky portfolio, PMO": ("tricky", ROLE_PMO, "PMO"),
         "tricky portfolio, project manager": ("tricky", ROLE_PM, "Priya Raman")}


def _dataset(kind: str, project_factory) -> PortfolioDataset:
    return demo_dataset() if kind == "demo" else tricky_dataset(project_factory)


# =========================================================================== parity
@pytest.mark.neo4j
@pytest.mark.parametrize("case", CASES)
def test_neo4j_answers_exactly_like_networkx(case, use_backend, project_factory):
    kind, role, user = CASES[case]
    use_backend("networkx")
    expected = answers(_dataset(kind, project_factory), role, user)
    use_backend("neo4j")
    actual = answers(_dataset(kind, project_factory), role, user)
    assert graph_tools._backend().name == "neo4j", backends.backend_status()["warning"]
    assert neo4j_backend._state["error"] is None
    assert actual == expected


@pytest.mark.neo4j
def test_a_larger_portfolio_answers_alike(use_backend, dataset_factory, project_factory):
    """300 projects in a dependency chain with 15 tasks each: deep chains hit the depth limits on both backends."""
    projects = [project_factory(project_number=f"P-{i:04d}", project_name=f"Project {i}",
                                status="Red" if i % 7 == 0 else "Green") for i in range(300)]
    tasks = pd.DataFrame([{"task_number": f"T-{i}-{j}", "project_number": f"P-{i:04d}", "state": "Open",
                           "dependency_task_number": f"T-{i}-{j - 1}" if j else "", "assignment_group": "Team A",
                           "planned_effort_hours": 40, "percent_complete": 0} for i in range(300) for j in range(20)])
    deps = pd.DataFrame([{"dependency_number": f"D-{i}", "successor_project_number": f"P-{i:04d}",
                          "predecessor_project_number": f"P-{i - 1:04d}", "dependency_type": "Hard",
                          "status": "At Risk" if i % 11 == 0 else "On Track"} for i in range(1, 300)])
    big = PortfolioDataset.from_tables({"Projects": dataset_factory(projects).projects, "Project_Tasks": tasks,
                                        "Resource_Plans": pd.DataFrame(), "Stakeholders": pd.DataFrame(),
                                        "Risks": pd.DataFrame(), "Dependencies": deps})
    portfolio_graph.pin(big)
    ctx.set_portfolio_context(big, {}, as_of=AS_OF)
    probe = ["P-0000", "P-0150", "P-0299"]

    def run() -> list:
        return [f(pid) for pid in probe for f in (graph_tools.downstream_impact, graph_tools.upstream_bottlenecks,
                                                  graph_tools.task_level_impact, graph_tools.successor_deadlines)]

    use_backend("networkx")
    expected = run()
    use_backend("neo4j")
    portfolio_graph.pin(big)
    assert run() == expected and graph_tools._backend().name == "neo4j"
    assert "5 projects affected" in expected[4]  # five levels deep, not the whole chain


# =========================================================================== the same cases on both backends
BACKENDS = ["networkx", pytest.param("neo4j", marks=pytest.mark.neo4j)]


@pytest.mark.parametrize("backend", BACKENDS)
class TestBothBackends:
    @pytest.fixture(autouse=True)
    def _tricky(self, backend, use_backend, project_factory):
        use_backend(backend)
        portfolio_graph.pin(tricky_dataset(project_factory))
        ctx.set_portfolio_context(tricky_dataset(project_factory), {}, as_of=AS_OF)
        yield
        assert graph_tools._backend().name == backend

    def test_a_cycle_is_never_its_own_dependent(self, backend):
        text = graph_tools.downstream_impact("P-A")
        assert "2 projects affected" in text and "P-A (" not in text.split("\n", 1)[1]
        assert "direct, Hard/Soft dependency (At Risk/On Track)" in text

    def test_parallel_dependencies_keep_both_deadlines(self, backend):
        rows = graph_tools.successor_deadlines("P-A")
        assert [(r["id"], r["dependency"]) for r in rows] == [("P-B", "D-2"), ("P-B", "D-1"), ("P-C", "D-9")]
        assert graph_tools.successor_deadlines("P-D") == [{"id": "P-E", "name": "Echo ERP", "type": "Hard",
                                                           "status": "At Risk", "required_by": "2030-02-01",
                                                           "dependency": "D-6"}]

    def test_a_task_cycle_counts_the_task_itself(self, backend):
        text = graph_tools.task_level_impact("P-C")
        assert "T-8 'Task T-8 – Übergabe' [Open, 0 %, Team Orion], blocks 1 follow-up tasks" in text
        assert "T-9 'Task T-9 – Übergabe' [Open, 0 %, Team Orion], blocks 2 follow-up tasks" in text

    def test_a_project_shares_its_teams_with_itself(self, backend):
        assert "P-A ↔ P-A: 1 shared teams" in graph_tools.shared_resources("P-A", "P-A")
        assert "No shared teams" in graph_tools.shared_resources("P-A", "P-E")

    def test_keyword_with_umlaut(self, backend):
        text = graph_tools.cluster_risk("ärger")
        assert "Cluster 'Supplier'" in text and "Lieferant Ärger" in text and "Capacity" not in text

    def test_parallel_overload(self, backend):
        text = graph_tools.team_bottlenecks()
        assert "Team Orion: peak 130.0 % across 3 projects" in text and "max. 3 projects at once (2026-01-01)" in text


# =========================================================================== Neo4j specifics
@pytest.mark.neo4j
class TestNeo4jProjection:
    @pytest.fixture(autouse=True)
    def _neo4j(self, use_backend, project_factory):
        use_backend("neo4j")
        self.dataset = tricky_dataset(project_factory)
        ctx.set_portfolio_context(self.dataset, {}, as_of=AS_OF)

    def test_a_rebuild_replaces_only_derkurator_data(self):
        neo4j_backend._run("CREATE (:OtherTeam_Project {id: 'P-A', gk: 'theirs'})-[:DEPENDS_ON]->(:Project {id: 'x'})",
                           write=True)
        try:
            first = portfolio_graph.pin(self.dataset)
            first_key = neo4j_backend._state["key"]
            self.dataset.projects.loc[self.dataset.projects["project_number"] == "P-B", "status"] = "Red"
            portfolio_graph.pin(self.dataset)
            assert neo4j_backend._state["key"] != first_key and first is not portfolio_graph.get_graph()
            keys = neo4j_backend._run(f"MATCH (n:{neo4j_backend.PROJECT}) RETURN DISTINCT n.gk AS gk")
            assert [r["gk"] for r in keys] == [neo4j_backend._state["key"]]  # the old version is gone
            other = neo4j_backend._run("MATCH (a:OtherTeam_Project)-[:DEPENDS_ON]->(b:Project) RETURN count(*) AS n")
            assert other[0]["n"] == 1  # another team's data is never touched
        finally:
            neo4j_backend._run("MATCH (n) WHERE n:OtherTeam_Project OR n:Project DETACH DELETE n", write=True)

    def test_an_unchanged_graph_is_not_written_again(self):
        portfolio_graph.pin(self.dataset)
        assert neo4j_backend._state["reused"] is False
        portfolio_graph.pin(self.dataset)  # e.g. a second process with the same data
        assert neo4j_backend._state["reused"] is True and graph_tools._backend().name == "neo4j"

    def test_status_shows_counts_and_the_last_sync(self):
        graph = portfolio_graph.pin(self.dataset)
        status = backends.backend_status()
        rows = {r["Setting"]: r["Value"] for r in status["rows"]}
        assert status["active"] == "neo4j" and status["warning"] is None
        nodes = sum(v for k, v in graph.counts().items() if not k.isupper())
        assert rows["Nodes / relationships"].startswith(f"{nodes} / ") and "same as the in-memory" in \
            rows["Nodes / relationships"]
        assert rows["Reachable"].startswith("yes") and rows["Last sync"].endswith(("written", "already in Neo4j"))
        assert NEO4J_ENV["NEO4J_PASSWORD"] not in repr(status)

    def test_a_removed_projection_falls_back_with_the_same_answer(self, caplog):
        portfolio_graph.pin(self.dataset)
        expected = graph_tools.downstream_impact("P-A")
        neo4j_backend._run(f"MATCH (m:{neo4j_backend.META}) DELETE m", write=True)  # e.g. replaced by another process
        with caplog.at_level(logging.WARNING):
            assert graph_tools.downstream_impact("P-A") == expected
        assert "in-memory graph (NetworkX) answers instead" in caplog.text
        assert graph_tools._backend().name == "networkx"  # until the next attempt
        assert "not used" in backends.backend_status()["warning"]

    def test_wrong_password_falls_back_and_is_never_shown(self, use_backend, caplog):
        use_backend("neo4j", NEO4J_PASSWORD="wrong-password-4711")
        with caplog.at_level(logging.WARNING):
            portfolio_graph.pin(self.dataset)
            text = graph_tools.downstream_impact("P-A")
        assert "2 projects affected" in text and graph_tools._backend().name == "networkx"
        status = backends.backend_status()
        assert status["active"] == "networkx" and "unauthorized" in status["warning"]
        assert "wrong-password-4711" not in caplog.text + repr(status)


# =========================================================================== without a Neo4j server
class TestFallbackWithoutServer:
    def test_unreachable_neo4j_answers_from_memory(self, use_backend, project_factory, caplog):
        use_backend("networkx")
        expected = answers(tricky_dataset(project_factory), ROLE_PMO, "PMO")
        use_backend("unreachable", NEO4J_PASSWORD="s3cret-not-shown")
        with caplog.at_level(logging.WARNING):
            assert answers(tricky_dataset(project_factory), ROLE_PMO, "PMO") == expected
        assert caplog.text.count("in-memory graph (NetworkX) answers instead") == 1  # once per outage
        status = backends.backend_status()
        assert status["configured"] == "neo4j" and status["active"] == "networkx"
        assert "not used" in status["warning"] and "s3cret-not-shown" not in caplog.text + repr(status)
        rows = {r["Setting"]: r["Value"] for r in status["rows"]}
        assert rows["Reachable"].startswith("no") and rows["Server"].startswith(UNREACHABLE)
        ok, message = graph_tools.check_connection()
        assert ok and "Neo4j configured but not available" in message

    def test_neo4j_without_uri(self, use_backend, monkeypatch, dataset):
        use_backend("unreachable")
        monkeypatch.setenv("NEO4J_URI", "")
        get_settings.cache_clear()
        portfolio_graph.pin(dataset)
        assert graph_tools._backend().name == "networkx"
        assert "NEO4J_URI is not set" in backends.backend_status()["warning"]

    def test_a_failed_sync_is_retried_only_after_a_pause(self, use_backend, dataset, monkeypatch):
        use_backend("unreachable")
        graph = portfolio_graph.pin(dataset)
        calls = []
        monkeypatch.setattr(neo4j_backend, "sync", lambda g: calls.append(g) or False)
        neo4j_backend.backend_for(graph)
        assert calls == []  # failed a moment ago: the in-memory graph answers without a new attempt
        monkeypatch.setattr(neo4j_backend, "RETRY_SECONDS", 0.0)
        neo4j_backend.backend_for(graph)
        assert calls == [graph]

    def test_networkx_is_the_default(self, monkeypatch):
        monkeypatch.delenv("GRAPH_BACKEND", raising=False)
        get_settings.cache_clear()
        assert get_settings().graph_backend == "networkx"
        monkeypatch.setenv("GRAPH_BACKEND", "orientdb")
        get_settings.cache_clear()
        assert get_settings().graph_backend == "networkx"  # an unknown value keeps the default
        status = backends.backend_status()
        assert status["active"] == "networkx" and status["warning"] is None and "GRAPH_BACKEND" in status["note"]
        get_settings.cache_clear()

    def test_password_is_not_in_the_settings_repr(self, monkeypatch):
        monkeypatch.setenv("NEO4J_PASSWORD", "pw-never-printed")
        get_settings.cache_clear()
        assert get_settings().neo4j_password == "pw-never-printed" and "pw-never-printed" not in repr(get_settings())
        get_settings.cache_clear()

    def test_server_label_drops_user_info(self):
        assert neo4j_backend.server_label("neo4j+s://user:pw@graph.example.com:7687/x?y=1") == \
            "neo4j+s://graph.example.com:7687"
        assert neo4j_backend.server_label("") == "not set"

    def test_the_version_key_follows_the_content(self, dataset):
        graph = portfolio_graph.build_graph(dataset)
        payload = neo4j_backend._payload(graph)
        assert neo4j_backend.graph_key(payload) == neo4j_backend.graph_key(neo4j_backend._payload(
            portfolio_graph.build_graph(dataset)))
        counts = graph.counts()
        assert (len(payload["projects"]), len(payload["tasks"]), len(payload["teams"]), len(payload["risks"])) == \
            (counts["Project"], counts["Task"], counts["Team"], counts["Risk"])
        assert (len(payload["belongs"]), len(payload["blocks"]), len(payload["handled"]), len(payload["depends"]),
                len(payload["allocations"])) == (counts["BELONGS_TO"], counts["BLOCKS"], counts["HANDLED_BY"],
                                                 counts["DEPENDS_ON"], counts["ALLOCATED_TO"])
        dataset.projects.loc[0, "status"] = "Red"
        assert neo4j_backend.graph_key(neo4j_backend._payload(portfolio_graph.build_graph(dataset))) != \
            neo4j_backend.graph_key(payload)
