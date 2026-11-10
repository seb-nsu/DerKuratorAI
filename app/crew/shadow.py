"""Per-agent shadow mode: the complete agent crew runs a second time in the background, with every agent built on
its shadow model from the environment config (agents without a shadow model reuse their main model).

Safety principles:
- Runs in a background thread and never blocks the UI.
- Own scenario context (set in the shadow thread): the shadow crew can never modify the
  main run's scenario, ledger or data source. Its only effect is an entry in the shadow log.
- Receives only PII-masked data; its output is masked again before logging.
- If a shadow model is configured for the Data Steward, a shadow Executive Interpretation of the data quality is
  generated as well (the deterministic checks themselves never change).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from app.core.logs import log_shadow_comparison
from app.core.runtime_config import AGENT_ROLES, get_runtime_config
from app.core.security import mask_pii_data

logger = logging.getLogger("derkurator.shadow")
_VERDICT_RE = re.compile(r"\*\*Recommendation:\*\*\s*([^\n|]+)", re.IGNORECASE)


def extract_verdict(paper: str) -> str | None:
    match = _VERDICT_RE.search(paper or "")
    return match.group(1).strip() if match else None


def _verdict_key(verdict: str | None) -> str | None:
    if not verdict:
        return None
    v = verdict.upper()
    if "INCOMPLETE" in v:
        return "INCOMPLETE"
    return "NO-GO" if "NO-GO" in v else "CONDITIONAL" if "CONDITIONAL" in v else "GO" if "GO" in v else v.split(" ")[0]


def run_shadow_crew(request: str, safe_dataset, blocked: dict[str, str], portfolio_md: str, steward_md: str, role: str,
                    plan_override, primary: dict[str, Any], shadow_models: dict[str, str], dq_summary: dict | None = None,
                    steward_shadow_model: str = "", forced: list[str] | None = None,
                    base_state: dict[str, Any] | None = None, as_of=None) -> dict[str, Any]:
    """Synchronous core (testable): runs the shadow crew and writes one entry to the shadow log."""
    from app.crew import pipeline
    from app.crew.tools import context as ctx
    from app.crew.verdict import enforce_verdict, verdict_from_context

    ctx.set_portfolio_context(safe_dataset, blocked, base_state=base_state,
                              as_of=as_of)  # own state, same scenario step and reference date
    started = time.perf_counter()
    shadow: dict[str, Any] = {"models": shadow_models, "result": "", "error": None}
    try:
        paper, _plan, warnings, _evidence = pipeline._run_agents(
            request, portfolio_md, steward_md, role, safe_dataset.projects, plan_override, variant="shadow",
            forced=forced,
        )
        paper, _deviated = enforce_verdict(paper, verdict_from_context(forced))  # same rule engine as the main run
        shadow["result"] = mask_pii_data(paper)
        shadow["warnings"] = warnings
        shadow["proposed_changes"] = len(ctx.get_ledger())
    except Exception as exc:  # noqa: BLE001 - shadow must never affect the main analysis
        shadow["error"] = f"{exc.__class__.__name__}: {exc}"
        logger.warning("Shadow crew failed: %s", exc)
    shadow["duration_s"] = round(time.perf_counter() - started, 2)

    extra: dict[str, Any] = {}
    if steward_shadow_model and dq_summary is not None:
        from app.crew.dq_interpretation import interpret

        dq = interpret(dq_summary, use_llm=True, model=steward_shadow_model)
        extra["data_steward_shadow"] = {"model": steward_shadow_model, "source": dq.source, "interpretation": dq.text}

    verdict_primary, verdict_shadow = extract_verdict(primary.get("result", "")), extract_verdict(shadow["result"])
    log_shadow_comparison(
        request, primary, shadow, role=role, verdict_primary=verdict_primary, verdict_shadow=verdict_shadow,
        verdict_match=(_verdict_key(verdict_primary) == _verdict_key(verdict_shadow)) if verdict_primary and verdict_shadow else None,
        **extra,
    )
    logger.info("Shadow comparison logged (%.1fs, error=%s)", shadow["duration_s"], bool(shadow["error"]))
    return shadow


def start_shadow_run(request: str, safe_dataset, blocked: dict[str, str], portfolio_md: str, steward_md: str, role: str,
                     plan_override, primary: dict[str, Any], dq_summary=None,
                     forced: list[str] | None = None, base_state: dict[str, Any] | None = None,
                     as_of=None) -> threading.Thread | None:
    """Starts the shadow crew in the background if at least one agent has a shadow model and all keys exist."""
    cfg = get_runtime_config()
    if not cfg.shadow_available:
        return None
    from app.crew.dq_interpretation import summarize
    from app.crew.tools.data_steward_tools import DataQualityReport

    summary = summarize(dq_summary) if isinstance(dq_summary, DataQualityReport) else dq_summary
    shadow_models = {a: cfg.model_for_role(a, "shadow") for a in AGENT_ROLES}
    thread = threading.Thread(
        target=run_shadow_crew,
        args=(request, safe_dataset, dict(blocked), portfolio_md, steward_md, role, plan_override, primary, shadow_models,
              summary, cfg.shadow_model_for("data_steward"), list(forced or []), base_state, as_of),
        name="shadow-crew", daemon=True,
    )
    thread.start()
    return thread
