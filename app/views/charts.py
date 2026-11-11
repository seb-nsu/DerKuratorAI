"""Plotly figures of the HTTP API (the API returns them as figure JSON, so every front
end draws the same chart): the portfolio risk/value matrix, overloaded teams, the skill heatmap, a report's dependency
graph and its before/after deltas. Status colours are fixed and always paired with a shape and a word."""
from __future__ import annotations

import html
import json
from typing import Any

import pandas as pd
import plotly.express as px

from app.core.formatting import eur_m
from app.core.runtime_config import get_runtime_config
from app.data_adapters.dataset import PortfolioDataset
from app.views.exports import RESTRICTED_NODE
from app.views.palette import SERIES_COLOR, STATUS_COLORS, STATUS_SYMBOLS

# Status palette (fixed, never themed) – always paired with icon + label, never colour alone
BEFORE_COLOR = "#8a8a86"
RISKY_DEPENDENCY_STATES = {"at risk", "blocked", "late", "delayed"}


def figure_json(fig) -> dict[str, Any] | None:
    """A figure as Plotly JSON for the API (None without a figure)."""
    return json.loads(fig.to_json()) if fig is not None else None


# =========================================================================== portfolio overview
def bubble_chart(view: pd.DataFrame):
    data = view.dropna(subset=["risk_score", "planned_roi_pct", "budget_cost_eur"]).copy()
    if data.empty:
        return None
    data["budget_label"] = (data["budget_cost_eur"] / 1e6).map(lambda v: f"€{v:,.2f} M")
    # place projects with the same risk score side by side so bubbles don't hide each other
    data = data.sort_values(["risk_score", "planned_roi_pct"])
    rank = data.groupby("risk_score").cumcount()
    size = data.groupby("risk_score")["risk_score"].transform("size")
    data["risk_x"] = data["risk_score"] + (rank - (size - 1) / 2) * 0.22
    fig = px.scatter(
        data, x="risk_x", y="planned_roi_pct", size="budget_cost_eur", color="status", symbol="status",
        color_discrete_map=STATUS_COLORS, symbol_map=STATUS_SYMBOLS, category_orders={"status": ["Green", "Yellow", "Red"]},
        hover_name="project_name", custom_data=["project_number", "budget_label", "execution_type", "risk_score"],
        size_max=40, labels={"risk_x": "Risk score", "planned_roi_pct": "Planned ROI (%)", "status": "RAG"},
    )
    fig.update_traces(
        marker=dict(opacity=0.85, line=dict(width=2, color="rgba(252,252,251,0.9)")),
        hovertemplate="<b>%{hovertext}</b><br>%{customdata[0]} · %{customdata[2]}<br>"
                      "Risk score %{customdata[3]} · ROI %{y:.1f} %<br>Budget %{customdata[1]}<extra></extra>",
    )
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.08, x=0, title_text=""),
        xaxis=dict(dtick=1, range=[data["risk_score"].min() - 0.8, data["risk_score"].max() + 0.8], showgrid=False),
        yaxis=dict(ticksuffix=" %", zeroline=True),
    )
    return fig


def team_bar_chart(teams: pd.DataFrame):
    top = teams.head(5).sort_values("peak")  # largest overload on top
    fig = px.bar(top, x="peak", y="team", orientation="h", text=top["peak"].map(lambda v: f"{v:.1f} %"),
                 custom_data=["months", "projects"], labels={"peak": "Peak utilisation (%)", "team": ""})
    fig.update_traces(
        marker_color=SERIES_COLOR, marker_cornerradius=4, textposition="outside", cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>Peak %{x:.1f} %<br>%{customdata[0]} months > 100 % · %{customdata[1]} projects<extra></extra>",
    )
    limit = get_runtime_config().business_rules.team_capacity_limit_pct
    fig.add_vline(x=limit, line_dash="dash", line_width=1, line_color="#8a8a86",
                  annotation_text=f"Capacity limit {limit:g} %", annotation_position="top")
    fig.update_layout(height=360, margin=dict(l=10, r=40, t=30, b=10), bargap=0.35,
                      xaxis=dict(range=[0, max(top["peak"].max() * 1.15, 110)], ticksuffix=" %", showgrid=False))
    return fig


def skill_heatmap(skills: pd.DataFrame):
    """Utilisation per skill and month: one hue, darker = more loaded (no red/green pair); the numbers are in the
    cells. None without skill data."""
    if skills.empty:
        return None
    grid = skills.assign(month=pd.to_datetime(skills["month"]).dt.strftime("%Y-%m")) \
        .pivot_table(index="skill", columns="month", values="utilisation_pct", aggfunc="first")
    fig = px.imshow(grid.round(0), text_auto=True, aspect="auto", color_continuous_scale="Blues",
                    zmin=50, zmax=150, labels={"color": "Utilisation %", "x": "", "y": ""})
    fig.update_layout(height=60 + 28 * len(grid), margin=dict(l=10, r=10, t=10, b=10))
    return fig


