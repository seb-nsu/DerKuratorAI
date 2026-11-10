"""HTTP API, Projects page: the project detail as the page shows it, the master-data form and saving it (direct fields
written, governed fields as a proposal), and reporting an actual benefit – on a temp copy of the demo portfolio."""
from __future__ import annotations

import json

from app.core import clock, logs
from tests import test_api
from tests.test_api import ERP, OTHER, PM, PMO, sign_in

source, client = test_api.source, test_api.client


def _project(path, pid=ERP) -> dict:
    return next(p for p in json.loads(path.read_text(encoding="utf-8"))["Projects"] if p["project_number"] == pid)


def _set(path, pid, **fields) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    next(p for p in data["Projects"] if p["project_number"] == pid).update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def _save(client, headers, section, values, pid=ERP):
    return client.post(f"/api/v1/projects/{pid}/master-data", headers=headers,
                       json={"section": section, "values": values})


class TestDetail:
    def test_detail_answers_every_tab_of_the_page(self, client):
        detail = client.get(f"/api/v1/projects/{ERP}", headers=sign_in(client, PM)).json()
        assert detail["access"] == "edit" and detail["tabs"][-1] == "Edit"
        assert detail["header"]["budget"] == 1850000 and detail["header"]["data_quality"] in ("ok", "warnings", "blocked")
        assert {r["Field"] for r in detail["master_data"]["governed"]} >= {"Approved budget (€)", "Planned end"}
        assert detail["status_reports"] and detail["costs"]["curve"]
        figure = detail["costs"]["figure"]  # the page's Plotly chart, built by the same function
        assert figure and {t["name"] for t in figure["data"]} == {"Planned (cumulative)", "Actual (cumulative)"}
        assert detail["costs"]["baselines"] and detail["benefits"]["tranches"]
        assert detail["benefits"]["can_report"] and detail["benefits"]["plan_options"][0] == ""
        assert detail["benefits"]["default_month"] == clock.today().replace(day=1).isoformat()
        assert detail["risks"][0]["exposure_eur"] >= detail["risks"][-1]["exposure_eur"]
        assert detail["total_risk_exposure_eur"] > 0 and detail["deadlines"]

    def test_pm_never_sees_a_project_outside_the_scope(self, client, source):
        """ERP feeds a project of another PM: the dependency appears as one neutral row, without its id or name."""
        detail = client.get(f"/api/v1/projects/{ERP}", headers=sign_in(client, PM))
        outgoing = detail.json()["dependencies"]["outgoing"]
        assert [d["project"] for d in outgoing] == ["Restricted project"]
        hidden = [p for p in json.loads(source.read_text(encoding="utf-8"))["Projects"] if p["project_manager"] != PM]
        for p in hidden:
            assert p["project_number"] not in detail.text and p["project_name"] not in detail.text

    def test_every_project_answers_its_contract(self, client):
        """Rows with gaps (data-quality cases, other currencies) still answer the declared response model."""
        pmo = sign_in(client, PMO)
        for p in client.get("/api/v1/projects", headers=pmo).json()["projects"]:
            for path in ("", "/edit-form"):
                response = client.get(f"/api/v1/projects/{p['project_number']}{path}", headers=pmo)
                assert response.status_code == 200, (p["project_number"], response.text[:300])
        paths = client.get("/api/v1/openapi.json").json()["paths"]
        schema = paths["/api/v1/projects/{project_id}"]["get"]["responses"]["200"]["content"]["application/json"]
        assert schema["schema"]["$ref"].endswith("/ProjectDetail")

    def test_findings_carry_the_labels_the_page_shows(self, client):
        pmo = sign_in(client, PMO)
        findings = [f for p in client.get("/api/v1/projects", headers=pmo).json()["projects"]
                    for f in client.get(f"/api/v1/projects/{p['project_number']}", headers=pmo)
                    .json()["data_quality_findings"]]
        assert findings and all(f["rule_label"] for f in findings)
        assert all(f["field_label"] and "_" not in f["field_label"] for f in findings if f["field"])

    def test_pmo_reads_without_the_edit_tab(self, client):
        detail = client.get(f"/api/v1/projects/{ERP}", headers=sign_in(client, PMO)).json()
        assert detail["access"] == "read" and "Edit" not in detail["tabs"] and detail["benefits"]["can_report"]


class TestEditForm:
    def test_owner_edits_but_an_existing_budget_stays_locked(self, client):
        form = client.get(f"/api/v1/projects/{ERP}/edit-form", headers=sign_in(client, PM)).json()
        fields = {f["field"]: f for section in form["sections"].values() for f in section}
        assert form["access"] == "edit" and fields["project_name"]["editable"]
        assert not fields["budget_cost_eur"]["editable"] and fields["budget_cost_eur"]["fill_only_while_empty"]
        assert fields["risk_score"]["min"] == 1 and fields["risk_score"]["max"] == 5
        assert fields["planned_end_date"]["value"] == "2027-03-31" and fields["status"]["options"][:3] == ["Green", "Yellow", "Red"]
        assert [f["field"] for f in form["sections"]["direct"]][-1] == "business_case"

    def test_pmo_form_is_read_only(self, client):
        form = client.get(f"/api/v1/projects/{ERP}/edit-form", headers=sign_in(client, PMO)).json()
        assert form["access"] == "read" and not any(f["editable"] for s in form["sections"].values() for f in s)

    def test_value_outside_the_range_is_flagged_and_never_written_back(self, client, source):
        _set(source, ERP, risk_score=9)
        pm = sign_in(client, PM)
        form = client.get(f"/api/v1/projects/{ERP}/edit-form", headers=pm).json()
        risk = next(f for f in form["sections"]["direct"] if f["field"] == "risk_score")
        assert risk["value"] == 5 and risk["stored_out_of_range"] == 9
        saved = _save(client, pm, "direct", {"risk_score": 5, "project_sponsor": "New Sponsor"})
        assert saved.status_code == 200 and saved.json()["applied"] == 1, saved.text
        assert _project(source)["risk_score"] == 9


