"""Stage 2b of the red trigger: what does the red project mean for the rest of the portfolio? Only existing functions:
the dependency graph (dependents, predecessors, required-by dates), the shared teams of the resource plans, the
management reserve and the projects that spend less than their budget, the Monte Carlo over the portfolio (fixed
seed), the strategic goals and the risk clusters. The result is an ImpactReport with structured figures and the verbatim
graph tool texts; on its own it is already the first part of the recommendation."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.core.business_rules import BusinessRules
from app.crew.tools.status_check import StatusCheck
from app.recovery.diagnosis import SCHEDULE, Cause

SHARED_TEAM_MONTHS = 3


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


def red_projects(checks: list[StatusCheck], rules: BusinessRules) -> set[str]:
    """Reported red, or calculated red when the calculated trigger is on."""
    return {c.project_id for c in checks
            if c.reported == "Red" or (rules.recovery_trigger_calculated and c.calculated == "Red")}


def teams_of(resource_plans: pd.DataFrame | None, today: date, months: int = SHARED_TEAM_MONTHS) -> pd.DataFrame:
    """Resource plan rows of the next months (team, project, utilisation, FTE)."""
    if resource_plans is None or resource_plans.empty:
        return pd.DataFrame(columns=["project_number", "assignment_group", "utilization_pct", "allocated_fte"])
    rows = resource_plans.copy()
    rows["_month"] = pd.to_datetime(rows["month"], errors="coerce")
    start = pd.Timestamp(today.replace(day=1))
    rows = rows[(rows["_month"] >= start) & (rows["_month"] < start + pd.DateOffset(months=months))]
    return rows


def shared_teams(resource_plans: pd.DataFrame | None, project_id: str, today: date, red: set[str]) -> list[dict[str, Any]]:
    rows = teams_of(resource_plans, today)
    if rows.empty:
        return []
    rows = rows.assign(project_number=rows["project_number"].astype(str))
    own = rows[rows["project_number"] == str(project_id)]
    out = []
    for team in sorted(set(own["assignment_group"].astype(str))):
        others = rows[(rows["assignment_group"].astype(str) == team) & (rows["project_number"] != str(project_id))]
        neighbours = sorted(set(others["project_number"]))
        util = pd.to_numeric(rows[rows["assignment_group"].astype(str) == team]["utilization_pct"], errors="coerce")
        out.append({"team": team, "projects": neighbours, "red_neighbours": [p for p in neighbours if p in red],
                    "peak_utilisation_pct": None if util.dropna().empty else round(float(util.max()), 1),
                    "own_fte": round(float(pd.to_numeric(own[own["assignment_group"].astype(str) == team]
                                                        ["allocated_fte"], errors="coerce").fillna(0).mean()), 2)})
    return out


def underspend(projects: pd.DataFrame, exclude: set[str]) -> list[dict[str, Any]]:
    """Open projects whose forecast stays below their budget (possible donors of budget)."""
    out = []
    for row in projects.to_dict("records"):
        pid = str(row.get("project_number"))
        if pid in exclude or str(row.get("state") or "").lower() in ("closed", "cancelled", "canceled", "complete",
                                                                     "completed", "on hold"):
            continue
        budget, eac = _num(row.get("budget_cost_eur")), _num(row.get("estimate_at_completion_eur"))
        actual = _num(row.get("actual_cost_eur")) or 0.0
        if budget is None or eac is None or eac >= budget:
            continue
        free = min(budget - eac, budget - actual)
        if free > 0:
            out.append({"project_id": pid, "project_name": str(row.get("project_name") or ""),
                        "priority": _num(row.get("priority")), "underspend_eur": round(free, 2)})
    return sorted(out, key=lambda x: -x["underspend_eur"])


def expected_delay_days(causes: list[Cause]) -> int:
    return max((c.gap_days for c in causes if c.kind == SCHEDULE), default=0)


def build_impact(project_id: str, dataset: Any, checks: list[StatusCheck], causes: list[Cause], today: date,
                 rules: BusinessRules) -> dict[str, Any]:
    """Runs inside a scenario context of the whole portfolio (app.recovery.engine sets it)."""
    from app.crew.tools import context as ctx
    from app.crew.tools import graph_tools, pmo_tools
    from app.crew.tools.forecast_ranges import simulate_portfolio
    from app.graph.backends import backend_for
    from app.graph.portfolio_graph import get_graph

    red = red_projects(checks, rules)
    texts: list[str] = []
    impact: dict[str, Any] = {"project_id": project_id, "red_projects": sorted(red)}
    graph_ok = True
    try:
        backend = backend_for(get_graph())
        dependents = backend.dependents(project_id, graph_tools.MAX_DEPTH)
        predecessors = backend.predecessors(project_id, graph_tools.MAX_DEPTH)
        successors = graph_tools.successor_deadlines(project_id)
    except Exception:  # noqa: BLE001 - the graph is a dependency: without it the report says so
        graph_ok, dependents, predecessors, successors = False, [], [], []
    texts.append(graph_tools.downstream_impact(project_id))
    texts.append(graph_tools.upstream_bottlenecks(project_id))
    delay = expected_delay_days(causes)
    row = ctx.get_project(project_id)
    planned = _day(row.get("planned_end_date"))
    expected_end = (max(planned, today) if planned else today) + pd.Timedelta(days=delay).to_pytimedelta() \
        if planned or delay else None
    breaches = []
    for s in successors:
        required = _day(s.get("required_by"))
        if required and expected_end and expected_end > required:
            breaches.append({"successor": s["id"], "name": s.get("name"), "type": s.get("type"),
                             "required_by": required.isoformat(), "days_late": (expected_end - required).days})
    impact.update(
        graph_available=graph_ok,
        dependents=[{"id": d["id"], "name": d.get("name"), "hops": d["hops"], "dep_types": d.get("dep_types"),
                     "red": d["id"] in red} for d in dependents],
        predecessors=[{"id": p["id"], "name": p.get("name"), "hops": p["hops"], "dep_types": p.get("dep_types"),
                       "red": p["id"] in red} for p in predecessors],
        upstream_red=[p["id"] for p in predecessors if p["id"] in red and p["hops"] == 1],
        expected_delay_days=delay, expected_end=expected_end.isoformat() if expected_end else None,
        deadline_breaches=breaches,
    )
    teams = shared_teams(dataset.resource_plans, project_id, today, red)
    impact["teams"] = teams
    try:
        balance = pmo_tools.reserve_balance()
    except Exception:  # noqa: BLE001 - no reserve figure, no reserve lever
        balance = rules.management_reserve_eur
    floor = rules.management_reserve_eur * rules.reserve_floor_pct / 100
    gap = max((c.gap_eur for c in causes if c.kind == "cost"), default=0.0)
    impact["budget"] = {"gap_eur": round(gap, 2), "reserve_balance_eur": round(balance, 2),
                        "reserve_floor_eur": round(floor, 2), "reserve_free_eur": round(max(balance - floor, 0), 2),
                        "underspend": underspend(dataset.projects, red | {project_id})}
    try:
        forecast = simulate_portfolio(dataset, today, rules)
        own = next((p for p in forecast["projects"] if p["project_id"] == project_id), {})
        impact["portfolio"] = {"probability_within_budget_pct": forecast["summary"].get("probability_within_budget_pct"),
                               "reserve_needed_eur": forecast["summary"].get("reserve_needed_eur"),
                               "project_probability_within_budget_pct": own.get("probability_within_budget_pct"),
                               "project_probability_on_time_pct": own.get("probability_on_time_pct"),
                               "seed": forecast["summary"].get("seed"), "runs": forecast["summary"].get("runs")}
    except Exception:  # noqa: BLE001 - the Monte Carlo is context, not a precondition
        impact["portfolio"] = {}
    goals = dataset.project_goals if not dataset.project_goals.empty else pd.DataFrame()
    linked = []
    if not goals.empty and "project_number" in goals.columns:
        mine = goals[goals["project_number"].astype(str) == project_id]
        names = dict(zip(dataset.strategic_goals.get("goal_id", []), dataset.strategic_goals.get("goal_name", []),
                         strict=False)) if not dataset.strategic_goals.empty else {}
        weights = dict(zip(dataset.strategic_goals.get("goal_id", []), dataset.strategic_goals.get("weight", []),
                           strict=False)) if not dataset.strategic_goals.empty else {}
        for g in mine.to_dict("records"):
            gid = g.get("goal_id")
            linked.append({"goal_id": gid, "goal_name": names.get(gid, gid), "weight": _num(weights.get(gid))})
    impact["strategic_goals"] = linked
    clusters = []
    risks = dataset.risks
    if not risks.empty and "systemic_risk_cluster" in risks.columns:
        mine = risks[risks["project_number"].astype(str) == project_id]
        for cluster in sorted({str(c) for c in mine["systemic_risk_cluster"].dropna() if str(c).strip()}):
            members = sorted(set(risks[risks["systemic_risk_cluster"].astype(str) == cluster]["project_number"]
                                 .astype(str)))
            clusters.append({"cluster": cluster, "projects": members, "red": [p for p in members if p in red],
                             "systemic": len(members) >= 3})
    impact["clusters"] = clusters
    impact["tool_texts"] = [t for t in texts if t]
    return impact
