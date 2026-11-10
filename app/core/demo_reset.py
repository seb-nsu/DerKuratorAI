"""Reset of a demo instance to the state it started with – DEV and TEST only, never PROD.

The portfolio file goes back to its first snapshot (taken before the first write, see snapshots.first_snapshot; the
current state is snapshotted first, so the reset can be undone). Open proposals, chats and saved scenarios are moved
to data/archive/demo_reset_<timestamp>/ – nothing is deleted. The caller rebuilds the graph and writes the audit record.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.core import clock, logs
from app.core.snapshots import SnapshotError, first_snapshot, restore_snapshot

RESET_ENVIRONMENTS = ("DEV", "TEST")


class DemoResetError(RuntimeError):
    pass


def reset_demo_data(source: Path | None, environment: str, user: str | None) -> dict[str, Any]:
    """Returns {"restored": snapshot id or None, "previous_state": snapshot id or None, "archived": [names]}."""
    if environment not in RESET_ENVIRONMENTS:
        raise DemoResetError(f"Reset demo data is not available in {environment}.")
    result: dict[str, Any] = {"restored": None, "previous_state": None, "archived": []}
    if source is not None:
        first = first_snapshot(Path(source).name)
        if first is not None:
            try:
                restored = restore_snapshot(first["id"], Path(source), user=user)
            except SnapshotError as exc:
                raise DemoResetError(str(exc)) from exc
            result["restored"], result["previous_state"] = first["id"], restored["previous_state"]["id"]
    target = Path(logs.ARCHIVE_DIR) / f"demo_reset_{clock.now():%Y%m%d_%H%M%S}"
    for path in (Path(logs.PROPOSALS_LOG), Path(logs.SCENARIOS_LOG), Path(logs.NOTIFY_OUTBOX), Path(logs.CHAT_DIR)):
        if path.exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target / path.name))
            result["archived"].append(path.name)
    return result
