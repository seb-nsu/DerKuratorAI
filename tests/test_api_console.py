"""HTTP API of the Steering Console and the What-if comparison: guided scenarios, scenario memory, direct apply,
saved what-ifs and the chat history, on a temp copy of the demo portfolio."""
from __future__ import annotations

import json

from app.api import runs
from app.api.routes import steering
from app.core import logs
from tests import test_api
from tests.test_api import ERP, OTHER, PM, PMO, budget, sign_in, what_if

source, client = test_api.source, test_api.client


def guided(client, headers, action, project=ERP, target=None, **params):
    body = {"guided": {"action": action, "project": project, "target": target, "params": params}}
    return client.post("/api/v1/steering/runs", headers=headers, json=body)


def held_run(user_id, role, changes, verdict="GO") -> str:
    """A run as the pipeline leaves it (for change sets the demo requests do not produce)."""
    run_id = f"run-{len(changes)}-{verdict}"
    runs.put(run_id, {"user_id": user_id, "role": role, "request": "Set the status to green", "paper": "",
                      "verdict": {"label": verdict, "decision": verdict}, "changes": changes,
                      "proposed_changes": changes, "forced": False, "scenario_state": {"turns": ["x"]}})
    steering._set_latest(user_id, run_id)
    return run_id


def status_of(path, pid):
    return next(p for p in json.loads(path.read_text(encoding="utf-8"))["Projects"] if p["project_number"] == pid)["status"]


class TestGuidedScenarios:
    def test_catalog_lists_scenarios_and_only_projects_in_scope(self, client):
        catalog = client.get("/api/v1/steering/guided", headers=sign_in(client, PM)).json()
        assert {s["action"] for s in catalog["scenarios"]} >= {"budget_change", "schedule", "portfolio_cut"}
        numbers = {p["project_number"] for p in catalog["projects"]}
        assert ERP in numbers and OTHER not in numbers and catalog["can_force"] is False

    def test_form_has_the_schedule_bound_and_hides_foreign_projects(self, client):
        pm = sign_in(client, PM)
        form = client.get("/api/v1/steering/guided/schedule", headers=pm, params={"project": ERP}).json()
        months = next(p for p in form["params"] if p["name"] == "months_delta")
        assert months["min"] <= 0 < months["max"] and months["kind"] == "integer"
        assert client.get("/api/v1/steering/guided/schedule", headers=pm, params={"project": OTHER}).status_code == 404
        assert client.get("/api/v1/steering/guided/nonsense", headers=pm).status_code == 404

    def test_guided_budget_cut_runs_the_plan(self, client):
        response = guided(client, sign_in(client, PM), "budget_change", value=10)
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["request"].startswith("Cut Total budget of PRJ-DEMO-1002") and run["approval_mode"] == "propose"
        assert run["verdict"]["label"] == "CONDITIONAL GO" and "cost_usd" not in run

    def test_guided_inputs_are_checked_on_the_server(self, client):
        pm = sign_in(client, PM)
        assert guided(client, pm, "budget_change", project=OTHER, value=10).status_code == 404
        assert guided(client, pm, "schedule", months_delta=999).status_code == 422
        assert guided(client, pm, "budget_change", cost_type="everything").status_code == 422
        assert guided(client, pm, "budget_change", unknown=1).status_code == 422
        assert guided(client, pm, "budget_transfer", target=OTHER).status_code == 404
        assert guided(client, pm, "rebaseline", new_budget_eur=1).status_code == 422  # reason is mandatory
        both = client.post("/api/v1/steering/runs", headers=pm,
                           json={"request": "x", "guided": {"action": "cancel", "project": ERP}})
        assert both.status_code == 422

    def test_only_the_pmo_forces_a_simulation(self, client):
        response = client.post("/api/v1/steering/runs", headers=sign_in(client, PM), json={"request": "x", "force": True})
        assert response.status_code == 403


