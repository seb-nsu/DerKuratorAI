"""What-if comparison: budget impact of a change set, the key figures of a saved what-if, the A/B difference matrix and
the two charts of the page. Shared by the Streamlit page (app/ui/executive.py) and the HTTP API."""
from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from app.core.formatting import eur, format_value
from app.views.palette import SERIES_COLOR, STATUS_COLORS

STOPPED_STATES = ("Canceled", "Cancelled")


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def budget_impact(changes: list[dict[str, Any]], projects: pd.DataFrame) -> dict[str, float]:
    """Budget effect per project of a change set: budget changes (first old to last new value) and project stops
    (remaining budget released = negative). On hold defers spend and has no budget effect."""
    current = projects.assign(project_number=projects["project_number"].astype(str)).set_index("project_number") \
        if not projects.empty else pd.DataFrame()
    first_old: dict[str, float] = {}
    last_new: dict[str, float] = {}
    stopped: set[str] = set()
    for c in changes or []:
        pid = str(c.get("project_id"))
        if c.get("field") == "budget_cost_eur" and _num(c.get("new")) is not None:
            first_old.setdefault(pid, _num(c.get("old")) or 0.0)
            last_new[pid] = _num(c.get("new"))
        elif c.get("field") == "state" and str(c.get("new")) in STOPPED_STATES:
            stopped.add(pid)
    impact: dict[str, float] = {}
    for pid in set(first_old) | stopped:
        base = first_old.get(pid)
        if base is None and pid in current.index:
            base = _num(current.at[pid, "budget_cost_eur"]) or 0.0
        if pid in stopped:
            actual = _num(current.at[pid, "actual_cost_eur"]) if pid in current.index else 0.0
            impact[pid] = -max((base or 0.0) - (actual or 0.0), 0.0)
        else:
            impact[pid] = last_new[pid] - (base or 0.0)
    return impact


def scenario_figures(s: dict[str, Any]) -> dict[str, Any]:
    """Key figures of a saved what-if as numbers (the page formats them)."""
    changes, impact = s.get("changes") or [], s.get("budget_impact") or {}
    fte = [c for c in changes if c.get("field") == "allocated_fte"]
    return {
        "verdict": (s.get("verdict") or {}).get("label"),
        "net_budget_impact": sum(impact.values()),
        "budget_released": -sum(v for v in impact.values() if v < 0),
        "budget_added": sum(v for v in impact.values() if v > 0),
        "projects_stopped": sum(1 for c in changes if c.get("field") == "state" and str(c.get("new")) in STOPPED_STATES),
        "projects_on_hold": sum(1 for c in changes if c.get("field") == "state" and str(c.get("new")) == "On Hold"),
        "fte_moved": sum(max((_num(c.get("new")) or 0) - (_num(c.get("old")) or 0), 0) for c in fte),
        "changes": len(changes),
        "steps": len(s.get("turns") or []),
    }


def compare_frame(a: dict[str, Any], b: dict[str, Any]) -> pd.DataFrame:
    """Project × field matrix of the final values in A and B (differences flagged)."""
    def finals(s):
        out: dict[tuple[str, str], Any] = {}
        for c in s.get("changes") or []:
            out[(str(c.get("project_id")), str(c.get("field")))] = c.get("new")
        return out

    fa, fb = finals(a), finals(b)
    rows = [{"project": p, "field": f, "A": format_value(f, fa[(p, f)]) if (p, f) in fa else "–",
             "B": format_value(f, fb[(p, f)]) if (p, f) in fb else "–",
             "differs": "≠" if str(fa.get((p, f), "–")) != str(fb.get((p, f), "–")) else ""}
            for p, f in sorted(set(fa) | set(fb))]
    return pd.DataFrame(rows, columns=["project", "field", "A", "B", "differs"])


def waterfall_figure(s: dict[str, Any], title: str):
    impact = s.get("budget_impact") or {}
    if not impact:
        return None
    items = sorted(impact.items(), key=lambda kv: kv[1])
    labels = [pid for pid, _ in items] + ["Net impact"]
    values = [v for _, v in items] + [sum(impact.values())]
    fig = go.Figure(go.Waterfall(
        x=labels, y=values, measure=["relative"] * len(items) + ["total"],
        text=[eur(v, signed=True) for v in values], textposition="outside", cliponaxis=False,
        decreasing={"marker": {"color": STATUS_COLORS["Green"]}}, increasing={"marker": {"color": STATUS_COLORS["Red"]}},
        totals={"marker": {"color": SERIES_COLOR}}, connector={"line": {"color": "#8a8a86", "dash": "dot"}},
    ))
    fig.update_layout(title=dict(text=title, font=dict(size=14)), height=340, margin=dict(l=10, r=10, t=40, b=10),
                      yaxis=dict(title="Budget impact (€)", showgrid=False), showlegend=False)
    return fig


def impact_bars(a: dict[str, Any], b: dict[str, Any]) -> pd.DataFrame:
    """Budget impact per project of A and B side by side (0 where one of them leaves the project's budget alone)."""
    projects = sorted(set(a.get("budget_impact") or {}) | set(b.get("budget_impact") or {}))
    return pd.DataFrame([{"project": p, "scenario": tag, "impact": (s.get("budget_impact") or {}).get(p, 0.0)}
                         for p in projects for s, tag in ((a, "A"), (b, "B"))], columns=["project", "scenario", "impact"])


def impact_bars_figure(bars: pd.DataFrame):
    if bars.empty:
        return None
    fig = px.bar(bars, x="project", y="impact", color="scenario", barmode="group",
                 color_discrete_map={"A": SERIES_COLOR, "B": "#1F3A5F"}, text_auto=".3s",
                 labels={"impact": "Budget impact (€)", "project": ""})
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.1))
    return fig


def in_scope(s: dict[str, Any], visible: set[str]) -> dict[str, Any]:
    """A saved what-if limited to the reader's current scope: changes and budget effects of projects outside it are
    left out (no id, no count); `restricted` says that something was left out."""
    changes = [c for c in s.get("changes") or [] if str(c.get("project_id")) in visible]
    impact = {k: v for k, v in (s.get("budget_impact") or {}).items() if str(k) in visible}
    restricted = len(changes) != len(s.get("changes") or []) or len(impact) != len(s.get("budget_impact") or {})
    return {**s, "changes": changes, "budget_impact": impact, "restricted": restricted}
