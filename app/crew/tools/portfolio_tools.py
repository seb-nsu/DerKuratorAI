"""Advanced PMO scenarios: put a project on hold, and a prioritised portfolio cut list.

Both tools calculate deterministically (no LLM), respect the Data Steward quality gate and the RLS scope of the
scenario context, record their proposed changes in the ledger (approval / four-eyes applies) and report facts for
the verdict engine. Business assumptions come from the governance config (current_rules()).

On hold vs. cancellation: a hold has no exit/contract costs and no impairment, but a standby team keeps know-how
(capacity kept, cost continues), the restart needs a ramp-up, and benefits move later (NPV) – successors may miss
their required-by dates. A cancellation releases budget and capacity for good.

Portfolio cut: "find €2 M savings" -> a ranked cut list of complete project stops. Ranking (cut first):
lowest priority (highest priority number), then lowest value_score, then lowest risk of NOT performing; ties go to the
project with the lowest completion, then the smallest benefit forgone, then the fewest dependents. From the ranked
candidates the smallest combination that reaches the target is chosen (fewest stops; among those the lowest ranked
ones) and its surplus over the target is reported – a greedy sum would stop a large, nearly finished flagship just
because it releases the most. `risk_of_not_performing` is free text in ServiceNow; it is scored by keywords
(regulatory / compliance / audit / security / penalty / contractual / legal -> high). Projects up to the protected
priority (business rule), blocked projects (quality gate), closed projects and projects without releasable budget are
excluded and listed with the reason.

Value-optimal cut (method "value"): instead of the ranking, the combination of stops that reaches the target while
losing the least strategic value (strategy score 0-100 from the weighted contribution to the strategic goals, see
dataset.strategy_scores; each stop also counts 0.01 so that fewer stops win a tie). Solved exactly as a min-cover
knapsack (dynamic programming over the net release in steps of 1/4000 of the target, rounded down so a hit always
reaches the target). The ranked cut list is shown next to it for comparison; protection and exclusions are the same.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any

import numpy as np

from app.core.business_rules import current_rules
from app.crew.tools.context import as_of, get_scenario_df, get_table, record_change, record_fact
from app.crew.tools.simulator_tools import (
    INPUT_PREFIX,
    _add_months,
    _eur,
    _monthly_burn,
    _num,
    _open_project,
    _opt_num,
    _safe,
    _to_date,
    indicative_note,
    repeated_call,
)
from app.crew.tools.timeline_effects import schedule_effects
from app.data_adapters.dataset import CLOSED_PROJECT_STATES
from app.llm.tools import tool

HIGH_RNP_KEYWORDS = ("regulat", "compliance", "audit", "security", "penalt", "contract", "legal", "mandatory", "fine")
RNP_LABELS = {3: "high", 2: "medium", 1: "low"}
CUT_METHODS = ("ranked", "value")
KNAPSACK_STEPS = 4000  # resolution of the value-optimal cut: the target is split into this many release steps
STOP_PENALTY = 0.01  # strategic value counted per stop on top of its score – fewer stops win a tie
SUBSET_SEARCH_MAX = 16  # exhaustive search up to 2^16 combinations; larger portfolios fall back to the ranked order
NO_CAPACITY_KEPT = "No capacity kept – know-how loss likely; the restart may take longer than the ramp-up assumes."


def rnp_severity(text: Any) -> int:
    """Risk of NOT performing (free text) -> 3 high / 2 medium / 1 low (keyword-based, documented above)."""
    value = str(text or "").strip().lower()
    if not value or value in ("nan", "none"):
        return 1
    return 3 if any(k in value for k in HIGH_RNP_KEYWORDS) else 2


# =========================================================================== on hold
@_safe
def on_hold(project_id: str, months: float, retention_pct: float | None = None) -> str:
    # a pause is possible despite the quality gate; its figures are then indicative (condition, not NO-GO)
    p = _open_project(project_id, allow_blocked=True, needs=("budget_cost_eur", "actual_cost_eur"))
    if isinstance(p, str):
        return p
    rules = current_rules()
    hold = _num(months, float("nan"))
    if math.isnan(hold) or hold <= 0:
        return f"{INPUT_PREFIX} months must be a positive number (duration of the hold)."
    if hold > rules.max_hold_months:
        return f"ERROR: A hold of {hold:g} months exceeds {rules.max_hold_months} months – consider a cancellation instead."
    retention = rules.hold_retention_pct if retention_pct is None else _num(retention_pct, float("nan"))
    if math.isnan(retention) or not 0 <= retention <= 100:
        return f"{INPUT_PREFIX} retention_pct must be between 0 and 100."
    end = _to_date(p.get("planned_end_date"))
    if end is None:
        return f"ERROR: No planned_end_date recorded for {p['project_number']}."
    if str(p.get("state")) == "On Hold":
        return f"{INPUT_PREFIX} {p['project_number']} is already on hold."

    pid, fte, rate = p["project_number"], _num(p.get("current_fte")), rules.fte_monthly_cost_eur
    burn = _monthly_burn(p)
    kept_fte = fte * retention / 100
    standby_cost = kept_fte * rate * hold
    ramp = rules.hold_restart_ramp_months
    restart_cost = burn * min(ramp, hold)  # remobilising never costs more than the pause itself
    shift = hold + ramp
    new_end = _add_months(end, shift)
    remaining_budget = max(_num(p.get("budget_cost_eur")) - _num(p.get("actual_cost_eur")), 0.0)
    etc = _opt_num(p.get("estimate_to_completion_eur"))
    deferred = min(burn * hold, etc if etc is not None else remaining_budget)  # never more than the work still open

    record_change(pid, "state", "On Hold", "simulate_on_hold", f"{hold:g} months, {retention:g} % capacity kept")
    record_change(pid, "planned_end_date", new_end.isoformat(), "simulate_on_hold")
    lines = [
        f"RESULT simulate_on_hold | {pid} ({p['project_name']}) paused for {hold:g} months",
        "- No exit/contract costs and no CapEx impairment (unlike a cancellation) – the budget stays committed.",
        f"- Capacity kept (standby, {retention:g} %): {kept_fte:.1f} of {fte:g} FTE → standby cost {_eur(standby_cost)} "
        f"({_eur(kept_fte * rate)}/month)",
        f"- Capacity released temporarily: {fte - kept_fte:.1f} FTE for {hold:g} months (≈ {_eur((fte - kept_fte) * rate)}/month)",
        f"- Spend deferred (not saved): ≈ {_eur(deferred)} of the remaining budget {_eur(remaining_budget)}",
        f"- Restart ramp-up: {ramp:g} month(s), remobilisation cost ≈ {_eur(restart_cost)}",
        f"- Planned end: {end:%Y-%m-%d} -> {new_end:%Y-%m-%d} (+{shift:g} months incl. ramp-up)",
        f"- Total extra cost of the hold: {_eur(standby_cost + restart_cost)}",
    ]
    if kept_fte == 0:
        lines.append(f"⚠ {NO_CAPACITY_KEPT}")
        record_fact("no_capacity_kept", project=pid, warning=NO_CAPACITY_KEPT)
    record_fact("on_hold", project=pid, months=hold, restart=_add_months(as_of(), hold).isoformat(),
                standby_fte=round(kept_fte, 2), extra_cost=round(standby_cost + restart_cost, 2))
    lines += indicative_note(pid)
    lines += schedule_effects(p, end, new_end, shift, standby_cost + restart_cost)
    return "\n".join(lines)


# =========================================================================== portfolio cut
@_safe
def portfolio_cut(target_savings_eur: float, method: str = "ranked") -> str:
    target = _num(target_savings_eur, float("nan"))
    if math.isnan(target) or target <= 0:
        return f"{INPUT_PREFIX} target_savings_eur must be a positive amount in EUR."
    method = str(method or "ranked").strip().lower()
    if method not in CUT_METHODS:
        return f"{INPUT_PREFIX} method must be one of {', '.join(CUT_METHODS)}."
    rules = current_rules()
    exit_rate = rules.exit_cost_rate_pct / 100
    protected = rules.cut_protected_max_priority

    candidates, excluded = [], []
    dependents = _dependent_counts()
    for row in get_scenario_df().to_dict(orient="records"):
        pid = str(row["project_number"])
        if str(row.get("state")) in CLOSED_PROJECT_STATES:
            continue  # already stopped – nothing to release
        priority = _opt_num(row.get("priority"))
        if priority is not None and protected and priority <= protected:
            excluded.append(f"{pid} (protected: priority {priority:g})")
            continue
        p = _open_project(pid, needs=("budget_cost_eur", "actual_cost_eur"))
        if isinstance(p, str):
            excluded.append(f"{pid} ({'quality gate' if 'Data quality' in p else 'not simulable'})")
            continue
        remaining = max(_num(p.get("budget_cost_eur")) - _num(p.get("actual_cost_eur")), 0.0)
        release = remaining * (1 - exit_rate)
        if release <= 0:
            excluded.append(f"{pid} (no releasable budget)")
            continue
        candidates.append({
            "pid": pid, "name": p.get("project_name"), "priority": priority if priority is not None else 9,
            "value": _opt_num(p.get("value_score")) or 0, "rnp": rnp_severity(p.get("risk_of_not_performing")),
            "release": release, "exit": remaining * exit_rate, "fte": _num(p.get("current_fte")),
            "complete": _num(p.get("percent_complete")), "benefit": _num(p.get("planned_benefit_eur")),
            "dependents": dependents.get(pid, 0), "strategy": _num(p.get("strategy_score")),
        })

    ranked = sorted(candidates, key=lambda c: (-c["priority"], c["value"], c["rnp"], c["complete"], c["benefit"],
                                               c["dependents"], -c["release"]))
    selected = _smallest_cut(ranked, target)
    if method == "value":
        return _value_cut(ranked, selected, target, excluded, rules)
    total = sum(c["release"] for c in selected)

    lines = [f"RESULT simulate_portfolio_cut | savings target {_eur(target)} – cut list of {len(selected)} project stop(s), "
             f"net release {_eur(total)}",
             f"- Ranking: lowest priority first (1 = highest), then lowest value score, then lowest risk of not performing, "
             f"then lowest completion, smallest benefit forgone and fewest dependents; the fewest stops that reach the "
             f"target are chosen; exit costs {rules.exit_cost_rate_pct:g} % deducted"]
    cumulative = 0.0
    for rank, c in enumerate(selected, start=1):
        cumulative += c["release"]
        lines.append(f"  {rank}. {c['pid']} ({c['name']}) – priority {c['priority']:g}, value {c['value']:g}/5, "
                     f"risk of not performing {RNP_LABELS[c['rnp']]} → net release {_eur(c['release'])} "
                     f"(exit costs {_eur(c['exit'])}, {c['fte']:g} FTE freed), cumulative {_eur(cumulative)}")
        record_change(c["pid"], "state", "Canceled", "simulate_portfolio_cut", f"cut list rank {rank} of {len(selected)}")
    reserve = [c for c in ranked if c not in selected][:3]
    if reserve:
        lines.append("- Next candidates (not needed for the target): "
                     + "; ".join(f"{c['pid']} ({_eur(c['release'])})" for c in reserve))
    if excluded:
        lines.append(f"- Excluded: {', '.join(excluded)}")
    if selected and total >= target:
        lines.append(f"- Surplus over the target: {_eur(total - target)}")
    if total < target:
        lines.append(f"- ⚠ Savings target not reached: {_eur(total)} of {_eur(target)} "
                     f"(shortfall {_eur(target - total)}) with all eligible projects.")
    lines += _cut_dependents([c["pid"] for c in selected])
    record_fact("portfolio_cut", target=round(target, 2), achieved=round(total, 2), projects=[c["pid"] for c in selected],
                method="ranked")
    return "\n".join(lines)


def _value_cut(ranked: list[dict[str, Any]], ranked_cut: list[dict[str, Any]], target: float, excluded: list[str],
               rules: Any) -> str:
    """The value-optimal cut list, compared with the ranked one (same candidates and exclusions)."""
    selected = _min_value_cut(ranked, target)
    total = sum(c["release"] for c in selected)
    lost = sum(c["strategy"] for c in selected)
    lines = [f"RESULT simulate_portfolio_cut | savings target {_eur(target)} – value-optimal cut list of "
             f"{len(selected)} project stop(s), net release {_eur(total)}, strategic value lost {lost:g}",
             f"- Method: the combination of stops that reaches the target with the least strategic value lost "
             f"(strategy score 0-100 per project, from the weighted contribution to the strategic goals; without a "
             f"goal mapping value score x 20); exit costs {rules.exit_cost_rate_pct:g} % deducted"]
    cumulative = 0.0
    for rank, c in enumerate(selected, start=1):
        cumulative += c["release"]
        lines.append(f"  {rank}. {c['pid']} ({c['name']}) – strategy score {c['strategy']:g}, priority "
                     f"{c['priority']:g} → net release {_eur(c['release'])} (exit costs {_eur(c['exit'])}, "
                     f"{c['fte']:g} FTE freed), cumulative {_eur(cumulative)}")
        record_change(c["pid"], "state", "Canceled", "simulate_portfolio_cut",
                      f"value-optimal cut list {rank} of {len(selected)}")
    ranked_ids, value_ids = [c["pid"] for c in ranked_cut], [c["pid"] for c in selected]
    ranked_lost = sum(c["strategy"] for c in ranked_cut)
    if sorted(ranked_ids) == sorted(value_ids):
        lines.append("- Comparison: the ranked cut list (priority first) stops the same projects.")
    else:
        lines.append(f"- Comparison with the ranked cut list (priority first): {', '.join(ranked_ids) or '–'} – "
                     f"net release {_eur(sum(c['release'] for c in ranked_cut))}, strategic value lost "
                     f"{ranked_lost:g} ({ranked_lost - lost:+g} against this list)")
    if excluded:
        lines.append(f"- Excluded: {', '.join(excluded)}")
    if selected and total >= target:
        lines.append(f"- Surplus over the target: {_eur(total - target)}")
    if total < target:
        lines.append(f"- ⚠ Savings target not reached: {_eur(total)} of {_eur(target)} "
                     f"(shortfall {_eur(target - total)}) with all eligible projects.")
    lines += _cut_dependents(value_ids)
    record_fact("portfolio_cut", target=round(target, 2), achieved=round(total, 2), projects=value_ids, method="value",
                value_lost=round(lost, 1), ranked_projects=ranked_ids, ranked_value_lost=round(ranked_lost, 1))
    return "\n".join(lines)


def _min_value_cut(cands: list[dict[str, Any]], target: float) -> list[dict[str, Any]]:
    """Min-cover knapsack: the stops whose net release reaches `target` with the least strategic value lost
    (strategy score + STOP_PENALTY per stop). Exact on a grid of target/KNAPSACK_STEPS; releases are rounded down so
    a solution always reaches the target. If no combination reaches it, all candidates are returned (the shortfall
    becomes a condition). Returned in the order of `cands`."""
    if not cands:
        return []
    unit = target / KNAPSACK_STEPS
    need = KNAPSACK_STEPS
    if sum(c["release"] for c in cands) < target:
        return list(cands)
    best = np.full(need + 1, np.inf)
    best[0] = 0.0
    origin = []  # per candidate: for each reached step, the step it came from (-1 = candidate not taken)
    for c in cands:
        w = min(int(c["release"] // unit), need)
        loss = max(c["strategy"], 0.0) + STOP_PENALTY
        take = np.full(need + 1, np.inf)
        source = np.full(need + 1, -1, dtype=np.int64)
        if w > 0:
            take[w:need] = best[:need - w] + loss
            source[w:need] = np.arange(0, need - w)
            tail = int(np.argmin(best[need - w:])) + need - w  # every step from need-w on reaches the target
            take[need], source[need] = best[tail] + loss, tail
        improved = take < best
        origin.append(np.where(improved, source, -1))
        best = np.where(improved, take, best)
    if not np.isfinite(best[need]):  # only reachable without rounding – fall back to the ranked selection
        return _smallest_cut(cands, target)
    chosen, step = [], need
    for i in range(len(cands) - 1, -1, -1):
        if origin[i][step] >= 0:
            chosen.append(i)
            step = int(origin[i][step])
    return [cands[i] for i in sorted(chosen)]


def _dependent_counts() -> dict[str, int]:
    """Direct successors per predecessor (Dependencies table) – a tie-breaker of the cut ranking."""
    deps = get_table("Dependencies")
    if deps.empty or "predecessor_project_number" not in deps.columns:
        return {}
    return {str(k): int(v) for k, v in deps["predecessor_project_number"].astype(str).value_counts().items()}


def _smallest_cut(ranked: list[dict[str, Any]], target: float) -> list[dict[str, Any]]:
    """The fewest project stops whose net release reaches the target; among equally many, the lowest ranked ones
    (smallest sum of rank positions), then the smallest surplus. Returned in ranking order. If no combination
    reaches the target, all candidates are returned (the shortfall becomes a condition)."""
    if len(ranked) > SUBSET_SEARCH_MAX:  # ranked order until the target is reached
        selected, total = [], 0.0
        for c in ranked:
            if total >= target:
                break
            selected.append(c)
            total += c["release"]
        return selected
    for size in range(1, len(ranked) + 1):
        hits = [combo for combo in combinations(range(len(ranked)), size)
                if sum(ranked[i]["release"] for i in combo) >= target]
        if hits:
            best = min(hits, key=lambda combo: (sum(combo), sum(ranked[i]["release"] for i in combo)))
            return [ranked[i] for i in best]
    return list(ranked)


def _cut_dependents(pids: list[str]) -> list[str]:
    """Every stopped project gets the same dependency check as a single project stop: the graph query records a
    `domino` fact per project, so the verdict engine applies the domino rule deterministically (not left to an
    LLM analyst). The query results themselves are reported as graph results."""
    from app.crew.tools.graph_tools import downstream_impact  # local import avoids a cycle

    if not pids:
        return []
    lines = ["- Dependents of the stopped projects (dependency graph):"]
    for pid in pids:
        first = downstream_impact(pid).splitlines()[0]
        lines.append(f"  - {pid}: {first.split(': ', 1)[-1] if first.startswith('RESULT') else first}")
    lines.append("- ℹ️ Check the task domino of each stopped project (get_task_level_impact) before approval.")
    return lines


# =========================================================================== agent tools
@tool("simulate_on_hold")
def simulate_on_hold(project_id: str, months: float, retention_pct: float = -1.0) -> str:
    """Puts a project ON HOLD for `months` (pause, NOT a cancellation): no exit costs or impairment, but a standby
    team keeps retention_pct % of the capacity (default: organisation rule; pass -1 for the default), a restart
    ramp-up follows, benefits move later (NPV) and successor deadlines are checked in the dependency graph."""
    if skipped := repeated_call("simulate_on_hold", project_id):
        return skipped
    return on_hold(project_id, months, None if retention_pct is None or retention_pct < 0 else retention_pct)


@tool("simulate_portfolio_cut")
def simulate_portfolio_cut(target_savings_eur: float, method: str = "ranked") -> str:
    """Portfolio prioritisation for a savings target across ALL projects in scope (e.g. 'find €2 M savings'):
    method 'ranked' (default) returns the smallest set of project stops (lowest priority, lowest value score, lowest
    risk of not performing, lowest completion first) whose net budget release reaches target_savings_eur; method
    'value' returns the stops that lose the least strategic value (strategy score) and compares them with the ranked
    list – use it when the request asks for a strategic / value-optimal cut. Protected, blocked and closed projects
    are excluded with the reason."""
    if skipped := repeated_call("simulate_portfolio_cut", ""):
        return skipped
    return portfolio_cut(target_savings_eur, method)


PORTFOLIO_TOOLS = [simulate_on_hold, simulate_portfolio_cut]
