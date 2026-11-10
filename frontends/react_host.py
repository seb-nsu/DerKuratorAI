"""Serves the built React front end (frontends/react/dist) from the API process: static files, and index.html for
every other path that is not an API route (client-side routing, deep links such as /projects?project=PRJ-1)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DIST = Path(__file__).resolve().parent / "react" / "dist"


def mount(app: FastAPI, dist: Path = DIST) -> FastAPI:
    index = dist / "index.html"
    if not index.exists():
        raise RuntimeError(f"React build missing ({index}); run `npm ci && npm run build` in frontends/react")
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="react-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "Not found.")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)

    return app


def create_app() -> FastAPI:
    """The API with the React front end (the factory every worker process calls when several serve)."""
    from app.api.main import create_app as create_api

    return mount(create_api())
