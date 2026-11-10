"""The in-memory dependency graph (NetworkX): build, process-wide state, privacy and performance."""
from __future__ import annotations

import pandas as pd
import pytest

from app.crew.tools import graph_tools
from app.data_adapters.dataset import PortfolioDataset
from app.graph import portfolio_graph
from app.graph.portfolio_graph import GraphUnavailable, build_graph


class TestBuild:
    def test_counts_from_the_dataset(self, dataset):
        counts = build_graph(dataset).counts()
        assert counts["Project"] == 3 and counts["Task"] == 3 and counts["BLOCKS"] == 1
        assert counts["ALLOCATED_TO"] == 3 and counts["HAS_RISK"] == 1

    def test_no_person_names_in_the_graph(self, dataset):
        graph = build_graph(dataset)
        text = repr((graph.projects, graph.tasks, graph.allocations, graph.risks))
        for name in ("Priya Raman", "Marco Bianchi", "Nora Lindqvist", "Helena Brandt"):
            assert name not in text

    def test_dependencies_to_unknown_projects_are_dropped(self, dataset):
        dataset.dependencies = pd.DataFrame([
            {"dependency_number": "D-1", "successor_project_number": "P-1002", "predecessor_project_number": "P-1001",
             "dependency_type": "Hard", "status": "On Track"},
            {"dependency_number": "D-2", "successor_project_number": "P-1002", "predecessor_project_number": "P-9999",
             "dependency_type": "Hard", "status": "On Track"},
        ])
        graph = build_graph(dataset)
        assert list(graph.dependencies.edges()) == [("P-1002", "P-1001")] and "P-9999" not in graph.dependencies

    def test_parallel_dependencies_keep_both_deadlines(self, dataset):
        dataset.dependencies = pd.DataFrame([
            {"dependency_number": f"D-{i}", "successor_project_number": "P-1002", "predecessor_project_number": "P-1001",
             "dependency_type": "Hard", "status": "On Track", "required_by_date": f"2030-0{i}-01"} for i in (1, 2)])
        portfolio_graph.pin(dataset)
        rows = graph_tools.successor_deadlines("P-1001")
        assert [r["dependency"] for r in rows] == ["D-1", "D-2"]

    def test_three_hundred_projects_build_in_seconds(self, dataset_factory, project_factory):
        """The former graph database needed about 20 s for a 308-project copy of the test portfolio."""
        projects = [project_factory(project_number=f"P-{i:04d}", project_name=f"Project {i}") for i in range(300)]
        ds = dataset_factory(projects)
        tasks = pd.DataFrame([{"task_number": f"T-{i}-{j}", "project_number": f"P-{i:04d}", "state": "Open",
                               "dependency_task_number": f"T-{i}-{j - 1}" if j else "", "assignment_group": "Team A",
                               "planned_effort_hours": 40, "percent_complete": 0} for i in range(300) for j in range(15)])
        plans = pd.DataFrame([{"resource_plan_number": f"R-{i}-{m}", "project_number": f"P-{i:04d}",
                               "month": pd.Timestamp(2026, m, 1), "assignment_group": f"Team {i % 12}",
                               "capacity_hours": 320, "demand_hours": 300, "utilization_pct": 93.8,
                               "requested_fte": 2.0, "allocated_fte": 2.0} for i in range(300) for m in range(1, 13)])
        deps = pd.DataFrame([{"dependency_number": f"D-{i}", "successor_project_number": f"P-{i:04d}",
                              "predecessor_project_number": f"P-{i - 1:04d}", "dependency_type": "Hard",
                              "status": "On Track"} for i in range(1, 300)])
        big = PortfolioDataset.from_tables({"Projects": ds.projects, "Project_Tasks": tasks, "Resource_Plans": plans,
                                            "Stakeholders": pd.DataFrame(), "Risks": pd.DataFrame(),
                                            "Dependencies": deps})
        graph = build_graph(big)
        assert graph.counts()["Project"] == 300 and graph.counts()["Task"] == 4500
        assert graph.build_seconds < 5


