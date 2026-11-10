"""The page frame: sign-in guard, header (logo, navigation of the role, environment badge, scope line, account menu,
reload, light/dark switch) and the page body. On a phone the navigation collapses into a menu."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import quote

from nicegui import app, ui

from frontends.nicegui import api, theme
from frontends.nicegui.components import badge, error_box, logo

Render = Callable[[dict, dict], Awaitable[None]]  # (workspace info, query parameters)


def _dark_toggle() -> None:
    dark = ui.dark_mode(value=bool(app.storage.user.get("dark")))

    def flip() -> None:
        app.storage.user["dark"] = not bool(app.storage.user.get("dark"))
        dark.set_value(app.storage.user["dark"])
        ui.navigate.reload()  # charts pick up the new theme

    icon = "light_mode" if app.storage.user.get("dark") else "dark_mode"
    ui.button(icon=icon, on_click=flip).props('flat round dense aria-label="Switch light/dark theme"') \
        .tooltip("Light / dark")


def page_title(info: dict, page_id: str, default: str) -> str:
    """The heading of a page: the words of its navigation entry for this role ("My projects" for a Project Manager)."""
    return next((p["title"] for p in info["pages"] if p["id"] == page_id), default)


def scope_text(scope: dict) -> str:
    """"2 projects" for a Project Manager (no portfolio total), "11 of 11 projects" for the PMO."""
    if scope.get("total") is None:
        return f"{scope['visible']} project{'' if scope['visible'] == 1 else 's'}"
    return f"{scope['visible']} of {scope['total']} projects"


def _header(info: dict, active: str) -> None:
    user = info["user"]
    # sub-pages (Data quality, What-if comparison) are opened from their parent page, which stays highlighted
    pages = [p for p in info["pages"] if not p.get("parent")]
    active = next((p.get("parent") for p in info["pages"] if p["id"] == active and p.get("parent")), active)
    with ui.header(elevated=False).classes("dk-header items-center no-wrap q-px-md q-py-xs gap-1"):
        with ui.button(icon="menu").props('flat round dense aria-label="Open navigation"').classes("lt-md"):
            with ui.menu():
                for page in pages:
                    ui.menu_item(_title(page, info), on_click=lambda p=page: ui.navigate.to(f"/{p['id']}"))
        with ui.link(target=f"/{pages[0]['id']}").style("text-decoration:none;color:inherit") \
                .props('aria-label="DerKurator AI home"'):
            logo(wordmark=False)
        with ui.tabs(value=active).props("dense no-caps inline-label active-color=primary indicator-color=primary") \
                .classes("dk-nav gt-sm"):
            for page in pages:
                ui.tab(page["id"], label=_title(page, info), icon=page["icon"]) \
                    .on("click", lambda p=page: ui.navigate.to(f"/{p['id']}"))
        ui.space()
        _dark_toggle()
        if info.get("scope") is not None:
            ui.button(icon="refresh", on_click=_reload).props('flat round dense aria-label="Reload portfolio data"') \
                .tooltip("Reload portfolio data")
        with ui.button(icon="account_circle").props('flat no-caps aria-label="Account"').classes("gt-xs") \
                .tooltip(f"{user['display_name']} · {user['role_label']}"):
            ui.label(f"{user['display_name']} · {user['role_label']}").classes("q-ml-xs dk-hide-narrow")
            _account_menu(user)
        with ui.button(icon="account_circle").props('flat round dense aria-label="Account"').classes("xs"):
            _account_menu(user)
    with ui.row().classes("dk-subbar w-full items-center no-wrap q-px-md q-py-xs gap-3"):
        ui.label("Enterprise Portfolio Digital Twin").classes("dk-muted text-caption gt-xs")
        ui.space()
        badge(info["environment"], theme.tone("environment", info["environment"]), "dns")
        ui.label(f"{scope_text(info['scope'])} visible · data {info['loaded_at']}"
                 if info.get("scope") else "No access to portfolio data").classes("dk-muted text-caption")
    if info.get("production"):
        with ui.element("div").classes("w-full dk-callout dk-tone-danger text-center text-weight-bold"):
            ui.label("LIVE PRODUCTION ENVIRONMENT")


def _title(page: dict, info: dict) -> str:
    if page["id"] == "approvals" and info.get("pending_approvals"):
        return f"{page['title']} ({info['pending_approvals']})"
    return page["title"]


def _account_menu(user: dict) -> None:
    with ui.menu():
        with ui.column().classes("q-pa-md gap-1"):
            ui.label(user["display_name"]).classes("text-weight-bold")
            ui.label(f"User {user['user_id']} · {user['role_label']}").classes("dk-muted")
            ui.label("To work as another identity, sign out and sign in again.").classes("dk-muted text-caption")
            ui.button("Sign out", icon="logout", on_click=_sign_out).props("outline").classes("q-mt-sm")


async def _sign_out() -> None:
    await api.end_session()
    ui.navigate.to("/sign-in")


async def _reload() -> None:
    await api.post("/data/reload")
    ui.navigate.reload()


def _next_url(page_id: str, query: dict) -> str:
    params = "&".join(f"{k}={quote(str(v))}" for k, v in query.items())
    return quote(f"/{page_id}" + (f"?{params}" if params else ""), safe="")


async def frame(page_id: str, query: dict, render: Render) -> None:
    """Draws one page: without a valid sign-in it sends the user to the sign-in (which returns here afterwards)."""
    theme.apply()
    if not api.signed_in():
        ui.navigate.to(f"/sign-in?next={_next_url(page_id, query)}")
        return
    try:
        info = await api.get("/workspace")
    except api.ApiError as exc:
        if exc.status == 401:
            ui.navigate.to(f"/sign-in?next={_next_url(page_id, query)}")
            return
        error_box(exc.message)
        return
    ids = [p["id"] for p in info["pages"]]
    if page_id not in ids:  # a link to a page this role does not have: its first page instead
        ui.navigate.to(f"/{ids[0]}")
        return
    _header(info, page_id)
    with ui.column().classes("dk-page"):
        try:
            await render(info, query)
        except api.ApiError as exc:
            if exc.status == 401:
                ui.navigate.to(f"/sign-in?next={_next_url(page_id, query)}")
                return
            error_box(exc.message)
