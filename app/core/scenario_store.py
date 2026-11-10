"""Saved scenarios for the A/B comparison.

The PMO (or a Project Manager) saves the current scenario under a name, e.g. "Scenario A – hard cut". A saved
scenario is an immutable snapshot: request history, rule-based verdict, change set and the budget impact per project
(computed at save time, so a portfolio cut – which only changes project states – still shows its released budget).

Storage: data/saved_scenarios.jsonl – append-only, cross-process lock (like the proposal inbox). A "deleted" event
hides a scenario. Visibility: the PMO sees all saved scenarios; a Project Manager only their own.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import portalocker

from app.core import clock, logs

logger = logging.getLogger("derkurator.scenarios")
MAX_NAME = 80


class ScenarioStoreError(RuntimeError):
    pass


def scenarios_path() -> Path:
    return Path(logs.SCENARIOS_LOG)  # resolved at call time (tests redirect it)


@contextmanager
def _locked() -> Iterator[Path]:
    path = scenarios_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(path.with_name(path.name + ".lock")), mode="a", timeout=15, check_interval=0.05):
            yield path
    except portalocker.exceptions.LockException as exc:
        raise ScenarioStoreError("The scenario store is busy – please retry.") from exc


def _append(path: Path, event: dict[str, Any]) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        raise ScenarioStoreError(f"Scenario could not be saved: {exc}") from exc


def save_scenario(name: str, owner: str, role: str, turns: list[str], verdict: dict[str, Any] | None,
                  changes: list[dict[str, Any]], budget_impact: dict[str, float], environment: str = "") -> dict[str, Any]:
    name = " ".join(str(name or "").split())[:MAX_NAME]
    if not name:
        raise ScenarioStoreError("Please give the scenario a name.")
    if not changes:
        raise ScenarioStoreError("Only scenarios with proposed changes can be saved for a comparison.")
    entry = {
        "type": "saved", "id": uuid.uuid4().hex[:10], "timestamp": clock.stamp(),
        "environment": environment or logs.current_environment(), "name": name, "owner": owner, "role": role,
        "turns": list(turns or []), "verdict": verdict or {}, "changes": changes,
        "budget_impact": {str(k): round(float(v), 2) for k, v in (budget_impact or {}).items()},
    }
    with _locked() as path:
        if any(s["name"].casefold() == name.casefold() and s["owner"] == owner for s in _read(path)):
            raise ScenarioStoreError(f"You already saved a scenario named '{name}'.")
        _append(path, entry)
    logger.info("Scenario %s '%s' saved by %s (%d changes)", entry["id"], name, owner, len(changes))
    return entry


def delete_scenario(scenario_id: str, by: str, role: str) -> None:
    from app.core.security import ROLE_PMO

    with _locked() as path:
        current = next((s for s in _read(path) if s["id"] == scenario_id), None)
        if current is None:
            raise ScenarioStoreError(f"Scenario {scenario_id} not found.")
        if current["owner"] != by and role != ROLE_PMO:
            raise ScenarioStoreError("Only the owner or the PMO can delete a saved scenario.")
        _append(path, {"type": "deleted", "id": scenario_id, "by": by, "timestamp": clock.stamp()})


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        records, malformed = logs.read_jsonl(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.error("Scenario store unreadable: %s", exc)
        return []
    if malformed:
        logger.warning("Scenario store: %d corrupt line(s) skipped", malformed)
    saved = {r["id"]: r for r in records if r.get("type") == "saved" and r.get("id")}
    for r in records:
        if r.get("type") == "deleted":
            saved.pop(r.get("id"), None)
    return list(saved.values())


def list_scenarios(role: str, user_id: str) -> list[dict[str, Any]]:
    """Newest first. The PMO sees all scenarios; everyone else sees only their own."""
    from app.core.security import ROLE_PMO

    items = [s for s in _read(scenarios_path()) if role == ROLE_PMO or s.get("owner") == user_id]
    # ascending stable sort, then reversed: scenarios saved within the same second keep "last saved first"
    return list(reversed(sorted(items, key=lambda s: str(s.get("timestamp", "")))))


def get_scenario(scenario_id: str) -> dict[str, Any] | None:
    return next((s for s in _read(scenarios_path()) if s["id"] == scenario_id), None)
