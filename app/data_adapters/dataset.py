"""Relational portfolio dataset: all tables as DataFrames, linked via project_number."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from typing import Any, Iterable

import pandas as pd

from app.core import clock

PK = "project_number"

# dataset attribute name to table name in JSON/Excel
TABLES: dict[str, str] = {
    "projects": "Projects",
    "tasks": "Project_Tasks",
    "resource_plans": "Resource_Plans",
    "stakeholders": "Stakeholders",
    "risks": "Risks",
    "stories": "Stories",
    "dependencies": "Dependencies",
    "cost_plans": "Cost_Plans",
    "benefit_plans": "Benefit_Plans",  # benefit tranches with realisation dates (NPV timing)
    "status_reports": "Status_Reports",  # RAG history per reporting date (project detail page)
    "baselines": "Baselines",  # approved plan versions per project (re-baselining)
    "approvals": "Approvals",  # approval records incl. stage-gate decisions
    "reserve_ledger": "Reserve_Ledger",  # draws on and returns to the management reserve
    "team_skills": "Team_Skills",  # skills per team (no project – kept in every RLS view)
    "skill_demand": "Skill_Demand",  # demand hours per project, month and skill
    "benefit_actuals": "Benefit_Actuals",  # realised benefits reported after go-live (per tranche and month)
    "exchange_rates": "Exchange_Rates",  # currency -> EUR rate with a valid-from date (multi-currency sources)
    "strategic_goals": "Strategic_Goals",  # the organisation's strategic goals with their weight
    "project_goals": "Project_Goals",  # contribution (0-5) of a project to a strategic goal
    "demands": "Demands",  # planned initiatives that are no project yet (same master data as Projects)
    "demand_year_plans": "Demand_Year_Plans",  # CapEx, OpEx and benefit per demand, year and quarter
    "demand_resource_plans": "Demand_Resource_Plans",  # FTE per demand, year, quarter and team
    "scoring_model": "Scoring_Model",  # assessment criteria with weight, scale and direction
    "demand_scores": "Demand_Scores",  # score per demand and criterion (append-only, the latest counts)
    "budget_envelopes": "Budget_Envelopes",  # yearly (or quarterly) CapEx/OpEx cap per business unit or portfolio
    "capacity_limits": "Capacity_Limits",  # available FTE per team (optionally skill) and year (or quarter)
    "planning_scenarios": "Planning_Scenarios",  # saved planning scenarios, one row per status (append-only)
    "planning_items": "Planning_Items",  # decision per demand in a saved scenario (append-only)
}
# Tables without a project: every role sees them (they hold no project data)
GLOBAL_TABLES = ("team_skills", "exchange_rates", "strategic_goals", "scoring_model", "budget_envelopes",
                 "capacity_limits", "planning_scenarios")
# Tables of planned initiatives (Demands): filtered by the visible demand numbers instead of the projects
DEMAND_KEY = "demand_number"
DEMAND_TABLES = ("demands", "demand_year_plans", "demand_resource_plans", "demand_scores")
# Money tables: a row with a `currency` other than EUR holds its *_eur columns in that currency; they are converted to
# EUR when the dataset is built (the rate is kept in `fx_rate`, the source stays in its currency)
MONEY_TABLES = ("projects", "cost_plans", "benefit_plans", "benefit_actuals")
BASE_CURRENCY = "EUR"
# Phases of the stage-gate model, in order
PHASES = ("Initiation", "Planning", "Executing", "Closing", "Closed")
REQUIRED_TABLES = ("Projects", "Project_Tasks", "Resource_Plans", "Stakeholders", "Risks")

CLOSED_TASK_STATES = {"Closed Complete", "Closed Incomplete", "Closed Skipped", "Canceled", "Cancelled"}
CLOSED_PROJECT_STATES = {"Closed Complete", "Closed Incomplete", "Closed Skipped", "Canceled", "Cancelled"}
DONE_STORY_STATES = {"Complete", "Completed", "Closed"}
HIGH_IMPACT = {"High", "Very High", "Critical"}
RESOURCE_PLAN_COLUMNS = ("resource_plan_number", PK, "month", "assignment_group", "capacity_hours", "demand_hours",
                         "utilization_pct", "requested_fte", "allocated_fte")


def normalize_table(df: pd.DataFrame) -> pd.DataFrame:
    """Trims strings, maps empty strings to None, converts purely numeric columns to float and date fields to
    datetime. Makes the JSON and Excel sources identical."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_bool_dtype(s) or pd.api.types.is_numeric_dtype(s):
            continue
        values = s.astype(object).where(s.notna(), None)
        values = values.map(lambda v: (v.strip() or None) if isinstance(v, str) else v)
        non_null = [v for v in values if v is not None]
        if non_null and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
            df[col] = pd.to_numeric(values, errors="coerce").astype(float)
        else:
            df[col] = values
    for col in df.columns:
        if col.endswith("_date") or col == "month":
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def exchange_rates(df: pd.DataFrame, on: date | None = None) -> dict[str, float]:
    """Currency -> EUR rate valid on `on` (default today): the latest row whose valid_from is not after that day.
    EUR is always 1."""
    rates = {BASE_CURRENCY: 1.0}
    if df.empty or not {"currency", "rate_to_eur"} <= set(df.columns):
        return rates
    on = pd.Timestamp(on or clock.today())
    frame = df.assign(_rate=pd.to_numeric(df["rate_to_eur"], errors="coerce"),
                      _from=pd.to_datetime(df["valid_from"], errors="coerce") if "valid_from" in df.columns
                      else pd.Timestamp.min)
    frame = frame[frame["_rate"].notna() & (frame["_rate"] > 0) & (frame["_from"].isna() | (frame["_from"] <= on))]
    for currency, rows in frame.groupby(frame["currency"].astype(str).str.strip().str.upper()):
        rates[currency] = float(rows.sort_values("_from", na_position="first")["_rate"].iloc[-1])
    rates[BASE_CURRENCY] = 1.0
    return rates


