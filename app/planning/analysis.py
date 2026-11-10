"""Analysis of the annual plan, like the project pages offer it for running projects: the dependency chain of a demand
(and what a delay would push along), its money against the budget envelopes of its area, its people against the team
capacities, and the hot spots of the whole plan. Everything is read from the base plan the scenario engine calculates
(rank order, current data) – the same figures the planning verdict uses."""
from __future__ import annotations

from datetime import date
from typing import Any

import networkx as nx
import pandas as pd

from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.planning.model import resource_plans, start_quarter, year_plans
from app.planning.scenario import KINDS, OUT, ScenarioInput, ScenarioResult, run_scenario
from app.planning.steward import split_ids

PK = "project_number"
HOT_SHARE = 90.0  # a budget or capacity limit used to this share or more is a hot spot


def base_plan(ds: PortfolioDataset, as_of: date | None = None) -> ScenarioResult:
    """The plan as it stands: rank order, no forcing, no moves."""
    return run_scenario(ds, ScenarioInput(), as_of)


def _q(text: Any) -> int | None:
    """'2027-Q2' -> quarter index (year * 4 + quarter - 1)."""
    try:
        year, quarter = str(text).split("-Q")
        return int(year) * 4 + int(quarter) - 1
    except (ValueError, AttributeError):
        return None


def _qtext(index: int | None) -> str:
    return "–" if index is None else f"{index // 4}-Q{index % 4 + 1}"


def dependency_graph(ds: PortfolioDataset) -> nx.DiGraph:
    """Predecessor -> successor over demands and the projects they wait for (`depends_on`)."""
    g = nx.DiGraph()
    d = ds.demands
    if d.empty or DEMAND_KEY not in d.columns:
        return g
    for did, deps in zip(d[DEMAND_KEY].astype(str).str.strip(), d.get("depends_on", pd.Series(index=d.index)),
                         strict=True):
        g.add_node(did)
        for ref in split_ids(deps):
            g.add_edge(ref, did)
    return g


