"""HTTP API of the page assistant: a change question is calculated by the rules, any other question is answered from
facts the server builds from the caller's scope (offline: a rule-based digest). Questions are audited without text."""
from __future__ import annotations

import json

from app.api import runs
from app.core import logs
from app.core.auth import hash_password
from app.core.security import ROLE_ADMIN
from tests import test_api

source, client = test_api.source, test_api.client
sign_in, what_if = test_api.sign_in, test_api.what_if
PM, PMO, BOARD = test_api.PM, test_api.PMO, "BOARD-CHAIR"
ERP, OTHER = test_api.ERP, test_api.OTHER
FOREIGN_DEMAND = "DMD-2027-006"  # not in Priya Raman's scope
OWN_DEMAND = "DMD-2027-001"


def ask(client, headers, page, question, **ids):
    return client.post("/api/v1/assist/ask", headers=headers, json={"page": page, "question": question, **ids})


class TestPages:
    def test_each_role_gets_the_boxes_of_its_pages(self, client):
        pm = client.get("/api/v1/assist/pages", headers=sign_in(client, PM)).json()
        assert [p["page"] for p in pm["pages"]] == ["overview", "data_quality", "report", "project", "compare",
                                                    "demands", "demand"]
        assert not pm["ai_available"] and "rule-based digest" in pm["hint"]
        board = client.get("/api/v1/assist/pages", headers=sign_in(client, BOARD)).json()
        assert [p["page"] for p in board["pages"]] == ["plan", "demands", "demand"]
        pmo = client.get("/api/v1/assist/pages", headers=sign_in(client, PMO)).json()
        assert "scenario" in [p["page"] for p in pmo["pages"]]
        assert ask(client, sign_in(client, PM), "plan", "Where is the plan tightest?").status_code == 403
        assert ask(client, sign_in(client, BOARD), "overview", "Brief me").status_code == 403
        assert ask(client, sign_in(client, PM), "scenario", "Why?", scenarios=[{}]).status_code == 403

    def test_system_admin_has_no_assistant(self, client, monkeypatch):
        monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
            {"user_id": "SYSTEM-ADMIN", "role": ROLE_ADMIN, "password_hash": hash_password(test_api.DEMO_PASSWORD)}]}))
        admin = sign_in(client, "SYSTEM-ADMIN")
        assert client.get("/api/v1/assist/pages", headers=admin).status_code == 403
        assert ask(client, admin, "overview", "Brief me").status_code == 403


class TestAnswers:
    def test_plain_question_gets_a_digest_of_the_callers_facts_only(self, client):
        response = ask(client, sign_in(client, PM), "overview", "Which projects need attention first?")
        answer = response.json()
        assert response.status_code == 200 and answer["kind"] == "answer" and answer["mode"] == "rules"
        assert not answer["ai_generated"] and answer["note"].startswith("Rule-based")
        assert ERP in answer["facts"] and "PRJ-DEMO-1001" not in answer["facts"]
        assert "cost" not in json.dumps(answer).lower()

    def test_question_is_audited_without_its_text(self, client):
        question = "Which data quality rule hits Priya Raman hardest?"
        ask(client, sign_in(client, PM), "data_quality", question)
        events = [e for e in logs.read_jsonl(logs.AUDIT_LOG)[0] if e.get("event") == "assistant_question"]
        assert events and events[-1]["chars"] == len(question) and events[-1]["page"] == "data_quality"
        assert question not in json.dumps(events)

    def test_change_question_is_calculated_by_the_rules_and_points_to_the_console(self, client):
        result = ask(client, sign_in(client, PM), "overview", f"Cut the budget of {ERP} by 10 %").json()
        assert result["kind"] == "calculation" and result["verdict"]["label"] == "CONDITIONAL GO"
        assert result["verdict"]["lead_reason"] and result["verdict"]["reasons"][0] in result["verdict"]["lead_reason"]
        assert result["changes"] and result["change_rows"][0]["project"].startswith(ERP)
        assert result["next_step"]["target"] == "console" and result["next_step"]["request"]

    def test_board_has_no_console_to_continue_in(self, client):
        result = ask(client, sign_in(client, BOARD), "plan", f"Cut the budget of {ERP} by 10 %").json()
        assert result["kind"] == "calculation" and result["next_step"]["target"] is None

    def test_planning_question_pre_fills_the_scenario_inputs(self, client):
        result = ask(client, sign_in(client, PMO), "overview", "What drops out in 2027 at -10 %?").json()
        step = result["next_step"]
        assert step["target"] == "planning" and step["inputs"]["budget_factor_pct"] == -10


