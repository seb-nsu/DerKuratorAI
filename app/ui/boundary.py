"""Global UI error boundary: end users never see a Python traceback.

Every unexpected exception inside a guarded section is logged with its full traceback and a short incident ID
(data/app.log), recorded in the incident log (data/incidents.jsonl, not the decision log) without the exception
message (it may contain data values), and replaced in the UI by a neutral error with the incident ID. Only the System Admin additionally sees the traceback.

Streamlit's control-flow exceptions (st.rerun / st.stop) derive from BaseException and pass through untouched.
Set DERKURATOR_ERROR_BOUNDARY=raise to re-raise instead (used by the test suite so real bugs still fail tests).
"""
from __future__ import annotations

import logging
import os
import traceback
import uuid
from contextlib import contextmanager
from typing import Iterator

import streamlit as st

from app.core import logs

logger = logging.getLogger("derkurator.ui.errors")
RAISE_ENV = "DERKURATOR_ERROR_BOUNDARY"


def _is_admin() -> bool:
    from app.core.security import ROLE_ADMIN

    return st.session_state.get("role") == ROLE_ADMIN


def report_incident(section: str, exc: BaseException) -> str:
    """Logs and audits an incident and renders the user-facing message. Returns the incident ID; never raises."""
    incident = uuid.uuid4().hex[:8].upper()
    try:
        logger.error("UI incident %s in section '%s': %s", incident, section, exc.__class__.__name__,
                     exc_info=(type(exc), exc, exc.__traceback__))
        logs.append_jsonl(logs.INCIDENTS_LOG, {"event": "ui_error", "incident_id": incident, "section": section,
                                           "error_type": exc.__class__.__name__,
                                           "role": st.session_state.get("role"), "user_id": st.session_state.get("user_id")})
        st.error(f"A system error occurred (Incident ID: **{incident}**). Please contact your System Admin.",
                 icon=":material/error:")
        if _is_admin():
            with st.expander(f"Technical details for incident {incident} (visible to System Admin only)"):
                st.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), language="text")
    except Exception:  # the boundary itself must never fail
        logger.exception("Error boundary failed while reporting incident %s", incident)
    return incident


@contextmanager
def error_boundary(section: str) -> Iterator[None]:
    """Guards one UI section: an exception replaces only this section, the rest of the page keeps rendering."""
    try:
        yield
    except Exception as exc:  # that is the purpose of the boundary
        if os.getenv(RAISE_ENV, "").strip().lower() == "raise":
            raise
        report_incident(section, exc)
