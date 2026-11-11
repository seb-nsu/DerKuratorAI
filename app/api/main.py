"""FastAPI application: `uvicorn app.api.main:app` (localhost by default; see docs/API.md for hosting).

Every route lives under /api/v1. Unexpected errors answer 500 with an incident id; the traceback goes to the system
log and the incident log only, never to the client."""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api import routes, state_store
from app.api.deps import environment
from app.approval_flow import record_incident
from app.core.logs import setup_logging
from app.data_adapters import DataSourceError

logger = logging.getLogger("derkurator.api")
PREFIX = "/api/v1"


def _workers_allowed() -> None:
    """`uvicorn ... --workers` / WEB_CONCURRENCY > 1 starts several workers. They share runs, sign-outs and the
    calculation counters only through the file store (STATE_STORE=file) and accept each other's tokens only with one
    API_TOKEN_SECRET; NiceGUI stays in one process. Refuse to start instead of losing runs silently."""
    try:
        workers = state_store.worker_count()
    except ValueError:
        workers = 1  # serve.py reports a malformed value; uvicorn itself refuses it
    refused = state_store.several_workers_refused(workers, os.getenv("FRONTEND", "").strip().lower() or None)
    if refused:
        raise RuntimeError(refused)


class Health(BaseModel):
    status: str
    environment: str


def create_app() -> FastAPI:
    setup_logging()
    _workers_allowed()
    app = FastAPI(title="DerKurator AI API", version="1", docs_url=f"{PREFIX}/docs",
                  openapi_url=f"{PREFIX}/openapi.json", redoc_url=None)

    @app.get(f"{PREFIX}/health", tags=["system"])
    def health() -> Health:
        return Health(status="ok", environment=environment())

    for module in sorted(m.name for m in pkgutil.iter_modules(routes.__path__)):  # one router per area module
        app.include_router(importlib.import_module(f"{routes.__name__}.{module}").router, prefix=PREFIX)

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # where and why only – never echo the submitted values (a password on /auth/token)
        return JSONResponse({"detail": [{"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
                                        for e in exc.errors()]}, status_code=422)

    @app.exception_handler(DataSourceError)
    def data_source_unavailable(request: Request, exc: DataSourceError) -> JSONResponse:
        logger.error("Data source unavailable for %s: %s", request.url.path, exc)
        return JSONResponse({"detail": "The portfolio data source is currently unavailable. Please contact your "
                                       "System Admin."}, status_code=503)

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> JSONResponse:
        incident = uuid.uuid4().hex[:8].upper()
        logger.error("API error on %s (incident %s)", request.url.path, incident, exc_info=exc)
        record_incident("api_error", incident, path=request.url.path)
        return JSONResponse({"detail": f"System error (incident {incident}). Please contact your System Admin.",
                             "incident_id": incident}, status_code=500)

    return app


app = create_app()
