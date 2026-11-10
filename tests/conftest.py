"""Shared fixtures: deterministic mock portfolio and isolated global state."""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core import security  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.runtime_config import reset_runtime_config, update_runtime_config  # noqa: E402
from app.data_adapters.dataset import PortfolioDataset  # noqa: E402

FUTURE_END = "2030-06-30"
DEMO_REFERENCE_DAY = date(2026, 10, 1)  # the demo portfolio's dates are written for this day (= EVAL_AS_OF)


def pin_demo_reference_day(mp: pytest.MonkeyPatch) -> None:
    """Shifts the app clock so that today is the demo portfolio's reference day; the time keeps running. Without it,
    tests on the real demo data change their verdict once a demo project passes its planned end date."""
    from app.core import clock

    real_now = clock.now
    offset = real_now().date() - DEMO_REFERENCE_DAY
    mp.setattr(clock, "now", lambda: real_now() - offset)

# files of the live data/ folder a test run must never change (decisions, proposals, portfolio, accounts)
GUARDED_DATA = ("pending_changes.jsonl", "decision_log.jsonl", "saved_scenarios.jsonl", "business_audit.log",
                "notifications.jsonl", "demo_users.json", "DerKurator_Demo_Portfolio.json",
                "DerKurator_Demo_Portfolio.xlsx")
_data_before: dict[str, str | None] = {}
NOTIFY_SETTINGS = ("NOTIFY_ENABLED", "NOTIFY_SMTP_HOST", "NOTIFY_SMTP_PORT", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD",
                   "NOTIFY_SMTP_FROM", "NOTIFY_SMTP_STARTTLS", "NOTIFY_TEAMS_PMO_URL", "NOTIFY_TEAMS_BOARD_URL",
                   "NOTIFY_PMO_TO", "NOTIFY_BOARD_TO", "NOTIFY_MAX_ATTEMPTS", "APP_BASE_URL")


def _fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def pytest_sessionstart(session):
    _data_before.update({name: _fingerprint(PROJECT_ROOT / "data" / name) for name in GUARDED_DATA})


