"""Projects page: the project table with filters and the project edit form with role-based editing."""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from app.core.security import ROLE_PM, is_governed_change
from app.crew.scenarios import PORTFOLIO_ACTIONS
from app.crew.tools.data_steward_tools import DataQualityReport
from app.data_adapters.project_edit import (
    EDITABLE_FIELDS,
    access_level,
    diff_and_validate,
    field_editable,
    missing_required,
)
from app.ui.boundary import error_boundary
from app.ui.common import ACTIONS, DQ_OK, field_label, load_dataset, notify, page_header, rule_label
from app.ui.executive import render_project_page
from app.ui.shell import go_to
from app.views import projects as project_views
from app.views.navigation import page_title
from app.views.projects import DATE_BOUNDS, display_value, field_range, select_options
from app.views.projects import clamp as _clamp  # noqa: F401 - the input range rule, tested here

logger = logging.getLogger("derkurator.ui.projects_ui")

# =========================================================================== project table & detail view
GRID_COLUMNS = {
    "project_number": st.column_config.TextColumn("ID"),
    "project_name": st.column_config.TextColumn("Project", width="medium"),
    "project_manager": st.column_config.TextColumn("PM"),
    "status": st.column_config.TextColumn("RAG"),
    "execution_type": st.column_config.TextColumn("Method"),
    "percent_complete": st.column_config.ProgressColumn("Progress", format="%.0f%%", min_value=0, max_value=100),
    "budget_cost_eur": st.column_config.NumberColumn("Budget", format="euro"),
    "actual_cost_eur": st.column_config.NumberColumn("Actual cost", format="euro"),
    "estimate_at_completion_eur": st.column_config.NumberColumn("EAC", format="euro"),
    "planned_roi_pct": st.column_config.NumberColumn("ROI", format="%.1f%%"),
    "risk_score": st.column_config.NumberColumn("Risk", format="%d"),
    "planned_end_date": st.column_config.DateColumn("Planned end", format="YYYY-MM-DD"),
    "data_quality": st.column_config.TextColumn("Data quality"),
}


def filter_projects(view: pd.DataFrame, text: str = "", rag: list[str] | None = None, managers: list[str] | None = None,
                    quality: str = "All") -> pd.DataFrame:
    """Search (ID or name) and filters of the project table."""
    out = view
    if text.strip():
        needle = text.strip().lower()
        out = out[out["project_number"].astype(str).str.lower().str.contains(needle, regex=False)
                  | out["project_name"].astype(str).str.lower().str.contains(needle, regex=False)]
    if rag:
        out = out[out["status"].isin(rag)]
    if managers:
        out = out[out["project_manager"].astype(str).isin(managers)]
    if quality == "With findings" and "data_quality" in out.columns:
        out = out[out["data_quality"].astype(str) != DQ_OK]
    return out


def render_project_table(view: pd.DataFrame) -> str | None:
    """Searchable, filterable project overview. Returns the selected project number (or None)."""
    with st.container(border=True):
        f1, f2, f3, f4 = st.columns([1.6, 1, 1.4, 1.2], vertical_alignment="bottom")
        text = f1.text_input("Search", key="proj_search", placeholder="Project ID or name, then Enter", icon=":material/search:")
        rag = f2.multiselect("RAG", ["Green", "Yellow", "Red"], key="proj_rag", placeholder="All")
        managers = f3.multiselect("Project manager", sorted(view["project_manager"].dropna().astype(str).unique()),
                                  key="proj_pm", placeholder="All")
        quality = f4.segmented_control("Data quality", ["All", "With findings"], default="All", key="proj_dq",
                                       required=True)
        table = filter_projects(view, text, rag, managers, quality)
        table = table[[c for c in GRID_COLUMNS if c in table.columns]].reset_index(drop=True)
        st.caption(f"{len(table)} of {len(view)} projects · select a row to open the project")
        version = st.session_state.get("project_grid_version", 0)
        event = st.dataframe(table, hide_index=True, width="stretch", column_config=GRID_COLUMNS,
                             height=min(36 * (len(table) + 1) + 4, 640), selection_mode="single-row", on_select="rerun",
                             key="project_grid" if not version else f"project_grid_{version}")
    rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if not rows or rows[0] >= len(table):
        return None
    return str(table.iloc[rows[0]]["project_number"])


