"""Record of one analysis run (Admin Console → Logs → Analysis runs), shared by every front end: run id, user, role,
data version, mode, duration, verdict and plan outline – no request text, no project data beyond IDs."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from app.approval_flow import audit
from app.core.config import get_settings

logger = logging.getLogger("derkurator.analysis_runs")


def data_version() -> tuple:
    """Version of the configured data source for cache keys: path, mtime and size of every source file (JSON, Excel,
    the files of a mapping); without files the TTL window of load_dataset (60 s)."""
    from app.data_adapters import source_files

    s = get_settings()
    try:
        files = source_files(s)
    except Exception:  # noqa: BLE001 - the load itself reports a broken mapping
        files = []
    if files:
        version: list = [s.data_source]
        for path in files:
            try:
                st_ = Path(path).stat()
                version += [str(path), st_.st_mtime_ns, st_.st_size]
            except OSError:
                version += [str(path), None, None]
        return tuple(version)
    return s.data_source, int(time.time() // 60)


def plan_outline(plan: dict | None) -> list[dict]:
    """The plan without free text (descriptions can carry names): intent type, action, project IDs, parameters."""
    outline = []
    for sub in (plan or {}).get("sub_intents") or []:
        params = dict(sub.get("parameters") or {})
        action = params.pop("action", None) or str(sub.get("description") or "").split(":", 1)[0]
        outline.append({"type": sub.get("intent_type"), "action": action if re.fullmatch(r"[\w/]+", action or "") else None,
                        "projects": list(sub.get("project_ids") or []), "parameters": params})
    return outline


def run_mode(mode: str, warnings: list[str] | None) -> str:
    """LLM / offline / fallback (the LLM run failed and the offline engine answered)."""
    warnings = warnings or []
    if mode in ("agents", "crewai"):  # "crewai": runs stored before the agents called the providers directly
        return "LLM (parser fallback)" if any(w.startswith("Orchestrator fallback") for w in warnings) else "LLM"
    if mode == "offline" and any(w.startswith(("LLM pipeline failed", "CrewAI pipeline failed")) for w in warnings):
        return "fallback"
    return mode


def log_analysis_run(role: str, result, run_id: str | None = None, user_id: str | None = None) -> None:
    """Record of an analysis (Admin Console → Logs → Analysis runs): run id, user, role, data version, mode, duration,
    verdict and plan outline – no request text, no project data beyond IDs. Proposals and applies cite the run id."""
    try:
        audit("analysis_run", run_id=run_id, role=role, user_id=user_id, data_version=list(data_version()),
              mode=run_mode(result.mode, result.warnings), duration_s=round(result.duration_s or 0, 2),
              verdict=result.verdict.label if result.verdict else None,
              plan=plan_outline(result.intent_plan.model_dump() if result.intent_plan else None))
    except Exception as exc:  # noqa: BLE001 - a log problem never fails the analysis the user already sees
        logger.warning("Analysis run could not be logged: %s", exc.__class__.__name__)
