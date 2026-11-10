"""HTTP API report exports: SteerCo Pack, Markdown and CSV of a what-if or a proposal – audited, in the caller's
scope, never for a run or proposal the caller may not see."""
from __future__ import annotations

from app.core import logs
from tests import test_api

source, client = test_api.source, test_api.client
sign_in, what_if = test_api.sign_in, test_api.what_if
PM, PMO, ERP = test_api.PM, test_api.PMO, test_api.ERP
CASEY = "Tomasz Wolski"  # PRJ-DEMO-1003 depends on PRJ-DEMO-1001 and PRJ-DEMO-1011, both outside her scope


def test_three_exports_of_a_run_are_downloads_and_audited(client):
    pm = sign_in(client, PM)
    run_id = what_if(client, pm, 10)["run_id"]
    pack = client.get(f"/api/v1/steering/runs/{run_id}/export/steerco-pack", headers=pm)
    assert pack.status_code == 200 and pack.headers["content-type"].startswith("text/html")
    assert "attachment" in pack.headers["content-disposition"] and "CONDITIONAL GO" in pack.text
    markdown = client.get(f"/api/v1/steering/runs/{run_id}/export/markdown", headers=pm)
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "## 1. Request" in markdown.text and f"Exported by:** {PM}" in markdown.text and ERP in markdown.text
    csv = client.get(f"/api/v1/steering/runs/{run_id}/export/csv", headers=pm)
    assert csv.headers["content-type"].startswith("text/csv") and "budget_cost_eur" in csv.text
    events = [e["event"] for e in logs.read_jsonl(logs.AUDIT_LOG)[0] if e.get("event", "").endswith("exported")]
    assert events == ["steerco_pack_exported", "report_exported", "report_exported"]
    assert client.get(f"/api/v1/steering/runs/{run_id}/export/pdf", headers=pm).status_code == 422


def test_run_of_another_user_cannot_be_exported(client):
    run_id = what_if(client, sign_in(client, PM), 10)["run_id"]
    assert client.get(f"/api/v1/steering/runs/{run_id}/export/markdown", headers=sign_in(client, PMO)).status_code == 404


def test_proposal_exports_follow_proposal_visibility(client):
    pm = sign_in(client, PM)
    pid = client.post("/api/v1/proposals", headers=pm, json={"run_id": what_if(client, pm, 10)["run_id"]}).json()["id"]
    pack = client.get(f"/api/v1/proposals/{pid}/export/steerco-pack", headers=sign_in(client, PMO))
    assert pack.status_code == 200 and f"prepared by {PM}" in pack.text and pid in pack.text
    other = sign_in(client, "Rowan Ellis")
    assert client.get(f"/api/v1/proposals/{pid}/export/csv", headers=other).status_code == 404


def test_markdown_names_no_project_outside_the_scope(client):
    casey = sign_in(client, CASEY)
    run = client.post("/api/v1/steering/runs", headers=casey,
                      json={"request": "Which dependencies block PRJ-DEMO-1003 (downstream and upstream)?"}).json()
    markdown = client.get(f"/api/v1/steering/runs/{run['run_id']}/export/markdown", headers=casey).text
    assert "PRJ-DEMO-1003" in markdown
    assert "PRJ-DEMO-1001" not in markdown and "PRJ-DEMO-1011" not in markdown
