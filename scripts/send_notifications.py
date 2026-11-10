"""Delivers the queued notifications (mail, Microsoft Teams) and retries the failed ones that are due.

    python scripts/send_notifications.py

Suitable for a scheduler (cron, Task Scheduler), e.g. every five minutes. The app delivers right after each event
too; this run catches what a stopped process or an unreachable mail server left behind. Does nothing while
NOTIFY_ENABLED is off. Exit code 1 if a delivery failed (it stays queued for its retry or is shown as failed in the
Admin Console › Operations › Notifications).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import notifications


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 2
    if not notifications.enabled():
        print("Notifications are off (NOTIFY_ENABLED) – nothing sent.")
        return 0
    notifications.set_background(False)
    try:
        result = notifications.deliver()
    except notifications.NotificationError as exc:
        print(f"Delivery failed: {exc}")
        return 1
    status = notifications.status()
    print(f"{result['queued']} message(s) queued, {result['sent']} sent, {result['failed']} failed · outbox: "
          f"{status['queued']} waiting, {status['failed']} given up.")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
