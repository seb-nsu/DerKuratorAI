"""Portfolio overview and data quality pages: top risks, KPI ribbon, Data Steward panel and the portfolio charts."""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from app.core.formatting import eur_m, pct, timestamp
from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_PM
from app.crew.tools.data_steward_tools import DataQualityReport
from app.data_adapters import PortfolioDataset
from app.ui.ask_box import render_ask_box
from app.ui.assist_facts import data_quality_facts, overview_facts
from app.ui.boundary import error_boundary
from app.ui.common import (
    PLOTLY_CONFIG,
    STATUS_COLORS,
    graph_version,
    page_header,
    show_table,
    top_risks,
)
from app.ui.executive import render_pmo_overview
from app.ui.shell import go_to
from app.views.assist import PAGES
from app.views.charts import bubble_chart, team_bar_chart
from app.views.data_quality import (
    INDEX_HELP,
    filter_findings,
    findings_frame,
    interpretation_inputs,
    quality_bars,
    quality_level,
)
from app.views.data_quality import interpret as interpret_dq
from app.views.overview import TOP_RISKS_UNAVAILABLE, kpis, scope_line, team_overload, waiting_proposals  # noqa: F401

logger = logging.getLogger("derkurator.ui.overview")

# =========================================================================== top risks
def render_top_risks(ds: PortfolioDataset) -> None:
    """Alert bar at the very top: top 3 blocked projects / systemic risks from the dependency graph."""
    risks, error = top_risks(tuple(sorted(ds.project_numbers)), graph_version())
    st.subheader("Top risks", anchor=False)
    if error:  # the detail is for the System Admin (system log), never on the business page
        logger.warning("Top risks unavailable: %s", error.splitlines()[0][:300])
        st.info(TOP_RISKS_UNAVAILABLE, icon=":material/hourglass_top:")
        return
    if not risks:
        st.caption(":material/check_circle: No blocked projects or systemic risks in your scope.")
        return
    for i, (col, risk) in enumerate(zip(st.columns(len(risks)), risks, strict=True)):
        with col, st.container(border=True):
            (st.error if risk["severity"] == "critical" else st.warning)(f"**{risk['title']}**", icon=":material/error:" if risk["severity"] == "critical" else ":material/warning:")
            st.metric(risk["metric_label"], risk["metric_value"], border=True)
            st.caption(f"{risk['name']} · {risk['detail']}")
            if risk.get("project") in ds.project_numbers:
                if st.button("Open project", icon=":material/open_in_new:", key=f"risk_open_{i}", type="tertiary"):
                    st.session_state["detail_pid"] = risk["project"]
                    go_to("projects")


# =========================================================================== KPIs & data quality
def render_kpi_ribbon(view: pd.DataFrame, ds: PortfolioDataset, teams: pd.DataFrame,
                      steward: DataQualityReport | None = None) -> None:
    figures = kpis(view, ds, teams)
    active, critical = figures["active_projects"], figures["critical_projects"]
    max_util = figures["max_team_utilisation_pct"]
    # a card whose number only repeats the scope ("10 of 10 active") says nothing and is left out
    show_active = figures["show_active"]
    cols = st.columns(5 if show_active else 4)
    k1, (k2, k3, k4, k5) = (cols[0], cols[1:]) if show_active else (None, cols)
    if k1 is not None:
        k1.metric("Active projects", active, border=True)
        k1.caption(f"of {len(view)} projects within your permissions")
    k2.metric("Total budget", eur_m(view["budget_cost_eur"].sum()), border=True)
    k2.caption(f"EAC {eur_m(view['estimate_at_completion_eur'].sum())}")
    k3.metric("Critical projects", critical, border=True, help="Projects with RAG status red within your permissions.")
    k3.caption(f"{pct(critical / len(view) * 100)} of the portfolio" if critical and len(view) else "No red projects")
    k4.metric("Max team utilisation", pct(max_util, 1) if max_util is not None else "–", border=True)
    k4.caption(f"Top team: {teams.iloc[0]['team']}" if not teams.empty else "No team above the capacity limit")
    if steward is not None:
        blocked = len(steward.blocked_project_ids)
        k5.metric("Data quality index", pct(steward.quality_index), border=True, help=INDEX_HELP)
        k5.caption(f"{blocked} project(s) blocked for simulation" if blocked else "No project blocked")
        if st.button("Data quality details", key="kpi_dq_details", type="tertiary", icon=":material/arrow_forward:"):
            go_to("data_quality")


def quality_bar(label: str, value: float, detail: str) -> None:
    """Coloured progress bar (st.progress has no colours in Streamlit 1.64).
    Uses the status palette plus status word and value as text, so colour never carries the meaning alone."""
    status, word = quality_level(value)
    color = STATUS_COLORS[status]
    width = max(0.0, min(100.0, value))
    st.html(
        f'<div style="margin:2px 0 12px 0">'
        f'<div style="display:flex;justify-content:space-between;font-size:0.9rem"><span>{label}</span>'
        f'<strong>{value:.0f} % · {word}</strong></div>'
        f'<div role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{value:.0f}" '
        f'aria-label="{label}: {value:.0f} %, {word}" '
        f'style="background:#eef0f3;border-radius:4px;height:10px;overflow:hidden">'
        f'<div style="width:{width:.1f}%;background:{color};height:10px;border-radius:4px"></div></div>'
        f'<div style="font-size:0.8rem;color:#4a5566;margin-top:2px">{detail}</div></div>'
    )


@st.cache_data(ttl=3600, show_spinner="AI is interpreting the data risks ...")
def cached_dq_interpretation(summary_json: str, model: str, llm_on: bool):
    """Cached per data state and model: the AI interpretation is generated once, not on every rerun."""
    return interpret_dq(summary_json, model, llm_on)


