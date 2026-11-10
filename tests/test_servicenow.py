"""ServiceNow connector: keyset-paged Table API reads into a local snapshot, delta sync on sys_updated_on, deleted
records removed on a reconcile interval, retries and a failed sync that leaves the snapshot unchanged, credentials
only from the environment, and the SPM mapping into the canonical schema. No live system: the Table API is replayed
from recorded-format responses (tests/fixtures/servicenow)."""
from __future__ import annotations

import copy
import json
import re
import shutil
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.data_adapters import DataSourceError, MappedPortfolioAdapter, get_data_provider
from app.data_adapters.mapping import load_mapping
from app.data_adapters.servicenow import (
    Credentials,
    ServiceNowClient,
    ServiceNowConnector,
    ServiceNowUnreachable,
    SyncReport,
    flatten,
)

ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads((ROOT / "tests" / "fixtures" / "servicenow" / "table_api_spm.json").read_text(encoding="utf-8"))
SPM_MAPPING = ROOT / "data" / "mappings" / "servicenow_spm.yaml"
INSTANCE = "https://example.service-now.com"
SINCE_RE = re.compile(r"sys_updated_on>=javascript:gs\.dateGenerate\('([\d-]+)','([\d:]+)'\)")
AFTER_RE = re.compile(r"sys_id>(\w+)")


class FakeTableApi:
    """Replays the Table API: keyset paging (ORDERBYsys_id, sys_id>last, sysparm_limit), X-Total-Count, the
    sys_updated_on filter of the delta query, id-only reads; records every request. `max_limit` caps the page size
    like an instance's limit, `count=False` sends no X-Total-Count, `extra_count` inflates it, `before` runs before
    every answer (to change the data between pages)."""

    def __init__(self, tables: dict[str, list[dict]], max_limit: int | None = None, count: bool = True):
        self.tables = copy.deepcopy(tables)
        self.calls: list[dict[str, str]] = []
        self.status: list[tuple[int, dict[str, str]]] = []  # forced answers before the next real one
        self.max_limit, self.count, self.extra_count = max_limit, count, 0
        self.before = None

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        assert url.startswith(INSTANCE + "/api/now/table/") and headers["Authorization"].startswith(("Basic", "Bearer"))
        if self.status:
            code, extra = self.status.pop(0)
            return code, extra, b'{"error": {"message": "forced"}}'
        path, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        table = path.rsplit("/", 1)[-1]
        assert "sysparm_offset" not in params and params["sysparm_query"].endswith("ORDERBYsys_id")
        self.calls.append({"table": table, **params})
        if self.before:
            self.before(self, table, params)
        rows = sorted(self.tables.get(table, []), key=lambda r: r["sys_id"]["value"])
        if m := SINCE_RE.search(params["sysparm_query"]):
            since = f"{m.group(1)} {m.group(2)}"
            rows = [r for r in rows if r["sys_updated_on"]["value"] >= since]
        if m := AFTER_RE.search(params["sysparm_query"]):
            rows = [r for r in rows if r["sys_id"]["value"] > m.group(1)]
        if params.get("sysparm_fields") == "sys_id":
            rows = [{"sys_id": r["sys_id"]["value"]} for r in rows]
        limit = min(int(params["sysparm_limit"]), self.max_limit or 10**9)
        answer = {"X-Total-Count": str(len(rows) + self.extra_count)} if self.count else {}
        return 200, answer, json.dumps({"result": rows[:limit]}).encode()


def _client(api: FakeTableApi, page_size: int = 1000) -> ServiceNowClient:
    return ServiceNowClient(Credentials(INSTANCE, "integration.user", "not-a-real-secret"), api, page_size,
                            sleep=lambda s: None)


@pytest.fixture
def mapping_file(tmp_path):
    """The shipped SPM mapping, with its snapshot in a temporary folder."""
    folder = tmp_path / "mappings"
    folder.mkdir()
    shutil.copy(SPM_MAPPING, folder / "servicenow_spm.yaml")
    return folder / "servicenow_spm.yaml"


