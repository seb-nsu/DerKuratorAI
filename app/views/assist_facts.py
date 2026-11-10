"""The facts the page assistant answers from, written by the code from what the page shows the reader.

Every builder reads only the reader's own (scope-filtered) dataset and the frames the page itself renders, so the
assistant never sees more than the reader: a dependency outside the scope stays the neutral "restricted" row, a Project
Manager's demand facts carry no rank and no envelope of a foreign area. Plain text, one fact per line – no tables
library needed."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from app.core.formatting import field_label, format_value
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.views.data_quality import rule_label

MAX_ROWS = 25


_FIELD_RE = re.compile(r"[a-z][a-z0-9_]*")
_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]00:00:00$")


def _label(key: Any) -> str:
    """A data field name as its business label ("status_date" -> "Status date"); other keys unchanged."""
    text = str(key)
    return field_label(text) if _FIELD_RE.fullmatch(text) and "_" in text else text


def _cell(value: Any, key: Any = "") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return "–"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d") if value == value.normalize() else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and re.search(r"_eur$|_pct$|fte", str(key)):
        return format_value(str(key), value)  # € / % / FTE fields; years, counts and scores stay plain
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    text = str(value).replace("\n", " ").strip()
    text = _MIDNIGHT_RE.sub(r"\1", text)
    return text[:200] + ("…" if len(text) > 200 else "") if text and text.lower() not in ("nan", "nat", "none") else "–"


def table(frame: pd.DataFrame | None, title: str, limit: int = MAX_ROWS) -> str:
    """'## title' plus one 'col: value · col: value' line per row (at most `limit`, the rest counted)."""
    if frame is None or frame.empty:
        return f"## {title}\nnone"
    lines = [f"## {title}"]
    for row in frame.head(limit).to_dict(orient="records"):
        lines.append("- " + " · ".join(f"{_label(k)}: {_cell(v, k)}" for k, v in row.items()))
    if len(frame) > limit:
        lines.append(f"- … {len(frame) - limit} more row(s) not listed")
    return "\n".join(lines)


def section(title: str, pairs: dict[str, Any]) -> str:
    return f"## {title}\n" + "\n".join(f"- {_label(k)}: {_cell(v, k)}" for k, v in pairs.items())


def join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- portfolio overview
def overview_facts(view: pd.DataFrame, ds: PortfolioDataset, teams: pd.DataFrame, steward) -> str:
    projects = view if view is not None else pd.DataFrame()
    num = lambda c: pd.to_numeric(projects.get(c, pd.Series(dtype=float)), errors="coerce")  # noqa: E731
    budget, eac = num("budget_cost_eur"), num("estimate_at_completion_eur")
    overrun = projects.assign(_over=(eac - budget)).sort_values("_over", ascending=False)
    cols = [c for c in ("project_number", "project_name", "status", "state", "budget_cost_eur",
                        "estimate_at_completion_eur", "percent_complete", "planned_end_date") if c in projects.columns]
    head = section("Portfolio in your scope", {
        "Projects": len(projects), "Total budget": budget.sum(), "Total EAC": eac.sum(),
        "Projects with EAC above budget": int((eac > budget).sum()),
        "Status red": int((projects.get("status", pd.Series(dtype=str)).astype(str).str.lower() == "red").sum()),
        "Data quality index %": getattr(steward, "quality_index", None),
        "Projects blocked for simulation": len(getattr(steward, "blocked_project_ids", []) or [])})
    states = projects["state"].value_counts().rename_axis("State").reset_index(name="Projects") \
        if "state" in projects.columns else None
    return join(head, table(states, "Projects per state"),
                table(overrun[overrun["_over"] > 0][cols], "Largest EAC overruns", 10) if cols else "",
                table(projects[cols], "All projects", 40) if cols else "",
                table(teams, "Teams above 100 % utilisation", 10))


def data_quality_facts(steward) -> str:
    issues = sorted(getattr(steward, "issues", []), key=lambda i: (i.severity != "ERROR", i.project_id, i.rule))
    frame = pd.DataFrame([{"Severity": i.severity, "Project": i.project_id, "Rule": rule_label(i.rule), "Field": i.column,
                           "Finding": i.message} for i in issues])
    rules = frame.groupby(["Severity", "Rule"]).size().reset_index(name="Findings") if not frame.empty else None
    return join(section("Data quality", {
        "Projects checked": getattr(steward, "total_rows", None),
        "Data quality index % (share without ERROR)": getattr(steward, "quality_index", None),
        "Required-field completeness %": getattr(steward, "completeness_pct", None),
        "Blocked for simulation": ", ".join(getattr(steward, "blocked_project_ids", []) or []) or "none"}),
        table(rules, "Findings per rule"), table(frame, "Findings", 60))


# --------------------------------------------------------------------------- running project
def project_facts(pid: str, project: dict[str, Any], ds: PortfolioDataset, issues: list) -> str:
    from app.views.projects import (
        baseline_frame,
        cockpit_cost_curve,
        cockpit_deadlines,
        cockpit_dependencies,
        risk_exposure_frame,
    )

    keys = ["project_name", "state", "status", "phase", "execution_type", "business_unit", "portfolio",
            "budget_cost_eur", "actual_cost_eur", "estimate_at_completion_eur", "percent_complete",
            "planned_start_date", "planned_end_date", "approved_end_date", "allocated_fte", "assignment_group",
            "expected_roi_pct", "strategic_goal"]
    head = section(f"Project {pid}", {k: project.get(k) for k in keys if k in project})
    case = str(project.get("business_case") or "").strip()
    sr = ds.status_reports
    if not sr.empty and "project_number" in sr.columns:
        sr = sr[sr["project_number"].astype(str) == pid]
        cols = [c for c in ("status_date", "overall_health", "schedule", "cost", "scope", "resources",
                            "issues_decisions") if c in sr.columns]
        sr = sr.sort_values("status_date", ascending=False)[cols].head(4) if cols else sr.head(0)
    curve = cockpit_cost_curve(pid, ds)
    cost = ""
    if not curve.empty:
        last_actual = curve.dropna(subset=["Actual (cumulative)"]).tail(1)
        cost = section("Cost curve", {
            "Planned total": curve["Planned (cumulative)"].iloc[-1],
            "Actual to date": last_actual["Actual (cumulative)"].iloc[0] if not last_actual.empty else None,
            "Planned by the last actual month": last_actual["Planned (cumulative)"].iloc[0]
            if not last_actual.empty else None,
            "Last actual month": last_actual["month"].iloc[0] if not last_actual.empty else None})
    incoming, outgoing = cockpit_dependencies(pid, ds)
    rk = ds.risks
    risks = risk_exposure_frame(rk[rk["project_number"].astype(str) == pid]) if not rk.empty else rk
    risk_cols = [c for c in ("risk_number", "short_description", "impact", "probability", "exposure_eur", "risk_status",
                             "due_date", "mitigation") if c in risks.columns]
    plans = ds.benefit_plans
    if not plans.empty and "project_number" in plans.columns:
        plans = plans[plans["project_number"].astype(str) == pid]
    dq = pd.DataFrame([{"Severity": i.severity, "Rule": rule_label(i.rule), "Field": i.column, "Finding": i.message}
                       for i in issues])
    return join(head, f"## Business case\n{case[:1500] or 'none recorded'}",
                table(sr, "Latest status reports"), cost,
                table(cockpit_deadlines(pid, project, ds), "Deadlines (days = days left from today)"),
                table(incoming, "Waits for (incoming dependencies)"),
                table(outgoing, "Is needed by (outgoing dependencies)"),
                table(risks[risk_cols] if risk_cols else risks, "Risks, highest exposure first", 10),
                table(plans, "Benefit plans", 10), table(baseline_frame(pid, project, ds), "Baselines"),
                table(dq, "Data quality findings"))


# --------------------------------------------------------------------------- reports and proposals
def report_facts(message: dict[str, Any], data: dict[str, Any], changes_frame: pd.DataFrame | None = None) -> str:
    meta = message.get("meta") or {}
    verdict = meta.get("verdict") if isinstance(meta.get("verdict"), dict) else {}
    metrics = {k: v for k, v in (data.get("metrics") or {}).items() if not isinstance(v, dict | list | set)}
    paper = str(message.get("content") or "")
    return join(section("Report", {"Request": data.get("title"), "Rule-based verdict": data.get("verdict"),
                                   "As of": verdict.get("as_of"), "Forced simulation": bool(meta.get("forced"))}),
                "## Why this verdict (rule-based)\n" + ("\n".join(f"- {r}" for r in data.get("reasons") or []) or "none"),
                "## Conditions\n" + ("\n".join(f"- {c}" for c in data.get("conditions") or []) or "none"),
                "## Flags\n" + ("\n".join(f"- {f}" for f in data.get("flags") or []) or "none"),
                section("Key metrics", metrics) if metrics else "",
                table(changes_frame, "Proposed changes", 40),
                section("Thresholds applied", verdict.get("rules") or {}) if verdict.get("rules") else "",
                f"## Decision paper (text)\n{paper[:9000]}")


def scenario_compare_facts(a: dict[str, Any], b: dict[str, Any], summary: pd.DataFrame, diff: pd.DataFrame) -> str:
    def conditions(s):
        return "\n".join(f"- {c}" for c in (s.get("verdict") or {}).get("conditions", [])) or "none"

    return join(table(summary, "Summary A vs B", 20),
                f"## Conditions of A ({a.get('name')})\n{conditions(a)}",
                f"## Conditions of B ({b.get('name')})\n{conditions(b)}",
                "## Steps\n- A: " + (" → ".join(a.get("turns") or []) or "–") + "\n- B: "
                + (" → ".join(b.get("turns") or []) or "–"),
                table(diff, "Change-by-change differences", 40))


def compare_facts(a: dict[str, Any], b: dict[str, Any]) -> str:
    """The A/B comparison of two saved what-ifs (already limited to the reader's scope), as the page shows it."""
    from app.core.formatting import eur
    from app.views.whatifs import compare_frame, scenario_figures

    def figures(s: dict[str, Any]) -> list[str]:
        f = scenario_figures(s)
        return [str(f["verdict"] or "–"), eur(f["net_budget_impact"], signed=True), eur(f["budget_released"]),
                eur(f["budget_added"]), str(f["projects_stopped"]), str(f["projects_on_hold"]), f"{f['fte_moved']:g}",
                str(f["changes"]), str(f["steps"])]

    summary = pd.DataFrame({"Metric": ["Verdict", "Net budget impact", "Budget released", "Budget added",
                                       "Projects stopped", "Projects on hold", "FTE moved", "Changes", "Steps"],
                            f"A – {a['name']}": figures(a), f"B – {b['name']}": figures(b)})
    diff = compare_frame(a, b)
    diff["field"] = diff["field"].map(field_label)  # as the page's table shows it
    return scenario_compare_facts(a, b, summary, diff)


# --------------------------------------------------------------------------- planning
def demands_facts(ds: PortfolioDataset, table_frame: pd.DataFrame, issues: list) -> str:
    dq = pd.DataFrame([{"Demand": i.project_id, "Severity": i.severity, "Field": i.column, "Finding": i.message}
                       for i in issues])
    return join(table(table_frame, "Demands in your scope", 40), table(dq, "Data quality of the demands", 20))


def demand_facts(ds: PortfolioDataset, demand: str, plan, full_view: bool) -> str:
    """One demand: master data, money, people, score, its dependency chain and what a delay would push along – and,
    for the PMO and the Board (`full_view`), its plan decision and its place in the budget envelopes and team limits."""
    from app.planning import analysis
    from app.planning.model import resource_plans, year_plans
    from app.planning.scoring import score_breakdown

    rows = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand]
    if rows.empty:
        return f"## Demand {demand}\nnot in your scope"
    row = rows.iloc[0].to_dict()
    keys = ["demand_name", "demand_status", "demand_type", "mandatory_reason", "business_unit", "portfolio",
            "demand_owner", "duration_months", "earliest_start_date", "latest_start_date", "net_present_value_eur",
            "run_cost_after_go_live_eur", "tshirt_size", "strategic_goal", "business_capability",
            "project_summary", "business_case"]
    money = year_plans(ds)
    money = money[money[DEMAND_KEY] == demand]
    people = resource_plans(ds)
    people = people[people[DEMAND_KEY] == demand]
    parts = score_breakdown(ds)
    parts = parts[parts[DEMAND_KEY].astype(str) == demand]
    out = [section(f"Demand {demand}", {k: row.get(k) for k in keys if k in row}),
           table(money.drop(columns=[DEMAND_KEY], errors="ignore"), "Money per quarter", 20),
           table(people.drop(columns=[DEMAND_KEY], errors="ignore"), "FTE per team and quarter", 20),
           table(parts.drop(columns=[DEMAND_KEY], errors="ignore"), "Assessment (score parts)", 10)]
    if full_view and plan is not None and not plan.items.empty:
        item = plan.items[plan.items[DEMAND_KEY] == demand]
        out.append(table(item, "Decision in the base plan (rank order, today's data)"))
    out.append(table(analysis.dependency_chain(ds, demand, plan if full_view else None),
                     "Dependency chain (Steps = distance)"))
    out.append(table(analysis.slip_impact(ds, demand, 1, plan if full_view else None),
                     "If this demand slips by one quarter"))
    if analysis.hidden_links(ds, demand):
        out.append("## Note\n- Further dependencies lie outside the reader's scope (not named); the PMO coordinates.")
    if full_view and plan is not None:
        out.append(table(analysis.budget_position(ds, demand, plan), "Budget envelopes of its area (base plan)", 20))
        out.append(table(analysis.capacity_position(ds, demand, plan), "Team capacity (base plan)", 20))
    return join(*out)


