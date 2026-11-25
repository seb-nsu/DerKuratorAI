"""Stage 3c of the red trigger: from single candidates to 2–4 options, each a package of measures.

- "least intervention": closes the gap with measures that touch no other project's status (no collateral);
- "fastest effect": a measure on the red project alone (re-baseline; the end date for a schedule gap);
- "portfolio rebuild": may stop a lower-priority project and use its budget;
- plus options for a schedule, resource or risk cause, and always the reference "do nothing".

The mix of a cost option is chosen by an exact 0-1 programme (HiGHS via scipy, like app/planning/optimizer.py):
close the gap, with as few interventions and as little collateral as possible, within the free reserve and
recovery_max_donors; the reserve amount is continuous. If the solver gives no solution, a greedy choice (largest
closure per collateral point) is used and the option says so.

Every package runs at the end once more through the pipeline as one multi-step scenario (base_state chain): the
package verdict is the real verdict of the combination, never a sum of single verdicts. The board approval threshold
applies to the sum the package moves to the red project (decision of 24.11.2026) – the verdict engine accumulates the
budget changes of a project across the steps; a single step just below the threshold is flagged
(recovery_threshold_avoidance_flag)."""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from app.core.business_rules import BusinessRules
from app.recovery.levers import (
    CAPACITY,
    COMPETITOR_HOLD,
    DONOR_STOP,
    END_SHIFT,
    REASON,
    REBASELINE,
    RESERVE,
    RISK,
    SCOPE_CUT,
    SUCCESSOR,
    TRANSFER,
)

logger = logging.getLogger("derkurator.recovery.package")

LEAST, FASTEST, REBUILD, SCHEDULE_OPT, CAPACITY_OPT, RISK_OPT, REFERENCE = (
    "least_intervention", "fastest_effect", "portfolio_rebuild", "schedule", "capacity", "risk", "reference")
OPTION_LABELS = {LEAST: "Least intervention", FASTEST: "Fastest effect", REBUILD: "Portfolio rebuild",
                 SCHEDULE_OPT: "Recover the schedule", CAPACITY_OPT: "Relieve the team", RISK_OPT: "Treat the risks",
                 REFERENCE: "Do nothing (reference)", END_SHIFT: "Move the end date openly"}
MAX_OPTIONS = 4
MIX_LEVERS = (TRANSFER, DONOR_STOP, SCOPE_CUT)
MEASURE_PENALTY = 0.01


def money_value(c: dict[str, Any]) -> float:
    """What a single candidate contributes to closing the cost gap (€)."""
    r = c.get("result") or {}
    measured = max((r.get("gap_before_eur") or 0) - (r.get("gap_after_eur") or 0), 0.0)
    if c["lever"] == DONOR_STOP:
        measured = max(measured, (r.get("cost") or {}).get("budget_to_project_eur") or 0)
    return round(max(measured, c.get("money_to_project") or 0.0), 0)


