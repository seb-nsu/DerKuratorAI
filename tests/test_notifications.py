"""Notifications by mail and Microsoft Teams: event -> outbox -> delivery with a fake SMTP server and a fake webhook,
retry with backoff, one message per event and recipient, texts in the recipient's view, the approval unaffected by a
failing delivery, and the Admin Console's status and test action (System Admin only)."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import approval_flow
from app.core import clock, logs, notifications, proposals
from app.core.config import PROJECT_ROOT
from app.core.security import ROLE_BOARD, ROLE_PM, ROLE_PMO
from tests import test_api, test_api_admin
from tests.conftest import make_dataset

source, client = test_api.source, test_api.client
admin_account, admin = test_api_admin.admin_account, test_api_admin.admin
PM, PMO, ERP = test_api.PM, test_api.PMO, test_api.ERP
EMAILS = {PMO: "pmo.admin@example.com", "PMO-DEPUTY": "pmo.deputy@example.com", PM: "priya.raman@example.com",
          "BOARD-CHAIR": "board.chair@example.com"}
SMTP_PASSWORD = "smtp-secret-password-123"
WEBHOOK = "https://example.webhook.office.com/webhookb2/secret-token-xyz"


class FakeSMTP:
    """smtplib.SMTP stand-in: records the mails; `fail` makes the next sends raise."""
    sent: list = []
    logins: list = []
    fail = 0

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, message):
        if FakeSMTP.fail:
            FakeSMTP.fail -= 1
            raise OSError(f"Connection refused by mail.example.com ({SMTP_PASSWORD})")
        FakeSMTP.sent.append(message)


@pytest.fixture
def mail(source, tmp_path, monkeypatch):
    """Notifications on with SMTP, a PMO mailbox and accounts with mail addresses; delivery runs in the test."""
    demo = json.loads((PROJECT_ROOT / "data" / "demo_users.json").read_text(encoding="utf-8"))
    for user in demo["users"]:
        if user["user_id"] in EMAILS:
            user["email"] = EMAILS[user["user_id"]]
    users = tmp_path / "users.json"
    users.write_text(json.dumps(demo), encoding="utf-8")
    for key, value in {"AUTH_USERS_FILE": str(users), "NOTIFY_ENABLED": "true", "NOTIFY_SMTP_HOST": "mail.example.com",
                       "NOTIFY_SMTP_FROM": "derkurator@example.com", "NOTIFY_SMTP_USER": "derkurator",
                       "NOTIFY_SMTP_PASSWORD": SMTP_PASSWORD, "NOTIFY_PMO_TO": "pmo-team@example.com",
                       "NOTIFY_BOARD_TO": "board@example.com", "APP_BASE_URL": "https://kurator.example.com/"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(FakeSMTP, "sent", [])
    monkeypatch.setattr(FakeSMTP, "logins", [])
    monkeypatch.setattr(FakeSMTP, "fail", 0)
    monkeypatch.setitem(notifications._background, "on", False)  # the test calls deliver() itself
    return FakeSMTP


@pytest.fixture
def webhook(monkeypatch):
    calls: list = []
    answer = {"code": 202}

    def post(url, payload):
        calls.append((url, payload))
        return answer["code"]

    monkeypatch.setenv("NOTIFY_TEAMS_PMO_URL", WEBHOOK)
    monkeypatch.setattr(notifications, "_post_json", post)
    return calls, answer


def outbox() -> list[dict]:
    try:
        return logs.read_jsonl(logs.NOTIFY_OUTBOX)[0]
    except FileNotFoundError:
        return []


def budget_change(delta: float = 10_000) -> list[dict]:
    from app.core.config import get_settings

    old = test_api.budget(get_settings().json_path)
    return [{"project_id": ERP, "field": "budget_cost_eur", "old": old, "new": old + delta}]


def submit(changes: list[dict] | None = None, **kwargs) -> dict:
    return proposals.submit_proposal(PM, ROLE_PM, "Raise the ERP budget", "paper", "CONDITIONAL GO",
                                     changes or budget_change(), **kwargs)


def to(mails) -> list[str]:
    return sorted(str(m["To"]) for m in mails)


class TestEventToDelivery:
    def test_off_by_default_nothing_is_queued(self, source):
        submit()
        assert not logs.NOTIFY_OUTBOX.exists()

    def test_new_proposal_reaches_the_pmo_accounts_and_mailbox_with_link(self, mail):
        entry = submit()
        assert [r["type"] for r in outbox()] == ["event"]  # recorded with the proposal, delivered afterwards
        result = notifications.deliver()
        assert result == {"queued": 3, "sent": 3, "failed": 0}
        assert to(mail.sent) == sorted([EMAILS[PMO], EMAILS["PMO-DEPUTY"], "pmo-team@example.com"])
        body = mail.sent[0].get_content()
        assert "General Ledger Consolidation" in mail.sent[0]["Subject"] and "Waiting for your decision" in body + \
            mail.sent[0]["Subject"]
        assert f"https://kurator.example.com/approvals?proposal={entry['id']}" in body
        assert "Budget change: +€10,000" in body and "Requested by: Priya Raman" in body
        assert SMTP_PASSWORD not in body and set(mail.logins) == {("derkurator", SMTP_PASSWORD)}
        assert notifications.deliver()["sent"] == 0  # nothing twice

    def test_decision_goes_back_to_the_requester(self, mail):
        entry = submit()
        outcome = approval_flow.approve_proposal(entry["id"], PMO, ROLE_PMO, comment="ok")
        assert outcome.ok, outcome.message
        notifications.deliver()
        decided = [m for m in mail.sent if "Your proposal was approved" in m["Subject"]]
        assert to(decided) == [EMAILS[PM]]
        assert "Decided by: PMO-ADMIN" in decided[0].get_content()
        # the "waiting" mails of a proposal decided before the delivery ran are not sent any more
        assert not any("Waiting" in m["Subject"] for m in mail.sent)

    def test_rejected_plan_is_sent_back_and_plans_go_to_the_board(self, mail):
        plan = proposals.submit_proposal(PMO, ROLE_PMO, "Approve annual plan S1", "paper", "GO",
                                         [{"project_id": None, "table": "Planning_Scenarios", "key": "S1",
                                           "field": "scenario_status", "old": "Submitted", "new": "Approved"}],
                                         kind=proposals.PLAN)
        notifications.deliver()
        assert to(mail.sent) == sorted([EMAILS["BOARD-CHAIR"], "board@example.com"])
        assert "/planning" in mail.sent[0].get_content()
        proposals.decide(plan["id"], proposals.REJECTED, "BOARD-CHAIR", ROLE_BOARD, comment="Not this year")
        notifications.deliver()
        back = mail.sent[-1]
        assert str(back["To"]) == EMAILS[PMO] and "sent back" in back["Subject"]
        assert "Comment: Not this year" in back.get_content()

    def test_escalation_and_expiry_from_the_deadline_check(self, mail, monkeypatch):
        entry = submit()
        notifications.deliver()
        mail.sent.clear()
        start = clock.now()
        monkeypatch.setattr(clock, "now", lambda: start + timedelta(days=6))
        proposals.enforce_deadlines()
        proposals.enforce_deadlines()  # idempotent: one event
        notifications.deliver()
        assert all("Overdue (escalation level 1)" in m["Subject"] for m in mail.sent) and len(mail.sent) == 3
        mail.sent.clear()
        monkeypatch.setattr(clock, "now", lambda: start + timedelta(days=60))
        from scripts import approval_deadlines

        assert approval_deadlines.main([]) == 0  # queues and delivers the expiry
        assert proposals.get_proposal(entry["id"])["status"] == proposals.EXPIRED
        assert EMAILS[PM] in to(mail.sent) and all("Expired" in m["Subject"] for m in mail.sent)


class TestRetryAndDedup:
    def test_failed_delivery_is_retried_with_backoff_then_given_up(self, mail, monkeypatch):
        monkeypatch.setenv("NOTIFY_MAX_ATTEMPTS", "2")
        monkeypatch.setenv("NOTIFY_PMO_TO", "")
        monkeypatch.setattr(notifications, "_accounts", lambda: [])
        submit()
        mail.fail = 5
        now = clock.now()
        assert notifications.deliver(now=now)["failed"] == 0  # no recipient at all: nothing to send
        monkeypatch.setenv("NOTIFY_PMO_TO", "pmo-team@example.com")
        submit(budget_change(20_000))
        first = notifications.deliver(now=now)
        assert first == {"queued": 1, "sent": 0, "failed": 1}
        status = notifications.status()
        assert status["queued"] == 1 and status["retrying"] == 1
        assert SMTP_PASSWORD not in json.dumps(status) and "***" in status["last_error"]["error"]
        assert notifications.deliver(now=now + timedelta(seconds=30))["failed"] == 0  # not due yet
        assert notifications.deliver(now=now + timedelta(minutes=2))["failed"] == 1  # second and last attempt
        status = notifications.status()
        assert status["failed"] == 1 and status["queued"] == 0
        assert notifications.deliver(now=now + timedelta(days=1)) == {"queued": 0, "sent": 0, "failed": 0}

    def test_retry_succeeds(self, mail):
        submit()
        mail.fail = 1
        now = clock.now()
        assert notifications.deliver(now=now)["failed"] == 1
        assert notifications.deliver(now=now + timedelta(minutes=5))["sent"] == 1
        assert len(mail.sent) == 3 and notifications.status()["sent"] == 3

    def test_one_message_per_event_and_recipient(self, mail, monkeypatch):
        monkeypatch.setenv("NOTIFY_PMO_TO", f"{EMAILS[PMO]}, pmo-team@example.com")  # an account is also a mailbox
        entry = submit()
        notifications.record_event("submitted", entry["id"])  # the same event again
        notifications.deliver()
        notifications.deliver()
        assert to(mail.sent) == sorted([EMAILS[PMO], EMAILS["PMO-DEPUTY"], "pmo-team@example.com"])
        assert sum(r["type"] == "event" for r in outbox()) == 1


class TestTeams:
    def test_webhook_gets_an_adaptive_card_and_a_failure_hides_the_url(self, mail, webhook, monkeypatch):
        calls, answer = webhook
        entry = submit()
        notifications.deliver()
        assert len(calls) == 1 and calls[0][0] == WEBHOOK
        card = calls[0][1]["attachments"][0]["content"]
        assert card["actions"][0]["url"].endswith(f"/approvals?proposal={entry['id']}")
        assert WEBHOOK not in json.dumps(outbox())  # the outbox names the role channel, never the URL
        answer["code"] = 500
        submit(budget_change(30_000))
        assert notifications.deliver()["failed"] == 1
        error = notifications.status()["last_error"]
        assert error["channel"] == "teams" and "HTTP 500" in error["error"] and "secret-token" not in error["error"]


class TestVisibility:
    def test_a_project_manager_never_learns_foreign_project_ids(self):
        ds = make_dataset([{"project_number": "P-1", "project_name": "Own", "project_manager": "Priya Raman"},
                           {"project_number": "P-2", "project_name": "Locked downstream", "project_manager": "Alex"}])
        proposal = {"id": "abc", "status": "approved", "verdict": "GO", "decided_by": PMO, "projects": ["P-1", "P-2"],
                    "changes": [{"project_id": "P-1", "field": "budget_cost_eur", "old": 100, "new": 150},
                                {"project_id": "P-2", "field": "budget_cost_eur", "old": 100, "new": 900}]}
        pm = notifications.compose("decided", proposal, notifications.Recipient("mail", "x@y.z", ROLE_PM, PM), ds)
        text = pm["subject"] + pm["text"]
        assert "Own" in text and "+€50" in text
        assert "P-2" not in text and "Locked downstream" not in text and "€900" not in text
        pmo = notifications.compose("decided", proposal, notifications.Recipient("mail", "x@y.z", ROLE_PMO, PMO), ds)
        assert "P-2" in pmo["text"] and "+€850" in pmo["text"]
        # without the data a Project Manager's message names no project (deny by default)
        blind = notifications.compose("decided", proposal, notifications.Recipient("mail", "x@y.z", ROLE_PM, PM))
        assert "P-1" not in blind["text"] and "P-2" not in blind["text"]


class TestApprovalNeverBlocked:
    def test_approval_succeeds_when_delivery_fails(self, mail, monkeypatch):
        mail.fail = 99
        entry = submit()
        monkeypatch.setattr(notifications, "kick", notifications.deliver)  # delivery right after the write
        outcome = approval_flow.approve_proposal(entry["id"], PMO, ROLE_PMO)
        assert outcome.ok and proposals.get_proposal(entry["id"])["status"] == proposals.APPROVED
        assert notifications.status()["queued"] >= 1 and not mail.sent

    def test_approval_succeeds_when_the_outbox_is_broken(self, mail, monkeypatch):
        entry = submit()

        def broken(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(notifications, "_append", broken)
        outcome = approval_flow.approve_proposal(entry["id"], PMO, ROLE_PMO)
        assert outcome.ok and proposals.get_proposal(entry["id"])["status"] == proposals.APPROVED


class TestAdmin:
    def test_status_and_test_action_for_the_admin_only(self, mail, webhook, client, admin):
        submit()
        notifications.deliver()
        view = client.get("/api/v1/admin/notifications", headers=admin)
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["enabled"] and body["smtp"]["configured"] and body["smtp"]["password_set"]
        assert body["teams_pmo"] and not body["teams_board"] and body["sent"] == 4
        assert body["accounts_with_email"] == 4 and body["pmo_mailboxes"] == 1
        for secret in (SMTP_PASSWORD, WEBHOOK, "ERP", EMAILS[PM], "pmo-team@example.com"):
            assert secret not in view.text, secret
        test = client.post("/api/v1/admin/notifications/test", headers=admin, json={"to": "ops@example.com"})
        assert test.status_code == 200 and test.json()["kind"] == "success", test.text
        assert str(mail.sent[-1]["To"]) == "ops@example.com" and test.json()["channels"] == ["mail", "teams"]
        records, _ = logs.read_jsonl(logs.AUDIT_LOG)
        audited = [r for r in records if r.get("event") == "notification_test_sent"]
        assert audited and "ops@example.com" not in json.dumps(audited)
        bad = client.post("/api/v1/admin/notifications/test", headers=admin, json={"to": "not-an-address"})
        assert bad.status_code == 422
        for user in (PM, PMO, "BOARD-CHAIR"):
            headers = test_api.sign_in(client, user)
            assert client.get("/api/v1/admin/notifications", headers=headers).status_code == 403
            assert client.post("/api/v1/admin/notifications/test", headers=headers, json={}).status_code == 403

    def test_test_without_a_channel_is_an_error(self, source, client, admin):
        response = client.post("/api/v1/admin/notifications/test", headers=admin, json={})
        assert response.status_code == 409 and "No channel" in response.json()["message"]
        assert client.get("/api/v1/admin/notifications", headers=admin).json()["enabled"] is False


def test_purge_removes_the_users_mails(mail):
    from app.core.purge import purge_user
    from app.core.security import ROLE_ADMIN

    entry = submit()
    approval_flow.approve_proposal(entry["id"], PMO, ROLE_PMO)
    notifications.deliver()
    assert EMAILS[PM] in logs.NOTIFY_OUTBOX.read_text(encoding="utf-8")
    result = purge_user(PM, "ADMIN", ROLE_ADMIN)
    assert result["notifications"] >= 1
    assert EMAILS[PM] not in logs.NOTIFY_OUTBOX.read_text(encoding="utf-8")
    assert PM not in logs.NOTIFY_OUTBOX.read_text(encoding="utf-8")
