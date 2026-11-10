"""Planning page: demands (initiatives that are no project yet), their assessment and rank, planning scenarios
against the budget envelopes and capacity limits, and the approval of the annual plan.

Visibility as for projects: a Project Manager sees only the demands they own (no rank among foreign demands, no
foreign names); the PMO and the Portfolio Board see everything. All figures come from app/planning – deterministic."""
from __future__ import annotations

import logging
from collections.abc import Callable

import pandas as pd
import streamlit as st

from app.core import clock, proposals
from app.core.formatting import eur, field_label, timestamp
from app.core.proposals import ProposalError
from app.core.security import ROLE_BOARD, ROLE_PMO
from app.data_adapters import DataConflictError, DataSourceError, PortfolioDataset, get_data_provider
from app.data_adapters.base import WriteNotAuthorizedError
from app.data_adapters.dataset import DEMAND_KEY
from app.planning import analysis, approval, reports, store
from app.planning.model import horizon
from app.planning.scenario import METHODS, VALUE, ScenarioInput, ScenarioResult, run_scenario
from app.planning.scoring import criteria
from app.planning.steward import known_scopes
from app.ui.ask_box import render_ask_box
from app.ui.assist_facts import demand_facts, demands_facts, plan_facts, scenario_facts
from app.ui.boundary import error_boundary
from app.ui.common import (
    ACTIONS,
    PLOTLY_CONFIG,
    badge,
    load_dataset,
    notify,
    page_header,
    show_table,
    status_text,
    verdict_badge,
)
from app.ui.console import override_controls
from app.views import planning as views
from app.views.navigation import page_title

logger = logging.getLogger("derkurator.ui.planning")

DECISION_BADGES = {"In": ("green", ":material/check_circle:"), "Forced-in": ("violet", ":material/push_pin:"),
                   "Deferred": ("orange", ":material/schedule:"), "Out": ("red", ":material/block:")}
MAX_COMPARED = 3
OUTSIDE_SCOPE_PREDECESSORS = "Further predecessors lie outside your scope – the PMO coordinates."
STATUS_BADGES = {"Draft": "gray", "Submitted": "blue", "Assessed": "violet", "Planned": "orange", "Approved": "green",
                 "Deferred": "orange", "Rejected": "red", "Converted": "green"}


def base_plan(ds: PortfolioDataset) -> ScenarioResult:
    """The plan as it stands (rank order, today's data), calculated once per loaded dataset and day."""
    key = (id(ds), len(ds.demands), clock.today().isoformat())
    memo = st.session_state.get("plan_base")
    if not memo or memo[0] != key:
        with st.spinner("Calculating the base plan ..."):
            memo = (key, analysis.base_plan(ds))
        st.session_state["plan_base"] = memo
    return memo[1]


def _render_detail(ds: PortfolioDataset, demand: str, role: str) -> None:
    row = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand].iloc[0].to_dict()
    st.subheader(f"{demand} · {row.get('demand_name')}", anchor=False)
    st.markdown(" ".join([badge(row.get("demand_status") or "–", STATUS_BADGES.get(row.get("demand_status"), "gray")),
                          badge(row.get("demand_type") or "–", "red" if row.get("demand_type") == "Mandatory" else "gray"),
                          badge(f"{row.get('business_unit')} · {row.get('portfolio')}", "gray")]))
    st.caption(f"Owner: {row.get('demand_owner') or '–'}")
    c2, c3, c4 = st.columns(3)
    c2.metric("Duration", f"{row.get('duration_months') or 0:g} months", border=True)
    c3.metric("NPV", eur(float(row.get("net_present_value_eur") or 0)), border=True)
    c4.metric("Run cost after go-live", f"{eur(float(row.get('run_cost_after_go_live_eur') or 0))} p.a.", border=True)
    full = views.sees_whole_plan(role)
    plan = base_plan(ds) if full else None
    tabs = st.tabs(["Master data", "Annual figures", "Capacity", "Assessment", "Dependencies",
                    *(["In the plan"] if full else [])])
    with tabs[0]:
        master = views.master_data(row)
        show_table(master.assign(Field=master["Field"].map(field_label)) if not master.empty else master)
    with tabs[1]:
        plans = views.annual_figures(ds, demand)
        if plans.empty:
            st.caption("No annual figures.")
        else:
            show_table(plans, hide_index=True, width="stretch",
                column_config={c: st.column_config.NumberColumn(format="€%,.0f") for c in ("CapEx", "OpEx", "Benefit")})
            st.caption(f"Total {eur(plans['CapEx'].sum())} CapEx and {eur(plans['OpEx'].sum())} OpEx.")
    with tabs[2]:
        pivot = views.capacity_demand(ds, demand)
        if pivot.empty:
            st.caption("No capacity demand recorded.")
        else:
            show_table(pivot, column_config={c: st.column_config.NumberColumn(format="%.1f FTE")
                                             for c in pivot.columns if c != "Team"})
    with tabs[3]:
        parts = views.assessment_parts(ds, demand)
        if row.get("demand_type") == "Mandatory":
            st.info(f"Mandatory demand ({row.get('mandatory_reason') or 'reason missing'}) – marked, not scored; it "
                    "is planned before every scored demand.", icon=":material/gavel:")
        elif parts.empty:
            st.caption("Not assessed yet.")
        else:
            show_table(parts, column_config={c: st.column_config.NumberColumn(format="%.1f")
                                             for c in ("Score", "Points", "Weight", "Contribution")})
            st.caption(f"Score {parts['Contribution'].sum():.1f} of 100 = sum of the weighted contributions.")
    with tabs[4]:
        _render_chain(ds, demand, plan)
    if full:
        with tabs[5]:
            _render_position(ds, demand, plan)
    render_ask_box(f"demand_{demand}", f"demand {demand} in the annual planning",
                   lambda: demand_facts(ds, demand, plan, full), title="Ask about this demand",
                   examples=("What does this demand wait for?", "What happens if it starts a quarter later?"))


