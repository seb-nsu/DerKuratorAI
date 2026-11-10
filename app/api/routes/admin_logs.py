"""Admin Console – logs: business audit trail, analysis runs, decision log with its hash chain, incidents, shadow
comparisons, system log and LLM calls (no prompt or answer text). Clearing a log archives it first and needs
confirm = true. System Admin only."""
from __future__ import annotations

import json
import statistics

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api import admin_schemas as schemas
from app.api.admin_common import Confirmation, renamed, require_confirm, respond
from app.api.deps import admin
from app.core.auth import User
from app.core.runtime_config import get_runtime_config
from app.views import admin as views

router = APIRouter(prefix="/admin/logs", tags=["admin"], dependencies=[Depends(admin)])
MAX_ROWS = 1000


def _safe(rows: list[dict]) -> list[dict]:
    return json.loads(json.dumps(rows, default=str))


@router.get("/business-audit")
def business_audit(limit: int = Query(200, ge=1, le=MAX_ROWS)) -> schemas.LinesView:
    try:
        return schemas.LinesView(lines=views.business_audit_lines(limit))
    except OSError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Business audit log unreadable: {exc}") from exc


@router.get("/analysis-runs")
def analysis_runs(mode: list[str] | None = Query(None),
                  limit: int = Query(200, ge=1, le=MAX_ROWS)) -> schemas.AnalysisRunsView:
    rows = renamed(views.analysis_runs(limit), {"Time": "time", "Environment": "environment", "Role": "role",
                                                "Mode": "mode", "Duration (s)": "duration_s", "Verdict": "verdict",
                                                "Plan": "plan"})
    for row in rows:
        row["plan"] = json.loads(row["plan"])
    modes = sorted({str(r["mode"]) for r in rows if r["mode"] is not None})
    durations = [float(r["duration_s"]) for r in rows if isinstance(r["duration_s"], (int, float))]
    total = len(rows)
    if mode:
        rows = [r for r in rows if str(r["mode"]) in mode]
    return schemas.AnalysisRunsView(runs=_safe(rows), modes=modes, total=total,
                                    median_s=round(statistics.median(durations), 1) if durations else None,
                                    p95_s=round(_quantile(durations, 0.95), 1) if durations else None)


def _quantile(values: list[float], q: float) -> float:
    """Linear interpolation between the closest ranks (the same as pandas' default quantile)."""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


@router.get("/audit")
def audit_log(event: list[str] | None = Query(None), limit: int = Query(500, ge=1, le=MAX_ROWS)) -> schemas.AuditLogView:
    try:
        records, malformed = views.audit_records()
    except FileNotFoundError:
        records, malformed = [], 0
    events = sorted({str(r["event"]) for r in records if r.get("event") is not None})
    if event:
        records = [r for r in records if r.get("event") in event]
    return schemas.AuditLogView(records=_safe(records[:limit]), total=len(records), events=events, malformed=malformed)


@router.post("/audit/verify-chain")
def verify_chain(user: User = Depends(admin)) -> schemas.ChainCheck:
    return schemas.ChainCheck.model_validate(views.verify_audit_chain(user.user_id))


@router.get("/incidents")
def incidents(limit: int = Query(200, ge=1, le=MAX_ROWS)) -> schemas.IncidentsView:
    records, malformed = views.incidents(limit)
    return schemas.IncidentsView(incidents=_safe(records), malformed=malformed)


@router.get("/shadow")
def shadow(limit: int = Query(100, ge=1, le=MAX_ROWS)) -> schemas.ShadowView:
    entries, malformed = views.shadow_entries()
    return schemas.ShadowView(entries=_safe(entries[:limit]), total=len(entries), malformed=malformed,
                              matrix=views.matrix_rows(), shadow_available=get_runtime_config().shadow_available)


@router.get("/system")
def system_log(level: str = Query("ALL"), limit: int = Query(100, ge=1, le=MAX_ROWS)) -> schemas.LinesView:
    level = level.upper()
    if level not in views.LOG_FILTER_LEVELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"Unknown level – allowed: {', '.join(views.LOG_FILTER_LEVELS)}.")
    return schemas.LinesView(lines=views.system_log_lines(level, limit))


@router.get("/llm-calls")
def llm_calls(limit: int = Query(50, ge=1, le=MAX_ROWS)) -> schemas.LlmCallsView:
    log = views.llm_call_log(limit)
    summary = renamed(log["summary"], {"Caller": "caller", "Model": "model", "Calls": "calls", "Errors": "errors",
                                       "Median s": "median_s", "Tokens": "tokens", "Cost USD": "cost_usd"})
    return schemas.LlmCallsView(summary=summary, recent=_safe(log["recent"]), window=log["window"])


@router.post("/{name}/clear")
def clear_log(name: str, body: Confirmation, response: Response, user: User = Depends(admin)) -> schemas.LogCleared:
    if name not in views.CLEARABLE_LOGS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Only these logs can be cleared: {', '.join(views.CLEARABLE_LOGS)}.")
    require_confirm(body)
    return respond(views.clear_log(name, user.user_id), response, schemas.LogCleared)
