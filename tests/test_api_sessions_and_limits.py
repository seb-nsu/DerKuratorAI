"""HTTP API before real use of the front ends: sign-out on the server, token renewal, the limit on calculations,
when several server processes are allowed and the NiceGUI session cookie."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import limits, state_store, tokens
from app.api.main import create_app
from app.views.approvals import shown_cells
from tests import test_api
from tests.test_api import ERP, PM, PMO, sign_in

source, client = test_api.source, test_api.client


class TestSessions:
    def test_sign_out_revokes_the_session_on_the_server(self, client):
        headers = sign_in(client, PM)
        renewed = client.post("/api/v1/auth/refresh", headers=headers)
        assert renewed.status_code == 200
        renewed_headers = {"Authorization": f"Bearer {renewed.json()['access_token']}"}
        assert client.post("/api/v1/auth/sign-out", headers=headers).json() == {"message": "Signed out."}
        for h in (headers, renewed_headers):  # the token and its renewal: same session, both refused
            answer = client.get("/api/v1/me", headers=h)
            assert answer.status_code == 401 and "Signed out" in answer.json()["detail"]
        assert client.get("/api/v1/me", headers=sign_in(client, PM)).status_code == 200  # a new sign-in works

    def test_renewal_keeps_the_session_and_ends_after_twelve_hours(self):
        first = tokens.issue(PM, 60, "DEV", now=1000)
        old = tokens.claims(first, "DEV", now=1030)
        renewed = tokens.claims(tokens.issue(PM, 60, "DEV", now=1030, renews=old), "DEV", now=1080)
        assert renewed["sid"] == old["sid"] and renewed["auth"] == 1000 and renewed["exp"] == 1090
        assert tokens.renewable(renewed, now=1000 + tokens.MAX_SESSION_S - 1)
        assert not tokens.renewable(renewed, now=1000 + tokens.MAX_SESSION_S)

    def test_revoked_entries_are_forgotten_once_no_token_can_be_valid(self):
        tokens.revoke("abc", 60, now=1000)
        assert tokens._is_revoked("abc", 1059) and not tokens._is_revoked("abc", 1060)
        tokens.revoke("other", 60, now=2000)
        assert state_store.store().get(tokens.REVOKED, "abc", now=1000) is None  # dropped by the later sign-out


class TestCalculationLimits:
    def test_second_calculation_of_the_same_user_waits_for_the_first(self, client):
        headers = sign_in(client, PM)
        with limits.heavy(PM):
            answer = client.post("/api/v1/steering/runs", headers=headers,
                                 json={"request": f"Cut the budget of {ERP} by 10 %"})
        assert answer.status_code == 429 and answer.json()["detail"] == limits.RUNNING
        assert answer.headers["Retry-After"] == "10"

    def test_per_minute_limit_per_user(self, monkeypatch):
        monkeypatch.setenv("API_HEAVY_PER_MINUTE", "2")
        for _ in range(2):
            with limits.heavy(PM):
                pass
        with pytest.raises(HTTPException) as refused, limits.heavy(PM):
            pass
        assert refused.value.status_code == 429 and refused.value.detail == limits.TOO_MANY
        with limits.heavy(PMO):  # another user is not affected
            pass

    def test_busy_server_refuses_after_waiting(self, monkeypatch):
        monkeypatch.setenv("API_HEAVY_SLOTS", "1")
        monkeypatch.setattr(limits, "WAIT_S", 0.01)
        with limits.heavy(PM):
            with pytest.raises(HTTPException) as refused, limits.heavy(PMO):
                pass
        assert refused.value.detail == limits.BUSY
        with limits.heavy(PMO):  # the slot is free again, and PMO's refused call left no "running" mark
            pass


class TestServerProcesses:
    def test_api_refuses_several_workers_without_the_file_store(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        with pytest.raises(RuntimeError, match="STATE_STORE=file"):
            create_app()
        monkeypatch.setenv("STATE_STORE", "file")
        with pytest.raises(RuntimeError, match="API_TOKEN_SECRET"):
            create_app()
        monkeypatch.setenv("API_TOKEN_SECRET", "x" * 32)
        create_app()  # the API alone (or React) with the file store and one secret
        monkeypatch.setenv("FRONTEND", "nicegui")
        with pytest.raises(RuntimeError, match="sticky sessions"):
            create_app()

    def test_serve_allows_several_workers_only_for_react_on_the_file_store(self, monkeypatch):
        from scripts import serve

        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with pytest.raises(SystemExit, match="STATE_STORE=file"):
            serve.workers("react")
        monkeypatch.setenv("STATE_STORE", "file")
        monkeypatch.setenv("API_TOKEN_SECRET", "x" * 32)
        assert serve.workers("react") == 4
        with pytest.raises(SystemExit, match="exactly one server process"):
            serve.workers("nicegui")
        monkeypatch.setenv("STATE_STORE", "redis")
        with pytest.raises(SystemExit, match="STATE_STORE must be one of"):
            serve.workers("react")
        monkeypatch.setenv("WEB_CONCURRENCY", "two")
        with pytest.raises(SystemExit, match="must be a number"):
            serve.workers("react")
        monkeypatch.setenv("WEB_CONCURRENCY", "1")
        monkeypatch.delenv("STATE_STORE")
        assert serve.workers("nicegui") == 1


def test_nicegui_session_cookie_is_secure_in_prod(monkeypatch):
    from app.core.runtime_config import update_runtime_config
    from frontends.nicegui.main import session_cookie

    update_runtime_config(environment="DEV")
    assert session_cookie() == {"https_only": False}
    update_runtime_config(environment="PROD")
    assert session_cookie() == {"https_only": True}


def test_approvals_cells_are_formatted_once():
    cells = shown_cells({"verdict": "NO-GO", "timestamp": "2026-10-02T18:40:12",
                         "changes": [{"field": "budget_cost_eur", "old": 100_000, "new": 80_000}]})
    assert cells == {"verdict_label": "■ NO-GO", "budget_delta_label": "−€20,000", "submitted_label": "2026-10-02 18:40"}
    assert shown_cells({}) == {"verdict_label": "– No verdict", "budget_delta_label": "–", "submitted_label": "–"}
