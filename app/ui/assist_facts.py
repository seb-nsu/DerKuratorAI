"""The facts of the page assistant, built in app/views/assist_facts.py (shared with the HTTP API) and re-exported
here for the Streamlit pages."""
from __future__ import annotations

from app.views.assist_facts import (  # noqa: F401 - re-exported for the pages and tests
    MAX_ROWS,
    data_quality_facts,
    demand_facts,
    demands_facts,
    join,
    overview_facts,
    plan_facts,
    project_facts,
    report_facts,
    scenario_compare_facts,
    scenario_facts,
    section,
    table,
)
