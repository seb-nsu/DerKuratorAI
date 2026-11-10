"""Planning scenarios: which demands fit into the budget envelopes and capacity limits of the planning years.

Filling in rank order (method "rank", the default): first the money and capacity bound by running projects, by demands of an
approved plan and by demands the PMO forces in, then the other demands in rank order (a predecessor always before its
successor). A demand is In when every limit holds over all planning years it touches; otherwise it is deferred by whole
years as long as its latest start allows, else it is Out – with the limit it would break (area, year, amount). The PMO can force a demand in (with a
reason), exclude it, or move it by whole years. Soft limits count while filling like hard ones – the engine never
plans a free demand over a soft limit on its own; a soft limit is only exceeded by money that is already bound or by
a demand the PMO forces in, and then needs a board resolution (CONDITIONAL GO).

Value-optimal filling (method "value", see optimizer.py): the same bound money and capacity and the same limits, but
the free demands are chosen together – the combination with the most mandatory demands and the highest score sum.
compare() shows what it does differently from the rank order and why.

Verdict (deterministic, never an LLM):
  NO-GO               a hard budget limit or a capacity limit of the binding (first) year is exceeded, a mandatory
                      demand is Out, or a forced demand starts before its predecessor ends
  ANALYSIS INCOMPLETE a forced demand has no annual figures or no assessment
  CONDITIONAL GO      a soft limit is exceeded (board resolution), a preview year is planned to the alert share or over
                      its limit, a forced demand is ranked below a demand that is Out, a run cost after go-live
                      raises the OpEx of its area by more than the alert share, a large demand rests on a rough
                      estimate or a planned demand's assessment is older than the maximum age
  GO                  everything fits
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import networkx as nx
import pandas as pd

from app.core import clock
from app.core.business_rules import BusinessRules, current_rules
from app.crew.verdict import CONDITIONAL_GO, GO, INCOMPLETE, NO_GO, PMO, SPONSOR, Verdict
from app.data_adapters.dataset import DEMAND_KEY, PK, PortfolioDataset
from app.planning import optimizer
from app.planning.committed import committed_capacity, committed_costs
from app.planning.model import horizon, resource_plans, year_plans
from app.planning.scoring import COMMITTED_STATUSES, ELIGIBLE_STATUSES, MANDATORY, latest_scores, ranking
from app.planning.steward import blocked_demands, check_demands, split_ids

IN, OUT, DEFERRED, FORCED = "In", "Out", "Deferred", "Forced-in"
RANK, VALUE = "rank", "value"
METHODS = {RANK: "Rank order", VALUE: "Value-optimal"}
KINDS = (("capex_eur", "CapEx"), ("opex_eur", "OpEx"))
TOLERANCE = 1.0  # EUR / 0.01 FTE: rounding, never a breach
LARGE = ("L", "XL")


@dataclass(frozen=True)
class ScenarioInput:
    name: str = "Base plan"
    year_from: int | None = None  # default: the year after the reference date
    year_to: int | None = None  # default: year_from + 2
    budget_factor_pct: float = 0.0  # -10 = every envelope 10 % smaller
    scope: str | None = None  # "business_unit:Field Services" / "portfolio:Data & Insights" / None = all
    forced_in: dict[str, str] = field(default_factory=dict)  # demand -> reason
    excluded: dict[str, str] = field(default_factory=dict)  # demand -> reason
    moves: dict[str, int] = field(default_factory=dict)  # demand -> whole years (+1 later, -1 earlier)
    method: str = RANK  # rank = filling in rank order, value = value-optimal choice

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "year_from": self.year_from, "year_to": self.year_to,
                "budget_factor_pct": self.budget_factor_pct, "scope": self.scope, "forced_in": dict(self.forced_in),
                "excluded": dict(self.excluded), "moves": dict(self.moves), "method": self.method}


@dataclass
class ScenarioResult:
    input: ScenarioInput
    as_of: date
    years: tuple[int, ...]
    items: pd.DataFrame
    budget: pd.DataFrame
    capacity: pd.DataFrame
    facts: list[dict[str, Any]]
    verdict: Verdict
    estimated_commitments: bool = False
    solved: bool = True  # False: the optimizer found no solution and the rank order was used

    @property
    def binding_year(self) -> int:
        return self.years[0]

    def decided(self, decision: str) -> list[str]:
        return list(self.items.loc[self.items["decision"] == decision, DEMAND_KEY]) if not self.items.empty else []


def eur(value: float) -> str:
    return f"€{value:,.0f}"


def scope_label(scope_type: str, scope: str) -> str:
    return f"{scope_type.replace('_', ' ')} {scope}"


def _quarter_index(year: int, quarter: int) -> int:
    return year * 4 + quarter - 1


def _quarter_text(index: int) -> str:
    return f"{index // 4}-Q{index % 4 + 1}"


# --------------------------------------------------------------------------- limits
@dataclass
class _Limit:
    scope_type: str
    scope: str
    year: int
    quarter: int | None
    kind: str  # capex_eur | opex_eur
    cap: float  # after the budget factor
    limit: float  # cap minus the reserve
    reserve_pct: float
    hard: bool
    committed: float = 0.0
    planned: float = 0.0

    @property
    def used(self) -> float:
        return self.committed + self.planned

    def text(self) -> str:
        period = f"{self.year}-Q{self.quarter}" if self.quarter else str(self.year)
        kind = dict(KINDS)[self.kind]
        reserve = f" after {self.reserve_pct:g} % reserve" if self.reserve_pct else ""
        soft = "" if self.hard else ", soft limit"
        return f"{kind} {period} of {scope_label(self.scope_type, self.scope)} (limit {eur(self.limit)}{reserve}{soft})"


@dataclass
class _Capacity:
    team: str
    skill: str
    year: int
    quarter: int | None
    available: float
    max_pct: float
    committed: dict[int, float] = field(default_factory=dict)  # quarter index -> FTE
    planned: dict[int, float] = field(default_factory=dict)

    @property
    def limit(self) -> float:
        return self.available * self.max_pct / 100

    def quarters(self) -> list[int]:
        qs = [self.quarter] if self.quarter else [1, 2, 3, 4]
        return [_quarter_index(self.year, q) for q in qs]

    def label(self) -> str:
        return self.team + (f" ({self.skill})" if self.skill else "")


def _envelopes(ds: PortfolioDataset, years: tuple[int, ...], factor: float, scope: tuple[str, str] | None) -> list[_Limit]:
    env = ds.budget_envelopes
    if env.empty or not {"year", "scope_type", "scope"} <= set(env.columns):
        return []
    limits = []
    for r in env.to_dict(orient="records"):
        year = pd.to_numeric(r.get("year"), errors="coerce")
        if pd.isna(year) or int(year) not in years:
            continue
        stype, sname = str(r.get("scope_type") or "").strip(), str(r.get("scope") or "").strip()
        if scope and (stype, sname) != scope:
            continue
        quarter = pd.to_numeric(r.get("quarter"), errors="coerce")
        reserve = float(pd.to_numeric(r.get("reserve_pct"), errors="coerce") or 0) if pd.notna(
            pd.to_numeric(r.get("reserve_pct"), errors="coerce")) else 0.0
        hard = str(r.get("cap_type") or "hard").strip().lower() != "soft"
        for kind, _ in KINDS:
            cap = pd.to_numeric(r.get(kind.replace("_eur", "_cap_eur")), errors="coerce")
            if pd.isna(cap):
                continue
            cap = float(cap) * (1 + factor / 100)
            limits.append(_Limit(stype, sname, int(year), int(quarter) if pd.notna(quarter) else None, kind, cap,
                                 cap * (1 - reserve / 100), reserve, hard))
    return limits


def _capacities(ds: PortfolioDataset, years: tuple[int, ...]) -> list[_Capacity]:
    cl = ds.capacity_limits
    if cl.empty or not {"year", "assignment_group", "available_fte"} <= set(cl.columns):
        return []
    out = []
    for r in cl.to_dict(orient="records"):
        year = pd.to_numeric(r.get("year"), errors="coerce")
        fte = pd.to_numeric(r.get("available_fte"), errors="coerce")
        if pd.isna(year) or int(year) not in years or pd.isna(fte):
            continue
        quarter = pd.to_numeric(r.get("quarter"), errors="coerce")
        pct = pd.to_numeric(r.get("max_utilization_pct"), errors="coerce")
        skill = r.get("skill")
        out.append(_Capacity(str(r["assignment_group"]).strip(), "" if skill is None or pd.isna(skill) else str(skill).strip(),
                             int(year), int(quarter) if pd.notna(quarter) else None, float(fte),
                             float(pct) if pd.notna(pct) else 100.0))
    return out


# --------------------------------------------------------------------------- footprints
@dataclass
class _Footprint:
    money: list[dict[str, Any]]  # year, quarter, capex_eur, opex_eur (incl. run cost after go-live)
    fte: list[dict[str, Any]]  # quarter index, team, skill, fte
    start: int  # quarter index of the first quarter with money
    end: int  # quarter index of the last project quarter (go-live)
    run_cost: float


def _footprint(plans: pd.DataFrame, res: pd.DataFrame, shift: int, run_cost: float, last_year: int) -> _Footprint | None:
    if plans.empty:
        return None
    money = [{"year": int(r["year"]) + shift, "quarter": int(r["quarter"]), "capex_eur": float(r["capex_eur"]),
              "opex_eur": float(r["opex_eur"])} for r in plans.to_dict(orient="records")]
    quarters = [_quarter_index(m["year"], m["quarter"]) for m in money if m["capex_eur"] + m["opex_eur"] > 0] or \
        [_quarter_index(m["year"], m["quarter"]) for m in money]
    start, end = min(quarters), max(quarters)
    if run_cost > 0:
        for q in range(end + 1, _quarter_index(last_year, 4) + 1):
            money.append({"year": q // 4, "quarter": q % 4 + 1, "capex_eur": 0.0, "opex_eur": run_cost / 4,
                          "run_cost": True})
    fte = [{"q": _quarter_index(int(r["year"]) + shift, int(r["quarter"])), "team": r["assignment_group"],
            "skill": "" if r["skill"] is None or pd.isna(r["skill"]) else str(r["skill"]).strip(), "fte": float(r["fte"])}
           for r in res.to_dict(orient="records")]
    return _Footprint(money, fte, start, end, run_cost)


class _Book:
    """The limits with their committed and planned usage; checks and books footprints."""

    def __init__(self, limits: list[_Limit], capacities: list[_Capacity]):
        self.limits, self.capacities = limits, capacities

    @staticmethod
    def _applies(limit: _Limit, row: dict[str, Any], scopes: dict[str, str]) -> bool:
        return (scopes.get(limit.scope_type) == limit.scope and row["year"] == limit.year
                and (limit.quarter is None or row["quarter"] == limit.quarter))

    def amounts(self, money: list[dict[str, Any]], scopes: dict[str, str]) -> list[tuple[_Limit, float]]:
        out = []
        for limit in self.limits:
            amount = sum(r[limit.kind] for r in money if self._applies(limit, r, scopes))
            if amount:
                out.append((limit, amount))
        return out

    def fte(self, rows: list[dict[str, Any]]) -> list[tuple[_Capacity, int, float]]:
        out = []
        for cap in self.capacities:
            for q in cap.quarters():
                amount = sum(r["fte"] for r in rows if r["q"] == q and r["team"] == cap.team
                             and (not cap.skill or r["skill"] == cap.skill))
                if amount:
                    out.append((cap, q, amount))
        return out

    def commit(self, money: list[dict[str, Any]], scopes: dict[str, str], fte: list[dict[str, Any]]) -> None:
        for limit, amount in self.amounts(money, scopes):
            limit.committed += amount
        for cap, q, amount in self.fte(fte):
            cap.committed[q] = cap.committed.get(q, 0.0) + amount

    def violations(self, fp: _Footprint, scopes: dict[str, str], hard_only: bool = False) -> list[dict[str, Any]]:
        """Limits the footprint would break (soft limits too, unless hard_only), ordered by year."""
        found = []
        for limit, amount in self.amounts(fp.money, scopes):
            if (limit.hard or not hard_only) and limit.used + amount > limit.limit + TOLERANCE:
                over = limit.used + amount - limit.limit
                found.append({"type": "budget", "limit": limit, "year": limit.year, "over": over,
                              "text": f"{limit.text()} would be exceeded by {eur(over)}"})
        for cap, q, amount in self.fte(fp.fte):
            need = cap.committed.get(q, 0.0) + cap.planned.get(q, 0.0) + amount
            if need > cap.limit + 0.01:
                found.append({"type": "capacity", "capacity": cap, "year": q // 4, "q": q, "over": need - cap.limit,
                              "text": f"{cap.label()} {_quarter_text(q)} would need {need:.1f} FTE of "
                                      f"{cap.limit:.1f} available ({cap.max_pct:g} % of {cap.available:g})"})
        return sorted(found, key=lambda v: (v["year"], v.get("q", 0), v["type"]))

    def book(self, fp: _Footprint, scopes: dict[str, str], sign: float = 1.0) -> None:
        for limit, amount in self.amounts(fp.money, scopes):
            limit.planned += sign * amount
        for cap, q, amount in self.fte(fp.fte):
            cap.planned[q] = cap.planned.get(q, 0.0) + sign * amount

    def unbook(self, fp: _Footprint, scopes: dict[str, str]) -> None:
        self.book(fp, scopes, -1.0)


# --------------------------------------------------------------------------- the scenario
def _scope(spec: str | None) -> tuple[str, str] | None:
    if not spec:
        return None
    kind, _, name = spec.partition(":")
    return kind.strip(), name.strip()


def _demand_order(candidates: pd.DataFrame) -> tuple[list[str], set[str]]:
    """Rank order with every predecessor before its successor; demands in a dependency cycle are returned apart."""
    rank = dict(zip(candidates[DEMAND_KEY], candidates["rank"], strict=True))
    graph = nx.DiGraph()
    graph.add_nodes_from(rank)
    for did, deps in zip(candidates[DEMAND_KEY], candidates.get("depends_on", [None] * len(candidates)), strict=True):
        for pred in split_ids(deps):
            if pred in rank and pred != did:
                graph.add_edge(pred, did)
    cyclic: set[str] = set()
    for component in nx.strongly_connected_components(graph):
        if len(component) > 1:
            cyclic |= component
    graph.remove_nodes_from(cyclic)
    return list(nx.lexicographical_topological_sort(graph, key=lambda d: (rank[d], d))), cyclic


def run_scenario(ds: PortfolioDataset, spec: ScenarioInput | None = None, as_of: date | None = None,
                 rules: BusinessRules | None = None) -> ScenarioResult:
    spec = spec or ScenarioInput()
    as_of = as_of or clock.today()
    rules = rules or current_rules()
    first, last = horizon(as_of)
    year_from = spec.year_from or first
    year_to = spec.year_to or max(year_from, last if spec.year_from is None else year_from + 2)
    years = tuple(range(year_from, year_to + 1))
    scope = _scope(spec.scope)
    book = _Book(_envelopes(ds, years, spec.budget_factor_pct, scope), _capacities(ds, years))

    committed = committed_costs(ds, as_of)
    for row in committed.to_dict(orient="records"):
        book.commit([row], {"business_unit": row["business_unit"], "portfolio": row["portfolio"]}, [])
    for row in committed_capacity(ds, as_of).to_dict(orient="records"):
        book.commit([], {}, [{"q": _quarter_index(int(row["year"]), int(row["quarter"])), "team": row["assignment_group"],
                              "skill": "", "fte": row["fte"]}])

    ranked = ranking(ds)
    if scope and not ranked.empty:
        ranked = ranked[ranked[scope[0]].astype(str).str.strip() == scope[1]]
    statuses = ELIGIBLE_STATUSES + COMMITTED_STATUSES
    candidates = ranked[ranked["demand_status"].isin(statuses)] if not ranked.empty else ranked
    if not candidates.empty:  # a duplicate number is a Data Steward error (blocked below) – the first row stands for it
        candidates = candidates.drop_duplicates(DEMAND_KEY, keep="first")
    plans, res = year_plans(ds), resource_plans(ds)
    blocked = blocked_demands(check_demands(ds, as_of))
    scores = latest_scores(ds)
    project_end = {}
    if not ds.projects.empty and "planned_end_date" in ds.projects.columns:
        for pid, end in zip(ds.projects[PK].astype(str), pd.to_datetime(ds.projects["planned_end_date"], errors="coerce"),
                            strict=True):
            if pd.notna(end):
                project_end[pid] = _quarter_index(end.year, end.quarter)

    order, cyclic = _demand_order(candidates) if not candidates.empty else ([], set())
    info = candidates.set_index(DEMAND_KEY) if not candidates.empty else pd.DataFrame()
    placed: dict[str, _Footprint] = {}
    items: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []

    def record(did: str, decision: str, reason: str, fp: _Footprint | None, shift: int = 0, note: str = "") -> None:
        row = info.loc[did]
        item = {"rank": int(row["rank"]), DEMAND_KEY: did, "demand_name": row.get("demand_name"),
                "business_unit": row.get("business_unit"), "portfolio": row.get("portfolio"),
                "demand_type": row.get("demand_type"), "demand_status": row.get("demand_status"),
                "score": row.get("score"), "decision": decision, "shift_years": shift, "reason": reason,
                "note": note, "start": _quarter_text(fp.start) if fp and decision != OUT else None,
                "end": _quarter_text(fp.end) if fp and decision != OUT else None,
                "fte_peak": 0.0, "benefit_eur": float(pd.to_numeric(row.get("planned_benefit_eur"), errors="coerce") or 0),
                "npv_eur": float(pd.to_numeric(row.get("net_present_value_eur"), errors="coerce") or 0)}
        for year in years:
            for kind, label in KINDS:
                item[f"{label} {year}"] = sum(m[kind] for m in fp.money if m["year"] == year) \
                    if fp and decision != OUT else 0.0
        if fp and decision != OUT and fp.fte:
            per_q: dict[int, float] = {}
            for r in fp.fte:
                per_q[r["q"]] = per_q.get(r["q"], 0.0) + r["fte"]
            item["fte_peak"] = max(per_q.values())
        items.append(item)

    def scopes_of(did: str) -> dict[str, str]:
        row = info.loc[did]
        return {"business_unit": str(row.get("business_unit") or "").strip(),
                "portfolio": str(row.get("portfolio") or "").strip()}

    def fits_window(fp: _Footprint, did: str) -> bool:
        latest = pd.to_datetime(info.loc[did].get("latest_start_date"), errors="coerce")
        start_year = fp.start // 4
        if start_year > year_to:
            return False
        return pd.isna(latest) or fp.start <= _quarter_index(latest.year, latest.quarter)

    def earliest_ok(fp: _Footprint, did: str) -> bool:
        earliest = pd.to_datetime(info.loc[did].get("earliest_start_date"), errors="coerce")
        return pd.isna(earliest) or fp.start >= _quarter_index(earliest.year, earliest.quarter)

    def predecessor_end(did: str) -> tuple[int | None, str | None]:
        """Latest end quarter of the predecessors; (None, name) when a predecessor demand is Out."""
        ends = []
        for pred in split_ids(info.loc[did].get("depends_on")):
            if pred in placed:
                ends.append(placed[pred].end)
            elif pred in info.index:
                return None, pred
            elif pred in project_end:
                ends.append(project_end[pred])
        return (max(ends) if ends else -1), None

    out_reasons: dict[str, str] = {}
    optimise = spec.method == VALUE
    free: dict[str, optimizer.Candidate] = {}  # value-optimal filling: the free demands with their possible starts
    free_notes: dict[str, tuple[str, int, dict[str, Any] | None]] = {}

    def out_no_fit(did: str, reason: str) -> None:
        out_reasons[did] = f"Out: {reason}"
        record(did, OUT, out_reasons[did], None)
        if info.loc[did].get("demand_type") == MANDATORY:
            facts.append({"type": "mandatory_out", "demand": did, "text": f"Mandatory demand {did} "
                          f"({info.loc[did].get('mandatory_reason')}) does not fit: {reason}"})

    sequence = [d for d in order if d in info.index]
    booked_first = [d for d in sequence if d in spec.forced_in or info.loc[d].get("demand_status") in COMMITTED_STATUSES]
    for did in booked_first + [d for d in sequence if d not in booked_first] + sorted(cyclic):
        row = info.loc[did]
        own_plans, own_res = plans[plans[DEMAND_KEY] == did], res[res[DEMAND_KEY] == did]
        run_cost = float(pd.to_numeric(row.get("run_cost_after_go_live_eur"), errors="coerce") or 0)
        mandatory = row.get("demand_type") == MANDATORY
        approved = row.get("demand_status") in COMMITTED_STATUSES
        forced = did in spec.forced_in
        move = int(spec.moves.get(did, 0))
        base = _footprint(own_plans, own_res, move, run_cost, year_to)
        scopes = scopes_of(did)
        if base is not None and base.start // 4 > year_to:
            continue  # starts after the planning years: not part of this scenario
        if did in cyclic:
            out_reasons[did] = "Circular dependency between demands – fix depends_on"
            record(did, OUT, out_reasons[did], None)
            facts.append({"type": "incomplete", "demand": did, "text": f"{did}: circular dependency"})
            continue
        if did in spec.excluded and not approved:
            out_reasons[did] = f"Excluded by the PMO: {spec.excluded[did] or 'no reason given'}"
            record(did, OUT, out_reasons[did], None)
            if mandatory:
                facts.append({"type": "mandatory_out", "demand": did,
                              "text": f"Mandatory demand {did} ({row.get('mandatory_reason')}) is excluded"})
            continue
        if forced or approved:
            if base is None or (not mandatory and not approved and not bool(row.get("assessed"))):
                missing = "annual figures" if base is None else "an assessment"
                facts.append({"type": "incomplete", "demand": did, "text": f"Forced demand {did} has no {missing}"})
                record(did, FORCED if forced else IN, f"Without {missing}", base)
                continue
            for v in book.violations(base, scopes):
                facts.append({"type": "breach", "demand": did, **v})
            book.book(base, scopes)
            placed[did] = base
            reason = f"Forced in: {spec.forced_in[did] or 'no reason given'}" if forced else "Approved in an earlier plan"
            record(did, FORCED if forced else IN, reason, base, move)
            continue
        if did in blocked:
            out_reasons[did] = f"Data quality: {blocked[did]}"
            record(did, OUT, out_reasons[did], None)
            if mandatory:
                facts.append({"type": "incomplete", "demand": did,
                              "text": f"Mandatory demand {did} is blocked by its data ({blocked[did]})"})
            continue
        if not mandatory and not bool(row.get("assessed")):
            out_reasons[did] = "Not assessed yet"
            record(did, OUT, out_reasons[did], None)
            continue
        if base is None:
            out_reasons[did] = "No annual figures"
            record(did, OUT, out_reasons[did], None)
            continue
        note = ""
        if move < 0 and not earliest_ok(base, did):
            note = "Cannot start before its earliest start – kept in its planned year"
            move, base = 0, _footprint(own_plans, own_res, 0, run_cost, year_to)
        if optimise:
            fixed_end, free_preds, missing_pred = -1, [], None
            for pred in split_ids(row.get("depends_on")):
                if pred in placed:
                    fixed_end = max(fixed_end, placed[pred].end)
                elif pred in free:
                    free_preds.append(pred)
                elif pred in info.index:
                    missing_pred = pred
                    break
                elif pred in project_end:
                    fixed_end = max(fixed_end, project_end[pred])
            if not missing_pred:
                options, shifts, first_violation, shift = [], [], None, move
                while (fp := _footprint(own_plans, own_res, shift, run_cost, year_to)) is not None and fits_window(fp, did):
                    if fp.start <= fixed_end:
                        first_violation = first_violation or {"text": f"its predecessor ends {_quarter_text(fixed_end)}"}
                    elif violations := book.violations(fp, scopes):
                        first_violation = first_violation or violations[0]
                    else:
                        options.append(fp)
                        shifts.append(shift)
                    shift += 1
                if options:
                    score = pd.to_numeric(row.get("score"), errors="coerce")
                    free[did] = optimizer.Candidate(did, int(row["rank"]), 0.0 if pd.isna(score) else float(score),
                                                    mandatory, options, shifts, scopes, free_preds)
                    free_notes[did] = (note, move, first_violation)
                else:
                    out_no_fit(did, first_violation["text"] if first_violation else "outside its latest start")
                continue
        pred_end, missing_pred = predecessor_end(did)
        if missing_pred:
            out_reasons[did] = f"Predecessor {missing_pred} is out"
            record(did, OUT, out_reasons[did], None)
            if mandatory:
                facts.append({"type": "mandatory_out", "demand": did,
                              "text": f"Mandatory demand {did} cannot start: predecessor {missing_pred} is out"})
            continue
        shift, booked_firstviolation, chosen = move, None, None
        while True:
            fp = _footprint(own_plans, own_res, shift, run_cost, year_to)
            if fp is None or not fits_window(fp, did):
                break
            if pred_end is not None and fp.start <= pred_end:
                booked_firstviolation = booked_firstviolation or {"text": f"its predecessor ends {_quarter_text(pred_end)}"}
                shift += 1
                continue
            violations = book.violations(fp, scopes)
            if not violations:
                chosen = fp
                break
            booked_firstviolation = booked_firstviolation or violations[0]
            shift += 1
        if chosen is None:
            out_no_fit(did, booked_firstviolation["text"] if booked_firstviolation else "outside its latest start")
            continue
        book.book(chosen, scopes)
        placed[did] = chosen
        if shift > move:
            record(did, DEFERRED, f"Deferred to {_quarter_text(chosen.start)}: {booked_firstviolation['text']}", chosen, shift,
                   note)
        elif move > 0:
            record(did, DEFERRED, f"Moved by the PMO to {_quarter_text(chosen.start)}", chosen, shift, note)
        else:
            record(did, IN, "Pulled forward by the PMO" if move < 0 else "Fits all limits", chosen, shift, note)

    solved = True
    if free:
        solved = _place_chosen(free, free_notes, book, placed, record, out_no_fit, out_reasons)

    for did in booked_first:  # forced and approved demands were booked first: check their predecessors now
        if did not in placed:
            continue
        pred_end, missing_pred = predecessor_end(did)
        if missing_pred or (pred_end is not None and placed[did].start <= pred_end):
            what = f"predecessor {missing_pred} is out" if missing_pred else \
                f"starts {_quarter_text(placed[did].start)} before its predecessor ends {_quarter_text(pred_end)}"
            facts.append({"type": "dependency_violated", "demand": did, "text": f"{did}: {what}"})

    items_frame = pd.DataFrame(items)
    if not items_frame.empty:
        items_frame = items_frame.sort_values("rank").reset_index(drop=True)
    facts += _limit_facts(book, years, rules)
    facts += _quality_facts(items_frame, info, scores, placed, book, spec, as_of, rules, out_reasons, plans)
    verdict = _verdict(facts, as_of, rules, years)
    return ScenarioResult(spec, as_of, years, items_frame, _budget_frame(book), _capacity_frame(book), facts, verdict,
                          bool(committed["estimated"].any()) if not committed.empty else False, solved)


def _place_chosen(free: dict[str, optimizer.Candidate], notes: dict[str, tuple[str, int, dict[str, Any] | None]],
                  book: _Book, placed: dict[str, _Footprint], record: Any, out_no_fit: Any,
                  out_reasons: dict[str, str]) -> bool:
    """Books the optimizer's choice and records every free demand with the reason: a deferral or an Out names the
    limit its earliest start would break next to the chosen demands."""
    chosen, solved = optimizer.choose(list(free.values()), book)
    for did, c in free.items():
        if chosen[did] is not None:
            book.book(c.options[chosen[did]], c.scopes)
            placed[did] = c.options[chosen[did]]

    def blocker(c: optimizer.Candidate) -> str:
        missing = [p for p in c.predecessors if p not in placed]
        if missing:
            return f"predecessor {missing[0]} is out"
        violations = book.violations(c.options[0], c.scopes)
        if violations:
            return violations[0]["text"]
        ends = [placed[p].end for p in c.predecessors]
        if ends and max(ends) >= c.options[0].start:
            return f"its predecessor ends {_quarter_text(max(ends))}"
        return "not chosen by the optimizer"

    for did, c in free.items():
        note, move, first_violation = notes[did]
        i = chosen[did]
        if i is None:
            reason = blocker(c)
            if first_violation and not reason.startswith("predecessor"):
                reason = first_violation["text"]  # as in rank order: the limit its earliest start breaks
            if reason.startswith("predecessor"):
                out_reasons[did] = reason[0].upper() + reason[1:]
                record(did, OUT, out_reasons[did], None)
            else:
                out_no_fit(did, reason)
            continue
        fp, shift = c.options[i], c.shifts[i]
        if i > 0:
            book.unbook(fp, c.scopes)
            placed.pop(did)
            why = blocker(c)
            book.book(fp, c.scopes)
            placed[did] = fp
            record(did, DEFERRED, f"Deferred to {_quarter_text(fp.start)}: {why}", fp, shift, note)
        elif shift > move:
            record(did, DEFERRED, f"Deferred to {_quarter_text(fp.start)}: {first_violation['text']}", fp, shift, note)
        elif move > 0:
            record(did, DEFERRED, f"Moved by the PMO to {_quarter_text(fp.start)}", fp, shift, note)
        else:
            record(did, IN, "Pulled forward by the PMO" if move < 0 else "Fits all limits", fp, shift, note)
    return solved


# --------------------------------------------------------------------------- facts after the filling
def _limit_facts(book: _Book, years: tuple[int, ...], rules: BusinessRules) -> list[dict[str, Any]]:
    facts = []
    binding = years[0]
    for limit in book.limits:
        over = limit.used - limit.limit
        if over > TOLERANCE:
            base = {"scope": scope_label(limit.scope_type, limit.scope), "year": limit.year, "over": over,
                    "text": f"{limit.text()} is exceeded by {eur(over)}"}
            if limit.hard and limit.year == binding:
                facts.append({"type": "budget_limit", **base})
            elif not limit.hard:
                facts.append({"type": "soft_limit", **base})
            else:
                facts.append({"type": "preview_over", **base})
        elif limit.year != binding and limit.limit > 0 and limit.used / limit.limit * 100 >= rules.planning_preview_alert_pct:
            facts.append({"type": "preview_alert", "scope": scope_label(limit.scope_type, limit.scope),
                          "year": limit.year, "pct": limit.used / limit.limit * 100,
                          "text": f"{dict(KINDS)[limit.kind]} {limit.year} of {scope_label(limit.scope_type, limit.scope)} "
                                  f"is already {limit.used / limit.limit * 100:.0f} % planned"})
    for cap in book.capacities:
        for q in cap.quarters():
            need = cap.committed.get(q, 0.0) + cap.planned.get(q, 0.0)
            if need > cap.limit + 0.01:
                text = (f"{cap.label()} {_quarter_text(q)} needs {need:.1f} FTE of {cap.limit:.1f} available "
                        f"({cap.max_pct:g} % of {cap.available:g})")
                facts.append({"type": "capacity_limit" if q // 4 == binding else "preview_over", "year": q // 4,
                              "scope": cap.label(), "over": need - cap.limit, "text": text})
    return facts


def _quality_facts(items: pd.DataFrame, info: pd.DataFrame, scores: pd.DataFrame, placed: dict[str, _Footprint],
                   book: _Book, spec: ScenarioInput, as_of: date, rules: BusinessRules,
                   out_reasons: dict[str, str], plans: pd.DataFrame) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    if items.empty:
        return facts
    out_scores = {d: s for d, s in zip(items[DEMAND_KEY], items["score"], strict=True)
                  if d in out_reasons and pd.notna(s) and not out_reasons[d].startswith(("Excluded", "Data quality"))}
    for did in spec.forced_in:
        if did not in placed or did not in info.index:
            continue
        score = info.loc[did].get("score")
        better = sorted(d for d, s in out_scores.items() if pd.isna(score) or s > score)
        if better:
            facts.append({"type": "forced_over_better", "demand": did,
                          "text": f"Justify forcing in {did} while higher-scored {', '.join(better[:3])} "
                                  f"{'is' if len(better) == 1 else 'are'} out"})
    max_age = timedelta(days=rules.planning_score_max_age_days)
    assessed_on = scores.assign(on=pd.to_datetime(scores["assessed_on"], errors="coerce")).groupby(DEMAND_KEY)["on"].min() \
        if not scores.empty else pd.Series(dtype="datetime64[ns]")
    for did in placed:
        on = assessed_on.get(did)
        if on is not None and pd.notna(on) and pd.Timestamp(as_of) - on > max_age:
            facts.append({"type": "stale_assessment", "demand": did,
                          "text": f"Re-assess {did}: its assessment dates from {on.date().isoformat()}"})
    return facts + _run_cost_facts(info, placed, book, rules) + _rough_estimates(info, plans, placed)


def _run_cost_facts(info: pd.DataFrame, placed: dict[str, _Footprint], book: _Book,
                    rules: BusinessRules) -> list[dict[str, Any]]:
    """One fact per area and year whose OpEx limit the new run costs after go-live take more than the alert share of
    (the year after go-live of each demand)."""
    per_limit: dict[int, tuple[Any, list[str], float]] = {}
    for did, fp in placed.items():
        if fp.run_cost <= 0:
            continue
        for lim in book.limits:
            if lim.kind == "opex_eur" and lim.quarter is None and lim.year == fp.end // 4 + 1 and lim.limit > 0 \
                    and lim.scope == str(info.loc[did].get(lim.scope_type) or "").strip():
                _, names, total = per_limit.get(id(lim), (lim, [], 0.0))
                per_limit[id(lim)] = (lim, [*names, did], total + fp.run_cost)
    facts = []
    for lim, names, total in per_limit.values():
        share = total / lim.limit * 100
        if share > rules.planning_run_cost_alert_pct:
            facts.append({"type": "run_cost", "scope": scope_label(lim.scope_type, lim.scope), "year": lim.year,
                          "text": f"Run cost after go-live of {', '.join(sorted(names))} ({eur(total)} p.a.) takes "
                                  f"{share:.0f} % of the OpEx limit {lim.year} of "
                                  f"{scope_label(lim.scope_type, lim.scope)} – include it in the operating budget"})
    return facts


def _rough_estimates(info: pd.DataFrame, plans: pd.DataFrame, placed: dict[str, _Footprint]) -> list[dict[str, Any]]:
    """A large demand (T-shirt L/XL) in the plan whose annual figures are only a rough estimate."""
    if plans.empty or "confidence" not in plans.columns:
        return []
    rough = set(plans.loc[plans["confidence"].astype(str).str.strip() == "Rough", DEMAND_KEY].astype(str))
    return [{"type": "rough_estimate", "demand": did,
             "text": f"Refine the estimate of {did} before approval: size {info.loc[did].get('tshirt_size')} "
                     f"rests on a rough estimate"}
            for did in sorted(placed) if did in rough and info.loc[did].get("tshirt_size") in LARGE]


# --------------------------------------------------------------------------- verdict
NO_GO_FACTS = ("budget_limit", "capacity_limit", "mandatory_out", "dependency_violated")
CONDITION_FACTS = {"soft_limit": SPONSOR, "preview_over": PMO, "preview_alert": PMO, "forced_over_better": PMO,
                   "run_cost": SPONSOR, "stale_assessment": PMO, "rough_estimate": PMO}
RULE_TEXT = {"budget_limit": "budget limit exceeded", "capacity_limit": "capacity limit exceeded",
             "mandatory_out": "mandatory demand missing", "dependency_violated": "dependency violated"}


def _condition(fact: dict[str, Any]) -> str:
    kind = fact["type"]
    if kind == "soft_limit":
        return f"Board resolution needed: {fact['text']} (soft limit)."
    if kind == "preview_over":
        return f"Plan the preview year down: {fact['text']}."
    if kind == "preview_alert":
        return f"Note for the next planning round: {fact['text']}."
    return f"{fact['text']}."


def _verdict(facts: list[dict[str, Any]], as_of: date, rules: BusinessRules, years: tuple[int, ...]) -> Verdict:
    snapshot = {k: getattr(rules, k) for k in ("planning_preview_alert_pct", "planning_run_cost_alert_pct",
                                                "planning_score_max_age_days", "condition_due_days")}
    snapshot["planning_years"] = f"{years[0]}–{years[-1]}"
    due = (as_of + timedelta(days=rules.condition_due_days)).isoformat()
    hard = [f for f in facts if f["type"] in NO_GO_FACTS]
    if hard:
        rule = RULE_TEXT[hard[0]["type"]]
        reasons = (f"{rule[0].upper()}{rule[1:]}.", *[f"{f['text']}." for f in hard])
        return Verdict(NO_GO, NO_GO, reasons, (), snapshot, as_of.isoformat(), rule)
    incomplete = [f for f in facts if f["type"] == "incomplete"]
    if incomplete:
        return Verdict(INCOMPLETE, INCOMPLETE, ("Data of a planned demand is incomplete.",
                                                *[f"{f['text']}." for f in incomplete]), (), snapshot,
                       as_of.isoformat(), "data incomplete")
    conditions: dict[str, str] = {}
    for f in facts:
        if f["type"] in CONDITION_FACTS:
            conditions.setdefault(_condition(f), CONDITION_FACTS[f["type"]])
    if conditions:
        return Verdict(CONDITIONAL_GO, CONDITIONAL_GO, ("The plan fits its hard limits, with conditions.",),
                       tuple(conditions), snapshot, as_of.isoformat(), owners=tuple(conditions.values()), due=due)
    return Verdict(GO, GO, ("Every planned demand fits the budget envelopes and capacity limits.",), (), snapshot,
                   as_of.isoformat())


# --------------------------------------------------------------------------- tables for the UI and reports
def _budget_frame(book: _Book) -> pd.DataFrame:
    rows = [{"Scope type": lim.scope_type.replace("_", " "), "Scope": lim.scope, "Year": lim.year,
             "Quarter": f"Q{lim.quarter}" if lim.quarter else "year", "Kind": dict(KINDS)[lim.kind],
             "Cap type": "hard" if lim.hard else "soft", "Cap": round(lim.cap, 2), "Limit": round(lim.limit, 2),
             "Committed": round(lim.committed, 2), "Planned": round(lim.planned, 2),
             "Free": round(lim.limit - lim.used, 2),
             "Used %": round(lim.used / lim.limit * 100, 1) if lim.limit > 0 else None}
            for lim in book.limits]
    return pd.DataFrame(rows)


def _capacity_frame(book: _Book) -> pd.DataFrame:
    rows = []
    for cap in book.capacities:
        for q in cap.quarters():
            used = cap.committed.get(q, 0.0) + cap.planned.get(q, 0.0)
            rows.append({"Team": cap.label(), "Quarter": _quarter_text(q), "Limit FTE": round(cap.limit, 2),
                         "Committed FTE": round(cap.committed.get(q, 0.0), 2),
                         "Planned FTE": round(cap.planned.get(q, 0.0), 2),
                         "Used %": round(used / cap.limit * 100, 1) if cap.limit > 0 else None})
    return pd.DataFrame(rows)



# --------------------------------------------------------------------------- rank order vs. value-optimal
def _plan_figures(result: ScenarioResult) -> dict[str, Any]:
    items = result.items if not result.items.empty else pd.DataFrame(columns=[DEMAND_KEY, "decision", "score", "npv_eur"])
    placed = items[items["decision"] != OUT]
    binding = result.years[0]
    return {"Filling": METHODS.get(result.input.method, result.input.method), "Verdict": result.verdict.label,
            "Demands in the plan": len(placed),
            f"Starting {binding}": int((placed.get("start", pd.Series(dtype=str)).astype(str).str[:4] == str(binding)).sum()),
            "Score sum": round(float(pd.to_numeric(placed["score"], errors="coerce").fillna(0).sum()), 1),
            "NPV planned": round(float(placed["npv_eur"].sum()), 2)}


def compare(rank: ScenarioResult, value: ScenarioResult) -> tuple[list[str], pd.DataFrame, pd.DataFrame]:
    """What the value-optimal filling does differently from the rank order, and why: key figures of both, one summary
    sentence per direction and one row per demand whose decision or start differs (with the reason of each side –
    the reason of a deferral or an Out names the limit)."""
    figures = pd.DataFrame([_plan_figures(rank), _plan_figures(value)])
    a = rank.items.set_index(DEMAND_KEY) if not rank.items.empty else pd.DataFrame()
    b = value.items.set_index(DEMAND_KEY) if not value.items.empty else pd.DataFrame()
    rows = []
    for did in [d for d in a.index if d in b.index]:
        ra, rb = a.loc[did], b.loc[did]
        if (ra["decision"], ra["start"]) == (rb["decision"], rb["start"]):
            continue

        def side(r: pd.Series) -> str:
            return r["decision"] if r["decision"] == OUT else f"{r['decision']} {r['start']}"

        rows.append({"Rank": int(ra["rank"]), "Demand": did, "Name": ra.get("demand_name"),
                     "Score": ra.get("score"), "Rank order": side(ra), "Value-optimal": side(rb),
                     "Why (rank order)": ra["reason"], "Why (value-optimal)": rb["reason"]})
    changes = pd.DataFrame(rows)
    if changes.empty:
        return ["The value-optimal filling plans the same demands with the same starts as the rank order."], figures, changes

    def names(frame: pd.DataFrame) -> str:
        return ", ".join(f"{d} (score {s:g})" if pd.notna(s) else f"{d} (mandatory)"
                         for d, s in zip(frame["Demand"], pd.to_numeric(frame["Score"], errors="coerce"), strict=True))

    gained = changes[(changes["Rank order"] == OUT) & (changes["Value-optimal"] != OUT)]
    lost = changes[(changes["Rank order"] != OUT) & (changes["Value-optimal"] == OUT)]
    moved = changes[(changes["Rank order"] != OUT) & (changes["Value-optimal"] != OUT)]
    fa, fb = figures.iloc[0], figures.iloc[1]
    summary = [f"Score sum of the plan {fa['Score sum']:g} → {fb['Score sum']:g} "
               f"({fb['Score sum'] - fa['Score sum']:+g}), demands in the plan {fa['Demands in the plan']} → "
               f"{fb['Demands in the plan']}, verdict {fa['Verdict']} → {fb['Verdict']}."]
    if not gained.empty:
        summary.append(f"Additionally in the plan: {names(gained)} – the rank order left them out because an "
                       f"earlier-ranked demand had already taken the money or capacity.")
    if not lost.empty:
        summary.append(f"Left out instead: {names(lost)} – together the others bring more value within the same limits.")
    if not moved.empty:
        summary.append(f"Other start: {', '.join(moved['Demand'])} – to make room within the limits of the earlier "
                       f"years.")
    return summary, figures, changes
