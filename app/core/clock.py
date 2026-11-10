"""The one clock of the app: timezone-aware timestamps for audit, proposals, snapshots and chats, and the reference
date (as_of) of every date-dependent calculation. Tests freeze both by patching `now`."""
from __future__ import annotations

from datetime import date, datetime


def now() -> datetime:
    """Current time with the local UTC offset (stored as e.g. "2026-10-02T18:40:00+02:00")."""
    return datetime.now().astimezone()


def stamp() -> str:
    """ISO timestamp with seconds and UTC offset – the format of every stored record."""
    return now().isoformat(timespec="seconds")


def today() -> date:
    """Reference date (as_of) when the caller gives none."""
    return now().date()


def parse(value: object) -> datetime:
    """A stored timestamp as an aware datetime; records written before timestamps carried an offset count as local
    time. Raises ValueError on text that is no timestamp."""
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.astimezone()
