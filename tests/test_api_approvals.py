"""HTTP API, Approvals page: the page views with deadline columns, withdrawing an own proposal, reverting an approval
and the delegation of the approval right (deputy decides on behalf, never their own) – on a temp copy of the demo
portfolio."""
from __future__ import annotations

import json
from datetime import timedelta

from app.core import clock, logs
from tests import test_api
from tests.test_api import DEPUTY, ERP, PM, PMO, sign_in

source, client = test_api.source, test_api.client


def _end_date(path, pid=ERP) -> str:
    return next(p for p in json.loads(path.read_text(encoding="utf-8"))["Projects"]
                if p["project_number"] == pid)["planned_end_date"]


def _propose(client, headers, end="2027-06-30") -> str:
    """A governed master-data change of the PM: one proposal without a pipeline run."""
    response = client.post(f"/api/v1/projects/{ERP}/master-data", headers=headers,
                           json={"section": "governed", "values": {"planned_end_date": end}})
    assert response.status_code == 200, response.text
    return response.json()["proposal_id"]


def _events() -> list[str]:
    return [json.loads(line)["event"] for line in logs.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]


def _delegate(client, headers, deputy=DEPUTY, days=2):
    today = clock.today()
    return client.post("/api/v1/delegations", headers=headers,
                       json={"deputy": deputy, "start": today.isoformat(),
                             "end": (today + timedelta(days=days)).isoformat(), "reason": "vacation"})


class TestViews:
    def test_overview_and_views_per_role(self, client):
        pm = sign_in(client, PM)
        pid = _propose(client, pm)
        overview = client.get("/api/v1/approvals/overview", headers=pm).json()
        assert overview["views"] == ["mine"] and overview["counts"] == {"mine": 1} and not overview["approver"]
        assert client.get("/api/v1/proposals", headers=pm, params={"view": "pending"}).status_code == 403
        pmo = sign_in(client, PMO)
        overview = client.get("/api/v1/approvals/overview", headers=pmo).json()
        assert overview["views"] == ["pending", "decided", "mine", "delegations"]
        assert overview["counts"] == {"pending": 1, "decided": 0, "mine": 0} and overview["deputy_of"] is None
        inbox = client.get("/api/v1/proposals", headers=pmo, params={"view": "pending"}).json()
        row = inbox["proposals"][0]
        assert row["id"] == pid and row["deadline_label"] == "On time" and row["due_date"]
        assert row["status_label"] == "Waiting for approval" and row["budget_delta_eur"] == 0
        # the formatted cells come from the API, the same for every front end
        assert row["budget_delta_label"] == "–" and row["submitted_label"] == row["timestamp"].replace("T", " ")[:16]
        assert row["verdict_label"].endswith(row["verdict"] or "No verdict")
        assert inbox["project_names"] == {ERP: "General Ledger Consolidation"}

    def test_decided_view_and_sla_after_the_decision(self, client):
        pid = _propose(client, sign_in(client, PM))
        pmo = sign_in(client, PMO)
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo, json={}).json()["kind"] == "success"
        decided = client.get("/api/v1/proposals", headers=pmo, params={"view": "decided"}).json()["proposals"]
        assert [p["id"] for p in decided] == [pid]
        assert decided[0]["deadline_label"] == "–" and decided[0]["due_date"] is None
        assert decided[0]["decided_by_label"] == PMO

    def test_overdue_proposal_is_escalated_and_listed_first(self, client, monkeypatch):
        pm = sign_in(client, PM)
        old = _propose(client, pm)
        later = clock.now() + timedelta(days=6)
        monkeypatch.setattr(clock, "now", lambda: later)
        fresh = _propose(client, pm, end="2027-07-31")
        pmo = sign_in(client, PMO)
        overview = client.get("/api/v1/approvals/overview", headers=pmo).json()
        assert overview["deadline_events"] == {"escalated": 1, "expired": 0}
        inbox = client.get("/api/v1/proposals", headers=pmo, params={"view": "pending"}).json()["proposals"]
        assert [p["id"] for p in inbox] == [old, fresh]
        assert inbox[0]["deadline_label"] == "Overdue · escalated" and inbox[0]["escalation_level"] == 1


    def test_detail_carries_the_change_table(self, client):
        pid = _propose(client, sign_in(client, PM))
        detail = client.get(f"/api/v1/proposals/{pid}", headers=sign_in(client, PMO)).json()
        row = detail["change_rows"][0]
        assert row["Project"] == f"{ERP} – General Ledger Consolidation" and row["Change"] == "Planned end"
        assert row["Proposed"].startswith("2027-06-30") and row["Approval"] == "Four-eyes (PMO)"


class TestWithdraw:
    def test_requester_withdraws_an_open_proposal(self, client):
        pm = sign_in(client, PM)
        pid = _propose(client, pm)
        assert client.post(f"/api/v1/proposals/{pid}/withdraw", headers=sign_in(client, "Rowan Ellis"),
                           json={}).status_code == 404  # another PM does not see it
        assert client.post(f"/api/v1/proposals/{pid}/withdraw", headers=sign_in(client, PMO),
                           json={}).status_code == 403  # the PMO sees it but cannot withdraw it
        response = client.post(f"/api/v1/proposals/{pid}/withdraw", headers=pm, json={"reason": "not needed"})
        assert response.status_code == 200 and "withdrawn" in response.json()["message"]
        assert "proposal_withdrawn" in _events()
        detail = client.get(f"/api/v1/proposals/{pid}", headers=pm).json()
        assert detail["status"] == "withdrawn" and not detail["can_withdraw"]
        assert client.post(f"/api/v1/proposals/{pid}/withdraw", headers=pm, json={}).status_code == 409


