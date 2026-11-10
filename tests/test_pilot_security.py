"""Pilot security hardening: one-line log records, strict user file, global sign-in budget, user-file cache, role
checks in the core, write evidence, protected snapshots and the user purge."""
from __future__ import annotations

import json
import logging
import time

import pytest

from app.core import auth, logs
from app.core.chat_store import append_messages, chat_path
from app.core.proposals import submit_proposal
from app.core.purge import PurgeError, pseudonym, purge_user
from app.core.runtime_config import get_runtime_config, switch_environment, update_runtime_config
from app.core.scenario_store import list_scenarios, save_scenario
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO, log_safe, log_security_event
from app.core.snapshots import DECISION_AUDIT_EVENTS, SnapshotError, restore_snapshot

FAST = auth.MIN_ITERATIONS


def _user_file(tmp_path, monkeypatch, users):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    monkeypatch.setenv("AUTH_USERS_FILE", str(path))
    return path


@pytest.fixture(scope="module")
def pw_hash():
    return auth.hash_password("pw", FAST)


class TestLogInjection:
    def test_a_user_id_with_a_line_break_gives_exactly_one_record(self, caplog):
        with caplog.at_level(logging.INFO, logger="derkurator.security"):
            log_security_event("Login failed", user="mallory\nSECURITY | Login ok | user=PMO-ADMIN\x1b[2J")
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "\n" not in message and "\x1b" not in message

    def test_values_are_capped(self):
        assert len(log_safe("x" * 1000)) == 200


