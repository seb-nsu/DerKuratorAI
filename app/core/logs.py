"""Global system logging (data/app.log) and file helpers for audit, shadow and system logs.

- All loggers at INFO and above (including Streamlit) are appended continuously to data/app.log.
  The file rotates at 5 MB (3 backups) so it does not grow without bound.
- Every line carries the active environment (DEV/TEST/PROD).
- Read helpers deliberately re-raise FileNotFoundError – the UI then shows "No entries".
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core import clock
from app.core.config import PROJECT_ROOT


def current_environment() -> str:
    # Lazy import: runtime_config -> env_config -> logs would otherwise be circular
    from app.core.runtime_config import current_environment as _current

    return _current()

DATA_DIR = PROJECT_ROOT / "data"
# system log and incidents can go elsewhere (DERKURATOR_LOG_DIR, e.g. the smoke test's temp folder); decisions stay
LOG_DIR = Path(os.getenv("DERKURATOR_LOG_DIR") or DATA_DIR)
# decisions, proposals, chats and snapshots can go elsewhere too (DERKURATOR_WRITE_DIR): the system tests started from
# the Admin Console set it, so a test run never writes into the live data/ folder
WRITE_DIR = Path(os.getenv("DERKURATOR_WRITE_DIR") or DATA_DIR)
APP_LOG = LOG_DIR / "app.log"
AUDIT_LOG = WRITE_DIR / "decision_log.jsonl"
SHADOW_LOG = WRITE_DIR / "shadow_comparison_log.jsonl"
INCIDENTS_LOG = LOG_DIR / "incidents.jsonl"  # system errors – kept apart from the decision log
LLM_CALLS_LOG = LOG_DIR / "llm_calls.jsonl"  # one line per LLM call, without content
TEST_REPORT = DATA_DIR / "test_report.html"
ARCHIVE_DIR = WRITE_DIR / "archive"
BUSINESS_AUDIT_LOG = WRITE_DIR / "business_audit.log"
CHAT_DIR = WRITE_DIR / "chat_sessions"
SNAPSHOT_DIR = WRITE_DIR / "snapshots"
STATE_DIR = WRITE_DIR / "state"  # state shared by all worker processes (app.core.shared_state)
PROPOSALS_LOG = WRITE_DIR / "pending_changes.jsonl"
SCENARIOS_LOG = WRITE_DIR / "saved_scenarios.jsonl"
NOTIFY_OUTBOX = WRITE_DIR / "notifications.jsonl"  # mail / Teams outbox (app.core.notifications)

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(env)-4s | %(name)s | %(message)s"
_HANDLER_NAME = "derkurator-app-log"
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "openai._base_client", "anthropic._base_client",
                  "watchdog", "choreographer", "kaleido", "asyncio", "sqlalchemy.engine")

logger = logging.getLogger(__name__)


class EnvironmentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.env = current_environment()
        return True


def _file_handler(log_file: Path, level: int) -> RotatingFileHandler:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, mode="a", maxBytes=5 * 1024**2, backupCount=3, encoding="utf-8")
    handler.set_name(_HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(EnvironmentFilter())
    return handler


def get_app_log_handler() -> RotatingFileHandler | None:
    return next((h for h in logging.getLogger().handlers if h.get_name() == _HANDLER_NAME), None)


def shutdown_logging() -> None:
    """Removes and closes the file handler (tests, orderly shutdown)."""
    handler = get_app_log_handler()
    if handler is None:
        return
    for obj in [logging.getLogger(), *logging.root.manager.loggerDict.values()]:
        if isinstance(obj, logging.Logger) and handler in obj.handlers:
            obj.removeHandler(handler)
    handler.close()


def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> None:
    """Idempotent (Streamlit re-runs the script on every interaction)."""
    root = logging.getLogger()
    if get_app_log_handler() is not None:
        return
    log_file = log_file or APP_LOG  # resolve at runtime (tests can redirect APP_LOG)
    handler = _file_handler(log_file, level)
    root.addHandler(handler)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        console.addFilter(EnvironmentFilter())
        root.addHandler(console)
    root.setLevel(level)

    # Streamlit loggers do not propagate to the root logger, so attach the file handler directly and set them
    # explicitly to INFO (Streamlit assigns levels per logger from its config; the default would be WARNING).
    logging.getLogger("streamlit").setLevel(logging.INFO)
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if not isinstance(obj, logging.Logger) or name.split(".")[0] != "streamlit":
            continue
        if name.startswith("streamlit") and obj.level > logging.INFO:
            obj.setLevel(logging.INFO)
        if not obj.propagate:
            obj.addHandler(handler)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    logger.info("System logging initialised -> %s (level %s)", log_file, logging.getLevelName(level))


# =========================================================================== readers
def tail_lines(path: Path, n: int = 100) -> list[str]:
    """Last n lines, newest first. Raises FileNotFoundError if the file is missing."""
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        last = deque(fh, maxlen=n)
    return [line.rstrip("\n") for line in reversed(last)]


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Reads JSON Lines; malformed lines are counted instead of blocking the display."""
    records, malformed = [], 0
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                records.append(obj if isinstance(obj, dict) else {"value": obj})
            except json.JSONDecodeError:
                malformed += 1
    return records, malformed


