"""Microsoft Planner and monday.com connectors (E8): sync through a fake transport into the snapshot, paging, change
counts, retry, refused credentials and secrets, a failed sync that leaves the snapshot alone, and the team-tools
mapping that takes Project_Tasks from ServiceNow, Planner and monday.com together. No network."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.data_adapters.base import DataSourceError
from app.data_adapters.mapped_adapter import MappedPortfolioAdapter
from app.data_adapters.work_management import GRAPH, MONDAY_API, MondayConnector, PlannerConnector

ROOT = Path(__file__).resolve().parents[1]
PLAN = "plan-1"


class FakeApi:
    """Answers by URL (GET) or by the GraphQL query (POST); `fail` answers 503 that many times first."""

    def __init__(self, answers, fail: int = 0, status: int = 200):
        self.answers, self.fail, self.status, self.calls = answers, fail, status, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if self.fail:
            self.fail -= 1
            return 503, {"Retry-After": "0"}, b""
        if self.status != 200:
            return self.status, {}, b"no"
        answer = self.answers(method, url, json.loads(body)["query"] if body and method == "POST" and
                              url == MONDAY_API else None)
        return 200, {}, json.dumps(answer).encode()


def planner_api(tasks_pages, plan=PLAN):
    def answer(method, url, query):
        if url.endswith(f"/plans/{plan}"):
            return {"id": plan, "title": "ERP rollout", "createdDateTime": "2026-01-05T08:00:00Z"}
        if url.endswith("/buckets"):
            return {"value": [{"id": "b1", "name": "Finance"}]}
        page = int(url.split("page=")[1]) if "page=" in url else 0
        nxt = {"@odata.nextLink": f"{GRAPH}/planner/plans/{plan}/tasks?page={page + 1}"} \
            if page + 1 < len(tasks_pages) else {}
        return {"value": tasks_pages[page], **nxt}
    return answer


def task(tid, pct=50, etag="W/1"):
    return {"id": tid, "bucketId": "b1", "title": f"Task {tid}", "percentComplete": pct,
            "startDateTime": "2026-02-01T00:00:00Z", "dueDateTime": "2026-03-15T00:00:00Z",
            "assignments": {"user-guid-1": {}}, "@odata.etag": etag, "priority": 5}


@pytest.fixture
def graph_token(monkeypatch):
    monkeypatch.setenv("MS_GRAPH_TOKEN", "graph-secret-token")


def planner(tmp_path) -> PlannerConnector:
    return PlannerConnector(tmp_path / "planner", options={"plans": {PLAN: {"project": "PRJ-DEMO-1002"}}})


def test_planner_sync_pages_and_counts_changes(tmp_path, graph_token):
    conn = planner(tmp_path)
    report = conn.sync(FakeApi(planner_api([[task("t1"), task("t2", 0)], [task("t3", 100)]])), sleep=lambda s: None)
    assert report.ok, report.error
    tasks = conn.read()["planner_tasks"]
    assert sorted(tasks["id"]) == ["t1", "t2", "t3"]
    row = tasks.set_index("id").loc["t3"]
    assert row["state"] == "Closed Complete" and row["project_number"] == "PRJ-DEMO-1002"
    assert row["bucket"] == "Finance" and row["due_date"] == "2026-03-15" and row["assignees"] == 1
    assert "user-guid-1" not in json.dumps(tasks.to_dict("records"))  # assignees as a count, no person
    again = conn.sync(FakeApi(planner_api([[task("t1", etag="W/2"), task("t4")]])), sleep=lambda s: None)
    counts = {t.table: t for t in again.tables}["planner_tasks"]
    assert (counts.new, counts.updated, counts.deleted, counts.total) == (1, 1, 2, 2)
    assert "graph-secret-token" not in json.dumps(again.to_dict()) + again.summary()


def test_planner_client_credentials_flow(tmp_path, monkeypatch):
    for key, value in {"MS_GRAPH_TENANT_ID": "contoso", "MS_GRAPH_CLIENT_ID": "app",
                       "MS_GRAPH_CLIENT_SECRET": "s3cret"}.items():
        monkeypatch.setenv(key, value)

    def answer(method, url, query):
        if method == "POST":
            return {"access_token": "issued"}
        return planner_api([[task("t1")]])(method, url, query)

    api = FakeApi(answer)
    assert planner(tmp_path).sync(api).ok
    assert "login.microsoftonline.com/contoso/" in api.calls[0][1] and b"s3cret" in api.calls[0][3]
    assert all(c[2]["Authorization"] == "Bearer issued" for c in api.calls[1:])


def test_retry_then_failure_leaves_the_snapshot(tmp_path, graph_token):
    conn = planner(tmp_path)
    waits = []
    assert conn.sync(FakeApi(planner_api([[task("t1")]]), fail=2), sleep=waits.append).ok and waits == [0.0, 0.0]
    failed = conn.sync(FakeApi(planner_api([[]]), status=403))
    assert not failed.ok and "refused the credentials" in failed.error and "unchanged" in failed.summary()
    assert list(conn.read()["planner_tasks"]["id"]) == ["t1"]


def test_missing_credentials_and_secrets_in_the_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("MS_GRAPH_TOKEN", raising=False)
    report = planner(tmp_path).sync(FakeApi(planner_api([[]])))
    assert "No Microsoft Graph credentials" in report.error
    with pytest.raises(DataSourceError, match="must not contain token"):
        MondayConnector(tmp_path, options={"boards": ["1"], "token": "x"})
    with pytest.raises(DataSourceError, match="needs 'options"):
        PlannerConnector(tmp_path, options={})
    with pytest.raises(DataSourceError, match="No Microsoft Planner snapshot"):
        planner(tmp_path).read()


def monday_api(pages):
    def answer(method, url, query):
        if "next_items_page" in query:
            cursor = json.loads(query.split("cursor: ")[1].split(")")[0])
            index = int(cursor)
            return {"data": {"next_items_page": {"cursor": str(index + 1) if index + 1 < len(pages) else None,
                                                 "items": pages[index]}}}
        return {"data": {"boards": [{"id": "42", "name": "Partner API", "items_page": {
            "cursor": "1" if len(pages) > 1 else None, "items": pages[0]}}]}}
    return answer


def item(iid, status="Working on it"):
    return {"id": iid, "name": f"Item {iid}", "updated_at": "2026-10-01T10:00:00Z", "group": {"title": "Build"},
            "column_values": [{"id": "status", "text": status}, {"id": "date", "text": "2026-11-30"}]}


def test_monday_sync_with_cursor_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("MONDAY_TOKEN", "monday-secret")
    conn = MondayConnector(tmp_path / "monday", options={"boards": {"42": {"project": "PRJ-DEMO-1001"}}})
    api = FakeApi(monday_api([[item("1"), item("2")], [item("3", "Done")]]))
    report = conn.sync(api)
    assert report.ok, report.error
    items = conn.read()["monday_items"].set_index("id")
    assert list(items.index) == ["1", "2", "3"] and items.loc["3", "col_status"] == "Done"
    assert items.loc["1", "project_number"] == "PRJ-DEMO-1001" and items.loc["1", "group"] == "Build"
    assert api.calls[0][2]["Authorization"] == "monday-secret" and api.calls[0][2]["API-Version"]
    assert "monday-secret" not in json.dumps(report.to_dict())


def test_monday_query_errors_and_bad_board_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("MONDAY_TOKEN", "t")
    conn = MondayConnector(tmp_path, options={"boards": ["42"]})
    report = conn.sync(FakeApi(lambda m, u, q: {"errors": [{"message": "Not authorized"}]}))
    assert "refused the query: Not authorized" in report.error
    assert "are numbers" in MondayConnector(tmp_path, options={"boards": ["4 OR 1"]}).sync(FakeApi(dict)).error


def test_team_tools_mapping_combines_the_tasks(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    (folder / "mappings").mkdir(parents=True)
    shutil.copy(ROOT / "data" / "mappings" / "team_tools.yaml", folder / "mappings")
    shutil.copy(ROOT / "data" / "DerKurator_Demo_Portfolio.json", folder)
    monkeypatch.setenv("MS_GRAPH_TOKEN", "x")
    monkeypatch.setenv("MONDAY_TOKEN", "y")
    from app.data_adapters.mapping import load_mapping

    mapping = load_mapping(folder / "mappings" / "team_tools.yaml")
    plan_id = next(iter(mapping.connectors["planner"].specs))
    assert mapping.connectors["planner"].sync(FakeApi(planner_api([[task("pl-1")]], plan_id))).ok
    assert mapping.connectors["monday"].sync(FakeApi(monday_api([[item("901", "Done")]]))).ok
    ds = MappedPortfolioAdapter(folder / "mappings" / "team_tools.yaml").load_dataset()
    tasks = ds.tasks.set_index("task_number")
    assert tasks.loc["pl-1", "project_number"] == "PRJ-DEMO-1002"
    assert tasks.loc["pl-1", "source_system"] == "Microsoft Planner"
    assert tasks.loc["901", "state"] == "Closed Complete" and tasks.loc["901", "source_system"] == "monday.com"
    assert (tasks["source_system"] == "ServiceNow").sum() > 10