def choose_mix(items: list[dict[str, Any]], gap: float, reserve_free: float, max_donors: int,
               solver: bool = True) -> tuple[list[dict[str, Any]], float, bool]:
    """Chosen candidates, reserve amount and whether the 0-1 programme solved it (False = greedy). Minimises
    100 × collateral + a small penalty per measure (twice for a measure touching another project) + the share of the
    gap paid from the reserve, so that the chosen measures plus the reserve close the gap. The reserve is the scarce
    portfolio buffer: unused budget of donors goes first when it closes at least 2 % of the gap."""
    values = [money_value(c) for c in items]
    if solver and items:
        try:
            from scipy.optimize import Bounds, LinearConstraint, milp

            n = len(items)
            cost = np.array([100.0 * (c["result"].get("collateral_points") or 0) + MEASURE_PENALTY
                             + (MEASURE_PENALTY if c.get("touches_others") else 0.0)
                             for c in items] + [1.0 / max(gap, 1.0)])
            a = np.zeros((2, n + 1))
            a[0, :n], a[0, n] = values, 1.0  # closure >= gap
            a[1, :n] = [len(c.get("donors") or []) for c in items]  # donors <= max
            result = milp(cost, constraints=LinearConstraint(a, np.array([gap, -np.inf]),
                                                             np.array([np.inf, max_donors])),
                          integrality=np.array([1] * n + [0]),
                          bounds=Bounds(np.zeros(n + 1), np.array([1.0] * n + [max(reserve_free, 0.0)])),
                          options={"time_limit": 10.0})
            if result.x is not None:
                chosen = [c for i, c in enumerate(items) if result.x[i] > 0.5]
                reserve = max(gap - sum(values[i] for i, c in enumerate(items) if result.x[i] > 0.5), 0.0)
                return chosen, round(min(reserve, reserve_free), 0), True
        except Exception:
            logger.exception("Recovery package optimiser failed – greedy fallback")
    order = sorted(range(len(items)), key=lambda i: (items[i]["result"].get("collateral_points") or 0,
                                                     -values[i]))
    chosen, covered, donors = [], 0.0, 0
    for i in order:
        if values[i] < MEASURE_PENALTY * 2 * gap and covered + reserve_free >= gap:
            continue
        d = len(items[i].get("donors") or [])
        if donors + d > max_donors or values[i] <= 0:
            continue
        chosen.append(items[i])
        covered += values[i]
        donors += d
        if covered >= gap:
            break
    return chosen, round(min(max(gap - covered, 0.0), reserve_free), 0), False


def _steps_of(chosen: list[dict[str, Any]], reserve: float, red_pid: str) -> list[dict[str, Any]]:
    """Reserve first (the budget then covers the actual costs), then transfers, stops, scope."""
    steps: list[dict[str, Any]] = []
    if reserve > 0:
        steps.append({"action": "reserve_draw", "project": red_pid, "target": None,
                      "params": {"amount": reserve, "reason": REASON}})
    for lever in (TRANSFER, DONOR_STOP, SCOPE_CUT):
        for c in chosen:
            if c["lever"] == lever:
                steps.extend(c["steps"])
    return steps


def package_facts(steps: list[dict[str, Any]], red_pid: str, rules: BusinessRules) -> list[dict[str, Any]]:
    """`threshold_near`: a single money step just below the board threshold. The board threshold on the package sum
    needs no fact of its own: the verdict engine accumulates every budget change of a project across the steps of a
    scenario, so a package whose steps together exceed the threshold is a board decision already."""
    threshold = rules.board_approval_threshold_eur
    amounts = [float((s.get("params") or {}).get("amount") or 0) for s in steps
               if s["action"] in ("reserve_draw", "budget_transfer") and (s["action"] == "reserve_draw"
                                                                         or s.get("target") == red_pid)]
    facts: list[dict[str, Any]] = []
    if rules.recovery_threshold_avoidance_flag and threshold:
        low = threshold * (1 - rules.recovery_threshold_margin_pct / 100)
        for amount in amounts:
            if low <= amount < threshold:
                facts.append({"type": "threshold_near", "project": red_pid, "amount": round(amount, 0),
                              "threshold": threshold})
    return facts


def _best(cands: list[dict[str, Any]], lever: str, key: str = "cost_pct") -> dict[str, Any] | None:
    pool = [c for c in cands if c["lever"] == lever and c.get("usable")]
    if not pool:
        return None
    return max(pool, key=lambda c: (-(c["result"].get("collateral_points") or 0),
                                    ((c["result"].get("closure") or {}).get(key) or 0)))


