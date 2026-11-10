"""Reads the changes from ServiceNow into the local snapshot (DATA_SOURCE=servicenow).

    python scripts/servicenow_sync.py          # changes since the last sync (everything on the first run)
    python scripts/servicenow_sync.py --full   # read every table completely

Credentials come from the environment or .env: SERVICENOW_INSTANCE, SERVICENOW_USER + SERVICENOW_PASSWORD or
SERVICENOW_TOKEN. Suitable for a scheduler (cron, Task Scheduler); the app loads the new snapshot on its next request.
Exit code 0 when every source was synced, 1 when one failed (its snapshot stays unchanged), 2 for a wrong argument.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data_adapters import DataSourceError, sync_servicenow


def main(argv: list[str]) -> int:
    if set(argv) - {"--full"}:
        print(__doc__)
        return 2
    try:
        reports = sync_servicenow(full="--full" in argv)
    except DataSourceError as exc:
        print(f"Sync failed: {exc}")
        return 1
    for report in reports:
        print(report.summary())
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
