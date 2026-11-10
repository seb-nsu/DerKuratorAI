"""Capacity by skill: FTE are not interchangeable – bottlenecks appear per skill (e.g. SAP FI, Cloud Security), not
per head count.

Data: `Team_Skills` (assignment_group, skill, fte) gives each team's skill mix; `Skill_Demand` (project_number, month,
assignment_group, skill, demand_hours) the demand per project. The capacity of a skill in a team and month is the
team's capacity hours of that month (Resource_Plans) times the skill's share of the team's FTE. Computed in pandas,
not in the graph (keeps the graph dependency small, see docs/adr/ADR-001-graph-database.md).

`skill_capacity` is an analysis (no verdict); `skill_transfer` a scenario: it moves skill capacity from the team that
serves the source project to the team that serves the target project and reports utilisation per skill before and
after. Like a resource transfer it is executed by hand (resource management) – the source is not written.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from app.core.business_rules import current_rules
from app.crew.tools.context import (
    as_of,
    first_call,
    get_dataset,
    get_facts,
    get_table,
    record_change,
    record_fact,
    record_tool_result,
)
from app.crew.tools.simulator_tools import INPUT_PREFIX, INTERNAL_PREFIX, _num, _open_project, _safe
from app.llm.tools import tool

logger = logging.getLogger(__name__)
DEFAULT_HOURS_PER_FTE = 160.0  # only if the resource plans hold no capacity per FTE
SKILL_GAP = "Source project loses skill capacity it needs – check its schedule (may slip)."


def _analysis(fn):
    """Like _safe, but the result is an analysis (no simulation, so the verdict stays NO DECISION REQUIRED)."""
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            logger.exception("Skill analysis %s failed", fn.__name__)
            result = f"{INTERNAL_PREFIX} calculation defect in {fn.__name__} ({exc.__class__.__name__}: {exc})."
        record_tool_result("graph", fn.__name__, result)
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _months(horizon: int) -> list[pd.Timestamp]:
    start = get_dataset().reference_month(as_of())
    return [start + pd.DateOffset(months=i) for i in range(max(int(horizon), 1))]


def hours_per_fte() -> float:
    rp = get_dataset().resource_plans
    if rp.empty or not {"capacity_hours", "allocated_fte"} <= set(rp.columns):
        return DEFAULT_HOURS_PER_FTE
    fte = pd.to_numeric(rp["allocated_fte"], errors="coerce")
    cap = pd.to_numeric(rp["capacity_hours"], errors="coerce")
    ratio = (cap / fte.where(fte > 0)).dropna()
    return float(ratio.median()) if not ratio.empty else DEFAULT_HOURS_PER_FTE


def _skill_shares() -> pd.DataFrame:
    """assignment_group, skill, share of the team's FTE."""
    ts = get_table("Team_Skills")
    if ts.empty or not {"assignment_group", "skill", "fte"} <= set(ts.columns):
        return pd.DataFrame(columns=["assignment_group", "skill", "share"])
    ts = ts.assign(fte=pd.to_numeric(ts["fte"], errors="coerce").fillna(0))
    total = ts.groupby("assignment_group")["fte"].transform("sum")
    return ts.assign(share=ts["fte"] / total.where(total > 0)).fillna({"share": 0})[["assignment_group", "skill", "share"]]


def _moves() -> list[dict[str, Any]]:
    return [f for f in get_facts() if f.get("type") == "skill_move"]