def build_options(red_pid: str, causes: list[Any], candidates: list[dict[str, Any]], impact: dict[str, Any],
                  rules: BusinessRules) -> list[dict[str, Any]]:
    """Option specs (steps, why) – evaluated by the engine. The reference option has no steps."""
    kinds = {c.kind for c in causes}
    gap = max((c.gap_eur for c in causes if c.kind == "cost"), default=0.0)
    free = (impact.get("budget") or {}).get("reserve_free_eur") or 0.0
    options: list[dict[str, Any]] = []
    own = [c for c in candidates if c["project"] == red_pid]
    if "cost" in kinds and gap >= rules.recovery_min_gap_eur:
        clean = [c for c in own if c["lever"] in (TRANSFER, SCOPE_CUT) and c.get("usable")
                 and not c["result"].get("collateral_points")]
        chosen, reserve, solved = choose_mix(clean, gap, free, rules.recovery_max_donors)
        steps = _steps_of(chosen, reserve, red_pid)
        if steps:
            options.append({"kind": LEAST, "steps": steps, "solver": solved,
                            "why": "Closes the gap without changing another project's status: "
                                   + ", ".join([f"reserve €{reserve:,.0f}"] * (reserve > 0)
                                               + [c["label"].lower() + (f" ({', '.join(c['donors'])})"
                                                                        if c.get("donors") else "") for c in chosen])
                                   + "."})
        rebase = _best(own, REBASELINE)
        if rebase:
            options.append({"kind": FASTEST, "steps": rebase["steps"], "solver": None,
                            "why": "Acts on the red project alone: the budget follows the forecast (board decision)."})
        mix = [c for c in own if c["lever"] in MIX_LEVERS and c.get("usable")]
        if any(c["lever"] == DONOR_STOP for c in mix):
            stop = next(c for c in mix if c["lever"] == DONOR_STOP)
            rest = [c for c in mix if c is not stop and c["lever"] != SCOPE_CUT]
            chosen, reserve, solved = choose_mix([stop] + rest, gap, free, rules.recovery_max_donors)
            if stop not in chosen:
                chosen, reserve = [stop], round(min(max(gap - money_value(stop), 0), free), 0)
            options.append({"kind": REBUILD, "steps": _steps_of(chosen, reserve, red_pid), "solver": solved,
                            "why": f"Stops {', '.join(stop['donors'])} (lower priority) and uses its free budget"
                                   + (f"; reserve €{reserve:,.0f} for the rest." if reserve > 0 else ".")})
    if "schedule" in kinds:
        capacity = _best(own, CAPACITY, "schedule_pct")
        replans = [c for c in candidates if c["lever"] == SUCCESSOR and c.get("usable")]
        if capacity or replans:
            steps = (capacity["steps"] if capacity else []) + [s for c in replans for s in c["steps"]]
            options.append({"kind": SCHEDULE_OPT, "steps": steps, "solver": None,
                            "why": "Recovers time: " + "; ".join(([capacity["rationale"]] if capacity else [])
                                                                + [c["rationale"] for c in replans])})
        shift = _best(own, END_SHIFT, "schedule_pct")
        if shift:
            options.append({"kind": FASTEST if not any(o["kind"] == FASTEST for o in options) else END_SHIFT,
                            "steps": shift["steps"], "solver": None,
                            "why": "Moves the end date openly; the domino and NPV effect are on the table."})
    if "resources" in kinds:
        hold = _best(candidates, COMPETITOR_HOLD)
        if hold:
            options.append({"kind": CAPACITY_OPT, "steps": hold["steps"], "solver": None, "why": hold["rationale"]})
    if "scope_risk" in kinds:
        risks = [c for c in own if c["lever"] == RISK and c.get("usable")]
        if risks:
            options.append({"kind": RISK_OPT, "steps": [s for c in risks for s in c["steps"]], "solver": None,
                            "why": "; ".join(c["rationale"] for c in risks)})
    upstream = [c for c in candidates if c["project"] != red_pid and c["lever"] == RESERVE and c.get("usable")]
    if upstream and len(options) < MAX_OPTIONS:
        options.append({"kind": "upstream", "steps": upstream[0]["steps"], "solver": None,
                        "why": upstream[0]["rationale"]})
    options = options[:MAX_OPTIONS]
    for letter, option in zip("ABCD", options, strict=False):
        option["key"] = letter
        option["label"] = OPTION_LABELS.get(option["kind"], "Recover the predecessor"
                                             if option["kind"] == "upstream" else option["kind"])
    options.append({"key": "R", "kind": REFERENCE, "label": OPTION_LABELS[REFERENCE], "steps": [], "solver": None,
                    "why": "No intervention: the gap stays, the dependents and the portfolio carry it."})
    return options
