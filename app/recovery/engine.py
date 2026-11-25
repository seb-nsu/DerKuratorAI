"""The red trigger end to end: trigger -> diagnosis -> portfolio impact -> candidates -> evaluation -> packages ->
explanation -> recommendation store. Deterministic up to the explanation; the explanation is written by the code
(offline) and, when a model is configured, an LLM may add a readable text whose figures are checked against the tool
results (app/crew/advisory_check.py). Nothing is written to the portfolio: a recommendation only becomes a proposal
when a person adopts it, re-runs it in the Steering Console and submits it; a second person approves it."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import date
from typing import Any

from app.core import clock
from app.core.business_rules import BusinessRules, current_rules
from app.crew.tools.status_check import check_dataset
from app.recovery import evaluate, levers, package, store, watch
from app.recovery.diagnosis import COST, REPORTING, UNMEASURED, Cause, diagnose
from app.recovery.impact import build_impact, red_projects

logger = logging.getLogger("derkurator.recovery")

RECOVERY_RULES = tuple(name for name in BusinessRules.model_fields if name.startswith("recovery_"))


class RecoveryError(RuntimeError):
    pass


def _names(dataset: Any) -> dict[str, str]:
    projects = dataset.projects
    return dict(zip(projects["project_number"].astype(str), projects["project_name"].astype(str), strict=True))


def _blocked(dataset: Any, today: date) -> dict[str, str]:
    from app.crew.agents import DataStewardAgent

    try:
        return dict(DataStewardAgent().run(dataset, today).blocked_reasons)
    except Exception:
        logger.exception("Data Steward failed during the recovery analysis")
        return {str(p): "Data Steward unavailable" for p in dataset.projects["project_number"]}


def _impact_in_context(project_id: str, dataset: Any, checks, causes, today: date,
                       rules: BusinessRules) -> dict[str, Any]:
    import contextvars

    from app.core.security import mask_dataset_pii
    from app.crew.tools import context as ctx

    def run() -> dict[str, Any]:
        ctx.set_portfolio_context(mask_dataset_pii(dataset), {}, as_of=today)
        return build_impact(project_id, dataset, checks, causes, today, rules)

    return contextvars.copy_context().run(run)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in ("timestamp", "created_at")}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def fingerprint(payload: dict[str, Any]) -> str:
    """Determinism check: the same data and rules give the same recommendation (ids and timestamps left out)."""
    body = {k: v for k, v in payload.items()
            if k not in ("created_by", "duration_s", "explanation", "trigger_key", "fingerprint")}
    return hashlib.sha256(json.dumps(_stable(body), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def analyse(project_id: str, dataset: Any, today: date | None = None, rules: BusinessRules | None = None,
            trigger: dict[str, Any] | None = None, by: str = watch.SYSTEM_IDENTITY,
            explain_with_llm: bool = True) -> dict[str, Any]:
    """The recommendation for one project (not stored). `dataset`: the whole portfolio (system identity)."""
    started = time.perf_counter()
    rules = rules or current_rules()
    today = today or clock.today()
    pid = str(project_id)
    rows = {str(r.get("project_number")): r for r in dataset.projects.to_dict("records")}
    if pid not in rows:
        raise RecoveryError(f"Project {pid} not found.")
    checks = check_dataset(dataset, today, rules)
    by_id = {c.project_id: c for c in checks}
    check = by_id.get(pid)
    if check is None:
        raise RecoveryError(f"{pid} is closed – no recovery analysis.")
    red = red_projects(checks, rules)
    causes, report = diagnose(check, rows[pid], dataset, today, rules)
    impact = _impact_in_context(pid, dataset, checks, causes, today, rules)
    blocked = _blocked(dataset, today)
    force = {pid} & set(blocked)  # the red project itself is calculated even when the quality gate holds it
    for pred in impact.get("upstream_red") or []:
        if pred in blocked:
            force.add(pred)
    names = _names(dataset)
    base_probability = (impact.get("portfolio") or {}).get("probability_within_budget_pct")
    candidates, not_possible = levers.generate(pid, causes, impact, dataset, red, set(blocked), today, rules)
    evaluated: list[dict[str, Any]] = []
    for cand in candidates:
        target = cand.project
        target_causes = causes if target == pid else [
            Cause(COST, "Cost gap of the red predecessor.", gap_eur=watch.cost_gap(rows.get(target, {})))]
        outcome = evaluate.run_chain(cand.steps, dataset, names, force, today, rules)
        result = evaluate.measure(outcome, target, target_causes, by_id, rows.get(target, rows[pid]),
                                  dataset, cand.donors, today, rules, base_probability, monte_carlo=False)
        item = {**cand.to_dict(), "result": result}
        item["usable"] = evaluate.usable(result, cand.partial, target)
        evaluated.append(item)
    specs = package.build_options(pid, causes, evaluated, impact, rules)
    options: list[dict[str, Any]] = []
    for spec in specs:
        if not spec["steps"]:
            options.append({**spec, "result": _reference(pid, causes, impact, rows[pid], today, base_probability)})
            continue
        facts = package.package_facts(spec["steps"], pid, rules)
        outcome = evaluate.run_chain(spec["steps"], dataset, names, force, today, rules)
        donors = sorted({s["project"] for s in spec["steps"] if s["project"] != pid}
                        | {s["target"] for s in spec["steps"] if s.get("target") and s["target"] != pid})
        result = evaluate.measure(outcome, pid, causes, by_id, rows[pid], dataset, donors, today, rules,
                                  base_probability, extra_facts=facts)
        options.append({**spec, "result": result,
                        "threshold_flag": any(f["type"] == "threshold_near" for f in facts)})
    used = {c["id"] for o in options for c in evaluated if any(s in o["steps"] for s in c["steps"])}
    rejected = [{"id": c["id"], "label": c["label"], "lever": c["lever"], "rationale": c["rationale"],
                 "verdict": (c["result"].get("verdict") or {}).get("label"),
                 "why": _why_rejected(c)} for c in evaluated if not c["usable"]]
    recommendation: dict[str, Any] = {
        "project_id": pid, "project_name": names.get(pid, pid), "as_of": today.isoformat(),
        "trigger": (trigger or {}).get("trigger") or watch.MANUAL,
        "trigger_label": watch.TRIGGER_LABELS.get((trigger or {}).get("trigger") or watch.MANUAL),
        "trigger_key": (trigger or {}).get("key"), "data_state": watch.data_state(rows[pid], check),
        "reported": check.reported, "calculated": check.calculated, "status_reasons": list(check.reasons),
        "causes": [c.to_dict() for c in causes], "report_hint": (report or {}).get("issues_decisions"),
        "impact": impact, "candidates": evaluated, "not_possible": [n.to_dict() for n in not_possible],
        "rejected": rejected, "options": options, "used_candidates": sorted(used), "forced": sorted(force),
        "rules": {k: getattr(rules, k) for k in RECOVERY_RULES} | {
            "board_approval_threshold_eur": rules.board_approval_threshold_eur},
        "created_by": by, "duration_s": round(time.perf_counter() - started, 2),
    }
    recommendation["recommended"] = _recommended(options)
    recommendation["fingerprint"] = fingerprint(recommendation)
    from app.recovery.explain import explain

    recommendation["explanation"] = explain(recommendation, use_llm=explain_with_llm)
    return recommendation


def _reference(pid: str, causes, impact: dict[str, Any], row: dict[str, Any], today: date,
               probability: float | None) -> dict[str, Any]:
    gap = watch.cost_gap(row)
    dependents = impact.get("dependents") or []
    return {"blocked": False, "decision": "NO DECISION REQUIRED", "verdict": None, "changes": [],
            "closure": {}, "gap_before_eur": gap, "gap_after_eur": gap,
            "days_before": evaluate.schedule_gap_days(row, today), "days_after": evaluate.schedule_gap_days(row, today),
            "calculated_after": "Red", "collateral": [f"{d['id']} waits on {pid}" for d in dependents
                                                      if d.get("hops") == 1]
            + [f"{b['successor']} misses {b['required_by']} by {b['days_late']} days"
               for b in impact.get("deadline_breaches") or []],
            "collateral_points": 0, "cost": {}, "probability_before_pct": probability,
            "probability_after_pct": probability, "forced": [], "touched": []}


def _why_rejected(c: dict[str, Any]) -> str:
    r = c["result"]
    verdict = r.get("verdict") or {}
    if r.get("blocked"):
        return "The Data Steward blocks the data of this measure."
    reasons = verdict.get("reasons") or []
    return (reasons[1] if len(reasons) > 1 else reasons[0]) if reasons else (verdict.get("label") or "not usable")


def _recommended(options: list[dict[str, Any]]) -> str | None:
    """The first option (in the order A–D) whose package verdict is GO or CONDITIONAL GO and that closes the gap
    best with the least collateral; None if no option qualifies."""
    ok = [o for o in options if o["key"] != "R" and o["result"].get("decision") in ("GO", "CONDITIONAL GO")]
    if not ok:
        return None

    def score(o: dict[str, Any]) -> tuple:
        r = o["result"]
        closure = max([v for v in (r.get("closure") or {}).values() if v is not None] or [0])
        return (-(min(closure, 100)), r.get("collateral_points") or 0, len(r.get("touched") or []), o["key"])

    return sorted(ok, key=score)[0]["key"]


def needs_options(recommendation: dict[str, Any]) -> bool:
    return any(c["kind"] not in (REPORTING, UNMEASURED) for c in recommendation.get("causes") or [])


# =========================================================================== orchestration
def store_analysis(recommendation: dict[str, Any], key: str | None = None) -> dict[str, Any] | None:
    key = key or recommendation.get("trigger_key") or (
        f"{recommendation['project_id']}|{watch.MANUAL}|{recommendation['data_state']}|{uuid.uuid4().hex}")
    stored = store.create(recommendation, key)
    if stored is not None:
        gap = max((c.get("gap_eur") or 0 for c in recommendation.get("causes") or [] if c["kind"] == "cost"), default=0)
        watch.mark_analysed(recommendation["project_id"], gap)
        from app.recovery.notify import notify_recommendation

        notify_recommendation(stored)
    return stored


def run_scan(dataset: Any | None = None, today: date | None = None, source: str = "manual",
             rules: BusinessRules | None = None) -> dict[str, Any]:
    """The idempotent entry point of every path. Does nothing while the red trigger is off."""
    rules = rules or current_rules()
    if not rules.recovery_enabled:
        return {"enabled": False, "triggers": [], "stored": []}
    dataset = dataset if dataset is not None else _full_dataset()
    today = today or clock.today()
    store.expire(ttl_days=rules.recovery_ttl_days)
    result = watch.scan(dataset, today, rules, source=source)
    stored = []
    for trig in result.triggers:
        try:
            rec = analyse(trig.project_id, dataset, today, rules, trigger=trig.to_dict())
            item = store_analysis(rec, trig.key)
            if item is not None:
                stored.append(item["id"])
        except Exception:
            logger.exception("Recovery analysis of %s failed", trig.project_id)
    return {"enabled": True, **result.to_dict(), "stored": stored}


def analyse_now(project_id: str, by: str, dataset: Any | None = None, today: date | None = None) -> dict[str, Any]:
    """'Analyse now' / 'Recalculate' of the PMO: works for a project that is red already (no trigger needed)."""
    dataset = dataset if dataset is not None else _full_dataset()
    rec = analyse(project_id, dataset, today, trigger={"trigger": watch.MANUAL}, by=by)
    stored = store_analysis(rec)
    if stored is None:
        raise RecoveryError("The recommendation could not be stored.")
    return stored


def _full_dataset() -> Any:
    from app.data_adapters import get_data_provider

    return get_data_provider().load_dataset()