def _timeline(ds: PortfolioDataset, plan: ScenarioResult | None) -> dict[str, dict[str, Any]]:
    """Per demand and project: kind, name, start/end quarter index, plan decision (without a plan: the demand status),
    latest start."""
    out: dict[str, dict[str, Any]] = {}
    items = plan.items.set_index(DEMAND_KEY) if plan is not None and not plan.items.empty else pd.DataFrame()
    starts = start_quarter(ds)
    for row in ds.demands.to_dict(orient="records") if not ds.demands.empty else []:
        did = str(row.get(DEMAND_KEY) or "").strip()
        item = items.loc[did] if did in items.index else None
        decision = item["decision"] if item is not None else row.get("demand_status")
        start = _q(item["start"]) if item is not None and decision != OUT else _q(starts.get(did))
        end = _q(item["end"]) if item is not None and decision != OUT else None
        if start is None:  # no money planned yet: the earliest start
            earliest = pd.to_datetime(row.get("earliest_start_date"), errors="coerce")
            start = None if pd.isna(earliest) else earliest.year * 4 + earliest.quarter - 1
        if end is None and start is not None:
            months = pd.to_numeric(row.get("duration_months"), errors="coerce")
            end = start + max(int((months if pd.notna(months) else 3) + 2) // 3, 1) - 1
        latest = pd.to_datetime(row.get("latest_start_date"), errors="coerce")
        out[did] = {"kind": "Demand", "name": row.get("demand_name"), "start": start, "end": end,
                    "decision": decision, "latest": None if pd.isna(latest) else latest.year * 4 + latest.quarter - 1}
    projects = ds.projects
    if not projects.empty and PK in projects.columns:
        for row in projects.to_dict(orient="records"):
            end = pd.to_datetime(row.get("planned_end_date"), errors="coerce")
            begin = pd.to_datetime(row.get("planned_start_date"), errors="coerce")
            out[str(row[PK]).strip()] = {"kind": "Project", "name": row.get("project_name"),
                                         "start": None if pd.isna(begin) else begin.year * 4 + begin.quarter - 1,
                                         "end": None if pd.isna(end) else end.year * 4 + end.quarter - 1,
                                         "decision": row.get("state"), "latest": None}
    return out


def dependency_chain(ds: PortfolioDataset, demand: str, plan: ScenarioResult | None) -> pd.DataFrame:
    """Everything the demand waits for and everything that waits for it (transitively), with dates and plan decision.
    Only nodes of the given (possibly scope-filtered) dataset are named."""
    g, timeline = dependency_graph(ds), _timeline(ds, plan)
    if demand not in g:
        return pd.DataFrame()
    rows = []
    for direction, nodes, dist in (("Needs first", nx.ancestors(g, demand), nx.shortest_path_length(g.reverse(), demand)),
                                   ("Needed by", nx.descendants(g, demand), nx.shortest_path_length(g, demand))):
        for node in sorted(nodes, key=lambda n: (dist.get(n, 0), n)):
            if node not in timeline:
                continue  # outside the visible scope (or unknown): never named
            t = timeline[node]
            rows.append({"Direction": direction, "Steps": dist.get(node), "Item": node, "Kind": t["kind"],
                         "Name": t["name"], "Start": _qtext(t["start"]), "End": _qtext(t["end"]),
                         "Plan / state": t["decision"]})
    return pd.DataFrame(rows)


def hidden_links(ds: PortfolioDataset, demand: str) -> bool:
    """True if the demand's chain reaches items outside the visible dataset (a neutral hint, no IDs, no count)."""
    g = dependency_graph(ds)
    if demand not in g:
        return False
    known = set(ds.demands[DEMAND_KEY].astype(str)) | set(ds.project_numbers)
    return any(n not in known for n in nx.ancestors(g, demand) | nx.descendants(g, demand))


def slip_impact(ds: PortfolioDataset, demand: str, quarters: int, plan: ScenarioResult | None) -> pd.DataFrame:
    """What a delay of `quarters` pushes along: every successor that would start before its predecessor ends moves
    just enough, and so on down the chain. Says whether each move still fits the successor's latest start."""
    g, timeline = dependency_graph(ds), _timeline(ds, plan)
    if demand not in g or quarters <= 0 or timeline.get(demand, {}).get("end") is None:
        return pd.DataFrame()
    shift = {demand: quarters}
    for node in nx.topological_sort(g.subgraph(nx.descendants(g, demand) | {demand})):
        if node == demand or node not in timeline or timeline[node]["start"] is None:
            continue
        needed = 0
        for pred in g.predecessors(node):
            if pred in shift and timeline.get(pred, {}).get("end") is not None:
                needed = max(needed, timeline[pred]["end"] + shift[pred] + 1 - timeline[node]["start"])
        if needed > 0:
            shift[node] = needed
    rows = []
    for node, moved in shift.items():
        if node == demand or node not in timeline:
            continue
        t = timeline[node]
        new_start = t["start"] + moved
        fits = t["latest"] is None or new_start <= t["latest"]
        rows.append({"Item": node, "Kind": t["kind"], "Name": t["name"], "Start now": _qtext(t["start"]),
                     "Start after the delay": _qtext(new_start), "Moves by (quarters)": moved,
                     "Latest start": _qtext(t["latest"]), "Still fits": "yes" if fits else "no – drops out"})
    return pd.DataFrame(rows)


def budget_position(ds: PortfolioDataset, demand: str, plan: ScenarioResult) -> pd.DataFrame:
    """The demand's money per year next to the envelopes of its business unit and portfolio in the base plan."""
    row = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand]
    if row.empty or plan.budget.empty:
        return pd.DataFrame()
    row = row.iloc[0]
    money = year_plans(ds)
    money = money[money[DEMAND_KEY] == demand]
    item = plan.items.set_index(DEMAND_KEY).loc[demand] if demand in set(plan.items.get(DEMAND_KEY, [])) else None
    shift = int(item["shift_years"]) if item is not None and item["decision"] != OUT else 0
    own = {}
    for kind, label in KINDS:
        for year, value in money.groupby("year")[f"{kind}"].sum().items() if not money.empty else []:
            own[(int(year) + shift, label)] = float(value)
    b = plan.budget
    mine = b[((b["Scope type"] == "business unit") & (b["Scope"] == str(row.get("business_unit")))) |
             ((b["Scope type"] == "portfolio") & (b["Scope"] == str(row.get("portfolio"))))]
    mine = mine[mine["Quarter"] == "year"] if "year" in set(mine["Quarter"]) else mine
    out = mine.assign(**{"This demand": [own.get((int(y), k), 0.0) for y, k in zip(mine["Year"], mine["Kind"],
                                                                                    strict=True)]})
    out["In the plan"] = "yes" if item is not None and item["decision"] != OUT else "no"
    return out[["Scope type", "Scope", "Year", "Kind", "Cap type", "Limit", "Committed", "Planned", "Free", "Used %",
                "This demand", "In the plan"]].reset_index(drop=True)


