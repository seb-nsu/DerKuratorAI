"""The page frame of the API front ends: sign-in page info, pages per role, scope line and inbox count."""
from __future__ import annotations

import json

from app.core.auth import hash_password
from app.core.security import ROLE_ADMIN
from tests import test_api
from tests.test_api import DEMO_PASSWORD, PM, PMO, sign_in

source, client = test_api.source, test_api.client  # the API fixtures (temp demo portfolio, test client)


def test_sign_in_info_lists_demo_accounts_only_in_dev_on_demo_data(client):
    info = client.get("/api/v1/sign-in-info").json()
    assert info["configured"] and info["environment"] == "DEV"
    assert info["demo"]["password"] == DEMO_PASSWORD
    assert PM in {a["user_id"] for a in info["demo"]["accounts"]}
    assert all("password_hash" not in a for a in info["demo"]["accounts"])


def test_sign_in_info_in_prod_shows_no_demo_and_reports_missing_user_file(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "PROD")
    from app.core.config import get_settings
    from app.core.runtime_config import reset_runtime_config

    get_settings.cache_clear()
    reset_runtime_config()
    info = client.get("/api/v1/sign-in-info").json()
    assert info["environment"] == "PROD" and info["demo"] is None and info["configured"] is False


def test_pages_follow_the_role(client, monkeypatch):
    pm = client.get("/api/v1/workspace", headers=sign_in(client, PM)).json()
    nav = [p["title"] for p in pm["pages"] if not p["parent"]]
    assert nav == ["Overview", "My projects", "Steering Console", "My proposals", "My demands"]
    assert {p["id"]: p["parent"] for p in pm["pages"] if p["parent"]} == {"data_quality": "projects", "scenarios": "console"}
    assert pm["scope"] == {"visible": 2, "total": None} and pm["pending_approvals"] == 0  # no portfolio total for a PM
    assert pm["user"]["pmo_head"] is False
    pmo = client.get("/api/v1/workspace", headers=sign_in(client, PMO)).json()
    assert pmo["scope"] == {"visible": 11, "total": 11} and pmo["user"]["pmo_head"] is True
    assert [p["id"] for p in pmo["pages"] if not p["parent"]] == ["overview", "projects", "console", "approvals",
                                                                  "planning", "rules"]
    monkeypatch.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [
        {"user_id": "SYSTEM-ADMIN", "role": ROLE_ADMIN, "password_hash": hash_password(DEMO_PASSWORD)}]}))
    admin = client.get("/api/v1/workspace", headers=sign_in(client, "SYSTEM-ADMIN")).json()
    assert [p["id"] for p in admin["pages"]] == ["admin"] and admin["scope"] is None


def test_pending_count_counts_the_pmo_inbox(client):
    pm = sign_in(client, PM)
    run = client.post("/api/v1/steering/runs", headers=pm,
                      json={"request": "Cut the budget of PRJ-DEMO-1002 by 10 %"}).json()
    assert client.post("/api/v1/proposals", headers=pm, json={"run_id": run["run_id"]}).status_code == 201
    assert client.get("/api/v1/workspace", headers=sign_in(client, PMO)).json()["pending_approvals"] == 1
    assert client.post("/api/v1/data/reload", headers=pm).json() == {"reloaded": True}
