"""Executive workspace: project detail page and the A/B comparison of saved what-ifs (What-if comparison page).

Pure data helpers (budget_impact, cockpit_* frames, compare_frame) are separated from rendering so they can be
unit-tested; everything shown respects the RLS-filtered dataset of the signed-in user.
"""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd
import streamlit as st

from app.core import clock
from app.core.formatting import eur, timestamp
from app.core.scenario_store import ScenarioStoreError, delete_scenario, list_scenarios
from app.core.security import ROLE_PMO
from app.data_adapters.dataset import PortfolioDataset
from app.data_adapters.project_edit import EDITABLE_FIELDS, access_level, missing_required
from app.ui.ask_box import render_ask_box
from app.ui.assist_facts import project_facts, scenario_compare_facts
from app.ui.common import (
    PLOTLY_CONFIG,
    audit,
    badge,
    field_label,
    notify,
    page_header,
    rag_badge,
    rule_label,
    show_table,
    status_text,
    title_columns,
    verdict_badge,
)
from app.views.projects import (  # noqa: F401 - the detail page helpers live in the shared page logic
    CLOSED_RISK_STATES,
    RESTRICTED,
    RESTRICTED_NOTE,
    baseline_frame,
    cockpit_cost_curve,
    cockpit_deadlines,
    cockpit_dependencies,
    cost_curve_caption,
    cost_curve_figure,
    key_figures,
    master_data_frames,
    project_risks,
    risk_exposure_frame,
    status_history,
)
from app.views.projects import to_day as _day
from app.views.whatifs import (  # noqa: F401 - re-exported for the pages and tests
    budget_impact,
    compare_frame,
    impact_bars,
    impact_bars_figure,
    scenario_figures,
    waterfall_figure,
)


# =========================================================================== pure helpers
def scenario_summary(s: dict[str, Any]) -> dict[str, Any]:
    f = scenario_figures(s)
    return {
        "Verdict": status_text(f["verdict"]),
        "Net budget impact": eur(f["net_budget_impact"], signed=True),
        "Budget released": eur(f["budget_released"]),
        "Budget added": eur(f["budget_added"]),
        "Projects stopped": f["projects_stopped"],
        "Projects on hold": f["projects_on_hold"],
        "FTE moved": f"{f['fte_moved']:g}",
        "Changes": f["changes"],
        "Steps": f["steps"],
    }




def pmo_overview(ds: PortfolioDataset, months: int = 6) -> tuple[pd.DataFrame, float]:
    """Skill utilisation per skill and month and the management reserve balance, computed by the same tools the
    simulations use (in an isolated scenario context of the current view, so no running analysis is touched)."""
    from app.services import pmo_figures

    return pmo_figures(ds, months, clock.today())


def render_pmo_overview(ds: PortfolioDataset) -> None:
    """Overview section: management reserve against its floor and the skill heatmap (where a team average hides a
    bottleneck)."""
    from app.core.business_rules import current_rules

    skills, balance = pmo_overview(ds)
    rules = current_rules()
    floor = rules.management_reserve_eur * rules.reserve_floor_pct / 100
    left, right = st.columns([1, 3])
    left.metric("Management reserve", eur(balance), f"floor {eur(floor)}", delta_color="off", border=True,
                help="Configured start (business rules) plus the bookings in Reserve_Ledger.")
    if balance < floor:
        left.error("The reserve is below its floor.")
    with right:
        if skills.empty:
            st.caption("No skill data recorded.")
            return
        from app.views.charts import skill_heatmap

        st.plotly_chart(skill_heatmap(skills), theme="streamlit", config=PLOTLY_CONFIG, width="stretch", key="skill_heatmap")
        st.caption(f"Utilisation per skill across all teams; alert above {rules.overload_alert_pct:g} %.")