def _render_chain(ds: PortfolioDataset, demand: str, plan: ScenarioResult | None) -> None:
    """The whole chain – what the demand needs first and what waits for it, directly and indirectly – and what a delay
    would push along. A project manager's view names only the own projects and demands: other links are never named
    or counted."""
    chain = analysis.dependency_chain(ds, demand, plan)
    if chain.empty:
        st.caption("No dependencies within your view.")
    else:
        show_table(chain, hide_index=True, width="stretch",
                     column_config={"Steps": st.column_config.NumberColumn(help="1 = direct, 2 = via one item …")})
    if analysis.hidden_links(ds, demand):
        st.caption(OUTSIDE_SCOPE_PREDECESSORS)
    if not chain.empty and (chain["Direction"] == "Needed by").any():
        quarters = st.slider("If this demand slips by … quarters", 1, 4, 1, key=f"slip_{demand}")
        impact = analysis.slip_impact(ds, demand, quarters, plan)
        if impact.empty:
            st.caption("Nothing moves – every successor starts late enough.")
        else:
            show_table(impact, hide_index=True, width="stretch")
            late = int((impact["Still fits"] != "yes").sum())
            st.caption(f"{len(impact)} item(s) move; {late} would start after their latest start." if late else
                       f"{len(impact)} item(s) move; all still start before their latest start.")


def _render_position(ds: PortfolioDataset, demand: str, plan: ScenarioResult) -> None:
    """The demand's money and people next to the envelopes and team limits of the base plan."""
    item = plan.items[plan.items[DEMAND_KEY] == demand] if not plan.items.empty else plan.items
    if not item.empty:
        row = item.iloc[0]
        st.markdown(f"Base plan (rank order, today's data): **{row['decision']}**"
                    + (f" · {row['reason']}" if str(row.get("reason") or "").strip() else ""))
    budget = analysis.budget_position(ds, demand, plan)
    st.markdown("**Budget envelopes of its business unit and portfolio**")
    if budget.empty:
        st.caption("No annual figures or no envelopes.")
    else:
        show_table(budget, hide_index=True, width="stretch",
                     column_config={**{c: st.column_config.NumberColumn(format="€%,.0f")
                                       for c in ("Limit", "Committed", "Planned", "Free", "This demand")},
                                    "Used %": st.column_config.ProgressColumn(min_value=0, max_value=100,
                                                                              format="%.0f %%")})
        st.caption("Committed = running projects, Planned = demands in the plan. "
                   "If the demand is not in the plan, “This demand” shows what it would add.")
    capacity = analysis.capacity_position(ds, demand, plan)
    st.markdown("**Team capacity**")
    if capacity.empty:
        st.caption("No capacity demand recorded.")
    else:
        show_table(capacity, hide_index=True, width="stretch",
                     column_config={"Used %": st.column_config.ProgressColumn(min_value=0, max_value=100,
                                                                              format="%.0f %%")})


def _save_scores(ds: PortfolioDataset, demand: str, role: str, user_id: str) -> None:
    model = criteria(ds)
    scores = {c: float(st.session_state[f"score_{demand}_{i}"]) for i, c in enumerate(model["criterion"])}
    comments = {c: st.session_state.get(f"score_{demand}_{i}_comment", "") for i, c in enumerate(model["criterion"])}
    try:
        result = views.save_scores(ds, demand, scores, comments, user_id, role)
    except (ValueError, WriteNotAuthorizedError, DataConflictError, DataSourceError) as exc:
        notify("error", f"Assessment not saved: {exc}")
        return
    load_dataset.clear()
    notify("success", f"Assessment of {demand} saved ({result.applied} change(s), snapshot "
                                            f"{result.snapshot_id}).")


