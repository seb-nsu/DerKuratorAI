"""Chat persistence: append-only JSONL event log per identity, so the history survives a page reload and two browser
tabs of the same user can never overwrite or delete each other's messages.

File: data/chat_sessions/chat_<hash>.jsonl (hash of role and user – no names in file names). One JSON object per line:
    {"type": "message", "id": ..., "ts": ..., "message": {...}}   a new chat message
    {"type": "update",  "id": ..., "ts": ..., "meta": {...}}      e.g. "applied" after Approve & Apply
    {"type": "clear",   "ts": ...}                                 "Clear chat" – hides everything written before
Messages older than MAX_AGE are ignored. Appends and the occasional compaction serialise on a cross-process lock.
All I/O is wrapped – a broken line or file never breaks the app.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

import portalocker

from app.core import clock, logs

logger = logging.getLogger("derkurator.core.chat")
MAX_AGE = timedelta(hours=24)
MAX_MESSAGES = 200
COMPACT_ABOVE_BYTES = 2 * 1024 * 1024


def chat_path(role: str, user_id: str) -> Path:
    digest = hashlib.sha256(f"{role}|{user_id}".encode("utf-8")).hexdigest()[:24]
    return Path(logs.CHAT_DIR) / f"chat_{digest}.jsonl"  # resolved at call time (tests redirect CHAT_DIR)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path.with_name(path.name + ".lock")), mode="a", timeout=10, check_interval=0.05):
        yield


def _append(path: Path, events: list[dict[str, Any]]) -> bool:
    try:
        with _locked(path), path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return True
    except (OSError, TypeError, ValueError, portalocker.exceptions.LockException) as exc:
        logger.warning("Chat history could not be saved (%s)", exc)
        return False


def _replay(lines: list[str], identity: list[str]) -> list[dict[str, Any]]:
    """Rebuilds the visible message list from the event log (clear / expiry / updates applied)."""
    return [message for message, _ts in _replay_with_ts(lines, identity)]


def _replay_with_ts(lines: list[str], identity: list[str]) -> list[tuple[dict[str, Any], str]]:
    cutoff = clock.now() - MAX_AGE
    messages: dict[str, dict[str, Any]] = {}
    stamps: dict[str, str] = {}
    for line in lines:
        try:
            event = json.loads(line)
            if event.get("identity", identity) != identity:
                continue
            ts = clock.parse(event.get("ts"))
        except (ValueError, TypeError, AttributeError):
            continue  # skip a corrupt line and keep the rest
        kind = event.get("type")
        if kind == "clear":
            messages.clear()
        elif kind == "message" and ts >= cutoff and isinstance(event.get("message"), dict):
            messages[str(event.get("id"))] = {**event["message"], "id": str(event.get("id"))}
            stamps[str(event.get("id"))] = str(event["ts"])
        elif kind == "update" and str(event.get("id")) in messages and isinstance(event.get("meta"), dict):
            target = messages[str(event["id"])]
            target["meta"] = {**(target.get("meta") or {}), **event["meta"]}
    return [(m, stamps[mid]) for mid, m in messages.items()][-MAX_MESSAGES:]


def load_chat(role: str, user_id: str) -> list[dict[str, Any]]:
    path = chat_path(role, user_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Chat history %s unreadable (%s) – starting empty", path.name, exc)
        return []
    return _replay(lines, [role, user_id])


def append_messages(role: str, user_id: str, messages: list[dict[str, Any]]) -> None:
    """Appends every message without an ID (not yet persisted) and assigns one. Existing lines are never modified."""
    new = [m for m in messages if not m.get("id")]
    if not new:
        return
    now = clock.stamp()
    events = []
    for message in new:
        message["id"] = uuid.uuid4().hex
        events.append({"type": "message", "id": message["id"], "ts": now, "identity": [role, user_id],
                       "message": {k: v for k, v in message.items() if k != "id"}})
    path = chat_path(role, user_id)
    if _append(path, events):
        _maybe_compact(path, [role, user_id])


def update_message(role: str, user_id: str, message_id: str | None, meta: dict[str, Any]) -> None:
    """Records a meta change (e.g. applied) of an already persisted message."""
    if not message_id:
        return
    _append(chat_path(role, user_id), [{"type": "update", "id": message_id, "ts": clock.stamp(),
                                        "identity": [role, user_id], "meta": meta}])


def clear_chat(role: str, user_id: str) -> None:
    """Appends a clear marker: hides all earlier messages in every tab, but a tab writing afterwards only adds its
    new messages (no resurrection of the cleared history)."""
    _append(chat_path(role, user_id), [{"type": "clear", "ts": clock.stamp(),
                                        "identity": [role, user_id]}])


def _maybe_compact(path: Path, identity: list[str]) -> None:
    """Rewrites the log with only the live messages once it grows large (under the same lock as appends)."""
    try:
        if path.stat().st_size <= COMPACT_ABOVE_BYTES:
            return
        with _locked(path):
            live = _replay_with_ts(path.read_text(encoding="utf-8").splitlines(), identity)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for message, ts in live:  # original timestamps: compaction never extends the retention
                    fh.write(json.dumps({"type": "message", "id": message["id"], "ts": ts, "identity": identity,
                                         "message": {k: v for k, v in message.items() if k != "id"}},
                                        ensure_ascii=False, default=str) + "\n")
            os.replace(tmp, path)
    except (OSError, portalocker.exceptions.LockException) as exc:
        logger.warning("Chat history compaction skipped (%s)", exc)