# =========================================================================== project detail page
def render_project_page(pid: str, view: pd.DataFrame, ds: PortfolioDataset, steward, role: str, user_id: str,
                        edit_form: Callable[[], None], on_back: Callable[[], None] | None = None,
                        on_simulate: Callable[[str], None] | None = None,
                        on_scenario: Callable[[str, str, dict[str, Any]], None] | None = None) -> None:
    """Project detail page: header with status, key figures and tabs; the edit form is the last tab
    (owning Project Manager only)."""
    matches = view[view["project_number"].astype(str) == pid]
    if matches.empty:
        return
    project = matches.iloc[0].to_dict()
    access = access_level(role, user_id, project)
    flags = steward.project_status()
    if on_back is not None:
        st.button("All projects", icon=":material/arrow_back:", key="detail_back", type="tertiary", on_click=on_back)
    with st.container(border=True):
        head, actions = st.columns([4, 2], vertical_alignment="center")
        with head:
            page_header(f"{pid} – {project.get('project_name')}")
        flag = flags.get(pid)
        head.markdown(" ".join([rag_badge(project.get("status")), badge(str(project.get("state") or "–"), "gray"),
                                badge(str(project.get("execution_type") or "–"), "gray"),
                                badge(f"Phase: {project.get('phase') or '–'}", "blue", ":material/flag:"),
                                badge("Blocked for simulation", "red", ":material/block:") if flag == "Blocked"
                                else badge("Data warnings", "orange", ":material/warning:") if flag
                                else badge("Data ok", "green", ":material/check:")]))
        if on_simulate is not None and actions.button("Simulate a change for this project", icon=":material/query_stats:",
                                                      key="detail_simulate", width="stretch"):
            on_simulate(pid)
        if on_scenario is not None and actions.button("Check stage gate", icon=":material/flag:", key="detail_gate",
                                                      width="stretch"):
            on_scenario(pid, "stage_gate", {})
        figures = key_figures(project, flag)
        budget, actual, eac, end = figures["budget"], figures["actual"], figures["eac"], figures["planned_end"]
        m = st.columns(5)
        # context as captions, not metric deltas: a delta arrow suggests a change where there is none
        m[0].metric("Budget", eur(budget), border=True)
        m[1].metric("Actual", eur(actual), border=True)
        if figures["actual_pct_of_budget"] is not None:
            m[1].caption(f"{figures['actual_pct_of_budget']:.0f} % of budget")
        m[2].metric("EAC", eur(eac), border=True)
        if figures["eac_vs_budget"] is not None:
            over = figures["eac_vs_budget"]
            m[2].caption("On budget" if abs(over) < 1 else
                         f"{':red[' if over > 0 else ''}{eur(over, signed=True)} vs budget{']' if over > 0 else ''}")
        m[3].metric("Progress", f"{figures['progress']:.0f} %", border=True)
        m[4].metric("Planned end", end.isoformat() if end else "–", border=True)
        if end:
            left = figures["days_to_end"]
            m[4].caption(f"in {left} days" if left >= 0 else f":red[{-left} days overdue]")
        missing = missing_required(project)
        if missing:
            st.markdown(":red[:material/error: **Required fields missing:** "
                        + ", ".join(EDITABLE_FIELDS[f].label if f in EDITABLE_FIELDS else f for f in missing) + "]")

        issues = [i for i in steward.issues if i.project_id == pid]
        # one tab per question; the Edit tab exists only for whoever may edit
        names = ["Master data", "Status history", "Costs & baselines", "Benefits", "Schedule & dependencies", "Risks",
                 f"Data quality ({len(issues)})", *(["Edit"] if access == "edit" else [])]
        tabs = st.tabs(names)
        t_master, t_status, t_cost, t_benefits, t_deadlines, t_risks, t_dq = tabs[:7]
        t_edit = tabs[7] if access == "edit" else None
        t_baselines, t_deps = t_cost, t_deadlines
        with t_master:  # master data readable right when the detail page opens
            free, governed = master_data_frames(project)
            left, right = st.columns(2)
            with left:
                st.markdown("**Master data**")
                show_table(free, hide_index=True, width="stretch")
            with right:
                st.markdown("**Governed fields** · changes need PMO approval")
                show_table(governed, hide_index=True, width="stretch")
            st.markdown("**Business case**")
            st.markdown(str(project.get("business_case") or "").strip() or "_No business case recorded._")
        with t_status:
            table = status_history(pid, ds)
            if table.empty:
                st.caption("No status reports recorded.")
            else:
                show_table(title_columns(table, {"issues_decisions": "Issues & decisions"}), hide_index=True,
                             width="stretch")
        with t_cost:
            curve = cockpit_cost_curve(pid, ds)
            if curve.empty:
                st.caption("No cost plan recorded.")
            else:
                fig = cost_curve_figure(curve, budget, eac)
                st.plotly_chart(fig, theme="streamlit", config=PLOTLY_CONFIG, width="stretch", key=f"cockpit_cost_{pid}")
                caption = cost_curve_caption(curve)
                if caption:  # the chart in one sentence (text alternative)
                    st.caption(caption)
                st.markdown("**Baselines**")
        with t_benefits:
            from app.ui.benefits_ui import render_benefits_tab  # local: keeps the cockpit free of the data adapters

            render_benefits_tab(pid, project, ds, role, user_id, access == "edit" or role == ROLE_PMO, on_scenario)
        with t_deadlines:
            st.markdown("**Deadlines**")
            deadlines = cockpit_deadlines(pid, project, ds)
            if deadlines.empty:
                st.caption("No dated milestones.")
            else:
                shown = deadlines.assign(days=deadlines["days"].map(lambda d: f"{d} (overdue)" if d < 0 else str(d)))
                show_table(title_columns(shown, {"days": "Days left", "item": "Milestone / project"}),
                             hide_index=True, width="stretch")
        with t_deps:
            st.markdown("**Dependencies**")
            incoming, outgoing = cockpit_dependencies(pid, ds)
            dep_labels = {"type": "Dependency type", "required_by": "Required by"}
            st.markdown("**Waits for (incoming)**")
            if incoming.empty:
                st.caption("None.")
            else:
                show_table(title_columns(incoming, dep_labels), hide_index=True, width="stretch")
            st.markdown("**Is needed by (outgoing)**")
            if outgoing.empty:
                st.caption("None.")
            else:
                show_table(title_columns(outgoing, dep_labels), hide_index=True, width="stretch")
        with t_risks:
            mine = project_risks(pid, ds)
            if mine.empty:
                st.caption("No risks recorded.")
            else:
                shown = mine.assign(exposure_eur=mine["exposure_eur"].map(eur))
                show_table(title_columns(shown, {"risk_number": "Risk ID", "short_description": "Risk",
                                                   "risk_status": "Status", "systemic_risk_cluster": "Risk cluster",
                                                   "exposure_eur": "Exposure (€)"}),
                             hide_index=True, width="stretch")
                st.caption(f"Total exposure: {eur(float(mine['exposure_eur'].sum()))} · probability × impact on the "
                           "risk scale of the business rules.")
                if on_scenario is not None:
                    pick, button = st.columns([3, 1], vertical_alignment="bottom")
                    risk = pick.selectbox("Risk", list(mine["risk_number"].astype(str)), key=f"detail_risk_{pid}")
                    if button.button("Plan response", icon=":material/shield:", key=f"detail_risk_btn_{pid}"):
                        on_scenario(pid, "risk_response", {"sb_risk": risk})
        with t_baselines:
            history = baseline_frame(pid, project, ds)
            if history.empty:
                st.caption("No baseline recorded.")
            else:
                show_table(history, hide_index=True, width="stretch")
                approved = _day(project.get("approved_end_date"))
                if approved and end:
                    st.caption(f"Planned end vs. approved end: {(end - approved).days:+d} days.")
            if on_scenario is not None and st.button("Re-baseline", icon=":material/restart_alt:",
                                                     key=f"detail_rebaseline_{pid}"):
                on_scenario(pid, "rebaseline", {})

        with t_dq:
            if not issues:
                st.caption(":material/check_circle: No Data Steward findings for this project.")
            else:
                show_table(pd.DataFrame([{"Severity": i.severity, "Rule": rule_label(i.rule), "Field": field_label(i.column),
                                            "Finding": i.message} for i in issues]), hide_index=True, width="stretch")
        if t_edit is not None:
            with t_edit:
                st.caption("Master data can be edited by the owning Project Manager; budget changes go through the "
                           "Steering Console; governed fields are submitted as a proposal to the PMO inbox.")
                edit_form()
        else:
            st.caption(":material/lock: Read-only – master data is maintained by the owning Project Manager.")
    render_ask_box(f"project_{pid}", f"project {pid}", lambda: project_facts(pid, project, ds, issues),
                   title="Ask about this project", project_id=pid,
                   examples=("Why is this project at risk?", "What is due in the next 90 days?",
                             "What if the budget is cut by 10 %?"))