def to_base_currency(df: pd.DataFrame, rates: dict[str, float]) -> pd.DataFrame:
    """Converts the *_eur columns of rows in another currency to EUR (rounded to cents) and records the rate in
    `fx_rate` (NaN = no rate known: the amounts stay unconverted and the Data Steward reports the row). A table
    without a `currency` column is returned unchanged."""
    if df.empty or "currency" not in df.columns:
        return df
    df = df.copy()
    currency = df["currency"].astype(object).where(df["currency"].notna(), BASE_CURRENCY).astype(str).str.strip().str.upper()
    currency = currency.where(currency != "", BASE_CURRENCY)
    df["currency"] = currency
    df["fx_rate"] = currency.map(rates).astype(float)
    convert = df["fx_rate"].notna() & (df["fx_rate"] != 1.0)
    if convert.any():
        for col in [c for c in df.columns if c.endswith("_eur")]:
            values = pd.to_numeric(df[col], errors="coerce")
            converted = values.where(~convert, (values * df["fx_rate"]).round(2))
            if pd.api.types.is_numeric_dtype(df[col]):
                df[col] = converted
            else:  # text in a money column stays as it is (the Data Steward reports it)
                df[col] = df[col].astype(object)
                df.loc[convert & values.notna(), col] = converted[convert & values.notna()]
    return df


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


