"""Scheduler scripts of the pilot: the approval deadline run, the data backup with hash-chain check and pruning."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.core import backup, clock, logs, proposals
from app.core.security import ROLE_PM
from scripts import approval_deadlines, backup_data

CUT = [{"project_id": "P-1", "field": "budget_cost_eur", "old": 800000, "new": 640000}]


def test_the_deadline_script_escalates_without_anyone_opening_the_inbox(monkeypatch, capsys):
    t0 = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "now", lambda: t0)
    entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "Cut", "", "GO", CUT)
    monkeypatch.setattr(clock, "now", lambda: t0 + timedelta(days=60))
    assert approval_deadlines.main([]) == 0
    out = capsys.readouterr().out
    assert entry["id"] in out and "deadline event(s) recorded" in out
    assert approval_deadlines.main([]) == 0 and "0 deadline event(s)" in capsys.readouterr().out  # idempotent


def test_a_backup_copies_the_data_folder_checks_the_chain_and_keeps_the_newest(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(logs, "DATA_DIR", data)
    monkeypatch.setattr(logs, "AUDIT_LOG", data / "decision_log.jsonl")
    logs.append_jsonl(logs.AUDIT_LOG, {"event": "test", "user": "PMO-ADMIN"})
    logs.append_jsonl(logs.AUDIT_LOG, {"event": "test", "user": "PMO-DEPUTY"})
    (data / "pending_changes.jsonl").write_text("{}\n", encoding="utf-8")
    (data / "pending_changes.jsonl.lock").write_text("", encoding="utf-8")
    target = tmp_path / "backups"
    stamps = iter(datetime(2026, 10, 6, 1, minute, tzinfo=timezone.utc) for minute in range(10))
    monkeypatch.setattr(clock, "now", lambda: next(stamps))
    results = [backup.create_backup(target, keep=2) for _ in range(3)]
    kept = sorted(p.name for p in target.iterdir())
    assert kept == [results[1].folder.name, results[2].folder.name] and results[2].removed == [results[0].folder]
    copied = results[2].folder / "data"
    assert (copied / "pending_changes.jsonl").exists() and not (copied / "pending_changes.jsonl.lock").exists()
    assert results[2].chain["ok"] and "hash chain intact" in results[2].summary()


def test_verify_reports_a_broken_chain_with_exit_code_one(monkeypatch, tmp_path, capsys):
    record = {"event": "x", "prev_hash": "0" * 64, "hash": "not-the-hash"}
    (tmp_path / "decision_log.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert backup_data.main(["--verify", str(tmp_path)]) == 1
    assert "BROKEN at line 1" in capsys.readouterr().out
