"""Value-optimal filling of a planning scenario: the second proposal next to the filling in rank order.

The rank-order filling takes one demand after the other and keeps what fits. That can leave money unused: a large
demand early in the rank takes an envelope that two smaller demands with more value together would have used. The
optimizer chooses, for every free demand, either one start (its planned start or a later whole year within its latest
start) or Out, so that

  1. as many mandatory demands as possible are in the plan,
  2. the sum of the scores of the planned demands is as high as possible,
  3. every demand that still fits is taken (each counts one point on top of its score),
  4. earlier starts and better ranks win a tie,

while every budget envelope (hard and soft), every capacity limit and every dependency holds – the same limits as the
rank-order filling, on top of what running projects, approved and forced-in demands already bind. It is an exact
0-1 integer programme (HiGHS via scipy), deterministic for the same data. If the solver gives no solution, the
rank-order choice is used and the result says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

TIME_LIMIT_S = 20.0
DEFER_PENALTY = 0.001  # per year a start is later than the earliest possible one
RANK_PENALTY = 1e-6  # per rank position – a better rank wins a tie and is deferred last


@dataclass
class Candidate:
    """A free demand with its possible starts (index 0 = the earliest)."""
    demand: str
    rank: int
    score: float
    mandatory: bool
    options: list[Any]  # footprints, one per start
    shifts: list[int]
    scopes: dict[str, str]
    predecessors: list[str]  # free demands that must end before this one starts


def choose(candidates: list[Candidate], book: Any) -> tuple[dict[str, int | None], bool]:
    """Index of the chosen start per demand (None = Out) and whether the optimizer solved it (False = rank order)."""
    if not candidates:
        return {}, True
    columns = [(c, i) for c in candidates for i in range(len(c.options))]
    index = {(c.demand, i): n for n, (c, i) in enumerate(columns)}
    big = 101.0 * (len(candidates) + 1)
    worst = max(c.rank for c in candidates) + 1
    cost = np.array([-((big if c.mandatory else 0.0) + max(c.score, 0.0) + 1.0
                       - i * (DEFER_PENALTY + RANK_PENALTY * (worst - c.rank)) - RANK_PENALTY * c.rank)
                     for c, i in columns])

    rows: list[dict[int, float]] = []
    upper: list[float] = []
    for c in candidates:  # one start at most
        rows.append({index[(c.demand, i)]: 1.0 for i in range(len(c.options))})
        upper.append(1.0)
    money: dict[int, dict[int, float]] = {}  # per limit: column -> amount
    people: dict[tuple[int, int], dict[int, float]] = {}  # per capacity and quarter: column -> FTE
    for n, (c, i) in enumerate(columns):
        for limit, amount in book.amounts(c.options[i].money, c.scopes):
            money.setdefault(id(limit), {})[n] = amount
        for cap, q, amount in book.fte(c.options[i].fte):
            people.setdefault((id(cap), q), {})[n] = amount
    for limit in book.limits:
        if id(limit) in money:
            rows.append(money[id(limit)])
            upper.append(limit.limit + 1.0 - limit.used)
    for cap in book.capacities:
        for q in cap.quarters():
            if (id(cap), q) in people:
                rows.append(people[(id(cap), q)])
                upper.append(cap.limit + 0.01 - cap.committed.get(q, 0.0) - cap.planned.get(q, 0.0))
    by_demand = {c.demand: c for c in candidates}
    for c in candidates:  # a successor starts only after its predecessor (which must be in the plan) has ended
        for pred in c.predecessors:
            p = by_demand[pred]
            for i, fp in enumerate(c.options):
                row = {index[(c.demand, i)]: 1.0}
                for j, pfp in enumerate(p.options):
                    if pfp.end < fp.start:
                        row[index[(pred, j)]] = -1.0
                rows.append(row)
                upper.append(0.0)

    matrix = lil_matrix((len(rows), len(columns)))
    for r, row in enumerate(rows):
        for n, value in row.items():
            matrix[r, n] = value
    result = milp(cost, constraints=LinearConstraint(matrix.tocsr(), -np.inf, np.array(upper)),
                  integrality=np.ones(len(columns)), bounds=Bounds(0, 1),
                  options={"time_limit": TIME_LIMIT_S, "presolve": True})
    if result.x is None:
        return _rank_order(candidates, book), False
    chosen: dict[str, int | None] = {c.demand: None for c in candidates}
    for n, (c, i) in enumerate(columns):
        if result.x[n] > 0.5:
            chosen[c.demand] = i
    return chosen, True


def _rank_order(candidates: list[Candidate], book: Any) -> dict[str, int | None]:
    """Fallback: the first start that fits, in rank order (as the rank-order filling)."""
    chosen: dict[str, int | None] = {}
    ends: dict[str, int] = {}
    for c in candidates:
        chosen[c.demand] = None
        if any(p not in ends for p in c.predecessors):
            continue
        for i, fp in enumerate(c.options):
            if all(ends[p] < fp.start for p in c.predecessors) and not book.violations(fp, c.scopes):
                book.book(fp, c.scopes)
                chosen[c.demand], ends[c.demand] = i, fp.end
                break
    for c in candidates:  # the caller books the choice itself
        if chosen[c.demand] is not None:
            book.unbook(c.options[chosen[c.demand]], c.scopes)
    return chosen
