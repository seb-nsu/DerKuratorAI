"""Stage 3b of the red trigger: every candidate (and later every package) runs through the real Steering pipeline on
its offline path – deterministic, no LLM, its own scenario context – exactly like a guided what-if. Verdict, change
set and facts come from there. On top, this module measures per candidate:

- gap closure: how much of the measured gap (€ forecast gap, days of slip) the measure closes, in %;
- collateral: does another project turn calculated red on the scenario data (status check), does a required-by date
  break, is a team overloaded;
- cost: exit costs, extra hold costs, NPV effect;
- the portfolio probability before and after (Monte Carlo with the same seed).

and adds the facts of the red trigger to the verdict (rule stage 4, CONDITIONAL GO, never silently):
`collateral_red`, `gap_not_closed`, `donor_strategic`, and for packages `threshold_near`."""
from __future__ import annotations

import contextvars
import logging
from datetime import date
from typing import Any

import pandas as pd

from app.core.business_rules import BusinessRules, candidate_rules
from app.crew.tools.status_check import CLOSED_STATES, StatusCheck, check_project
from app.crew.verdict import CONDITIONAL_GO, GO, INCOMPLETE, NO_GO, compute_verdict
from app.recovery.watch import SYSTEM_IDENTITY, cost_gap

logger = logging.getLogger("derkurator.recovery.evaluate")