def skill_table(months: list[pd.Timestamp]) -> pd.DataFrame:
    """Capacity, demand and utilisation per (team, skill, month) incl. the skill moves of this scenario."""
    shares = _skill_shares()
    rp = get_dataset().resource_plans
    cols = ["assignment_group", "skill", "month", "capacity_hours", "demand_hours"]
    if shares.empty or rp.empty:
        return pd.DataFrame(columns=[*cols, "utilisation_pct"])
    rp = rp[rp["month"].isin(months)]
    team_cap = rp.assign(cap=pd.to_numeric(rp["capacity_hours"], errors="coerce").fillna(0)) \
        .groupby(["assignment_group", "month"])["cap"].sum().reset_index()
    cap = team_cap.merge(shares, on="assignment_group")
    cap["capacity_hours"] = cap["cap"] * cap["share"]
    sd = get_table("Skill_Demand")
    if not sd.empty and {"month", "skill", "assignment_group", "demand_hours"} <= set(sd.columns):
        sd = sd[sd["month"].isin(months)]
        demand = sd.assign(demand_hours=pd.to_numeric(sd["demand_hours"], errors="coerce").fillna(0)) \
            .groupby(["assignment_group", "skill", "month"])["demand_hours"].sum().reset_index()
    else:
        demand = pd.DataFrame(columns=["assignment_group", "skill", "month", "demand_hours"])
    table = cap[["assignment_group", "skill", "month", "capacity_hours"]].merge(
        demand, on=["assignment_group", "skill", "month"], how="outer").fillna({"capacity_hours": 0, "demand_hours": 0})
    hpf = hours_per_fte()
    for move in _moves():
        for team, sign in ((move["from_team"], -1), (move["to_team"], 1)):
            mask = (table["assignment_group"] == team) & (table["skill"] == move["skill"]) & \
                   table["month"].isin(pd.to_datetime(move["months"]))
            if not mask.any() and sign > 0:  # a team that gains a skill it did not have
                extra = pd.DataFrame([{"assignment_group": team, "skill": move["skill"], "month": m, "capacity_hours": 0.0,
                                       "demand_hours": 0.0} for m in pd.to_datetime(move["months"])])
                table = pd.concat([table, extra], ignore_index=True)
                mask = (table["assignment_group"] == team) & (table["skill"] == move["skill"]) & \
                       table["month"].isin(pd.to_datetime(move["months"]))
            table.loc[mask, "capacity_hours"] = (table.loc[mask, "capacity_hours"] + sign * move["fte"] * hpf).clip(lower=0)
    table["utilisation_pct"] = table["demand_hours"] / table["capacity_hours"].where(table["capacity_hours"] > 0) * 100
    return table


def skill_utilisation(months: int = 6) -> pd.DataFrame:
    """Utilisation per skill and month across all teams (for the overview heatmap): skill, month, capacity, demand,
    utilisation_pct."""
    table = skill_table(_months(months))
    if table.empty:
        return table
    agg = table.groupby(["skill", "month"])[["capacity_hours", "demand_hours"]].sum().reset_index()
    agg["utilisation_pct"] = agg["demand_hours"] / agg["capacity_hours"].where(agg["capacity_hours"] > 0) * 100
    return agg


def _pct(value: float) -> str:
    return "∞" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value:.0f} %"


@_analysis
def skill_capacity(skill: str | None = None, months: int = 6) -> str:
    horizon = int(_num(months, 6) or 6)
    if horizon < 1 or horizon > 24:
        return f"{INPUT_PREFIX} months must be between 1 and 24."
    agg = skill_utilisation(horizon)
    if agg.empty:
        return "RESULT analyse_skill_capacity | No skill data (Team_Skills / Skill_Demand) recorded."
    if skill:
        known = sorted(agg["skill"].astype(str).unique())
        hit = [s for s in known if s.casefold() == str(skill).strip().casefold()]
        if not hit:
            return f"{INPUT_PREFIX} Skill '{skill}' not found. Known skills: {', '.join(known)}."
        agg = agg[agg["skill"] == hit[0]]
    limit = current_rules().team_capacity_limit_pct
    lines = [f"RESULT analyse_skill_capacity | {agg['skill'].nunique()} skill(s), next {horizon} month(s), limit {limit:g} %"]
    for name, rows in agg.groupby("skill"):
        cells = ", ".join(f"{m:%Y-%m} {_pct(u)}" for m, u in zip(rows["month"], rows["utilisation_pct"], strict=True))
        lines.append(f"- {name}: {cells}")
    over = agg[agg["utilisation_pct"] > limit].sort_values("utilisation_pct", ascending=False)
    if over.empty:
        lines.append("- No skill is over its capacity in this period.")
        return "\n".join(lines)
    sd = get_table("Skill_Demand")
    lines.append("- Top bottlenecks:")
    for row in over.head(5).to_dict(orient="records"):
        month_rows = sd[(sd["skill"] == row["skill"]) & (sd["month"] == row["month"])] if not sd.empty else sd
        projects = ", ".join(sorted(month_rows["project_number"].astype(str).unique())) or "–"
        lines.append(f"  - {row['skill']} {row['month']:%Y-%m}: {row['utilisation_pct']:.0f} % "
                     f"({row['demand_hours']:,.0f} h demand / {row['capacity_hours']:,.0f} h capacity) – projects {projects}")
    return "\n".join(lines)


