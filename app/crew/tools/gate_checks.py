"""Stage-gate criteria: named, deterministic checks of one project, each testable on its own.

The criteria of every phase transition are configuration (`stage_gates` in data/env_<env>.json), as a list of
{"criterion": <name>, "kind": "mandatory" | "advisory"} per transition "<from> -> <to>". A failed mandatory criterion
is a NO-GO of the stage-gate scenario, a failed advisory criterion a condition. The checks read the scenario context,
so a baseline or a risk response proposed in an earlier step of the same scenario counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from app.core.stage_gates import ADVISORY, DEFAULT_STAGE_GATES, MANDATORY, normalize_phase, transition  # noqa: F401


@dataclass(frozen=True)
class GateResult:
    criterion: str
    label: str
    kind: str
    passed: bool
    value: str


def _filled(value: Any) -> bool:
    return value is not None and not (isinstance(value, float) and pd.isna(value)) and bool(str(value).strip())


def _business_case(p: dict[str, Any]) -> tuple[bool, str]:
    ok = _filled(p.get("business_case"))
    return ok, "recorded" if ok else "missing"


def _sponsor(p: dict[str, Any]) -> tuple[bool, str]:
    ok = _filled(p.get("project_sponsor"))
    return ok, str(p.get("project_sponsor")) if ok else "missing"


def _baseline(p: dict[str, Any]) -> tuple[bool, str]:
    from app.crew.tools.context import table_with_pending

    rows = table_with_pending("Baselines", str(p["project_number"]))
    return (not rows.empty), f"{len(rows)} baseline(s)" if not rows.empty else "no baseline"


def _no_dq_errors(p: dict[str, Any]) -> tuple[bool, str]:
    from app.crew.tools.context import blocked_reason

    reason = blocked_reason(str(p["project_number"]))
    return reason is None, "none" if reason is None else reason


def _no_unmitigated_high_risk(p: dict[str, Any]) -> tuple[bool, str]:
    from app.crew.tools.context import get_table, row_value

    risks = get_table("Risks", str(p["project_number"]))
    open_high = []
    for row in risks.to_dict(orient="records"):
        key = str(row.get("risk_number"))
        probability = row_value("Risks", key, "probability")
        status = str(row_value("Risks", key, "risk_status") or "")
        mitigation = row_value("Risks", key, "mitigation")
        if probability == "High" and row.get("impact") == "High" and (status == "Accept" or not _filled(mitigation)):
            open_high.append(key)
    return not open_high, ", ".join(open_high) if open_high else "none"


def _resource_plan_next_3_months(p: dict[str, Any]) -> tuple[bool, str]:
    from app.crew.tools.context import as_of, get_dataset, get_table

    plans = get_table("Resource_Plans", str(p["project_number"]))
    start = get_dataset().reference_month(as_of())
    wanted = {start + pd.DateOffset(months=i) for i in range(3)}
    months = set(pd.to_datetime(plans["month"], errors="coerce").dropna()) if not plans.empty else set()
    covered = len(wanted & months)
    return covered == 3, f"{covered} of 3 months planned"


def _work_nearly_complete(p: dict[str, Any]) -> tuple[bool, str]:
    done = float(pd.to_numeric(p.get("percent_complete"), errors="coerce") or 0)
    return done >= 90, f"{done:g} % complete (gate: 90 %)"


def _work_complete(p: dict[str, Any]) -> tuple[bool, str]:
    done = float(pd.to_numeric(p.get("percent_complete"), errors="coerce") or 0)
    return done >= 100, f"{done:g} % complete (gate: 100 %)"


def _benefit_plan(p: dict[str, Any]) -> tuple[bool, str]:
    from app.crew.tools.context import get_table

    rows = get_table("Benefit_Plans", str(p["project_number"]))
    ok = not rows.empty or _filled(p.get("planned_benefit_eur"))
    return ok, f"{len(rows)} benefit tranche(s)" if not rows.empty else ("planned benefit only" if ok else "missing")


CRITERIA: dict[str, tuple[str, Callable[[dict[str, Any]], tuple[bool, str]]]] = {
    "business_case": ("Business case recorded", _business_case),
    "sponsor": ("Sponsor named", _sponsor),
    "baseline": ("Baseline recorded", _baseline),
    "no_data_quality_errors": ("No Data Steward errors", _no_dq_errors),
    "no_unmitigated_high_risk": ("No high/high risk without mitigation", _no_unmitigated_high_risk),
    "resource_plan_next_3_months": ("Resource plan for the next 3 months", _resource_plan_next_3_months),
    "work_nearly_complete": ("Work at least 90 % complete", _work_nearly_complete),
    "work_complete": ("Work 100 % complete", _work_complete),
    "benefit_plan": ("Benefit plan recorded", _benefit_plan),
}


def configured_gates() -> dict[str, list[dict[str, str]]]:
    try:
        from app.core.runtime_config import get_runtime_config

        return dict(get_runtime_config().stage_gates or DEFAULT_STAGE_GATES)
    except Exception:  # noqa: BLE001 - a gate check never fails because of configuration
        return DEFAULT_STAGE_GATES


def evaluate(project: dict[str, Any], from_phase: str, to_phase: str) -> list[GateResult]:
    """Every criterion of the transition, in configured order."""
    results = []
    for item in configured_gates().get(transition(from_phase, to_phase), []):
        label, check = CRITERIA[item["criterion"]]
        passed, value = check(project)
        results.append(GateResult(item["criterion"], label, item["kind"], passed, value))
    return results

