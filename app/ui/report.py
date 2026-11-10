"""Report viewer: one decision report (a Steering Console analysis or a PMO proposal) on one screen.

Layout: header (request, verdict, status, actions) → key-figure row → tabs Summary · Decision paper · Changes ·
Impact · Dependencies · Data quality. The Steering Console and the PMO approval inbox use the same viewer; only the
actions differ (passed in as a callable). Everything shown respects the RLS-filtered dataset of the signed-in user.
"""
from __future__ import annotations

from io import StringIO
from typing import Any, Callable

import pandas as pd
import streamlit as st

from app.core import clock
from app.core.runtime_config import get_runtime_config
from app.core.security import is_governed_change
from app.data_adapters.dataset import PortfolioDataset
from app.ui.ask_box import render_ask_box
from app.ui.assist_facts import report_facts
from app.ui.common import (
    PLOTLY_CONFIG,
    audit,
    decision_count,
    graph_version,
    project_names,
    show_table,
    title_columns,
    verdict_badge,
    verdict_callout,
)
from app.ui.steerco_pack import metric_texts, pack_data, render_pack_html
from app.ui.visuals import (
    build_report_markdown,
    changes_csv,
    delta_chart,
    dependency_edges,
    dependency_graph_figure,
    focus_projects,
    scenario_deltas,
    wants_dependency_graph,
)
from app.views.exports import change_table
from app.views.reports import (
    FULL_PAPER,
    OFFLINE_NOTICE,
    paper_sections,
    report_findings,
    requested_projects,
)
from app.views.reports import rule_rows as _rule_rows


@st.cache_data(max_entries=8, show_spinner=False)
def cached_graph_figure(_ds: PortfolioDataset, edges_json: str, focus: tuple[str, ...], data_key: tuple):
    """Build the graph figure once per report and data state."""
    return dependency_graph_figure(_ds, pd.read_json(StringIO(edges_json), orient="records", dtype=str), list(focus))


def render_report(key: str, message: dict[str, Any], ds: PortfolioDataset | None, *, request: str | None = None,
                  eyebrow: str = "", status_md: str = "", actions: Callable[[], None] | None = None,
                  requester: str | None = None, preview: bool = False) -> None:
    """Renders one report. `preview=True`: early verdict card while the paper is still being written (no tabs)."""
    meta = message.get("meta") or {}
    identity = st.session_state.get("identity") or ("", "")
    data = pack_data(message, requester=requester or identity[1], environment=get_runtime_config().environment)
    changes = meta.get("proposed_changes") or []
    known = set(ds.project_numbers) if ds is not None else set()
    focus = focus_projects(meta, known, str(message.get("content", ""))) if ds is not None else []
    # Dependencies only when the request is about them (stop, domino, bottleneck ...), as the paper then reports them
    show_deps = ds is not None and bool(focus) and wants_dependency_graph(meta, str(message.get("content", "")))
    edges = dependency_edges(ds, focus) if show_deps else pd.DataFrame()
    findings = report_findings(meta, requested_projects(meta))

    with st.container(border=True, key=f"report_{key}"):
        head, act = st.columns([3, 2])
        with head:
            if eyebrow:
                st.caption(eyebrow)
            st.markdown(f"**{request or meta.get('request') or data['title']}**")
            st.markdown(verdict_badge(data['verdict']) + (f"&nbsp;&nbsp;{status_md}" if status_md else ""))
        with act:
            if actions is not None and not preview:
                actions()
            elif preview:
                st.caption(":material/hourglass_top: Verdict ready – the decision paper is being written ...")

        verdict_callout(data["verdict"], data.get("reasons"))
        # only the key figures this report changes; an empty "–" card says nothing
        shown = [m for m in metric_texts(data["metrics"]) if m[1] != "–"]
        cols = st.columns(len(shown) + 1) if shown else st.columns([1, 3])
        for col, (label, value, detail) in zip(cols, shown, strict=False):
            col.metric(label, value, border=True)
            col.caption(detail)
        changed = list(dict.fromkeys(str(c.get("project_id")) for c in changes))
        referenced = [p for p in focus if p not in changed]
        decisions = decision_count(changes)
        cols[len(shown)].metric("Changes", decisions, border=True,
                                help="Changes counted as decisions: a budget cut that also moves CapEx, OpEx and total "
                                     "cost counts once. Governed changes (budget, schedule, state, phase …) need the "
                                     "approval of a PMO other than the requester (four-eyes principle).")
        cols[len(shown)].caption(
            f"{len(changes)} field(s) in {len(changed)} project(s) · "
            f"{sum(1 for c in changes if is_governed_change(c))} governed"
            + (f" · {len(referenced)} referenced" if referenced else ""))

        if preview:
            _render_summary(data, meta)
            return

        labels = ["Summary", "Decision paper", "Changes", "Impact"]
        if not edges.empty:
            labels.append(f"Dependencies ({len(edges)})")
        if not findings.empty:
            labels.append(f"Data quality ({len(findings)})")
        tabs = dict(zip(labels, st.tabs(labels), strict=True))
        t_sum, t_paper, t_chg, t_imp = (tabs[label] for label in labels[:4])
        with t_sum:
            _render_summary(data, meta)
        with t_paper:
            _render_paper(key, str(message.get("content", "")))
        with t_chg:
            if changes:
                show_table(change_table(changes, ds), hide_index=True, width="stretch")
            else:
                st.caption("No data changes proposed.")
        with t_imp:
            _render_impact(key, changes)
        if not edges.empty:
            with tabs[f"Dependencies ({len(edges)})"]:
                _render_dependencies(key, ds, edges, focus)
        if not findings.empty:
            with tabs[f"Data quality ({len(findings)})"]:
                st.caption("Data Steward findings for the projects this request names or changes, as of the analysis. "
                           "The portfolio-wide view is on the Data Quality page.")
                show_table(findings, hide_index=True, width="stretch")
        render_ask_box(f"report_{key}", "a steering report (a simulated change request, its rule-based verdict and "
                       "decision paper)", lambda: report_facts(message, data, change_table(changes, ds) if changes
                                                               else None),
                       title="Ask about this report",
                       examples=("Explain the verdict in two sentences", "What must happen to meet the conditions?"))



