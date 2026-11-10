"""Short-lived API state that every server process must see the same way: what-if runs, the latest step per user,
signed-out sessions and the per-user calculation counters. `STATE_STORE` picks the backend:

  memory (default) – a dict in this process; enough for one server process
  file             – one small JSON file per entry below data/state/api/, read-modify-write under a cross-process
                     lock (like app/core/shared_state.py); several workers on one host, or replicas sharing one data
                     volume, then answer for each other

Every entry has an absolute expiry (wall clock, so it means the same in every process) and is forgotten after it. A
shared network store (Redis, Postgres) would be a third backend behind the same four calls for a production setup with
many replicas; the pilot needs no extra service."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import portalocker

from app.core import logs

BACKENDS = ("memory", "file")
LOCK_TIMEOUT_S = 10
CAPS = {"runs": 500}  # most entries kept per space (the oldest go first); a sign-out is never dropped early


class StateStore(Protocol):
    def get(self, space: str, key: str, now: float | None = None) -> Any | None: ...

    def put(self, space: str, key: str, value: Any, expires_at: float, now: float | None = None) -> None: ...

    def update(self, space: str, key: str, change: Callable[[Any | None], tuple[Any | None, float]],
               now: float | None = None) -> Any | None: ...

    def delete(self, space: str, key: str) -> None: ...

    def clear(self, space: str | None = None) -> None: ...


def backend_name() -> str:
    name = (os.getenv("STATE_STORE") or "memory").strip().lower()
    if name not in BACKENDS:
        raise RuntimeError(f"STATE_STORE must be one of {', '.join(BACKENDS)} (got {name!r})")
    return name


def worker_count() -> int:
    """The number of server processes uvicorn starts (WEB_CONCURRENCY, default 1). Raises ValueError."""
    raw = os.getenv("WEB_CONCURRENCY", "").strip()
    return int(raw) if raw else 1


def several_workers_refused(workers: int, frontend: str | None) -> str | None:
    """Why `workers` server processes cannot serve `frontend` (None: they can). Several workers need the React front
    end or the API alone (NiceGUI and Streamlit hold every user's session in one process: run replicas behind sticky
    sessions instead), the file store, and one token secret for all of them. Raises RuntimeError for an unknown
    STATE_STORE."""
    backend = backend_name()  # an unknown STATE_STORE fails at start-up, not on the first run
    if workers <= 1:
        return None
    head = f"WEB_CONCURRENCY={workers}:"
    if frontend in ("streamlit", "nicegui"):
        return (f"{head} {frontend} holds each user's session in its server process (websocket), so it runs in "
                "exactly one server process. Run several replicas behind a load balancer with sticky sessions "
                "instead (docs/PILOT_OPERATIONS.md, \"Several users at once\").")
    if backend != "file":
        return (f"{head} several workers need STATE_STORE=file, otherwise runs and sign-outs are held per process "
                "and a worker answers \"run not found\" for another one's run (docs/API.md, \"Limits\").")
    from app.api.tokens import MIN_SECRET_LENGTH  # local: tokens imports this module

    if len(os.getenv("API_TOKEN_SECRET", "")) < MIN_SECRET_LENGTH:
        return (f"{head} several workers need API_TOKEN_SECRET (at least {MIN_SECRET_LENGTH} characters), otherwise "
                "each worker signs with its own random secret and refuses the others' tokens.")
    return None


class MemoryStore:
    """Entries in this process's memory (one server process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spaces: dict[str, dict[str, tuple[float, Any]]] = {}

    def get(self, space: str, key: str, now: float | None = None) -> Any | None:
        now = time.time() if now is None else now
        with self._lock:
            entries = self._spaces.get(space, {})
            item = entries.get(key)
            if item is None or now >= item[0]:
                entries.pop(key, None)
                return None
            return item[1]

    def put(self, space: str, key: str, value: Any, expires_at: float, now: float | None = None) -> None:
        with self._lock:
            self._put(space, key, value, expires_at, time.time() if now is None else now)

    def _put(self, space: str, key: str, value: Any, expires_at: float, now: float) -> None:
        entries = self._spaces.setdefault(space, {})
        entries.pop(key, None)  # re-inserted last: the oldest entry is dropped first when the space is full
        entries[key] = (expires_at, value)
        for k in [k for k, (until, _) in entries.items() if until <= now]:
            del entries[k]
        while len(entries) > CAPS.get(space, len(entries)):
            del entries[next(iter(entries))]

    def update(self, space: str, key: str, change: Callable[[Any | None], tuple[Any | None, float]],
               now: float | None = None) -> Any | None:
        now = time.time() if now is None else now
        with self._lock:
            item = self._spaces.get(space, {}).get(key)
            value, expires_at = change(item[1] if item and now < item[0] else None)
            if value is None:
                self._spaces.get(space, {}).pop(key, None)
            else:
                self._put(space, key, value, expires_at, now)
            return value

    def delete(self, space: str, key: str) -> None:
        with self._lock:
            self._spaces.get(space, {}).pop(key, None)

    def clear(self, space: str | None = None) -> None:
        with self._lock:
            if space is None:
                self._spaces.clear()
            else:
                self._spaces.pop(space, None)


