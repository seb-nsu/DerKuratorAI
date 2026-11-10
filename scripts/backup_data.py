"""Backs up the data folder and checks the hash chain of the decision log.

    python scripts/backup_data.py --target D:/backups/derkurator          # backup, keep the newest 7
    python scripts/backup_data.py --target D:/backups/derkurator --keep 14
    python scripts/backup_data.py --verify                                # hash chain of the live data folder
    python scripts/backup_data.py --verify D:/backups/.../data            # hash chain of a backup or after a restore

Suitable for a scheduler (cron, Task Scheduler), best at night when nobody approves. Exit code 1 if the hash chain is
broken.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import backup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=Path, help="folder that holds the dated backups")
    parser.add_argument("--keep", type=int, default=7, help="number of backups to keep (default 7)")
    parser.add_argument("--verify", nargs="?", const="", metavar="DATA_FOLDER", help="only check the hash chain")
    args = parser.parse_args(argv)
    if args.verify is not None:
        chain = backup.verify(Path(args.verify) if args.verify else None)
        print(f"Hash chain intact ({chain['records']} records)" if chain["ok"] else
              f"Hash chain BROKEN at line {chain['broken_at']}: {chain['reason']}")
        return 0 if chain["ok"] else 1
    if args.target is None:
        parser.print_help()
        return 2
    result = backup.create_backup(args.target, args.keep)
    print(result.summary())
    return 0 if result.chain["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
