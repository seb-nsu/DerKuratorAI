"""Snapshots of the portfolio data source: taken before every write, restorable from the Admin Console.

Layout (data/snapshots/):
    manifest.jsonl                                   one JSON line per snapshot (append-only)
    <YYYYmmdd_HHMMSS>_<id>_<reason><.json|.xlsx>     byte-exact copy of the source file

Each manifest entry carries id, timestamp, environment, reason, user, source file, sha256, size and – for data
changes – the change set plus its inverse (old values), so an audit reviewer can see exactly what the write did.
Retention: the newest MAX_SNAPSHOTS snapshots are kept; older files are pruned after each new snapshot, except
snapshots referenced by an approval (proposal decision or Approve & Apply audit record), which are audit evidence,
and the first snapshot of each source, which holds the state the instance started with ("Reset demo data").

Writers and restores serialise on a cross-process lock per source file (`<source>.writelock`), so a restore can
never interleave with an "Approve & Apply" write.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import portalocker

from app.core import clock, logs

logger = logging.getLogger("derkurator.snapshots")
MANIFEST = "manifest.jsonl"
MAX_SNAPSHOTS = 50
WRITE_LOCK_TIMEOUT_S = 60.0


class SnapshotError(RuntimeError):
    pass


def snapshot_dir() -> Path:
    return Path(logs.SNAPSHOT_DIR)  # resolved at call time (tests redirect it)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def source_write_lock(source: Path, timeout: float = WRITE_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Exclusive cross-process lock for read-check-write cycles on one data source file."""
    lock_path = Path(source).with_name(Path(source).name + ".writelock")
    try:
        with portalocker.Lock(str(lock_path), mode="a", timeout=timeout, check_interval=0.1):
            yield
    except portalocker.exceptions.LockException as exc:
        raise SnapshotError(f"{Path(source).name} is being written by another user – please retry.") from exc


