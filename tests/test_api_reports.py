"""HTTP API of the report viewer: a what-if or a proposal as the viewer shows it (tabs, key figures, charts as
Plotly figure JSON, the console's actions), in the caller's scope, and the exports of the whole analysis history."""
from __future__ import annotations

import json

from app.core import logs
from tests import test_api
from tests.test_api import ERP, PM, PMO, sign_in, what_if

source, client = test_api.source, test_api.client
CASEY = "Tomasz Wolski"  # PRJ-DEMO-1003 depends on PRJ-DEMO-1001 and PRJ-DEMO-1011, both outside her scope


def report(client, headers, run_id):
    response = client.get(f"/api/v1/steering/runs/{run_id}/report", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_run_report_has_the_viewer_content_and_the_submit_action(client):
    pm = sign_in(client, PM)
    run_id = what_if(client, pm, 10)["run_id"]
    view = report(client, pm, run_id)
    assert view["verdict"]["label"] == "CONDITIONAL GO" and view["verdict"]["key"] == "CONDITIONAL"
    assert [t["id"] for t in view["tabs"]][:4] == ["summary", "paper", "changes", "impact"]
    assert view["metrics"][0]["label"] == "Budget Δ" and view["changes"]["decisions"] == 1
    assert view["change_table"][0]["Project"].startswith(ERP) and view["impact"]["figures"][0]["data"]
    assert view["eyebrow"].startswith("Decision report · ") and PM in view["eyebrow"]
    assert view["status"] == {"label": "Draft · not submitted", "tone": "neutral", "icon": "edit_note"}
    actions = view["actions"]
    assert actions["primary"] == "submit" and actions["primary_enabled"] and actions["can_save"] and actions["held"]
    assert "cost_usd" not in json.dumps(view)


def test_submitted_and_outdated_runs_offer_no_action(client):
    pm = sign_in(client, PM)
    first = what_if(client, pm, 10)["run_id"]
    second = what_if(client, pm, 5)["run_id"]
    old = report(client, pm, first)
    assert old["status"]["label"] == "Outdated" and old["actions"]["primary"] is None
    assert old["actions"]["note"].startswith("Outdated step")
    proposal = client.post("/api/v1/proposals", headers=pm, json={"run_id": second}).json()["id"]
    done = report(client, pm, second)
    assert done["status"]["label"] == f"Proposal {proposal} · Waiting for approval"
    assert done["actions"]["primary"] is None and not done["actions"]["can_save"]
    assert client.get(f"/api/v1/steering/runs/{second}/export/markdown", headers=pm).status_code == 200


def test_report_of_another_user_is_not_found(client):
    run_id = what_if(client, sign_in(client, PM), 10)["run_id"]
    assert client.get(f"/api/v1/steering/runs/{run_id}/report", headers=sign_in(client, PMO)).status_code == 404


def test_dependency_tab_hides_projects_outside_the_scope(client):
    casey = sign_in(client, CASEY)
    run = client.post("/api/v1/steering/runs", headers=casey,
                      json={"request": "Which dependencies block PRJ-DEMO-1003 (downstream and upstream)?"}).json()
    view = report(client, casey, run["run_id"])
    assert view["dependencies"] is not None and view["dependencies"]["direct"]["figure"]["data"]
    text = json.dumps(view["dependencies"])
    assert "PRJ-DEMO-1003" in text and "PRJ-DEMO-1001" not in text and "PRJ-DEMO-1011" not in text


def test_proposal_report_follows_proposal_visibility(client):
    pm = sign_in(client, PM)
    pid = client.post("/api/v1/proposals", headers=pm, json={"run_id": what_if(client, pm, 10)["run_id"]}).json()["id"]
    view = client.get(f"/api/v1/proposals/{pid}/report", headers=sign_in(client, PMO))
    assert view.status_code == 200 and view.json()["verdict"]["label"] == "CONDITIONAL GO"
    assert "actions" not in view.json()
    assert client.get(f"/api/v1/proposals/{pid}/report", headers=sign_in(client, "Rowan Ellis")).status_code == 404


def test_history_exports_are_downloads_and_audited(client):
    pm = sign_in(client, PM)
    what_if(client, pm, 10)
    markdown = client.get("/api/v1/steering/chat/export/markdown", headers=pm)
    assert markdown.status_code == 200 and "attachment" in markdown.headers["content-disposition"]
    assert "Simulation Report" in markdown.text and ERP in markdown.text
    csv = client.get("/api/v1/steering/chat/export/csv", headers=pm)
    assert csv.headers["content-type"].startswith("text/csv") and "budget_cost_eur" in csv.text
    formats = [e.get("format") for e in logs.read_jsonl(logs.AUDIT_LOG)[0] if e.get("event") == "report_exported"]
    assert formats == ["markdown", "csv"]
    assert client.get("/api/v1/steering/chat/export/html", headers=pm).status_code == 422


def test_catalog_has_examples_and_the_chat_carries_status_badges(client):
    pm = sign_in(client, PM)
    catalog = client.get("/api/v1/steering/guided", headers=pm).json()
    assert catalog["examples"] and all("PRJ-DEMO-1001" not in e for e in catalog["examples"])
    what_if(client, pm, 10)
    chat = client.get("/api/v1/steering/chat", headers=pm).json()
    assert chat["messages"][-1]["status"]["badge"]["label"] == "Draft · not submitted"
