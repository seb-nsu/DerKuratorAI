"""Generic data connection: connectors (JSON, Excel, CSV folder), mapping files, canonical schema, import report,
write-back through the inverse mapping or into the mapping's own store, and the Excel template."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from app.core import proposals
from app.core.security import ROLE_PM, ROLE_PMO
from app.crew.tools.data_steward_tools import run_all_checks
from app.data_adapters import DataSourceError, JsonPortfolioAdapter, MappedPortfolioAdapter, get_data_provider
from app.data_adapters.dataset import TABLES, PortfolioDataset
from app.data_adapters.mapping import load_mapping
from app.data_adapters.schema import SCHEMA
from app.data_adapters.template import FIELDS_SHEET, write_template
from app.graph.portfolio_graph import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
DEMO_MAPPING = ROOT / "data" / "mappings" / "servicenow_demo.yaml"
MONEY = ("budget_cost_eur", "actual_cost_eur", "estimate_at_completion_eur", "total_planned_cost_eur",
         "planned_capital_eur", "planned_operating_eur")
GERMAN = {"project_number": "Nr", "project_name": "Bezeichnung", "project_manager": "Projektleiter",
          "status": "Ampel", **{c: f"{c} (TEUR)" for c in MONEY}}
TRAFFIC = {"Green": "grün", "Yellow": "gelb", "Red": "rot"}


@pytest.fixture(scope="module")
def demo():
    if not SOURCE.exists():
        pytest.skip("Test portfolio JSON not available")
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def _write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _customer_workbook(demo, path: Path, *, drop: tuple[str, ...] = (), years_as_columns: bool = True) -> Path:
    """The demo portfolio as a customer would keep it: German sheet and column names, amounts in TEUR, traffic
    lights in German and – optionally – the cost plan with one column per year."""
    projects = pd.DataFrame(demo["Projects"])
    for col in MONEY:
        projects[col] = projects[col] / 1000
    projects["status"] = projects["status"].map(TRAFFIC)
    projects = projects.rename(columns=GERMAN).drop(columns=list(drop))
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        projects.to_excel(writer, sheet_name="Vorhaben", index=False)
        for name in TABLES.values():
            if name in ("Projects", "Cost_Plans") or name not in demo:
                continue
            pd.DataFrame(demo[name]).to_excel(writer, sheet_name=name, index=False)
        costs = pd.DataFrame(demo["Cost_Plans"])
        if years_as_columns:
            costs["year"] = pd.to_datetime(costs["month"]).dt.year.astype(str)
            wide = costs.pivot_table(index=["project_number", "cost_type"], columns="year", values="planned_cost_eur",
                                     aggfunc="sum").reset_index()
            wide.columns = [str(c) for c in wide.columns]
            for year in [c for c in wide.columns if c.isdigit()]:
                wide[year] = wide[year] / 1000
            wide.rename(columns={"project_number": "Projekt", "cost_type": "Kostenart"}).to_excel(
                writer, sheet_name="Kostenplan", index=False)
        else:
            costs.to_excel(writer, sheet_name="Cost_Plans", index=False)
    return path


CUSTOMER_MAPPING = """
name: Customer portfolio
sources:
  book: {type: excel, path: customer.xlsx}
tables:
  Projects:
    from: Vorhaben
    columns: {COLUMNS}
    values: {status: {grün: Green, gelb: Yellow, rot: Red}}
    units: {UNITS}
  Cost_Plans:
    from: Kostenplan
    columns: {Projekt: project_number, Kostenart: cost_type}
    unpivot: {columns: "^\\\\d{4}$", to: month, value: planned_cost_eur, year_to_date: true}
    units: {planned_cost_eur: 1000}
    defaults: {actual_cost_eur: 0}
    key: {generate: "{project_number}-{cost_type}-{month:%Y}"}