class TestProcessGraph:
    def test_pinned_graph_until_reset(self, dataset, monkeypatch):
        pinned = portfolio_graph.pin(dataset)
        assert portfolio_graph.get_graph() is pinned
        assert portfolio_graph.current_metadata()["counts"]["Project"] == 3
        version = portfolio_graph.graph_version()
        portfolio_graph.reset()
        assert portfolio_graph.current_metadata() is None

        class Provider:
            def load_dataset(self):
                return dataset

        import app.data_adapters as adapters

        monkeypatch.setattr(portfolio_graph, "_source", lambda: ("json", None))
        monkeypatch.setattr(adapters, "get_data_provider", lambda *a, **k: Provider())
        built = portfolio_graph.get_graph()  # built on first use from the configured source
        assert built is not pinned and portfolio_graph.graph_version() > version
        assert portfolio_graph.get_graph() is built  # without a source file nothing changes

    def test_unreadable_source_is_graph_unavailable(self, monkeypatch):
        import app.data_adapters as adapters

        class Broken:
            def load_dataset(self):
                raise RuntimeError("file locked")

        monkeypatch.setattr(portfolio_graph, "_source", lambda: ("json", None))
        monkeypatch.setattr(adapters, "get_data_provider", lambda *a, **k: Broken())
        with pytest.raises(GraphUnavailable, match="file locked"):
            portfolio_graph.get_graph()
        ok, message = graph_tools.check_connection()
        assert not ok and "file locked" in message


class TestTopRisks:
    def test_top_risks_query_respects_scope(self, dataset):
        portfolio_graph.pin(dataset)
        risks = graph_tools.top_portfolio_risks({"P-1001", "P-1002", "P-1003"})
        assert risks and risks[0]["kind"] == "cluster" and "Supplier" in risks[0]["title"]
        assert graph_tools.top_portfolio_risks({"P-1002"}) == []  # the risk belongs to P-1001 only

    def test_clusters_on_the_same_projects_with_the_same_count_are_one_card(self, dataset):
        import pandas as pd

        extra = dataset.risks.iloc[[0]].assign(risk_number="RK-9", systemic_risk_cluster="Capacity")
        dataset.risks = pd.concat([dataset.risks, extra], ignore_index=True)
        portfolio_graph.pin(dataset)
        risks = graph_tools.top_portfolio_risks({"P-1001"})
        assert [r["title"] for r in risks] == ["Systemic risks: Capacity, Supplier"]

    def test_top_risk_says_blocked_only_behind_a_hard_dependency(self, dataset):
        """A successor of a red project behind a soft dependency is 'at risk', not 'blocked'."""
        dataset.projects.loc[dataset.projects["project_number"] == "P-1001", "status"] = "Red"
        dataset.dependencies = pd.DataFrame([
            {"dependency_number": "D-1", "successor_project_number": "P-1002", "predecessor_project_number": "P-1001",
             "dependency_type": "Hard", "status": "On Track", "required_by_date": pd.Timestamp("2030-08-15")},
            {"dependency_number": "D-2", "successor_project_number": "P-1003", "predecessor_project_number": "P-1001",
             "dependency_type": "Soft", "status": "On Track", "required_by_date": pd.Timestamp("2030-08-15")},
        ])
        portfolio_graph.pin(dataset)
        titles = {r["project"]: r["title"] for r in graph_tools.top_portfolio_risks({"P-1001", "P-1002", "P-1003"})
                  if r.get("project")}
        assert titles == {"P-1002": "P-1002 blocked", "P-1003": "P-1003 at risk"}

    def test_steward_recommends_a_clean_up_only_when_projects_are_blocked(self):
        """The clean-up recommendation appears only when projects are blocked."""
        from app.crew.dq_interpretation import rules_interpretation

        clean = {"index": 100, "projects": 3, "completeness": 100, "blocked": [], "missing_fields": {}, "rules": {}}
        assert "blocked projects" not in rules_interpretation(clean)
        assert "No clean-up needed" in rules_interpretation(clean)
        blocked = {**clean, "index": 66, "blocked": ["P-1"], "missing_fields": {"business_case": 1}}
        assert "clean up the master data of the blocked projects" in rules_interpretation(blocked)
