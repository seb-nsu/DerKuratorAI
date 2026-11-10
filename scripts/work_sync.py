"""Reads the Microsoft Planner plans and monday.com boards of the configured mapping into their local snapshots
(DATA_SOURCE=mapped or servicenow, a mapping with a `planner` or `monday` source).

    python scripts/work_sync.py

Credentials come from the environment or .env: MS_GRAPH_TOKEN, or MS_GRAPH_TENANT_ID + MS_GRAPH_CLIENT_ID +
MS_GRAPH_CLIENT_SECRET (Planner); MONDAY_TOKEN (monday.com). Suitable for a scheduler; the app loads the new snapshot
on its next request. Exit code 0 when every source was synced, 1 when one failed (its snapshot stays unchanged).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data_adapters import DataSourceError, sync_work_management


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 2
    try:
        reports = sync_work_management()
    except DataSourceError as exc:
        print(f"Sync failed: {exc}")
        return 1
    for report in reports:
        print(report.summary())
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