# =========================================================================== writers
class AuditWriteError(OSError):
    """An audit record could not be written – the calling transaction must fail."""


GENESIS = "0" * 64  # prev_hash of the first record of a chain


def record_hash(record: dict[str, Any]) -> str:
    """SHA-256 over the record without its own hash (canonical JSON: sorted keys)."""
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _last_hash(path: Path) -> str:
    """The hash of the last chained record (GENESIS for an empty or unchained file)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size, block, data = fh.tell(), 8192, b""
            while size > 0 and data.count(b"\n") < 2:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
    except FileNotFoundError:
        return GENESIS
    for line in reversed(data.decode("utf-8", errors="replace").splitlines()):
        if line.strip():
            try:
                return json.loads(line).get("hash") or GENESIS
            except (json.JSONDecodeError, AttributeError):
                return GENESIS
    return GENESIS


def _chained(path: Path) -> bool:
    return Path(path).resolve() == Path(AUDIT_LOG).resolve()


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Appends an entry, adding timestamp and environment unless already set. Records of the decision log are chained:
    each carries the hash of its predecessor and its own (tamper evidence, see verify_chain).
    Blocking: a failed write raises AuditWriteError – it is never swallowed silently."""
    record = {"timestamp": clock.stamp(), "environment": current_environment(), **entry}
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _chained(path):
            import portalocker  # cross-process: two workers must not chain onto the same predecessor

            with portalocker.Lock(str(path) + ".lock", timeout=10):
                record = {**record, "prev_hash": _last_hash(path)}
                record["hash"] = record_hash(record)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            return
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except (OSError, portalocker_errors()) as exc:
        logger.exception("Log entry could not be written: %s", path)
        raise AuditWriteError(f"Log entry could not be written to {Path(path).name}: {exc}") from exc


def portalocker_errors() -> type[Exception]:
    from portalocker.exceptions import LockException

    return LockException


def verify_chain(path: Path | None = None) -> dict[str, Any]:
    """Checks the hash chain of the decision log. Records written before the chain existed (no hash) are counted as
    `unchained` and must all come first. Returns {"ok", "records", "chained", "unchained", "broken_at", "reason"} –
    `broken_at` is the 1-based line of the first record that was changed, removed or inserted."""
    path = Path(path or AUDIT_LOG)
    out = {"ok": True, "records": 0, "chained": 0, "unchained": 0, "broken_at": None, "reason": ""}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return out
    previous = None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        out["records"] += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return {**out, "ok": False, "broken_at": number, "reason": "unreadable record"}
        if "hash" not in record:
            if previous is not None:
                return {**out, "ok": False, "broken_at": number, "reason": "record without hash inside the chain"}
            out["unchained"] += 1
            continue
        expected_prev = GENESIS if previous is None else previous
        if record.get("prev_hash") != expected_prev and not (previous is None and record.get("event") == "log_archived"):
            return {**out, "ok": False, "broken_at": number, "reason": "predecessor does not match (record removed, "
                    "inserted or reordered)"}
        if record_hash(record) != record["hash"]:
            return {**out, "ok": False, "broken_at": number, "reason": "content changed after it was written"}
        previous = record["hash"]
        out["chained"] += 1
    return out