OTHER
"""


def _customer_mapping(tmp_path: Path, years_as_columns: bool = True) -> Path:
    columns = "{" + ", ".join(f'"{src}": {canon}' for canon, src in GERMAN.items()) + "}"
    units = "{" + ", ".join(f"{c}: 1000" for c in MONEY) + "}"
    others = "".join(f"  {t}: {{}}\n" for t in TABLES.values() if t not in ("Projects", "Cost_Plans"))
    text = CUSTOMER_MAPPING.replace("{COLUMNS}", columns).replace("{UNITS}", units).replace("OTHER\n", others)
    if not years_as_columns:
        start = text.index("  Cost_Plans:")
        end = text.index("\n", text.index("    key:")) + 1
        text = text[:start] + "  Cost_Plans: {}\n" + text[end:]
    return _write_yaml(tmp_path / "customer.yaml", text)


def _approve(adapter, changes):
    entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "test", "", None, changes)
    return adapter.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"])


def _budget(pid, old, new):
    return {"project_id": pid, "table": "Projects", "field": "budget_cost_eur", "old": old, "new": new,
            "source_tool": "test"}


# =========================================================================== demo data through a mapping
class TestDemoThroughMapping:
    def test_mapping_file_loads_the_demo_identically(self, demo):
        raw = PortfolioDataset.from_tables({n: pd.DataFrame(demo[n]) for n in TABLES.values() if n in demo})
        mapped = MappedPortfolioAdapter(DEMO_MAPPING).load_dataset()
        legacy = JsonPortfolioAdapter(SOURCE).load_dataset()
        for attr in TABLES:
            pd.testing.assert_frame_equal(getattr(mapped, attr), getattr(raw, attr))
            pd.testing.assert_frame_equal(getattr(legacy, attr), getattr(raw, attr))
        assert mapped.metadata["import_report"]["findings"] == []

    def test_same_verdict_through_the_mapping(self, demo, monkeypatch):
        from app.crew import pipeline
        from app.crew.scenarios import build_guided_scenario
        from app.crew.tools import graph_tools
        from app.graph.portfolio_graph import build_graph

        dataset = MappedPortfolioAdapter(DEMO_MAPPING).load_dataset()
        monkeypatch.setattr(graph_tools, "get_graph", lambda: build_graph(dataset))
        names = dict(zip(dataset.projects["project_number"], dataset.projects["project_name"], strict=True))
        for pct, label in ((20, "NO-GO"), (10, "CONDITIONAL GO")):
            scenario = build_guided_scenario("budget_change", "PRJ-DEMO-1002", names, value=pct, is_perc=True,
                                             direction="decrease")
            result = pipeline.run_steering_pipeline(scenario.request, dataset, ROLE_PMO, "PMO-ADMIN", plan=scenario.plan)
            assert result.verdict.label == label

    def test_factory_and_change_detection(self, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setenv("DATA_SOURCE", "mapped")
        monkeypatch.setenv("MAPPING_FILE", str(DEMO_MAPPING))
        get_settings.cache_clear()
        try:
            provider = get_data_provider()
            assert isinstance(provider, MappedPortfolioAdapter) and provider.write_mode == "source"
            assert provider.source_path == SOURCE.resolve()
            files = provider.source_files()
            assert SOURCE.resolve() in files and DEMO_MAPPING in files
            fingerprint = source_fingerprint([f for f in files if f.exists()])
            assert fingerprint and fingerprint["sha256"]
        finally:
            get_settings.cache_clear()


# =========================================================================== a customer's own structure
class TestCustomerExcel:
    @pytest.fixture
    def customer(self, demo, tmp_path):
        _customer_workbook(demo, tmp_path / "customer.xlsx")
        return MappedPortfolioAdapter(_customer_mapping(tmp_path))

    def test_renamed_sheets_teur_and_year_columns_load(self, demo, customer):
        ds = customer.load_dataset()
        original = pd.DataFrame(demo["Projects"]).set_index("project_number")
        loaded = ds.projects.set_index("project_number")
        for col in MONEY:
            assert loaded[col].round(2).to_dict() == original[col].astype(float).round(2).to_dict()
        assert loaded["status"].to_dict() == original["status"].to_dict()
        costs = pd.DataFrame(demo["Cost_Plans"]).assign(year=lambda d: pd.to_datetime(d["month"]).dt.year)
        expected = costs.groupby(["project_number", "year"])["planned_cost_eur"].sum().round(2)
        got = ds.cost_plans.assign(year=ds.cost_plans["month"].dt.year) \
            .groupby(["project_number", "year"])["planned_cost_eur"].sum().round(2)
        assert got.to_dict() == expected.to_dict()
        assert ds.cost_plans["cost_plan_number"].is_unique
        assert ds.metadata["import_report"]["findings"] == []
        assert ds.metadata["import_report"]["tables"]["Cost_Plans"]["source"] == "customer.xlsx:Kostenplan"

    def test_the_customer_data_runs_through_the_steering_pipeline(self, customer, monkeypatch):
        from app.crew import pipeline
        from app.crew.tools import graph_tools
        from app.graph.portfolio_graph import build_graph

        dataset = customer.load_dataset()
        monkeypatch.setattr(graph_tools, "get_graph", lambda: build_graph(dataset))
        result = pipeline.run_steering_pipeline("Cut the budget of General Ledger Consolidation by 20 %", dataset, ROLE_PMO,
                                                "PMO-ADMIN")
        assert result.verdict.label == "NO-GO"

    def test_unpivoted_source_writes_into_the_own_store(self, customer, tmp_path):
        assert customer.write_mode == "store" and customer.source_path == tmp_path / "customer.store.json"
        before = (tmp_path / "customer.xlsx").read_bytes()
        _approve(customer, [_budget("PRJ-DEMO-1001", 420000.0, 400000.0)])
        assert (tmp_path / "customer.xlsx").read_bytes() == before  # the customer's file is never touched
        store = json.loads((tmp_path / "customer.store.json").read_text(encoding="utf-8"))
        assert store["updates"]["Projects"]["PRJ-DEMO-1001"] == {"budget_cost_eur": 400000.0}
        projects = customer.load_dataset().projects.set_index("project_number")
        assert projects.at["PRJ-DEMO-1001", "budget_cost_eur"] == 400000.0

    def test_the_source_wins_once_it_moved_on_after_the_approval(self, customer, tmp_path):
        """An approved change in the store is not copied into the customer's system: once that system changes the
        same field (e.g. the PMO transferred the change, or someone decided differently), its value applies again."""
        _approve(customer, [_budget("PRJ-DEMO-1001", 420000.0, 400000.0)])
        store = json.loads((tmp_path / "customer.store.json").read_text(encoding="utf-8"))
        assert store["source_values"]["Projects"]["PRJ-DEMO-1001"] == {"budget_cost_eur": 420000.0}
        book = load_workbook(tmp_path / "customer.xlsx")
        ws = book["Vorhaben"]
        header = {c.value: c.column for c in ws[1]}
        row = next(r for r in ws.iter_rows(min_row=2) if r[header["Nr"] - 1].value == "PRJ-DEMO-1001")
        row[header["budget_cost_eur (TEUR)"] - 1].value = 410
        book.save(tmp_path / "customer.xlsx")
        ds = customer.load_dataset()
        assert ds.projects.set_index("project_number").at["PRJ-DEMO-1001", "budget_cost_eur"] == 410000.0
        assert any("source changed budget_cost_eur" in f["message"] for f in ds.metadata["import_report"]["findings"])

    def test_store_is_conflict_checked_like_a_source(self, customer):
        from app.data_adapters import DataConflictError

        _approve(customer, [_budget("PRJ-DEMO-1001", 420000.0, 400000.0)])
        with pytest.raises(DataConflictError):
            _approve(customer, [_budget("PRJ-DEMO-1001", 420000.0, 380000.0)])

    def test_one_to_one_mapping_writes_back_in_source_names_and_units(self, demo, tmp_path):
        _customer_workbook(demo, tmp_path / "customer.xlsx", years_as_columns=False)
        adapter = MappedPortfolioAdapter(_customer_mapping(tmp_path, years_as_columns=False))
        assert adapter.write_mode == "source"
        status = adapter.load_dataset().projects.set_index("project_number").at["PRJ-DEMO-1001", "status"]
        assert status == "Red"
        _approve(adapter, [_budget("PRJ-DEMO-1001", 420000.0, 400000.0),
                           {**_budget("PRJ-DEMO-1001", status, "Yellow"), "field": "status"}])
        ws = load_workbook(tmp_path / "customer.xlsx")["Vorhaben"]
        header = {c.value: c.column for c in ws[1]}
        row = next(r for r in ws.iter_rows(min_row=2) if r[header["Nr"] - 1].value == "PRJ-DEMO-1001")
        assert row[header["budget_cost_eur (TEUR)"] - 1].value == 400
        assert row[header["Ampel"] - 1].value == "gelb"
        assert "budget_cost_eur" not in header  # no canonical column is added to the customer's sheet
        assert adapter.load_dataset().projects.set_index("project_number").at["PRJ-DEMO-1001", "status"] == "Yellow"


# =========================================================================== findings instead of crashes
class TestImportFindings:
    def test_missing_required_column_is_a_finding(self, demo, tmp_path):
        _customer_workbook(demo, tmp_path / "customer.xlsx", drop=("Projektleiter",))
        dataset = MappedPortfolioAdapter(_customer_mapping(tmp_path)).load_dataset()
        assert dataset.projects["project_manager"].isna().all()
        report = run_all_checks(dataset)
        imported = [i for i in report.issues if i.rule == "Import"]
        assert any(i.severity == "ERROR" and "project_manager" in i.message for i in imported)
        assert any("Projektleiter" in i.message for i in imported)  # names the source column the mapping expects
        assert not report.can_proceed  # no quantitative simulation on a portfolio without owners

    def test_bad_values_and_rows_without_project(self, tmp_path):
        rows = [{"project_number": "P-1", "project_name": "A", "project_manager": "Priya Raman", "status": "Grün"}]
        tasks = [{"task_number": "T-1", "project_number": "P-1", "state": "Open", "percent_complete": 0,
                  "planned_effort_hours": 8},
                 {"task_number": "T-2", "project_number": None, "state": "Open", "percent_complete": 0,
                  "planned_effort_hours": 8}]
        src = tmp_path / "p.json"
        src.write_text(json.dumps({"Projects": rows, "Project_Tasks": tasks, "Resource_Plans": [],
                                   "Stakeholders": [], "Risks": []}), encoding="utf-8")
        mapping = _write_yaml(tmp_path / "m.yaml", """