class TestSave:
    def test_direct_fields_are_written_and_audited(self, client, source):
        response = _save(client, sign_in(client, PM), "direct", {"project_sponsor": "Ingrid Solberg"})
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "success" and response.json()["snapshot_id"]
        assert _project(source)["project_sponsor"] == "Ingrid Solberg"
        events = [json.loads(line)["event"] for line in logs.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
        assert "project_edited" in events and logs.BUSINESS_AUDIT_LOG.exists()
        # the next read sees the saved value
        detail = client.get(f"/api/v1/projects/{ERP}", headers=sign_in(client, PM)).json()
        assert detail["project"]["project_sponsor"] == "Ingrid Solberg"

    def test_governed_fields_become_a_proposal_and_nothing_is_written(self, client, source):
        pm = sign_in(client, PM)
        response = _save(client, pm, "governed", {"planned_end_date": "2027-06-30"})
        assert response.status_code == 200, response.text
        pid = response.json()["proposal_id"]
        assert pid and "submitted to the PMO" in response.json()["message"]
        assert _project(source)["planned_end_date"] == "2027-03-31"
        proposal = client.get(f"/api/v1/proposals/{pid}", headers=pm).json()
        assert proposal["changes"][0]["field"] == "planned_end_date" and proposal["can_withdraw"]

    def test_no_change_is_reported_as_info(self, client):
        response = _save(client, sign_in(client, PM), "direct", {"project_name": "General Ledger Consolidation"})
        assert response.status_code == 200 and response.json()["kind"] == "info"

    def test_refusals(self, client, source):
        pm = sign_in(client, PM)
        before = source.read_text(encoding="utf-8")
        # a governed field cannot be saved directly, a field of the other section is refused
        assert _save(client, pm, "direct", {"planned_end_date": "2027-06-30"}).status_code == 422
        assert _save(client, pm, "direct", {"project_number": "X"}).status_code == 422
        # an existing budget is changed only through a budget scenario
        assert _save(client, pm, "governed", {"budget_cost_eur": 1}).status_code == 422
        # ranges, options, dates
        assert _save(client, pm, "direct", {"percent_complete": 120}).status_code == 422
        assert _save(client, pm, "direct", {"status": "Purple"}).status_code == 422
        assert _save(client, pm, "direct", {"planned_start_date": "1999-12-31"}).status_code == 422
        assert _save(client, pm, "direct", {"planned_start_date": "not a date"}).status_code == 422
        # a required field must not be cleared
        assert _save(client, pm, "direct", {"project_name": ""}).status_code == 422
        # the PMO reads only; another PM does not even learn the project exists
        assert _save(client, sign_in(client, PMO), "direct", {"project_sponsor": "X"}).status_code == 403
        assert _save(client, pm, "direct", {"project_sponsor": "X"}, pid=OTHER).status_code == 404
        assert source.read_text(encoding="utf-8") == before


class TestBenefits:
    def test_owner_reports_an_actual_benefit(self, client, source):
        pm = sign_in(client, PM)
        response = client.post(f"/api/v1/projects/{ERP}/benefit-actuals", headers=pm,
                               json={"amount": 25000, "plan": "BPL-DEMO-0201", "month": "2026-09-17",
                                     "comment": "first savings"})
        assert response.status_code == 201, response.text
        assert response.json()["benefit_actual_number"] == f"BAC-{ERP}-001"
        row = json.loads(source.read_text(encoding="utf-8"))["Benefit_Actuals"][0]
        assert row["month"] == "2026-09-01" and row["reported_by"] == PM
        detail = client.get(f"/api/v1/projects/{ERP}", headers=pm).json()
        assert detail["benefits"]["realisation"]["early"] == 25000

    def test_benefit_refusals(self, client):
        pm = sign_in(client, PM)
        url = f"/api/v1/projects/{ERP}/benefit-actuals"
        assert client.post(url, headers=pm, json={"amount": 0}).status_code == 422
        assert client.post(url, headers=pm, json={"amount": 1, "plan": "BPL-OF-ANOTHER"}).status_code == 422
        assert client.post(f"/api/v1/projects/{OTHER}/benefit-actuals", headers=pm, json={"amount": 1}).status_code == 404
        assert client.post(url, headers=sign_in(client, "BOARD-CHAIR"), json={"amount": 1}).status_code == 403