def _render_assessment(ds: PortfolioDataset, role: str, user_id: str) -> None:
    model = criteria(ds)
    if model.empty:
        st.info("No scoring model in the data (sheet Scoring_Model).")
        return
    open_ = views.assessable_demands(ds)
    if open_.empty:
        st.caption("No demand to assess.")
        return
    demand = st.selectbox("Demand", open_[DEMAND_KEY], key="assess_pick",
                          format_func=lambda d: f"{d} · {open_.set_index(DEMAND_KEY).at[d, 'demand_name']}")
    with st.form(f"assess_{demand}", border=True):
        for i, crit in enumerate(views.assessment_form(ds, demand)):
            c1, c2 = st.columns([1, 2])
            c1.number_input(f"{crit['criterion']} ({crit['direction']})", min_value=crit["scale_min"],
                            max_value=crit["scale_max"], value=crit["value"], step=1.0,
                            key=f"score_{demand}_{i}", help=f"Weight {crit['weight']:g}")
            c2.text_input("Comment", key=f"score_{demand}_{i}_comment", label_visibility="hidden",
                          placeholder="Comment (optional)")
        st.form_submit_button("Save assessment", type="primary", icon=":material/save:", on_click=_save_scores,
                              args=(ds, demand, role, user_id))
    st.caption("Each save appends new score rows; the latest assessment per criterion counts (history stays).")


def _submit_weights(ds: PortfolioDataset, role: str, user_id: str) -> None:
    edited = st.session_state.get("weights_edit_frame")
    if edited is None:
        return
    try:
        entry = views.submit_weights(ds, dict(zip(edited["criterion"], edited["weight"], strict=True)), user_id,
                                     role)
        if entry is None:
            notify("info", "No weight changed.")
            return
    except (ValueError, ProposalError) as exc:
        notify("error", f"Weights not submitted: {exc}")
        return
    notify("success", f"Weight change submitted as proposal {entry['id']} – a second PMO "
                                            "approves it in Approvals.")


def _render_weights(ds: PortfolioDataset, role: str, user_id: str) -> None:
    model = criteria(ds)
    st.caption("A weight change is a proposal: a second PMO approves it, the write journal records it.")
    edited = st.data_editor(model[["criterion", "weight", "scale_min", "scale_max", "direction"]], hide_index=True,
                            disabled=["criterion", "scale_min", "scale_max", "direction"], key="weights_editor",
                            width="stretch",
                            column_config={"criterion": "Criterion", "weight": st.column_config.NumberColumn(
                                "Weight", format="%.0f"), "scale_min": "Lowest score", "scale_max": "Highest score",
                                "direction": "Direction"})
    st.session_state["weights_edit_frame"] = edited
    st.button("Submit weight change", icon=":material/send:", on_click=_submit_weights, args=(ds, role, user_id),
              key="weights_submit")


def render_demands(ds: PortfolioDataset, role: str, user_id: str) -> None:
    table = views.demand_table(ds, role)
    if table.empty:
        st.info("No demands within your permissions.")
        return
    first, last = horizon()
    f1, f2, f3 = st.columns(3)
    units = f1.multiselect("Business unit", sorted(table["Business unit"].dropna().unique()), key="plan_f_unit")
    statuses = f2.multiselect("Status", sorted(table["Status"].dropna().unique()), key="plan_f_status")
    year = f3.selectbox("Has money in", ["any year"] + [str(y) for y in range(first, last + 1)], key="plan_f_year")
    shown = views.filter_demands(table, units, statuses, year)
    money = {str(y): st.column_config.NumberColumn(format="€%,.0f") for y in range(first, last + 1)}
    show_table(shown, hide_index=True, width="stretch",
                 column_config={**money, "Score": st.column_config.ProgressColumn(min_value=0, max_value=100,
                                                                                  format="%.1f")})
    if views.sees_whole_plan(role):
        st.caption("Rank: mandatory demands first, then score, then NPV, then the latest start, then the number.")
    issues = views.demand_issues(ds, role)
    if issues:
        with st.expander(f"Data quality of the demands · {len(issues)} finding(s)", icon=":material/fact_check:"):
            show_table(pd.DataFrame([{"Demand": i.project_id, "Severity": i.severity, "Field": i.column,
                                        "Finding": i.message} for i in issues]), hide_index=True, width="stretch")
    render_ask_box("demands", "the demands of the annual planning in the reader's scope",
                   lambda: demands_facts(ds, shown, issues), title="Ask about the demands",
                   examples=("Which demands are not assessed yet?", "Which demands need money in the first year?"))
    pick = st.selectbox("Open demand", [""] + list(shown["Demand"]), key="plan_detail",
                        format_func=lambda d: "Select a demand ..." if not d else f"{d} · "
                        f"{shown.set_index('Demand').at[d, 'Name']}")
    if pick:
        with st.container(border=True), error_boundary("demand detail"):
            _render_detail(ds, pick, role)


