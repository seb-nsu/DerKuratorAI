"""Forecast ranges instead of single values: reference classes (E3) and the portfolio with probabilities (E4).

Both are plain arithmetic on the loaded portfolio, without an LLM, and reproducible (fixed random seed).

Reference class (E3): for a project or a planned demand, the comparable projects of the portfolio – same execution
type and portfolio, else the same execution type, else all – and how far their cost and end date moved from the
first baseline: cost ratio = forecast at completion (closed projects: actual cost) / budget of baseline 1, schedule
ratio = planned duration / baseline duration. Percentiles of these ratios give a range and a buffer for the budget at
the configured confidence level ("80 % of such projects stayed within +35 %"). Running projects count with their
current forecast; the result says how many of the class are closed.

Portfolio with probabilities (E4): a Monte Carlo run over all open projects. Per run and project the remaining cost
(forecast minus actual) is scaled by a cost ratio drawn from the project's reference class (centred on its median,
so the project's own forecast stays the middle), and every risk without a response (status Accept or open) occurs
with its probability on the risk scale and adds its impact; a mitigated, avoided or transferred risk is taken to be
in the forecast already. The same for the end date with the schedule ratio. Results: the probability that each project
and the whole portfolio stay within budget and approved end date, and P50 / P-confidence values."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.core.business_rules import BusinessRules, current_rules

SEED = 20261027
CLOSED_STATES = ("closed", "cancelled", "canceled", "complete", "completed")
LEVELS = ("Low", "Moderate", "High")
OPEN_RISK = ("", "accept", "open", "new")  # risks without a response; a mitigated, avoided or transferred one is left out


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _day(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or not str(value).strip():
        return None
    try:
        stamp = pd.Timestamp(str(value)[:10])
    except (ValueError, TypeError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def _closed(row: dict[str, Any]) -> bool:
    return str(row.get("state") or "").strip().lower() in CLOSED_STATES


# =========================================================================== reference classes (E3)
@dataclass
class HistoryRow:
    project_id: str
    execution_type: str
    portfolio: str
    closed: bool
    cost_ratio: float | None
    schedule_ratio: float | None


def history(projects: pd.DataFrame, baselines: pd.DataFrame | None) -> list[HistoryRow]:
    """Cost and schedule ratio of every project against its first baseline (without baselines: its budget and
    approved end date)."""
    first: dict[str, dict[str, Any]] = {}
    if baselines is not None and not baselines.empty and "project_number" in baselines.columns:
        ordered = baselines.assign(_v=pd.to_numeric(baselines.get("version"), errors="coerce").fillna(1))
        for row in ordered.sort_values("_v").to_dict("records"):
            first.setdefault(str(row["project_number"]), row)
    rows = []
    for row in projects.to_dict("records"):
        pid = str(row.get("project_number"))
        base = first.get(pid, {})
        budget = _num(base.get("baseline_budget_eur")) or _num(row.get("budget_cost_eur"))
        closed = _closed(row)
        final = _num(row.get("actual_cost_eur")) if closed else _num(row.get("estimate_at_completion_eur"))
        cost = final / budget if budget and final else None
        start = _day(row.get("actual_start_date")) or _day(row.get("planned_start_date"))
        base_end = _day(base.get("baseline_end_date")) or _day(row.get("approved_end_date"))
        end = _day(row.get("planned_end_date"))
        schedule = None
        if start and base_end and end and base_end > start:
            schedule = (end - start).days / (base_end - start).days
        rows.append(HistoryRow(pid, str(row.get("execution_type") or ""), str(row.get("portfolio") or ""), closed,
                               None if cost is None else round(cost, 4),
                               None if schedule is None else round(schedule, 4)))
    return rows


@dataclass
class ReferenceClass:
    subject: str
    basis: str  # which projects form the class
    members: list[str]
    closed: int
    cost_ratios: list[float] = field(default_factory=list)
    schedule_ratios: list[float] = field(default_factory=list)
    widened: bool = False

    def percentile(self, kind: str, pct: float) -> float | None:
        values = self.cost_ratios if kind == "cost" else self.schedule_ratios
        return round(float(np.percentile(values, pct)), 3) if values else None

    def to_dict(self, confidence: float, budget: float | None = None,
                duration_days: int | None = None) -> dict[str, Any]:
        cost = {f"p{p}": self.percentile("cost", p) for p in (10, 50, 90)}
        cost["p_conf"] = self.percentile("cost", confidence)
        schedule = {f"p{p}": self.percentile("schedule", p) for p in (10, 50, 90)}
        schedule["p_conf"] = self.percentile("schedule", confidence)
        out: dict[str, Any] = {"subject": self.subject, "basis": self.basis, "size": len(self.members),
                               "closed": self.closed, "widened": self.widened, "confidence_pct": confidence,
                               "cost_ratio": cost, "schedule_ratio": schedule}
        if budget and cost["p_conf"] is not None:
            out["budget_at_confidence_eur"] = round(budget * max(cost["p_conf"], 1.0), 0)
            out["buffer_eur"] = round(budget * max(cost["p_conf"] - 1.0, 0.0), 0)
        if duration_days and schedule["p_conf"] is not None:
            out["buffer_days"] = int(round(duration_days * max(schedule["p_conf"] - 1.0, 0.0)))
        if cost["p_conf"] is not None:
            out["headline"] = (f"{confidence:g} % of {len(self.members)} comparable projects ({self.basis}) stayed "
                               f"within {(cost['p_conf'] - 1) * 100:+.0f} % of their first budget"
                               + (f" and {(schedule['p_conf'] - 1) * 100:+.0f} % of their planned duration"
                                  if schedule["p_conf"] is not None else "") + ".")
        return out


def reference_class(subject: str, execution_type: str, portfolio: str, rows: list[HistoryRow],
                    min_size: int, exclude: str | None = None) -> ReferenceClass:
    """The comparable projects of a subject: same execution type and portfolio, else same execution type, else
    the whole portfolio (widened)."""
    pool = [r for r in rows if r.project_id != exclude and r.cost_ratio is not None]
    tiers = ((f"{execution_type}, {portfolio}", [r for r in pool if r.execution_type == execution_type
                                                and r.portfolio == portfolio]),
             (f"{execution_type or 'any method'}", [r for r in pool if r.execution_type == execution_type]),
             ("whole portfolio", pool))
    for i, (basis, members) in enumerate(tiers):
        if len(members) >= min_size or i == len(tiers) - 1:
            return ReferenceClass(subject, basis, [m.project_id for m in members], sum(m.closed for m in members),
                                  [m.cost_ratio for m in members if m.cost_ratio is not None],
                                  [m.schedule_ratio for m in members if m.schedule_ratio is not None],
                                  widened=i > 0)
    raise AssertionError("unreachable")


def project_reference(dataset: Any, project_id: str, rules: BusinessRules | None = None) -> dict[str, Any]:
    """Reference class forecast of one project (its own row is not part of its class)."""
    rules = rules or current_rules()
    row = dataset.projects[dataset.projects["project_number"].astype(str) == str(project_id)]
    if row.empty:
        raise KeyError(project_id)
    r = row.iloc[0].to_dict()
    rows = history(dataset.projects, getattr(dataset, "baselines", None))
    cls = reference_class(str(project_id), str(r.get("execution_type") or ""), str(r.get("portfolio") or ""), rows,
                          rules.reference_class_min_projects, exclude=str(project_id))
    start, end = _day(r.get("planned_start_date")), _day(r.get("approved_end_date"))
    duration = (end - start).days if start and end and end > start else None
    return cls.to_dict(rules.confidence_level_pct, _num(r.get("budget_cost_eur")), duration)


def demand_reference(dataset: Any, demand_id: str, rules: BusinessRules | None = None) -> dict[str, Any]:
    """Reference class forecast of a planned demand: its planned cost (CapEx + OpEx of its year plans) and duration
    against the projects of the same execution type and portfolio."""
    rules = rules or current_rules()
    demands = getattr(dataset, "demands", pd.DataFrame())
    row = demands[demands["demand_number"].astype(str) == str(demand_id)] if not demands.empty else demands
    if row.empty:
        raise KeyError(demand_id)
    d = row.iloc[0].to_dict()
    plans = getattr(dataset, "demand_year_plans", pd.DataFrame())
    cost = None
    if not plans.empty and "demand_number" in plans.columns:
        mine = plans[plans["demand_number"].astype(str) == str(demand_id)]
        cost = float(pd.to_numeric(mine.get("capex_eur"), errors="coerce").fillna(0).sum()
                     + pd.to_numeric(mine.get("opex_eur"), errors="coerce").fillna(0).sum()) or None
    months = _num(d.get("duration_months"))
    rows = history(dataset.projects, getattr(dataset, "baselines", None))
    cls = reference_class(str(demand_id), str(d.get("execution_type") or ""), str(d.get("portfolio") or ""), rows,
                          rules.reference_class_min_projects)
    return cls.to_dict(rules.confidence_level_pct, cost, int(months * 30.44) if months else None)


# =========================================================================== portfolio with probabilities (E4)
def _risk_scale(rules: BusinessRules) -> tuple[dict[str, float], dict[str, float]]:
    probability = {"Low": rules.risk_probability_low_pct / 100, "Moderate": rules.risk_probability_moderate_pct / 100,
                   "High": rules.risk_probability_high_pct / 100}
    impact = {"Low": rules.risk_impact_low_eur, "Moderate": rules.risk_impact_moderate_eur,
              "High": rules.risk_impact_high_eur}
    return probability, impact


def _level(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return next((lvl for lvl in LEVELS if lvl.casefold() == text), "Moderate")


def _centred(values: list[float]) -> np.ndarray:
    """Ratios divided by their median: the spread of the class around 1."""
    arr = np.asarray(values or [1.0], dtype=float)
    median = float(np.median(arr)) or 1.0
    return arr / median


def simulate_portfolio(dataset: Any, today: date, rules: BusinessRules | None = None,
                       runs: int | None = None, seed: int = SEED) -> dict[str, Any]:
    """Monte Carlo over the open projects of the dataset (see the module text)."""
    rules = rules or current_rules()
    runs = int(runs or rules.simulation_runs)
    conf = rules.confidence_level_pct
    rng = np.random.default_rng(seed)
    projects = dataset.projects
    rows = history(projects, getattr(dataset, "baselines", None))
    risks = getattr(dataset, "risks", pd.DataFrame())
    probability, impact = _risk_scale(rules)
    total_cost = np.zeros(runs)
    total_budget = 0.0
    result_rows = []
    for row in projects.to_dict("records"):
        if _closed(row):
            continue
        pid = str(row.get("project_number"))
        budget = _num(row.get("budget_cost_eur"))
        actual = _num(row.get("actual_cost_eur")) or 0.0
        eac = _num(row.get("estimate_at_completion_eur")) or budget
        if not budget or eac is None:
            continue
        cls = reference_class(pid, str(row.get("execution_type") or ""), str(row.get("portfolio") or ""), rows,
                              rules.reference_class_min_projects, exclude=pid)
        remaining = max(eac - actual, 0.0)
        cost = actual + remaining * rng.choice(_centred(cls.cost_ratios), size=runs)
        open_risks = risks[risks["project_number"].astype(str) == pid] if not risks.empty else risks
        for risk in open_risks.to_dict("records"):
            if str(risk.get("risk_status") or "").strip().lower() not in OPEN_RISK:
                continue
            occurs = rng.random(runs) < probability[_level(risk.get("probability"))]
            cost = cost + occurs * impact[_level(risk.get("impact"))]
        total_cost += cost
        total_budget += budget
        entry: dict[str, Any] = {
            "project_id": pid, "project_name": str(row.get("project_name") or ""), "budget_eur": budget,
            "forecast_eur": eac, "p50_cost_eur": round(float(np.percentile(cost, 50)), 0),
            "p_conf_cost_eur": round(float(np.percentile(cost, conf)), 0),
            "probability_within_budget_pct": round(float((cost <= budget + 1e-6).mean()) * 100, 1),
            "reference_basis": cls.basis, "reference_size": len(cls.members)}
        start = _day(row.get("actual_start_date")) or _day(row.get("planned_start_date"))
        approved, planned = _day(row.get("approved_end_date")), _day(row.get("planned_end_date"))
        if start and approved and planned and planned > today:
            remaining_days = (planned - max(start, today)).days
            ends = remaining_days * rng.choice(_centred(cls.schedule_ratios), size=runs)
            deadline = (approved - max(start, today)).days
            entry["probability_on_time_pct"] = round(float((ends <= deadline).mean()) * 100, 1)
            entry["p_conf_end_date"] = (pd.Timestamp(max(start, today))
                                        + pd.Timedelta(days=int(np.percentile(ends, conf)))).date().isoformat()
        elif planned and planned <= today:
            entry["probability_on_time_pct"] = 0.0 if approved and planned > approved else None
        result_rows.append(entry)
    result_rows.sort(key=lambda r: (r["probability_within_budget_pct"], r["project_id"]))
    summary: dict[str, Any] = {"runs": runs, "seed": seed, "confidence_pct": conf, "projects": len(result_rows),
                               "budget_eur": round(total_budget, 0)}
    if result_rows:
        summary.update({
            "p50_cost_eur": round(float(np.percentile(total_cost, 50)), 0),
            "p_conf_cost_eur": round(float(np.percentile(total_cost, conf)), 0),
            "probability_within_budget_pct": round(float((total_cost <= total_budget + 1e-6).mean()) * 100, 1),
            "reserve_needed_eur": round(max(float(np.percentile(total_cost, conf)) - total_budget, 0.0), 0)})
        summary["headline"] = (f"The open portfolio stays within its budget of €{total_budget:,.0f} with a "
                               f"probability of {summary['probability_within_budget_pct']:g} %; at {conf:g} % "
                               f"confidence it costs €{summary['p_conf_cost_eur']:,.0f}.")
    return {"summary": summary, "projects": result_rows}