def _connector(mapping_file) -> ServiceNowConnector:
    return next(c for c in load_mapping(mapping_file).connectors.values() if isinstance(c, ServiceNowConnector))


@pytest.fixture(autouse=True)
def _default_reconcile_interval(monkeypatch):
    monkeypatch.delenv("SERVICENOW_RECONCILE_HOURS", raising=False)


class TestSync:
    def test_first_sync_reads_every_table_page_by_page(self, mapping_file):
        api = FakeTableApi(RECORDED)
        report = _connector(mapping_file).sync(_client(api, page_size=2))
        counts = {t.table: (t.fetched, t.new, t.total, t.full) for t in report.tables}
        assert counts == {"pm_project": (3, 3, 3, True), "pm_project_task": (4, 4, 4, True),
                          "risk": (2, 2, 2, True), "project_status": (2, 2, 2, True)}
        queries = [c["sysparm_query"] for c in api.calls if c["table"] == "pm_project"]
        # 2 + 1 rows: the second page asks for the records after the last sys_id, X-Total-Count ends the read
        assert queries == ["active=true^ORDERBYsys_id", f"active=true^sys_id>p{'0' * 30}2^ORDERBYsys_id"]
        first = api.calls[0]
        assert first["sysparm_display_value"] == "all" and first["sysparm_exclude_reference_link"] == "true"
        assert "3 read, 3 new" in report.summary() and report.ok and not report.warnings
        assert all(t.expected == t.fetched and t.reconciled for t in report.tables)

    def test_second_sync_reads_only_the_changes_and_removes_deleted_records(self, mapping_file, monkeypatch):
        monkeypatch.setenv("SERVICENOW_RECONCILE_HOURS", "0")  # check for deleted records on every sync
        api = FakeTableApi(RECORDED)
        api.tables["pm_project"][2]["sys_updated_on"]["value"] = "2026-10-03 08:00:00"  # the newest change so far
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        changed = api.tables["pm_project"][0]
        changed["short_description"]["value"] = "General Ledger Consolidation II"
        changed["sys_updated_on"]["value"] = "2026-10-04 09:00:00"
        new = copy.deepcopy(api.tables["pm_project"][2])
        new["sys_id"]["value"], new["number"]["value"] = "p" + "9" * 31, "PRJ0010099"
        new["sys_updated_on"]["value"] = "2026-10-04 10:00:00"
        api.tables["pm_project"].append(new)
        api.tables["risk"].pop()  # deleted in ServiceNow
        api.calls.clear()
        report = connector.sync(_client(api))
        by_table = {t.table: t for t in report.tables}
        # changed + new + the unchanged record inside the overlap window (merged by sys_id, not counted as changed)
        assert (by_table["pm_project"].fetched, by_table["pm_project"].new, by_table["pm_project"].updated) == (3, 1, 1)
        assert by_table["pm_project"].watermark == "2026-10-04 10:00:00" and not by_table["pm_project"].full
        assert by_table["pm_project_task"].new + by_table["pm_project_task"].updated == 0
        assert by_table["risk"].deleted == 1
        delta = next(c for c in api.calls if c["table"] == "pm_project" and c.get("sysparm_fields") != "sys_id")
        assert "gs.dateGenerate('2026-10-02','08:00:00')" in delta["sysparm_query"]  # watermark minus the overlap
        rows = connector.read()
        assert len(rows["pm_project"]) == 4 and len(rows["risk"]) == 1
        assert "General Ledger Consolidation II" in set(rows["pm_project"]["short_description"])
        again = connector.sync(_client(api))
        assert {t.table: t.updated + t.new for t in again.tables}["pm_project"] == 0  # the overlap re-reads, no change

    def test_full_sync_starts_over(self, mapping_file):
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        report = connector.sync(_client(api), full=True)
        assert all(t.full and t.fetched == t.total for t in report.tables)

    def test_rate_limit_is_retried_and_a_refusal_explained(self, mapping_file):
        api = FakeTableApi(RECORDED)
        waits = []
        client = ServiceNowClient(Credentials(INSTANCE, token="t"), api, sleep=waits.append)
        api.status = [(429, {"Retry-After": "3"}), (503, {})]
        _connector(mapping_file).sync(client)
        assert waits == [3.0, 2.0]
        api.status = [(401, {})]
        report = _connector(mapping_file).sync(_client(api))
        assert not report.ok and "refused the credentials" in report.error
        assert "not-a-real-secret" not in json.dumps(report.to_dict()) + report.summary()

    def test_display_values_are_kept_next_to_the_value(self):
        row = flatten({"number": {"value": "PRJ1", "display_value": "PRJ1"},
                       "project_manager": {"value": "abc", "display_value": "Priya Raman"}, "plain": 3})
        assert row == {"number": "PRJ1", "project_manager": "abc", "project_manager_display": "Priya Raman", "plain": 3}