class TestScenarioMemory:
    def test_chat_history_offers_to_build_on_the_latest_step(self, client):
        pm = sign_in(client, PM)
        first = what_if(client, pm, 10)
        chat = client.get("/api/v1/steering/chat", headers=pm).json()
        assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
        assert chat["messages"][1]["status"]["state"] == "draft" and chat["messages"][1]["status"]["actionable"]
        assert "cost_usd" not in chat["messages"][1]["meta"] and "scenario_state" not in chat["messages"][1]["meta"]
        cont = chat["continuation"]
        assert cont["available"] and cont["base_run_id"] == first["run_id"]
        assert not cont["default_continue"]  # building on a step is an explicit choice
        second = client.post("/api/v1/steering/runs", headers=pm,
                             json={"request": f"Cut the budget of General Ledger Consolidation ({ERP}) by 10 %",
                                   "base_run_id": cont["base_run_id"]}).json()
        assert second["continued"] and second["step"] == 2
        budgets = [c["new"] for c in second["proposed_changes"] if c["field"] == "budget_cost_eur"]
        assert budgets == [1665000.0, 1498500.0]
        assert [c["new"] for c in second["change_set"] if c["field"] == "budget_cost_eur"] == [1498500.0]
        statuses = [m["status"]["state"] for m in client.get("/api/v1/steering/chat", headers=pm).json()["messages"]
                    if m["role"] == "assistant"]
        assert statuses == ["outdated", "draft"]

    def test_a_no_go_step_starts_fresh_by_default(self, client):
        pm = sign_in(client, PM)
        assert what_if(client, pm, 20)["verdict"]["label"] == "NO-GO"
        cont = client.get("/api/v1/steering/chat", headers=pm).json()["continuation"]
        assert cont["available"] and cont["previous_verdict_blocks"] == "NO-GO" and not cont["default_continue"]

    def test_clear_history(self, client):
        pm = sign_in(client, PM)
        run = what_if(client, pm, 10)
        assert client.delete("/api/v1/steering/chat", headers=pm).status_code == 204
        assert client.get("/api/v1/steering/chat", headers=pm).json()["messages"] == []
        saved = client.post(f"/api/v1/steering/runs/{run['run_id']}/save", headers=pm, json={"name": "A"})
        assert saved.status_code == 409  # cleared: no longer the latest step

    def test_history_is_per_user(self, client):
        what_if(client, sign_in(client, PM), 10)
        assert client.get("/api/v1/steering/chat", headers=sign_in(client, PMO)).json()["messages"] == []


class TestDirectApply:
    def test_governed_changes_cannot_be_applied_directly(self, client, source):
        pm = sign_in(client, PM)
        run = what_if(client, pm, 10)
        before = budget(source)
        response = client.post(f"/api/v1/steering/runs/{run['run_id']}/apply", headers=pm, json={})
        assert response.status_code == 409 and budget(source) == before

    def test_resource_transfer_is_recorded_as_a_hand_over(self, client, source):
        pmo = sign_in(client, PMO)
        run = guided(client, pmo, "resource_transfer", target=OTHER, fte=1).json()
        assert run["approval_mode"] == "apply" and run["handover"]
        before = source.read_text(encoding="utf-8")
        done = client.post(f"/api/v1/steering/runs/{run['run_id']}/apply", headers=pmo, json={})
        assert done.status_code == 200, done.text
        assert done.json()["closed_as"] == "handed_over" and done.json()["applied"] == 0
        assert source.read_text(encoding="utf-8") == before
        last = client.get("/api/v1/steering/chat", headers=pmo).json()["messages"][-1]
        assert last["status"]["state"] == "handed_over"
        assert client.post(f"/api/v1/steering/runs/{run['run_id']}/apply", headers=pmo, json={}).status_code == 404

    def test_apply_writes_snapshot_and_audit_record(self, client, source):
        pm = sign_in(client, PM)
        change = {"project_id": ERP, "table": "Projects", "field": "status", "old": status_of(source, ERP), "new": "Green"}
        run_id = held_run(PM, "Project Manager (Restricted)", [change])
        done = client.post(f"/api/v1/steering/runs/{run_id}/apply", headers=pm, json={}).json()
        assert done["kind"] == "success" and done["closed_as"] == "applied" and done["snapshot_id"]
        assert status_of(source, ERP) == "Green"
        record = [r for r in logs.read_jsonl(logs.AUDIT_LOG)[0] if r.get("event") == "scenario_applied"][-1]
        assert record["run_id"] == run_id and record["applied"] == 1

    def test_no_go_needs_override_and_reason(self, client, source):
        pm = sign_in(client, PM)
        change = {"project_id": ERP, "table": "Projects", "field": "status", "old": status_of(source, ERP), "new": "Green"}
        run_id = held_run(PM, "Project Manager (Restricted)", [change], verdict="NO-GO")
        url = f"/api/v1/steering/runs/{run_id}/apply"
        assert client.post(url, headers=pm, json={"override": True}).status_code == 422
        assert status_of(source, ERP) != "Green"
        done = client.post(url, headers=pm, json={"override": True, "comment": "Accepted"}).json()
        assert done["override"] and status_of(source, ERP) == "Green"

    def test_change_outside_the_scope_is_refused(self, client, source):
        pm = sign_in(client, PM)
        change = {"project_id": OTHER, "table": "Projects", "field": "status", "old": status_of(source, OTHER), "new": "Green"}
        run_id = held_run(PM, "Project Manager (Restricted)", [change])
        response = client.post(f"/api/v1/steering/runs/{run_id}/apply", headers=pm, json={})
        assert response.status_code == 403 and OTHER not in response.text
        assert status_of(source, OTHER) != "Green"

    def test_run_of_another_user_cannot_be_applied(self, client):
        run = what_if(client, sign_in(client, PM), 10)
        assert client.post(f"/api/v1/steering/runs/{run['run_id']}/apply", headers=sign_in(client, PMO),
                           json={}).status_code == 404