# --------------------------------------------------------------------------- scenario workshop
def _prefill_defaults(first: int, last: int, demands: set[str], scopes: dict) -> None:
    """Defaults of the scenario inputs – and a question box's pre-fill kept only where it fits the inputs (a
    demand that cannot be planned, a year or scope outside the choices fall back to the default)."""
    state = st.session_state
    state.setdefault("scn_name", "Base plan")
    state.setdefault("scn_factor", 0)
    (from_low, from_high), (to_low, to_high) = views.year_bounds(first, last)
    for key, default, low, high in (("scn_from", first, from_low, from_high), ("scn_to", last, to_low, to_high)):
        if not isinstance(state.get(key), int) or not low <= state[key] <= high:
            state[key] = default
    if state.get("scn_scope") and state["scn_scope"] not in {f"{k}:{v}" for k in ("business_unit", "portfolio")
                                                               for v in scopes.get(k, ())}:
        state["scn_scope"] = ""
    for key in ("scn_forced", "scn_excluded", "scn_later", "scn_earlier"):
        if key in state:
            state[key] = [d for d in state[key] if d in demands]


def _scenario_input(ds: PortfolioDataset) -> ScenarioInput:
    first, last = horizon()
    label = views.scenario_candidates(ds)
    _prefill_defaults(first, last, set(label), known_scopes(ds))
    c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
    name = c1.text_input("Scenario name", key="scn_name")
    (from_low, from_high), (to_low, to_high) = views.year_bounds(first, last)
    year_from = c2.number_input("From year", min_value=from_low, max_value=from_high, step=1, key="scn_from")
    year_to = c3.number_input("To year", min_value=to_low, max_value=to_high, step=1, key="scn_to")
    low, high = views.BUDGET_FACTOR_RANGE
    factor = c4.slider("Budget envelopes", min_value=low, max_value=high, step=views.BUDGET_FACTOR_STEP,
                       format="%+d %%", key="scn_factor",
                       help="Every envelope larger or smaller by this share (e.g. a budget cut).")
    options = views.scope_options(ds)
    c5, c6 = st.columns([3, 2])
    scope = c5.selectbox("Scope", options, key="scn_scope",
                         format_func=lambda o: "Whole portfolio" if not o else o.replace("_", " ").replace(":", ": "))
    method = c6.radio("Filling", list(METHODS), format_func=METHODS.get, horizontal=True, key="scn_method",
                      help="Rank order: one demand after the other, as long as it fits. Value-optimal: the combination "
                           "of demands with the highest score sum within the same limits (mandatory demands first).")
    with st.expander("PMO decisions: force in, exclude, move", icon=":material/tune:"):
        forced = st.multiselect("Force in (with a reason)", list(label), format_func=label.get, key="scn_forced")
        forced_reason = st.text_input("Reason for forcing in", key="scn_forced_reason",
                                      placeholder="e.g. board commitment, regulatory deadline")
        excluded = st.multiselect("Exclude", [d for d in label if d not in forced], format_func=label.get,
                                  key="scn_excluded")
        later = st.multiselect("Move one year later", [d for d in label if d not in excluded], format_func=label.get,
                               key="scn_later")
        earlier = st.multiselect("Move one year earlier", [d for d in label if d not in excluded and d not in later],
                                 format_func=label.get, key="scn_earlier")
    return views.build_scenario_input(name, year_from, year_to, factor, scope, forced, forced_reason, excluded, later,
                                      earlier, method)


def _render_verdict(result: ScenarioResult) -> None:
    v = result.verdict
    with st.container(border=True):
        st.markdown(f"{verdict_badge(v.label)} **{result.input.name}** · {result.years[0]}–{result.years[-1]} · "
                    f"binding year {result.binding_year}")
        for reason in v.reasons:
            st.markdown(f"- {reason}")
        conditions = v.condition_details()
        if conditions:
            show_table(pd.DataFrame([{"Condition": c["condition"], "Owner": c["owner_role"], "Due": c["due"]}
                                     for c in conditions]),
                       column_config={"Condition": st.column_config.TextColumn(width="large")})
        st.caption(f"Rule-based verdict (not AI-generated) · reference date {result.as_of.isoformat()}"
                   + (" · committed money partly estimated from the forecast to complete"
                      if result.estimated_commitments else ""))