def _snapshot_bytes(connector: ServiceNowConnector) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in connector.files()}


def _age_reconcile(connector: ServiceNowConnector, hours: float) -> None:
    """Moves the last check for deleted records `hours` into the past."""
    file = connector.path / "_sync_state.json"
    state = json.loads(file.read_text(encoding="utf-8"))
    for known in state["tables"].values():
        stamp = datetime.fromisoformat(known["reconciled_at"]) - timedelta(hours=hours)
        known["reconciled_at"] = stamp.isoformat(timespec="seconds")
    file.write_text(json.dumps(state), encoding="utf-8")


class TestPaging:
    def test_a_capped_page_size_does_not_cut_the_read_short(self, mapping_file):
        api = FakeTableApi(RECORDED, max_limit=1)  # the instance answers 1 record although 1000 were asked for
        report = _connector(mapping_file).sync(_client(api))
        tasks = next(t for t in report.tables if t.table == "pm_project_task")
        assert (tasks.fetched, tasks.expected, tasks.total) == (4, 4, 4) and report.ok and not report.warnings
        assert len([c for c in api.calls if c["table"] == "pm_project_task"]) == 4

    def test_without_x_total_count_the_read_goes_on_until_an_empty_page(self, mapping_file):
        api = FakeTableApi(RECORDED, max_limit=3, count=False)
        report = _connector(mapping_file).sync(_client(api))
        tasks = next(t for t in report.tables if t.table == "pm_project_task")
        assert (tasks.fetched, tasks.expected) == (4, None) and not report.warnings
        assert len([c for c in api.calls if c["table"] == "pm_project_task"]) == 3  # 3 + 1 + the empty page

    def test_fewer_records_than_x_total_count_are_flagged(self, mapping_file):
        api = FakeTableApi(RECORDED)
        api.extra_count = 1
        report = _connector(mapping_file).sync(_client(api))
        assert report.ok and len(report.warnings) == 4
        assert "pm_project: 3 record(s) read, X-Total-Count reported 4" in report.warnings[0]
        assert "Warning – pm_project: 3 record(s) read" in report.summary()

    def test_an_unchanged_project_survives_a_deletion_during_the_id_read(self, mapping_file, monkeypatch):
        """With offset paging, deleting a record from the first page shifts the next one past the second page: it
        would be missing from the id list and removed from the snapshot although nothing happened to it."""
        monkeypatch.setenv("SERVICENOW_RECONCILE_HOURS", "0")
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        first = api.tables["pm_project"][0]["sys_id"]["value"]

        def delete_after_first_page(fake, table, params):
            id_read = params.get("sysparm_fields") == "sys_id"
            if table == "pm_project" and id_read and "sys_id>" in params["sysparm_query"]:
                fake.tables["pm_project"] = [r for r in fake.tables["pm_project"] if r["sys_id"]["value"] != first]

        api.before = delete_after_first_page
        report = connector.sync(_client(api, page_size=1))
        project = next(t for t in report.tables if t.table == "pm_project")
        assert project.deleted == 0 and project.total == 3  # the deleted one goes on the next check
        api.before = None
        assert next(t for t in connector.sync(_client(api)).tables if t.table == "pm_project").deleted == 1
        assert sorted(connector.read()["pm_project"]["number"]) == ["PRJ0010002", "PRJ0010003"]

    def test_an_incomplete_id_list_deletes_nothing(self, mapping_file, monkeypatch):
        monkeypatch.setenv("SERVICENOW_RECONCILE_HOURS", "0")
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        api.tables["risk"].pop()
        api.extra_count = 1  # the instance announces more ids than it sends
        report = connector.sync(_client(api))
        risk = next(t for t in report.tables if t.table == "risk")
        assert risk.deleted == 0 and not risk.reconciled and any("id list incomplete" in w for w in risk.warnings)
        assert len(connector.read()["risk"]) == 2


