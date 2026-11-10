"""App shell: header bar (environment, sign-in, scope, data reload) and the role-based page navigation.

The header is rendered by app/main.py before `st.navigation(...).run()`, so it appears above every page. Pages are
plain callables; `go_to(page_id)` switches page from code (e.g. "Simulate scenario for this project").
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import streamlit as st

from app.core import auth, clock
from app.core.chat_store import load_chat
from app.core.runtime_config import get_runtime_config
from app.core.security import ROLE_ADMIN, ROLE_BOARD, ROLE_PM, ROLE_PMO, apply_rls_to_dataset, log_security_event
from app.ui.common import badge, graph_status, load_dataset
from app.views.navigation import scope_text

logger = logging.getLogger("derkurator.ui")

NAV_KEY = "nav_page"  # page id the navigation opens when the URL names none (tests and go_to set it)
ENV_BADGES = {"DEV": "gray", "TEST": "orange", "PROD": "red"}
ROLE_LABELS = {ROLE_ADMIN: "System Admin", ROLE_PMO: "PMO", ROLE_PM: "Project Manager", ROLE_BOARD: "Portfolio Board"}


def go_to(page_id: str) -> None:
    """Switch to another page from code. Works in the browser (URL changes) and under AppTest (default page)."""
    st.session_state[NAV_KEY] = page_id
    pages = st.session_state.get("_pages") or {}
    if page_id in pages:
        st.switch_page(pages[page_id])
    st.rerun()


# =========================================================================== sign-in
AUTH_KEY = "auth_user"  # {"user_id", "role", "display_name", "demo", "pmo_head"} of the signed-in identity
ACTIVITY_KEY = "auth_last_activity"


def signed_in_user() -> dict | None:
    """The signed-in identity, or None. Signs out after `session_timeout_minutes` without activity."""
    user = st.session_state.get(AUTH_KEY)
    if user and user.get("demo") and not auth.demo_allowed():  # e.g. the System Admin switched this instance to PROD
        log_security_event("Demo account signed out – not allowed in this environment", level=logging.WARNING,
                           user=user["user_id"], role=user["role"])
        sign_out(reason=None)
        return None
    if user and auth.session_expired(st.session_state.get(ACTIVITY_KEY)):
        log_security_event("Session timed out", user=user["user_id"], role=user["role"])
        sign_out(reason=None)
        st.session_state["login_notice"] = "Your session expired – please sign in again."
        return None
    if user:
        st.session_state[ACTIVITY_KEY] = time.time()
    return user


def is_pmo_head() -> bool:
    """The signed-in identity owns the business rules and required fields (the Rules page)."""
    return bool((st.session_state.get(AUTH_KEY) or {}).get("pmo_head"))


def sign_out(reason: str | None = "Logout") -> None:
    user = st.session_state.get(AUTH_KEY)
    if user and reason:
        log_security_event(reason, user=user["user_id"], role=user["role"])
    for key in (AUTH_KEY, ACTIVITY_KEY, "identity", "role", "user_id", "selected_run", "detail_pid"):
        st.session_state.pop(key, None)
    st.query_params.pop("project", None)  # a project link never carries over to the next identity
    st.session_state["messages"] = []


def _submit_sign_in() -> None:
    try:
        result = auth.sign_in(st.session_state.get("login_user", ""), st.session_state.get("login_password", ""))
    except auth.AuthConfigError as exc:  # e.g. the user file changed into an invalid one after the page loaded
        logger.error("Sign-in not configured: %s", exc)  # details for the System Admin only
        result = auth.SignInResult(False, message="Sign-in is not configured – contact your System Admin.")
    st.session_state["login_password"] = ""  # never kept in the session
    if result.ok and result.user:
        st.session_state[AUTH_KEY] = {"user_id": result.user.user_id, "role": result.user.role,
                                      "display_name": result.user.display_name, "demo": result.user.demo,
                                      "pmo_head": result.user.pmo_head}
        st.session_state[ACTIVITY_KEY] = time.time()
        st.session_state.pop("login_notice", None)
    else:
        st.session_state["login_notice"] = result.message


def _demo_sign_in(user_id: str, password: str) -> None:
    """Demo quick button: signs in with one click – through the same password check as the form."""
    st.session_state["login_user"], st.session_state["login_password"] = user_id, password
    _submit_sign_in()


def render_sign_in() -> None:
    """The only page without a signed-in identity."""
    cfg = get_runtime_config()
    st.markdown(badge(cfg.environment, ENV_BADGES.get(cfg.environment, "gray"), ":material/dns:"))
    st.title("Sign in")
    try:
        users = auth.load_users()
    except auth.AuthConfigError as exc:
        logger.error("Sign-in not configured: %s", exc)  # details for the System Admin only
        st.error("Sign-in is not configured – contact your System Admin.", icon=":material/lock:")
        return
    if notice := st.session_state.get("login_notice"):
        st.warning(notice, icon=":material/info:")
    with st.form("sign_in", border=True):
        st.text_input("User", key="login_user", placeholder="e.g. PMO-ADMIN or your name")
        st.text_input("Password", key="login_password", type="password")
        st.form_submit_button("Sign in", type="primary", icon=":material/login:", on_click=_submit_sign_in)
    password = auth.demo_password()
    if password and auth.demo_allowed(cfg.environment):
        with st.container(border=True, key="demo_accounts"):
            st.markdown(f"**Demo environment – demo accounts** · password `{password}`")
            st.caption("Shared demo – all visitors with the same account see the same inbox, chats and saved what-ifs. "
                       "Changes reset when the hosted demo restarts. A button signs you in as that account.")
            demo = [u for u in users.values() if u.demo]
            cols = st.columns(3)
            for i, u in enumerate(demo):
                cols[i % 3].button(f"{u.display_name} · {ROLE_LABELS.get(u.role, u.role)}", key=f"demo_{u.user_id}",
                                   on_click=_demo_sign_in, args=(u.user_id, password), width="stretch")


def render_header(full_ds, user: dict):
    """Header row on every page. Returns (role, user_id, ds) – ds is the RLS-filtered dataset of the identity.
    Role and user come only from the sign-in (user file), never from a selection."""
    cfg = get_runtime_config()
    role, user_id = user["role"], user["user_id"]
    with st.container(key="app_header"):
        left, right = st.columns([2, 3], vertical_alignment="center")
        with right:
            env, scope, account, reload_col = st.columns([0.7, 2.1, 1.9, 0.4], vertical_alignment="center")
            env.markdown(badge(cfg.environment, ENV_BADGES.get(cfg.environment, "gray"), ":material/dns:"))
            with account.popover(f"{user.get('display_name') or user_id} · {ROLE_LABELS[role]}",
                                 icon=":material/account_circle:", width="stretch"):
                st.markdown(f"**{user.get('display_name') or user_id}**  \nUser {user_id} · {ROLE_LABELS[role]}")
                st.caption("To work as another identity, sign out and sign in again.")
                if st.button("Sign out", icon=":material/logout:", key="sign_out", width="stretch"):
                    sign_out()
                    st.rerun()
            if reload_col.button("", icon=":material/refresh:", key="reload_data", help="Reload portfolio data",
                                 type="tertiary"):
                logger.info("UI | Data cache cleared and reloaded | user=%s", user_id)
                load_dataset.clear()
                graph_status.clear()
                st.session_state["data_loaded_at"] = None
                st.rerun()
        st.session_state["role"], st.session_state["user_id"] = role, user_id
        if st.session_state.get("identity") != (role, user_id):  # own history per identity, never another role's
            previous = st.session_state.get("identity")
            log_security_event("Session started" if previous is None else "User login switched",
                               source=previous[0] if previous else "-", target=role, user=user_id)
            st.session_state["identity"] = (role, user_id)
            st.session_state["messages"] = load_chat(role, user_id) if role != ROLE_ADMIN else []
            if previous is not None:  # another identity: never keep the previous one's selection open
                st.session_state.pop("selected_run", None)
                st.session_state.pop("detail_pid", None)
                st.query_params.pop("project", None)

        ds = apply_rls_to_dataset(full_ds, role, user_id) if role != ROLE_ADMIN else full_ds.filter_projects([])
        loaded = st.session_state.setdefault("data_loaded_at", None) or clock.now().strftime("%H:%M")
        st.session_state["data_loaded_at"] = loaded
        with left:
            st.caption("Enterprise Portfolio Digital Twin")
        if role == ROLE_ADMIN:
            scope.caption("No access to portfolio data")
        else:
            scope.caption(f"{scope_text(role, len(ds.projects), len(full_ds.projects))} visible · data {loaded}")
    if cfg.environment == "PROD":
        st.error("LIVE PRODUCTION ENVIRONMENT", icon=":material/warning:")
    return role, user_id, ds


# every URL path of the signed-in app: before sign-in each of them shows the sign-in page (a reload or a shared link
# never ends on Streamlit's "Page not found") and opens that page after the sign-in
PAGE_PATHS = ("overview", "console", "approvals", "projects", "scenarios", "planning", "data_quality", "rules", "admin",
              "insights")


def _sign_in_at(path: str) -> Callable[[], None]:
    def page() -> None:
        st.session_state[NAV_KEY] = path  # the page the link or reload asked for opens after the sign-in
        render_sign_in()

    page.__name__ = f"sign_in_{path}"  # st.Page identifies callable pages by name
    return page


def sign_in_navigation():
    """Navigation without a signed-in identity: the sign-in page under its own path and under every app path."""
    pages = [st.Page(render_sign_in, title="Sign in", icon=":material/login:", url_path="sign-in", default=True)]
    pages += [st.Page(_sign_in_at(path), title="Sign in", icon=":material/login:", url_path=path) for path in PAGE_PATHS]
    return st.navigation(pages, position="hidden")


def build_navigation(role: str, pages: dict[str, tuple]):
    """`pages`: page id -> (title, material icon, callable[, "hidden"]); a hidden page is a sub-page, opened from its
    parent page and not listed in the navigation. The default page is the one in st.session_state[NAV_KEY]
    (falls back to the first page), so code and tests can open a page without a URL."""
    wanted = st.session_state.get(NAV_KEY)
    if wanted not in pages:
        wanted = next(iter(pages))
    built = {pid: st.Page(spec[2], title=spec[0], icon=spec[1], url_path=pid, default=(pid == wanted),
                          visibility=spec[3] if len(spec) > 3 else "visible")
             for pid, spec in pages.items()}
    st.session_state["_pages"] = built
    return st.navigation(list(built.values()), position="top")
