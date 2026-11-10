"""Guards the CPU-heavy routes (a what-if runs the full pipeline, a plan scenario or quick calculation several
seconds): at most `API_HEAVY_SLOTS` (default 2) of them at a time in this process, one per user, and at most
`API_HEAVY_PER_MINUTE` (default 20) per user and minute. A script of a single user can no longer keep the API busy;
the others answer 429 with a message to try again. A per-IP limit at the company reverse proxy stays the outer fence.

"One per user" and the per-minute count live in the API state store, so with `STATE_STORE=file` they hold across all
workers; the slots are per process (several workers allow `API_HEAVY_SLOTS` calculations each)."""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException, status

from app.api.state_store import store

WAIT_S = 20.0  # how long a request waits for a free slot before it is refused
WINDOW_S = 60.0
RUNNING_LEASE_S = 600.0  # a "running" mark left by a worker that died ends after this, not with the next restart
SPACE = "limits"  # state store space: user id -> start times of the last minute and the running mark
BUSY = "The server is busy with other calculations. Please try again in a moment."
RUNNING = "Your previous calculation is still running. Please wait for it to finish."
TOO_MANY = "Too many calculations in a short time. Please wait a minute and try again."

_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_slot_count = 0


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def _semaphore() -> threading.BoundedSemaphore:
    global _slots, _slot_count
    wanted = _int_env("API_HEAVY_SLOTS", 2)
    with _lock:
        if _slots is None or _slot_count != wanted:
            _slots, _slot_count = threading.BoundedSemaphore(wanted), wanted
        return _slots


def _refuse(message: str) -> HTTPException:
    return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message, headers={"Retry-After": "10"})


@contextmanager
def heavy(user_id: str) -> Iterator[None]:
    """Wrap one CPU-heavy calculation of `user_id`; raises 429 when the user or the server is at its limit."""
    now = time.time()
    per_minute = _int_env("API_HEAVY_PER_MINUTE", 20)

    def start(entry: dict[str, Any] | None) -> tuple[dict[str, Any], float]:
        entry = entry or {}
        if float(entry.get("running_until") or 0) > now:
            raise _refuse(RUNNING)
        recent = [t for t in entry.get("recent", []) if now - t <= WINDOW_S]
        if len(recent) >= per_minute:
            raise _refuse(TOO_MANY)
        return {"recent": [*recent, now], "running_until": now + RUNNING_LEASE_S}, now + RUNNING_LEASE_S

    def finish(entry: dict[str, Any] | None) -> tuple[dict[str, Any] | None, float]:
        recent = [t for t in (entry or {}).get("recent", []) if time.time() - t <= WINDOW_S]
        return ({"recent": recent} if recent else None), time.time() + WINDOW_S

    store().update(SPACE, user_id, start)
    slots = _semaphore()
    try:
        if not slots.acquire(timeout=WAIT_S):
            raise _refuse(BUSY)
        try:
            yield
        finally:
            slots.release()
    finally:
        store().update(SPACE, user_id, finish)


def reset() -> None:
    """Forget all counters (tests)."""
    global _slots
    store().clear(SPACE)
    with _lock:
        _slots = None
