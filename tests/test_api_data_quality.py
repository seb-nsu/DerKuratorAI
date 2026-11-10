"""HTTP API of the Data quality page: Data Steward figures, quality bars, findings with filters and the executive
interpretation – each in the caller's scope."""
from __future__ import annotations

from tests import test_api

source, client = test_api.source, test_api.client
sign_in = test_api.sign_in
PM, PMO = test_api.PM, test_api.PMO
OWN = {"PRJ-DEMO-1002", "PRJ-DEMO-1008", "*"}  # Priya Raman's projects; "*" = a finding about the source as a whole


def test_pmo_sees_every_finding_and_the_quality_bars(client):
    page = client.get("/api/v1/data-quality", headers=sign_in(client, PMO)).json()
    summary = page["summary"]
    assert summary["projects_checked"] == 11 and page["findings_total"] == page["findings_shown"] > 0
    assert [b["label"] for b in page["bars"]] == ["Released for simulation", "Required-field completeness",
                                                  "Projects without findings"]
    assert set(page["findings"][0]) == {"severity", "project", "rule", "field", "finding"}
    assert not any(f["rule"].startswith("DQ-Profile:") for f in page["findings"])  # internal rule ids stay hidden


def test_filters_by_severity_and_project(client):
    headers = sign_in(client, PMO)
    errors = client.get("/api/v1/data-quality", headers=headers, params={"severity": "Errors"}).json()
    assert errors["findings"] and {f["severity"] for f in errors["findings"]} == {"ERROR"}
    project = errors["projects"][0]
    one = client.get("/api/v1/data-quality", headers=headers, params={"project": project}).json()
    assert {f["project"] for f in one["findings"]} == {project}
    assert client.get("/api/v1/data-quality", headers=headers, params={"severity": "Bad"}).status_code == 422


def test_project_manager_sees_only_findings_of_own_projects(client):
    page = client.get("/api/v1/data-quality", headers=sign_in(client, PM)).json()
    assert page["summary"]["projects_checked"] == 2
    assert {f["project"] for f in page["findings"]} <= OWN and set(page["projects"]) <= OWN
    assert set(page["summary"]["blocked_projects"]) <= OWN


def test_interpretation_is_rule_based_offline_and_names_no_model(client):
    answer = client.get("/api/v1/data-quality/interpretation", headers=sign_in(client, PMO)).json()
    assert answer["source"] == "rules" and not answer["ai_generated"] and answer["model"] is None and answer["text"]
    assert "cost" not in answer


def test_board_has_no_data_quality_page(client):
    assert client.get("/api/v1/data-quality", headers=sign_in(client, "BOARD-CHAIR")).status_code == 403


def test_dependency_findings_never_name_a_project_outside_the_scope(client):
    """PRJ-DEMO-1003 waits for PRJ-DEMO-1001 and PRJ-DEMO-1011 (both 'At Risk', neither in Tomasz Wolski's scope)."""
    page = client.get("/api/v1/data-quality", headers=sign_in(client, "Tomasz Wolski")).json()
    text = str(page)
    assert "PRJ-DEMO-1001" not in text and "PRJ-DEMO-1011" not in text
    hidden = [f for f in page["findings"] if f["rule"] == "Dependency Risk" and f["project"] == "PRJ-DEMO-1003"]
    assert len(hidden) == 1 and "outside your scope" in hidden[0]["finding"]
    pmo = client.get("/api/v1/data-quality", headers=sign_in(client, PMO)).json()
    assert "Hard dependency on PRJ-DEMO-1001 is 'At Risk'" in [f["finding"] for f in pmo["findings"]]
