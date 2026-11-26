"""Recommendation store of the red trigger: data/recovery.jsonl, append-only, hash-chained like the decision log
(app.core.logs.append_jsonl under a cross-process lock). A recommendation is not a proposal: it only becomes one when a
person adopts an option, re-runs it in the Steering Console and submits it under their own name.

Records (every record carries `timestamp`, `environment`, `prev_hash`, `hash`):
    {"type": "created",    "id": ..., "key": "<project>|<trigger>|<data state>", "recommendation": {...}}
    {"type": "adopted",    "id": ..., "by": ..., "role": ..., "option": "A", "run_id": ...}
    {"type": "dismissed",  "id": ..., "by": ..., "role": ..., "reason": ...}             reason is mandatory
    {"type": "superseded", "id": ..., "by_id": ...}                                       a newer trigger, newer data
    {"type": "expired",    "id": ...}                                                     open past recovery_ttl_days
    {"type": "proposal",   "id": ..., "proposal_id": ..., "by": ...}                      a proposal names it
The state of a recommendation is its `created` record merged with its latest state record."""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import clock, logs

logger = logging.getLogger("derkurator.recovery.store")

OPEN, ADOPTED, DISMISSED, SUPERSEDED, EXPIRED = "open", "adopted", "dismissed", "superseded", "expired"
STATES = (OPEN, ADOPTED, DISMISSED, SUPERSEDED, EXPIRED)
CLOSED = (DISMISSED, SUPERSEDED, EXPIRED)


class RecoveryStoreError(RuntimeError):
    pass


def store_path() -> Path:
    return Path(logs.RECOVERY_LOG)  # resolved at call time (tests redirect it)


def _append(record: dict[str, Any]) -> None:
    try:
        logs.append_jsonl(store_path(), record)
    except logs.AuditWriteError as exc:
        raise RecoveryStoreError(str(exc)) from exc


def _records() -> list[dict[str, Any]]:
    try:
        records, malformed = logs.read_jsonl(store_path())
    except FileNotFoundError:
        return []
    if malformed:
        logger.error("Recommendation store: %d malformed line(s) skipped", malformed)
    return records


def _merge(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for r in records:
        rid = str(r.get("id") or "")
        if r.get("type") == "created" and rid:
            items[rid] = {**r.get("recommendation", {}), "id": rid, "key": r.get("key"), "status": OPEN,
                          "created_at": r.get("timestamp"), "history": [], "proposals": []}
            continue
        item = items.get(rid)
        if item is None:
            continue
        kind = r.get("type")
        item["history"].append({k: v for k, v in r.items() if k not in ("prev_hash", "hash", "environment")})
        if kind == "proposal":
            item["proposals"].append(r.get("proposal_id"))
        elif kind in (ADOPTED, DISMISSED, SUPERSEDED, EXPIRED):
            item["status"] = kind
            item["closed_at" if kind in CLOSED else "adopted_at"] = r.get("timestamp")
            if kind == ADOPTED:
                item.update(adopted_by=r.get("by"), adopted_option=r.get("option"), run_id=r.get("run_id"))
            elif kind == DISMISSED:
                item.update(dismissed_by=r.get("by"), dismiss_reason=r.get("reason"))
            elif kind == SUPERSEDED:
                item["superseded_by"] = r.get("by_id")
    return items


def list_recommendations(status: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    items = list(_merge(_records()).values())
    if status:
        items = [i for i in items if i["status"] == status]
    if project_id:
        items = [i for i in items if str(i.get("project_id")) == str(project_id)]
    return sorted(items, key=lambda i: str(i.get("created_at") or ""), reverse=True)


def get_recommendation(rec_id: str) -> dict[str, Any] | None:
    return _merge(_records()).get(str(rec_id))


def known_keys() -> set[str]:
    """Idempotency keys of every recommendation ever created (project|trigger|data state)."""
    return {str(r.get("key")) for r in _records() if r.get("type") == "created" and r.get("key")}


def create(recommendation: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Stores a recommendation unless one with the same key exists (two workers, a repeated scan: one analysis).
    An open recommendation of the same project is superseded by the new one. Returns the stored item or None."""
    import portalocker

    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec_id = uuid.uuid4().hex[:10]
    try:
        with portalocker.Lock(str(path) + ".create.lock", timeout=15):  # check and append as one step
            records = _records()
            if any(r.get("type") == "created" and r.get("key") == key for r in records):
                return None
            for old in _merge(records).values():
                if old["status"] in (OPEN,) and str(old.get("project_id")) == str(recommendation.get("project_id")):
                    _append({"type": SUPERSEDED, "id": old["id"], "by_id": rec_id})
            _append({"type": "created", "id": rec_id, "key": key, "recommendation": recommendation})
    except portalocker.exceptions.LockException as exc:
        raise RecoveryStoreError("The recommendation store is busy – please retry.") from exc
    return get_recommendation(rec_id)


def _open_or_raise(rec_id: str) -> dict[str, Any]:
    item = get_recommendation(rec_id)
    if item is None:
        raise KeyError(rec_id)
    if item["status"] in CLOSED:
        raise RecoveryStoreError(f"The recommendation is {item['status']} – recalculate it first.")
    return item


def adopt(rec_id: str, by: str, role: str, option: str, run_id: str) -> dict[str, Any]:
    _open_or_raise(rec_id)
    _append({"type": ADOPTED, "id": rec_id, "by": by, "role": role, "option": option, "run_id": run_id})
    return get_recommendation(rec_id)  # type: ignore[return-value]


def dismiss(rec_id: str, by: str, role: str, reason: str) -> dict[str, Any]:
    if not (reason or "").strip():
        raise RecoveryStoreError("Give the reason for dismissing the recommendation.")
    item = _open_or_raise(rec_id)
    if item["status"] != OPEN:
        raise RecoveryStoreError("Only an open recommendation can be dismissed.")
    _append({"type": DISMISSED, "id": rec_id, "by": by, "role": role, "reason": reason.strip()[:1000]})
    return get_recommendation(rec_id)  # type: ignore[return-value]


def link_proposal(rec_id: str, proposal_id: str, by: str) -> None:
    if get_recommendation(rec_id) is None:
        return
    _append({"type": "proposal", "id": rec_id, "proposal_id": proposal_id, "by": by})


def expire(now: datetime | None = None, ttl_days: int | None = None) -> list[str]:
    """Closes open recommendations older than the TTL (idempotent). Returns their ids."""
    from app.core.business_rules import current_rules

    now = now or clock.now()
    ttl = timedelta(days=ttl_days if ttl_days is not None else current_rules().recovery_ttl_days)
    expired = []
    for item in list_recommendations(status=OPEN):
        try:
            created = clock.parse(item.get("created_at"))
        except ValueError:
            continue
        if now - created > ttl:
            _append({"type": EXPIRED, "id": item["id"]})
            expired.append(item["id"])
    return expired


def verify() -> dict[str, Any]:
    """The hash chain of the store (tamper evidence)."""
    return logs.verify_chain(store_path())


def today() -> date:
    return clock.today()
