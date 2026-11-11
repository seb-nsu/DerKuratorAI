"""State that every worker process of the app must see the same way – today the sign-in lockout. An app run
with several worker processes (or replicas on one volume) would otherwise count failed sign-ins per process, so an
attacker could spread attempts across workers. Each state is one small JSON file in data/state/, read and written
under a cross-process lock; the file is replaced atomically, so a reader never sees half a write."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import portalocker

from app.core import logs

LOCK_TIMEOUT_S = 10


def state_path(name: str) -> Path:
    return Path(logs.STATE_DIR) / f"{name}.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}  # a damaged state file starts empty – it only holds short-lived counters
    return data if isinstance(data, dict) else {}


def read(name: str) -> dict[str, Any]:
    return _load(state_path(name))


@contextmanager
def update(name: str) -> Iterator[dict[str, Any]]:
    """Read-modify-write of one state under its lock: change the yielded dict in place."""
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path) + ".lock", timeout=LOCK_TIMEOUT_S):
        data = _load(path)
        yield data
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def clear(name: str) -> None:
    with update(name) as data:
        data.clear()
