"""DerKurator AI – Enterprise Portfolio SteerCo Copilot (Streamlit entry point).

Start:  python -m streamlit run app/main.py      (server settings: .streamlit/config.toml, localhost only)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="DerKurator AI", page_icon=":material/insights:", layout="wide")

from app.core.config import PROJECT_ROOT as APP_ROOT  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logs import setup_logging  # noqa: E402
from app.core.security import ROLE_ADMIN  # noqa: E402
from app.data_adapters import DataSourceError  # noqa: E402
from app.ui.admin_ui import render_admin_dashboard, sync_admin_state_from_config  # noqa: E402
from app.ui.boundary import error_boundary  # noqa: E402
from app.ui.common import get_tracing_status, graph_status, inject_css, load_dataset, show_flash  # noqa: E402
from app.ui.shell import build_navigation, render_header, sign_in_navigation, signed_in_user  # noqa: E402
from app.ui.workspace import workspace_pages  # noqa: E402

setup_logging()  # idempotent; appends INFO and above to data/app.log, including Streamlit and the agents
logger = logging.getLogger("derkurator.ui")
inject_css()


def init_session_state() -> None:
    """Initialise session-state defaults on first run (avoids KeyErrors) and sync with the runtime config."""
    for key, default in {"messages": [], "identity": None, "flash": None, "role": None, "user_id": None}.items():
        st.session_state.setdefault(key, default)
    sync_admin_state_from_config()


bootstrapped = False
with error_boundary("startup"):  # never show raw tracebacks to end users
    init_session_state()
    settings = get_settings()
    get_tracing_status()

    try:
        full_ds = load_dataset()
    except DataSourceError as exc:
        # Paths and raw exception text go to the system log only; end users see a neutral message
        logger.error("Data source unavailable (DATA_SOURCE=%s, JSON_PATH=%s, EXCEL_PATH=%s): %s",
                     settings.data_source, settings.json_path, settings.excel_path, exc)
        st.error("**The portfolio data source is currently unavailable.** Please contact your System Admin.")
        st.stop()
    except Exception:
        logger.exception("Unexpected error while loading data")
        st.error("The portfolio data could not be loaded. Please contact your System Admin.")
        st.stop()

    st.logo(str(APP_ROOT / "app" / "ui" / "assets" / "logo.svg"), size="medium")
    user = signed_in_user()
    if user is not None:
        role, user_id, ds = render_header(full_ds, user)
        show_flash()
    bootstrapped = True
if not bootstrapped:
    st.stop()

if user is None:  # without a sign-in, only the sign-in page is available
    sign_in_navigation().run()
    st.stop()

if role == ROLE_ADMIN:
    navigation = build_navigation(role, {"admin": ("Admin Console", ":material/admin_panel_settings:", render_admin_dashboard)})
    with error_boundary("admin console"):
        navigation.run()
else:
    # The graph is built automatically on first access; this notice appears only if that fails
    graph_ok, graph_msg = graph_status()
    if not graph_ok:
        st.warning("The dependency graph could not be built from the portfolio data. Please ask the System Admin to "
                   "check the data source, otherwise the dependency analyses will fail.")
        logger.warning("Graph database not ready for the workspace: %s", graph_msg)  # details for the System Admin only
    navigation = build_navigation(role, workspace_pages(ds, full_ds, role, user_id))
    with error_boundary("portfolio workspace"):
        navigation.run()
    # LLM costs are an IT controlling concern: visible only in the Admin Console (FinOps panel), never to PMO or PM