def _render_items(result: ScenarioResult) -> None:
    if result.items.empty:
        st.caption("No demand in this scenario.")
        return
    counts = views.decision_counts(result)
    st.markdown(" ".join(badge(f"{counts[k]} {k}", *DECISION_BADGES[k]) for k in DECISION_BADGES))
    shown = views.scenario_items(result)
    money = [c for c in shown.columns if c.startswith(("CapEx ", "OpEx "))]
    show_table(shown, hide_index=True, width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="€%,.0f") for c in money})


def _render_optimizer(ds: PortfolioDataset, result: ScenarioResult) -> None:
    """What the value-optimal choice does differently from the rank order, and why."""
    with st.spinner("Calculating the rank order for the comparison ..."):
        summary, figures, changes = views.optimizer_comparison(ds, result)
    show_table(figures, hide_index=True, width="stretch",
                 column_config={"NPV planned": st.column_config.NumberColumn(format="€%,.0f")})
    for line in summary:
        st.markdown(f"- {line}")
    if not changes.empty:
        show_table(changes, hide_index=True, width="stretch")
    if not result.solved:
        st.warning("The optimizer found no solution in time – the rank order was used.", icon=":material/warning:")
    st.caption("Same limits, same bound money and capacity, same verdict rules – only the choice of the free demands "
               "differs. The reason of a deferral or an Out names the limit it would break.")


def _render_budget(result: ScenarioResult) -> None:
    budget = result.budget
    if budget.empty:
        st.info("No budget envelopes for these years (sheet Budget_Envelopes).")
        return
    money = ("Cap", "Limit", "Committed", "Planned", "Free")
    show_table(views.budget_lights(budget),
                 hide_index=True, width="stretch",
                 column_config={**{c: st.column_config.NumberColumn(format="€%,.0f") for c in money},
                                "Used %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f %%")})
    st.caption("Limit = cap minus the reserve. Committed = running projects (cost plans, rest of the forecast to "
               "complete) and approved demands; Planned = the demands of this scenario incl. run cost after go-live.")


def _render_capacity(result: ScenarioResult) -> None:
    grid = views.capacity_grid(result)
    if grid.empty:
        st.info("No capacity limits for these years (sheet Capacity_Limits).")
        return
    fig = views.capacity_heatmap(grid)
    st.plotly_chart(fig, theme="streamlit", config=PLOTLY_CONFIG, width="stretch", key=f"cap_heat_{result.input.name}")
    st.caption("Share of the usable FTE (available × maximum utilisation) per team and quarter, running projects "
               "included. Darker = fuller; above 100 % the team is overbooked.")


def _save_scenario(ds: PortfolioDataset, name: str, role: str, user_id: str) -> None:
    result = st.session_state.get("plan_results", {}).get(name)
    if result is None:
        return
    try:
        scenario_id, written = views.save_draft(ds, result, user_id, role)
    except (ValueError, WriteNotAuthorizedError, DataConflictError, DataSourceError) as exc:
        notify("error", f"Plan scenario not saved: {exc}")
        return
    load_dataset.clear()
    notify("success", f"Plan scenario “{name}” saved as draft {scenario_id} "
                                            f"({written.applied} row(s)).")


def render_scenarios(ds: PortfolioDataset, role: str, user_id: str) -> None:
    spec = _scenario_input(ds)
    results: dict[str, ScenarioResult] = st.session_state.setdefault("plan_results", {})
    if st.button("Calculate plan scenario", type="primary", icon=":material/calculate:", key="scn_run"):
        results.pop(spec.name, None)
        with st.spinner("Calculating the scenario ..."):
            results[spec.name] = run_scenario(ds, spec)
        while len(results) > MAX_COMPARED:
            results.pop(next(iter(results)))
        st.session_state["scn_show"] = spec.name  # show the scenario just calculated
    if not results:
        st.caption("Calculate a plan scenario: running projects bind their money and capacity first, then the demands fill "
                   "the envelopes in rank order – or value-optimal, as a second proposal. Up to three scenarios stay "
                   "side by side for comparison.")
        return
    if len(results) > 1:
        st.subheader("Comparison", anchor=False)
        frame = store.comparison(list(results.values()))
        show_table(frame, hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="€%,.0f") for c in frame.columns
                                    if c.startswith(("CapEx", "OpEx", "NPV"))})
    pick = (st.radio("Show scenario", list(results), index=len(results) - 1, horizontal=True, key="scn_show")
            if len(results) > 1 else next(iter(results)))
    result = results[pick]
    _render_verdict(result)
    st.button("Save as draft", icon=":material/save:", on_click=_save_scenario, args=(ds, pick, role, user_id),
              key="scn_save", help="Stores the input and the decision per demand; the approval follows separately "
                                   "(Saved plan scenarios › Submit to the Portfolio Board).")
    optimised = result.input.method == VALUE
    tabs = st.tabs(["Demands", "Budget", "Capacity", *(["Optimizer vs. rank order"] if optimised else [])])
    with tabs[0], error_boundary("scenario demands"):
        _render_items(result)
    with tabs[1], error_boundary("scenario budget"):
        _render_budget(result)
    with tabs[2], error_boundary("scenario capacity"):
        _render_capacity(result)
    if optimised:
        with tabs[3], error_boundary("scenario optimizer"):
            _render_optimizer(ds, result)
    render_ask_box(f"scenario_{pick}", "a planning scenario (rule-based annual plan) and its comparison",
                   lambda: _scenario_facts(ds, result, results), title="Ask about this plan scenario",
                   examples=("Why does the plan get this verdict?", "What drops out and why?"))