sources: {s: {type: json, path: p.json}}
tables:
  Projects: {values: {status: {Grün: Green}, phase: {Plan: Planning}}, columns: {Leiter: project_manager}}
  Project_Tasks: {}
  Resource_Plans: {}
  Stakeholders: {}
  Risks: {}
""")
        adapter = MappedPortfolioAdapter(mapping)
        dataset = adapter.load_dataset()
        assert dataset.projects.at[0, "status"] == "Green"
        assert dataset.tasks["task_number"].tolist() == ["T-1"]
        table = adapter.last_import_report.tables["Project_Tasks"]
        assert (table["read"], table["loaded"], table["dropped"]) == (2, 1, 1)
        messages = " ".join(f["message"] for f in adapter.last_import_report.findings)
        assert "without project_number" in messages and "'Leiter'" in messages

    def test_csv_folder_with_german_numbers_and_dates(self, demo, tmp_path):
        folder = tmp_path / "csv"
        folder.mkdir()
        benefits = pd.DataFrame(demo["Benefit_Plans"])
        benefits["planned_benefit_eur"] = benefits["planned_benefit_eur"].map(lambda v: f"{v:,.2f}".replace(",", "X")
                                                                             .replace(".", ",").replace("X", "."))
        benefits["realization_start_date"] = pd.to_datetime(benefits["realization_start_date"]).dt.strftime("%d.%m.%Y")
        benefits.to_csv(folder / "Nutzen.csv", sep=";", index=False)
        (tmp_path / "p.json").write_text(json.dumps(demo), encoding="utf-8")
        others = "".join(f"  {t}: {{}}\n" for t in TABLES.values() if t != "Benefit_Plans")
        mapping = _write_yaml(tmp_path / "m.yaml", f"""