def plan_facts(ds: PortfolioDataset, result, label: str = "Base plan") -> str:
    from app.planning import analysis

    v = result.verdict
    items = result.items
    counts = items["decision"].value_counts().rename_axis("Decision").reset_index(name="Demands") \
        if not items.empty else None
    budget_hot, capacity_hot = analysis.hot_spots(result)
    return join(section(label, {"Planning years": ", ".join(map(str, result.years)),
                                "Rule-based planning verdict": v.label, "Scenario": result.input.name,
                                "Method": getattr(result.input, "method", None),
                                "Budget factor %": getattr(result.input, "budget_factor_pct", None),
                                "As of": result.as_of}),
                "## Why this verdict (rule-based)\n" + ("\n".join(f"- {r}" for r in getattr(v, "reasons", []) or [])
                                                       or "none"),
                "## Conditions\n" + ("\n".join(f"- {c}" for c in v.conditions) or "none"),
                table(counts, "Demands per decision"), table(items, "Decision per demand", 40),
                table(budget_hot, f"Budget envelopes used to {analysis.HOT_SHARE:.0f} % or more", 15),
                table(capacity_hot, f"Team quarters used to {analysis.HOT_SHARE:.0f} % or more", 15),
                table(analysis.critical_chains(ds, result), "Demands others wait for (critical chains)", 10),
                table(pd.DataFrame(result.facts), "Verdict facts", 20))


def scenario_facts(ds: PortfolioDataset, result, results: list) -> str:
    """One calculated plan scenario and, with more than one, the comparison of all calculated scenarios."""
    from app.planning import store

    text = plan_facts(ds, result, f"Scenario “{result.input.name}”")
    if len(results) > 1:
        text += "\n\n" + table(store.comparison(list(results)), "Comparison of the calculated scenarios")
    return text
