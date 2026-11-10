"""Shared UI building blocks: cached resources, styling, flash messages, audit helpers."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from app import approval_flow
from app.analysis_runs import data_version  # noqa: F401 - re-exported for the pages
from app.core.config import PROJECT_ROOT
from app.core.formatting import (  # noqa: F401
    field_label,
    field_labels,
    format_delta,
    format_value,
    rule_label,
    status_text,
)
from app.data_adapters import PortfolioDataset, get_data_provider
from app.observability.tracer import init_tracing
from app.views.approvals import STATUS_LABELS  # noqa: F401 - proposal states as business words, shared with the API
from app.views.charts import SERIES_COLOR, STATUS_COLORS, STATUS_SYMBOLS  # noqa: F401 - re-exported for the pages
from app.views.exports import CHANGE_COLUMNS, change_rows  # noqa: F401
from app.views.projects import RESTRICTED_LABEL, project_names  # noqa: F401 - shared with the API
from app.views.reports import FOLLOW_ON_FIELDS, business_change_rows, decision_count  # noqa: F401 - shared with the API

logger = logging.getLogger("derkurator.ui")

# Business labels for data fields – business users never see snake_case database names (app/core/formatting.py)
FIELD_LABELS: dict[str, str] = field_labels()


def business_changes(changes: list[dict]) -> pd.DataFrame:
    """A change set as a business table (Project · Change · Current · Proposed · Note) – no table / tool internals."""
    return pd.DataFrame(business_change_rows(changes), columns=["Project", "Change", "Current", "Proposed", "Note"])


def title_columns(frame: pd.DataFrame, labels: dict[str, str] | None = None) -> pd.DataFrame:
    """snake_case column headers -> readable titles ("status_date" -> "Status date"); explicit labels win."""
    labels = labels or {}
    return frame.rename(columns={c: labels.get(c) or str(c).replace("_", " ").capitalize() for c in frame.columns})

_SNAKE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]00:00:00$")


def _readable_cell(value):
    if value is None or value is pd.NA or value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return "–"
    if isinstance(value, str):
        return "–" if value.strip().lower() in ("none", "nan", "nat", "") else _MIDNIGHT_RE.sub(r"\1", value)
    if isinstance(value, pd.Timestamp) and value == value.normalize():
        return value.date()
    return value


def readable_frame(frame: pd.DataFrame, keep: set[str] | frozenset[str] = frozenset()) -> pd.DataFrame:
    """A table for business readers: field names as labels, empty cells as "–", midnight timestamps as dates.
    Columns named in `keep` (they have a column_config) keep their name."""
    if frame is None or frame.empty:
        return frame
    out = frame.copy()
    for col in out.columns:
        series = out[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            if (series.dropna() == series.dropna().dt.normalize()).all():
                out[col] = series.dt.date
        elif pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            out[col] = series.astype(object).map(_readable_cell)
    return out.rename(columns={c: field_label(c) for c in out.columns
                               if isinstance(c, str) and c not in keep and _SNAKE_RE.fullmatch(c)})


def show_table(frame: pd.DataFrame, column_config: dict | None = None, **kwargs):
    """st.dataframe for business tables: readable headers and cells (readable_frame), no index, full width."""
    kwargs.setdefault("hide_index", True)
    kwargs.setdefault("width", "stretch")
    return st.dataframe(readable_frame(frame, set(column_config or {})), column_config=column_config, **kwargs)


PLOTLY_CONFIG = {"displayModeBar": False}

# Status badges (Streamlit markdown badge directives): shape-bearing icon + label, never colour alone
VERDICT_BADGES = {  # verdict_key -> (badge colour, icon)
    "GO": ("green", ":material/check_circle:"),
    "CONDITIONAL": ("orange", ":material/change_history:"),
    "NO-GO": ("red", ":material/block:"),
    "INCOMPLETE": ("gray", ":material/help:"),
    "NONE": ("gray", ":material/remove:"),
}
RAG_BADGES = {"Green": ("green", ":material/circle:"), "Yellow": ("orange", ":material/change_history:"),
              "Red": ("red", ":material/square:")}


# One label and one icon per business action, on every page (button text, confirm dialogs, help texts)
ACTIONS = {
    "apply": ("Approve & apply", ":material/task_alt:"),
    "handover": ("Approve & record hand-over", ":material/assignment_turned_in:"),
    "reject": ("Reject", ":material/close:"),
    "submit": ("Submit for approval", ":material/send:"),
    "withdraw": ("Withdraw", ":material/undo:"),
    "revert": ("Revert", ":material/history:"),
    "approve_plan": ("Approve the plan", ":material/verified:"),
    "send_back": ("Send back", ":material/reply:"),
    "save": ("Save", ":material/save:"),
    "reset": ("Reset", ":material/restart_alt:"),
    "rebuild": ("Rebuild", ":material/refresh:"),
    "open": ("Open", ":material/open_in_new:"),
}



def badge(label: str, colour: str = "gray", icon: str = "") -> str:
    """Markdown for a status pill. Square brackets in the label are escaped so the directive never breaks."""
    text = str(label).replace("[", "(").replace("]", ")")
    return f":{colour}-badge[{icon + ' ' if icon else ''}{text}]"


def verdict_badge(label: str | None) -> str:
    from app.ui.steerco_pack import verdict_key

    colour, icon = VERDICT_BADGES[verdict_key(label)]
    return badge(label or "No verdict", colour, icon)


def rag_badge(status) -> str:
    colour, icon = RAG_BADGES.get(str(status), ("gray", ""))
    return badge(str(status or "–"), colour, icon)


def confirm_button(label: str, key: str, action: Callable[..., Any], message: str, args: tuple = (),
                   icon: str | None = None, confirm_label: str | None = None, **button_kwargs) -> None:
    """A button for an action that discards settings or data: opens one confirmation dialog (what is lost, Cancel /
    confirm) and only then runs `action(*args)`. Confirm key: f"{key}_confirm"."""
    pending = f"{key}_pending"
    if st.button(f"{label} …", key=key, icon=icon, **button_kwargs):
        st.session_state[pending] = True
    if not st.session_state.get(pending):
        return

    @st.dialog(f"Confirm: {label}", on_dismiss=lambda: st.session_state.pop(pending, None))
    def dialog() -> None:
        st.warning(message, icon=":material/warning:")
        yes, no = st.columns(2)
        if no.button("Cancel", key=f"{key}_cancel", width="stretch"):
            st.session_state.pop(pending, None)
            st.rerun()
        # as an on_click callback: the action may reset widget state of the page (settings forms)
        yes.button(confirm_label or label, key=f"{key}_confirm", type="primary", icon=icon, width="stretch",
                   on_click=_run_confirmed, args=(pending, action, args))

    dialog()


def _run_confirmed(pending: str, action: Callable[..., Any], args: tuple) -> None:
    st.session_state.pop(pending, None)
    action(*args)


VERDICT_CALLOUTS = {"GO": st.success, "CONDITIONAL": st.warning, "NO-GO": st.error, "INCOMPLETE": st.info}


def verdict_callout(label: str | None, reasons: list[str] | None = None) -> None:
    """The rule-based verdict as the report's main element: a wide callout with the first reason."""
    from app.ui.steerco_pack import verdict_key

    key = verdict_key(label)
    if key not in VERDICT_CALLOUTS:
        return
    from app.crew.verdict import lead_reason

    first = lead_reason(reasons)
    VERDICT_CALLOUTS[key](f"**Recommendation: {label}**" + (f" – {first}" if first else ""),
                          icon=VERDICT_BADGES[key][1])