def _submit_plan(ds: PortfolioDataset, role: str, user_id: str) -> None:
    scenario = st.session_state.get("saved_pick")
    try:
        entry = approval.submit_plan(ds, scenario, user_id, role, get_data_provider())
    except (approval.PlanError, ProposalError, WriteNotAuthorizedError, DataConflictError, DataSourceError) as exc:
        notify("error", f"Plan not submitted: {exc}")
        return
    load_dataset.clear()
    notify("success", f"Scenario {scenario} submitted to the Portfolio Board (proposal "
                                            f"{entry['id']}, verdict {entry['verdict']}).")


def render_saved(ds: PortfolioDataset, role: str, user_id: str) -> None:
    saved = store.saved_scenarios(ds)
    if saved.empty:
        st.caption("No saved plan scenario yet. Calculate one under **Plan scenarios** and save it as a draft; it then appears here for the submission to the Portfolio Board.")
        return
    shown = saved[["scenario_id", "scenario_name", "year_from", "year_to", "scenario_status", "verdict", "created_by",
                   "created_on"]].assign(verdict=saved["verdict"].map(status_text))
    show_table(shown.rename(columns=lambda c: c.replace("_", " ").capitalize()))
    drafts = list(saved.loc[saved["scenario_status"] == store.DRAFT, "scenario_id"])
    if role == ROLE_PMO and drafts:
        st.selectbox("Draft", drafts, key="saved_pick",
                     format_func=lambda s: f"{s} · {saved.set_index('scenario_id').at[s, 'scenario_name']}")
        st.button("Submit to the Portfolio Board", icon=":material/send:", on_click=_submit_plan,
                  args=(ds, role, user_id), key="saved_submit",
                  help="Recalculated on today's data; the Portfolio Board approves it (four-eyes).")


def _convert(ds: PortfolioDataset, role: str, user_id: str) -> None:
    demand = st.session_state.get("convert_pick")
    try:
        entry = approval.submit_conversion(ds, demand, user_id, role)
    except (approval.PlanError, ProposalError) as exc:
        notify("error", f"Not converted: {exc}")
        return
    notify("success", f"Conversion of {demand} into {entry['changes'][0]['key']} submitted – a "
                                            "second PMO approves it in Approvals (it sets the first baseline).")


def render_conversion(ds: PortfolioDataset, role: str, user_id: str) -> None:
    d = ds.demands
    if d.empty:
        st.caption("No demands.")
        return
    approved, converted = views.convertible_demands(ds)
    if not converted.empty:
        show_table(converted, hide_index=True, width="stretch")
    if approved.empty:
        st.caption("No approved demand waits for its conversion into a project. Demands become convertible once the Portfolio Board approves a plan that contains them.")
        return
    st.selectbox("Approved demand", list(approved[DEMAND_KEY]), key="convert_pick",
                 format_func=lambda x: f"{x} · {approved.set_index(DEMAND_KEY).at[x, 'demand_name']}")
    st.button("Convert into a project", icon=":material/rocket_launch:", on_click=_convert, args=(ds, role, user_id),
              key="convert_submit", help="Master data 1:1, quarterly money → cost plans, capacity → resource plans, "
                                         "first baseline. A second PMO approves.")


def _decide_plan(proposal_id: str, approve: bool, role: str, user_id: str, override: bool = False,
                 reason: str = "") -> None:
    comment = reason or st.session_state.get(f"plan_comment_{proposal_id}", "")
    try:
        if approve:
            result = approval.approve_plan(proposal_id, user_id, role, get_data_provider(), comment=comment,
                                           override=override)
            message = ("success", f"Annual plan approved (operation {result.operation_id}, snapshot "
                                  f"{result.snapshot_id}).")
        else:
            approval.reject_plan(proposal_id, user_id, role, comment)
            message = ("info", "Annual plan sent back to the PMO.")
    except (ProposalError, WriteNotAuthorizedError, DataConflictError, DataSourceError) as exc:
        notify("error", f"Not decided: {exc}")
        return
    load_dataset.clear()
    st.session_state["flash"] = message


