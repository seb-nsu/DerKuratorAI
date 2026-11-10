"""The pages of each role and their order in the top navigation – one list for all three front ends (Streamlit,
NiceGUI, React). A page with a parent is a sub-page: it keeps its own URL (deep links), is opened from its parent page
and is not listed in the navigation, where its parent stays highlighted."""
from __future__ import annotations

from app.core.security import ROLE_ADMIN, ROLE_BOARD, ROLE_PM, ROLE_PMO

# page id, title, icon (Material Symbols name), parent page id or None
_PMO = (
    ("overview", "Overview", "dashboard", None),
    ("projects", "Projects", "folder_open", None),
    ("console", "Steering Console", "query_stats", None),
    ("approvals", "Approvals", "approval", None),
    ("planning", "Planning", "event_note", None),
    ("rules", "Rules", "rule", None),  # business rules and required fields; the PMO Head changes them
    ("data_quality", "Data quality", "fact_check", "projects"),
    ("scenarios", "What-if comparison", "compare_arrows", "console"),
    ("insights", "Decision insights", "insights", "overview"),  # status check, probabilities, decision review
)
# a Project Manager works on their own projects, proposals and demands: the titles say so
_PM = (
    ("overview", "Overview", "dashboard", None),
    ("projects", "My projects", "folder_open", None),
    ("console", "Steering Console", "query_stats", None),
    ("approvals", "My proposals", "approval", None),
    ("planning", "My demands", "event_note", None),
    ("data_quality", "Data quality", "fact_check", "projects"),
    ("scenarios", "What-if comparison", "compare_arrows", "console"),
)
_BOARD = (("planning", "Planning", "event_note", None),)  # the Portfolio Board reads the plan and approves it
_ADMIN = (("admin", "Admin Console", "admin_panel_settings", None),)
_BY_ROLE = {ROLE_PMO: _PMO, ROLE_PM: _PM, ROLE_BOARD: _BOARD, ROLE_ADMIN: _ADMIN}


def pages_for(role: str) -> list[dict]:
    """The pages of `role` in navigation order, sub-pages last: id, title, icon and parent (None = in the navigation)."""
    return [{"id": pid, "title": title, "icon": icon, "parent": parent}
            for pid, title, icon, parent in _BY_ROLE.get(role, ())]


def page_title(role: str, page_id: str, default: str) -> str:
    """The heading of a page for `role` – the same words as its navigation entry."""
    return next((title for pid, title, _, _ in _BY_ROLE.get(role, ()) if pid == page_id), default)


def scope_text(role: str, visible: int, total: int) -> str:
    """"2 projects" for a Project Manager (the size of the whole portfolio is not theirs to know), "11 of 11 projects"
    for the other roles."""
    if role == ROLE_PM:
        return f"{visible} project{'' if visible == 1 else 's'}"
    return f"{visible} of {total} projects"
