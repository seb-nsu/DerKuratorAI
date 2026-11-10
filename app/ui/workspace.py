"""Portfolio workspace (PMO / Project Manager / Portfolio Board): the page set and its shared context per run. The pages live in
overview.py, console.py, approvals_ui.py, projects_ui.py and planning_ui.py."""
from __future__ import annotations

import logging

import streamlit as st

from app.core import clock
from app.core.proposals import SUBMITTED, list_proposals
from app.core.required_fields import load_required_fields
from app.crew.tools.data_steward_tools import DataQualityReport, run_all_checks
from app.data_adapters import PortfolioDataset
from app.ui.approval_governance import is_approver
from app.ui.approvals_ui import page_approvals, pmo_inbox
from app.ui.common import DQ_OK, data_version
from app.ui.console import page_console
from app.ui.executive import render_scenario_compare
from app.ui.insights_ui import page_insights
from app.ui.overview import page_data_quality, page_overview, team_overload
from app.ui.projects_ui import page_projects
from app.ui.shell import go_to
from app.views.navigation import pages_for

logger = logging.getLogger("derkurator.ui.workspace")


@st.cache_resource(max_entries=32, show_spinner=False)
def cached_checks(_ds: PortfolioDataset, version: tuple, scope: tuple[str, ...], required: tuple[str, ...],
                  day: str) -> DataQualityReport:
    """Data Steward checks once per data state: data source version, RLS project set, required-field config and
    day (date rules). Saving, approving or reloading changes the version, so the result is current at once."""
    return run_all_checks(_ds)


def workspace_pages(ds: PortfolioDataset, full_ds: PortfolioDataset, role: str, user_id: str) -> dict:
    """Page id -> (title, icon, callable, visibility) of the role. All pages share one context per run."""
    ctx: dict = {"ds": ds, "full_ds": full_ds, "role": role, "user_id": user_id}
    if not ds.projects.empty:
        view = ds.project_view()
        steward = cached_checks(ds, data_version(), tuple(sorted(ds.project_numbers)), tuple(load_required_fields()),
                                clock.today().isoformat())
        view["data_quality"] = view["project_number"].map(steward.project_status()).fillna(DQ_OK)
        ctx.update(view=view, steward=steward, teams=team_overload(ds))

    def guarded(fn, name: str):
        def page() -> None:
            if ds.projects.empty:
                st.warning("No projects are released for your role.")
                return
            fn(ctx)
        page.__name__ = name  # st.Page identifies callable pages by name
        return page

    pending = len(pmo_inbox(list_proposals(status=SUBMITTED))) if is_approver(role) else 0
    renderers = {
        "overview": guarded(page_overview, "page_overview"),
        "console": guarded(page_console, "page_console"),
        "approvals": guarded(page_approvals, "page_approvals"),
        "projects": guarded(page_projects, "page_projects"),
        "scenarios": guarded(page_scenarios, "page_scenarios"),
        "planning": _planning_page(ctx),
        "data_quality": guarded(page_data_quality, "page_data_quality"),
        "rules": _rules_page(),
        "insights": guarded(page_insights, "page_insights"),
    }
    pages = {}
    for page in pages_for(role):  # the same pages, titles and order as the API gives NiceGUI and React
        title = f"{page['title']} ({pending})" if page["id"] == "approvals" and pending else page["title"]
        pages[page["id"]] = (title, f":material/{page['icon']}:", renderers[page["id"]],
                             "hidden" if page["parent"] else "visible")
    return pages

def page_scenarios(ctx: dict) -> None:
    """What-if comparison, a sub-page of the Steering Console."""
    if st.button("Back to the Steering Console", icon=":material/arrow_back:", key="scenarios_back", type="tertiary"):
        go_to("console")
    render_scenario_compare(ctx["role"], ctx["user_id"])


def _rules_page():
    from app.ui.governance import page_rules

    def page_rules_view() -> None:  # the rules need no project data
        page_rules()
    return page_rules_view


def _planning_page(ctx: dict):
    from app.ui.planning_ui import page_planning

    def page_planning_view() -> None:  # demands exist before projects do: no project guard
        page_planning(ctx)
    return page_planning_view