def _inverse(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Old values per (project, table, field) – the first 'old' of a field is the value before the whole write."""
    seen: dict[tuple, dict[str, Any]] = {}
    for c in changes:
        key = (c.get("project_id"), c.get("table", "Projects"), c.get("field"))
        seen.setdefault(key, {"project_id": key[0], "table": key[1], "field": key[2], "new": c.get("old")})
    return list(seen.values())


def create_snapshot(source: Path, reason: str, user: str | None = None,
                    changes: list[dict[str, Any]] | None = None, prune_after: bool = True) -> dict[str, Any]:
    """Copies the source file into data/snapshots/ and appends a manifest entry. Raises SnapshotError on failure –
    a write must not proceed without its snapshot. `prune_after=False` defers retention to the caller (restore)."""
    source = Path(source)
    if not source.exists():
        raise SnapshotError(f"Snapshot not possible – source file not found: {source}")
    folder = snapshot_dir()
    snap_id = uuid.uuid4().hex[:10]
    stamp = clock.now()
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in reason)[:40]
    target = folder / f"{stamp:%Y%m%d_%H%M%S}_{snap_id}_{safe_reason}{source.suffix}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entry = {
            "id": snap_id, "created_at": stamp.isoformat(timespec="seconds"), "environment": logs.current_environment(),
            "reason": reason, "user": user, "source": source.name, "source_path": str(source.resolve()),
            "file": target.name, "sha256": _sha256(target), "size": target.stat().st_size,
            "changes": changes or [], "inverse": _inverse(changes or []),
        }
        with (folder / MANIFEST).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise SnapshotError(f"Snapshot could not be written: {exc}") from exc
    logger.info("Snapshot %s created (%s, %s by %s)", snap_id, source.name, reason, user)
    if prune_after:
        prune()
    return entry


def list_snapshots() -> list[dict[str, Any]]:
    """All manifest entries whose snapshot file still exists, newest first. Never raises."""
    try:
        records, malformed = logs.read_jsonl(snapshot_dir() / MANIFEST)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.error("Snapshot manifest unreadable: %s", exc)
        return []
    if malformed:
        logger.warning("Snapshot manifest: %d corrupt line(s) skipped", malformed)
    entries = [r for r in records if r.get("id") and r.get("file") and (snapshot_dir() / str(r["file"])).exists()]
    return sorted(entries, key=lambda r: str(r.get("created_at", "")), reverse=True)


def get_snapshot(snap_id: str) -> dict[str, Any] | None:
    return next((s for s in list_snapshots() if s["id"] == snap_id), None)


# Audit events that document a decision or a write – the snapshot they cite is evidence and never pruned
DECISION_AUDIT_EVENTS = ("scenario_applied", "proposal_approved", "project_edited", "data_write")


def _referenced_snapshot_ids() -> set[str]:
    """Snapshots that are evidence of an approval (proposal decision or a direct Approve & Apply) – never pruned."""
    from app.core.proposals import list_proposals

    ids = {str(p["snapshot_id"]) for p in list_proposals() if p.get("snapshot_id")}
    try:
        records, _ = logs.read_jsonl(Path(logs.AUDIT_LOG))
    except OSError:  # no audit log yet (or unreadable): proposal references still protect their snapshots
        records = []
    ids |= {str(r["snapshot_id"]) for r in records if r.get("event") in DECISION_AUDIT_EVENTS and r.get("snapshot_id")}
    return ids


def first_snapshot(source_name: str) -> dict[str, Any] | None:
    """The oldest snapshot of a source: taken before its first write, so it holds the state the instance started
    with. None if the source was never written."""
    entries = [s for s in list_snapshots() if s.get("source") == source_name]
    if not entries:
        return None
    oldest = min(str(s.get("created_at", "")) for s in entries)
    # timestamps have second resolution; among equal ones the list keeps manifest (write) order
    return next(s for s in entries if str(s.get("created_at", "")) == oldest)


def prune(keep: int = MAX_SNAPSHOTS) -> int:
    """Deletes snapshot files beyond the newest `keep` (manifest lines stay as history). Snapshots referenced by a
    proposal are kept and do not count against the retention. Returns the number of deleted files."""
    removed = 0
    protected = _referenced_snapshot_ids()
    protected |= {first["id"] for name in {s.get("source") for s in list_snapshots()}
                  if (first := first_snapshot(str(name))) is not None}
    for entry in [s for s in list_snapshots() if s["id"] not in protected][keep:]:
        try:
            (snapshot_dir() / str(entry["file"])).unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("Snapshot %s could not be pruned: %s", entry["id"], exc)
    return removed


def restore_snapshot(snap_id: str, source: Path, user: str | None = None, role: str | None = None) -> dict[str, Any]:
    """Restores a snapshot over the source file (atomic). The current state is snapshotted first, so a restore
    can itself be undone. Verifies the snapshot's sha256. The caller must rebuild the graph (run_seed) afterwards.
    Only the System Admin restores (`role`; None = in-process caller such as the demo reset)."""
    from app.core.runtime_config import require_admin

    require_admin(role, "Restoring a snapshot")
    entry = get_snapshot(snap_id)
    if entry is None:
        raise SnapshotError(f"Snapshot {snap_id} not found.")
    source = Path(source)
    if entry.get("source") != source.name:
        raise SnapshotError(f"Snapshot {snap_id} belongs to {entry.get('source')}, not to the active source {source.name}.")
    snap_file = snapshot_dir() / str(entry["file"])
    if _sha256(snap_file) != entry.get("sha256"):
        raise SnapshotError(f"Snapshot {snap_id} is corrupt (checksum mismatch) – not restored.")
    with source_write_lock(source):
        # No pruning before the copy – at the retention limit it would delete the very snapshot being restored
        before = create_snapshot(source, f"pre-restore-{snap_id}", user, prune_after=False)
        tmp = source.with_name(f"{source.name}.restore.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            shutil.copyfile(snap_file, tmp)  # copyfile, not copy2: the restored file gets a fresh mtime
            os.replace(tmp, source)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise SnapshotError(f"Restore failed: {exc}") from exc
    prune()
    logger.warning("Snapshot %s restored over %s by %s (previous state: snapshot %s)", snap_id, source.name, user, before["id"])
    return {"restored": entry, "previous_state": before}