def _render_summary(data: dict[str, Any], meta: dict[str, Any]) -> None:
    warnings = list(meta.get("warnings", []))
    # the offline mode is how the demo runs, not a problem: a neutral note at the end instead of a warning box
    offline = [w for w in warnings if str(w).startswith(OFFLINE_NOTICE)]
    steps = [w for w in warnings if str(w).startswith("Step ") and " of a what-if" in str(w)]  # context, no problem
    for warning in (w for w in warnings if w not in offline and w not in steps):
        st.warning(warning, icon=":material/warning:")
    for step in steps:
        st.caption(f":material/link: {step}")
    if meta.get("forced"):
        st.caption(":material/warning: Forced simulation – results for blocked projects are indicative only.")
    left, right = st.columns(2)
    with left:
        as_of = (meta.get("verdict") or {}).get("as_of") if isinstance(meta.get("verdict"), dict) else None
        st.markdown("**Why this verdict** · rule-based, not AI" + (f" · As of {as_of}" if as_of else ""))
        if data.get("reasons"):
            st.markdown("\n".join(f"- {r}" for r in data["reasons"]))
        else:
            st.caption("No rule-based reasons recorded (e.g. an analysis without a verdict).")
        rules = (meta.get("verdict") or {}).get("rules") if isinstance(meta.get("verdict"), dict) else None
        if rules:
            with st.popover("Thresholds applied", icon=":material/rule:"):
                st.caption("Business rules in force when this verdict was computed (stored with the decision).")
                show_table(_rule_rows(rules), hide_index=True, width="stretch")
    with right:
        st.markdown("**Conditions & next steps**")
        if data["conditions"]:
            st.markdown("\n".join(f"{i}. {c}" for i, c in enumerate(data["conditions"], start=1)))
        else:
            st.caption("No conditions.")
        for flag in data["flags"]:
            st.caption(f":material/flag: {flag}")
    if offline:
        st.caption(":material/rule: Written by the rule engine (offline mode, no AI agents) – the verdict and figures are "
                   "the same as with AI agents.")


def _render_paper(key: str, paper: str) -> None:
    sections = paper_sections(paper)
    titles = [t for t, _ in sections]
    if len(sections) == 1:
        st.markdown(paper or "_No decision paper stored._")
        return
    choice = st.segmented_control("Section", [*titles, FULL_PAPER], default=titles[0], key=f"paper_section_{key}",
                                  label_visibility="collapsed")
    if choice in (None, FULL_PAPER):
        st.markdown(paper)
        return
    body = dict(sections)[choice]
    st.markdown(f"#### {choice}\n\n{body or '_Empty section._'}")