def page_header(title: str, caption: str = "") -> None:
    """Uniform page head: title + scope line (the primary action, if any, sits in the page body's right column)."""
    st.header(title, anchor=False)
    if caption:
        st.caption(caption)


_CSS = """
<style>
#MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stAppDeployButton"], [data-testid="stDecoration"]
{ display: none !important; visibility: hidden !important; }
[data-testid="stCaptionContainer"] { opacity: 1; color: #4a5566; }
[data-testid="stHeaderActionElements"] { display: none; }
:focus-visible { outline: 2px solid #1F3A5F !important; outline-offset: 2px; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 8px; }
div[data-testid="stMetric"] { padding: 6px 10px; }
.block-container { padding-top: 4.6rem; max-width: 1480px; }
[class*="st-key-report_"] div[data-testid="stMetricValue"] { font-size: 1.6rem; }
h2 { font-weight: 600 !important; letter-spacing: -0.01em; }
div[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
.st-key-app_header { border-bottom: 1px solid rgba(128,128,128,0.25); padding-bottom: 6px; margin-bottom: 4px; }
.st-key-report_viewer div[data-testid="stTabs"] button p { font-weight: 500; }
</style>
"""


def inject_css() -> None:
    """Hides the Streamlit main menu, footer and branding; subtle card styling."""
    st.html(_CSS)


@st.cache_resource(show_spinner="Starting observability (OpenTelemetry export) ...")
def get_tracing_status():
    return init_tracing()


@st.cache_data(ttl=60, show_spinner="Loading portfolio data ...")
def load_dataset() -> PortfolioDataset:
    return get_data_provider().load_dataset()


@st.cache_data(ttl=30, show_spinner=False)
def graph_status() -> tuple[bool, str]:
    try:
        from app import services

        return services.graph_connection()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@st.cache_data(ttl=60, show_spinner=False)
def top_risks(allowed: tuple[str, ...], db_mtime: float) -> tuple[list[dict], str | None]:
    """Top blocked projects / systemic risks from the dependency graph (cached per scope and graph version)."""
    try:
        from app import services

        return services.top_portfolio_risks(set(allowed)), None
    except Exception as exc:  # noqa: BLE001 - the dashboard must render even without the graph
        logger.warning("Top risks unavailable: %s", exc)
        return [], str(exc)


DQ_OK = "OK"  # data-quality status of a project without findings (table value, filter key)


def graph_version() -> int:
    """Changes with every rebuild of the dependency graph (cache key)."""
    from app.graph.portfolio_graph import graph_version as version

    return version()


FLASH_ICONS = {"success": ":material/check_circle:", "info": ":material/info:", "warning": ":material/warning:",
               "error": ":material/error:"}


def notify(kind: str, text: str) -> None:
    """Feedback after an action. Shown at the top of the next page run (kept for navigation via go_to) and as a toast,
    so it is seen wherever the user is scrolled. Kinds: success = an action completed (incl. reject, withdraw,
    delete), warning = action needed, error = the action failed or data is at risk."""
    st.session_state["flash"] = (kind, text)


def show_flash() -> None:
    if st.session_state.get("flash"):
        kind, text = st.session_state["flash"]
        icon = FLASH_ICONS.get(kind, ":material/info:")
        getattr(st, kind, st.info)(text, icon=icon)
        st.toast(text, icon=icon, duration="long" if kind in ("error", "warning") else "short")
        st.session_state["flash"] = None


def display_path(path: Path) -> str:
    """Path relative to the project – without crashing if it lies outside (e.g. a configured log location)."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


audit = approval_flow.audit  # one audit writer for every front end
