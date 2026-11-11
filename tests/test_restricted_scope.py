"""A Project Manager learns nothing about projects outside the RLS scope: graph tools, deadline check, verdict and
dashboard risks show neither id, name, status, budget, load nor the number of hidden projects – only the neutral
note that the PMO coordinates. The PMO keeps seeing every project and count."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset, mask_dataset_pii
from app.crew.tools import context as ctx
from app.crew.tools import graph_tools
from app.crew.tools import simulator_tools as sim
from app.crew.verdict import verdict_from_context
from app.data_adapters import JsonPortfolioAdapter
from app.graph.portfolio_graph import build_graph

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
AS_OF = date(2026, 10, 1)
ERP = "PRJ-DEMO-1002"  # Priya Raman; its dependents Tariff Invoicing (1007) and Vendor Exchange (1009) belong to other PMs


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    dataset = JsonPortfolioAdapter(SOURCE).load_dataset()
    return dataset, build_graph(dataset)


def _context(demo, monkeypatch, role, user):
    dataset, graph = demo
    monkeypatch.setattr(graph_tools, "get_graph", lambda: graph)
    view = apply_rls_to_dataset(dataset, role, user)
    ctx.set_portfolio_context(mask_dataset_pii(view), {}, as_of=AS_OF)
    foreign = set(dataset.projects["project_number"].astype(str)) - set(view.projects["project_number"].astype(str))
    return foreign


@pytest.fixture
def pm(demo, monkeypatch):
    return _context(demo, monkeypatch, ROLE_PM, "Priya Raman")


def _leaks(text: str, foreign: set[str]) -> list[str]:
    hits = [pid for pid in foreign if pid in text or pid.rsplit("-", 1)[-1] in text]
    if "RESTRICTED PROJECT" in text or "[restricted]" in text:
        hits.append("restricted label")
    return hits


class TestProjectManagerSeesNoForeignProject:
    def test_graph_tools(self, pm):
        outputs = [graph_tools.downstream_impact(ERP), graph_tools.upstream_bottlenecks("PRJ-DEMO-1008"),
                   graph_tools.cluster_risk(""), graph_tools.team_bottlenecks(), graph_tools.task_level_impact(ERP)]
        for text in outputs:
            assert not _leaks(text, pm), text
        downstream = outputs[0]
        assert ctx.RESTRICTED_DEPENDENTS in downstream
        assert "domino effect outside your scope" in downstream
        assert not re.search(r"Budget volume", downstream)  # no sum over hidden budgets
        assert ctx.RESTRICTED_DEPENDENTS in outputs[4]

    def test_verdict_and_deadline_check_name_no_id_and_no_count(self, pm):
        sim.timeline_shift(ERP, 1)  # end 2027-03-31 -> 2027-04-30 misses Billing's required-by date 2027-04-15
        graph_tools.downstream_impact(ERP)
        facts = [f for f in ctx.get_facts() if f["type"] in ("domino", "deadline_breach")]
        assert facts and all(f.get("restricted") for f in facts)
        verdict = verdict_from_context()
        assert verdict.decision == "CONDITIONAL GO"
        text = verdict.to_markdown()
        assert not _leaks(text, pm), text
        assert ctx.RESTRICTED_DEPENDENTS in verdict.conditions
        assert not re.search(r"\d+ dependent project", text)
        for result in ctx.get_tool_results():
            assert not _leaks(result, pm), result

    def test_dashboard_risks(self, pm):
        allowed = ctx.allowed_project_ids()
        for risk in graph_tools.top_portfolio_risks(allowed, limit=10):
            assert not _leaks(" ".join(str(v) for v in risk.values()), pm), risk


class TestPmoSeesEverything:
    def test_ids_and_count(self, demo, monkeypatch):
        _context(demo, monkeypatch, ROLE_PMO, "PMO-ADMIN")
        text = graph_tools.downstream_impact(ERP)
        assert "PRJ-DEMO-1007" in text and "PRJ-DEMO-1009" in text
        assert ctx.RESTRICTED_DEPENDENTS not in text
        sim.timeline_shift(ERP, 1)
        graph_tools.downstream_impact(ERP)
        verdict = verdict_from_context()
        assert any("Align the 2 dependent project(s) of PRJ-DEMO-1002" in c for c in verdict.conditions)
        assert any("PRJ-DEMO-1007" in c for c in verdict.conditions)


class TestProjectManagerReportAndExport:
    @pytest.mark.parametrize("request_text", ["Delay General Ledger Consolidation by 1 month",
                                              "Which projects depend on General Ledger Consolidation?",
                                              "Stop General Ledger Consolidation"])
    def test_paper_verdict_graph_and_export(self, demo, monkeypatch, request_text):
        from app.crew import pipeline
        from app.views import charts, exports

        foreign = _context(demo, monkeypatch, ROLE_PM, "Priya Raman")
        view = apply_rls_to_dataset(demo[0], ROLE_PM, "Priya Raman")
        result = pipeline.run_steering_pipeline(request_text, view, ROLE_PM, "Priya Raman")
        meta = {"request": request_text, "plan": result.intent_plan.model_dump(), "verdict": result.verdict.to_dict(),
                "proposed_changes": result.proposed_changes}
        edges = exports.dependency_edges(view, exports.focus_projects(meta, set(view.project_numbers),
                                                                      result.decision_paper))
        assert exports.RESTRICTED_NODE in set(edges["successor"]) | set(edges["predecessor"])
        graph = charts.dependency_graph_figure(view, edges, [ERP]).to_json()
        export = exports.build_report_markdown([{"role": "user", "content": request_text},
                                                {"role": "assistant", "content": result.decision_paper, "meta": meta}],
                                               view, ROLE_PM, "Priya Raman", "TEST")
        for text in (result.decision_paper, result.verdict.to_markdown(), graph, export):
            assert not _leaks(text, foreign)
            assert not re.search(r"(?<!: )\d+ (dependent|restricted) project", text)  # the rule text names the threshold