def _field_widget(field: str, value, disabled: bool, missing: bool, key: str, clamped: dict | None = None):
    """Renders one input. Required-but-empty fields get a red label and marker (widget label hidden).
    Stored values outside the widget range are clamped for display, flagged red and recorded in `clamped`
    (field -> (original, displayed)) so that an untouched field is never written back with the clamped value."""
    spec = EDITABLE_FIELDS[field]
    label = spec.label
    if missing:
        st.markdown(f":red[**{label} · :material/error: required – missing**]")
    kwargs = {"key": key, "disabled": disabled, "label_visibility": "collapsed" if missing else "visible"}
    shown, original = display_value(field, value)  # Streamlit raises on out-of-range widget values: clamped first
    low, high, step = field_range(field)

    def flag(original, shown, low, high):
        if clamped is not None:
            clamped[field] = (original, shown)
        text = f"{original:g}" if isinstance(original, float) else original
        st.caption(f":red[:material/error: Stored value **{text}** is outside the valid range ({low if low is not None else '–'} "
                   f"to {high if high is not None else '–'}) – shown as {shown}; please correct it.]")

    if spec.kind == "select":
        options = select_options(field, value)
        return st.selectbox(label, options, index=None if shown is None else options.index(shown), **kwargs)
    if spec.kind == "textarea":
        return st.text_area(label, value=shown or "", height=110, **kwargs)
    if spec.kind == "text":
        return st.text_input(label, value=shown or "", **kwargs)
    if original is not None:
        flag(original, shown, low, high)
    if spec.kind == "date":
        return st.date_input(label, value=shown, min_value=DATE_BOUNDS[0], max_value=DATE_BOUNDS[1], **kwargs)
    if spec.kind == "int":  # the only whole-number field is the risk score
        return st.number_input(label, value=shown, min_value=low, max_value=high, step=step, **kwargs)
    return st.number_input(label, value=shown, min_value=low, max_value=high, step=step, format="%.0f", **kwargs)


def render_project_detail(pid: str, view: pd.DataFrame, steward: DataQualityReport, role: str, user_id: str) -> None:
    matches = view[view["project_number"].astype(str) == pid]
    if matches.empty:
        return
    project = matches.iloc[0].to_dict()
    access = access_level(role, user_id, project)
    missing = set(missing_required(project))
    version = st.session_state.setdefault("detail_version", {}).get(pid, 0)

    with st.container(border=True):
        st.markdown(f"#### Edit master data – {pid}")
        if access == "read":
            st.caption("PMO view is read-only. Only the owning Project Manager can edit project master data.")
        issues = [i for i in steward.issues if i.project_id == pid]
        if missing:
            st.error(f"Required fields missing: {', '.join(EDITABLE_FIELDS[f].label if f in EDITABLE_FIELDS else f for f in sorted(missing))}")
        if issues:
            with st.expander(f"Data Steward findings for this project ({len(issues)})"):
                st.markdown("\n".join(f"- **{i.severity}** · {rule_label(i.rule)} · {field_label(i.column)} – {i.message}"
                                      for i in issues))

        # No st.form – the save button reacts live to what was changed.
        edits, clamped = {}, {}
        free = [f for f in EDITABLE_FIELDS if f != "business_case" and not is_governed_change({"field": f})]
        governed = [f for f in EDITABLE_FIELDS if is_governed_change({"field": f})]

        def widgets(fields: list[str]) -> None:
            cols = st.columns(3)
            for idx, field in enumerate(fields):
                with cols[idx % 3]:
                    editable = access == "edit" and field_editable(field, project, role, user_id)
                    edits[field] = _field_widget(field, project.get(field), not editable, field in missing,
                                                 key=f"edit_{pid}_{version}_{field}", clamped=clamped)

        # two sections, two buttons: what is saved directly never waits for a governed change, and vice versa
        st.markdown("**Master data** · saved directly")
        widgets(free)
        edits["business_case"] = _field_widget("business_case", project.get("business_case"), access != "edit",
                                               "business_case" in missing, key=f"edit_{pid}_{version}_business_case")
        # a clamped field the user did not touch keeps its stored value (no silent write of the display value)
        for field, (_original, shown) in clamped.items():
            if edits.get(field) == shown:
                edits[field] = project.get(field)
        direct_edits = {f: (edits[f] if f in edits and not is_governed_change({"field": f}) else project.get(f))
                        for f in EDITABLE_FIELDS}
        direct, direct_errors = diff_and_validate(project, direct_edits, role, user_id) if access == "edit" else ([], [])
        if direct_errors:
            st.error("Please correct:\n\n" + "\n".join(f"- {e}" for e in direct_errors))
        if access == "edit" and st.button(ACTIONS["save"][0] + " changes", icon=ACTIONS["save"][1], type="primary",
                                          key=f"edit_save_{pid}_{version}", disabled=not direct or bool(direct_errors),
                                          help=None if direct else "Change a master-data field first."):
            _save_project_changes(pid, project, direct_edits, role, user_id)

        st.divider()
        st.markdown("**Governed fields** · require PMO approval (four-eyes principle)")
        if access == "edit":
            st.caption("Changes in this section are submitted as a proposal to the PMO inbox and written after "
                       "approval. Approved budget and CapEx/OpEx plan can be filled in here only while empty – to "
                       "change an existing budget, simulate a budget scenario in the Steering Console.")
        widgets(governed)
        for field, (_original, shown) in clamped.items():
            if edits.get(field) == shown:
                edits[field] = project.get(field)
        governed_edits = {f: (edits[f] if f in edits and is_governed_change({"field": f}) else project.get(f))
                          for f in EDITABLE_FIELDS}
        proposed, governed_errors = (diff_and_validate(project, governed_edits, role, user_id) if access == "edit"
                                     else ([], []))
        if governed_errors:
            st.error("Please correct:\n\n" + "\n".join(f"- {e}" for e in governed_errors))
        label, icon = ACTIONS["submit"]
        if access == "edit" and st.button(label, icon=icon, type="primary", key=f"edit_submit_{pid}_{version}",
                                          disabled=not proposed or bool(governed_errors),
                                          help=None if proposed else "Change a governed field first."):
            _save_project_changes(pid, project, governed_edits, role, user_id)