def _render_impact(key: str, changes: list[dict[str, Any]]) -> None:
    deltas = scenario_deltas(changes)
    if deltas.empty:
        st.caption("No budget or FTE shift in this report (e.g. schedule, stop or analysis scenario).")
        return
    st.caption("Before vs. after per project")
    for unit in ("€", "FTE"):
        fig = delta_chart(deltas, unit)
        if fig is not None:
            st.plotly_chart(fig, theme="streamlit", config=PLOTLY_CONFIG, width="stretch", key=f"delta_{key}_{unit}")


def _render_dependencies(key: str, ds: PortfolioDataset | None, edges: pd.DataFrame, focus: list[str]) -> None:
    if ds is None or edges.empty:
        st.caption("No dependencies around the projects of this report.")
        return
    direct = edges[edges["successor"].isin(focus) | edges["predecessor"].isin(focus)]
    show_all = len(direct) < len(edges) and st.toggle(f"Show all levels ({len(edges)} links)", key=f"deps_all_{key}",
                                                       help="By default only the direct links of this report's "
                                                            "projects are drawn.")
    shown = edges if show_all or direct.empty else direct
    st.caption(f"Around {', '.join(focus[:3])}{' …' if len(focus) > 3 else ''} · arrow = depends on · red, solid = "
               "at-risk dependency · bold border = project in this report · shape = RAG (● Green, ▲ Yellow, ■ Red)")
    # cache key includes the visible (RLS) project set – names outside a user's scope never leak via the cache
    fig = cached_graph_figure(ds, shown.to_json(orient="records"), tuple(focus),
                              (tuple(sorted(ds.project_numbers)), len(ds.dependencies), graph_version()))
    st.plotly_chart(fig, config=PLOTLY_CONFIG, width="stretch", key=f"deps_{key}")
    names = project_names(ds)
    table = shown.copy()
    for col in ("successor", "predecessor"):
        table[col] = table[col].map(lambda p: f"{p} – {names[p]}" if p in names else p)
    show_table(title_columns(table, {"successor": "Project (waits)", "predecessor": "Depends on"}))


def export_menu(key: str, message: dict[str, Any], user_message: dict[str, Any] | None, ds: PortfolioDataset | None,
                requester: str | None = None) -> None:
    """Export popover for one report: SteerCo Pack (print / PDF), this analysis as Markdown, its changes as CSV."""
    role, user_id = st.session_state.get("identity") or ("-", "-")
    data = pack_data(message, requester=requester or user_id, environment=get_runtime_config().environment)
    stamp = clock.now().strftime("%Y%m%d_%H%M")
    with st.popover("Export", icon=":material/download:"):
        st.download_button("SteerCo Pack (print / PDF)", render_pack_html(data), mime="text/html", icon=":material/print:",
                           file_name=f"steerco_pack_{stamp}.html", key=f"pack_{key}", width="stretch",
                           help="One-page HTML summary – open it and use the browser's 'Print → Save as PDF'.",
                           on_click=audit, args=("steerco_pack_exported",),
                           kwargs={"role": role, "user_id": user_id, "verdict": data["verdict"]})
        pair = [m for m in (user_message, message) if m]
        if ds is not None:
            st.download_button("This analysis (Markdown)", build_report_markdown(pair, ds, role, user_id,
                                                                                get_runtime_config().environment),
                               file_name=f"derkurator_analysis_{stamp}.md", mime="text/markdown", width="stretch",
                               key=f"report_md_{key}", icon=":material/description:", on_click=audit,
                               args=("report_exported",), kwargs={"role": role, "user_id": user_id, "format": "markdown",
                                                                  "messages": len(pair)})
        st.download_button("Changes (CSV)", changes_csv([message]), file_name=f"derkurator_changes_{stamp}.csv",
                           mime="text/csv", key=f"report_csv_{key}", icon=":material/table_view:", width="stretch",
                           on_click=audit, args=("report_exported",),
                           kwargs={"role": role, "user_id": user_id, "format": "csv", "messages": 1})
