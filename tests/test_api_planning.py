"""HTTP API of the Planning page: demands in the caller's scope, the PMO's assessment, weight changes, scenarios and
conversions, and the annual plan the Portfolio Board alone decides – on a temp copy of the demo portfolio."""
from __future__ import annotations

import json

import pytest

from app.core import logs
from app.core.auth import hash_password
from app.core.security import ROLE_ADMIN
from tests import test_api
from tests.test_api import DEMO_PASSWORD, DEPUTY, PM, PMO, sign_in

source, client = test_api.source, test_api.client
BOARD = "BOARD-CHAIR"
OWN, FOREIGN = "DMD-2027-001", "DMD-2027-003"  # Priya Raman's demand / Tomasz Wolski's
API = "/api/v1/planning"


def table(path, name) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))[name]


def demand_status(path, demand) -> str:
    return next(d for d in table(path, "Demands") if d["demand_number"] == demand)["demand_status"]


def audit_events() -> list[str]:
    path = logs.AUDIT_LOG
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def submitted_plan(client, pmo, **scenario) -> str:
    saved = client.post(f"{API}/scenarios", headers=pmo, json={"name": "Plan 2027", **scenario})
    assert saved.status_code == 201, saved.text
    submitted = client.post(f"{API}/scenarios/{saved.json()['scenario_id']}/submit", headers=pmo)
    assert submitted.status_code == 201, submitted.text
    return submitted.json()["proposal"]["id"]


class TestScope:
    def test_project_manager_sees_only_own_demands_without_rank(self, client):
        pm = sign_in(client, PM)
        listed = client.get(f"{API}/demands", headers=pm).json()
        assert listed["total"] and OWN in {d["Demand"] for d in listed["demands"]}
        assert FOREIGN not in {d["Demand"] for d in listed["demands"]}
        assert not listed["rank_shown"] and "Rank" not in listed["demands"][0]
        assert client.get(f"{API}/demands/{FOREIGN}", headers=pm).status_code == 404
        assert client.get(f"{API}/demands/{FOREIGN}/slip", headers=pm).status_code == 404
        detail = client.get(f"{API}/demands/{OWN}", headers=pm).json()
        assert detail["demand"]["demand_owner"] == PM and detail["position"] is None
        assert client.get(API, headers=pm).json()["sections"] == ["demands"]

    def test_project_manager_has_no_plan_or_pmo_tools(self, client):
        pm = sign_in(client, PM)
        for path in ("/analysis", "/board", "/scenarios", "/plans", "/reports", "/criteria", "/conversions",
                     "/scenario-options"):
            assert client.get(f"{API}{path}", headers=pm).status_code == 403, path
        assert client.post(f"{API}/scenarios/calculate", headers=pm, json={}).status_code == 403
        assert client.post(f"{API}/weights", headers=pm, json={"weights": {"Urgency": 20}}).status_code == 403
        assert client.post(f"{API}/demands/{OWN}/assessment", headers=pm,
                           json={"scores": {"Urgency": 3}}).status_code == 403

    def test_system_admin_sees_no_planning_data(self, client, monkeypatch):
        monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
            {"user_id": "SYSTEM-ADMIN", "role": ROLE_ADMIN, "password_hash": hash_password(DEMO_PASSWORD)}]}))
        admin = sign_in(client, "SYSTEM-ADMIN")
        for path in ("", "/demands", f"/demands/{OWN}", "/plans"):
            assert client.get(f"{API}{path}", headers=admin).status_code == 403, path

    def test_pmo_sees_every_demand_with_rank_and_plan_position(self, client):
        pmo = sign_in(client, PMO)
        listed = client.get(f"{API}/demands", headers=pmo).json()
        assert listed["total"] == 19 and listed["rank_shown"] and listed["demands"][0]["Rank"] == 1
        filtered = client.get(f"{API}/demands", headers=pmo, params={"status": ["Submitted"], "year": "2027"}).json()
        assert filtered["total"] and {d["Status"] for d in filtered["demands"]} == {"Submitted"}
        detail = client.get(f"{API}/demands/{OWN}", headers=pmo).json()
        assert detail["position"]["decision"] == "Deferred" and detail["annual_figures"]
        assert detail["assessment"]["score"] > 0 and detail["master_data"][0]["label"]
        slip = client.get(f"{API}/demands/{OWN}/slip", headers=pmo, params={"quarters": 2}).json()
        assert slip["quarters"] == 2 and isinstance(slip["moves"], list)
        assert client.get(f"{API}/demands/{OWN}/slip", headers=pmo, params={"quarters": 5}).status_code == 422

    def test_board_sees_the_plan_but_works_on_no_scenario(self, client):
        board = sign_in(client, BOARD)
        assert client.get(API, headers=board).json()["sections"][0] == "plan_approval"
        analysis = client.get(f"{API}/analysis", headers=board).json()
        assert analysis["verdict"]["label"] == "CONDITIONAL GO" and analysis["years"] == [2027, 2028, 2029]
        landing = client.get(f"{API}/board", headers=board).json()
        assert landing["waiting"] == 0 and landing["plan_in_force"] is None and landing["demands"] == 19
        assert client.post(f"{API}/scenarios/calculate", headers=board, json={}).status_code == 403
        assert client.post(f"{API}/reports", headers=board, json={"scenario": {}}).status_code == 403


