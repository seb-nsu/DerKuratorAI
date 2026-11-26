"""When the watch runs: after every committed write (in the background, so the write path never waits or fails),
after a ServiceNow / work-tool sync, on a schedule (scripts/recovery_scan.py, because the calculated status also tips
without a data change: an end date passes, a report ages) and when the overview opens. All paths call the same
idempotent app.recovery.engine.run_scan; nothing happens while the rule recovery_enabled is off."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("derkurator.recovery.hooks")
_running = threading.Lock()  # one scan per process at a time; another worker is kept apart by the store's key lock


def enabled() -> bool:
    from app.core.business_rules import current_rules

    try:
        return bool(current_rules().recovery_enabled)
    except Exception:  # noqa: BLE001 - a hook never breaks its caller
        return False


def scan_now(source: str) -> dict:
    """Runs the scan in this thread (scripts); skipped while another scan of this process runs."""
    from app.recovery.engine import run_scan

    if not _running.acquire(blocking=False):
        return {"enabled": True, "skipped": "a scan is already running"}
    try:
        return run_scan(source=source)
    finally:
        _running.release()


def _quiet(source: str) -> None:
    try:
        scan_now(source)
    except Exception:
        logger.exception("Recovery scan (%s) failed", source)


def scan_in_background(source: str) -> bool:
    """Starts a scan in a daemon thread when the red trigger is on. Returns whether one was started. Never raises."""
    if not enabled():
        return False
    try:
        threading.Thread(target=_quiet, args=(source,), name=f"recovery-scan-{source}", daemon=True).start()
        return True
    except Exception:
        logger.exception("Recovery scan could not be started")
        return False