def capacity_position(ds: PortfolioDataset, demand: str, plan: ScenarioResult) -> pd.DataFrame:
    """The demand's FTE per team and quarter next to the team's capacity use in the base plan."""
    res = resource_plans(ds)
    res = res[res[DEMAND_KEY] == demand]
    if res.empty:
        return pd.DataFrame()
    item = plan.items.set_index(DEMAND_KEY).loc[demand] if demand in set(plan.items.get(DEMAND_KEY, [])) else None
    shift = int(item["shift_years"]) if item is not None and item["decision"] != OUT else 0
    res = res.assign(Quarter=(res["year"] + shift).astype(str) + "-Q" + res["quarter"].astype(str))
    own = res.groupby(["assignment_group", "Quarter"], as_index=False)["fte"].sum()
    cap = plan.capacity if not plan.capacity.empty else pd.DataFrame(columns=["Team", "Quarter", "Limit FTE",
                                                                             "Used %"])
    cap = cap.assign(_team=cap["Team"].astype(str).str.split(" · ").str[0])
    merged = own.merge(cap, left_on=["assignment_group", "Quarter"], right_on=["_team", "Quarter"], how="left")
    merged = merged.rename(columns={"assignment_group": "Team (demand)", "fte": "This demand FTE"})
    keep = ["Team (demand)", "Quarter", "This demand FTE", "Limit FTE", "Committed FTE", "Planned FTE", "Used %"]
    return merged.reindex(columns=keep).sort_values(["Quarter", "Team (demand)"]).reset_index(drop=True)


def hot_spots(plan: ScenarioResult, share: float = HOT_SHARE) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Budget envelopes and team quarters used to `share` % or more in the base plan, fullest first."""
    budget = plan.budget
    if not budget.empty:
        budget = budget[pd.to_numeric(budget["Used %"], errors="coerce") >= share].sort_values("Used %",
                                                                                                ascending=False)
    capacity = plan.capacity
    if not capacity.empty:
        capacity = capacity[pd.to_numeric(capacity["Used %"], errors="coerce") >= share].sort_values("Used %",
                                                                                                    ascending=False)
    return budget.reset_index(drop=True), capacity.reset_index(drop=True)


def critical_chains(ds: PortfolioDataset, plan: ScenarioResult | None, limit: int = 10) -> pd.DataFrame:
    """Demands with the most items waiting for them (direct and indirect) – where a delay hurts most."""
    g, timeline = dependency_graph(ds), _timeline(ds, plan)
    rows = []
    for node in g.nodes:
        if timeline.get(node, {}).get("kind") != "Demand":
            continue
        waiting = [n for n in nx.descendants(g, node) if n in timeline]
        if waiting:
            t = timeline[node]
            rows.append({"Demand": node, "Name": t["name"], "Start": _qtext(t["start"]), "End": _qtext(t["end"]),
                         "Plan": t["decision"], "Items waiting": len(waiting),
                         "Waiting": ", ".join(sorted(waiting)[:6]) + (" …" if len(waiting) > 6 else "")})
    frame = pd.DataFrame(rows)
    return frame.sort_values(["Items waiting", "Demand"], ascending=[False, True]).head(limit).reset_index(drop=True) \
        if not frame.empty else frame