def render_scenario_compare(role: str, user_id: str) -> None:
    """What-if comparison page: A/B comparison of saved what-ifs, the budget impact side by side, differences and the list."""
    saved = list_scenarios(role, user_id)
    page_header("What-if comparison", f"A/B comparison · {len(saved)} saved what-if(s) in your scope")
    if not saved:
        st.info("No saved what-ifs yet. Save a report in the Steering Console with 'Save as what-if'.")
        return
    labels = {s["id"]: f"{s['name']} · {s.get('owner')} · {timestamp(s.get('timestamp'))}" for s in saved}
    if len(saved) < 2:
        st.caption(f"1 saved what-if ('{saved[0]['name']}'). Save a second one to compare them side by side.")
        _render_saved_scenarios(saved, labels, role, user_id)
        return
    ids = list(labels)
    c1, c2 = st.columns(2)
    # newest first in the store: A defaults to the older, B to the newer of the two latest scenarios
    a_id = c1.selectbox("Scenario A (older)", ids, index=1, format_func=labels.get, key="ab_a")
    b_id = c2.selectbox("Scenario B (newer)", ids, index=0, format_func=labels.get, key="ab_b")
    a, b = next(s for s in saved if s["id"] == a_id), next(s for s in saved if s["id"] == b_id)
    if a_id == b_id:
        st.info("Choose two different what-ifs.")
        _render_saved_scenarios(saved, labels, role, user_id)
        return

    summary = pd.DataFrame({"Metric": list(scenario_summary(a)),
                            f"A – {a['name']}": [str(v) for v in scenario_summary(a).values()],
                            f"B – {b['name']}": [str(v) for v in scenario_summary(b).values()]})
    show_table(summary, hide_index=True, width="stretch")
    left, right = st.columns(2)
    for col, s, tag in ((left, a, "A"), (right, b, "B")):
        with col, st.container(border=True):
            verdict = (s.get("verdict") or {}).get("label") or "–"
            st.markdown(f"**{tag}: {s['name']}**  \n{verdict_badge(verdict)}")
            fig = waterfall_figure(s, f"Budget impact per project – {tag}")
            if fig is None:
                st.caption("No budget effect (e.g. a schedule or capacity change).")
            else:
                st.plotly_chart(fig, theme="streamlit", config=PLOTLY_CONFIG, width="stretch", key=f"ab_waterfall_{tag}")
            for condition in (s.get("verdict") or {}).get("conditions", [])[:4]:
                st.caption(f"Condition: {condition}")
            steps = s.get("turns") or []
            if steps:
                st.caption("Steps: " + " → ".join(steps))
    t_bars, t_diff, t_saved = st.tabs(["Budget impact A vs B", "Change-by-change differences", "Saved what-ifs"])
    with t_bars:
        fig = impact_bars_figure(impact_bars(a, b))
        if fig is not None:
            st.plotly_chart(fig, theme="streamlit", config=PLOTLY_CONFIG, width="stretch", key="ab_budget_deltas")
        else:
            st.caption("Neither what-if changes a budget.")
    with t_diff:
        diff = compare_frame(a, b)
        diff["field"] = diff["field"].map(field_label)
        show_table(diff.rename(columns={"project": "Project", "field": "Change", "A": f"A – {a['name']}",
                                          "B": f"B – {b['name']}", "differs": "Differs"}).astype(str),
                     hide_index=True, width="stretch")
    with t_saved:
        _render_saved_scenarios(saved, labels, role, user_id)
    render_ask_box(f"compare_{a_id}_{b_id}", "the comparison of two saved what-ifs (A and B)",
                   lambda: scenario_compare_facts(a, b, summary, compare_frame(a, b)), title="Ask about A vs B",
                   examples=("Where do A and B differ most?", "Which one has fewer conditions?"))