class TestScenarios:
    def test_rank_order_and_value_optimal_with_the_capacity_chart(self, client):
        pmo = sign_in(client, PMO)
        rank = client.post(f"{API}/scenarios/calculate", headers=pmo, json={}).json()
        assert rank["verdict"]["label"] == "CONDITIONAL GO" and rank["optimizer"] is None
        assert set(rank["decision_counts"]) == {"In", "Forced-in", "Deferred", "Out"}
        assert sum(rank["decision_counts"].values()) == len(rank["items"]) and rank["budget"][0]["Status"]
        assert rank["capacity_chart"]["data"][0]["type"] == "heatmap" and rank["capacity_grid"]
        value = client.post(f"{API}/scenarios/calculate", headers=pmo, json={"name": "Value", "method": "value"}).json()
        assert value["optimizer"]["figures"] and value["input"]["method"] == "value"

    def test_choices_outside_the_inputs_are_refused(self, client):
        pmo = sign_in(client, PMO)
        options = client.get(f"{API}/scenario-options", headers=pmo).json()
        assert options["year_from"]["default"] == 2027 and OWN in {c["demand"] for c in options["candidates"]}
        for body in ({"budget_factor_pct": 7}, {"budget_factor_pct": -40}, {"forced_in": ["DMD-2027-018"]},
                     {"forced_in": [OWN], "excluded": [OWN]}, {"scope": "portfolio:Nowhere"}, {"method": "magic"},
                     {"year_from": 2040}):
            assert client.post(f"{API}/scenarios/calculate", headers=pmo, json=body).status_code == 422, body

    def test_forced_demand_and_comparison(self, client):
        pmo = sign_in(client, PMO)
        forced = client.post(f"{API}/scenarios/calculate", headers=pmo,
                             json={"name": "Forced", "forced_in": [OWN], "forced_reason": "Board commitment"}).json()
        assert next(i for i in forced["items"] if i["Demand"] == OWN)["Decision"] == "Forced-in"
        compared = client.post(f"{API}/scenarios/compare", headers=pmo,
                               json={"scenarios": [{"name": "A"}, {"name": "Cut", "budget_factor_pct": -10}]}).json()
        assert [r["Scenario"] for r in compared["comparison"]] == ["A", "Cut"]
        assert compared["scenarios"][1]["verdict"]["label"] == "NO-GO"
        assert client.post(f"{API}/scenarios/compare", headers=pmo, json={"scenarios": [{}]}).status_code == 422

    def test_reports(self, client):
        pmo = sign_in(client, PMO)
        assert "Base plan (today's data)" in client.get(f"{API}/reports", headers=pmo).json()["options"]
        multi = client.post(f"{API}/reports", headers=pmo, json={}).json()
        assert multi["multi_year"][-1]["Business unit"] == "Total"
        xlsx = client.post(f"{API}/reports", headers=pmo, json={"format": "xlsx", "compared": [{"name": "B"}]})
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
        html = client.post(f"{API}/reports", headers=sign_in(client, BOARD), json={"format": "html"})
        assert html.status_code == 200 and "text/html" in html.headers["content-type"]
        assert client.post(f"{API}/reports", headers=pmo, json={"option": "nothing"}).status_code == 404