def skill_bottleneck_lines(months: int = 3) -> list[str]:
    """Skill view for the team-bottleneck check after a resource transfer (empty without skill data)."""
    agg = skill_utilisation(months)
    if agg.empty:
        return []
    limit = current_rules().team_capacity_limit_pct
    over = agg[agg["utilisation_pct"] > limit].sort_values("utilisation_pct", ascending=False).head(3)
    if over.empty:
        return [f"- Skills: none over {limit:g} % in the next {months} months."]
    return ["- Skill bottlenecks (next months): " + "; ".join(
        f"{r['skill']} {r['month']:%Y-%m} {r['utilisation_pct']:.0f} %" for r in over.to_dict(orient="records"))]


def _serving_team(pid: str, skill: str) -> str | None:
    """The team that serves `skill` for the project: from its skill demand, else the first of its teams that has the
    skill, else its first team in the resource plans."""
    sd = get_table("Skill_Demand", pid)
    if not sd.empty and "skill" in sd.columns:
        teams = sd[sd["skill"].astype(str).str.casefold() == skill.casefold()]["assignment_group"].astype(str)
        if not teams.empty:
            return teams.value_counts().index[0]
    rp = get_table("Resource_Plans", pid)
    teams = list(dict.fromkeys(rp["assignment_group"].astype(str))) if not rp.empty else []
    shares = _skill_shares()
    with_skill = set(shares[shares["skill"].astype(str).str.casefold() == skill.casefold()]["assignment_group"])
    return next((t for t in teams if t in with_skill), teams[0] if teams else None)


