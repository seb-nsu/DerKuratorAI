"""Agent definitions including LLM routing (model per role from the runtime configuration).

Routing: each role gets the model the runtime configuration names for it (Admin Console, defaults from .env); the
agents call the providers directly through their SDKs (app/llm). The Data Steward deliberately uses no LLM.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Literal

from app.core.config import detect_provider
from app.core.model_registry import is_thinking_model
from app.core.runtime_config import get_runtime_config
from app.crew.tools.data_steward_tools import DataQualityReport, run_all_checks
from app.data_adapters.dataset import PortfolioDataset
from app.llm.client import AnthropicClient, LLMConfigError, OpenAIClient, clear_clients
from app.observability import llm_calls

logger = logging.getLogger(__name__)

AgentRole = Literal["orchestrator", "data_steward", "analyst", "simulator", "advisory"]
__all__ = ["LLMConfigError", "model_for_agent", "clear_llm_caches", "test_model_connection"]


def effective_temperature(model: str, temperature: float) -> float:
    """Reasoning/thinking models (flag in data/supported_models.json) only accept temperature 1.0."""
    return 1.0 if is_thinking_model(model) else temperature


def model_for_agent(role: str, variant: str = "main", model: str | None = None) -> str:
    """The model an agent runs on. `variant`: "main" or "shadow" (per-agent matrix in the env config). Raises
    LLMConfigError when its key – or, under EU data residency, its endpoint – is missing."""
    cfg = get_runtime_config()
    model = model or cfg.model_for_role(role, variant)
    missing = cfg.missing_key_for_model(model)
    if missing:
        raise LLMConfigError(f"{missing} missing for {variant} model of '{role}' ({model})")
    logger.info("LLM routing: %s/%s -> %s (thinking=%s)", role, variant, model, is_thinking_model(model))
    return model


def clear_llm_caches() -> None:
    """Drops cached clients – called when keys/settings are reloaded."""
    clear_clients()


def test_model_connection(model_id: str, is_thinking: bool) -> tuple[bool, str]:
    """Live validation for "Bring Your Own Model": one minimal call through exactly the client path the app uses.
    Returns (success, message). Never raises."""
    cfg = get_runtime_config()
    model_id = (model_id or "").strip()
    if not model_id:
        return False, "Model ID must not be empty."
    provider = detect_provider(model_id)
    missing = cfg.missing_key_for_model(model_id)
    if missing:
        return False, f"{missing} is not set or invalid – add it to .env or the API key fields first."
    try:
        from app.core.config import provider_endpoint
        from app.llm.client import GEMINI_OPENAI_URL

        cls = AnthropicClient if provider == "anthropic" else OpenAIClient
        client = cls(model_id, cfg.api_key_for(provider),
                     provider_endpoint(provider) or (GEMINI_OPENAI_URL if provider == "google" else ""),
                     1.0 if is_thinking else cfg.temperature)
        client.thinking = is_thinking  # the flag of the model being added, not yet in the registry
        started = time.perf_counter()
        with llm_calls.caller("model test"):
            text = client.chat("You are a connectivity check.", [client.user("Reply with the single word OK.")],
                               max_tokens=16).text
        latency = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - provider errors are reported to the admin, not raised
        return False, f"Test call failed ({exc.__class__.__name__}): {str(exc).splitlines()[0][:300]}"
    return True, f"Test call succeeded in {latency:.1f}s via {provider} (response: {text.strip()[:40]!r})."


def _agent_kwargs(role: str, variant: str, model: str | None) -> dict:
    return {"model": model_for_agent(role, variant, model), "name": role if variant == "main" else f"{role}.{variant}"}


def _with_assumptions(backstory: str) -> str:
    """Every agent works with the organisation's configured business assumptions (governance config)."""
    from app.core.business_rules import business_assumptions_md

    return (f"{backstory}\n\nBusiness assumptions of this organisation (set by the PMO – use them, never your own "
            f"defaults):\n{business_assumptions_md()}")


# --------------------------------------------------------------------------- Data Steward (no LLM)
class DataStewardAgent:
    """Deterministic 'agent': pure Python rules, runs BEFORE every LLM agent run."""

    name = "Data Steward (deterministic, no LLM)"

    def run(self, dataset: PortfolioDataset, as_of: date | None = None) -> DataQualityReport:
        try:
            return run_all_checks(dataset, as_of=as_of)
        except Exception as exc:
            logger.exception("Data Steward failed")
            from app.crew.tools.data_steward_tools import DataQualityIssue

            return DataQualityReport(
                total_rows=0 if dataset is None else len(dataset.projects),
                dataset=dataset,
                issues=[DataQualityIssue("ERROR", "*", "data_steward", f"Check failed: {exc}", "Internal")],
            )


# --------------------------------------------------------------------------- LLM agents
# `variant`/`model`: the shadow crew instantiates the same agents with the per-agent shadow models.
# Time limit per agent (seconds) – a safety net well above the measured p95 of a whole run (~110 s); a timeout ends
# the run and the pipeline falls back to the deterministic path with a warning. max_iter stays the step limit.
AGENT_TIME_LIMITS_S = {"orchestrator": 60, "simulator": 150, "analyst": 120, "advisory": 150}
ADVISORY_ROLE = "Executive Advisor"
ADVISORY_GOAL = "Explain the rule-based verdict and all analysis results in a decision-ready Executive Decision Paper."
_ADVISORY_BACKSTORY = (
    "You are a partner at a top strategy consultancy writing for executive boards: clear, concise, fact-based. "
    "You take numbers exclusively from your colleagues' work, clearly flag AI estimates "
    "(⚠️ [AI ESTIMATE]) and never hide guardrail errors. The GO / CONDITIONAL GO / NO-GO verdict is decided "
    "by the organisation's deterministic rule engine, not by you: you explain and justify it, you never change it."
)