class TestRevert:
    def test_revert_is_a_new_proposal_approved_by_a_second_pmo(self, client, source):
        pm = sign_in(client, PM)
        pid = _propose(client, pm)
        pmo, deputy = sign_in(client, PMO), sign_in(client, DEPUTY)
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=pmo, json={}).status_code == 200
        assert _end_date(source) == "2027-06-30"
        assert client.post(f"/api/v1/proposals/{pid}/revert", headers=pm).status_code == 403
        detail = client.get(f"/api/v1/proposals/{pid}", headers=pmo).json()
        assert detail["revert"] == {"possible": True, "existing": None, "message": None}
        reverted = client.post(f"/api/v1/proposals/{pid}/revert", headers=pmo)
        assert reverted.status_code == 201, reverted.text
        revert_id = reverted.json()["proposal"]["id"]
        assert reverted.json()["proposal"]["reverts"] == pid and _end_date(source) == "2027-06-30"
        assert client.get(f"/api/v1/proposals/{pid}", headers=pmo).json()["revert"]["existing"]["id"] == revert_id
        assert client.post(f"/api/v1/proposals/{pid}/revert", headers=pmo).status_code == 409  # only once
        # four-eyes again: the PMO who proposed the revert cannot approve it, the deputy can
        assert client.post(f"/api/v1/proposals/{revert_id}/approve", headers=pmo, json={}).status_code == 409
        assert client.post(f"/api/v1/proposals/{revert_id}/approve", headers=deputy, json={}).status_code == 200
        assert _end_date(source) == "2027-03-31"

    def test_open_proposal_cannot_be_reverted(self, client):
        pid = _propose(client, sign_in(client, PM))
        pmo = sign_in(client, PMO)
        assert client.get(f"/api/v1/proposals/{pid}", headers=pmo).json()["revert"]["possible"] is False
        assert client.post(f"/api/v1/proposals/{pid}/revert", headers=pmo).status_code == 409


class TestDelegation:
    def test_deputy_decides_on_behalf_and_the_delegators_own_in_own_name(self, client):
        pmo, deputy, pm = sign_in(client, PMO), sign_in(client, DEPUTY), sign_in(client, PM)
        granted = _delegate(client, pmo)
        assert granted.status_code == 201, granted.text
        assert "approval_delegated" in _events()
        listing = client.get("/api/v1/delegations", headers=pmo).json()
        assert listing["delegations"][0]["status_label"] == "Active" and listing["delegations"][0]["revocable"]
        assert listing["deputy_candidates"] == [DEPUTY]
        assert client.get("/api/v1/approvals/overview", headers=deputy).json()["deputy_of"]["delegator"] == PMO
        pid = _propose(client, pm)
        hints = client.get(f"/api/v1/proposals/{pid}", headers=deputy).json()["decision"]
        assert hints["acting_for"] == PMO and not hints["proxy_own"] and not hints["blocked"]
        assert client.post(f"/api/v1/proposals/{pid}/approve", headers=deputy, json={}).status_code == 200
        decided = client.get(f"/api/v1/proposals/{pid}", headers=pm).json()
        assert decided["on_behalf_of"] == PMO and decided["decided_by_label"] == f"{DEPUTY} (deputy of {PMO})"
        # the delegator's own proposal: the deputy decides it in their own name
        own = client.post("/api/v1/proposals", headers=pmo,
                          json={"run_id": test_api.what_if(client, pmo, 10)["run_id"]}).json()["id"]
        hints = client.get(f"/api/v1/proposals/{own}", headers=deputy).json()["decision"]
        assert hints["proxy_own"] and hints["acting_for"] is None
        assert client.get(f"/api/v1/proposals/{own}", headers=pmo).json()["decision"]["own"]

    def test_delegation_rules_and_revoke(self, client):
        pmo = sign_in(client, PMO)
        assert _delegate(client, pmo, deputy=PM).status_code == 422  # only another PMO deputises
        assert _delegate(client, pmo, deputy=PMO).status_code == 422
        assert _delegate(client, pmo, days=400).status_code == 422
        assert _delegate(client, sign_in(client, PM)).status_code == 403
        assert client.get("/api/v1/delegations", headers=sign_in(client, PM)).status_code == 403
        did = _delegate(client, pmo).json()["delegation"]["delegation_id"]
        assert _delegate(client, pmo).status_code == 422  # overlapping
        assert client.post(f"/api/v1/delegations/{did}/revoke", headers=pmo).status_code == 200
        assert "approval_delegation_revoked" in _events()
        assert client.post(f"/api/v1/delegations/{did}/revoke", headers=pmo).status_code == 409
        assert client.get("/api/v1/delegations", headers=pmo).json()["delegations"][0]["status_label"] == "Revoked"
        assert client.get("/api/v1/approvals/overview", headers=sign_in(client, DEPUTY)).json()["deputy_of"] is None