@dataclass
class PortfolioDataset:
    projects: pd.DataFrame
    tasks: pd.DataFrame = field(default_factory=pd.DataFrame)
    resource_plans: pd.DataFrame = field(default_factory=pd.DataFrame)
    stakeholders: pd.DataFrame = field(default_factory=pd.DataFrame)
    risks: pd.DataFrame = field(default_factory=pd.DataFrame)
    stories: pd.DataFrame = field(default_factory=pd.DataFrame)
    dependencies: pd.DataFrame = field(default_factory=pd.DataFrame)
    cost_plans: pd.DataFrame = field(default_factory=pd.DataFrame)
    benefit_plans: pd.DataFrame = field(default_factory=pd.DataFrame)
    status_reports: pd.DataFrame = field(default_factory=pd.DataFrame)
    baselines: pd.DataFrame = field(default_factory=pd.DataFrame)
    approvals: pd.DataFrame = field(default_factory=pd.DataFrame)
    reserve_ledger: pd.DataFrame = field(default_factory=pd.DataFrame)
    team_skills: pd.DataFrame = field(default_factory=pd.DataFrame)
    skill_demand: pd.DataFrame = field(default_factory=pd.DataFrame)
    benefit_actuals: pd.DataFrame = field(default_factory=pd.DataFrame)
    exchange_rates: pd.DataFrame = field(default_factory=pd.DataFrame)
    strategic_goals: pd.DataFrame = field(default_factory=pd.DataFrame)
    project_goals: pd.DataFrame = field(default_factory=pd.DataFrame)
    demands: pd.DataFrame = field(default_factory=pd.DataFrame)
    demand_year_plans: pd.DataFrame = field(default_factory=pd.DataFrame)
    demand_resource_plans: pd.DataFrame = field(default_factory=pd.DataFrame)
    scoring_model: pd.DataFrame = field(default_factory=pd.DataFrame)
    demand_scores: pd.DataFrame = field(default_factory=pd.DataFrame)
    budget_envelopes: pd.DataFrame = field(default_factory=pd.DataFrame)
    capacity_limits: pd.DataFrame = field(default_factory=pd.DataFrame)
    planning_scenarios: pd.DataFrame = field(default_factory=pd.DataFrame)
    planning_items: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_tables(cls, tables: dict[str, pd.DataFrame], metadata: dict[str, Any] | None = None) -> "PortfolioDataset":
        kwargs = {}
        for attr, name in TABLES.items():
            df = tables.get(name)
            kwargs[attr] = normalize_table(df) if df is not None else pd.DataFrame()
        rates = exchange_rates(kwargs["exchange_rates"])
        for attr in MONEY_TABLES:
            kwargs[attr] = to_base_currency(kwargs[attr], rates)
        return cls(**kwargs, metadata=metadata or {})

    def table(self, name: str) -> pd.DataFrame:
        attr = next((a for a, n in TABLES.items() if n == name), name)
        return getattr(self, attr)

    def copy(self) -> "PortfolioDataset":
        return PortfolioDataset(
            **{f.name: (getattr(self, f.name).copy() if f.name != "metadata" else dict(self.metadata)) for f in fields(self)}
        )

    @property
    def project_numbers(self) -> list[str]:
        if self.projects.empty or PK not in self.projects.columns:
            return []
        return self.projects[PK].dropna().astype(str).tolist()

    @property
    def demand_numbers(self) -> list[str]:
        if self.demands.empty or DEMAND_KEY not in self.demands.columns:
            return []
        return self.demands[DEMAND_KEY].dropna().astype(str).tolist()

    def filter_projects(self, project_numbers: Iterable[str],
                        demand_numbers: Iterable[str] | None = None) -> "PortfolioDataset":
        """Row-level filter across all tables. Dependencies are kept if one side is visible, team-level tables
        always. The management reserve is a portfolio figure: its total movement is kept in the metadata, so the
        balance is right in every view while the ledger rows of other projects stay hidden. Demand tables keep the
        rows of `demand_numbers` (none when not given)."""
        keep = {str(p) for p in project_numbers}
        keep_demands = {str(d) for d in demand_numbers} if demand_numbers is not None else set()
        out = {}
        metadata = dict(self.metadata)
        metadata.setdefault("reserve_movement_eur", self.reserve_movement())
        for attr in TABLES:
            df = getattr(self, attr)
            if attr in GLOBAL_TABLES:
                out[attr] = df.copy()
            elif attr == "planning_items":  # a decision on a visible demand or project
                visible = keep | keep_demands
                out[attr] = df[df["item_number"].astype(str).isin(visible)].copy() \
                    if not df.empty and "item_number" in df.columns else df.iloc[0:0].copy()
            elif attr in DEMAND_TABLES:
                out[attr] = df[df[DEMAND_KEY].astype(str).isin(keep_demands)].copy() \
                    if not df.empty and DEMAND_KEY in df.columns else df.iloc[0:0].copy()
            elif df.empty:
                out[attr] = df.copy()
            elif PK in df.columns:
                out[attr] = df[df[PK].astype(str).isin(keep)].copy()
            elif attr == "dependencies" and {"predecessor_project_number", "successor_project_number"} <= set(df.columns):
                mask = df["predecessor_project_number"].astype(str).isin(keep) | df["successor_project_number"].astype(str).isin(keep)
                out[attr] = df[mask].copy()
            else:
                out[attr] = df.iloc[0:0].copy()
        return PortfolioDataset(**out, metadata=metadata)

    def reserve_movement(self) -> float:
        """Net movement of the management reserve over all ledger rows (draws negative, returns positive); the value
        kept in the metadata wins, so a filtered view reports the portfolio total."""
        if "reserve_movement_eur" in self.metadata:
            return float(self.metadata["reserve_movement_eur"])
        rl = self.reserve_ledger
        if rl.empty or "amount_eur" not in rl.columns:
            return 0.0
        return float(_num(rl["amount_eur"]).fillna(0).sum())

    # ------------------------------------------------------------------ joins / aggregates
    def reference_month(self, as_of: date | None = None) -> pd.Timestamp:
        return pd.Timestamp(as_of or clock.today()).to_period("M").to_timestamp()

    def current_allocation(self, as_of: date | None = None) -> pd.DataFrame:
        """Resource plan rows of the reference month per project (latest month <= today, otherwise earliest).
        Without resource plans: an empty frame that keeps the resource-plan columns, so callers can filter safely."""
        rp = self.resource_plans
        if rp.empty or not {PK, "month"} <= set(rp.columns):
            return pd.DataFrame({c: pd.Series(dtype=object) for c in RESOURCE_PLAN_COLUMNS})
        month = self.reference_month(as_of)
        past = rp[rp["month"] <= month].groupby(PK)["month"].max()
        first = rp.groupby(PK)["month"].min()
        ref = past.reindex(first.index).fillna(first).rename("ref_month")
        cur = rp.merge(ref, left_on=PK, right_index=True)
        return cur[cur["month"] == cur["ref_month"]].drop(columns="ref_month")

    def project_view(self, as_of: date | None = None) -> pd.DataFrame:
        """Project table enriched with metrics from tasks, resources, stories, risks and costs."""
        view = self.projects.copy()
        if view.empty:
            return view
        pid = view[PK].astype(str)

        def attach(series: pd.Series, name: str, default: float = 0.0) -> None:
            view[name] = pid.map(series).fillna(default) if series is not None and not series.empty else default

        rp = self.resource_plans
        if not rp.empty:
            rp = rp.assign(utilization_pct=_num(rp["utilization_pct"]), allocated_fte=_num(rp["allocated_fte"]))
            cur = self.current_allocation(as_of)
            attach(_num(cur["allocated_fte"]).groupby(cur[PK]).sum(), "current_fte")
            attach(rp.groupby(PK)["utilization_pct"].max(), "peak_utilization_pct")
            attach(rp[rp["utilization_pct"] > 100].groupby(PK)["assignment_group"].nunique(), "overloaded_teams")

        tk = self.tasks
        if not tk.empty:
            open_tk = tk[~tk["state"].isin(CLOSED_TASK_STATES)]
            remaining = _num(open_tk["planned_effort_hours"]).fillna(0) * (1 - _num(open_tk["percent_complete"]).fillna(0) / 100)
            attach(open_tk.groupby(PK).size(), "open_tasks")
            attach(remaining.groupby(open_tk[PK]).sum(), "remaining_task_hours")

        st = self.stories
        if not st.empty:
            sp = _num(st["story_points"]).fillna(0)
            done = st["state"].isin(DONE_STORY_STATES)
            attach(sp[~done].groupby(st.loc[~done, PK]).sum(), "remaining_story_points")
            attach(sp[done].groupby(st.loc[done, PK]).sum(), "completed_story_points")

        rk = self.risks
        if not rk.empty:
            attach(rk.groupby(PK).size(), "risk_count")
            high = rk[rk["impact"].isin(HIGH_IMPACT)]
            attach(high.groupby(PK).size(), "high_impact_risks")

        cp = self.cost_plans
        if not cp.empty:
            actual = _num(cp["actual_cost_eur"]).fillna(0)
            for ctype, col in (("CapEx", "actual_capex_eur"), ("OpEx", "actual_opex_eur")):
                m = cp["cost_type"] == ctype
                attach(actual[m].groupby(cp.loc[m, PK]).sum(), col)

        view["strategy_score"] = strategy_scores(view, self.strategic_goals, self.project_goals).to_numpy()
        return view


