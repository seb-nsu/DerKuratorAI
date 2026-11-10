"""Data Steward checks for demands (deterministic). ERROR blocks a demand from planning scenarios (it is listed as
Out with the reason), WARNING is reported. The issues use the demand number in place of the project id."""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.core import clock
from app.crew.tools.data_steward_tools import DataQualityIssue
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.data_adapters.schema import SCHEMA
from app.planning.scoring import COMMITTED_STATUSES, ELIGIBLE_STATUSES, MANDATORY, criteria

REQUIRED = ("demand_name", "demand_owner", "business_unit", "portfolio", "demand_type", "demand_status",
            "earliest_start_date", "duration_months")
RULE = "Planning"


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT or (isinstance(v, str) and not v.strip())


def split_ids(value) -> list[str]:
    """'DMD-1; PRJ-2' -> ['DMD-1', 'PRJ-2']."""
    if _blank(value):
        return []
    return [p.strip() for p in str(value).replace(",", ";").split(";") if p.strip()]


def known_scopes(ds: PortfolioDataset) -> dict[str, set[str]]:
    """Business units and portfolios the organisation knows: from projects and budget envelopes."""
    scopes: dict[str, set[str]] = {"business_unit": set(), "portfolio": set()}
    for col in scopes:
        if col in ds.projects.columns:
            scopes[col] |= set(ds.projects[col].dropna().astype(str).str.strip())
    env = getattr(ds, "budget_envelopes", pd.DataFrame())
    if not env.empty and {"scope_type", "scope"} <= set(env.columns):
        for scope_type, scope in zip(env["scope_type"].astype(str).str.strip(), env["scope"].astype(str).str.strip(),
                                     strict=True):
            scopes.setdefault(scope_type, set()).add(scope)
    return scopes