sources:
  main: {{type: json, path: p.json}}
  csv: {{type: csv, path: csv, options: {{sep: ";", decimal: ",", date_format: "%d.%m.%Y"}}}}
default_source: main
tables:
  Benefit_Plans: {{source: csv, from: Nutzen}}
{others}""")
        adapter = MappedPortfolioAdapter(mapping)
        assert adapter.write_mode == "store"  # two sources: approved changes go to the own store
        loaded = adapter.load_dataset().benefit_plans
        expected = JsonPortfolioAdapter(tmp_path / "p.json").load_dataset().benefit_plans
        pd.testing.assert_series_equal(loaded["planned_benefit_eur"], expected["planned_benefit_eur"])
        pd.testing.assert_series_equal(loaded["realization_start_date"], expected["realization_start_date"])
        assert (folder / "Nutzen.csv") in adapter.source_files()

    @pytest.mark.parametrize("text, message", [
        ("tables: {}", "needs a 'sources' section"),
        ("sources: {a: {type: sap, path: x}}", "needs a type"),
        ("sources: {a: {type: json, path: x.json}}\ntables: {Projekte: {}}", "Unknown table 'Projekte'"),
        ("sources: {a: {type: json, path: x.json}}\ntables: {Projects: {source: b}}", "unknown source 'b'"),
        ("sources: {a: {type: json, path: x.json}}\ntables: {Projects: {colums: {}}}", "Unknown setting"),
    ])
    def test_mapping_mistakes_are_explained(self, tmp_path, text, message):
        with pytest.raises(DataSourceError, match=message):
            load_mapping(_write_yaml(tmp_path / "m.yaml", text))

    def test_loading_involves_no_llm(self):
        modules = ["schema", "connectors", "mapping", "mapped_adapter", "dataset", "base"]
        for name in modules:
            tree = ast.parse((ROOT / "app" / "data_adapters" / f"{name}.py").read_text(encoding="utf-8"))
            imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
                a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            assert not any(m.startswith(("agents", "anthropic", "openai", "app.crew", "app.llm")) for m in imported), (name, imported)


# =========================================================================== Excel template
class TestExcelTemplate:
    def test_template_has_every_table_with_fields_and_dropdowns(self, tmp_path):
        path = write_template(tmp_path / "template.xlsx")
        wb = load_workbook(path)
        assert set(SCHEMA) | {FIELDS_SHEET} == set(wb.sheetnames)
        projects = wb["Projects"]
        header = [c.value for c in projects[1]]
        assert {"project_number", "project_manager", "budget_cost_eur"} <= set(header)
        lists = [dv.formula1 for dv in projects.data_validations.dataValidation]
        assert '"Green,Yellow,Red"' in lists
        rows = list(wb[FIELDS_SHEET].iter_rows(min_row=2, values_only=True))
        status = next(r for r in rows if r[0] == "Projects" and r[1] == "status")
        assert status[3] == "yes" and "Allowed: Green, Yellow, Red" in status[5]

    def test_filled_template_loads_without_mapping(self, tmp_path):
        from app.data_adapters import ExcelPortfolioAdapter

        path = write_template(tmp_path / "template.xlsx")
        wb = load_workbook(path)
        ws = wb["Projects"]
        header = [c.value for c in ws[1]]
        values = {"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                  "status": "Green", "state": "Open", "execution_type": "Agile", "percent_complete": 10,
                  "planned_end_date": "2030-06-30", **{c: 1000 for c in MONEY}}
        ws.append([values.get(h) for h in header])
        wb.save(path)
        dataset = ExcelPortfolioAdapter(path).load_dataset()
        assert dataset.project_numbers == ["P-1"]
        assert dataset.metadata["import_report"]["findings"] == []