# =========================================================================== dependency graph
def _layered_positions(nodes: list[str], edges: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """Predecessors left, their successors to the right (one column per level); a cycle falls back to a seeded
    spring layout, so the same graph always looks the same."""
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from(zip(edges["predecessor"], edges["successor"], strict=True))
    try:
        layers = list(nx.topological_generations(graph))
    except nx.NetworkXUnfeasible:
        return {n: (float(x), float(y)) for n, (x, y) in nx.spring_layout(graph, seed=7).items()}
    positions = {}
    for x, layer in enumerate(layers):
        for i, node in enumerate(sorted(layer)):
            positions[node] = (float(x), (len(layer) - 1) / 2 - i)
    return positions


def dependency_graph_figure(ds: PortfolioDataset, edges: pd.DataFrame, focus: list[str], height: int = 420):
    """Dependency graph as a Plotly figure (no extra JavaScript, works offline): an arrow points from the project
    that waits to the project it depends on; red = at-risk dependency, bold border = project of the report. Projects
    outside the RLS scope are one node without id or detail (see dependency_edges)."""
    import plotly.graph_objects as go

    projects = ds.projects.assign(project_number=ds.projects["project_number"].astype(str)).set_index("project_number")
    nodes = list(pd.unique(pd.concat([edges["successor"], edges["predecessor"], pd.Series(focus, dtype=str)])))
    pos = _layered_positions(nodes, edges)
    fig = go.Figure()
    for row in edges.itertuples(index=False):
        risky = row.status.strip().lower() in RISKY_DEPENDENCY_STATES
        (x0, y0), (x1, y1) = pos[row.successor], pos[row.predecessor]
        fig.add_trace(go.Scatter(x=[x0, x1], y=[y0, y1], mode="lines", hoverinfo="skip", showlegend=False,
                                 line={"color": "#d03b3b" if risky else "#8a8a86", "width": 3 if risky else 1.5,
                                       "dash": "solid" if risky else "dot"}))
        fig.add_annotation(x=x1, y=y1, ax=x0, ay=y0, xref="x", yref="y", axref="x", ayref="y", showarrow=True,
                           arrowhead=3, arrowsize=1.2, standoff=22, startstandoff=22, text="",
                           arrowcolor="#d03b3b" if risky else "#8a8a86")
        if risky:
            fig.add_annotation(x=(x0 + x1) / 2, y=(y0 + y1) / 2, text=html.escape(row.status), showarrow=False,
                               font={"color": "#b42318", "size": 12}, bgcolor="#ffffff")
    labels, hovers, colors, borders, widths, symbols = [], [], [], [], [], []
    for pid in nodes:
        visible = pid in projects.index
        name = str(projects.at[pid, "project_name"]) if visible and "project_name" in projects.columns else ""
        status = str(projects.at[pid, "status"]) if visible and "status" in projects.columns else ""
        # RAG as shape and word too, never colour alone
        rag = f" · {status}" if status in STATUS_SYMBOLS else ""
        labels.append(f"<b>{html.escape(pid)}</b>{rag}<br>{html.escape(name[:28])}" if visible else RESTRICTED_NODE)
        symbols.append(STATUS_SYMBOLS.get(status, "square-open"))
        hovers.append(html.escape(f"{pid} – {name} · RAG {status}" if visible else
                                  "Outside your scope – the PMO coordinates these dependencies."))
        colors.append(STATUS_COLORS.get(status, "#d9d9d6"))
        borders.append("#1f1f1d" if pid in focus else "#8a8a86")
        widths.append(4 if pid in focus else 1)
    fig.add_trace(go.Scatter(
        x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes], mode="markers+text", text=labels,
        textposition="bottom center", hovertext=hovers, hoverinfo="text", showlegend=False,
        textfont={"size": 12},  # the theme's text colour, readable in light and dark
        marker={"symbol": symbols, "size": 26, "color": colors, "line": {"color": borders, "width": widths}}))
    xs = [p[0] for p in pos.values()] or [0.0]
    ys = [p[1] for p in pos.values()] or [0.0]
    fig.update_layout(height=height, margin={"l": 10, "r": 10, "t": 10, "b": 10}, plot_bgcolor="#ffffff",
                      xaxis={"visible": False, "range": [min(xs) - 0.6, max(xs) + 0.6]},
                      yaxis={"visible": False, "range": [min(ys) - 0.9, max(ys) + 0.6]})
    return fig


# =========================================================================== delta charts
def delta_chart(deltas: pd.DataFrame, unit: str):
    data = deltas[deltas["unit"] == unit]
    if data.empty:
        return None
    long = data.melt(id_vars=["project", "metric"], value_vars=["before", "after"], var_name="scenario", value_name="value")
    long["scenario"] = long["scenario"].map({"before": "Before", "after": "After"})
    long["x"] = long["project"] + " · " + long["metric"]
    fmt = (lambda v: eur_m(v) if abs(v) >= 1e6 else f"€{v / 1e3:,.0f}k") if unit == "€" else (lambda v: f"{v:,.1f}")
    fig = px.bar(long, x="x", y="value", color="scenario", barmode="group", text=long["value"].map(fmt),
                 color_discrete_map={"Before": BEFORE_COLOR, "After": SERIES_COLOR},
                 labels={"x": "", "value": "EUR" if unit == "€" else "FTE", "scenario": ""})
    fig.update_traces(textposition="outside", cliponaxis=False, marker_cornerradius=4,
                      hovertemplate="%{x}<br>%{fullData.name}: %{text}<extra></extra>")
    fig.update_traces(marker_pattern_shape="/", selector={"name": "Before"})  # told apart without colour
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10), bargap=0.3,
                      legend=dict(orientation="h", y=1.12, x=0), yaxis=dict(showgrid=False))
    return fig
