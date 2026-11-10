"""Write-back to ServiceNow (E5): off unless the mapping and the host both switch it on; a PATCH per plain field with
the source value, a conflict when ServiceNow changed meanwhile, references listed for manual transfer, never sent
twice, and the transfer status after the next sync. Fake Table API, no network."""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from app.data_adapters.base import DataSourceError
from app.data_adapters.mapping import load_mapping
from app.data_adapters.servicenow import ServiceNowConnector
from app.data_adapters.servicenow_writeback import read_log, transfer_status, write_back
from tests.test_servicenow import INSTANCE, RECORDED, SPM_MAPPING, FakeTableApi, _client

PID = "PRJ0010001"
SYS_ID = "p0000000000000000000000000000001"
CHANGES = [{"project_id": PID, "field": "budget_cost_eur", "old": 1_200_000.0, "new": 1_300_000.0},
           {"project_id": PID, "field": "status", "old": "Red", "new": "Yellow"},
           {"project_id": PID, "field": "project_manager", "old": "A", "new": "B"}]


class FakeRecordApi:
    """GET and PATCH of single pm_project records on a copy of the recorded data."""

    def __init__(self):
        self.records = {r["sys_id"]["value"]: {k: v["value"] if isinstance(v, dict) else v for k, v in r.items()}
                        for r in copy.deepcopy(RECORDED["pm_project"])}
        self.patches = []

    def __call__(self, method, url, headers, body):
        assert url.startswith(f"{INSTANCE}/api/now/table/pm_project/") and headers["Authorization"].startswith("Basic")
        sys_id = url.split("/")[-1].split("?")[0]
        if method == "PATCH":
            values = json.loads(body)
            self.patches.append((sys_id, values))
            self.records[sys_id].update(values)
        return 200, {}, json.dumps({"result": self.records[sys_id]}).encode()


@pytest.fixture
def mapping(tmp_path, monkeypatch):
    folder = tmp_path / "mappings"
    folder.mkdir()
    text = SPM_MAPPING.read_text(encoding="utf-8").replace("      page_size: 1000", "      page_size: 1000\n"
                                                                                  "      write_back: true")
    (folder / "servicenow_spm.yaml").write_text(text, encoding="utf-8")
    for key, value in {"SERVICENOW_INSTANCE": INSTANCE, "SERVICENOW_USER": "integration.user",
                       "SERVICENOW_PASSWORD": "not-a-real-secret", "SERVICENOW_WRITE_BACK": "true"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SERVICENOW_TOKEN", raising=False)
    loaded = load_mapping(folder / "servicenow_spm.yaml")
    connector(loaded).sync(_client(FakeTableApi(RECORDED)))
    return loaded


def connector(mapping) -> ServiceNowConnector:
    return next(c for c in mapping.connectors.values() if isinstance(c, ServiceNowConnector))


def test_plain_fields_are_patched_references_listed(mapping):
    api = FakeRecordApi()
    results = write_back("prop1", CHANGES, mapping, "PMO-ADMIN", api)
    assert [r["status"] for r in results] == ["sent", "sent", "manual"]
    assert api.patches == [(SYS_ID, {"budget_cost": 1_300_000.0}), (SYS_ID, {"overall_health": "yellow"})]
    assert "reference" in results[2]["reason"]
    log = read_log(connector(mapping))
    assert len(log) == 3 and "not-a-real-secret" not in json.dumps(log)
    again = write_back("prop1", CHANGES, mapping, "PMO-ADMIN", api)  # never twice
    assert [r["status"] for r in again[:2]] == ["already sent", "already sent"] and len(api.patches) == 2


def test_a_value_changed_in_servicenow_is_a_conflict(mapping):
    api = FakeRecordApi()
    api.records[SYS_ID]["budget_cost"] = "1250000.00"
    results = write_back("prop2", CHANGES[:1], mapping, "PMO-ADMIN", api)
    assert results[0]["status"] == "conflict" and "changed meanwhile" in results[0]["reason"]
    assert api.patches == []


def test_transfer_status_after_the_next_sync(mapping):
    api = FakeRecordApi()
    write_back("prop3", CHANGES[:2], mapping, "PMO-ADMIN", api)
    assert {r["check"] for r in transfer_status(connector(mapping)) if r["status"] == "sent"} == \
        {"waiting for the next sync"}
    tables = copy.deepcopy(RECORDED)
    project = next(r for r in tables["pm_project"] if r["sys_id"]["value"] == SYS_ID)
    project["budget_cost"]["value"] = "1300000.00"  # the budget arrived, the status was changed back by someone
    connector(mapping).sync(_client(FakeTableApi(tables)), full=True)
    checks = {r["column"]: r["check"] for r in transfer_status(connector(mapping))}
    assert checks == {"budget_cost": "confirmed", "overall_health": "differs"}


def test_off_unless_mapping_and_host_say_so(mapping, monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICENOW_WRITE_BACK", "false")
    with pytest.raises(DataSourceError, match="Write-back to ServiceNow is off"):
        write_back("p", CHANGES, mapping, "PMO-ADMIN", FakeRecordApi())
    folder = tmp_path / "plain"
    folder.mkdir()
    shutil.copy(SPM_MAPPING, folder / "servicenow_spm.yaml")
    monkeypatch.setenv("SERVICENOW_WRITE_BACK", "true")
    with pytest.raises(DataSourceError, match="write_back: true"):
        write_back("p", CHANGES, load_mapping(folder / "servicenow_spm.yaml"), "PMO-ADMIN", FakeRecordApi())