class TestReconcileAndRecovery:
    def test_deleted_records_are_checked_only_once_the_interval_has_passed(self, mapping_file):
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        api.tables["risk"].pop()
        api.calls.clear()
        report = connector.sync(_client(api))  # right after the full read: no id read
        assert not any(c.get("sysparm_fields") == "sys_id" for c in api.calls)
        assert next(t for t in report.tables if t.table == "risk").deleted == 0
        _age_reconcile(connector, 25)  # default interval: 24 h
        report = connector.sync(_client(api))
        risk = next(t for t in report.tables if t.table == "risk")
        assert risk.deleted == 1 and risk.reconciled
        api.calls.clear()
        connector.sync(_client(api))  # checked a moment ago: not again
        assert not any(c.get("sysparm_fields") == "sys_id" for c in api.calls)

    def test_the_interval_comes_from_the_environment(self, mapping_file, monkeypatch):
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        _age_reconcile(connector, 2)
        monkeypatch.setenv("SERVICENOW_RECONCILE_HOURS", "1")
        api.tables["risk"].pop()
        assert next(t for t in connector.sync(_client(api)).tables if t.table == "risk").deleted == 1
        monkeypatch.setenv("SERVICENOW_RECONCILE_HOURS", "soon")
        with pytest.raises(DataSourceError, match="SERVICENOW_RECONCILE_HOURS"):
            connector.sync(_client(api))

    def test_a_missing_snapshot_file_reads_that_table_completely(self, mapping_file):
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        (connector.path / "pm_project.json").unlink()
        (connector.path / "risk.json").write_text("{not json", encoding="utf-8")
        api.calls.clear()
        report = connector.sync(_client(api))
        by_table = {t.table: t for t in report.tables}
        assert by_table["pm_project"].full and by_table["pm_project"].total == 3
        assert by_table["risk"].full and by_table["risk"].total == 2
        assert not by_table["pm_project_task"].full
        full_read = next(c for c in api.calls if c["table"] == "pm_project")
        assert "sys_updated_on>=" not in full_read["sysparm_query"]
        assert len(connector.read()["pm_project"]) == 3

    def test_server_errors_are_retried_with_exponential_backoff_then_fail_cleanly(self, mapping_file):
        api = FakeTableApi(RECORDED)
        connector = _connector(mapping_file)
        connector.sync(_client(api))
        before = _snapshot_bytes(connector)
        api.tables["pm_project"][0]["short_description"]["value"] = "Changed"
        api.tables["pm_project"][0]["sys_updated_on"]["value"] = "2026-10-05 08:00:00"

        def fail_last_table(url, headers):  # the tables before it were read, the last one stays unavailable
            return (503, {}, b"") if "/project_status?" in url else api(url, headers)
        waits = []
        client = ServiceNowClient(Credentials(INSTANCE, token="t"), fail_last_table, sleep=waits.append)
        report = connector.sync(client)
        assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]
        assert not report.ok and "HTTP 503" in report.error and "after 6 attempt(s)" in report.error
        assert "failed" in report.summary() and "snapshot is unchanged" in report.summary()
        assert _snapshot_bytes(connector) == before  # the tables read before the error were not written either
        assert not list(connector.path.glob("*.tmp"))

    def test_a_timeout_is_retried(self, mapping_file):
        api = FakeTableApi(RECORDED)
        failures = [ServiceNowUnreachable("ServiceNow is not reachable: timed out")]

        def flaky(url, headers):
            if failures:
                raise failures.pop()
            return api(url, headers)
        waits = []
        report = _connector(mapping_file).sync(ServiceNowClient(Credentials(INSTANCE, token="t"), flaky,
                                                                sleep=waits.append))
        assert report.ok and waits == [1.0]
        down = ServiceNowClient(Credentials(INSTANCE, token="t"), lambda u, h: (_ for _ in ()).throw(
            ServiceNowUnreachable("ServiceNow is not reachable: timed out")), sleep=lambda s: None, retries=2)
        report = _connector(mapping_file).sync(down)
        assert not report.ok and "timed out after 3 attempt(s)" in report.error and down.requests == 3

    def test_the_time_limit_of_the_whole_sync_stops_waiting(self, mapping_file):
        api = FakeTableApi(RECORDED)
        api.status = [(429, {"Retry-After": "3600"})]
        now = [0.0]
        client = ServiceNowClient(Credentials(INSTANCE, token="t"), api, sleep=lambda s: now.__setitem__(0, now[0] + s),
                                  max_seconds=600, clock=lambda: now[0])
        report = _connector(mapping_file).sync(client)
        assert not report.ok and "time limit of 10 min" in report.error and client.requests == 1
        assert _connector(mapping_file).state() == {}  # nothing written: the snapshot still says "run the sync"

    def test_the_admin_trigger_and_the_script_report_a_failed_sync(self, monkeypatch, capsys):
        import importlib.util

        from app.views import admin as views

        failed = SyncReport(INSTANCE, "2026-10-07T08:00:00+00:00", error="ServiceNow answered HTTP 503.")
        monkeypatch.setattr(views, "sync_servicenow", lambda full: [failed])
        outcome = views.run_servicenow_sync(False, "admin", "System Admin")
        assert outcome.kind == "error" and "failed" in outcome.message
        spec = importlib.util.spec_from_file_location("servicenow_sync", ROOT / "scripts" / "servicenow_sync.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        monkeypatch.setattr(script, "sync_servicenow", lambda full: [failed])
        assert script.main([]) == 1 and "snapshot is unchanged" in capsys.readouterr().out
        ok = SyncReport(INSTANCE, "2026-10-07T08:00:00+00:00")
        monkeypatch.setattr(script, "sync_servicenow", lambda full: [ok])
        assert script.main([]) == 0


class TestCredentialsAndSafety:
    def test_credentials_only_from_the_environment(self, monkeypatch):
        for name in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD", "SERVICENOW_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(DataSourceError, match="No ServiceNow instance"):
            Credentials.from_env()
        monkeypatch.setenv("SERVICENOW_INSTANCE", "example.service-now.com")
        with pytest.raises(DataSourceError, match="never in the mapping file"):
            Credentials.from_env()
        monkeypatch.setenv("SERVICENOW_USER", "integration.user")
        monkeypatch.setenv("SERVICENOW_PASSWORD", "s3cret-value")
        creds = Credentials.from_env()
        assert creds.instance == INSTANCE and "s3cret" not in repr(creds)
        assert creds.headers()["Authorization"].startswith("Basic ")

    def test_a_secret_in_the_mapping_file_is_refused(self, tmp_path):
        with pytest.raises(DataSourceError, match="must not contain password"):
            ServiceNowConnector(tmp_path, options={"tables": ["pm_project"], "password": "x"})

    def test_loading_before_the_first_sync_says_what_to_do(self, mapping_file):
        with pytest.raises(DataSourceError, match="run the sync first"):
            MappedPortfolioAdapter(mapping_file).load_dataset()

    def test_never_written_back_to_servicenow(self, mapping_file):
        adapter = MappedPortfolioAdapter(mapping_file)
        assert adapter.write_mode == "store" and not _connector(mapping_file).writable


class TestSpmMapping:
    @pytest.fixture
    def loaded(self, mapping_file):
        _connector(mapping_file).sync(_client(FakeTableApi(RECORDED)))
        adapter = MappedPortfolioAdapter(mapping_file)
        return adapter, adapter.load_dataset()

    def test_projects_tasks_risks_and_status_in_canonical_form(self, loaded):
        adapter, ds = loaded
        projects = ds.projects.set_index("project_number")
        assert list(projects.index) == ["PRJ0010001", "PRJ0010002", "PRJ0010003"]
        erp = projects.loc["PRJ0010001"]
        assert erp["project_name"] == "General Ledger Consolidation" and erp["project_manager"] == "Priya Raman"
        assert erp["status"] == "Red" and erp["state"] == "Work in Progress" and erp["execution_type"] == "Waterfall"
        assert erp["budget_cost_eur"] == pytest.approx(1_200_000) and erp["actual_cost_eur"] == pytest.approx(640_000)
        assert erp["business_unit"] == "Shared Services"
        assert set(ds.tasks["project_number"]) <= set(projects.index)
        risk = ds.risks.set_index("risk_number").loc["RSK0001"]
        assert (risk["project_number"], risk["probability"], risk["impact"]) == ("PRJ0010001", "High", "Moderate")
        assert set(ds.status_reports["overall_health"]) == {"Yellow"}
        report = adapter.last_import_report
        assert report is not None and not [f for f in report.findings if f["severity"] == "ERROR"]

    def test_the_pipeline_runs_on_servicenow_data(self, loaded):
        from app.core.security import ROLE_PMO
        from app.crew.pipeline import run_steering_pipeline
        from app.crew.tools.data_steward_tools import run_all_checks

        _, ds = loaded
        assert [i for i in run_all_checks(ds).issues if i.severity == "ERROR"] == []
        result = run_steering_pipeline("Cut the budget of PRJ0010001 by 10 %", ds, ROLE_PMO, "PMO-ADMIN")
        assert result.mode == "offline" and result.verdict is not None

    def test_factory_reads_the_servicenow_mapping(self, mapping_file, monkeypatch):
        from app.core.config import get_settings

        _connector(mapping_file).sync(_client(FakeTableApi(RECORDED)))
        monkeypatch.setenv("DATA_SOURCE", "servicenow")
        monkeypatch.setenv("SERVICENOW_MAPPING_FILE", str(mapping_file))
        get_settings.cache_clear()
        try:
            provider = get_data_provider()
            assert isinstance(provider, MappedPortfolioAdapter)
            assert len(provider.load_dataset().projects) == 3
            assert any(p.name == "_sync_state.json" for p in provider.source_files())
        finally:
            get_settings.cache_clear()


def test_three_hundred_projects_load_from_the_snapshot_within_a_second(mapping_file):
    tables = copy.deepcopy(RECORDED)
    template = tables["pm_project"][0]
    tables["pm_project"] = []
    for i in range(300):
        row = copy.deepcopy(template)
        row["sys_id"]["value"], row["number"]["value"] = f"p{i:031d}", f"PRJ{i:07d}"
        tables["pm_project"].append(row)
    tables["pm_project_task"] = [copy.deepcopy(tables["pm_project_task"][0]) for _ in range(3000)]
    for i, row in enumerate(tables["pm_project_task"]):
        row["sys_id"]["value"], row["number"]["value"] = f"t{i:031d}", f"PRJTASK{i:07d}"
        row["top_task"]["display_value"] = f"PRJ{i % 300:07d}"
    _connector(mapping_file).sync(_client(FakeTableApi(tables)))
    adapter = MappedPortfolioAdapter(mapping_file)
    started = time.perf_counter()
    ds = adapter.load_dataset()
    elapsed = time.perf_counter() - started
    assert len(ds.projects) == 300 and len(ds.tasks) == 3000
    assert elapsed < 1.0, f"loading took {elapsed:.2f} s"
