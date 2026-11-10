"""Starts scripts/serve.py with the app clock moved to the demo portfolio's reference date (E2E_AS_OF, ISO date), so
the expected verdicts of the demo hold on every day the suite runs. Time keeps running from 09:00 of that date; only
the app's own clock (app.core.clock) is moved, sign-in tokens keep the real time."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def freeze_date(as_of: date) -> None:
    from app.core import clock

    offset = datetime.combine(as_of, time(9)).astimezone() - datetime.now().astimezone()
    real_now = clock.now
    clock.now = lambda: real_now() + offset


if __name__ == "__main__":
    if os.getenv("E2E_AS_OF"):
        freeze_date(date.fromisoformat(os.environ["E2E_AS_OF"]))
    from scripts import serve

    serve.main()