COLLATERAL_FACTS = ("collateral_red", "deadline_breach", "overload", "on_hold", "cancel")
PROJECT_FIELDS = ("budget_cost_eur", "estimate_at_completion_eur", "estimate_to_completion_eur", "actual_cost_eur",
                  "planned_end_date", "approved_end_date", "state", "percent_complete", "total_planned_cost_eur")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _day(value: Any) -> date | None:
    try:
        stamp = pd.Timestamp(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def schedule_gap_days(row: dict[str, Any], today: date) -> int:
    """Slip of the planned end against the approved end, or the days past a planned end already passed."""
    planned, approved = _day(row.get("planned_end_date")), _day(row.get("approved_end_date"))
    progress = _num(row.get("percent_complete"))
    slip = (planned - approved).days if planned and approved else 0
    overdue = (today - planned).days if planned and planned < today and (progress is None or progress < 100) else 0
    return max(slip, overdue, 0)


def _run_chain(steps: list[dict[str, Any]], dataset: Any, names: dict[str, str], force_projects: set[str],
               today: date, rules: BusinessRules) -> dict[str, Any]:
    """Runs the steps as one multi-step scenario (base_state chain) in the current context; returns the raw outcome."""
    from app.core.security import ROLE_PMO
    from app.crew.pipeline import run_steering_pipeline
    from app.crew.scenarios import build_guided_scenario
    from app.crew.tools import context as ctx

    state, requests, result = None, [], None
    with candidate_rules(rules):
        for step in steps:
            guided = build_guided_scenario(step["action"], step["project"], names, target=step.get("target"),
                                           **(step.get("params") or {}))
            requests.append(guided.request)
            result = run_steering_pipeline(guided.request, dataset, ROLE_PMO, SYSTEM_IDENTITY, plan=guided.plan,
                                           force=bool(force_projects), force_projects=force_projects,
                                           base_state=state, as_of=today, use_llm=False)
            if result.mode == "blocked":
                break
            state = result.scenario_state
        if result is None or result.mode == "blocked" or state is None:
            return {"blocked": True, "requests": requests,
                    "paper": result.decision_paper if result else "", "verdict": result.verdict if result else None}
        return {"blocked": False, "requests": requests, "state": state, "scenario": ctx.get_scenario_df(),
                "adverse": ctx.adverse_projects(), "changes": result.proposed_changes,
                "forced": sorted(set(force_projects) & set(names)) if force_projects else [],
                "verdict": result.verdict, "paper": result.decision_paper, "warnings": result.warnings}


def run_chain(steps: list[dict[str, Any]], dataset: Any, names: dict[str, str], force_projects: set[str],
              today: date, rules: BusinessRules) -> dict[str, Any]:
    """Isolated: a fresh copy of the caller's context, so no scenario state leaks into the caller or the next run."""
    return contextvars.copy_context().run(_run_chain, steps, dataset, names, force_projects, today, rules)


def apply_scenario(dataset: Any, scenario: pd.DataFrame) -> Any:
    """A copy of the dataset with the scenario's project values (for the status check and the Monte Carlo after)."""
    after = dataset.copy()
    if scenario is None or scenario.empty:
        return after
    by_id = scenario.set_index(scenario["project_number"].astype(str))
    projects = after.projects.copy().astype(object)
    for i, pid in enumerate(projects["project_number"].astype(str)):
        if pid not in by_id.index:
            continue
        for col in PROJECT_FIELDS:
            if col in projects.columns and col in by_id.columns:
                projects.iat[i, projects.columns.get_loc(col)] = by_id.at[pid, col]
    after.projects = projects
    return after


def collateral(base: dict[str, StatusCheck], after: list[StatusCheck], red_pid: str) -> list[dict[str, Any]]:
    """Projects that were not calculated red before and are after (the red project itself excluded)."""
    out = []
    for c in after:
        before = base.get(c.project_id)
        if c.project_id == red_pid or before is None or before.calculated == "Red" or c.calculated != "Red":
            continue
        out.append({"type": "collateral_red", "project": red_pid, "successor": c.project_id,
                    "reason": "; ".join(c.reasons[:2])})
    return out


def donor_goals(dataset: Any, donors: list[str]) -> list[dict[str, Any]]:
    """Donors linked to a strategic goal weighted at least the average goal weight."""
    goals, links = dataset.strategic_goals, dataset.project_goals
    if goals.empty or links.empty or "project_number" not in links.columns or "weight" not in goals.columns:
        return []
    weights = dict(zip(goals["goal_id"], pd.to_numeric(goals["weight"], errors="coerce"), strict=False))
    names = dict(zip(goals["goal_id"], goals.get("goal_name", goals["goal_id"]), strict=False))
    average = float(pd.Series(list(weights.values())).mean() or 0)
    out = []
    for row in links.to_dict("records"):
        pid, gid = str(row.get("project_number")), row.get("goal_id")
        if pid in donors and (weights.get(gid) or 0) >= average:
            out.append({"type": "donor_strategic", "project": pid, "successor": pid, "goal": names.get(gid, gid),
                        "weight": float(weights.get(gid) or 0)})
    return out


def measure(outcome: dict[str, Any], red_pid: str, causes: list[Any], base_checks: dict[str, StatusCheck],
            base_row: dict[str, Any], dataset: Any, donors: list[str], today: date, rules: BusinessRules,
            base_probability: float | None, extra_facts: list[dict[str, Any]] | None = None,
            monte_carlo: bool = True) -> dict[str, Any]:
    """Figures and the final verdict (with the facts of the red trigger) of one candidate or package run."""
    from app.crew.tools.forecast_ranges import simulate_portfolio
    from app.crew.tools.status_check import check_dataset

    if outcome.get("blocked"):
        verdict = outcome.get("verdict")
        return {"verdict": verdict.to_dict() if verdict else None, "blocked": True, "requests": outcome["requests"],
                "decision": verdict.decision if verdict else INCOMPLETE, "changes": []}
    state = outcome["state"]
    after_ds = apply_scenario(dataset, outcome["scenario"])
    after_checks = check_dataset(after_ds, today, rules)
    red_after = next((c for c in after_checks if c.project_id == red_pid), None)
    row_after = next((r for r in after_ds.projects.to_dict("records") if str(r.get("project_number")) == red_pid),
                     base_row)
    if str(row_after.get("state") or "").lower() in CLOSED_STATES:
        red_after = None
    gap_before, gap_after = cost_gap(base_row), cost_gap(row_after)
    days_before, days_after = schedule_gap_days(base_row, today), schedule_gap_days(row_after, today)
    kinds = {c.kind for c in causes}
    closure: dict[str, Any] = {}
    if "cost" in kinds:
        closure["cost_pct"] = round((gap_before - gap_after) / gap_before * 100, 1) if gap_before else None
    if "schedule" in kinds:
        closure["schedule_pct"] = round((days_before - days_after) / days_before * 100, 1) if days_before else None
    facts = [f for f in state.get("facts") or []]
    new_facts = collateral(base_checks, after_checks, red_pid) + donor_goals(dataset, donors) + list(extra_facts or [])
    if "cost" in kinds and gap_after > rules.recovery_min_gap_eur:
        new_facts.append({"type": "gap_not_closed", "project": red_pid, "remaining": round(gap_after, 0)})
    results = state.get("results") or []
    verdict = compute_verdict([r["text"] for r in results if r.get("kind") == "simulation"],
                              [r["text"] for r in results if r.get("kind") == "graph"], facts + new_facts,
                              outcome.get("forced"), rules, as_of=today.isoformat(), adverse=outcome.get("adverse"))
    all_facts = facts + new_facts
    cost = {"exit_cost_eur": round(sum(f.get("exit_cost") or 0 for f in all_facts if f.get("type") == "cancel"), 0),
            "hold_cost_eur": round(sum(f.get("extra_cost") or 0 for f in all_facts if f.get("type") == "on_hold"), 0),
            "npv_delta_eur": round(sum(f.get("delta_npv") or 0 for f in all_facts if f.get("type") == "npv_change"), 0),
            "budget_to_project_eur": round((_num(row_after.get("budget_cost_eur")) or 0)
                                           - (_num(base_row.get("budget_cost_eur")) or 0), 0)}
    collateral_items = [f for f in all_facts if f.get("type") in COLLATERAL_FACTS and f.get("project") != red_pid
                        or f.get("type") == "collateral_red"
                        or (f.get("type") in ("deadline_breach",) and f.get("successor") != red_pid)]
    probability_after = None
    if monte_carlo:
        try:
            probability_after = simulate_portfolio(after_ds, today, rules)["summary"].get("probability_within_budget_pct")
        except Exception:  # noqa: BLE001 - context only
            logger.exception("Monte Carlo after a recovery candidate failed")
    return {
        "blocked": False, "requests": outcome["requests"], "verdict": verdict.to_dict(), "decision": verdict.decision,
        "changes": outcome.get("changes") or [], "facts": all_facts, "recovery_facts": new_facts,
        "closure": closure, "gap_before_eur": gap_before, "gap_after_eur": gap_after, "days_before": days_before,
        "days_after": days_after, "calculated_after": red_after.calculated if red_after else "closed",
        "collateral": [_collateral_text(f) for f in collateral_items], "collateral_points": len(collateral_items),
        "cost": cost, "probability_before_pct": base_probability, "probability_after_pct": probability_after,
        "forced": outcome.get("forced") or [], "touched": sorted({str(c.get("project_id")) for c in
                                                                  outcome.get("changes") or []}),
    }


def _collateral_text(f: dict[str, Any]) -> str:
    kind = f.get("type")
    if kind == "collateral_red":
        return f"{f['successor']} turns calculated red"
    if kind == "deadline_breach":
        return f"{f.get('successor')} misses its required-by date {f.get('required_by')}"
    if kind == "overload":
        return f"team overload on {f.get('project')}"
    if kind == "on_hold":
        return f"{f.get('project')} is paused"
    if kind == "cancel":
        return f"{f.get('project')} is stopped"
    return str(kind)


def usable(result: dict[str, Any], partial: bool, red_pid: str) -> bool:
    """A candidate may go into a package: GO or CONDITIONAL GO; a partial lever also when its only blocker is the red
    project's own gap (the budget stays below the actual costs until the rest of the package covers it)."""
    decision = result.get("decision")
    if decision in (GO, CONDITIONAL_GO):
        return True
    if not partial or decision != NO_GO:
        return False
    reasons = (result.get("verdict") or {}).get("reasons") or []
    detail = [r for r in reasons[1:]]
    return bool(detail) and all(red_pid in r and "below the actual costs" in r for r in detail)
