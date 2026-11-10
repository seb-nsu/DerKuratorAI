"""Creates one entry for the user file (AUTH_USERS_FILE) – the password is read without echo and never stored.

    python scripts/hash_password.py "Jane Doe" "Project Manager (Restricted)" [jane.doe@example.com]

Roles: "System Admin", "PMO (Full Access)", "Project Manager (Restricted)", "Portfolio Board". A project manager's
user ID must equal the name in the portfolio's `project_manager` column (row-level security). The optional mail
address receives the account's notifications (docs/PILOT_OPERATIONS.md › Notifications). Paste the printed JSON object
into the "users" list of the file.
"""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.auth import ROLES, hash_password


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or argv[1] not in ROLES or (len(argv) == 3 and "@" not in argv[2]):
        print(__doc__)
        return 2
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match.")
        return 1
    entry = {"user_id": argv[0], "role": argv[1], "display_name": argv[0], "password_hash": hash_password(password)}
    if len(argv) == 3:
        entry["email"] = argv[2].strip()
    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
