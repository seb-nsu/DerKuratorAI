"""Planning views of the dataset: demand figures per year and quarter, the planning horizon and the visible list."""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.core import clock
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset

HORIZON_YEARS = 3  # the next year plus two preview years


def horizon(as_of: date | None = None) -> tuple[int, int]:
    """(first, last) planning year: the year after the reference date plus two preview years."""
    year = (as_of or clock.today()).year + 1
    return year, year + HORIZON_YEARS - 1


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def year_plans(ds: PortfolioDataset) -> pd.DataFrame:
    """Demand_Year_Plans with clean types: demand_number, year, quarter, capex_eur, opex_eur, benefit_eur."""
    p = ds.demand_year_plans
    cols = [DEMAND_KEY, "year", "quarter", "capex_eur", "opex_eur", "benefit_eur", "confidence"]
    if p.empty or not {DEMAND_KEY, "year", "quarter"} <= set(p.columns):
        return pd.DataFrame(columns=cols)
    out = p.reindex(columns=cols).copy()
    out[DEMAND_KEY] = out[DEMAND_KEY].astype(str).str.strip()
    out["year"] = pd.to_numeric(out["year"], errors="coerce")
    out["quarter"] = pd.to_numeric(out["quarter"], errors="coerce")
    out = out.dropna(subset=["year", "quarter"])
    out = out[out["quarter"].isin([1, 2, 3, 4])]
    out["year"], out["quarter"] = out["year"].astype(int), out["quarter"].astype(int)
    for col in ("capex_eur", "opex_eur", "benefit_eur"):
        out[col] = _num(out[col])
    return out.reset_index(drop=True)


def resource_plans(ds: PortfolioDataset) -> pd.DataFrame:
    """Demand_Resource_Plans with clean types: demand_number, year, quarter, assignment_group, skill, fte."""
    p = ds.demand_resource_plans
    cols = [DEMAND_KEY, "year", "quarter", "assignment_group", "skill", "fte"]
    if p.empty or not {DEMAND_KEY, "year", "quarter", "assignment_group"} <= set(p.columns):
        return pd.DataFrame(columns=cols)
    out = p.reindex(columns=cols).copy()
    out[DEMAND_KEY] = out[DEMAND_KEY].astype(str).str.strip()
    out["year"] = pd.to_numeric(out["year"], errors="coerce")
    out["quarter"] = pd.to_numeric(out["quarter"], errors="coerce")
    out = out.dropna(subset=["year", "quarter"])
    out["year"], out["quarter"] = out["year"].astype(int), out["quarter"].astype(int)
    out["fte"] = _num(out["fte"])
    out["assignment_group"] = out["assignment_group"].astype(str).str.strip()
    return out.reset_index(drop=True)


def yearly_totals(ds: PortfolioDataset) -> pd.DataFrame:
    """CapEx + OpEx per demand and year (wide: one column per year)."""
    p = year_plans(ds)
    if p.empty:
        return pd.DataFrame()
    p = p.assign(total=p["capex_eur"] + p["opex_eur"])
    return p.pivot_table(index=DEMAND_KEY, columns="year", values="total", aggfunc="sum", fill_value=0.0)


def start_quarter(ds: PortfolioDataset) -> pd.Series:
    """First (year, quarter) with money per demand, as 'YYYY-Qn'."""
    p = year_plans(ds)
    if p.empty:
        return pd.Series(dtype=object)
    p = p[(p["capex_eur"] + p["opex_eur"]) > 0]
    first = p.sort_values(["year", "quarter"]).groupby(DEMAND_KEY).first()
    return first["year"].astype(str) + "-Q" + first["quarter"].astype(str)
