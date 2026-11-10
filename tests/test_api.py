"""HTTP API: sign-in and tokens, row-level security, a what-if through the offline pipeline, and the four-eyes
proposal flow on a temp copy of the demo portfolio."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient

from app.api import deps, runs, tokens
from app.api.main import create_app
from app.core import clock
from app.core.auth import hash_password
from app.core.config import PROJECT_ROOT, get_settings
from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_ADMIN
from app.graph import portfolio_graph
from scripts import run_evals

DEMO_PASSWORD = json.loads((PROJECT_ROOT / "data" / "demo_users.json").read_text(encoding="utf-8"))["public_demo_password"]
PM, PMO, DEPUTY = "Priya Raman", "PMO-ADMIN", "PMO-DEPUTY"
ERP = "PRJ-DEMO-1002"  # General Ledger Consolidation, managed by Priya Raman
OTHER = "PRJ-DEMO-1001"  # not Priya Raman's


@pytest.fixture
def source(tmp_path, monkeypatch):
    if not run_evals.PORTFOLIO.exists():
        pytest.skip("Demo portfolio not available")
    path = tmp_path / "portfolio.json"
    shutil.copy(run_evals.PORTFOLIO, path)
    for key, value in {"JSON_PATH": str(path), "DATA_SOURCE": "json", "ENVIRONMENT": "DEV"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("API_TOKEN_SECRET", raising=False)
    frozen = datetime.combine(run_evals.EVAL_AS_OF, time(9)).astimezone()
    monkeypatch.setattr(clock, "now", lambda: frozen)  # the demo's expected verdicts hold on this date
    get_settings.cache_clear()
    deps.clear_data_cache()
    runs.clear()
    portfolio_graph.reset()
    yield path
    get_settings.cache_clear()
    deps.clear_data_cache()
    runs.clear()


@pytest.fixture
def client(source):
    return TestClient(create_app())


def sign_in(client, user, password=DEMO_PASSWORD) -> dict:
    response = client.post("/api/v1/auth/token", json={"user_id": user, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def budget(path, pid=ERP) -> float:
    return next(p for p in json.loads(path.read_text(encoding="utf-8"))["Projects"]
                if p["project_number"] == pid)["budget_cost_eur"]


def what_if(client, headers, pct) -> dict:
    response = client.post("/api/v1/steering/runs", headers=headers,
                           json={"request": f"Cut the budget of General Ledger Consolidation ({ERP}) by {pct} %"})
    assert response.status_code == 200, response.text
    return response.json()


class TestSignIn:
    def test_health_needs_no_sign_in(self, client):
        assert client.get("/api/v1/health").json() == {"status": "ok", "environment": "DEV"}

    def test_wrong_password_is_refused_without_saying_why(self, client):
        response = client.post("/api/v1/auth/token", json={"user_id": PM, "password": "wrong"})
        assert response.status_code == 401 and response.json()["detail"] == "Unknown user or wrong password."

    def test_invalid_sign_in_request_does_not_echo_the_password(self, client):
        response = client.post("/api/v1/auth/token", json={"password": "secret-value-123"})
        assert response.status_code == 422 and "secret-value-123" not in response.text
        assert response.json()["detail"][0]["loc"] == ["body", "user_id"]

    def test_token_identifies_the_user_and_its_role_comes_from_the_user_file(self, client):
        me = client.get("/api/v1/me", headers=sign_in(client, PM)).json()
        assert me["user_id"] == PM and me["role"] == "Project Manager (Restricted)"

    def test_every_data_route_needs_a_token(self, client):
        for path in ("/api/v1/me", "/api/v1/projects", "/api/v1/proposals"):
            assert client.get(path).status_code == 401
        assert client.get("/api/v1/projects", headers={"Authorization": "Bearer forged.token"}).status_code == 401

    def test_tampered_token_is_rejected(self, client):
        token = sign_in(client, PM)["Authorization"].split()[1]
        body, signature = token.split(".")
        claims = json.loads(tokens._unb64(body))
        forged = tokens._b64(json.dumps({**claims, "sub": PMO}).encode())
        response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}.{signature}"})
        assert response.status_code == 401

    def test_expired_token_and_token_of_another_environment(self):
        token = tokens.issue(PM, 60, "DEV", now=1000)
        assert tokens.verify(token, "DEV", now=1059) == PM
        with pytest.raises(tokens.TokenError, match="expired"):
            tokens.verify(token, "DEV", now=1060)
        with pytest.raises(tokens.TokenError):
            tokens.verify(token, "TEST", now=1000)

    def test_prod_without_secret_issues_no_token(self, monkeypatch):
        monkeypatch.delenv("API_TOKEN_SECRET", raising=False)
        with pytest.raises(tokens.TokenConfigError):
            tokens.issue(PM, 60, "PROD")
        monkeypatch.setenv("API_TOKEN_SECRET", "s" * 31)  # too short counts as missing
        with pytest.raises(tokens.TokenConfigError):
            tokens.issue(PM, 60, "PROD")
        monkeypatch.setenv("API_TOKEN_SECRET", "s" * 32)
        assert tokens.verify(tokens.issue(PM, 60, "PROD"), "PROD") == PM

    def test_removed_account_loses_access_at_once(self, client, monkeypatch, tmp_path):
        headers = sign_in(client, PM)
        users = tmp_path / "users.json"
        users.write_text(json.dumps({"users": [{"user_id": PMO, "role": "PMO (Full Access)",
                                                "password_hash": hash_password("x" * 12)}]}), encoding="utf-8")
        monkeypatch.setenv("AUTH_USERS_FILE", str(users))
        assert client.get("/api/v1/me", headers=headers).status_code == 401


class TestScope:
    def test_project_manager_sees_only_own_projects(self, client):
        listed = client.get("/api/v1/projects", headers=sign_in(client, PM)).json()
        numbers = {p["project_number"] for p in listed["projects"]}
        assert ERP in numbers and OTHER not in numbers and listed["total"] == len(numbers)
        assert all(p["project_manager"] == PM for p in listed["projects"])
        assert {"data_quality", "project_name"} <= set(listed["projects"][0])

    def test_project_outside_the_scope_answers_not_found(self, client):
        headers = sign_in(client, PM)
        assert client.get(f"/api/v1/projects/{OTHER}", headers=headers).status_code == 404
        detail = client.get(f"/api/v1/projects/{ERP}", headers=headers).json()
        assert detail["project"]["project_number"] == ERP and isinstance(detail["risks"], list)

    def test_pmo_sees_the_whole_portfolio(self, client):
        assert client.get("/api/v1/projects", headers=sign_in(client, PMO)).json()["total"] == 11

    def test_system_admin_sees_no_project_data(self, client, monkeypatch):
        monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
            {"user_id": "SYSTEM-ADMIN", "role": ROLE_ADMIN, "password_hash": hash_password(DEMO_PASSWORD)}]}))
        headers = sign_in(client, "SYSTEM-ADMIN")
        assert client.get("/api/v1/projects", headers=headers).status_code == 403
        assert client.post("/api/v1/steering/runs", headers=headers, json={"request": "x"}).status_code == 403


class TestProposalFlow:
    def test_pm_proposal_approved_by_pmo_is_written(self, client, source):
        pm = sign_in(client, PM)
        run = what_if(client, pm, 10)
        assert run["verdict"]["label"] == "CONDITIONAL GO" and run["approval_mode"] == "propose"
        assert run["proposed_changes"] and "cost_usd" not in run  # LLM costs stay in the Admin Console
        submitted = client.post("/api/v1/proposals", headers=pm, json={"run_id": run["run_id"]})
        assert submitted.status_code == 201, submitted.text
        pid = submitted.json()["id"]
        # one proposal per run
        assert client.post("/api/v1/proposals", headers=pm, json={"run_id": run["run_id"]}).status_code == 404
        # the requester follows it but cannot decide
        assert [p["id"] for p in client.get("/api/v1/proposals", headers=pm).json()["proposals"]] == [pid]
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=pm, json={}).status_code == 403
        pmo = sign_in(client, PMO)
        inbox = client.get("/api/v1/proposals", headers=pmo, params={"status": "submitted"}).json()["proposals"]
        assert pid in [p["id"] for p in inbox]
        before = budget(source)
        decided = client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo, json={"comment": "ok"})
        assert decided.status_code == 200 and decided.json()["kind"] == "success", decided.text
        assert budget(source) == pytest.approx(before * 0.9, rel=1e-6)
        detail = client.get(f"/api/v1/proposals/{pid}", headers=pm).json()
        assert detail["status"] == "approved" and detail["snapshot_id"] and detail["paper"]

    def test_no_go_needs_a_reason_to_submit_and_an_override_to_approve(self, client, source):
        pm = sign_in(client, PM)
        run = what_if(client, pm, 20)
        assert run["verdict"]["label"] == "NO-GO"
        refused = client.post("/api/v1/proposals", headers=pm, json={"run_id": run["run_id"]})
        assert refused.status_code == 422
        pid = client.post("/api/v1/proposals", headers=pm,
                          json={"run_id": run["run_id"], "justification": "Board asked for the cut"}).json()["id"]
        pmo = sign_in(client, PMO)
        before = budget(source)
        plain = client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo, json={})
        assert plain.status_code == 409 and budget(source) == before
        override = client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo,
                               json={"override": True, "comment": "Risks accepted by the board"})
        assert override.status_code == 200 and "with override" in override.json()["message"]

    def test_pmo_never_decides_own_proposal_and_rejection_needs_a_comment(self, client):
        pmo = sign_in(client, PMO)
        pid = client.post("/api/v1/proposals", headers=pmo, json={"run_id": what_if(client, pmo, 10)["run_id"]}).json()["id"]
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo, json={}).status_code == 409
        deputy = sign_in(client, DEPUTY)
        assert client.post(f"/api/v1/proposals/{pid}/reject", headers=deputy, json={"comment": " "}).status_code == 422
        rejected = client.post(f"/api/v1/proposals/{pid}/reject", headers=deputy, json={"comment": "Not now"})
        assert rejected.status_code == 200 and rejected.json()["kind"] == "success"

    def test_run_of_another_user_cannot_be_submitted_or_continued(self, client):
        run = what_if(client, sign_in(client, PM), 10)
        pmo = sign_in(client, PMO)
        assert client.post("/api/v1/proposals", headers=pmo, json={"run_id": run["run_id"]}).status_code == 404
        continued = client.post("/api/v1/steering/runs", headers=pmo,
                                json={"request": "and cut by 5 %", "base_run_id": run["run_id"]})
        assert continued.status_code == 404

    def test_proposal_of_another_pm_is_hidden(self, client):
        pm = sign_in(client, PM)
        pid = client.post("/api/v1/proposals", headers=pm, json={"run_id": what_if(client, pm, 10)["run_id"]}).json()["id"]
        other = sign_in(client, "Rowan Ellis")
        assert client.get(f"/api/v1/proposals/{pid}", headers=other).status_code == 404
        assert client.get("/api/v1/proposals", headers=other).json()["proposals"] == []


def test_unexpected_error_answers_an_incident_id(client, monkeypatch):
    monkeypatch.setattr(deps, "full_dataset", lambda: 1 / 0)
    response = TestClient(create_app(), raise_server_exceptions=False).get(
        "/api/v1/projects", headers=sign_in(client, PM))
    assert response.status_code == 500 and response.json()["incident_id"]
    assert "ZeroDivision" not in response.text


def test_session_timeout_sets_the_token_lifetime(client):
    update_runtime_config(auth={"session_timeout_minutes": 5})
    response = client.post("/api/v1/auth/token", json={"user_id": PM, "password": DEMO_PASSWORD})
    assert response.json()["expires_in"] == 300