class TestAnnualPlan:
    def test_pmo_submits_and_only_the_board_approves(self, client, source):
        pmo = sign_in(client, PMO)
        pid = submitted_plan(client, pmo)
        saved = client.get(f"{API}/scenarios", headers=pmo).json()["scenarios"]
        assert saved[0]["scenario_status"] == "Submitted" and not saved[0]["submittable"]
        # the plan is no PMO proposal: hidden from the PMO inbox, decided neither by its requester nor a second PMO
        for view in ({"status": "submitted"}, {"view": "pending"}):  # the PMO inbox; the requester still sees it as own
            inbox = client.get("/api/v1/proposals", headers=pmo, params=view).json()["proposals"]
            assert pid not in [p["id"] for p in inbox]
        assert client.post(f"{API}/plans/{pid}/approve", headers=pmo, json={}).status_code == 403
        deputy = sign_in(client, DEPUTY)
        assert client.post(f"{API}/plans/{pid}/approve", headers=deputy, json={}).status_code == 403
        assert client.get(f"{API}/plans/{pid}", headers=deputy).status_code == 404  # not the requester
        assert client.get(f"{API}/plans/{pid}", headers=sign_in(client, PM)).status_code == 403
        board = sign_in(client, BOARD)
        inbox = client.get(f"{API}/plans", headers=board, params={"status": "submitted"}).json()["plans"]
        assert [p["id"] for p in inbox] == [pid] and not inbox[0]["requires_override"]
        detail = client.get(f"{API}/plans/{pid}", headers=board).json()
        assert detail["paper"] and detail["change_set"][0]["Table"] == "Planning_Scenarios"
        assert client.post(f"{API}/plans/{pid}/reject", headers=board, json={"comment": " "}).status_code == 422
        decided = client.post(f"{API}/plans/{pid}/approve", headers=board, json={"comment": "Approved in session"})
        assert decided.status_code == 200 and decided.json()["kind"] == "success", decided.text
        assert demand_status(source, "DMD-2027-002") == "Approved" and demand_status(source, OWN) == "Planned"
        assert client.get(f"{API}/board", headers=board).json()["plan_in_force"]["approved_by"] == BOARD
        assert client.post(f"{API}/plans/{pid}/approve", headers=board, json={}).status_code == 409  # decided
        assert {"plan_scenario_saved", "plan_submitted", "plan_approved"} <= set(audit_events())
        # the approved demand becomes a project through a PMO proposal
        converted = client.post(f"{API}/conversions", headers=pmo, json={"demand": "DMD-2027-002"})
        assert converted.status_code == 201 and converted.json()["proposal"]["status"] == "submitted"

    def test_no_go_plan_needs_an_override_and_rejection_needs_a_reason(self, client, source):
        pmo = sign_in(client, PMO)
        pid = submitted_plan(client, pmo, budget_factor_pct=-10)
        assert client.post(f"{API}/scenarios/{'SCN-2027-001'}/submit", headers=pmo).status_code == 409  # not a draft
        board = sign_in(client, BOARD)
        before = demand_status(source, "DMD-2027-002")
        plain = client.post(f"{API}/plans/{pid}/approve", headers=board, json={})
        assert plain.status_code == 409 and demand_status(source, "DMD-2027-002") == before
        back = client.post(f"{API}/plans/{pid}/reject", headers=board, json={"comment": "Cut too deep"})
        assert back.status_code == 200 and back.json()["message"] == "Annual plan sent back to the PMO."
        assert client.get(f"{API}/plans/{pid}", headers=pmo).json()["status"] == "rejected"
        assert "plan_rejected" in audit_events()

    def test_unknown_plan_and_scenario_answer_not_found(self, client):
        assert client.get(f"{API}/plans/nothing", headers=sign_in(client, BOARD)).status_code == 404
        assert client.post(f"{API}/scenarios/SCN-1999-001/submit", headers=sign_in(client, PMO)).status_code == 404


class TestPmoTools:
    def test_assessment_is_written_and_checked_against_the_scale(self, client, source):
        pmo = sign_in(client, PMO)
        demand = "DMD-2027-017"
        form = client.get(f"{API}/demands/{demand}/assessment", headers=pmo).json()["criteria"]
        scores = {c["criterion"]: 3 for c in form}
        assert client.post(f"{API}/demands/{demand}/assessment", headers=pmo,
                           json={"scores": {**scores, "Urgency": 9}}).status_code == 422
        saved = client.post(f"{API}/demands/{demand}/assessment", headers=pmo, json={"scores": scores})
        assert saved.status_code == 200, saved.text
        assert demand_status(source, demand) == "Assessed" and "demand_assessed" in audit_events()
        mandatory = "DMD-2027-002"
        assert client.post(f"{API}/demands/{mandatory}/assessment", headers=pmo,
                           json={"scores": scores}).status_code == 409

    def test_weight_change_is_a_four_eyes_proposal(self, client, source):
        pmo = sign_in(client, PMO)
        same = client.post(f"{API}/weights", headers=pmo, json={"weights": {"Urgency": 10}})
        assert same.status_code == 200 and same.json()["proposal"] is None
        assert client.post(f"{API}/weights", headers=pmo, json={"weights": {"Urgency": -1}}).status_code == 422
        submitted = client.post(f"{API}/weights", headers=pmo, json={"weights": {"Urgency": 20}})
        assert submitted.status_code == 201, submitted.text
        pid = submitted.json()["proposal"]["id"]
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo, json={}).status_code == 409
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=sign_in(client, BOARD), json={}).status_code == 403
        approved = client.post(f"/api/v1/proposals/{pid}/approve", headers=sign_in(client, DEPUTY), json={})
        assert approved.status_code == 200 and approved.json()["kind"] == "success", approved.text
        weights = {r["criterion"]: r["weight"] for r in table(source, "Scoring_Model")}
        assert weights["Urgency"] == pytest.approx(20)

    def test_only_an_approved_demand_is_converted(self, client):
        pmo = sign_in(client, PMO)
        assert client.get(f"{API}/conversions", headers=pmo).json()["approved"] == []
        assert client.post(f"{API}/conversions", headers=pmo, json={"demand": OWN}).status_code == 409
        assert client.post(f"{API}/conversions", headers=pmo, json={"demand": "DMD-9999-999"}).status_code == 404


def test_every_planning_route_declares_its_answer(client):
    """The front ends generate their types from the contract: every answer is a named schema."""
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    planning = {(path, method): op for path, ops in paths.items() if path.startswith(API) for method, op in ops.items()}
    assert len(planning) >= 25
    for (path, method), op in planning.items():
        ok = next(op["responses"][code] for code in ("200", "201") if code in op["responses"])
        assert "$ref" in ok["content"]["application/json"]["schema"], (method, path)
