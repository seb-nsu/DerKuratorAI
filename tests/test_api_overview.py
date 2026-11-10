"""HTTP API of the Overview page: scope line, KPIs and charts, top risks, waiting proposals and the management reserve –
each in the caller's scope, on a temp copy of the demo portfolio."""
from __future__ import annotations

import json

from app.core.auth import hash_password
from app.core.security import ROLE_ADMIN
from tests import test_api

source, client = test_api.source, test_api.client
sign_in, what_if = test_api.sign_in, test_api.what_if
PM, PMO = test_api.PM, test_api.PMO
OWN = {"PRJ-DEMO-1002", "PRJ-DEMO-1008"}  # Priya Raman's projects
CASEY = "Tomasz Wolski"  # PRJ-DEMO-1003 waits for PRJ-DEMO-1001 and PRJ-DEMO-1011, both outside her scope


def _admin(client, monkeypatch) -> dict:
    monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
        {"user_id": "SYSTEM-ADMIN", "role": ROLE_ADMIN, "password_hash": hash_password(test_api.DEMO_PASSWORD)}]}))
    return sign_in(client, "SYSTEM-ADMIN")


class TestOverview:
    def test_pmo_sees_the_portfolio_with_kpis_and_chart_figures(self, client):
        page = client.get("/api/v1/overview", headers=sign_in(client, PMO)).json()
        assert page["scope_line"] == "Portfolio · scope 11 of 11 projects" and page["projects"] == 11
        kpis = page["kpis"]
        assert kpis["projects"] == 11 and kpis["total_budget_eur"] > 0 and kpis["data_quality_index"] is not None
        assert page["risk_value"]["figure"]["data"] and len(page["risk_value"]["points"]) <= 11
        teams = page["overloaded_teams"]
        assert (teams["figure"] is None) == (not teams["teams"])

    def test_project_manager_sees_only_own_projects_and_never_the_portfolio_size(self, client):
        page = client.get("/api/v1/overview", headers=sign_in(client, PM)).json()
        assert page["scope_line"] == "Your projects · 2 in your scope" and page["kpis"]["projects"] == 2
        assert {p["project_number"] for p in page["risk_value"]["points"]} <= OWN
        assert "11" not in page["scope_line"]
        assert page["own_view"] and not client.get("/api/v1/overview", headers=sign_in(client, PMO)).json()["own_view"]

    def test_board_and_system_admin_have_no_overview(self, client, monkeypatch):
        assert client.get("/api/v1/overview", headers=sign_in(client, "BOARD-CHAIR")).status_code == 403
        admin = _admin(client, monkeypatch)
        for path in ("/api/v1/overview", "/api/v1/overview/top-risks", "/api/v1/overview/reserve"):
            assert client.get(path, headers=admin).status_code == 403


class TestTopRisks:
    def test_predecessors_outside_the_scope_stay_anonymous(self, client):
        risks = client.get("/api/v1/overview/top-risks", headers=sign_in(client, CASEY)).json()
        assert risks["available"]
        text = json.dumps(risks)
        assert "PRJ-DEMO-1001" not in text and "PRJ-DEMO-1011" not in text
        blocked = next(r for r in risks["risks"] if r["project"] == "PRJ-DEMO-1003")
        assert "projects outside your scope" in blocked["detail"] and blocked["can_open"]

    def test_pmo_sees_the_blockers_by_id(self, client):
        risks = client.get("/api/v1/overview/top-risks", headers=sign_in(client, PMO)).json()["risks"]
        assert risks and any("PRJ-DEMO-1001" in r["detail"] for r in risks if r["project"] == "PRJ-DEMO-1003")

    def test_graph_failure_answers_a_neutral_message(self, client, monkeypatch):
        from app import services

        monkeypatch.setattr(services, "top_portfolio_risks", lambda allowed: 1 / 0)
        risks = client.get("/api/v1/overview/top-risks", headers=sign_in(client, PMO)).json()
        assert risks == {"available": False, "message": risks["message"], "risks": []}
        assert "ZeroDivision" not in risks["message"]


class TestOpenItemsAndReserve:
    def test_waiting_proposals_are_the_callers_own(self, client):
        pm = sign_in(client, PM)
        pid = client.post("/api/v1/proposals", headers=pm, json={"run_id": what_if(client, pm, 10)["run_id"]}).json()["id"]
        waiting = client.get("/api/v1/overview/open-items", headers=pm).json()["proposals"]
        assert [p["id"] for p in waiting] == [pid] and waiting[0]["deadline"] == "on time" and waiting[0]["due"]
        assert waiting[0]["deadline_label"] and len(waiting[0]["due_date"]) == 10
        other = client.get("/api/v1/overview/open-items", headers=sign_in(client, "Rowan Ellis")).json()
        assert other["proposals"] == []

    def test_management_reserve_is_the_pmos_only(self, client):
        reserve = client.get("/api/v1/overview/reserve", headers=sign_in(client, PMO)).json()
        assert reserve["floor_eur"] > 0 and isinstance(reserve["below_floor"], bool)
        assert (reserve["figure"] is None) == (not reserve["skills"])
        assert client.get("/api/v1/overview/reserve", headers=sign_in(client, PM)).status_code == 403


def test_every_json_route_of_these_pages_declares_its_answer(client):
    """The React front end generates its types from the contract: no route of these pages answers a bare object."""
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    for path in ("/api/v1/overview", "/api/v1/overview/top-risks", "/api/v1/overview/open-items",
                 "/api/v1/overview/reserve", "/api/v1/data-quality", "/api/v1/data-quality/interpretation",
                 "/api/v1/assist/pages", "/api/v1/assist/ask"):
        operation = paths[path].get("get") or paths[path]["post"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in schema or "oneOf" in schema, path
