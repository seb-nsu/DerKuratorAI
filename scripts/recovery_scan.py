"""Red trigger: scans the portfolio for projects that turned red and stores a recommendation for each.

    python scripts/recovery_scan.py            scan (idempotent; the first scan only lays down the baseline)
    python scripts/recovery_scan.py --verify   check the hash chain of the recommendation store

Suitable for a scheduler, e.g. daily at 6:00: the calculated status also tips without a data change (an end date
passes, a status report gets too old). Does nothing while the business rule recovery_enabled is off. Nothing is
written to the portfolio – a recommendation becomes a proposal only through a person.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str]) -> int:
    from app.recovery import store
    from app.recovery.hooks import enabled, scan_now

    if argv == ["--verify"]:
        result = store.verify()
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    if argv:
        print(__doc__)
        return 2
    if not enabled():
        print("The red trigger is off (business rule recovery_enabled) – nothing scanned.")
        return 0
    result = scan_now("scheduler")
    print(f"Scanned {result.get('scanned', 0)} open projects; baseline: {result.get('baseline', False)}; "
          f"new recommendations: {', '.join(result.get('stored') or []) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
