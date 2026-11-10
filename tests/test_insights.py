"""Decision insights over the HTTP API and on the Streamlit page: PMO only; status check, forecast, reference class,
decision review, audit pack (audited) and the ServiceNow transfer that is refused while write-back is off."""
from __future__ import annotations

import io
import zipfile

from app import approval_flow
from app.core import logs, proposals
from app.core.security import ROLE_PM, ROLE_PMO
from tests import test_api

source, client = test_api.source, test_api.client
PM, PMO, ERP = test_api.PM, test_api.PMO, test_api.ERP


def approved(source) -> str:
    old = test_api.budget(source)
    entry = proposals.submit_proposal(PM, ROLE_PM, "Raise the ERP budget", "paper", "GO",
                                      [{"project_id": ERP, "field": "budget_cost_eur", "old": old, "new": old + 5_000}])
    assert approval_flow.approve_proposal(entry["id"], PMO, ROLE_PMO, comment="ok").ok
    return entry["id"]


def test_pmo_reads_every_insight(client):
    headers = test_api.sign_in(client, PMO)
    checks = client.get("/api/v1/insights/status-check", headers=headers).json()
    assert checks["checks"] and {"mismatch", "calculated", "reasons"} <= set(checks["checks"][0])
    forecast = client.get("/api/v1/insights/forecast", headers=headers).json()
    assert forecast["summary"]["runs"] > 0 and forecast["projects"]
    reference = client.get(f"/api/v1/insights/reference/{ERP}", headers=headers).json()
    assert reference["subject"] == ERP and "cost_ratio" in reference
    assert client.get("/api/v1/insights/reference/NOPE", headers=headers).status_code == 404
    assert client.get("/api/v1/insights/decision-review", headers=headers).json()["decisions"] == []


def test_project_manager_is_refused(client):
    headers = test_api.sign_in(client, PM)
    for path in ("status-check", "forecast", "decision-review", f"reference/{ERP}", "servicenow-transfers"):
        assert client.get(f"/api/v1/insights/{path}", headers=headers).status_code == 403


def test_audit_pack_download_and_review(client, source):
    pid = approved(source)
    headers = test_api.sign_in(client, PMO)
    review = client.get("/api/v1/insights/decision-review", headers=headers).json()
    assert [d["proposal_id"] for d in review["decisions"]] == [pid]
    response = client.get(f"/api/v1/insights/audit-pack/{pid}", headers=headers)
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "manifest.json" in zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    events = [r["event"] for r in logs.read_jsonl(logs.AUDIT_LOG)[0]]
    assert "audit_pack_built" in events
    assert client.get("/api/v1/insights/audit-pack/unknown", headers=headers).status_code == 404


def test_transfer_refused_without_a_servicenow_source(client, source):
    pid = approved(source)
    headers = test_api.sign_in(client, PMO)
    response = client.post(f"/api/v1/insights/servicenow-transfer/{pid}", headers=headers)
    assert response.status_code == 409 and "ServiceNow" in response.json()["detail"]
    assert client.post("/api/v1/insights/servicenow-transfer/unknown", headers=headers).status_code == 404
    assert client.get("/api/v1/insights/servicenow-transfers", headers=headers).json() == []
