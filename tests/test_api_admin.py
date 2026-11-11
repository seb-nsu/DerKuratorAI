"""HTTP API of the Admin Console: System Admin only, keys write-only, destructive actions need confirm = true and are
audited – on a temp copy of the demo portfolio with all logs in the temp folder."""
from __future__ import annotations

import json
import shutil

import pytest

from app.api.main import create_app
from app.core import env_config, logs
from app.core.auth import hash_password
from app.core.config import PROJECT_ROOT
from app.core.runtime_config import get_runtime_config, update_runtime_config
from app.core.security import ROLE_ADMIN
from tests import test_api

source, client = test_api.source, test_api.client
DEMO_PASSWORD, sign_in = test_api.DEMO_PASSWORD, test_api.sign_in
ADMIN = "SYSTEM-ADMIN"
SECRET = "sk-test-very-secret-value-1234567890"


@pytest.fixture(autouse=True)
def admin_account(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
        {"user_id": ADMIN, "role": ROLE_ADMIN, "password_hash": hash_password(DEMO_PASSWORD)}]}))
    # the model catalog and the test report live in DATA_DIR: a temp copy, never the real data/
    shutil.copy(PROJECT_ROOT / "data" / "supported_models.json", tmp_path / "supported_models.json")
    monkeypatch.setattr(logs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(logs, "TEST_REPORT", tmp_path / "test_report.html")


@pytest.fixture
def admin(client):
    return sign_in(client, ADMIN)


def audit_events(event: str) -> list[dict]:
    try:
        records, _ = logs.read_jsonl(logs.AUDIT_LOG)
    except FileNotFoundError:
        return []
    return [r for r in records if r.get("event") == event]


def admin_routes(client) -> list[tuple[str, str]]:
    paths = client.get("/api/v1/openapi.json").json()["paths"]
    return [(method, path.replace("{snapshot_id}", "x").replace("{name}", "audit"))
            for path, ops in paths.items() if path.startswith("/api/v1/admin") for method in ops]


class TestAccess:
    def test_every_admin_route_is_refused_for_other_roles_and_without_token(self, client):
        routes = admin_routes(client)
        assert len(routes) >= 35
        for user in (test_api.PM, test_api.PMO, test_api.DEPUTY):
            headers = sign_in(client, user)
            for method, path in routes:
                response = client.request(method, path, headers=headers, json={"confirm": True})
                assert response.status_code == 403, (user, method, path, response.text)
        for method, path in routes:
            assert client.request(method, path, json={}).status_code == 401, (method, path)

    def test_admin_sees_status_but_no_project_data(self, client, admin):
        status = client.get("/api/v1/admin/status", headers=admin).json()
        assert status["environment"] == "DEV" and status["data_source"] == "json"
        assert {"routing", "endpoints", "auth", "graph", "graph_backend", "tracing"} <= set(status)
        backend = status["graph_backend"]
        assert backend["configured"] == backend["active"] == "networkx" and backend["warning"] is None
        assert backend["rows"] == [{"setting": "Graph backend", "value": "NetworkX (in memory, default)"}]
        assert client.get("/api/v1/projects", headers=admin).status_code == 403

    def test_destructive_actions_need_confirm(self, client, admin):
        for path in ("/settings/reload", "/finops/reset", "/demo-reset", "/logs/audit/clear", "/logs/system/clear", "/logs/shadow/clear"):
            assert client.post(f"/api/v1/admin{path}", headers=admin, json={}).status_code == 422, path
        assert client.post("/api/v1/admin/purge-user", headers=admin, json={"user_id": "x"}).status_code == 422
        assert client.post("/api/v1/admin/environment", headers=admin, json={"environment": "TEST"}).status_code == 422
        assert client.post("/api/v1/admin/servicenow/sync", headers=admin, json={"full": True}).status_code == 422
        assert not audit_events("finops_counters_reset") and not audit_events("demo_data_reset")


class TestSettings:
    def test_keys_are_write_only_and_audited_masked(self, client, admin):
        saved = client.put("/api/v1/admin/settings", headers=admin, json={"openai_api_key": SECRET, "temperature": 0.3})
        assert saved.status_code == 200 and saved.json()["kind"] == "success", saved.text
        view = client.get("/api/v1/admin/settings", headers=admin)
        assert view.json()["api_keys"]["openai"] == {"set": True, "key_hint": "Currently set. Leave empty to keep."}
        assert view.json()["temperature"] == 0.3
        for path in ("/settings", "/status", "/finops", "/logs/audit", "/logs/system"):
            assert SECRET not in client.get(f"/api/v1/admin{path}", headers=admin).text, path
        changed = audit_events("admin_settings_changed")[-1]["changed"]
        assert changed["openai_api_key"] == "***" and changed["temperature"] == "0.3"
        assert "password_hash" not in client.get("/api/v1/admin/status", headers=admin).text

    def test_invalid_settings_are_refused(self, client, admin):
        assert client.put("/api/v1/admin/settings", headers=admin, json={"temperature": 2}).status_code == 422
        assert client.put("/api/v1/admin/settings", headers=admin,
                          json={"agents": {"analyst": {"main": "not-a-catalog-model"}}}).status_code == 422
        assert client.put("/api/v1/admin/settings", headers=admin,
                          json={"agents": {"nobody": {"main": "x"}}}).status_code == 422

    def test_agent_model_from_the_catalog_and_shadow_off(self, client, admin):
        options = client.get("/api/v1/admin/settings", headers=admin).json()["model_options"]
        response = client.put("/api/v1/admin/settings", headers=admin,
                              json={"agents": {"analyst": {"main": options[-1], "shadow": ""}}})
        assert response.status_code == 200, response.text
        assert get_runtime_config().model_for_role("analyst") == options[-1]

    def test_reload_and_clear_caches(self, client, admin):
        assert client.post("/api/v1/admin/settings/reload", headers=admin, json={"confirm": True}).status_code == 200
        assert audit_events("admin_settings_reloaded")
        assert client.post("/api/v1/admin/caches/clear", headers=admin).json()["kind"] == "success"

    def test_model_test_offline_fails_and_add_model_after_a_successful_test(self, client, admin, monkeypatch):
        from app import services

        refused = client.post("/api/v1/admin/models/test", headers=admin, json={"model_id": "some-model"})
        assert refused.status_code == 409 and refused.json()["kind"] == "error"
        monkeypatch.setattr(services, "test_model_connection", lambda model_id, thinking: (True, "Reachable."))
        added = client.post("/api/v1/admin/models", headers=admin, json={"model_id": "custom-test-model"})
        assert added.status_code == 200 and added.json()["updated"] is False, added.text
        assert "custom-test-model" in [m["model_id"] for m in client.get("/api/v1/admin/models", headers=admin).json()["models"]]
        assert audit_events("model_added")[-1]["model_id"] == "custom-test-model"

    def test_environment_switch_and_promotion(self, client, admin, tmp_path):
        promoted = client.post("/api/v1/admin/environment/promote", headers=admin,
                               json={"source": "DEV", "target": "TEST", "confirm": True})
        assert promoted.status_code == 200 and env_config.env_config_path("TEST").exists(), promoted.text
        assert client.post("/api/v1/admin/environment/promote", headers=admin,
                           json={"source": "DEV", "target": "PROD", "confirm": True}).status_code == 422
        refused = client.post("/api/v1/admin/environment", headers=admin, json={"environment": "PROD", "confirm": True})
        assert refused.status_code == 409 and get_runtime_config().environment == "DEV"  # demo accounts active
        switched = client.post("/api/v1/admin/environment", headers=admin, json={"environment": "TEST", "confirm": True})
        assert switched.status_code == 200 and get_runtime_config().environment == "TEST", switched.text
        assert audit_events("environment_switched")[-1]["target"] == "TEST"
        assert client.get("/api/v1/admin/status", headers=admin).status_code == 401  # tokens are bound to the environment

    def test_mcp_settings(self, client, admin):
        saved = client.put("/api/v1/admin/mcp", headers=admin,
                           json={"enabled": False, "servers": {"jira": "https://jira.example.com/mcp"}})
        assert saved.status_code == 200, saved.text
        view = client.get("/api/v1/admin/mcp", headers=admin).json()
        jira = next(s for s in view["servers"] if s["name"] == "jira")
        assert jira["url"] == "https://jira.example.com/mcp" and jira["token_set"] is False
        assert client.put("/api/v1/admin/mcp", headers=admin, json={"enabled": True, "servers": {"x": "y"}}).status_code == 422
        assert client.post("/api/v1/admin/mcp/unknown/probe", headers=admin).status_code == 404


class TestGovernance:
    """Business rules and required fields belong to the PMO Head; the System Admin and the other PMO accounts only read."""

    @pytest.fixture
    def head(self, client):
        return sign_in(client, test_api.PMO)  # PMO-ADMIN is the PMO Head in the demo accounts

    def test_only_the_pmo_head_changes_the_rules(self, client, admin, head):
        deputy, pm = sign_in(client, test_api.DEPUTY), sign_in(client, test_api.PM)
        writes = [("PUT", "/business-rules", {"values": {"max_budget_change_pct": 10}}),
                  ("POST", "/business-rules/reset", {"confirm": True}),
                  ("PUT", "/required-fields", {"fields": ["project_number"]}),
                  ("POST", "/required-fields/reset", {"confirm": True})]
        for headers in (admin, deputy, pm):
            for method, path, body in writes:
                response = client.request(method, f"/api/v1/governance{path}", headers=headers, json=body)
                assert response.status_code == 403, (method, path, response.text)
                assert client.request(method, f"/api/v1/admin{path}", headers=headers, json=body).status_code in (403, 404, 405)
        for path in ("/business-rules", "/required-fields"):
            assert client.get(f"/api/v1/governance{path}", headers=deputy).status_code == 200
            assert client.get(f"/api/v1/governance{path}", headers=pm).status_code == 403
            assert client.get(f"/api/v1/admin{path}", headers=admin).status_code == 200  # read-only for the admin
        assert not audit_events("business_rules_changed") and not audit_events("required_fields_changed")

    def test_core_refuses_everyone_but_the_pmo_head(self):
        from app.core.security import ROLE_PMO

        with pytest.raises(PermissionError):
            update_runtime_config(business_rules={"max_budget_change_pct": 10}, role=ROLE_ADMIN)
        with pytest.raises(PermissionError):
            update_runtime_config(business_rules={"max_budget_change_pct": 10}, role=ROLE_PMO)
        with pytest.raises(PermissionError):  # the PMO Head may not touch the System Admin's settings
            update_runtime_config(temperature=0.2, role=ROLE_PMO, pmo_head=True)
        update_runtime_config(business_rules={"max_budget_change_pct": 10}, role=ROLE_PMO, pmo_head=True)

    def test_destructive_resets_need_confirm(self, client, head):
        for path in ("/required-fields/reset", "/business-rules/reset"):
            assert client.post(f"/api/v1/governance{path}", headers=head, json={}).status_code == 422, path

    def test_required_fields(self, client, head):
        view = client.get("/api/v1/governance/required-fields", headers=head).json()
        assert "project_number" in view["options"] and view["locked"]
        fields = [f for f in view["required_fields"] if f != "project_sponsor"]
        saved = client.put("/api/v1/governance/required-fields", headers=head, json={"fields": fields})
        assert saved.status_code == 200 and "project_sponsor" in saved.json()["removed"]
        changed = audit_events("required_fields_changed")[-1]
        assert changed["removed"] == ["project_sponsor"] and changed["user_id"] == test_api.PMO
        assert client.post("/api/v1/governance/required-fields/reset", headers=head, json={"confirm": True}).status_code == 200

    def test_bounds_are_validated_and_changes_audited(self, client, head):
        rules = {r["name"]: r for r in client.get("/api/v1/governance/business-rules", headers=head).json()["rules"]}
        rule = rules["max_budget_change_pct"]
        assert rule["max"] is not None and rule["group"] == "Guardrails & capacity"
        too_high = client.put("/api/v1/governance/business-rules", headers=head,
                              json={"values": {"max_budget_change_pct": rule["max"] + 1}})
        assert too_high.status_code == 422
        assert client.put("/api/v1/governance/business-rules", headers=head, json={"values": {"nope": 1}}).status_code == 422
        new = rule["value"] - 1
        saved = client.put("/api/v1/governance/business-rules", headers=head, json={"values": {"max_budget_change_pct": new}})
        assert saved.status_code == 200 and saved.json()["reseeded"] is False, saved.text
        assert audit_events("business_rules_changed")[-1]["changed"]["max_budget_change_pct"]["new"] == new
        reset = client.post("/api/v1/governance/business-rules/reset", headers=head, json={"confirm": True})
        assert reset.status_code == 200 and audit_events("business_rules_reset")

    def test_capacity_limit_reseeds_the_graph(self, client, head):
        value = get_runtime_config().business_rules.team_capacity_limit_pct
        saved = client.put("/api/v1/governance/business-rules", headers=head,
                           json={"values": {"team_capacity_limit_pct": value - 5}})
        assert saved.status_code == 200 and saved.json()["reseeded"] is True and "Dependency graph" in saved.json()["message"]


class TestData:
    def test_finops_and_reset(self, client, admin):
        usage = client.get("/api/v1/admin/finops", headers=admin).json()
        assert {"cost_usd", "total_tokens", "calls", "by_model", "thinking_share"} <= set(usage)
        assert client.post("/api/v1/admin/finops/reset", headers=admin, json={"confirm": True}).status_code == 200
        assert audit_events("finops_counters_reset")

    def test_snapshot_restore_undoes_a_change_and_rebuilds_the_graph(self, client, admin, source):
        created = client.post("/api/v1/admin/snapshots", headers=admin)
        assert created.status_code == 201, created.text
        snap = created.json()["snapshot_id"]
        original = source.read_text(encoding="utf-8")
        source.write_text(original.replace("PRJ-DEMO-1001", "PRJ-DEMO-1001", 1) + "\n", encoding="utf-8")
        listed = client.get("/api/v1/admin/snapshots", headers=admin).json()
        assert snap in [s["id"] for s in listed["snapshots"]] and listed["source"] == source.name
        assert client.post("/api/v1/admin/snapshots/unknown/restore", headers=admin,
                           json={"confirm": True}).status_code == 404
        restored = client.post(f"/api/v1/admin/snapshots/{snap}/restore", headers=admin, json={"confirm": True})
        assert restored.status_code == 200 and "Dependency graph rebuilt" in restored.json()["message"], restored.text
        assert source.read_text(encoding="utf-8") == original
        assert audit_events("snapshot_restored")[-1]["previous_state_snapshot"] == restored.json()["previous_state_snapshot"]

    def test_demo_reset_is_audited(self, client, admin):
        reset = client.post("/api/v1/admin/demo-reset", headers=admin, json={"confirm": True})
        assert reset.status_code == 200, reset.text
        assert audit_events("demo_data_reset")

    def test_purge_logs_only_the_pseudonym(self, client, admin):
        purged = client.post("/api/v1/admin/purge-user", headers=admin, json={"user_id": "Priya Raman", "confirm": True})
        assert purged.status_code == 200, purged.text
        record = audit_events("user_purged")[-1]
        assert record["purged"] == purged.json()["pseudonym"] and "Priya Raman" not in json.dumps(record)
        assert "Priya Raman" not in purged.text
        own = client.post("/api/v1/admin/purge-user", headers=admin, json={"user_id": ADMIN, "confirm": True})
        assert own.status_code == 422

    def test_database_journal_and_servicenow(self, client, admin):
        database = client.get("/api/v1/admin/database", headers=admin).json()
        assert database["data_source"] == "json"
        rebuilt = client.post("/api/v1/admin/database/rebuild-graph", headers=admin)
        assert rebuilt.status_code == 200 and rebuilt.json()["counts"]["Project"] == 11
        assert rebuilt.json()["message"].startswith("Dependency graph built from JSON: 11 projects")
        assert audit_events("graph_rebuilt")
        assert client.get("/api/v1/admin/journal", headers=admin).json() == {"findings": []}
        missing = client.post("/api/v1/admin/journal/resolve", headers=admin,
                              json={"operation_id": "x", "kind": "intent_without_commit", "comment": "checked"})
        assert missing.status_code == 404
        assert client.get("/api/v1/admin/servicenow", headers=admin).json()["configured"] is False

    def test_system_tests_without_saved_run(self, client, admin):
        assert client.get("/api/v1/admin/system-tests", headers=admin).json()["last_run"] is None
        assert client.get("/api/v1/admin/system-tests/report", headers=admin).status_code == 404


class TestLogs:
    def test_audit_log_chain_and_clearing(self, client, admin):
        client.post("/api/v1/admin/finops/reset", headers=admin, json={"confirm": True})
        log = client.get("/api/v1/admin/logs/audit", headers=admin, params={"event": "finops_counters_reset"}).json()
        assert log["total"] == 1 and "finops_counters_reset" in log["events"]
        chain = client.post("/api/v1/admin/logs/audit/verify-chain", headers=admin).json()
        assert chain["ok"] is True and audit_events("audit_chain_verified")
        cleared = client.post("/api/v1/admin/logs/audit/clear", headers=admin, json={"confirm": True})
        assert cleared.status_code == 200 and cleared.json()["archive"], cleared.text
        assert client.post("/api/v1/admin/logs/decisions/clear", headers=admin, json={"confirm": True}).status_code == 404

    def test_log_readers(self, client, admin):
        update_runtime_config(temperature=0.2)  # nothing to show yet is a valid answer everywhere
        for path in ("business-audit", "analysis-runs", "incidents", "shadow", "system", "llm-calls"):
            assert client.get(f"/api/v1/admin/logs/{path}", headers=admin).status_code == 200, path
        assert client.get("/api/v1/admin/logs/system", headers=admin, params={"level": "DEBUG"}).status_code == 422

    def test_analysis_runs_list_no_request_text(self, client, admin):
        pm = sign_in(client, test_api.PM)
        test_api.what_if(client, pm, 10)
        runs = client.get("/api/v1/admin/logs/analysis-runs", headers=admin).json()
        assert runs["runs"] and "Cut the budget" not in json.dumps(runs["runs"])
        assert isinstance(runs["runs"][0]["plan"], list) and runs["runs"][0]["mode"] in runs["modes"]
        assert runs["total"] == len(runs["runs"]) and runs["median_s"] is not None and runs["p95_s"] >= runs["median_s"]
        other = client.get("/api/v1/admin/logs/analysis-runs", headers=admin, params={"mode": "no-such-mode"}).json()
        assert other["runs"] == [] and other["total"] == runs["total"]

    def test_files_are_named_relative_to_the_project(self, client, admin):
        files = client.get("/api/v1/admin/files", headers=admin).json()
        assert files["env_file"].endswith("env_dev.json") and files["model_registry"].endswith("supported_models.json")
        assert files["audit_log"].endswith("decision_log.jsonl") and files["system_log"].endswith("app.log")

    def test_required_fields_carry_labels_and_tests_the_report_time(self, client, admin):
        view = client.get("/api/v1/admin/required-fields", headers=admin).json()
        assert set(view["options"]) <= set(view["labels"]) and set(view["locked"]) <= set(view["labels"])
        assert client.get("/api/v1/admin/system-tests", headers=admin).json()["report_saved_at"] is None
        logs.TEST_REPORT.write_text("<html></html>", encoding="utf-8")
        assert client.get("/api/v1/admin/system-tests", headers=admin).json()["report_saved_at"]


def test_every_admin_route_declares_its_response_model():
    """The React front end generates its types from the contract: every answer is a named schema."""
    paths = create_app().openapi()["paths"]
    assert "/api/v1/admin/status" in paths and "/api/v1/admin/logs/{name}/clear" in paths
    for path, ops in paths.items():
        if not path.startswith("/api/v1/admin") or path.endswith("/system-tests/report"):
            continue
        for method, op in ops.items():
            answer = next(v for k, v in op["responses"].items() if k.startswith("2"))
            assert "$ref" in answer["content"]["application/json"]["schema"], (method, path)
