"""Stage 1 of the red trigger: notice that a project *turned* red.

The status check (app/crew/tools/status_check.py) only knows the current state. The watch keeps a memory per open
project – the last reported and calculated status, the cost gap and the data state – in data/state/status_watch.json
(shared by every worker, written under a cross-process lock and replaced atomically, app.core.shared_state). Comparing
old and new gives the trigger events:

  T1 reported    `status` changes from Green/Yellow to Red (the project manager escalated)
  T2 calculated  the calculated status changes to Red (catches "reported green, calculated red"; rule
                 recovery_trigger_calculated)
  T3 regap       the project was red already, but its cost gap grew by more than recovery_regap_pct since the last
                 analysis, so an older recommendation does not age silently

Every event carries an idempotency key `project|trigger|data state`: several workers or repeated calls create one
analysis only (the recommendation store refuses a known key). The first scan only lays down the baseline and triggers
nothing – a portfolio that already has red projects does not flood the PMO; the PMO starts those by hand.

`scan` is the one entry point of every path (after a write, after a sync, the scheduler, opening the overview); it is
cheap and idempotent. It runs with a technical system identity across the whole portfolio and never writes data."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from app.core import clock, shared_state
from app.core.business_rules import BusinessRules, current_rules
from app.crew.tools.status_check import StatusCheck, check_dataset

STATE = "status_watch"
SCAN_LOG = "recovery_scans"
SCAN_LOG_SIZE = 50
REPORTED, CALCULATED, REGAP, MANUAL = "reported", "calculated", "regap", "manual"
TRIGGER_LABELS = {REPORTED: "reported red", CALCULATED: "calculated red", REGAP: "gap grew", MANUAL: "started by hand"}
SYSTEM_IDENTITY = "system:recovery"  # the technical identity of the scan; authorize() refuses it every write
_FIELDS = ("status", "state", "budget_cost_eur", "estimate_at_completion_eur", "actual_cost_eur", "percent_complete",
           "approved_end_date", "planned_end_date", "priority")


@dataclass
class Trigger:
    project_id: str
    project_name: str
    trigger: str
    key: str
    data_state: str
    reported: str | None
    calculated: str
    gap_eur: float
    previous: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return TRIGGER_LABELS.get(self.trigger, self.trigger)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "label": self.label}


@dataclass
class ScanResult:
    as_of: str
    scanned: int
    triggers: list[Trigger]
    baseline: bool = False  # the first scan: memory laid down, nothing triggered
    skipped: list[str] = field(default_factory=list)  # keys already analysed

    def to_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "scanned": self.scanned, "baseline": self.baseline,
                "triggers": [t.to_dict() for t in self.triggers], "skipped": list(self.skipped)}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def cost_gap(row: dict[str, Any]) -> float:
    """Forecast (EAC) minus budget, never negative."""
    budget, eac = _num(row.get("budget_cost_eur")), _num(row.get("estimate_at_completion_eur"))
    return round(max((eac or 0) - (budget or 0), 0.0), 2) if budget is not None and eac is not None else 0.0


def data_state(row: dict[str, Any], check: StatusCheck) -> str:
    """Short fingerprint of what the trigger looked at: the project's decisive fields and its last report."""
    body = {k: str(row.get(k)) for k in _FIELDS} | {"last_report": check.last_report, "calculated": check.calculated}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def is_red(entry: dict[str, Any] | None, rules: BusinessRules) -> bool:
    if not entry:
        return False
    return entry.get("reported") == "Red" or (bool(rules.recovery_trigger_calculated) and entry.get("calculated") == "Red")


def scan(dataset: Any, today: date | None = None, rules: BusinessRules | None = None,
         known: set[str] | None = None, source: str = "manual") -> ScanResult:
    """Compares the portfolio with the memory, updates the memory and returns the new trigger events. `known`:
    idempotency keys already analysed (default: the recommendation store)."""
    from app.recovery import store

    rules = rules or current_rules()
    today = today or clock.today()
    known = store.known_keys() if known is None else known
    checks = check_dataset(dataset, today, rules)
    rows = {str(r.get("project_number")): r for r in dataset.projects.to_dict("records")}
    triggers: list[Trigger] = []
    skipped: list[str] = []
    with shared_state.update(STATE) as memory:
        baseline = not memory.get("initialized")
        projects: dict[str, Any] = memory.setdefault("projects", {})
        seen = set()
        for check in checks:
            pid = check.project_id
            seen.add(pid)
            row = rows.get(pid, {})
            gap, state = cost_gap(row), data_state(row, check)
            previous = projects.get(pid)
            current = {"reported": check.reported, "calculated": check.calculated, "gap_eur": gap,
                       "data_state": state, "seen": today.isoformat(),
                       "analysed_gap_eur": (previous or {}).get("analysed_gap_eur")}
            projects[pid] = current
            if baseline:
                continue
            kinds: list[str] = []
            if check.reported == "Red" and (previous or {}).get("reported") != "Red":
                kinds.append(REPORTED)
            if rules.recovery_trigger_calculated and check.calculated == "Red" \
                    and (previous or {}).get("calculated") != "Red":
                kinds.append(CALCULATED)
            analysed = (previous or {}).get("analysed_gap_eur")
            if not kinds and is_red(previous, rules) and is_red(current, rules) and analysed is not None \
                    and gap > analysed * (1 + rules.recovery_regap_pct / 100) \
                    and gap - analysed >= rules.recovery_min_gap_eur:
                kinds.append(REGAP)
            if not kinds:
                continue
            kind = kinds[0]  # one analysis per transition; a reported and calculated red at once is one event
            key = f"{pid}|{kind}|{state}"
            if key in known:
                skipped.append(key)
                continue
            triggers.append(Trigger(pid, check.project_name, kind, key, state, check.reported, check.calculated, gap,
                                    {k: (previous or {}).get(k) for k in ("reported", "calculated", "gap_eur")}))
        for pid in [p for p in projects if p not in seen]:  # closed or removed projects leave the memory
            projects.pop(pid, None)
        memory["initialized"] = True
        memory["last_scan"] = clock.stamp()
    result = ScanResult(today.isoformat(), len(checks), triggers, baseline, skipped)
    _log_scan(result, source)
    return result


def mark_analysed(project_id: str, gap_eur: float) -> None:
    """The gap an analysis was made for: the reference of the regap trigger (T3)."""
    with shared_state.update(STATE) as memory:
        entry = memory.setdefault("projects", {}).setdefault(str(project_id), {})
        entry["analysed_gap_eur"] = gap_eur


def reset() -> None:
    """Forget the memory (the next scan lays down a new baseline)."""
    shared_state.clear(STATE)


def memory() -> dict[str, Any]:
    return shared_state.read(STATE)


def _log_scan(result: ScanResult, source: str) -> None:
    with shared_state.update(SCAN_LOG) as log:
        entries = log.setdefault("scans", [])
        entries.append({"at": clock.stamp(), "source": source, "as_of": result.as_of, "scanned": result.scanned,
                        "baseline": result.baseline, "triggers": [t.key for t in result.triggers],
                        "skipped": len(result.skipped)})
        del entries[:-SCAN_LOG_SIZE]


def scan_log() -> list[dict[str, Any]]:
    """The last scans, newest first (Admin Console › Logs › Recovery scans)."""
    return list(reversed(shared_state.read(SCAN_LOG).get("scans") or []))
