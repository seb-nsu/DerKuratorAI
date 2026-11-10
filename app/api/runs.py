"""Results of what-if runs, held server-side until their owner submits them as a proposal: the client names a run, it
never sends a change set of its own. Kept in the API state store (`app/api/state_store.py`: this process's memory, or
files on the shared data volume with `STATE_STORE=file`, so every worker answers for a run another one started); a
run expires with the session timeout."""
from __future__ import annotations

import time
from typing import Any

from app.api.state_store import CAPS, store
from app.core.runtime_config import get_runtime_config

SPACE = "runs"
LATEST = "latest_run"  # user id -> their latest run: only that step can be applied or saved (as in Streamlit)
MAX_RUNS = CAPS[SPACE]


def _ttl() -> float:
    return get_runtime_config().auth.session_timeout_minutes * 60


def put(run_id: str, entry: dict[str, Any]) -> None:
    now = time.time()
    store().put(SPACE, run_id, {**entry, "stored_at": now}, now + _ttl())


def get(run_id: str, user_id: str) -> dict[str, Any] | None:
    """The run if it exists, belongs to `user_id` and has not expired."""
    entry = store().get(SPACE, run_id)
    return entry if entry is not None and entry.get("user_id") == user_id else None


def pop(run_id: str) -> None:
    store().delete(SPACE, run_id)


def set_latest(user_id: str, run_id: str | None) -> None:
    if run_id is None:
        store().delete(LATEST, user_id)
    else:
        store().put(LATEST, user_id, run_id, time.time() + _ttl())


def is_latest(user_id: str, run_id: str) -> bool:
    return store().get(LATEST, user_id) == run_id


def clear() -> None:
    store().clear(SPACE)
