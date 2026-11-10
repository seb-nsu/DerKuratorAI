"""Records the due escalations and expiries of open change proposals – without anyone opening the inbox.

    python scripts/approval_deadlines.py

Suitable for a scheduler (cron, Task Scheduler), e.g. every hour. Idempotent: each escalation level and each expiry is
recorded once; the inbox runs the same check on every page view. With notifications on (NOTIFY_ENABLED), each new
event is queued for the approvers (and an expiry for the requester) and delivered at the end of the run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import notifications
from app.core.proposals import ProposalError, enforce_deadlines


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 2
    notifications.set_background(False)  # delivered below, before the script ends
    try:
        events = enforce_deadlines()
    except ProposalError as exc:
        print(f"Deadline check failed: {exc}")
        return 1
    for event in events:
        what = "expired" if event["type"] == "expired" else f"escalated to level {event['level']}"
        print(f"Proposal {event['id']} of {event.get('requester')}: {what}")
    print(f"{len(events)} deadline event(s) recorded.")
    if events and notifications.enabled():
        try:
            sent = notifications.deliver()
        except notifications.NotificationError as exc:  # queued: scripts/send_notifications.py retries
            print(f"Notifications not delivered now: {exc}")
        else:
            print(f"Notifications: {sent['sent']} sent, {sent['failed']} failed (retried later).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
