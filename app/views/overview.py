"""Overview page: scope line, top risks, KPI ribbon, overloaded teams, a Project Manager's waiting proposals and the
PMO's management reserve – all from the reader's scope-filtered data."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_PM
from app.data_adapters import PortfolioDataset
from app.data_adapters.dataset import CLOSED_PROJECT_STATES

logger = logging.getLogger("derkurator.views.overview")
TOP_RISKS_UNAVAILABLE = "Top risks are temporarily unavailable – the dependency graph is not ready yet."


def scope_line(role: str, visible: int, total: int) -> str:
    """The page's scope line; a Project Manager never learns the size of the whole portfolio."""
    return f"Your projects · {visible} in your scope" if role == ROLE_PM else \
        f"Portfolio · scope {visible} of {total} projects"


def top_risks(allowed: set[str]) -> tuple[list[dict[str, Any]], str | None]:
    """Top blocked projects / systemic risks from the dependency graph in the reader's scope; (risks, error). The
    error detail is for the system log only, never for a business page."""
    try:
        from app import services

        return services.top_portfolio_risks(set(allowed)), None
    except Exception as exc:  # noqa: BLE001 - the dashboard must render even without the graph
        logger.warning("Top risks unavailable: %s", exc)
        return [], str(exc)


def team_overload(dataset: PortfolioDataset) -> pd.DataFrame:
    """Overloaded teams (above the configured capacity limit) from the resource plans, sorted by peak."""
    rp = dataset.resource_plans
    if rp.empty:
        return pd.DataFrame()
    rp = rp.assign(utilization_pct=pd.to_numeric(rp["utilization_pct"], errors="coerce"))
    over = rp[rp["utilization_pct"] > get_runtime_config().business_rules.team_capacity_limit_pct]
    if over.empty:
        return pd.DataFrame()
    return (
        over.groupby("assignment_group")
        .agg(peak=("utilization_pct", "max"), months=("month", "nunique"), projects=("project_number", "nunique"))
        .sort_values("peak", ascending=False)
        .reset_index()
        .rename(columns={"assignment_group": "team"})
    )


def _number(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def kpis(view: pd.DataFrame, ds: PortfolioDataset, teams: pd.DataFrame, steward=None) -> dict[str, Any]:
    """The KPI ribbon's figures. `show_active`: a card that only repeats the scope ("10 of 10 active") is left out."""
    active = int((~view["state"].isin(CLOSED_PROJECT_STATES)).sum())
    critical = int((view["status"] == "Red").sum())
    rp = ds.resource_plans
    max_util = pd.to_numeric(rp["utilization_pct"], errors="coerce").max() if not rp.empty else float("nan")
    out = {"projects": len(view), "active_projects": active, "show_active": active != len(view),
           "total_budget_eur": _number(pd.to_numeric(view["budget_cost_eur"], errors="coerce").sum()),
           "total_eac_eur": _number(pd.to_numeric(view["estimate_at_completion_eur"], errors="coerce").sum()),
           "critical_projects": critical,
           "critical_share_pct": critical / len(view) * 100 if critical and len(view) else 0.0,
           "max_team_utilisation_pct": _number(max_util),
           "top_team": str(teams.iloc[0]["team"]) if not teams.empty else None,
           "capacity_limit_pct": get_runtime_config().business_rules.team_capacity_limit_pct}
    if steward is not None:
        out.update(data_quality_index=steward.quality_index, blocked_projects=len(steward.blocked_project_ids))
    return out


def risk_value_points(view: pd.DataFrame) -> pd.DataFrame:
    """The projects of the risk/value matrix (risk score, planned ROI and budget all known)."""
    cols = [c for c in ("project_number", "project_name", "status", "execution_type", "risk_score", "planned_roi_pct",
                        "budget_cost_eur") if c in view.columns]
    return view.dropna(subset=["risk_score", "planned_roi_pct", "budget_cost_eur"])[cols]


def waiting_proposals(user_id: str) -> list[dict]:
    """A Project Manager's own proposals still waiting for the PMO."""
    from app.core.proposals import SUBMITTED, list_proposals

    return [p for p in list_proposals(requester=user_id) if p["status"] == SUBMITTED]


def management_reserve(ds: PortfolioDataset, months: int = 6) -> dict[str, Any]:
    """Management reserve against its floor and the skill utilisation per skill and month (PMO overview section)."""
    from app.core import clock
    from app.core.business_rules import current_rules
    from app.services import pmo_figures

    skills, balance = pmo_figures(ds, months, clock.today())
    rules = current_rules()
    floor = rules.management_reserve_eur * rules.reserve_floor_pct / 100
    return {"balance_eur": float(balance), "floor_eur": float(floor), "below_floor": bool(balance < floor),
            "overload_alert_pct": rules.overload_alert_pct, "skills": skills}