def _audit_value(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)) or (isinstance(value, str) and not value.strip()):
        return "NULL"
    return repr(value) if isinstance(value, str) else str(value)


def append_business_audit(user: str | None, project_id: str, field: str, old: Any, new: Any,
                          path: Path | None = None) -> None:
    """Human-readable business trail of master-data edits (data/business_audit.log). Raises AuditWriteError."""
    line = (f"{clock.stamp()} | {current_environment()} | User {user or '-'} changed "
            f"field {field} from {_audit_value(old)} to {_audit_value(new)} on Project ID {project_id}")
    target = Path(path or BUSINESS_AUDIT_LOG)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line.replace("\n", " ") + "\n")
    except OSError as exc:
        logger.exception("Business audit entry could not be written: %s", target)
        raise AuditWriteError(f"Business audit entry could not be written: {exc}") from exc


def log_shadow_comparison(
    request: str,
    primary: dict[str, Any],
    shadow: dict[str, Any],
    path: Path | None = None,
    **extra: Any,
) -> None:
    """Contract for shadow comparisons (main vs. shadow model) shown by the Shadow Logs tab.
    primary/shadow: {"model": str, "duration_s": float, "result": str, "error": str | None}.
    Diagnostics only (no audit trail): a failed write is logged, never raised into the background thread."""
    try:
        append_jsonl(path or SHADOW_LOG, {"event": "shadow_comparison", "request": request, "primary": primary,
                                          "shadow": shadow, **extra})
    except AuditWriteError:
        pass  # already logged by append_jsonl


# =========================================================================== maintenance
def archive_and_clear(path: Path, archive_dir: Path | None = None) -> Path | None:
    """Empties a JSONL file but archives its content first (the audit trail is never lost)."""
    path = Path(path)
    archive_dir = archive_dir or ARCHIVE_DIR
    if not path.exists() or path.stat().st_size == 0:
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{path.stem}_{clock.now():%Y%m%d_%H%M%S}{path.suffix}"
    last = _last_hash(path) if _chained(path) else None
    shutil.copy2(path, target)
    path.write_text("", encoding="utf-8")
    if last is not None:  # the new chain starts by pointing at the last record of the archive
        record = {"timestamp": clock.stamp(), "environment": current_environment(), "event": "log_archived",
                  "archive": target.name, "prev_hash": last}
        record["hash"] = record_hash(record)
        path.write_text(json.dumps(record, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return target


def clear_app_log(path: Path | None = None, archive_dir: Path | None = None) -> Path | None:
    """Empties data/app.log – via the open handler so its file position stays consistent.
    The content (incl. SECURITY events) is archived first; if archiving fails, nothing is cleared.
    Returns the archive file (None if the log was empty)."""
    path = Path(path or APP_LOG)
    archive_dir = archive_dir or ARCHIVE_DIR
    target = None
    if path.exists() and path.stat().st_size > 0:
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / f"{path.stem}_{clock.now():%Y%m%d_%H%M%S}{path.suffix}"
        shutil.copy2(path, target)  # an OSError propagates and leaves the log untouched
    handler = get_app_log_handler()
    if handler is not None and Path(handler.baseFilename) == Path(path).resolve():
        handler.acquire()
        try:
            if handler.stream is not None:
                handler.stream.seek(0)
                handler.stream.truncate()
                handler.stream.flush()
        finally:
            handler.release()
    elif path.exists():
        path.write_text("", encoding="utf-8")
    return target