def _save_project_changes(pid: str, project: dict, edits: dict, role: str, user_id: str) -> None:
    outcome = project_views.save_master_data(pid, project, edits, role, user_id, load_dataset())
    if outcome.kind != "success":
        getattr(st, outcome.kind)(outcome.message)
        return
    if outcome.written:
        load_dataset.clear()
    versions = st.session_state.setdefault("detail_version", {})
    versions[pid] = versions.get(pid, 0) + 1  # fresh form with the saved values
    notify("success", outcome.message)
    st.rerun()


def page_projects(ctx: dict) -> None:
    ds, view, steward, role, user_id = ctx["ds"], ctx["view"], ctx["steward"], ctx["role"], ctx["user_id"]
    visible = set(view["project_number"].astype(str))
    # a shared link (?project=…) opens the detail page – only for a project in the reader's own scope
    linked = st.query_params.get("project")
    if linked and not st.session_state.get("detail_pid") and linked in visible:
        st.session_state["detail_pid"] = linked
    pid = st.session_state.get("detail_pid")
    if pid and pid in visible:
        if st.query_params.get("project") != pid:
            st.query_params["project"] = pid
        with error_boundary("project detail"):
            render_project_page(pid, view, ds, steward, role, user_id,
                                edit_form=lambda: render_project_detail(pid, view, steward, role, user_id),
                                on_back=_close_detail, on_simulate=_simulate_for, on_scenario=_scenario_for)
        return
    head, link = st.columns([4, 1], vertical_alignment="bottom")
    with head:
        page_header(page_title(role, "projects", "Projects"),
                    f"{len(view)} projects within your permissions · open a project to see its detail page")
    if link.button("Data quality", icon=":material/fact_check:", key="projects_data_quality", type="tertiary"):
        go_to("data_quality")
    with error_boundary("project portfolio"):
        selected = render_project_table(view)
        if selected:
            st.session_state["detail_pid"] = selected
            st.rerun()
        if role == ROLE_PM:
            st.caption("Tip: select one of your projects to open its detail page and edit its master data.")


def _close_detail() -> None:
    st.session_state.pop("detail_pid", None)
    st.query_params.pop("project", None)
    st.session_state["project_grid_version"] = st.session_state.get("project_grid_version", 0) + 1  # clears the selection


def _simulate_for(pid: str) -> None:
    st.session_state["sb_project"] = pid
    if st.session_state.get("sb_action") in PORTFOLIO_ACTIONS:
        st.session_state["sb_action"] = "budget_change"
    st.session_state["composer_mode"] = "Guided"
    go_to("console")


def _scenario_for(pid: str, action: str, prefill: dict) -> None:
    """Opens the scenario builder with a PMO scenario preselected for the project (stage gate, risk response, ...)."""
    st.session_state.update({"sb_project": pid, "sb_action": action, "composer_mode": "Guided", **prefill})
    go_to("console")