class TestScopedSubjects:
    def test_report_of_a_run_is_answered_and_a_foreign_run_is_hidden(self, client):
        pm = sign_in(client, PM)
        run = what_if(client, pm, 10)
        answer = ask(client, pm, "report", "Explain the verdict in two sentences", run_id=run["run_id"]).json()
        assert answer["kind"] == "answer" and "CONDITIONAL GO" in answer["facts"]
        assert ask(client, sign_in(client, PMO), "report", "Explain", run_id=run["run_id"]).status_code == 404
        assert ask(client, pm, "report", "Explain").status_code == 422

    def test_proposal_of_another_project_manager_is_hidden(self, client):
        pm = sign_in(client, PM)
        pid = client.post("/api/v1/proposals", headers=pm, json={"run_id": what_if(client, pm, 10)["run_id"]}).json()["id"]
        assert ask(client, pm, "report", "Explain the verdict", proposal_id=pid).status_code == 200
        assert ask(client, sign_in(client, PMO), "report", "Explain the verdict", proposal_id=pid).status_code == 200
        other = sign_in(client, "Rowan Ellis")
        assert ask(client, other, "report", "Explain the verdict", proposal_id=pid).status_code == 404

    def test_demand_outside_the_scope_answers_not_found(self, client):
        pm = sign_in(client, PM)
        assert ask(client, pm, "demand", "What does it wait for?", demand_id=FOREIGN_DEMAND).status_code == 404
        own = ask(client, pm, "demand", "What does this demand wait for?", demand_id=OWN_DEMAND).json()
        assert own["kind"] == "answer" and f"Demand {OWN_DEMAND}" in own["facts"]
        assert "Decision in the base plan" not in own["facts"]  # a Project Manager sees no rank or envelope

    def test_project_outside_the_scope_answers_not_found(self, client):
        pm = sign_in(client, PM)
        assert ask(client, pm, "project", "Why is this project at risk?", project_id=OTHER).status_code == 404
        own = ask(client, pm, "project", "What is due in the next 90 days?", project_id=ERP).json()
        assert own["kind"] == "answer" and f"Project {ERP}" in own["facts"] and OTHER not in own["facts"]

    def test_change_question_on_a_project_is_about_that_project(self, client):
        result = ask(client, sign_in(client, PM), "project", "What if the budget is cut by 10 %?", project_id=ERP).json()
        assert result["kind"] == "calculation" and result["changes"]
        assert {c["project_id"] for c in result["changes"]} == {ERP}

    def test_report_of_the_history_is_answered_after_the_run_expired(self, client):
        pm = sign_in(client, PM)
        run = what_if(client, pm, 10)
        runs.clear()  # e.g. a restart: the history still holds the analysis
        answer = ask(client, pm, "report", "Explain the verdict", run_id=run["run_id"])
        assert answer.status_code == 200 and "CONDITIONAL GO" in answer.json()["facts"]

    def test_comparison_of_two_own_what_ifs(self, client):
        pm = sign_in(client, PM)
        ids = []
        for pct, name in ((10, "A – soft cut"), (30, "B – hard cut")):
            run = what_if(client, pm, pct)
            ids.append(client.post(f"/api/v1/steering/runs/{run['run_id']}/save", headers=pm,
                                   json={"name": name}).json()["id"])
        answer = ask(client, pm, "compare", "Where do A and B differ most?", a=ids[0], b=ids[1]).json()
        assert answer["kind"] == "answer" and "A – soft cut" in answer["facts"] and "B – hard cut" in answer["facts"]
        assert ask(client, pm, "compare", "Where?", a=ids[0], b=ids[0]).status_code == 422
        assert ask(client, pm, "compare", "Where?", a=ids[0], b="missing").status_code == 404

    def test_demands_follow_the_table_filters_and_the_scope(self, client):
        pm = sign_in(client, PM)
        answer = ask(client, pm, "demands", "Which demands are not assessed yet?").json()
        assert answer["kind"] == "answer" and OWN_DEMAND in answer["facts"] and FOREIGN_DEMAND not in answer["facts"]
        filtered = ask(client, pm, "demands", "Which demands?", status=["no such status"]).json()
        assert OWN_DEMAND not in filtered["facts"]

    def test_plan_scenarios_are_recalculated_from_the_page_inputs(self, client):
        pmo = sign_in(client, PMO)
        scenarios = [{"name": "Base"}, {"name": "Tight", "budget_factor_pct": -10}]
        answer = ask(client, pmo, "scenario", "Why does the plan get this verdict?", scenarios=scenarios,
                     scenario="Tight").json()
        assert answer["kind"] == "answer" and "Scenario “Tight”" in answer["facts"]
        assert "Comparison of the calculated scenarios" in answer["facts"]
        assert ask(client, pmo, "scenario", "Why?").status_code == 422
