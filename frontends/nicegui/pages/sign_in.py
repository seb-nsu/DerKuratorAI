"""The only page without a signed-in identity: centred sign-in card, demo accounts in DEV/TEST on demo data."""
from __future__ import annotations

from urllib.parse import unquote

from nicegui import ui

from frontends.nicegui import api, theme
from frontends.nicegui.components import badge, error_box, logo


def _target(next_url: str | None) -> str:
    target = unquote(next_url or "")
    return target if target.startswith("/") and not target.startswith("//") else "/"  # never an external redirect


async def render(next_url: str | None = None) -> None:
    theme.apply()
    ui.dark_mode(value=None)  # before sign-in: follow the device setting
    info = await api.request("GET", "/sign-in-info", anonymous=True)
    target = _target(next_url)

    async def submit(user_id: str, password: str) -> None:
        try:
            await api.sign_in(user_id.strip(), password)
        except api.ApiError as exc:
            message.set_text(exc.message)
            message.set_visibility(True)
            return
        ui.navigate.to(target)

    with ui.column().classes("w-full items-center justify-center q-pa-md").style("min-height: 90vh"):
        with ui.element("div").classes("dk-card").style("width: 100%; max-width: 420px; height: auto"):
            with ui.row().classes("items-center gap-2 q-mb-md"):
                logo()
                ui.space()
                badge(info["environment"], theme.tone("environment", info["environment"]), "dns")
            ui.label("Sign in").classes("text-h5 text-weight-bold").props('role="heading" aria-level="1"')
            if not info["configured"]:
                error_box("Sign-in is not configured – contact your System Admin.")
                return
            user = ui.input("User", placeholder="e.g. PMO-ADMIN or your name").props("outlined autofocus") \
                .classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True).props("outlined") \
                .classes("w-full")
            message = ui.label("").classes("text-negative").props('role="alert"')
            message.set_visibility(False)
            password.on("keydown.enter", lambda: submit(user.value or "", password.value or ""))
            ui.button("Sign in", icon="login", on_click=lambda: submit(user.value or "", password.value or "")) \
                .props("unelevated color=primary").classes("w-full q-mt-sm")
        demo = info.get("demo")
        if demo:
            with ui.element("div").classes("dk-card").style("width: 100%; max-width: 720px; height: auto"):
                ui.label("Demo environment – demo accounts").classes("text-weight-bold")
                ui.label(f"Password {demo['password']} · shared demo: everyone with the same account sees the same "
                         "inbox, chats and saved what-ifs. A button signs you in as that account.") \
                    .classes("dk-muted text-caption q-mb-sm")
                with ui.element("div").classes("dk-grid"):
                    for account in demo["accounts"]:
                        ui.button(f"{account['display_name']} · {account['role_label']}",
                                  on_click=lambda a=account: submit(a["user_id"], demo["password"])) \
                            .props("outline no-caps").classes("w-full")