def _confirm_plan(p: dict, role: str, user_id: str) -> None:
    """Approving writes the plan to the data source: one explicit confirmation, a NO-GO plan only with an override
    and its reason (as in the PMO inbox)."""
    pending_key = f"plan_pending_{p['id']}"

    @st.dialog("Confirm plan approval", on_dismiss=lambda: st.session_state.pop(pending_key, None))
    def dialog() -> None:
        st.warning(f"Approve **{p['request']}** and write **{len(p['changes'])} plan row(s)** to the portfolio data "
                   "source now? A snapshot is taken first; the System Admin can restore it.", icon=":material/warning:")
        override = override_controls(f"plan_{p['id']}", p.get("verdict"))
        ready = override is None or (override[0] and bool(override[1].strip()))
        yes, no = st.columns(2)
        if no.button("Cancel", key=f"plan_cancel_{p['id']}", width="stretch"):
            st.session_state.pop(pending_key, None)
            st.rerun()
        label, icon = ACTIONS["approve_plan"]
        if yes.button(f"Confirm: {label.lower()}", icon=icon, key=f"plan_confirm_{p['id']}", type="primary",
                      disabled=not ready, width="stretch"):
            st.session_state.pop(pending_key, None)
            _decide_plan(p["id"], True, role, user_id, override=override is not None,
                         reason=override[1] if override else "")
            st.rerun()

    dialog()


