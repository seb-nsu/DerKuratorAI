"""NiceGUI front end and the HTTP API in one process: `python -m frontends.nicegui.main` (or scripts/serve.py with
FRONTEND=nicegui). The API stays under /api/v1; the pages live at /<page id>, e.g. /projects?project=PRJ-1."""
from __future__ import annotations

import importlib
import os
import secrets

from fastapi import FastAPI, Request
from nicegui import app, ui
from nicegui.storage import Storage

from app.core.config import PROJECT_ROOT
from frontends.nicegui import api, shell
from frontends.nicegui.components import empty, page_header
from frontends.nicegui.pages import sign_in

# page id -> module under frontends.nicegui.pages (same ids as the Streamlit pages and the React routes)
PAGES = ("overview", "console", "approvals", "projects", "scenarios", "planning", "data_quality", "rules", "admin",
         "insights")


def _page_renderer(page_id: str):
    try:
        return importlib.import_module(f"frontends.nicegui.pages.{page_id}").render
    except ModuleNotFoundError:
        async def placeholder(info: dict, query: dict) -> None:
            page_header(next(p["title"] for p in info["pages"] if p["id"] == page_id))
            empty("This page is not available in this front end yet.")
        return placeholder


def register_pages() -> None:
    app.add_static_files("/assets", str(PROJECT_ROOT / "app" / "ui" / "assets"))

    @ui.page("/sign-in", title="Sign in · DerKurator AI")
    async def sign_in_page(next: str | None = None) -> None:
        await sign_in.render(next)

    @ui.page("/", title="DerKurator AI")
    async def home() -> None:
        await shell.frame("overview", {}, _page_renderer("overview"))

    for page_id in PAGES:
        def make(pid: str):
            async def page(request: Request) -> None:
                # every query parameter reaches the page (e.g. ?project=, ?tab=, ?proposal=, the console's prefill)
                query = {k: v for k, v in request.query_params.items() if v}
                await shell.frame(pid, query, _page_renderer(pid))
            return page
        ui.page(f"/{page_id}", title="DerKurator AI")(make(page_id))


def storage_secret() -> str:
    """Signs the browser session cookie. From NICEGUI_STORAGE_SECRET; DEV/TEST fall back to a random one per process
    (sessions end with a restart)."""
    configured = os.getenv("NICEGUI_STORAGE_SECRET", "")
    if len(configured) >= 32:
        return configured
    from app.core.runtime_config import get_runtime_config

    if get_runtime_config().environment == "PROD":
        raise RuntimeError("NICEGUI_STORAGE_SECRET (min. 32 characters) is required in PROD")
    return secrets.token_urlsafe(48)


def session_cookie() -> dict:
    """The browser session cookie is HttpOnly and SameSite=Lax; in PROD (always behind TLS) also Secure."""
    from app.core.runtime_config import get_runtime_config

    return {"https_only": get_runtime_config().environment == "PROD"}


def storage_path():
    """Where NiceGUI keeps the per-browser session (it holds the API token): NICEGUI_STORAGE_PATH, else a folder in
    the app's write directory – never the working directory, which is the repository or a read-only image."""
    from pathlib import Path

    from app.core.logs import WRITE_DIR

    return Path(os.getenv("NICEGUI_STORAGE_PATH") or WRITE_DIR / "nicegui").resolve()


def build(api_app: FastAPI | None = None) -> FastAPI:
    """The API app with the NiceGUI pages mounted next to /api/v1."""
    from app.api.main import create_app

    Storage.path = storage_path()

    api_app = api_app or create_app()
    if not os.getenv("API_BASE_URL"):
        api.use_asgi(api_app)
    register_pages()
    ui.run_with(api_app, title="DerKurator AI", favicon=str(PROJECT_ROOT / "app" / "ui" / "assets" / "logo.svg"),
                storage_secret=storage_secret(), dark=None, session_middleware_kwargs=session_cookie())
    return api_app


def main() -> None:
    import uvicorn

    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8080"))
    uvicorn.run(build(), host=host, port=port, log_level="info")


if __name__ in {"__main__", "__mp_main__"}:
    main()