def strategy_scores(projects: pd.DataFrame, goals: pd.DataFrame, contributions: pd.DataFrame) -> pd.Series:
    """Strategic fit 0-100 per project (index aligned with `projects`): the weighted contribution (0-5) of the project
    to the strategic goals, relative to a full contribution to every goal. A project without any goal mapping falls
    back to its value_score (1-5) x 20; without either it scores 0. Goals with weight <= 0 are ignored."""
    fallback = (_num(projects["value_score"]) * 20 if "value_score" in projects.columns
                else pd.Series(float("nan"), index=projects.index)).clip(0, 100).fillna(0.0)
    if goals.empty or contributions.empty or not {"goal_id", "weight"} <= set(goals.columns) \
            or not {PK, "goal_id", "contribution"} <= set(contributions.columns):
        return fallback.astype(float)
    weights = pd.Series(_num(goals["weight"]).to_numpy(), index=goals["goal_id"].astype(str).str.strip())
    weights = weights[weights > 0]
    weights = weights[~weights.index.duplicated(keep="last")]
    total = float(weights.sum())
    if total <= 0:
        return fallback.astype(float)
    pg = contributions.assign(goal_id=contributions["goal_id"].astype(str).str.strip(),
                              contribution=_num(contributions["contribution"]).clip(0, 5).fillna(0))
    pg = pg[pg["goal_id"].isin(weights.index)]
    pg = pg.drop_duplicates([PK, "goal_id"], keep="last")
    points = (pg["contribution"] * pg["goal_id"].map(weights)).groupby(pg[PK].astype(str)).sum()
    score = projects[PK].astype(str).map(points / (total * 5) * 100)
    return score.round(1).where(score.notna(), fallback).astype(float)
