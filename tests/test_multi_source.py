"""Portfolio from several sources (E1): one canonical table from several sources with a key prefix per source, the
source of every row, an error instead of an overwrite on a shared key, duplicates and contradictions as findings,
writes into the mapping's store, and the demo mapping over the ServiceNow export and an Excel list."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.core import proposals
from app.core.security import ROLE_PM, ROLE_PMO
from app.crew.tools.data_steward_tools import run_all_checks
from app.data_adapters import DataSourceError, MappedPortfolioAdapter
from app.data_adapters.mapping import SOURCE_COLUMN, load_mapping

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "mappings" / "multi_source_demo.yaml"
PROJECT = {"project_name": "A", "project_manager": "PM", "project_sponsor": "Sponsor", "status": "Green",
           "state": "Work in Progress", "execution_type": "Agile", "percent_complete": 10, "planned_end_date":
           "2027-01-01", "budget_cost_eur": 100.0, "actual_cost_eur": 10.0, "estimate_at_completion_eur": 100.0,
           "total_planned_cost_eur": 100.0, "planned_capital_eur": 50.0, "planned_operating_eur": 50.0}


def _source(path: Path, projects: list[dict], risks: list[dict] | None = None) -> Path:
    path.write_text(json.dumps({"Projects": projects, "Risks": risks or []}), encoding="utf-8")
    return path


def _mapping(tmp_path: Path, a_prefix: str = "", b_prefix: str = "B-", duplicates: str = "") -> Path:
    text = f"""
name: Two tools
sources:
  a: {{type: json, path: a.json, label: Tool A{', key_prefix: ' + a_prefix if a_prefix else ''}}}
  b: {{type: json, path: b.json, label: Tool B{', key_prefix: ' + b_prefix if b_prefix else ''}}}
tables:
  Projects: [{{source: a}}, {{source: b}}]
  Risks: [{{source: a}}, {{source: b}}]
{duplicates}
"""
    path = tmp_path / "two.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def two(tmp_path):
    _source(tmp_path / "a.json", [{**PROJECT, "project_number": "P1", "project_name": "Shared"},
                                  {**PROJECT, "project_number": "P2"}],
            [{"risk_number": "R1", "project_number": "P1", "short_description": "r", "impact": "High"}])
    _source(tmp_path / "b.json", [{**PROJECT, "project_number": "P1", "project_name": "Other"},
                                  {**PROJECT, "project_number": "7", "project_name": "shared ",
                                   "budget_cost_eur": 150.0, "planned_end_date": "2027-03-01"}],
            [{"risk_number": "R1", "project_number": "P1", "short_description": "r", "impact": "Low"}])
    return tmp_path


def test_rows_of_both_sources_carry_their_source_and_prefixed_keys(two):
    ds = MappedPortfolioAdapter(_mapping(two)).load_dataset()
    projects = ds.projects.set_index("project_number")
    assert sorted(projects.index) == ["B-7", "B-P1", "P1", "P2"]
    assert projects.at["B-P1", SOURCE_COLUMN] == "Tool B" and projects.at["P1", SOURCE_COLUMN] == "Tool A"
    risks = ds.risks.set_index("risk_number")
    assert risks.at["B-R1", "project_number"] == "B-P1"  # references inside a source keep pointing at its rows
    report = ds.metadata["import_report"]["tables"]["Projects"]
    assert report["by_source"] == {"Tool A": 2, "Tool B": 2} and report["loaded"] == 4


def test_a_key_in_two_sources_without_prefix_is_an_error(two):
    with pytest.raises(DataSourceError, match="key_prefix"):
        MappedPortfolioAdapter(_mapping(two, b_prefix="")).load_dataset()


def test_duplicates_and_contradictions_are_findings_and_both_rows_stay(two):
    mapping = _mapping(two, duplicates="duplicates: {Projects: [project_name, project_sponsor]}")
    ds = MappedPortfolioAdapter(mapping).load_dataset()
    findings = ds.metadata["import_report"]["findings"]
    same = [f for f in findings if f["message"].startswith("Same project in two sources")]
    assert len(same) == 1 and "P1 (Tool A) and B-7 (Tool B)" in same[0]["message"] and same[0]["project"] == "P1"
    disagree = {f["column"] for f in findings if "disagree" in f["message"]}
    assert disagree == {"budget_cost_eur", "planned_end_date"}
    assert {"P1", "B-7"} <= set(ds.projects["project_number"])
    steward = run_all_checks(ds)
    assert any(i.project_id == "P1" and i.rule == "Import" and "two sources" in i.message for i in steward.issues)


def test_writes_of_a_federated_mapping_go_into_the_store(two):
    adapter = MappedPortfolioAdapter(_mapping(two))
    assert adapter.write_mode == "store"
    before = (two / "b.json").read_bytes()
    entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "test", "", None, [
        {"project_id": "B-7", "table": "Projects", "field": "budget_cost_eur", "old": 150.0, "new": 120.0,
         "source_tool": "test"}])
    adapter.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"])
    assert (two / "b.json").read_bytes() == before
    store = json.loads((two / "two.store.json").read_text(encoding="utf-8"))
    assert store["updates"]["Projects"]["B-7"] == {"budget_cost_eur": 120.0}
    assert adapter.load_dataset().projects.set_index("project_number").at["B-7", "budget_cost_eur"] == 120.0


def test_a_table_naming_one_source_twice_is_refused(tmp_path):
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    path = tmp_path / "m.yaml"
    path.write_text("sources: {a: {type: json, path: a.json}}\ntables:\n  Projects: [{source: a}, {source: a}]\n",
                    encoding="utf-8")
    with pytest.raises(DataSourceError, match="same source twice"):
        load_mapping(path)


def test_single_source_mappings_get_no_source_column(two):
    path = two / "one.yaml"
    path.write_text("sources: {a: {type: json, path: a.json}}\ntables: {Projects: {}, Risks: {}}\n", encoding="utf-8")
    projects = MappedPortfolioAdapter(path).load_dataset().projects
    assert SOURCE_COLUMN not in projects.columns and sorted(projects["project_number"]) == ["P1", "P2"]


class TestDemo:
    @pytest.fixture(scope="class")
    def ds(self):
        return MappedPortfolioAdapter(DEMO).load_dataset()

    def test_the_demo_combines_servicenow_and_the_excel_list(self, ds):
        counts = ds.projects[SOURCE_COLUMN].value_counts().to_dict()
        assert counts == {"ServiceNow": 11, "Logistics (Excel)": 10}
        bu2 = ds.projects.set_index("project_number").loc["BU2-01"]
        assert bu2["status"] == "Green" and bu2["budget_cost_eur"] == 300000.0  # TEUR and German traffic lights

    def test_the_demo_reports_its_two_duplicates(self, ds):
        same = [f for f in ds.metadata["import_report"]["findings"] if f["message"].startswith("Same project")]
        assert [f["project"] for f in same] == ["PRJ-DEMO-1002", "PRJ-DEMO-1009"]
        messages = " ".join(f["message"] for f in ds.metadata["import_report"]["findings"])
        assert "1850000.0 (ServiceNow) vs. 2100000.0 (Logistics (Excel))" in messages

    def test_the_demo_file_matches_its_script(self, tmp_path):
        from scripts.make_multi_source_demo import write

        fresh = pd.read_excel(write(tmp_path / "bu2.xlsx"), sheet_name=None)
        stored = pd.read_excel(ROOT / "data" / "mappings" / "bu2_portfolio.xlsx", sheet_name=None)
        assert fresh.keys() == stored.keys()
        for name in fresh:
            pd.testing.assert_frame_equal(fresh[name], stored[name])