@_safe
def skill_transfer(skill: str, fte: float, from_project: str, to_project: str, months: int = 3) -> str:
    src = _open_project(from_project, allow_closed=True)
    if isinstance(src, str):
        return src
    tgt = _open_project(to_project)
    if isinstance(tgt, str):
        return tgt
    if src["project_number"] == tgt["project_number"]:
        return f"{INPUT_PREFIX} Source and target are identical."
    amount = _num(fte, float("nan"))
    if math.isnan(amount) or amount <= 0:
        return f"{INPUT_PREFIX} fte must be positive."
    horizon = int(_num(months, 3) or 3)
    if horizon < 1 or horizon > 24:
        return f"{INPUT_PREFIX} months must be between 1 and 24."
    shares = _skill_shares()
    known = sorted(shares["skill"].astype(str).unique())
    name = next((s for s in known if s.casefold() == str(skill or "").strip().casefold()), None)
    if name is None:
        return f"{INPUT_PREFIX} Skill '{skill}' not found. Known skills: {', '.join(known) or 'none'}."
    sp, tp = str(src["project_number"]), str(tgt["project_number"])
    from_team, to_team = _serving_team(sp, name), _serving_team(tp, name)
    if from_team is None:
        return f"{INPUT_PREFIX} No team serves {name} for {sp}."
    if to_team is None:
        return f"{INPUT_PREFIX} {tp} has no team in the resource plans."
    window = _months(horizon)
    before = skill_table(window)
    team_skill = before[(before["assignment_group"] == from_team) & (before["skill"] == name)]
    available = float(team_skill["capacity_hours"].min()) / hours_per_fte() if not team_skill.empty else 0.0
    if amount > available + 1e-9:
        return (f"ERROR: Insufficient skill capacity – {from_team} only has {available:.1f} FTE of {name} "
                f"in the period, requested: {amount:g}.")

    record_change(sp, "skill_fte", round(-amount, 2), "simulate_skill_transfer", f"{name}: {from_team} -> {tp}",
                  table="Skill_Demand", scenario_field="skill_fte_moved")
    record_change(tp, "skill_fte", round(amount, 2), "simulate_skill_transfer", f"{name}: {from_team} -> {to_team}",
                  table="Skill_Demand", scenario_field="skill_fte_moved")
    previous = next((f for f in _moves() if f.get("project") == sp and f.get("successor") == f"{tp}|{name}"), None)
    total = amount + (previous["fte"] if previous else 0)
    record_fact("skill_move", project=sp, successor=f"{tp}|{name}", skill=name, fte=round(total, 2),
                from_team=from_team, to_team=to_team, months=[m.date().isoformat() for m in window])
    after = skill_table(window)
    rules = current_rules()
    lines = [f"RESULT simulate_skill_transfer | {amount:g} FTE {name} from {sp} ({from_team}) to {tp} ({to_team}) "
             f"for {horizon} month(s)"]
    if from_team == to_team:
        lines.append(f"- Same team ({from_team}): the skill load of the team does not change, only the allocation "
                     "between the projects.")
    for label, team in (("source", from_team), ("target", to_team)):
        if label == "target" and team == from_team:
            break
        old = before[(before["assignment_group"] == team) & (before["skill"] == name)].set_index("month")
        new = after[(after["assignment_group"] == team) & (after["skill"] == name)].set_index("month")
        cells = ", ".join(f"{m:%Y-%m} {_pct(old['utilisation_pct'].get(m, float('nan')))} -> "
                          f"{_pct(new['utilisation_pct'].get(m, float('nan')))}" for m in window)
        lines.append(f"- {name} in {team} ({label}): {cells}")
        if label == "source":
            peak = new["utilisation_pct"].replace([float("inf")], float("nan")).max() if not new.empty else 0.0
            unbounded = not new.empty and bool((new["capacity_hours"] <= 0).any() and (new["demand_hours"] > 0).any())
            if unbounded or (peak and peak > rules.overload_alert_pct):
                lines.append(f"  ⚠ {name} in {team} becomes overloaded (> {rules.overload_alert_pct:g} %) – "
                             f"{sp} loses the skill it needs.")
                record_fact("overload", project=sp, skill=name, utilisation_pct=None if unbounded else round(peak, 1))
            elif peak and peak > rules.team_capacity_limit_pct:
                lines.append(f"  ⚠ {SKILL_GAP}")
                record_fact("skill_gap", project=sp, warning=SKILL_GAP)
    lines.append("ℹ️ Skill allocations are not changed automatically – execute via Resource Management.")
    return "\n".join(lines)


# =========================================================================== agent tools
@tool("analyse_skill_capacity")
def analyse_skill_capacity(skill: str = "", months: int = 6) -> str:
    """Capacity, demand and utilisation per SKILL (not per head) for the next `months` months, with the top
    bottlenecks and the projects that need the skill. skill empty = all skills. Analysis only."""
    return skill_capacity(skill or None, months)


@tool("simulate_skill_transfer")
def simulate_skill_transfer(skill: str, fte: float, from_project: str, to_project: str, months: int = 3) -> str:
    """Moves `fte` FTE of one skill (e.g. 'SAP FI') from the team serving from_project to the team serving
    to_project for `months` months and reports the skill utilisation before and after (overload above the alert)."""
    if not first_call("simulate_skill_transfer", f"{from_project}|{skill}"):
        return "SKIPPED: simulate_skill_transfer already ran for this source and skill in this step."
    return skill_transfer(skill, fte, from_project, to_project, months)


SKILL_TOOLS = [analyse_skill_capacity, simulate_skill_transfer]