class TestUserFile:
    def test_duplicate_user_id_is_refused(self, tmp_path, monkeypatch, pw_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": "Raman", "role": ROLE_PM, "password_hash": pw_hash},
                                           {"user_id": "raman ", "role": ROLE_PMO, "password_hash": pw_hash}])
        with pytest.raises(auth.AuthConfigError, match="appears twice"):
            auth.load_users("DEV")

    def test_empty_user_id_is_refused(self, tmp_path, monkeypatch, pw_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": " ", "role": ROLE_PM, "password_hash": pw_hash}])
        with pytest.raises(auth.AuthConfigError, match="empty user_id"):
            auth.load_users("DEV")

    def test_file_is_read_again_only_after_a_change(self, tmp_path, monkeypatch, pw_hash):
        path = _user_file(tmp_path, monkeypatch, [{"user_id": "a", "role": ROLE_PM, "password_hash": pw_hash}])
        assert set(auth.load_users("DEV")) == {"a"}
        reads = []
        real = auth._read_rows
        monkeypatch.setattr(auth, "_read_rows", lambda p: reads.append(p) or real(p))
        auth.load_users("DEV")
        assert reads == []  # cached
        path.write_text(json.dumps({"users": [{"user_id": "bb", "role": ROLE_PM, "password_hash": pw_hash}]}))
        assert set(auth.load_users("DEV")) == {"bb"}


class TestGlobalAttemptBudget:
    def test_a_flood_of_attempts_is_cut_off(self, tmp_path, monkeypatch, pw_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": "a", "role": ROLE_PM, "password_hash": pw_hash}])
        update_runtime_config(auth={"max_attempts_per_minute": 5})
        monkeypatch.setattr(auth, "verify_password", lambda *_: False)  # the budget, not the hashing, is under test
        now = time.time()
        answers = [auth.sign_in(f"user{i}", "wrong", now=now).message for i in range(50)]
        assert answers[:5] == ["Unknown user or wrong password."] * 5
        assert set(answers[5:]) == {"Too many sign-in attempts – try again in a minute."}
        assert auth.sign_in("user0", "wrong", now=now + 61).message == "Unknown user or wrong password."

    def test_fifty_attempts_stay_fast_once_the_budget_is_spent(self, tmp_path, monkeypatch, pw_hash):
        _user_file(tmp_path, monkeypatch, [{"user_id": "a", "role": ROLE_PM, "password_hash": pw_hash}])
        update_runtime_config(auth={"max_attempts_per_minute": 5})
        now = time.time()
        for i in range(5):
            auth.sign_in(f"user{i}", "wrong", now=now)
        started = time.perf_counter()
        for i in range(50):
            auth.sign_in(f"other{i}", "wrong", now=now)
        assert time.perf_counter() - started < 1.0  # no password hash once the budget is spent


class TestCoreRoleChecks:
    @pytest.mark.parametrize("role", [ROLE_PM, ROLE_PMO, ""])
    def test_only_the_system_admin_changes_settings(self, role):
        with pytest.raises(PermissionError):
            update_runtime_config(temperature=0.5, role=role)
        with pytest.raises(PermissionError):
            switch_environment("TEST", role=role)
        with pytest.raises(PermissionError):
            restore_snapshot("x", logs.DATA_DIR / "portfolio.json", user="u", role=role)

    def test_system_admin_passes(self):
        assert update_runtime_config(temperature=0.5, role=ROLE_ADMIN).temperature == 0.5
        assert switch_environment("TEST", role=ROLE_ADMIN).environment == "TEST"
        with pytest.raises(SnapshotError):  # past the role check: the snapshot does not exist
            restore_snapshot("missing", logs.DATA_DIR / "portfolio.json", role=ROLE_ADMIN)
        assert get_runtime_config().environment == "TEST"


class TestWriteEvidence:
    def test_snapshots_of_every_write_are_protected(self):
        assert {"project_edited", "data_write", "scenario_applied", "proposal_approved"} <= set(DECISION_AUDIT_EVENTS)

    def test_a_failed_write_is_recorded(self, dataset_factory, project_factory, tmp_path, monkeypatch):
        from app.data_adapters.json_adapter import JsonPortfolioAdapter

        source = tmp_path / "portfolio.json"
        source.write_text(json.dumps({"Projects": [project_factory(project_number="P-1")]}), encoding="utf-8")
        adapter = JsonPortfolioAdapter(source)

        def broken(_updates):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(adapter, "_write_project_fields", broken)
        with pytest.raises(RuntimeError):
            adapter.apply_changes([{"project_id": "P-1", "field": "project_name", "old": None, "new": "X"}],
                                  user="PMO-ADMIN", role=ROLE_PMO, check_conflicts=False, reason="test")
        events = [r["event"] for r in logs.read_jsonl(logs.AUDIT_LOG)[0]]
        assert events[-2:] == ["data_write", "data_write_failed"]


class TestPurgeUser:
    def _fill(self):
        append_messages(ROLE_PM, "Priya Raman", [{"role": "user", "content": "cut ERP"}])
        append_messages(ROLE_PM, "Rowan Ellis", [{"role": "user", "content": "other"}])
        save_scenario("A", "Priya Raman", ROLE_PM, ["cut"], None, [{"project_id": "P", "field": "f", "new": 1}], {})
        save_scenario("B", "Rowan Ellis", ROLE_PM, ["cut"], None, [{"project_id": "P", "field": "f", "new": 1}], {})
        submit_proposal("Priya Raman", ROLE_PM, "Master data change", "Requested by Priya Raman.", "GO",
                        [{"project_id": "P", "field": "budget_cost_eur", "old": 1, "new": 2}])
        archive = logs.ARCHIVE_DIR / "demo_reset_1"
        archive.mkdir(parents=True)
        (archive / "pending_changes.jsonl").write_text(json.dumps({"requester": "Priya Raman", "id": "x"}) + "\n")

    def test_chats_and_scenarios_removed_ids_pseudonymised(self):
        self._fill()
        result = purge_user("Priya Raman", "SYS-ADMIN", ROLE_ADMIN)
        assert result == {"chats": 1, "scenarios": 1, "proposals": 1, "incidents": 0, "notifications": 0,
                          "archived": 1}
        assert not chat_path(ROLE_PM, "Priya Raman").exists() and chat_path(ROLE_PM, "Rowan Ellis").exists()
        assert list_scenarios(ROLE_PM, "Priya Raman") == [] and len(list_scenarios(ROLE_PM, "Rowan Ellis")) == 1
        stores = [logs.PROPOSALS_LOG, logs.ARCHIVE_DIR / "demo_reset_1" / "pending_changes.jsonl"]
        for path in stores:
            text = path.read_text(encoding="utf-8")
            assert "Priya Raman" not in text and pseudonym("Priya Raman") in text

    def test_only_the_admin_and_never_the_own_account(self):
        with pytest.raises(PermissionError):
            purge_user("Priya Raman", "PMO-ADMIN", ROLE_PMO)
        with pytest.raises(PurgeError):
            purge_user("SYS-ADMIN", "SYS-ADMIN", ROLE_ADMIN)