def _board_start(ds: PortfolioDataset, pending: list[dict]) -> None:
    """The Board's landing: what waits, what is in force, how the plan stands today – and where to go next."""
    summary = views.board_summary(ds, base_plan(ds), pending)
    plan = summary["plan_in_force"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Waiting for your decision", summary["waiting"], border=True)
    c2.metric("Plan in force", plan["scenario_id"] if plan is not None else "none", border=True)
    c2.caption(f"“{plan['scenario_name']}” {plan['year_from']}–{plan['year_to']}" if plan is not None
               else "No annual plan approved yet")
    with c3.container(border=True):  # a metric would cut "CONDITIONAL GO"
        st.caption("Base plan today")
        st.markdown(f"**{status_text(summary['base_plan_verdict'])}**")
    c3.caption("Rank order on today's data")
    c4.metric("Demands", summary["demands"], border=True)
    c4.caption(f"{summary['in_base_plan']} in the base plan")
    if not pending:
        st.info("No annual plan waits for your decision. The PMO submits one from its saved plan scenarios. Meanwhile: "
                "**Analysis** shows what drops out and why, **Reports** has the board pack (Excel, print → PDF).",
                icon=":material/inbox:")


def render_plan_approval(ds: PortfolioDataset, role: str, user_id: str) -> None:
    pending = views.pending_plans()
    _board_start(ds, pending)
    for p in pending:
        with st.container(border=True):
            st.markdown(f"{verdict_badge(p.get('verdict'))} **{p['request']}** · submitted by {p['requester']} on "
                        f"{timestamp(p.get('timestamp'))[:10]}")
            st.markdown(p.get("paper") or "")
            with st.expander(f"Change set · {len(p['changes'])} row(s)"):
                show_table(views.plan_change_rows(p), hide_index=True, width="stretch")
            text = st.text_input("Comment (required to send the plan back)", key=f"plan_comment_{p['id']}")
            if proposals.requires_override(p.get("verdict")):
                st.caption(f":material/gpp_maybe: Rule-based verdict {p.get('verdict')} – approving needs an explicit "
                           "override with a reason.")
            c1, c2 = st.columns(2)
            label, icon = ACTIONS["approve_plan"]
            if c1.button(f"{label} …", type="primary", icon=icon, key=f"plan_ok_{p['id']}", width="stretch",
                         help="Opens a confirmation before the plan is written."):
                st.session_state[f"plan_pending_{p['id']}"] = True
            label, icon = ACTIONS["send_back"]
            c2.button(label, icon=icon, key=f"plan_back_{p['id']}", on_click=_decide_plan,
                      args=(p["id"], False, role, user_id), width="stretch", disabled=not text.strip(),
                      help=None if text.strip() else "Enter a comment first – sending back needs a reason.")
        if st.session_state.get(f"plan_pending_{p['id']}"):
            _confirm_plan(p, role, user_id)
    plan = store.approved_plan(ds)
    if plan is not None:
        st.caption(f"Approved plan in force: {plan['scenario_id']} “{plan['scenario_name']}” "
                   f"({plan['year_from']}–{plan['year_to']}), approved by {plan.get('approved_by') or '–'}.")


def render_reports(ds: PortfolioDataset) -> None:
    options = views.report_options(ds, st.session_state.get("plan_results", {}))
    pick = st.selectbox("Plan scenario", list(options), key="report_pick")
    result = run_scenario(ds, options[pick])
    compared = list(st.session_state.get("plan_results", {}).values())
    stamp = clock.today().isoformat()
    c1, c2 = st.columns(2)
    # files are built only when downloaded (callables), not on every rerun of the page
    c1.download_button("Board pack (Excel)", lambda: reports.board_pack_xlsx(ds, result, compared),
                       file_name=f"board_pack_{stamp}.xlsx", icon=":material/table_view:", key="report_xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    c2.download_button("Board pack (print → PDF)", lambda: reports.board_pack_html(ds, result),
                       file_name=f"board_pack_{stamp}.html", icon=":material/print:", key="report_html",
                       mime="text/html", width="stretch")
    st.caption("Excel: summary, ranking, decision per demand, budget, capacity, multi-year view, the comparison of "
               "the calculated scenarios and, for a value-optimal plan, the optimizer sheet. The HTML page prints to PDF in any browser.")
    multi = reports.multi_year(result)
    show_table(multi, column_config={c: st.column_config.NumberColumn(format="€%,.0f")
                                     for c in multi.columns if c != "Business unit"})


def _scenario_facts(ds: PortfolioDataset, result: ScenarioResult, results: dict[str, ScenarioResult]) -> str:
    return scenario_facts(ds, result, list(results.values()))


def render_analysis(ds: PortfolioDataset) -> None:
    """The plan as a whole: where envelopes and teams run full, and which demands others wait for."""
    plan = base_plan(ds)
    _render_verdict(plan)
    budget, capacity = analysis.hot_spots(plan)
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Budget hot spots** · envelopes used to {analysis.HOT_SHARE:.0f} % or more")
        if budget.empty:
            st.caption("None.")
        else:
            show_table(budget.drop(columns=["Cap"], errors="ignore"), hide_index=True, width="stretch",
                         column_config={c: st.column_config.NumberColumn(format="€%,.0f")
                                        for c in ("Limit", "Committed", "Planned", "Free")})
    with right:
        st.markdown(f"**Capacity hot spots** · team quarters used to {analysis.HOT_SHARE:.0f} % or more")
        if capacity.empty:
            st.caption("None.")
        else:
            show_table(capacity, hide_index=True, width="stretch")
    st.markdown("**Critical chains** · demands other demands or projects wait for – a delay here moves them too")
    chains = analysis.critical_chains(ds, plan)
    if chains.empty:
        st.caption("No demand has successors.")
    else:
        show_table(chains, hide_index=True, width="stretch")
    render_ask_box("plan_analysis", "the annual plan as it stands (rule-based base plan)",
                   lambda: plan_facts(ds, plan), title="Ask about the plan", expanded=True,
                   examples=("Where is the plan tightest?", "Which delay would hurt most?",
                             "What drops out in the first year and why?"))


PMO_TOOLS = {"assessment": "Assessment", "weights": "Scoring weights", "conversion": "Projects from demands"}


def _render_pmo_tools(ds: PortfolioDataset, role: str, user_id: str) -> None:
    """The PMO's maintenance work on demands – assessment, scoring weights, conversion into projects – in one tab."""
    tool = st.segmented_control("PMO tool", list(PMO_TOOLS), format_func=PMO_TOOLS.get, default="assessment",
                                key="pmo_tool", required=True, label_visibility="collapsed") or "assessment"
    {"assessment": _render_assessment, "weights": _render_weights, "conversion": render_conversion}[tool](
        ds, role, user_id)


def page_planning(ctx: dict) -> None:
    ds, role, user_id = ctx["ds"], ctx["role"], ctx["user_id"]
    first, last = horizon()
    page_header(page_title(role, "planning", "Planning"), f"Annual and multi-year planning {first}–{last} · {len(ds.demands)} demand(s) within your "
                            "permissions · ranks and verdicts are rule-based")
    sections: list[tuple[str, Callable[[], None]]] = [("Demands", lambda: render_demands(ds, role, user_id))]
    if role == ROLE_BOARD:
        sections = [("Plan approval", lambda: render_plan_approval(ds, role, user_id)), *sections,
                    ("Analysis", lambda: render_analysis(ds)),
                    ("Saved plan scenarios", lambda: render_saved(ds, role, user_id)), ("Reports", lambda: render_reports(ds))]
    elif role == ROLE_PMO:
        sections += [("Analysis", lambda: render_analysis(ds)),
                     ("Plan scenarios", lambda: render_scenarios(ds, role, user_id)),
                     ("Saved plan scenarios", lambda: render_saved(ds, role, user_id)),
                     ("Reports", lambda: render_reports(ds)),
                     ("PMO tools", lambda: _render_pmo_tools(ds, role, user_id))]
    tabs = st.tabs([name for name, _ in sections])
    for tab, (name, render) in zip(tabs, sections, strict=True):
        with tab, error_boundary(name.lower()):
            render()
    st.caption(f"Reference date {clock.today().isoformat()}.")