class FileStore:
    """One JSON file per entry below `root` (data/state/api by default), replaced atomically; writes of one space are
    serialized by a lock file, reads need no lock. Keys are hashed into the file name (no user id on disk)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root or Path(logs.STATE_DIR) / "api"

    def _dir(self, space: str) -> Path:
        return self.root / space

    def _path(self, space: str, key: str) -> Path:
        return self._dir(space) / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:40]}.json"

    def _lock(self, space: str) -> portalocker.Lock:
        self.root.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(str(self.root / f"{space}.lock"), timeout=LOCK_TIMEOUT_S)

    @staticmethod
    def _read(path: Path, now: float) -> Any | None:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # missing, or damaged: the entry is gone (it only holds short-lived state)
        if not isinstance(item, dict) or now >= float(item.get("expires_at") or 0):
            return None
        return item.get("value")

    def _write(self, space: str, key: str, value: Any, expires_at: float, now: float) -> None:
        folder = self._dir(space)
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"expires_at": expires_at, "value": value}, fh, default=str)
            os.utime(tmp, (expires_at, expires_at))  # the file's mtime is its expiry: pruning needs no read
            os.replace(tmp, self._path(space, key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._prune(folder, space, now)

    @staticmethod
    def _prune(folder: Path, space: str, now: float) -> None:
        """Drops expired entries, then the soonest to expire beyond the space's cap (under the space's lock)."""
        files = []
        for path in folder.glob("*.json"):
            try:
                until = path.stat().st_mtime
            except OSError:
                continue
            if until <= now:
                path.unlink(missing_ok=True)
            else:
                files.append((until, path))
        for _, path in sorted(files)[:max(0, len(files) - CAPS.get(space, len(files)))]:
            path.unlink(missing_ok=True)

    def get(self, space: str, key: str, now: float | None = None) -> Any | None:
        return self._read(self._path(space, key), time.time() if now is None else now)

    def put(self, space: str, key: str, value: Any, expires_at: float, now: float | None = None) -> None:
        with self._lock(space):
            self._write(space, key, value, expires_at, time.time() if now is None else now)

    def update(self, space: str, key: str, change: Callable[[Any | None], tuple[Any | None, float]],
               now: float | None = None) -> Any | None:
        now = time.time() if now is None else now
        with self._lock(space):
            value, expires_at = change(self._read(self._path(space, key), now))
            if value is None:
                self._path(space, key).unlink(missing_ok=True)
            else:
                self._write(space, key, value, expires_at, now)
            return value

    def delete(self, space: str, key: str) -> None:
        with self._lock(space):
            self._path(space, key).unlink(missing_ok=True)

    def clear(self, space: str | None = None) -> None:
        root = self.root
        if not root.exists():
            return
        for folder in [p for p in root.iterdir() if p.is_dir() and space in (None, p.name)]:
            with self._lock(folder.name):
                for path in folder.glob("*.json"):
                    path.unlink(missing_ok=True)


_lock = threading.Lock()
_stores: dict[str, StateStore] = {}


def store() -> StateStore:
    """The backend `STATE_STORE` selects (one instance per backend and process)."""
    name = backend_name()
    with _lock:
        if name not in _stores:
            _stores[name] = MemoryStore() if name == "memory" else FileStore()
        return _stores[name]


def reset() -> None:
    """Forget all entries of the selected backend (tests)."""
    with _lock:
        stores = list(_stores.values())
    for s in stores:
        s.clear()
