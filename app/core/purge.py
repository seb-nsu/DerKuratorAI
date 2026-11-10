"""Erasure of one user's personal data (Admin Console → "Purge user"): chats, saved scenarios and notifications of the
user are removed, the user id in proposals, incidents and archived stores is replaced by a stable pseudonym. Each store is rewritten
atomically under its own lock. The decision log and the snapshots stay as they are – they are the evidence of
approvals (retention, see README "Data retention"); the purge itself is recorded there with the pseudonym only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import portalocker

from app.core import logs

USER_KEYS = ("requester", "user_id", "decided_by", "owner", "by", "updated_by", "user")


class PurgeError(RuntimeError):
    pass


def pseudonym(user_id: str) -> str:
    return f"[PURGED-{hashlib.sha256(user_id.strip().casefold().encode('utf-8')).hexdigest()[:8].upper()}]"


def _replace(value: Any, pattern: re.Pattern[str], alias: str) -> Any:
    if isinstance(value, str):
        return pattern.sub(alias, value)
    if isinstance(value, list):
        return [_replace(v, pattern, alias) for v in value]
    if isinstance(value, dict):
        return {k: _replace(v, pattern, alias) for k, v in value.items()}
    return value


def _owned(record: dict[str, Any], key: str) -> bool:
    return any(str(record.get(k) or "").strip().casefold() == key for k in USER_KEYS)


def _rewrite(path: Path, user_id: str, drop_owned: bool) -> int:
    """Rewrites a JSONL store without the user: their records dropped (`drop_owned`) or pseudonymised. Returns the
    number of records changed. Lines that are no JSON are kept as they are."""
    if not path.exists():
        return 0
    key, alias = user_id.strip().casefold(), pseudonym(user_id)
    pattern = re.compile(rf"(?<![\w-]){re.escape(user_id.strip())}(?![\w-])", re.IGNORECASE)
    with portalocker.Lock(str(path.with_name(path.name + ".lock")), mode="a", timeout=15, check_interval=0.05):
        lines, changed = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                lines.append(line)
                continue
            if drop_owned and isinstance(record, dict) and _owned(record, key):
                changed += 1
                continue
            cleaned = _replace(record, pattern, alias)
            changed += cleaned != record
            lines.append(json.dumps(cleaned, ensure_ascii=False, default=str))
        tmp = path.with_name(path.name + ".purge.tmp")
        tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        os.replace(tmp, path)
    return changed


def purge_user(user_id: str, actor: str | None, role: str | None) -> dict[str, int]:
    """Removes one user's chats and saved scenarios and pseudonymises them in proposals and archives. Only the System
    Admin, never for the own account. Returns counts per store; the caller writes the audit record."""
    from app.core.chat_store import chat_path
    from app.core.runtime_config import require_admin
    from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO

    require_admin(role, "Purging a user")
    user_id = str(user_id or "").strip()
    if not user_id:
        raise PurgeError("No user given.")
    if actor and actor.strip().casefold() == user_id.casefold():
        raise PurgeError("You cannot purge your own account.")
    result = {"chats": 0, "scenarios": 0, "proposals": 0, "incidents": 0, "notifications": 0, "archived": 0}
    roles = (ROLE_ADMIN, ROLE_PMO, ROLE_PM)
    chat_names = {chat_path(r, u).name for r in roles for u in (user_id, user_id.casefold())}
    for directory in [Path(logs.CHAT_DIR), *Path(logs.ARCHIVE_DIR).glob("*/" + Path(logs.CHAT_DIR).name)]:
        for name in chat_names:
            for path in (directory / name, directory / f"{name}.lock"):
                if path.exists():
                    path.unlink()
                    result["chats"] += path.suffix == ".jsonl"
    result["scenarios"] = _rewrite(Path(logs.SCENARIOS_LOG), user_id, drop_owned=True)
    result["proposals"] = _rewrite(Path(logs.PROPOSALS_LOG), user_id, drop_owned=False)
    result["incidents"] = _rewrite(Path(logs.INCIDENTS_LOG), user_id, drop_owned=False)
    result["notifications"] = _rewrite(Path(logs.NOTIFY_OUTBOX), user_id, drop_owned=True)  # their mails and address
    for path in Path(logs.ARCHIVE_DIR).glob("**/*.jsonl"):
        if path.parent.name != Path(logs.CHAT_DIR).name:
            result["archived"] += _rewrite(path, user_id, drop_owned=path.name == Path(logs.SCENARIOS_LOG).name)
    return result