def check_demands(ds: PortfolioDataset, as_of: date | None = None, references: bool = True) -> list[DataQualityIssue]:
    """Data-quality findings of the demands. `references=False` skips the predecessor check: in a project manager's
    view the other projects are filtered out, and the finding would name them."""
    d = ds.demands
    if d.empty or DEMAND_KEY not in d.columns:
        return []
    as_of = as_of or clock.today()
    issues: list[DataQualityIssue] = []

    def add(severity: str, demand: str, column: str, message: str) -> None:
        issues.append(DataQualityIssue(severity, demand, column, message, RULE))

    ids = d[DEMAND_KEY].astype(str).str.strip()
    for dup in sorted(set(ids[ids.duplicated()])):
        add("ERROR", dup, DEMAND_KEY, "Demand number is not unique")
    plans = ds.demand_year_plans
    plan_ids = set(plans[DEMAND_KEY].astype(str).str.strip()) if not plans.empty and DEMAND_KEY in plans.columns else set()
    known_ids = set(ids) | set(ds.project_numbers)
    scopes = known_scopes(ds)
    goals = ds.strategic_goals
    goal_ids = set(goals["goal_id"].astype(str).str.strip()) if not goals.empty and "goal_id" in goals.columns else set()
    allowed = SCHEMA["Demands"].allowed
    horizon_end = as_of.year + 3
    for row in d.to_dict(orient="records"):
        did = str(row.get(DEMAND_KEY) or "").strip() or "?"
        for col in REQUIRED:
            if _blank(row.get(col)):
                add("ERROR", did, col, "Required field is empty")
        for col in ("demand_type", "demand_status", "tshirt_size"):
            value = row.get(col)
            if not _blank(value) and value not in allowed.get(col, (value,)):
                add("ERROR", did, col, f"Value '{value}' not allowed ({', '.join(allowed[col])})")
        if row.get("demand_type") == MANDATORY and _blank(row.get("mandatory_reason")):
            add("ERROR", did, "mandatory_reason", "Mandatory demand without a reason (regulation, contract, security)")
        earliest, latest = pd.to_datetime(row.get("earliest_start_date")), pd.to_datetime(row.get("latest_start_date"))
        if pd.notna(earliest) and pd.notna(latest) and latest < earliest:
            add("ERROR", did, "latest_start_date", "Latest start lies before the earliest start")
        duration = pd.to_numeric(row.get("duration_months"), errors="coerce")
        if not _blank(row.get("duration_months")) and (pd.isna(duration) or duration <= 0):
            add("ERROR", did, "duration_months", "Duration must be a positive number of months")
        status = row.get("demand_status")
        if status in ELIGIBLE_STATUSES + COMMITTED_STATUSES and did not in plan_ids:
            add("ERROR", did, "Demand_Year_Plans", "No annual figures (CapEx/OpEx per quarter)")
        for col in ("business_unit", "portfolio"):
            value = row.get(col)
            if not _blank(value) and scopes.get(col) and str(value).strip() not in scopes[col]:
                add("WARNING", did, col, f"Unknown {col.replace('_', ' ')} '{value}' (no project and no budget envelope)")
        goal = row.get("strategic_goal")
        if not _blank(goal) and goal_ids and str(goal).strip() not in goal_ids:
            add("WARNING", did, "strategic_goal", f"Unknown strategic goal '{goal}'")
        for ref in split_ids(row.get("depends_on")) if references else []:
            if ref not in known_ids:
                add("WARNING", did, "depends_on", f"Predecessor '{ref}' is neither a demand nor a project")
    if not plans.empty and DEMAND_KEY in plans.columns:
        p = plans.assign(_id=plans[DEMAND_KEY].astype(str).str.strip(), _year=pd.to_numeric(plans.get("year"), errors="coerce"),
                         _q=pd.to_numeric(plans.get("quarter"), errors="coerce"))
        for col in ("capex_eur", "opex_eur"):
            if col in p.columns:
                bad = p[pd.to_numeric(p[col], errors="coerce") < 0]
                for did in sorted(set(bad["_id"])):
                    add("ERROR", did, col, "Negative amount in the annual figures")
        for did in sorted(set(p.loc[~p["_q"].isin([1, 2, 3, 4]), "_id"])):
            add("ERROR", did, "quarter", "Quarter must be 1-4")
        for did in sorted(set(p.loc[p["_year"].isna(), "_id"])):
            add("ERROR", did, "year", "Year missing in the annual figures")
        late = p[p["_year"] > horizon_end]
        for did in sorted(set(late["_id"])):
            add("WARNING", did, "year", f"Annual figures beyond the planning horizon ({horizon_end})")
        valid = p.dropna(subset=["_year", "_q"])
        valid = valid[valid["_q"].isin([1, 2, 3, 4])].sort_values(["_year", "_q"]).groupby("_id").first()
        starts = {did: pd.Timestamp(int(r["_year"]), int(3 * r["_q"] - 2), 1) for did, r in valid.iterrows()}
        earliest = dict(zip(ids, pd.to_datetime(d.get("earliest_start_date"), errors="coerce"), strict=True))
        for did, start in starts.items():
            first = earliest.get(did)
            if first is not None and pd.notna(first) and start < first.to_period("Q").to_timestamp():
                add("WARNING", did, "earliest_start_date", "Annual figures start before the earliest start")
    model = criteria(ds)
    scores = ds.demand_scores
    if not scores.empty and {"criterion", "score"} <= set(scores.columns):
        scale = model.set_index("criterion")
        for row in scores.to_dict(orient="records"):
            did, crit = str(row.get(DEMAND_KEY) or "?").strip(), str(row.get("criterion") or "").strip()
            if crit not in scale.index:
                add("WARNING", did, "criterion", f"Score for unknown criterion '{crit}'")
                continue
            value = pd.to_numeric(row.get("score"), errors="coerce")
            if pd.isna(value) or not scale.at[crit, "scale_min"] <= value <= scale.at[crit, "scale_max"]:
                add("ERROR", did, "score", f"Score of '{crit}' outside the scale "
                    f"{scale.at[crit, 'scale_min']:g}-{scale.at[crit, 'scale_max']:g}")
    return issues


def blocked_demands(issues: list[DataQualityIssue]) -> dict[str, str]:
    """Demand -> first ERROR message (these demands cannot be planned until the data is fixed)."""
    out: dict[str, str] = {}
    for i in issues:
        if i.severity == "ERROR":
            out.setdefault(i.project_id, f"{i.column}: {i.message}")
    return out
