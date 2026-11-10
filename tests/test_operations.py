"""Operational robustness: the tamper-evident decision log (hash chain), state shared by several worker processes
(sign-in lockout, settings saved by another worker) and re-reading .env without a restart."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import auth, config, logs, runtime_config, shared_state


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestHashChain:
    def test_every_record_points_at_its_predecessor(self):
        for i in range(3):
            logs.append_jsonl(logs.AUDIT_LOG, {"event": "e", "n": i})
        records = _records(logs.AUDIT_LOG)
        assert records[0]["prev_hash"] == logs.GENESIS
        assert [r["prev_hash"] for r in records[1:]] == [r["hash"] for r in records[:-1]]
        assert logs.verify_chain() == {"ok": True, "records": 3, "chained": 3, "unchained": 0, "broken_at": None,
                                       "reason": ""}

    @pytest.mark.parametrize(("tamper", "reason"), [
        (lambda lines: [lines[0], lines[1].replace('"n": 1', '"n": 9'), lines[2]], "content changed"),
        (lambda lines: [lines[0], lines[2]], "predecessor does not match"),
        (lambda lines: [lines[1], lines[0], lines[2]], "predecessor does not match"),
    ], ids=["changed", "removed", "reordered"])
    def test_a_changed_removed_or_reordered_record_breaks_the_chain(self, tamper, reason):
        for i in range(3):
            logs.append_jsonl(logs.AUDIT_LOG, {"event": "e", "n": i})
        lines = logs.AUDIT_LOG.read_text(encoding="utf-8").splitlines()
        logs.AUDIT_LOG.write_text("\n".join(tamper(lines)) + "\n", encoding="utf-8")
        check = logs.verify_chain()
        assert not check["ok"] and reason in check["reason"]

    def test_older_records_without_hash_come_first_and_are_counted(self):
        logs.AUDIT_LOG.write_text(json.dumps({"event": "old"}) + "\n", encoding="utf-8")
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "new"})
        check = logs.verify_chain()
        assert check["ok"] and check["unchained"] == 1 and check["chained"] == 1

    def test_archiving_links_the_new_log_to_the_archive(self, tmp_path):
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "before"})
        last = _records(logs.AUDIT_LOG)[-1]["hash"]
        archive = logs.archive_and_clear(logs.AUDIT_LOG, tmp_path / "archive")
        [anchor] = _records(logs.AUDIT_LOG)
        assert anchor["event"] == "log_archived" and anchor["archive"] == archive.name and anchor["prev_hash"] == last
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "after"})
        assert logs.verify_chain()["ok"] and logs.verify_chain(archive)["ok"]

    def test_other_logs_are_not_chained(self):
        logs.append_jsonl(logs.SHADOW_LOG, {"event": "shadow"})
        assert "hash" not in _records(logs.SHADOW_LOG)[0]


_WORKER = """
import sys
from pathlib import Path
from app.core import logs
logs.AUDIT_LOG = Path(sys.argv[1])
for i in range(20):
    logs.append_jsonl(logs.AUDIT_LOG, {"event": "w", "worker": sys.argv[2], "n": i})
"""


def test_two_processes_appending_keep_one_chain():
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    workers = [subprocess.Popen([sys.executable, "-c", _WORKER, str(logs.AUDIT_LOG), str(w)], env=env,
                                stderr=subprocess.PIPE, text=True) for w in (1, 2)]
    errors = [w.communicate(timeout=120)[1] for w in workers]
    assert [w.returncode for w in workers] == [0, 0], errors
    check = logs.verify_chain()
    assert check["ok"] and check["chained"] == 40


class TestSharedState:
    def test_a_lockout_counts_across_workers(self, monkeypatch):
        """Failed attempts land in the shared state file – a second worker process sees the lock."""
        runtime_config.update_runtime_config(auth={"max_failed_attempts": 2, "lockout_minutes": 5})
        auth.sign_in("PMO-ADMIN", "wrong-1", now=1000.0)
        auth.sign_in("PMO-ADMIN", "wrong-2", now=1001.0)
        state = shared_state.read(auth.SIGN_IN_STATE)
        assert list(state["locked_until"].values()) == [1001.0 + 300]
        assert "PMO-ADMIN" not in json.dumps(state) and "pmo-admin" not in json.dumps(state)  # no names on disk
        result = auth.sign_in("PMO-ADMIN", "kurator-demo", now=1002.0)
        assert not result.ok and "Too many failed attempts" in result.message

    def test_settings_saved_by_another_worker_are_picked_up(self, monkeypatch):
        monkeypatch.setattr(runtime_config, "CHECK_INTERVAL_S", 0.0)
        cfg = runtime_config.get_runtime_config()
        data = json.loads(runtime_config.env_config_path(cfg.environment).read_text(encoding="utf-8"))
        data["temperature"] = 0.7 if cfg.temperature != 0.7 else 0.3
        runtime_config.env_config_path(cfg.environment).write_text(json.dumps(data), encoding="utf-8")
        os.utime(runtime_config.env_config_path(cfg.environment), ns=(1, 1))  # a different stamp, whatever the clock
        reloaded = runtime_config.get_runtime_config()
        assert reloaded.temperature == data["temperature"]
        assert reloaded.anthropic_api_key == cfg.anthropic_api_key  # keys stay in memory


class TestEnvReload:
    def test_new_and_removed_values_without_a_restart(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        monkeypatch.setattr(config, "ENV_FILE", env_file)
        monkeypatch.setattr(config, "_from_env_file", set())
        monkeypatch.setattr(config, "_REAL_ENV", frozenset({"KEEP_ME"}))
        monkeypatch.setenv("KEEP_ME", "from the shell")
        monkeypatch.delenv("DK_TEST_VALUE", raising=False)
        env_file.write_text("DK_TEST_VALUE=one\nKEEP_ME=from the file\n", encoding="utf-8")
        assert config.reload_env() == ["DK_TEST_VALUE"]
        assert os.environ["DK_TEST_VALUE"] == "one" and os.environ["KEEP_ME"] == "from the shell"
        env_file.write_text("KEEP_ME=from the file\n", encoding="utf-8")
        assert config.reload_env() == ["DK_TEST_VALUE"] and "DK_TEST_VALUE" not in os.environ
