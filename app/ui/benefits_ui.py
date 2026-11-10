"""Benefits tab of the project page: planned benefit tranches against the actual benefits reported after go-live,
and the form to report an actual benefit (an appended Benefit_Actuals row – no proposal needed, like a status
report). The calculation is the one of the benefit realisation review (app.crew.tools.benefit_tools)."""
from __future__ import annotations

import logging
from typing import Any, Callable

import streamlit as st

from app.core import clock
from app.core.business_rules import current_rules
from app.core.formatting import eur
from app.data_adapters.dataset import BASE_CURRENCY, PortfolioDataset
from app.ui.common import load_dataset, notify, show_table
from app.views.projects import (
    benefit_actual_change,
    benefit_currencies,
    benefit_frame,
    benefit_plan_options,
    realisation_colour,
    report_benefit,
)

logger = logging.getLogger("derkurator.ui.benefits")


def _report(pid: str, ds: PortfolioDataset, role: str, user_id: str) -> None:
    key = f"benefit_{pid}"
    change = benefit_actual_change(pid, ds, user_id, st.session_state.get(f"{key}_plan"),
                                   st.session_state.get(f"{key}_month"),
                                   float(st.session_state.get(f"{key}_amount") or 0),
                                   st.session_state.get(f"{key}_currency"), st.session_state.get(f"{key}_comment"))
    kind, message, result = report_benefit(change, role, user_id)
    if result is not None:
        load_dataset.clear()
    notify(kind, message)


def render_benefits_tab(pid: str, project: dict[str, Any], ds: PortfolioDataset, role: str, user_id: str,
                        can_write: bool, on_scenario: Callable[[str, str, dict[str, Any]], None] | None) -> None:
    frame, result = benefit_frame(pid, project, ds)
    rules = current_rules()
    if frame.empty:
        st.caption("No planned benefit recorded.")
    else:
        show_table(frame, hide_index=True, width="stretch")
        if result["pct"] is None:
            st.caption("No benefit tranche is due yet.")
        else:
            colour = realisation_colour(result["pct"], rules)
            st.markdown(f":{colour}[**{result['pct']:g} % realised**] – {eur(result['actual'])} of "
                        f"{eur(result['planned_due'])} due (corrective plan below "
                        f"{rules.benefit_realization_min_pct:g} %).")
    currency = str(project.get("currency") or BASE_CURRENCY)
    if currency != BASE_CURRENCY:
        st.caption(f"Amounts of this project are kept in {currency} and shown in EUR (rate {project.get('fx_rate')}).")
    if on_scenario is not None and st.button("Review benefit realisation", icon=":material/insights:",
                                             key=f"detail_benefit_{pid}"):
        on_scenario(pid, "benefit_review", {})
    if not can_write:
        return
    with st.expander("Report an actual benefit", icon=":material/add_chart:"):
        options = benefit_plan_options(pid, ds)
        key = f"benefit_{pid}"
        a, b, c = st.columns(3)
        a.selectbox("Tranche", options, key=f"{key}_plan", format_func=lambda x: x or "not assigned")
        b.number_input("Actual benefit", min_value=0.0, value=0.0, step=10_000.0, key=f"{key}_amount")
        c.selectbox("Currency", benefit_currencies(ds), key=f"{key}_currency")
        d, e = st.columns([1, 2])
        d.date_input("Month", value=clock.today(), key=f"{key}_month")
        e.text_input("Comment", key=f"{key}_comment")
        st.button("Save actual benefit", icon=":material/save:", key=f"{key}_save", type="primary",
                  on_click=_report, args=(pid, ds, role, user_id))