def _render_saved_scenarios(saved: list[dict], labels: dict[str, str], role: str, user_id: str) -> None:
    show_table(pd.DataFrame([{"What-if": s["name"], "Owner": s.get("owner"),
                                "Saved": timestamp(s.get("timestamp")),
                                "Verdict": status_text((s.get("verdict") or {}).get("label")),
                                "Changes": len(s.get("changes") or []), "Steps": len(s.get("turns") or [])}
                               for s in saved]), hide_index=True, width="stretch")
    own = [s for s in saved if s.get("owner") == user_id or role == ROLE_PMO]
    if not own:
        return
    pick, button = st.columns([3, 1], vertical_alignment="bottom")
    victim = pick.selectbox("Delete a saved what-if", ["—", *[s["id"] for s in own]],
                            format_func=lambda i: "—" if i == "—" else labels[i], key="ab_delete_pick")
    if victim != "—" and button.button("Delete scenario", icon=":material/delete:", key="ab_delete"):
        st.session_state["ab_delete_pending"] = victim  # irreversible, so an explicit confirmation is required
    pending = st.session_state.get("ab_delete_pending")
    if not (pending and pending == victim):
        return

    @st.dialog("Delete saved what-if", on_dismiss=lambda: st.session_state.pop("ab_delete_pending", None))
    def confirm() -> None:
        st.warning(f"Delete the saved what-if **{labels.get(pending, pending)}**? This cannot be undone.",
                   icon=":material/warning:")
        yes, no = st.columns(2)
        if yes.button("Yes, delete", type="primary", key="ab_delete_confirm", width="stretch"):
            st.session_state.pop("ab_delete_pending", None)
            try:
                delete_scenario(pending, user_id, role)
                audit("scenario_deleted", role=role, user_id=user_id, scenario_id=pending)
                notify("success", "Saved what-if deleted.")
            except ScenarioStoreError as exc:
                notify("error", str(exc))
            st.rerun()
        if no.button("Cancel", key="ab_delete_cancel", width="stretch"):
            st.session_state.pop("ab_delete_pending", None)
            st.rerun()

    confirm()
