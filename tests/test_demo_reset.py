"""Reset demo data: back to the first snapshot, proposals/chats/scenarios archived, never in PROD."""
from __future__ import annotations

import json

import pytest

from app.core import logs, snapshots
from app.core.demo_reset import DemoResetError, reset_demo_data


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "ARCHIVE_DIR", tmp_path / "archive")
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"budget": 100}), encoding="utf-8")
    return path


def test_reset_restores_the_first_snapshot_and_archives_runtime_state(source):
    snapshots.create_snapshot(source, "approve", "PMO")  # taken before the first write: the shipped state
    source.write_text(json.dumps({"budget": 80}), encoding="utf-8")
    snapshots.create_snapshot(source, "approve", "PMO")
    source.write_text(json.dumps({"budget": 60}), encoding="utf-8")
    logs.PROPOSALS_LOG.write_text('{"id": "p1"}\n', encoding="utf-8")
    result = reset_demo_data(source, "DEV", "SYSTEM-ADMIN")
    assert json.loads(source.read_text(encoding="utf-8")) == {"budget": 100}
    assert result["previous_state"] and "pending_changes.jsonl" in result["archived"]
    assert not logs.PROPOSALS_LOG.exists()
    assert list((logs.ARCHIVE_DIR).glob("demo_reset_*/pending_changes.jsonl"))


def test_first_snapshot_survives_pruning(source):
    first = snapshots.create_snapshot(source, "approve", "PMO")
    for _ in range(3):
        snapshots.create_snapshot(source, "approve", "PMO", prune_after=False)
    snapshots.prune(keep=1)
    assert snapshots.get_snapshot(first["id"]) is not None


def test_reset_is_refused_in_prod(source):
    with pytest.raises(DemoResetError, match="not available in PROD"):
        reset_demo_data(source, "PROD", "SYSTEM-ADMIN")