def render_data_quality_panel(steward: DataQualityReport) -> None:
    left, right = st.columns([1, 2])
    with left, st.container(border=True):
        st.metric(label="Data quality index", value=pct(steward.quality_index), border=True, help=INDEX_HELP)
        st.caption(f"{len(steward.blocked_project_ids)} project(s) blocked for simulation"
                   if steward.blocked_project_ids else "No project blocked")
        for bar in quality_bars(steward):
            quality_bar(bar["label"], bar["value"], bar["detail"])
    with right, st.container(border=True):
        interpretation = cached_dq_interpretation(*interpretation_inputs(steward))
        st.markdown("**Executive interpretation**")
        st.markdown(interpretation.text)
        st.caption(f"Written by `{interpretation.model}` based on deterministic Data Steward findings"
                   if interpretation.source == "llm" else
                   "Rule-based interpretation (no LLM available) based on deterministic Data Steward findings")
    render_findings_table(steward)


def render_findings_table(steward: DataQualityReport) -> None:
    with st.container(border=True):
        st.markdown(f"**Findings** · {len(steward.errors)} errors · {len(steward.warnings)} warnings · blocked for "
                    f"simulation: {', '.join(steward.blocked_project_ids) or '–'}")
        if not steward.issues:
            st.caption(f":material/check_circle: {steward.total_rows} projects checked – no findings.")
            return
        frame = findings_frame(steward)
        f1, f2 = st.columns([1, 2])
        severity = f1.segmented_control("Severity", ["All", "Errors", "Warnings"], default="All", key="dq_severity",
                                        required=True)
        projects = f2.multiselect("Projects", sorted(frame["Project"].unique()), key="dq_projects",
                                  placeholder="All projects")
        frame = filter_findings(frame, severity or "All", projects)
        show_table(frame, hide_index=True, width="stretch")
        st.caption(f"{len(frame)} finding(s) shown")


# =========================================================================== charts
def render_charts(view: pd.DataFrame, teams: pd.DataFrame) -> None:
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown("#### Risk vs. ROI")
        st.caption("Bubble size = budget · shape and colour = RAG status")
        fig = bubble_chart(view)
        if fig is None:
            st.info("No projects with risk score, ROI and budget for the risk/value matrix.")
        else:
            st.plotly_chart(fig, theme="streamlit", config=PLOTLY_CONFIG, width="stretch")
    with right, st.container(border=True):
        st.markdown("#### Top 5 overloaded teams")
        if teams.empty:
            limit = get_runtime_config().business_rules.team_capacity_limit_pct
            st.info(f"No team above the capacity limit ({limit:g} %) in your project scope.")
        else:
            st.plotly_chart(team_bar_chart(teams), theme="streamlit", config=PLOTLY_CONFIG, width="stretch")


def render_open_items(user_id: str) -> None:
    """A Project Manager's proposals still waiting for the PMO, with their deadline state."""
    from app.ui.approval_governance import sla_columns

    waiting = waiting_proposals(user_id)
    if not waiting:
        st.caption(":material/check_circle: No proposal of yours is waiting for the PMO.")
    else:
        show_table(pd.DataFrame([{"Proposal": p["id"], "Request": p.get("request") or "–",
                                  "Submitted": timestamp(p.get("timestamp")), **sla_columns(p)} for p in waiting]))
    if st.button("Open Approvals", icon=":material/arrow_forward:", key="overview_open_approvals", type="tertiary"):
        go_to("approvals")


# =========================================================================== pages
def page_overview(ctx: dict) -> None:
    ds, view, steward, teams = ctx["ds"], ctx["view"], ctx["steward"], ctx["teams"]
    own_view = ctx.get("role") == ROLE_PM
    page_header("Overview", scope_line(ctx.get("role"), len(ds.projects), len(ctx["full_ds"].projects)))
    # one error boundary per section: a failing chart never takes the rest of the page down with it
    with error_boundary("top risks"):
        render_top_risks(ds)
    with error_boundary("KPI ribbon"):
        render_kpi_ribbon(view, ds, teams, steward)
    with error_boundary("portfolio assistant"):
        title, subject, examples = PAGES["overview"]
        render_ask_box("overview", subject, lambda: overview_facts(view, ds, teams, steward), title=title,
                       examples=examples)
    st.subheader("Portfolio analysis", anchor=False)
    with error_boundary("portfolio charts"):
        render_charts(view, teams)
    if own_view:  # the management reserve is the PMO's lever; a PM sees what waits on them instead
        st.subheader("Your open items", anchor=False)
        with error_boundary("open items"):
            render_open_items(ctx["user_id"])
        return
    st.subheader("Skills and management reserve", anchor=False)
    with error_boundary("skills and reserve"):
        render_pmo_overview(ds)
    if st.button("Decision insights", icon=":material/insights:", key="overview_insights", type="tertiary",
                 help="Status check of the sources, the portfolio with probabilities and the review of past "
                      "decisions"):
        go_to("insights")


def page_data_quality(ctx: dict) -> None:
    if st.button("Back to the projects", icon=":material/arrow_back:", key="data_quality_back", type="tertiary"):
        go_to("projects")
    page_header("Data quality", f"Deterministic Data Steward · {ctx['steward'].total_rows} projects checked")
    with error_boundary("data quality panel"):
        render_data_quality_panel(ctx["steward"])
    with error_boundary("data quality assistant"):
        title, subject, examples = PAGES["data_quality"]
        render_ask_box("data_quality", subject, lambda: data_quality_facts(ctx["steward"]), title=title,
                       examples=examples)
