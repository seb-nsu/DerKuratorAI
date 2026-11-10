"""What running projects already bind in the planning years: money (from Cost_Plans) and capacity (from
Resource_Plans). Only the rest of an envelope is free for demands.

Money: the planned cost per month and cost type of every open project. Where the cost plan ends before the project
does, the part of the forecast to complete (estimate_to_completion_eur) that no cost plan month covers is spread
evenly over the remaining months up to planned_end_date, split into CapEx/OpEx like the planned capital and operating
cost – those rows are marked as estimated."""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.data_adapters.dataset import CLOSED_PROJECT_STATES, PK, PortfolioDataset

COLUMNS = [PK, "business_unit", "portfolio", "year", "quarter", "capex_eur", "opex_eur", "estimated"]


def _num(value, default: float = 0.0) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(number) else float(number)


def open_projects(ds: PortfolioDataset) -> pd.DataFrame:
    p = ds.projects
    if p.empty:
        return p
    return p[~p.get("state", pd.Series(index=p.index, dtype=object)).isin(CLOSED_PROJECT_STATES)]


def committed_costs(ds: PortfolioDataset, as_of: date) -> pd.DataFrame:
    """Rows per project, year and quarter with the CapEx/OpEx bound from the reference month on."""
    projects = open_projects(ds)
    if projects.empty:
        return pd.DataFrame(columns=COLUMNS)
    start = pd.Timestamp(as_of).to_period("M").to_timestamp()
    cp = ds.cost_plans
    rows: list[dict] = []
    if not cp.empty and {PK, "month", "cost_type", "planned_cost_eur"} <= set(cp.columns):
        cp = cp.assign(month=pd.to_datetime(cp["month"], errors="coerce"),
                       planned=pd.to_numeric(cp["planned_cost_eur"], errors="coerce").fillna(0.0))
    else:
        cp = pd.DataFrame(columns=[PK, "month", "cost_type", "planned"])
    for project in projects.to_dict(orient="records"):
        pid = str(project[PK])
        own = cp[cp[PK].astype(str) == pid]
        future = own[own["month"] >= start]
        for r in future.to_dict(orient="records"):
            kind = "capex_eur" if str(r["cost_type"]).strip().lower() == "capex" else "opex_eur"
            rows.append({PK: pid, "month": r["month"], "capex_eur": 0.0, "opex_eur": 0.0, kind: r["planned"],
                         "estimated": False})
        end = pd.to_datetime(project.get("planned_end_date"), errors="coerce")
        remainder = _num(project.get("estimate_to_completion_eur")) - float(future["planned"].sum())
        if pd.isna(end) or remainder <= 1.0:
            continue
        last = own["month"].max() if not own.empty else pd.NaT
        first_open = max(start, (last + pd.offsets.MonthBegin(1)) if pd.notna(last) else start)
        months = pd.date_range(first_open, end.to_period("M").to_timestamp(), freq="MS")
        if len(months) == 0:
            continue
        capital, operating = _num(project.get("planned_capital_eur")), _num(project.get("planned_operating_eur"))
        share = capital / (capital + operating) if capital + operating > 0 else 0.0
        per_month = remainder / len(months)
        rows += [{PK: pid, "month": m, "capex_eur": per_month * share, "opex_eur": per_month * (1 - share),
                  "estimated": True} for m in months]
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.DataFrame(rows)
    frame["year"], frame["quarter"] = frame["month"].dt.year, frame["month"].dt.quarter
    scopes = projects.set_index(projects[PK].astype(str))
    frame["business_unit"] = frame[PK].map(scopes.get("business_unit", pd.Series(dtype=object)))
    frame["portfolio"] = frame[PK].map(scopes.get("portfolio", pd.Series(dtype=object)))
    out = frame.groupby([PK, "business_unit", "portfolio", "year", "quarter"], dropna=False).agg(
        capex_eur=("capex_eur", "sum"), opex_eur=("opex_eur", "sum"), estimated=("estimated", "any")).reset_index()
    return out[COLUMNS]


def committed_capacity(ds: PortfolioDataset, as_of: date) -> pd.DataFrame:
    """FTE per team, year and quarter bound by open projects (allocated_fte, averaged over the quarter's months)."""
    rp = ds.resource_plans
    cols = ["assignment_group", "year", "quarter", "fte"]
    if rp.empty or not {PK, "month", "assignment_group", "allocated_fte"} <= set(rp.columns):
        return pd.DataFrame(columns=cols)
    open_ids = set(open_projects(ds)[PK].astype(str))
    rp = rp[rp[PK].astype(str).isin(open_ids)]
    rp = rp.assign(month=pd.to_datetime(rp["month"], errors="coerce"),
                   fte=pd.to_numeric(rp["allocated_fte"], errors="coerce").fillna(0.0))
    rp = rp[rp["month"] >= pd.Timestamp(as_of).to_period("M").to_timestamp()]
    if rp.empty:
        return pd.DataFrame(columns=cols)
    rp = rp.assign(year=rp["month"].dt.year, quarter=rp["month"].dt.quarter,
                   assignment_group=rp["assignment_group"].astype(str).str.strip())
    out = rp.groupby(["assignment_group", "year", "quarter"])["fte"].sum().div(3).reset_index()
    return out[cols]