def pytest_sessionfinish(session, exitstatus):
    """Fails the run if a test wrote into the live data/ folder (a test that lost its isolation)."""
    changed = [name for name, before in _data_before.items() if _fingerprint(PROJECT_ROOT / "data" / name) != before]
    if changed:
        session.config.get_terminal_writer().line(
            f"A test changed the live data folder: {', '.join(changed)} – restore it and fix the test's isolation.",
            red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _project(**overrides: Any) -> dict[str, Any]:
    base = {
        "project_number": "P-1001", "project_name": "Alpha Platform", "project_manager": "Priya Raman",
        "project_sponsor": "Marco Bianchi", "additional_assignee": "Rowan Ellis", "status": "Green",
        "state": "Work in Progress", "execution_type": "Waterfall", "percent_complete": 50.0,
        "planned_start_date": "2025-01-01", "actual_start_date": "2025-01-06", "planned_end_date": FUTURE_END,
        "budget_cost_eur": 420000, "actual_cost_eur": 238000, "estimate_at_completion_eur": 420000,
        "estimate_to_completion_eur": 182000, "total_planned_cost_eur": 420000, "planned_capital_eur": 155000,
        "planned_operating_eur": 265000, "planned_benefit_eur": 510000, "planned_return_eur": 90000,
        "planned_roi_pct": 21.4, "business_case": "Mandatory compliance scope.", "data_quality_profile": "valid",
        "risk_score": 3,
    }
    base.update(overrides)
    return base


def make_dataset(projects: list[dict[str, Any]] | None = None) -> PortfolioDataset:
    projects = projects if projects is not None else [
        _project(),
        _project(project_number="P-1002", project_name="Beta Billing", project_manager="Rowan Ellis",
                 project_sponsor="Ingrid Solberg", execution_type="Agile", status="Yellow",
                 budget_cost_eur=800000, actual_cost_eur=300000, estimate_at_completion_eur=800000,
                 estimate_to_completion_eur=500000, total_planned_cost_eur=800000, planned_capital_eur=500000,
                 planned_operating_eur=300000, planned_benefit_eur=1200000, planned_return_eur=400000, planned_roi_pct=50.0),
        # Data quality problems: sponsor missing, progress without actual start; locked for simulation
        _project(project_number="P-1003", project_name="Gamma Governance", project_manager="Priya Raman",
                 project_sponsor="", actual_start_date="", percent_complete=20.0, data_quality_profile="planning_incomplete"),
    ]
    tables = {
        "Projects": pd.DataFrame(projects),
        "Project_Tasks": pd.DataFrame([
            {"task_number": "T-1", "project_number": "P-1001", "short_description": "Design", "dependency_task_number": "",
             "assigned_to": "Marco Bianchi", "assignment_group": "Team Orion", "state": "Closed Complete", "percent_complete": 100,
             "planned_effort_hours": 80, "actual_start_date": "2025-01-06"},
            {"task_number": "T-2", "project_number": "P-1001", "short_description": "Build", "dependency_task_number": "T-1",
             "assigned_to": "Nora Lindqvist", "assignment_group": "Team Orion", "state": "Open", "percent_complete": 0,
             "planned_effort_hours": 160, "actual_start_date": ""},
            {"task_number": "T-3", "project_number": "P-1002", "short_description": "Sprint 1", "dependency_task_number": "",
             "assigned_to": "Rowan Ellis", "assignment_group": "Team Harbor", "state": "Open", "percent_complete": 0,
             "planned_effort_hours": 120, "actual_start_date": ""},
        ]),
        "Resource_Plans": pd.DataFrame([
            {"resource_plan_number": f"R-{i}", "project_number": pid, "month": "2025-01-01", "assignment_group": team,
             "capacity_hours": 320, "demand_hours": demand, "utilization_pct": round(demand / 320 * 100, 1),
             "requested_fte": 2.0, "allocated_fte": 2.0}
            for i, (pid, team, demand) in enumerate([("P-1001", "Team Orion", 300), ("P-1002", "Team Harbor", 400),
                                                     ("P-1003", "Team Orion", 200)])
        ]),
        "Stakeholders": pd.DataFrame([
            {"stakeholder_number": "S-1", "project_number": "P-1001", "user": "Clara Dubois", "function": "Architect"},
            {"stakeholder_number": "S-2", "project_number": "P-1002", "user": "Fatima Okafor", "function": "Controller"},
        ]),
        "Risks": pd.DataFrame([
            {"risk_number": "RK-1", "project_number": "P-1001", "short_description": "Supplier delay", "probability": "Moderate",
             "impact": "High", "risk_status": "Open", "assigned_to": "Helena Brandt", "systemic_risk_cluster": "Supplier"},
        ]),
        "Cost_Plans": pd.DataFrame([
            {"cost_plan_number": "C-1", "project_number": "P-1001", "month": "2025-01-01", "cost_type": "CapEx",
             "planned_cost_eur": 155000, "actual_cost_eur": 152320},
            {"cost_plan_number": "C-2", "project_number": "P-1001", "month": "2025-01-01", "cost_type": "OpEx",
             "planned_cost_eur": 265000, "actual_cost_eur": 85680},
        ]),
    }
    return PortfolioDataset.from_tables(tables)


@pytest.fixture(autouse=True, scope="session")
def fast_password_checks():
    """A PBKDF2 check costs ~0.3 s by design (600,000 rounds); the UI tests sign in a hundred times with the same few
    passwords. Each (password, hash) pair is checked once per session – the check itself stays the real one."""
    from app.core import auth

    real = auth.verify_password
    checked: dict[tuple[str, str], bool] = {}

    def verify(password: str, encoded: str) -> bool:
        key = (password or "", encoded or "")
        if key not in checked:
            checked[key] = real(password, encoded)
        return checked[key]

    patch = pytest.MonkeyPatch()
    patch.setattr(auth, "verify_password", verify)
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate global state per test: runtime config (never real LLM keys, shadow off), PII registry and
    the required-fields configuration (never read or write the real data/config_required_fields.json)."""
    from app.core import env_config

    # environment files (models, shadowing, required fields) live in a temp dir – never the real data/env_*.json
    monkeypatch.setattr(env_config, "config_dir", lambda: tmp_path)
    # the UI error boundary re-raises in tests, so a real bug still shows up as AppTest exception (not as a banner)
    monkeypatch.setenv("DERKURATOR_ERROR_BOUNDARY", "raise")
    with env_config._issues_lock:
        env_config._config_issues.clear()
    # business audit trail and persisted chats never touch the real data/ folder
    from app.core import cost_tracker, logs

    monkeypatch.setattr(logs, "BUSINESS_AUDIT_LOG", tmp_path / "business_audit.log")
    monkeypatch.setattr(logs, "AUDIT_LOG", tmp_path / "decision_log.jsonl")  # apply_changes writes a pre-write record
    monkeypatch.setattr(logs, "CHAT_DIR", tmp_path / "chat_sessions")
    monkeypatch.setattr(logs, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(logs, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(logs, "PROPOSALS_LOG", tmp_path / "pending_changes.jsonl")
    monkeypatch.setattr(logs, "SCENARIOS_LOG", tmp_path / "saved_scenarios.jsonl")
    monkeypatch.setattr(logs, "NOTIFY_OUTBOX", tmp_path / "notifications.jsonl")
    for name in NOTIFY_SETTINGS:  # a developer's mail or Teams settings never reach a test (notifications stay off)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(logs, "ARCHIVE_DIR", tmp_path / "archive")
    # system log and shadow log too: after pytest, data/app.log is unchanged and `git status` stays clean
    monkeypatch.setattr(logs, "APP_LOG", tmp_path / "app.log")
    monkeypatch.setattr(logs, "SHADOW_LOG", tmp_path / "shadow_comparison_log.jsonl")
    monkeypatch.setattr(logs, "INCIDENTS_LOG", tmp_path / "incidents.jsonl")
    monkeypatch.setattr(logs, "LLM_CALLS_LOG", tmp_path / "llm_calls.jsonl")
    from app.observability import llm_calls

    llm_calls.clear_recent()
    logs.shutdown_logging()  # a file handler opened by an earlier test may still point to another path
    from app.graph import portfolio_graph

    portfolio_graph.reset()  # no graph survives into the next test
    if os.environ.get("GRAPH_BACKEND"):  # the suite runs on the in-memory graph; the Neo4j tests switch it on
        monkeypatch.delenv("GRAPH_BACKEND")
        get_settings.cache_clear()
    cost_tracker.reset()
    from app.core import auth

    monkeypatch.delenv("AUTH_USERS_FILE", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_PASSWORD", raising=False)  # a developer's own admin secret never reaches a test
    monkeypatch.delenv("AUTH_ADMIN_USER", raising=False)  # the shipped demo accounts unless a test sets its own file
    auth.reset_lockouts()
    from app.api import limits, state_store, tokens

    monkeypatch.delenv("STATE_STORE", raising=False)  # the in-memory store unless a test selects the file store
    state_store.reset()  # runs, latest steps and sign-outs of an earlier test are gone
    limits.reset()  # calculation slots and per-minute counters of the API
    tokens.clear_revoked()
    reset_runtime_config()
    update_runtime_config(openai_api_key="", anthropic_api_key="", google_api_key="", pii_masking_enabled=True,
                          agents={a: {"shadow": ""} for a in env_config.AGENTS})
    with security._pii_lock:
        security._pii_registry.clear()
        security._protected_words.clear()
    yield
    logs.shutdown_logging()
    reset_runtime_config()


@pytest.fixture(scope="session")
def offline_evaluation():
    """The evaluation case set through the deterministic offline parser, once per test session: the evaluation test
    and the demo coverage test read the same runs. Returns the evaluation and the verdict of every pipeline run
    (exact plan and free text) with the request it answered."""
    from app.crew import pipeline
    from scripts import run_evals

    if not run_evals.PORTFOLIO.exists():
        pytest.skip("Demo portfolio not available")
    verdicts: list[tuple[str, object]] = []
    original = pipeline.run_steering_pipeline

    def recording(user_prompt, *args, **kwargs):
        result = original(user_prompt, *args, **kwargs)
        verdicts.append((user_prompt, result.verdict))
        return result

    patch = pytest.MonkeyPatch()
    patch.setattr(pipeline, "run_steering_pipeline", recording)
    try:
        # E7/E12 are covered by their own tests in this suite – not re-run from here
        evaluation = run_evals.evaluate("offline", include_pytest_cases=False)
    finally:
        patch.undo()
    return evaluation, verdicts


@pytest.fixture
def dataset() -> PortfolioDataset:
    return make_dataset()


@pytest.fixture
def project_factory():
    return _project


@pytest.fixture
def dataset_factory():
    return make_dataset
