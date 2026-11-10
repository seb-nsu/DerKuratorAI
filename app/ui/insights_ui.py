"""Decision insights page (PMO, a sub-page of the overview): status check of the sources, the portfolio with
probabilities, the reference class of a project, the review of past decisions, the audit pack and the transfer to
ServiceNow. The figures come from app/views/insights.py."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core import clock
from app.core.formatting import eur_m
from app.ui.boundary import error_boundary
from app.ui.common import data_version, page_header, show_table
from app.ui.shell import go_to
from app.views import insights


@st.cache_data(ttl=600, show_spinner="Simulating the portfolio ...")
def _forecast(_ds, version: tuple, day: str) -> dict:
    return insights.portfolio_forecast(_ds)


def render_status_check(ds) -> None:
    result = insights.status_check(ds)
    st.caption(f"{result['mismatches']} project(s) reported better than their numbers · {result['stale']} without a "
               "current status report")
    rows = [{"Project": c["project_id"], "Name": c["project_name"], "Reported": c["reported"] or "–",
             "Calculated": c["calculated"], "Check": c["label"], "Why": "; ".join(c["reasons"]),
             "Source": c["source"] or "–"} for c in result["checks"]]
    show_table(pd.DataFrame(rows))


def render_forecast(ds) -> None:
    result = _forecast(ds, data_version(), clock.today().isoformat())
    summary = result["summary"]
    if not result["projects"]:
        st.info("No open project with a budget to simulate.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Within budget", f"{summary['probability_within_budget_pct']:g} %", border=True)
    c2.metric(f"Cost at {summary['confidence_pct']:g} %", eur_m(summary["p_conf_cost_eur"]), border=True)
    c3.metric("Reserve needed", eur_m(summary["reserve_needed_eur"]), border=True)
    st.caption(f"{summary['headline']} {summary['runs']} runs, seed {summary['seed']}.")
    show_table(pd.DataFrame([{"Project": p["project_id"], "Name": p["project_name"], "Budget": p["budget_eur"],
                              "Within budget %": p["probability_within_budget_pct"],
                              "On time %": p.get("probability_on_time_pct"),
                              "Compared with": f"{p['reference_size']} ({p['reference_basis']})"}
                             for p in result["projects"]]))


def render_reference(ds) -> None:
    options = ds.projects["project_number"].astype(str).tolist()
    pid = st.selectbox("Project", options, key="insights_reference_project")
    if not pid:
        return
    result = insights.reference(ds, pid)
    if result.get("headline"):
        st.write(result["headline"])
    if "buffer_eur" in result:
        st.caption(f"Budget at {result['confidence_pct']:g} % confidence: {eur_m(result['budget_at_confidence_eur'])} "
                   f"(buffer {eur_m(result['buffer_eur'])}" + (f", {result['buffer_days']} days" if "buffer_days" in
                                                                result else "") + ").")


def render_decision_review(ds, user_id: str, role: str) -> None:
    review = insights.decision_review(ds)
    st.caption(f"{review['due_for_review']} decision(s) due for review · {review['conditions_overdue']} condition(s) "
               "past their due date")
    if review["hit_rate"]:
        show_table(pd.DataFrame([{"Verdict": h["verdict"], "Decisions": h["decisions"], "Held": h["held"],
                                  "Hit rate %": h["hit_rate_pct"]} for h in review["hit_rate"]]))
    if not review["decisions"]:
        st.info("No approved decision yet.")
        return
    show_table(pd.DataFrame([{"Proposal": d["proposal_id"], "Decided": d["decided_on"], "Verdict": d["verdict"],
                              "Review": d["review_stage"] or f"next {d['next_review']}",
                              "Held": "–" if d["held"] is None else ("yes" if d["held"] else "no"),
                              "Outcome": "; ".join(o["text"] for o in d["outcomes"])} for d in review["decisions"]]))
    chosen = st.selectbox("Approved proposal", [d["proposal_id"] for d in review["decisions"]],
                          key="insights_pack_proposal")
    if chosen and st.button("Build audit pack", icon=":material/inventory_2:", key="insights_build_pack"):
        name, data = insights.audit_pack(chosen, user_id, role, ds)
        st.session_state["insights_pack"] = (name, data)
    if st.session_state.get("insights_pack"):
        name, data = st.session_state["insights_pack"]
        st.download_button(f"Download {name}", data, file_name=name, mime="application/zip",
                           icon=":material/download:", key="insights_pack_download")


def render_transfers() -> None:
    rows = insights.servicenow_transfers()
    if not rows:
        st.caption("No transfer to ServiceNow yet (write-back is off unless the mapping and the host switch it on).")
        return
    show_table(pd.DataFrame([{"Proposal": r.get("proposal_id"), "Project": r.get("key"), "Field": r.get("field"),
                              "Value": r.get("value"), "Status": r.get("status"), "Check": r.get("check"),
                              "Note": r.get("reason", "")} for r in rows]))


def page_insights(ctx: dict) -> None:
    if st.button("Back to the overview", icon=":material/arrow_back:", key="insights_back", type="tertiary"):
        go_to("overview")
    ds = ctx["ds"]
    page_header(insights.PAGE_TITLE, "Independent of the source tools · deterministic, no AI")
    st.subheader("Status check of the sources", anchor=False)
    with error_boundary("status check"):
        render_status_check(ds)
    st.subheader("Portfolio with probabilities", anchor=False)
    with error_boundary("portfolio forecast"):
        render_forecast(ds)
    st.subheader("Reference class of a project", anchor=False)
    with error_boundary("reference class"):
        render_reference(ds)
    st.subheader("Review of past decisions", anchor=False)
    with error_boundary("decision review"):
        render_decision_review(ds, ctx["user_id"], ctx["role"])
    st.subheader("Transfers to ServiceNow", anchor=False)
    with error_boundary("servicenow transfers"):
        render_transfers()
