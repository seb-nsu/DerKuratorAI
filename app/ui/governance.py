"""Rules page (PMO): the business rules with their bounds and the Data Steward's required fields. They belong to the
PMO Head, who changes them here; every other PMO account reads them. The core checks the PMO Head again
(update_runtime_config), the same rules as the /governance API of NiceGUI and React."""
from __future__ import annotations

import streamlit as st

from app.ui.admin_ui import render_business_rules, render_required_fields_settings
from app.ui.boundary import error_boundary
from app.ui.common import page_header
from app.ui.shell import is_pmo_head


def page_rules() -> None:
    editable = is_pmo_head()
    page_header("Rules", "Business rules and Data Steward required fields · they apply to every new analysis, "
                         "verdict and data check")
    if not editable:
        st.info("Read-only – only the PMO Head changes these rules.", icon=":material/lock:")
    with error_boundary("business rules"):
        render_business_rules(editable)
    with error_boundary("required fields"):
        render_required_fields_settings(editable)