def advisory_system_prompt() -> str:
    """The Advisory's role as one system prompt – the same for the streamed call and the agent task."""
    return f"You are the {ADVISORY_ROLE}. Your goal: {ADVISORY_GOAL}\n\n{_with_assumptions(_ADVISORY_BACKSTORY)}"
def build_orchestrator_agent(variant: str = "main", model: str | None = None):
    from app.llm.agent import Agent

    return Agent(
        role="Portfolio Orchestrator",
        goal="Split management steering requests precisely into sub-intents and route them to the right specialists.",
        backstory=_with_assumptions(get_runtime_config().orchestrator_prompt),  # editable in the Admin Console
        max_iter=3,
        max_execution_time=AGENT_TIME_LIMITS_S["orchestrator"],
        **_agent_kwargs("orchestrator", variant, model),
    )


def build_simulator_agent(variant: str = "main", model: str | None = None, assess_only: bool = False):
    """`assess_only`: the simulations already ran deterministically (guided plan) – the agent gets no tools and only
    assesses the results."""
    from app.crew.tools.benefit_tools import BENEFIT_TOOLS
    from app.crew.tools.merge_tools import MERGE_TOOLS
    from app.crew.tools.pmo_tools import PMO_TOOLS
    from app.crew.tools.portfolio_tools import PORTFOLIO_TOOLS
    from app.crew.tools.simulator_tools import SIMULATOR_TOOLS
    from app.crew.tools.skill_tools import simulate_skill_transfer
    from app.llm.agent import Agent

    return Agent(
        role="Portfolio Simulator (The Quant)",
        goal="Calculate the financial and capacity impact of scenarios exclusively through the provided tools.",
        backstory=_with_assumptions(
            "You are a meticulous portfolio controller. You only extract parameters (project_id, amounts, cost_type "
            "capex/opex/total, FTE, team, capacity/scope change in %, months) and call the matching tool. "
            "You NEVER calculate yourself and never invent numbers. If a tool returns 'ERROR: ...' (including "
            "'ERROR: Data quality'), you pass that text on verbatim and do not try to work around it. "
            "For schedule questions prefer simulate_agile_vs_waterfall_shift (method-aware). "
            "Do not repeat tool results – the system passes them on verbatim; add only a one-sentence assessment. "
            "Use simulate_unstructured_scenario only if no calculation tool fits. A pause is simulate_on_hold (never "
            "a cancellation); a savings target across the portfolio is simulate_portfolio_cut. Schedule tools already "
            "check successor deadlines and NPV – the system passes their '⚠ RISK' lines on. PMO actions have their "
            "own tools: resuming a paused project simulate_resume, a portfolio cost increase simulate_cost_stress, a "
            "new baseline simulate_rebaseline, a phase change simulate_stage_gate, money from or back to the "
            "management reserve simulate_reserve_draw / simulate_reserve_return, people of one skill "
            "simulate_skill_transfer, a decision on a risk (RSK-...) simulate_risk_response, a check of realised "
            "benefits after go-live simulate_benefit_review, merging one project into another simulate_merge."
        ),
        tools=[] if assess_only else (SIMULATOR_TOOLS + PORTFOLIO_TOOLS + PMO_TOOLS + BENEFIT_TOOLS + MERGE_TOOLS
                                      + [simulate_skill_transfer]),
        max_iter=10,
        max_execution_time=AGENT_TIME_LIMITS_S["simulator"],
        **_agent_kwargs("simulator", variant, model),
    )


def build_analyst_agent(variant: str = "main", model: str | None = None):
    from app.crew.tools.graph_tools import ANALYST_TOOLS
    from app.crew.tools.skill_tools import analyse_skill_capacity
    from app.llm.agent import Agent

    return Agent(
        role="Portfolio Analyst (The Strategist)",
        goal="Uncover project and task dependencies, team bottlenecks and systemic risk clusters in the enterprise graph.",
        backstory=_with_assumptions(
            "You are an enterprise architect with an eye for systemic risk. For project stops you always check the "
            "task domino (get_task_level_impact), for resource questions identify_team_bottlenecks and, for a "
            "skill (e.g. 'SAP FI'), analyse_skill_capacity. You base every "
            "statement on graph tool results and flag tool errors transparently. Projects outside the user's "
            "scope appear only as the note that the PMO coordinates: repeat that note, never guess or count them."
        ),
        tools=ANALYST_TOOLS + [analyse_skill_capacity],
        max_iter=8,
        max_execution_time=AGENT_TIME_LIMITS_S["analyst"],
        **_agent_kwargs("analyst", variant, model),
    )


def build_advisory_agent(variant: str = "main", model: str | None = None):
    from app.llm.agent import Agent

    return Agent(
        role=ADVISORY_ROLE,
        goal=ADVISORY_GOAL,
        backstory=_with_assumptions(_ADVISORY_BACKSTORY),
        max_iter=3,
        max_execution_time=AGENT_TIME_LIMITS_S["advisory"],
        **_agent_kwargs("advisory", variant, model),
    )
