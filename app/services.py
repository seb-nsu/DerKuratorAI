"""Service layer between the UI and the analysis core: the one place the Streamlit pages call into the crew, the
tools and the graph. The imports are lazy, so a page loads without pulling in the LLM stack, and a test that patches
the function at its home module still reaches it through here."""
from __future__ import annotations

import contextvars
from datetime import date
from typing import Any

import pandas as pd


def run_steering_pipeline(*args: Any, **kwargs: Any) -> Any:
    """Runs one change request through data quality, simulation, dependency check and decision paper."""
    from app.crew import pipeline

    return pipeline.run_steering_pipeline(*args, **kwargs)


def graph_connection() -> tuple[bool, str]:
    """(reachable, detail) of the dependency graph."""
    from app.crew.tools.graph_tools import check_connection

    return check_connection()


def graph_backend_status() -> dict[str, Any]:
    """Configured and active graph backend (NetworkX or Neo4j) with its health; read-only, never the password."""
    from app.graph.backends import backend_status

    return backend_status()


def top_portfolio_risks(allowed: set[str]) -> list[dict[str, Any]]:
    """Blocked projects and systemic risk clusters in the visible scope, for the dashboard alert bar."""
    from app.crew.tools.graph_tools import top_portfolio_risks as top

    return top(allowed)


def benefit_realisation(plans: pd.DataFrame, actuals: pd.DataFrame, project: dict[str, Any],
                        today: date) -> dict[str, Any]:
    """Planned vs. actual benefit of the due tranches of one project."""
    from app.crew.tools.benefit_tools import realisation

    return realisation(plans, actuals, project, today)


def risk_exposure(probability: Any, impact: Any) -> float:
    """Exposure in € of one risk on the configured risk scale."""
    from app.crew.tools.pmo_tools import risk_exposure as exposure

    return exposure(probability, impact)


def pmo_figures(dataset: Any, months: int, as_of: date) -> tuple[pd.DataFrame, float]:
    """Skill utilisation per skill and month and the management reserve balance, computed by the simulation tools in
    an isolated scenario context of `dataset`, so no running analysis is touched."""
    from app.crew.tools import context as scenario
    from app.crew.tools.pmo_tools import reserve_balance
    from app.crew.tools.skill_tools import skill_utilisation

    def run() -> tuple[pd.DataFrame, float]:
        scenario.set_portfolio_context(dataset, as_of=as_of)
        return skill_utilisation(months), reserve_balance()

    return contextvars.copy_context().run(run)


def clear_llm_caches() -> None:
    """Drops the cached model clients (after keys or settings were reloaded)."""
    from app.crew.agents import clear_llm_caches as clear

    clear()


def test_model_connection(model_id: str, is_thinking: bool) -> tuple[bool, str]:
    """One minimal live call to a model through the app's client path. Never raises."""
    from app.crew.agents import test_model_connection as probe

    return probe(model_id, is_thinking)


test_model_connection.__test__ = False  # not a pytest test despite its name