class TestWhatIfComparison:
    def _save(self, client, headers, pct, name):
        run = what_if(client, headers, pct)
        response = client.post(f"/api/v1/steering/runs/{run['run_id']}/save", headers=headers, json={"name": name})
        assert response.status_code == 201, response.text
        return response.json()

    def test_save_twice_and_compare_with_charts(self, client):
        pm = sign_in(client, PM)
        a = self._save(client, pm, 10, "A – soft cut")
        b = self._save(client, pm, 30, "B – hard cut")
        assert a["budget_impact"] == {ERP: -185000.0} and b["budget_impact"] == {ERP: -555000.0}
        listed = client.get("/api/v1/whatifs", headers=pm).json()
        assert [s["name"] for s in listed["whatifs"]] == ["B – hard cut", "A – soft cut"]
        assert listed["default_pair"] == {"a": a["id"], "b": b["id"]}
        cmp = client.get("/api/v1/whatifs/compare", headers=pm, params=listed["default_pair"]).json()
        assert cmp["a"]["figures"]["net_budget_impact"] == -185000.0
        assert cmp["a"]["waterfall_figure"]["data"][0]["type"] == "waterfall"
        assert cmp["budget_impact_figure"]["data"][0]["type"] == "bar"
        assert any(d["field"] == "budget_cost_eur" and d["differs"] for d in cmp["differences"])
        assert client.get("/api/v1/whatifs/compare", headers=pm, params={"a": a["id"], "b": a["id"]}).status_code == 422

    def test_saved_what_if_is_saved_once_and_only_the_latest_step(self, client):
        pm = sign_in(client, PM)
        first = what_if(client, pm, 10)
        what_if(client, pm, 5)
        assert client.post(f"/api/v1/steering/runs/{first['run_id']}/save", headers=pm,
                           json={"name": "old"}).status_code == 409

    def test_pm_sees_and_deletes_only_own_what_ifs(self, client):
        pm, pmo = sign_in(client, PM), sign_in(client, PMO)
        mine = self._save(client, pm, 10, "Mine")
        other = sign_in(client, "Rowan Ellis")
        assert client.get("/api/v1/whatifs", headers=other).json()["whatifs"] == []
        assert client.get(f"/api/v1/whatifs/{mine['id']}", headers=other).status_code == 404
        assert client.delete(f"/api/v1/whatifs/{mine['id']}", headers=other).status_code == 404
        assert [s["id"] for s in client.get("/api/v1/whatifs", headers=pmo).json()["whatifs"]] == [mine["id"]]
        assert client.delete(f"/api/v1/whatifs/{mine['id']}", headers=pm).status_code == 204
        assert client.get("/api/v1/whatifs", headers=pm).json()["whatifs"] == []
        assert any(r.get("event") == "scenario_deleted" for r in logs.read_jsonl(logs.AUDIT_LOG)[0])

    def test_saved_what_if_shows_only_projects_in_the_current_scope(self, client):
        from app.core.scenario_store import save_scenario

        save_scenario("Mixed", PM, "Project Manager (Restricted)", ["x"], {"label": "GO", "conditions": ["c"]},
                      [{"project_id": ERP, "field": "budget_cost_eur", "old": 10, "new": 5},
                       {"project_id": OTHER, "field": "budget_cost_eur", "old": 10, "new": 5}],
                      {ERP: -5.0, OTHER: -5.0})
        detail = client.get("/api/v1/whatifs", headers=sign_in(client, PM)).json()["whatifs"][0]
        assert detail["restricted"] and detail["figures"]["changes"] == 1
        full = client.get(f"/api/v1/whatifs/{detail['id']}", headers=sign_in(client, PM)).text
        assert OTHER not in full
