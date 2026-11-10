"""Admin Console: test run integration (without a real pytest subprocess) and UI behavior via Streamlit AppTest.

AppTest rule: explicitly call .run() after EVERY interaction (set_value, check, click, input) – only then
does Streamlit re-run the script and the session state (e.g. a confirmation checkbox) take effect.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from app.core import logs, snapshots, system_tests
from app.core.auth import hash_password
from app.core.config import get_settings
from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_ADMIN, ROLE_PMO
from app.crew.tools import graph_tools
from tests.conftest import pin_demo_reference_day

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN = PROJECT_ROOT / "app" / "main.py"
SOURCE_JSON = PROJECT_ROOT / "data" / "DerKurator_Demo_Portfolio.json"

JUNIT_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
  <testcase classname="tests.test_x.TestA" name="test_ok" time="0.010"/>
  <testcase classname="tests.test_x.TestA" name="test_ok_too" time="0.020"/>
  <testcase classname="tests.test_x.TestA" name="test_broken" time="0.030">
    <failure message="assert 1 == 2">Traceback ...</failure>
  </testcase>
</testsuite></testsuites>
"""


# =========================================================================== test run integration
@pytest.fixture
def runner_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(logs, "TEST_REPORT", tmp_path / "test_report.html")
    return tmp_path


def _fake_run(returncode: int, calls: list):
    def fake(cmd, **kwargs):
        calls.append((cmd, kwargs))
        junit = next(a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml="))
        Path(junit).write_text(JUNIT_SAMPLE, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout="3 tests, 1 failed", stderr="")
    return fake


class TestSystemTestRunner:
    def test_command_uses_app_interpreter_and_html_report(self, runner_paths, monkeypatch):
        calls: list = []
        monkeypatch.setattr(system_tests.subprocess, "run", _fake_run(0, calls))
        system_tests.run_system_tests()
        cmd, kwargs = calls[0]
        assert cmd[:4] == [sys.executable, "-m", "pytest", "tests/"]
        assert f"--html={runner_paths / 'test_report.html'}" in cmd and "--self-contained-html" in cmd
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["cwd"] == system_tests.PROJECT_ROOT
        scratch = kwargs["env"]["DERKURATOR_WRITE_DIR"]  # the run writes logs and proposals outside the live data/
        assert scratch == kwargs["env"]["DERKURATOR_LOG_DIR"] and Path(scratch) != logs.DATA_DIR
        assert not Path(scratch).exists()  # removed after the run

    def test_failed_run_is_reported_with_parsed_results(self, runner_paths, monkeypatch):
        monkeypatch.setattr(system_tests.subprocess, "run", _fake_run(1, []))
        run = system_tests.run_system_tests()
        assert run.returncode == 1 and not run.passed
        assert run.counts == {"passed": 2, "failed": 1, "error": 0, "skipped": 0}
        broken = next(c for c in run.cases if c["outcome"] == "failed")
        assert broken["test"] == "test_broken" and broken["message"] == "assert 1 == 2"
        assert run.stdout == "3 tests, 1 failed"

    def test_timeout_is_reported_instead_of_raising(self, runner_paths, monkeypatch):
        def timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5, output=b"partial")
        monkeypatch.setattr(system_tests.subprocess, "run", timeout)
        run = system_tests.run_system_tests(timeout=5)
        assert run.returncode == -1 and "Timeout" in run.stderr and run.stdout == "partial"
        assert run.counts == {}

    def test_parallel_runs_are_rejected(self, runner_paths):
        assert system_tests._run_lock.acquire(blocking=False)
        try:
            with pytest.raises(system_tests.SystemTestsBusy):
                system_tests.run_system_tests()
        finally:
            system_tests._run_lock.release()

    def test_missing_junit_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            system_tests.parse_junit(tmp_path / "missing.xml")


# =========================================================================== UI (AppTest)
@pytest.fixture(scope="module")
def app_sandbox(tmp_path_factory):
    """Isolated app environment: copy of the data, own graph, all logs in the temp directory."""
    if not SOURCE_JSON.exists():
        pytest.skip("Portfolio-JSON nicht vorhanden")
    tmp = tmp_path_factory.mktemp("admin_ui")
    shutil.copy(SOURCE_JSON, tmp / "portfolio.json")
    shutil.copy(PROJECT_ROOT / "data" / "supported_models.json", tmp / "supported_models.json")
    mp = pytest.MonkeyPatch()
    for key, value in {"JSON_PATH": tmp / "portfolio.json", "DATA_SOURCE": "json",
                       "ENVIRONMENT": "DEV"}.items():
        mp.setenv(key, str(value))
    pin_demo_reference_day(mp)
    # the public demo has no admin account: the owner adds one as a real account (AUTH_EXTRA_USERS)
    mp.setenv("AUTH_EXTRA_USERS", json.dumps({"users": [{"user_id": "SYSTEM-ADMIN", "role": ROLE_ADMIN,
                                                         "password_hash": hash_password(DEMO_PASSWORD)}]}))
    for name, value in {"DATA_DIR": tmp, "APP_LOG": tmp / "app.log", "AUDIT_LOG": tmp / "decision_log.jsonl",
                        "SHADOW_LOG": tmp / "shadow_comparison_log.jsonl", "TEST_REPORT": tmp / "test_report.html",
                        "ARCHIVE_DIR": tmp / "archive"}.items():
        mp.setattr(logs, name, value)
    logs.shutdown_logging()  # never write to the real data/app.log
    get_settings.cache_clear()
    yield tmp
    logs.shutdown_logging()
    mp.undo()
    get_settings.cache_clear()


DEMO_PASSWORD = json.loads((PROJECT_ROOT / "data" / "demo_users.json").read_text(encoding="utf-8"))["public_demo_password"]
# maps former identity labels to the user IDs of the demo user file
USER_IDS = {ROLE_ADMIN: "SYSTEM-ADMIN", ROLE_PMO: "PMO-ADMIN", "PMO (Full Access) – Deputy": "PMO-DEPUTY"}


def _sign_in(at: AppTest, login: str, password: str = DEMO_PASSWORD) -> AppTest:
    """Signs in through the sign-in form (same check as in the browser)."""
    at.text_input(key="login_user").input(USER_IDS.get(login, login))
    at.text_input(key="login_password").input(password)
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception, at.exception
    return at


def _app(page: str | None = None, login: str | None = "PMO-ADMIN", **state) -> AppTest:
    """Fresh session, signed in as `login` (default PMO-ADMIN; None = stay on the sign-in page). `page` opens that
    navigation page (no URL under AppTest), further keyword arguments are pre-set session state (e.g. detail_pid for
    the project detail page)."""
    st.cache_data.clear()  # do not share graph status/data cache between tests
    at = AppTest.from_file(str(MAIN), default_timeout=90)
    if page:
        at.session_state["nav_page"] = page
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception, at.exception
    return _sign_in(at, login) if login is not None else at


def _login(at: AppTest, selection: str) -> AppTest:
    """Switch identity: sign out, then sign in as `selection`."""
    at.button(key="sign_out").click().run()
    assert not at.exception, at.exception
    return _sign_in(at, selection)


def _detail(pid: str, login: str | None = None) -> AppTest:
    """Project detail page of `pid` (edit form in its 'Edit' tab for the owning PM)."""
    return _app("projects", login or "PMO-ADMIN", detail_pid=pid)


def _save_after_input(at: AppTest, label: str = "Save changes") -> AppTest:
    """The save button reacts live to the edits – like the browser (input commits on blur), rerun first, then
    click the now enabled button."""
    at.run()
    next(b for b in at.button if b.label == label).click()
    return at.run()


def _markdown(at: AppTest) -> list[str]:
    return [m.value for m in at.markdown]


class TestAdminConsoleUI:
    def test_workspace_is_split_into_pages(self, app_sandbox):
        at = _app()  # Overview is the default page
        assert "Overview" in [h.value for h in at.header]
        assert "Portfolio analysis" in [h.value for h in at.subheader]
        assert any(m.label == "Data quality index" for m in at.metric)
        assert not any(k.startswith("sb_") for k in (b.key or "" for b in at.button))  # the console is its own page
        assert not any("LIVE PRODUCTION" in e.value for e in at.error)
        console = _app("console")
        assert "Steering Console" in [h.value for h in console.header]
        assert console.button(key="sb_run").label == "Simulate"
        quality = _app("data_quality")
        assert any(m.label == "Data quality index" for m in quality.metric)
        assert any("Executive interpretation" in m for m in _markdown(quality))

    def test_decision_insights_page(self, app_sandbox):
        at = _app("insights")
        assert "Decision insights" in [h.value for h in at.header]
        subheaders = [h.value for h in at.subheader]
        assert {"Status check of the sources", "Portfolio with probabilities", "Review of past decisions"} <= \
            set(subheaders)
        assert any(m.label == "Within budget" for m in at.metric) and not at.exception

    def test_guided_scenario_runs_from_steering_bar(self, app_sandbox):
        at = _app("console")
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1002").run()
        at.radio(key="sb_cost_type").set_value("capex").run()
        at.button(key="sb_run").click().run()  # .run() after the click
        assert not at.exception, at.exception
        request = at.session_state["messages"][-2]["content"]
        assert "Scenario:" in request and "Cut CapEx of PRJ-DEMO-1002" in request
        assert any("Cut CapEx of PRJ-DEMO-1002" in m for m in _markdown(at))  # history rail + report header
        assert any("Budget change | " in m for m in _markdown(at))  # paper section 1 in its tab

    def test_pmo_head_configures_required_fields_on_the_rules_page(self, app_sandbox):
        from app.core import required_fields

        at = _app("rules")  # PMO-ADMIN is the PMO Head
        assert "Rules" in [h.value for h in at.header]
        current = at.multiselect(key="admin_required_fields").value
        at.multiselect(key="admin_required_fields").set_value([f for f in current if f != "project_sponsor"])
        at.button(key="FormSubmitter:required_fields_form-Save required fields").click().run()
        assert not at.exception, at.exception
        saved = json.loads(required_fields.config_path().read_text(encoding="utf-8"))["required_fields"]
        assert "project_sponsor" not in saved and "project_number" in saved

    def test_admin_settings_show_matrix_staging_and_add_model(self, app_sandbox):
        at = _login(_app(), ROLE_ADMIN)
        assert any("Per-agent model matrix" in m.value for m in at.markdown)
        assert at.button(key="promote_dev_test").label == "Promote config to TEST"
        assert at.button(key="promote_test_prod").disabled  # PROD promotion needs explicit confirmation
        assert any(e.label == "Add new LLM" for e in at.expander)

    def test_add_model_requires_a_valid_provider_key(self, app_sandbox):
        at = _login(_app(), ROLE_ADMIN)
        at.text_input(key="byom_model_id").input("gpt-4.1-nano").run()
        at.button(key="byom_add").click().run()
        assert not at.exception, at.exception
        assert any("OPENAI_API_KEY is not set" in e.value for e in at.error)

    def test_promote_config_to_test_writes_env_test_file(self, app_sandbox):
        from app.core.env_config import env_config_path

        at = _login(_app(), ROLE_ADMIN)
        assert at.button(key="promote_dev_test").disabled  # promotion needs an explicit confirmation
        at.checkbox(key="confirm_promote_test").check().run()
        at.button(key="promote_dev_test").click().run()
        assert not at.exception, at.exception
        assert env_config_path("TEST").exists()
        assert any("promoted to TEST" in s.value for s in at.success)

    def test_switching_environment_loads_its_config_file(self, app_sandbox):
        from app.core.runtime_config import get_runtime_config

        at = _login(_app(), ROLE_ADMIN)
        assert at.selectbox(key="admin_env_select").disabled  # a switch needs an explicit confirmation
        at.checkbox(key="confirm_env_switch").check().run()
        at.selectbox(key="admin_env_select").set_value("TEST").run()
        assert not at.exception, at.exception
        assert get_runtime_config().environment == "TEST"
        assert any("Environment switched to TEST" in s.value for s in at.success)
        at.checkbox(key="confirm_env_switch").check().run()
        at.selectbox(key="admin_env_select").set_value("DEV").run()
        assert get_runtime_config().environment == "DEV"

    def test_prod_is_refused_while_the_demo_accounts_are_active(self, app_sandbox):
        from app.core.runtime_config import get_runtime_config

        at = _login(_app(), ROLE_ADMIN)
        at.checkbox(key="confirm_env_switch").check().run()
        at.selectbox(key="admin_env_select").set_value("PROD").run()
        assert not at.exception, at.exception
        assert get_runtime_config().environment == "DEV"  # the hosted demo cannot be locked out
        assert any("needs real accounts" in e.value for e in at.error)
        fresh = _app(login=None)  # a new visitor still gets the sign-in form
        assert any(b.label == "Sign in" for b in fresh.button)

    def test_technical_status_only_visible_for_system_admin(self, app_sandbox):
        at = _app()  # PMO
        status = [s.value for s in list(at.success) + list(at.warning)]
        assert not any("Dependency graph" in s for s in status)
        at = _login(at, ROLE_ADMIN)
        status = [s.value for s in list(at.success) + list(at.warning)]
        assert any("Dependency graph" in s for s in status)  # Admin Console › System status
        assert any(m.label == "Environment" for m in at.metric)

    def test_top_risks_are_shown_first(self, app_sandbox):
        at = _app()
        assert any("blocked" in e.value or "Systemic risk" in e.value for e in list(at.error) + list(at.warning))


class TestReleaseCandidateUI:
    def _run_budget_scenario(self) -> AppTest:
        at = _app("console")
        at.selectbox(key="sb_action").set_value("budget_transfer").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1002").run()
        at.selectbox(key="sb_target").set_value("PRJ-DEMO-1001").run()
        at.button(key="sb_run").click().run()
        assert not at.exception, at.exception
        return at

    def test_budget_shift_shows_delta_chart_export_and_cost_footer(self, app_sandbox):
        at = self._run_budget_scenario()
        assert "Impact" in [t.label for t in at.tabs] and at.get("plotly_chart")  # before/after delta chart
        assert at.get("download_button")  # SteerCo Pack + analysis export + history export
        # token/cost figures are IT controlling – never shown to PMO / PM
        assert not any("Estimated session costs" in c.value or "tokens" in c.value for c in at.caption)

    def test_project_stop_shows_interactive_dependency_graph(self, app_sandbox):
        at = _app("console")
        at.selectbox(key="sb_action").set_value("cancel").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1001").run()
        at.button(key="sb_run").click().run()
        assert not at.exception, at.exception
        assert any(t.label.startswith("Dependencies (") and t.label != "Dependencies (0)" for t in at.tabs)
        assert at.get("plotly_chart")

    def test_older_report_keeps_its_dependency_graph(self, app_sandbox):
        at = _app("console")
        at.selectbox(key="sb_action").set_value("cancel").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1001").run()
        at.button(key="sb_run").click().run()
        first = at.session_state["selected_run"]
        at.toggle(key="sb_continue").set_value(False).run()
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.button(key="sb_run").click().run()
        at.button(key=f"run_open_{first}").click().run()  # back to the older report
        assert not at.exception, at.exception
        assert at.session_state["selected_run"] == first and at.get("plotly_chart")

    def test_chat_history_survives_page_refresh(self, app_sandbox):
        self._run_budget_scenario()
        refreshed = _app("console")  # a browser reload (F5) starts a new Streamlit session
        contents = [m["content"] for m in refreshed.session_state["messages"] if m["role"] == "user"]
        assert any("Transfer" in c and "PRJ-DEMO-1002" in c for c in contents)
        refreshed.button(key="clear_chat").click().run()
        assert refreshed.session_state["messages"]  # nothing is deleted before the confirmation
        refreshed.button(key="clear_chat_confirm").click().run()
        assert not refreshed.session_state["messages"] and not _app("console").session_state["messages"]

    def test_admin_saves_mcp_integration_settings(self, app_sandbox):
        from app.core.env_config import env_config_path

        at = _login(_app(), ROLE_ADMIN)
        at.text_input(key="mcp_url_servicenow").input("https://servicenow.example.com/mcp")
        at.toggle(key="mcp_enabled").set_value(True)
        at.button(key="FormSubmitter:mcp_settings-Save integrations").click().run()
        assert not at.exception, at.exception
        stored = json.loads(env_config_path("DEV").read_text(encoding="utf-8"))["mcp"]
        assert stored == {"enabled": True, "servers": {"servicenow": "https://servicenow.example.com/mcp", "jira": ""}}
        assert any("architecture building block" in w.value for w in at.warning)

    def test_admin_finops_panel_and_creativity_slider(self, app_sandbox):
        at = _login(_app(), ROLE_ADMIN)
        assert any(s.value == "FinOps – estimated LLM costs" for s in at.subheader)
        assert at.slider(key="admin_temperature").label == "Agent creativity (temperature)"
        key_inputs = [t for t in at.text_input if (t.key or "").startswith("admin_") and t.key.endswith("_key")]
        assert len(key_inputs) == 3 and all(t.proto.type == t.proto.PASSWORD for t in key_inputs)


class TestUiSafeguards:
    """Admin config banner, clamped detail view, error boundary."""

    @pytest.fixture
    def patched_project(self, app_sandbox):
        """Temporarily gives PRJ-DEMO-1010 out-of-range values; the sandbox JSON is restored afterwards."""
        path = app_sandbox / "portfolio.json"
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        project = next(p for p in data["Projects"] if p["project_number"] == "PRJ-DEMO-1010")
        project.update(percent_complete=120, risk_score=12, planned_end_date="1999-12-31")
        path.write_text(json.dumps(data), encoding="utf-8")
        yield path
        path.write_text(original, encoding="utf-8")

    def test_out_of_range_values_are_clamped_flagged_and_not_written_back(self, app_sandbox, patched_project):
        at = _detail("PRJ-DEMO-1010", "Yusuf Demir")  # Streamlit would raise StreamlitValueAboveMaxError without clamping
        assert sum("outside the valid range" in c.value for c in at.caption) == 3
        next(t for t in at.text_input if (t.key or "").endswith("_project_sponsor")).input("Closing")
        _save_after_input(at)
        assert not at.exception, at.exception
        project = next(p for p in json.loads(patched_project.read_text(encoding="utf-8"))["Projects"]
                       if p["project_number"] == "PRJ-DEMO-1010")
        assert project["project_sponsor"] == "Closing"
        # untouched out-of-range fields are never written back with their clamped display value
        assert (project["percent_complete"], project["risk_score"], project["planned_end_date"]) == (120, 12, "1999-12-31")

    def test_error_boundary_hides_traceback_from_end_users(self, app_sandbox, monkeypatch):
        from app.ui import overview

        def boom(*args, **kwargs):
            raise RuntimeError("secret internal detail 4711")

        monkeypatch.delenv("DERKURATOR_ERROR_BOUNDARY", raising=False)
        monkeypatch.setattr(overview, "render_kpi_ribbon", boom)
        at = _app()  # PMO, Overview
        assert not at.exception
        errors = [e.value for e in at.error]
        assert any("A system error occurred (Incident ID:" in e for e in errors)
        assert not any("secret internal detail" in e or "Traceback" in e for e in errors + [c.value for c in at.code])
        assert "Portfolio analysis" in [h.value for h in at.subheader]  # the rest of the page still renders
        records, _ = logs.read_jsonl(logs.INCIDENTS_LOG)
        incident = [r for r in records if r.get("event") == "ui_error"][-1]
        assert incident["section"] == "KPI ribbon" and incident["error_type"] == "RuntimeError"
        assert "secret internal detail" not in json.dumps(incident)
        decisions = logs.read_jsonl(logs.AUDIT_LOG)[0] if logs.AUDIT_LOG.exists() else []
        assert not any(r.get("event") == "ui_error" for r in decisions)  # the decision log holds decisions only

    def test_error_boundary_shows_traceback_only_to_the_admin(self, app_sandbox, monkeypatch):
        from app.ui import admin_ui

        def boom(*args, **kwargs):
            raise RuntimeError("audit tab exploded")

        monkeypatch.delenv("DERKURATOR_ERROR_BOUNDARY", raising=False)
        monkeypatch.setattr(admin_ui, "render_audit_logs", boom)
        at = _login(_app(), ROLE_ADMIN)
        assert not at.exception
        assert any(e.label.startswith("Technical details for incident") for e in at.expander)
        assert any("audit tab exploded" in c.value and "Traceback" in c.value for c in at.code)
        assert any(s.value == "FinOps – estimated LLM costs" for s in at.subheader)  # other tabs unaffected

    def test_invalid_env_file_shows_admin_banner_instead_of_crashing(self, app_sandbox):
        from app.core.env_config import env_config_path
        from app.core.runtime_config import reset_runtime_config

        env_config_path("DEV").write_text(json.dumps({"temperature": "abc"}), encoding="utf-8")
        reset_runtime_config()
        at = _app()
        assert not at.exception, at.exception
        at = _login(at, ROLE_ADMIN)
        assert any("Environment config not applied" in e.value for e in at.error)


class TestBusinessRulesUI:
    def test_pmo_head_changes_business_rules_and_workspace_shows_them(self, app_sandbox):
        from app.core.env_config import env_config_path
        from app.core.runtime_config import get_runtime_config

        at = _app("rules")  # PMO-ADMIN is the PMO Head
        at.number_input(key="rule_fte_monthly_cost_eur").set_value(9800.0)
        at.button(key="FormSubmitter:business_rules_form-Save business rules").click().run()
        assert not at.exception, at.exception
        assert any("business rule(s) saved" in s.value for s in at.success)
        assert get_runtime_config().business_rules.fte_monthly_cost_eur == 9800
        stored = json.loads(env_config_path("DEV").read_text(encoding="utf-8"))["business_rules"]
        assert stored["fte_monthly_cost_eur"] == 9800
        pmo = _app("console")
        assert any("€9,800" in m for m in _markdown(pmo))  # "Rules in use" popover for PMO / PM

        at = _app("rules")
        at.button(key="reset_business_rules").click().run()  # resetting asks first
        assert get_runtime_config().business_rules.fte_monthly_cost_eur == 9800
        at.button(key="reset_business_rules_cancel").click().run()
        assert get_runtime_config().business_rules.fte_monthly_cost_eur == 9800
        at.button(key="reset_business_rules").click().run()
        at.button(key="reset_business_rules_confirm").click().run()
        assert not at.exception, at.exception
        assert get_runtime_config().business_rules.fte_monthly_cost_eur != 9800

    def test_other_pmo_reads_the_rules_and_the_core_refuses_changes(self, app_sandbox):
        from app.core.runtime_config import get_runtime_config
        from app.views import admin as views

        at = _app("rules", login="PMO-DEPUTY")
        assert any("only the PMO Head changes these rules" in i.value for i in at.info)
        assert at.number_input(key="rule_fte_monthly_cost_eur").disabled
        assert not any(b.key == "reset_business_rules" for b in at.button)
        before = get_runtime_config().business_rules.fte_monthly_cost_eur
        with pytest.raises(PermissionError):
            views.save_business_rules({"fte_monthly_cost_eur": 9800}, "PMO-DEPUTY", ROLE_PMO)
        with pytest.raises(PermissionError):
            views.save_business_rules({"fte_monthly_cost_eur": 9800}, "ADMIN", ROLE_ADMIN)
        assert get_runtime_config().business_rules.fte_monthly_cost_eur == before

    def test_navigation_follows_the_role(self, app_sandbox):
        pmo = _app()
        assert [p.title for p in pmo.session_state["_pages"].values() if p.visibility == "visible"] == [
            "Overview", "Projects", "Steering Console", "Approvals", "Planning", "Rules"]
        pm = _app("projects", login="Priya Raman")
        assert [p.title for p in pm.session_state["_pages"].values() if p.visibility == "visible"] == [
            "Overview", "My projects", "Steering Console", "My proposals", "My demands"]
        assert "My projects" in [h.value for h in pm.header]
        assert any(c.value.startswith("2 projects visible") for c in pm.caption)  # no portfolio total for a PM
        pm.button(key="projects_data_quality").click().run()
        assert pm.session_state["nav_page"] == "data_quality"

    def test_offline_paper_carries_rule_based_verdict(self, app_sandbox):
        at = _app("console")
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1002").run()
        at.button(key="sb_run").click().run()
        idx = at.session_state["selected_run"]
        at.segmented_control(key=f"paper_section_{idx}").set_value("Full paper").run()
        assert any("Verdict (rule-based, not AI-generated)" in m for m in _markdown(at))
        assert any("Why this verdict" in m for m in _markdown(at))  # reasons are shown in the Summary tab


class TestExecutiveWorkspaceUI:
    def _cut(self, at: AppTest, pid: str, pct: float) -> AppTest:
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.selectbox(key="sb_project").set_value(pid).run()
        at.number_input(key="sb_value_True").set_value(pct).run()
        at.button(key="sb_run").click().run()
        assert not at.exception, at.exception
        return at

    def test_project_detail_is_read_only_with_history_costs_deadlines_and_dependencies(self, app_sandbox):
        at = _detail("PRJ-DEMO-1002")  # PMO
        assert any(s.value.startswith("PRJ-DEMO-1002") for s in at.header)
        labels = [t.label for t in at.tabs]
        for expected in ("Master data", "Status history", "Costs & baselines", "Schedule & dependencies", "Risks"):
            assert expected in labels
        assert "Edit" not in labels  # no edit tab without edit rights
        assert any(label.startswith("Data quality (") for label in labels)
        assert any(m.label == "Planned end" for m in at.metric)
        assert not any("Save changes" in b.label for b in at.button)  # PMO: no edit form

    def test_owner_sees_edit_form_in_edit_tab(self, app_sandbox):
        at = _detail("PRJ-DEMO-1002", "Priya Raman")  # owns PRJ-DEMO-1002
        assert "Edit" in [t.label for t in at.tabs]
        assert any(b.label == "Save changes" and b.disabled for b in at.button)  # nothing changed yet

    def test_simulate_from_project_detail_prefills_the_console(self, app_sandbox):
        at = _detail("PRJ-DEMO-1002", "Priya Raman")
        at.button(key="detail_simulate").click().run()
        assert not at.exception, at.exception
        assert at.session_state["nav_page"] == "console"
        assert at.selectbox(key="sb_project").value == "PRJ-DEMO-1002"

    def test_continue_scenario_builds_on_previous_step(self, app_sandbox):
        at = self._cut(_app("console"), "PRJ-DEMO-1002", 10.0)
        assert at.toggle(key="sb_continue").value is False  # memory offered after the first step, off by default
        at.toggle(key="sb_continue").set_value(True).run()
        at = self._cut(at, "PRJ-DEMO-1002", 10.0)  # "... and now another 10 %"
        last = [m for m in at.session_state["messages"] if m["role"] == "assistant"][-1]
        budgets = [c["new"] for c in last["meta"]["proposed_changes"] if c["field"] == "budget_cost_eur"]
        assert budgets == [1665000.0, 1498500.0]  # 1,850k -> 1,665k (step 1) -> 1,498.5k (step 2 on top)
        assert last["meta"]["continued"] and len(last["meta"]["scenario_state"]["turns"]) == 2
        assert any("step 2" in c.value for c in at.caption)  # report header names the step

    def test_save_two_scenarios_and_compare_them(self, app_sandbox):
        from app.core import scenario_store

        at = self._cut(_app("console"), "PRJ-DEMO-1002", 10.0)
        idx = len(at.session_state["messages"]) - 1
        at.text_input(key=f"save_name_{idx}").input("Scenario A – soft cut").run()
        at.button(key=f"save_scenario_{idx}").click().run()
        assert not at.exception, at.exception
        assert any("saved" in s.value for s in at.success)
        assert at.toggle(key="sb_continue").value is False  # scenario B starts from the data by default
        at = self._cut(at, "PRJ-DEMO-1002", 30.0)
        idx = len(at.session_state["messages"]) - 1
        at.text_input(key=f"save_name_{idx}").input("Scenario B – hard cut").run()
        at.button(key=f"save_scenario_{idx}").click().run()
        assert not at.exception, at.exception
        saved = scenario_store.list_scenarios(ROLE_PMO, "PMO-ADMIN")
        assert [s["name"] for s in saved] == ["Scenario B – hard cut", "Scenario A – soft cut"]
        assert saved[0]["budget_impact"] == {"PRJ-DEMO-1002": -555000.0} and saved[1]["budget_impact"] == {"PRJ-DEMO-1002": -185000.0}
        compare = _app("scenarios")
        assert "What-if comparison" in [h.value for h in compare.header]
        assert compare.selectbox(key="ab_a").value != compare.selectbox(key="ab_b").value
        assert any("Net budget impact" in str(df.value.to_dict()) for df in compare.dataframe)


class TestFourEyesWorkflow:  # PM proposes -> PMO approves in the inbox
    def _pm_cuts_capex(self) -> AppTest:
        at = _app("console", "Priya Raman")  # owns PRJ-DEMO-1002 (not blocked)
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1002").run()
        at.radio(key="sb_cost_type").set_value("capex").run()
        at.button(key="sb_run").click().run()
        assert not at.exception, at.exception
        return at

    def test_pm_cannot_apply_budget_changes_and_submits_a_proposal(self, app_sandbox):
        from app.core import proposals

        at = self._pm_cuts_capex()
        keys = [b.key or "" for b in at.button]
        assert not any(k.startswith("approve_") for k in keys)  # no direct write for the PM
        next(b for b in at.button if (b.key or "").startswith("propose_")).click().run()
        assert not at.exception, at.exception
        assert any("submitted" in s.value for s in at.success)
        pending = proposals.list_proposals(status=proposals.SUBMITTED)
        assert len(pending) == 1 and pending[0]["requester"] == "Priya Raman"
        mine = _app("approvals", "Priya Raman")
        assert any(s.value == "My proposals" for s in mine.subheader)

    def test_pmo_approves_proposal_from_inbox_and_data_is_written(self, app_sandbox):
        from app.core import proposals

        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        try:
            pm = self._pm_cuts_capex()
            next(b for b in pm.button if (b.key or "").startswith("propose_")).click().run()
            proposal = proposals.list_proposals(status=proposals.SUBMITTED)[0]
            at = _app("approvals", inbox_pick=proposal["id"])  # default login = PMO
            assert any(s.value == "Pending approvals (1)" for s in at.subheader)
            at.button(key=f"inbox_approve_{proposal['id']}").click().run()
            assert not at.exception, at.exception
            assert proposals.get_proposal(proposal["id"])["status"] == proposals.SUBMITTED  # dialog first
            at.button(key=f"inbox_approve_confirm_{proposal['id']}").click().run()
            assert not at.exception, at.exception
            assert any("approved" in s.value and "snapshot" in s.value for s in at.success)
            stored = proposals.get_proposal(proposal["id"])
            assert stored["status"] == proposals.APPROVED and stored["decided_by"] == "PMO-ADMIN"
            projects = json.loads(source.read_text(encoding="utf-8"))["Projects"]
            capex = next(p for p in projects if p["project_number"] == "PRJ-DEMO-1002")["planned_capital_eur"]
            assert capex == pytest.approx(1100000 * 0.9)
        finally:
            source.write_text(original, encoding="utf-8")

    def test_inbox_decides_nothing_before_a_proposal_is_picked(self, app_sandbox):
        from app.core import proposals

        pm = self._pm_cuts_capex()
        next(b for b in pm.button if (b.key or "").startswith("propose_")).click().run()
        proposal = proposals.list_proposals(status=proposals.SUBMITTED)[0]
        at = _app("approvals")
        assert any("Select a proposal" in i.value for i in at.info)
        assert not any((b.key or "").startswith("inbox_approve_") for b in at.button)
        row = at.dataframe[0].value.iloc[0]
        assert "PRJ-DEMO-1002 General Ledger Consolidation" in row["Projects"] and row["Proposal"] == proposal["id"]

    def test_inbox_approve_cancel_writes_nothing(self, app_sandbox):
        from app.core import proposals

        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        pm = self._pm_cuts_capex()
        next(b for b in pm.button if (b.key or "").startswith("propose_")).click().run()
        proposal = proposals.list_proposals(status=proposals.SUBMITTED)[0]
        at = _app("approvals", inbox_pick=proposal["id"])
        at.button(key=f"inbox_approve_{proposal['id']}").click().run()
        assert any("write" in w.value and "change(s)" in w.value for w in at.warning)
        at.button(key=f"inbox_approve_cancel_{proposal['id']}").click().run()
        assert not any(b.key == f"inbox_approve_confirm_{proposal['id']}" for b in at.button)
        assert proposals.get_proposal(proposal["id"])["status"] == proposals.SUBMITTED
        assert source.read_text(encoding="utf-8") == original

    def test_no_go_proposal_needs_explicit_override_with_reason(self, app_sandbox):
        from app.core import proposals

        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        budget = next(p for p in json.loads(original)["Projects"] if p["project_number"] == "PRJ-DEMO-1002")["budget_cost_eur"]
        change = [{"table": "Projects", "project_id": "PRJ-DEMO-1002", "field": "budget_cost_eur", "old": budget,
                   "new": 1000000.0}]
        entry = proposals.submit_proposal("Priya Raman", "Project Manager", "Cut ERP by 50 %", "paper",
                                          "NO-GO", change)
        try:
            at = _app("approvals", inbox_pick=entry["id"])
            assert any("explicit override" in c.value for c in at.caption)
            at.button(key=f"inbox_approve_{entry['id']}").click().run()
            assert at.button(key=f"inbox_approve_confirm_{entry['id']}").disabled  # no override yet
            at.checkbox(key=f"override_inbox_{entry['id']}").check().run()
            assert at.button(key=f"inbox_approve_confirm_{entry['id']}").disabled  # reason still missing
            at.text_area(key=f"override_reason_inbox_{entry['id']}").input("Board decided on 2026-10-01").run()
            at.button(key=f"inbox_approve_confirm_{entry['id']}").click().run()
            assert not at.exception, at.exception
            stored = proposals.get_proposal(entry["id"])
            assert stored["status"] == proposals.APPROVED and stored["override"] is True
            assert stored["comment"] == "Board decided on 2026-10-01"
            assert any("with override" in s.value for s in at.success)
        finally:
            source.write_text(original, encoding="utf-8")

    def test_pm_withdraws_an_own_proposal(self, app_sandbox):
        from app.core import proposals

        pm = self._pm_cuts_capex()
        next(b for b in pm.button if (b.key or "").startswith("propose_")).click().run()
        proposal = proposals.list_proposals(status=proposals.SUBMITTED)[0]
        mine = _app("approvals", "Priya Raman")
        mine.selectbox(key="withdraw_pick").select_index(0).run()
        mine.button(key=f"withdraw_{proposal['id']}").click().run()
        assert proposals.get_proposal(proposal["id"])["status"] == proposals.SUBMITTED  # confirmation first
        mine.button(key=f"withdraw_{proposal['id']}_confirm").click().run()
        assert not mine.exception, mine.exception
        assert proposals.get_proposal(proposal["id"])["status"] == proposals.WITHDRAWN
        assert _app("approvals").subheader[0].value == "Pending approvals (0)"

    def test_a_deputy_approves_on_behalf_of_the_pmo(self, app_sandbox):
        from app.core import clock, proposals

        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        try:
            pm = self._pm_cuts_capex()
            next(b for b in pm.button if (b.key or "").startswith("propose_")).click().run()
            proposal = proposals.list_proposals(status=proposals.SUBMITTED)[0]
            proposals.grant_delegation("PMO-ADMIN", "PMO (Full Access)", "PMO-DEPUTY", clock.today(), clock.today(),
                                       "vacation")
            at = _app("approvals", "PMO-DEPUTY", inbox_pick=proposal["id"])
            assert any("deputy of **PMO-ADMIN**" in i.value for i in at.info)
            at.button(key=f"inbox_approve_{proposal['id']}").click().run()
            at.button(key=f"inbox_approve_confirm_{proposal['id']}").click().run()
            assert not at.exception, at.exception
            stored = proposals.get_proposal(proposal["id"])
            assert stored["status"] == proposals.APPROVED and stored["on_behalf_of"] == "PMO-ADMIN"
        finally:
            source.write_text(original, encoding="utf-8")

    def test_pmo_delegates_in_the_approvals_page(self, app_sandbox):
        from app.core import proposals

        at = _app("approvals")
        at.segmented_control(key="approvals_view").set_value("delegations").run()
        at.selectbox(key="delegation_deputy").set_value("PMO-DEPUTY")
        at.text_input(key="delegation_reason").input("vacation")
        next(b for b in at.button if "Delegate" in (b.label or "")).click().run()
        assert not at.exception, at.exception
        assert proposals.active_delegation("PMO-DEPUTY")["delegator"] == "PMO-ADMIN"

    def test_pmo_rejects_with_reason(self, app_sandbox):
        from app.core import proposals

        pm = self._pm_cuts_capex()
        next(b for b in pm.button if (b.key or "").startswith("propose_")).click().run()
        proposal = proposals.list_proposals(status=proposals.SUBMITTED)[0]
        at = _app("approvals", inbox_pick=proposal["id"])
        assert at.button(key=f"inbox_reject_{proposal['id']}").disabled  # a rejection needs a reason
        at.text_input(key=f"inbox_comment_{proposal['id']}").input("Budget freeze until Q1").run()
        at.button(key=f"inbox_reject_{proposal['id']}").click().run()
        assert proposals.get_proposal(proposal["id"])["status"] == proposals.REJECTED
        at.segmented_control(key="approvals_view").set_value("decided").run()
        assert "Decided proposals" in [h.value for h in at.subheader]
        assert any("Budget freeze until Q1" in str(df.value.to_dict()) for df in at.dataframe)

    def test_approve_and_apply_needs_confirmation(self, app_sandbox):
        at = _app("console")
        at.selectbox(key="sb_action").set_value("resource_transfer").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1002").run()
        at.selectbox(key="sb_target").set_value("PRJ-DEMO-1001").run()
        at.button(key="sb_run").click().run()
        idx = len(at.session_state["messages"]) - 1
        assert "hand-over" in at.button(key=f"approve_{idx}").label  # resource plans are executed by hand
        at.button(key=f"approve_{idx}").click().run()
        assert not at.exception, at.exception
        assert any("Nothing is written" in i.value for i in at.info)  # confirmation dialog
        assert not at.session_state["messages"][idx]["meta"].get("applied")  # nothing written yet
        at.button(key=f"approve_cancel_{idx}").click().run()
        assert not any(b.key == f"approve_confirm_{idx}" for b in at.button)
        assert any(b.key == f"approve_{idx}" for b in at.button)

    def test_approve_and_apply_writes_value_snapshot_and_record(self, app_sandbox):
        """The direct write path of the console (a change set without governed fields): confirm in the dialog ->
        value in the JSON source, a snapshot of the state before, a scenario_applied record naming both and the run."""
        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        old = next(p for p in json.loads(original)["Projects"] if p["project_number"] == "PRJ-DEMO-1001")["status"]
        change = {"project_id": "PRJ-DEMO-1001", "table": "Projects", "field": "status", "old": old, "new": "Green"}
        try:
            at = _app("console")
            at.session_state["messages"] = [
                {"role": "user", "content": "Set the status of the platform to green"},
                {"role": "assistant", "content": "**Recommendation:** GO", "meta": {
                    "request": "Set the status of the platform to green", "mode": "offline", "proposed_changes": [change],
                    "verdict": {"label": "GO", "decision": "GO"}, "scenario_state": {"changes": [change]},
                    "run_id": "run-4711"}}]
            at.run()
            at.button(key="approve_1").click().run()
            at.button(key="approve_confirm_1").click().run()
            assert not at.exception, at.exception
            written = next(p for p in json.loads(source.read_text(encoding="utf-8"))["Projects"]
                           if p["project_number"] == "PRJ-DEMO-1001")
            assert written["status"] == "Green"
            record = [r for r in logs.read_jsonl(logs.AUDIT_LOG)[0] if r.get("event") == "scenario_applied"][-1]
            assert record["applied"] == 1 and record["run_id"] == "run-4711"
            assert any(s["id"] == record["snapshot_id"] for s in snapshots.list_snapshots())
            assert at.session_state["messages"][1]["meta"].get("applied") is True
        finally:
            source.write_text(original, encoding="utf-8")

    def test_report_viewer_shows_verdict_metrics_and_tabs(self, app_sandbox):
        at = _app("console")
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.selectbox(key="sb_project").set_value("PRJ-DEMO-1002").run()
        at.button(key="sb_run").click().run()
        assert any(m.label in ("Budget Δ", "FTE Δ", "ROI Δ") for m in at.metric)
        labels = [t.label for t in at.tabs]
        for expected in ("Summary", "Decision paper", "Changes", "Impact"):
            assert expected in labels
        assert not any(m.value == "–" for m in at.metric)  # no empty key-figure cards
        # a budget cut moves several fields, but it is one decision
        assert at.metric[[m.label for m in at.metric].index("Changes")].value == "1"
        assert any("Recommendation:" in x.value for x in [*at.success, *at.warning, *at.error])  # verdict callout
        # data quality: only findings of the projects the request names or changes, never the portfolio-wide list
        from app.ui.report import requested_projects

        report = at.session_state["messages"][-1]
        focus = set(requested_projects(report["meta"]))
        own = [f for f in report["meta"]["dq_findings"] if f["Project"] in focus]
        assert len(report["meta"]["dq_findings"]) > len(own)  # the portfolio has more findings than this project
        dq_tab = [label for label in labels if label.startswith("Data quality")]
        assert dq_tab == ([f"Data quality ({len(own)})"] if own else [])
        assert any("-badge[" in m and "GO" in m for m in _markdown(at))  # verdict as status badge


def test_report_findings_cover_only_the_report_projects():
    from app.ui.report import report_findings

    rows = [{"Severity": "ERROR", "Project": "P-1", "Rule": "r", "Field": "f", "Finding": "x"},
            {"Severity": "WARNING", "Project": "P-2", "Rule": "r", "Field": "f", "Finding": "y"}]
    assert report_findings({"dq_findings": rows}, ["P-2"])["Project"].tolist() == ["P-2"]
    # older reports: read from the stored Data Steward table
    md = "**Data Steward:** ...\n\n| Severity | Project | Rule | Field | Finding |\n|---|---|---|---|---|\n" \
         "| ERROR | P-1 | r | f | x |\n| WARNING | P-2 | r | f | y |"
    assert report_findings({"steward_md": md}, ["P-1"])["Finding"].tolist() == ["x"]
    assert report_findings({"steward_md": md}, []).empty


class TestSnapshotsUI:
    def test_admin_restores_snapshot_and_graph_is_rebuilt(self, app_sandbox):
        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        try:
            at = _login(_app(), ROLE_ADMIN)
            at.button(key="create_snapshot").click().run()
            assert any("Snapshot" in s.value and "created" in s.value for s in at.success)
            data = json.loads(original)
            data["Projects"][0]["project_name"] = "Accidentally renamed"
            source.write_text(json.dumps(data), encoding="utf-8")  # an unwanted change after the snapshot
            at.checkbox(key="confirm_restore").check().run()
            at.button(key="restore_snapshot").click().run()
            assert not at.exception, at.exception
            assert any("restored over portfolio.json" in s.value and "Dependency graph rebuilt" in s.value for s in at.success)
            assert source.read_text(encoding="utf-8") == original
            records, _ = logs.read_jsonl(logs.AUDIT_LOG)
            assert any(r.get("event") == "snapshot_restored" for r in records)
        finally:
            source.write_text(original, encoding="utf-8")

    def test_project_edit_creates_snapshot(self, app_sandbox):
        from app.core import snapshots

        before = len(snapshots.list_snapshots())
        at = _detail("PRJ-DEMO-1010", "Yusuf Demir")
        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        try:
            next(t for t in at.text_input if (t.key or "").endswith("_project_sponsor")).input("Snapshot test")
            _save_after_input(at)
            assert not at.exception, at.exception
            latest = snapshots.list_snapshots()
            assert len(latest) == before + 1 and latest[0]["reason"] == "project_edit"
            assert latest[0]["inverse"][0]["field"] == "project_sponsor"
        finally:
            source.write_text(original, encoding="utf-8")


class TestProjectDetailView:
    SELECT_1010 = {"selection": {"rows": [9], "columns": [], "cells": []}}  # PMO view: PRJ-DEMO-1010 is row 10

    def test_pmo_sees_read_only_detail_with_missing_required_field_in_red(self, app_sandbox):
        at = _detail("PRJ-DEMO-1010")
        assert any(s.value.startswith("PRJ-DEMO-1010") for s in at.header)  # read-only detail page
        assert not any("Save changes" in b.label for b in at.button)  # PMO cannot open the edit form
        assert any(":red[" in m.value and "Sponsor" in m.value for m in at.markdown)

    def test_owning_pm_can_edit_and_save_to_json(self, app_sandbox):
        at = _detail("PRJ-DEMO-1010", "Yusuf Demir")  # owns PRJ-DEMO-1004 and PRJ-DEMO-1010
        sponsor = next(t for t in at.text_input if (t.key or "").endswith("_project_sponsor"))
        assert not sponsor.disabled
        sponsor.input("Helena Brandt")
        _save_after_input(at)
        assert not at.exception, at.exception
        assert any("saved and dependency graph updated" in s.value for s in at.success)
        projects = json.loads((app_sandbox / "portfolio.json").read_text(encoding="utf-8"))["Projects"]
        assert next(p for p in projects if p["project_number"] == "PRJ-DEMO-1010")["project_sponsor"] == "Helena Brandt"
        trail = logs.BUSINESS_AUDIT_LOG.read_text(encoding="utf-8")
        assert "User Yusuf Demir changed field project_sponsor from NULL to 'Helena Brandt' on Project ID PRJ-DEMO-1010" in trail

    def test_pm_saves_business_case_directly_without_proposal(self, app_sandbox):  # master data stays free
        from app.core import proposals

        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        try:
            at = _detail("PRJ-DEMO-1002", "Priya Raman")
            next(t for t in at.text_area if (t.key or "").endswith("_business_case")).input("Hotfix business case")
            at.run()
            assert any(b.label == "Save changes" for b in at.button)  # only master data touched
            next(b for b in at.button if b.label == "Save changes").click().run()
            assert not at.exception, at.exception
            project = next(p for p in json.loads(source.read_text(encoding="utf-8"))["Projects"]
                           if p["project_number"] == "PRJ-DEMO-1002")
            assert project["business_case"] == "Hotfix business case"
            assert proposals.list_proposals() == []  # no four-eyes detour for ungoverned master data
        finally:
            source.write_text(original, encoding="utf-8")

    def test_governed_field_is_submitted_with_its_own_button(self, app_sandbox):
        from app.core import proposals

        source = app_sandbox / "portfolio.json"
        original = source.read_text(encoding="utf-8")
        at = _detail("PRJ-DEMO-1002", "Priya Raman")
        assert at.selectbox(key="edit_PRJ-DEMO-1002_0_state").value != "On Hold"
        at.selectbox(key="edit_PRJ-DEMO-1002_0_state").set_value("On Hold")
        at.run()
        buttons = {b.label: b for b in at.button}
        assert not buttons["Submit for approval"].disabled and buttons["Save changes"].disabled  # own button each
        buttons["Submit for approval"].click().run()
        assert not at.exception, at.exception
        pending = proposals.list_proposals(status=proposals.SUBMITTED)
        assert len(pending) == 1 and [c["field"] for c in pending[0]["changes"]] == ["state"]
        assert source.read_text(encoding="utf-8") == original  # nothing written before the PMO approves

    def test_table_selection_opens_detail_page_instead_of_dialog(self, app_sandbox):
        at = AppTest.from_file(str(MAIN), default_timeout=90)
        at.session_state["nav_page"] = "projects"
        at.session_state["project_grid"] = self.SELECT_1010  # simulated row selection
        at.run()
        _sign_in(at, "PMO-ADMIN")
        assert at.session_state["detail_pid"] == "PRJ-DEMO-1010"
        assert not at.get("dialog")  # no pop-up any more
        assert any(s.value.startswith("PRJ-DEMO-1010") for s in at.header)
        assert "Master data" in [t.label for t in at.tabs]
        at.button(key="detail_back").click().run()
        assert "detail_pid" not in at.session_state and "Projects" in [h.value for h in at.header]

    @pytest.mark.parametrize("login, opens", [("PMO-ADMIN", True), ("Priya Raman", False)])
    def test_project_link_opens_only_a_project_in_scope(self, app_sandbox, login, opens):
        """?project=… opens the detail page for the PMO; for a PM outside their scope it is ignored silently."""
        at = AppTest.from_file(str(MAIN), default_timeout=90)
        at.session_state["nav_page"] = "projects"
        at.query_params["project"] = "PRJ-DEMO-1010"
        at.run()
        _sign_in(at, login)
        assert (at.session_state["detail_pid"] == "PRJ-DEMO-1010") if opens else "detail_pid" not in at.session_state
        texts = " ".join(str(e.value) for e in [*at.header, *at.caption, *at.markdown, *at.info])
        assert opens or "PRJ-DEMO-1010" not in texts  # no hint that the project exists

    def test_project_table_filters(self, app_sandbox):
        at = _app("projects")
        at.text_input(key="proj_search").input("General Ledger").run()
        table = next(df.value for df in at.dataframe if "project_number" in df.value.columns)
        assert list(table["project_number"]) == ["PRJ-DEMO-1002"]

    def test_prod_environment_refuses_the_demo_accounts(self, app_sandbox):
        # the PROD banner for a signed-in user is covered with a real user file in tests/test_auth.py
        update_runtime_config(environment="PROD")
        at = _app(login=None)
        assert any("Sign-in is not configured" in e.value for e in at.error)

    def test_admin_console_areas_and_missing_files(self, app_sandbox):
        for f in (logs.AUDIT_LOG, logs.SHADOW_LOG, logs.TEST_REPORT):
            f.unlink(missing_ok=True)
        at = _login(_app(), ROLE_ADMIN)
        labels = [t.label for t in at.tabs]
        for expected in ("System status", "Operations", "Logs", "Developer", "Models, prompt & shadowing",
                         "Integrations (MCP)", "Environment staging", "FinOps", "Snapshots", "Database", "Audit logs",
                         "Incidents", "Shadow logs", "System logs", "System tests"):
            assert expected in labels
        assert "Business rules" not in labels and "Required fields" not in labels  # the PMO Head's Rules page
        infos = [i.value for i in at.info]
        assert infos.count("No entries") == 3  # business audit + audit + shadow
        assert any("No test report yet" in i for i in infos)
        assert any(b.label == "Run system tests now" for b in at.button)

    def test_clearing_audit_log_requires_confirmation_and_archives(self, app_sandbox):
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "scenario_applied", "user_id": "PMO-ADMIN"})
        at = _login(_app(), ROLE_ADMIN)
        assert at.button(key="clear_audit").disabled  # disabled without confirmation

        at.checkbox(key="confirm_clear_audit").check().run()  # state refresh, otherwise the button stays disabled
        assert not at.button(key="clear_audit").disabled
        at.button(key="clear_audit").click().run()
        assert not at.exception, at.exception

        # only the anchor is left: it links the new log to the archive (hash chain)
        [anchor] = [json.loads(line) for line in logs.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
        assert anchor["event"] == "log_archived"
        assert len(list(logs.ARCHIVE_DIR.glob("decision_log_*.jsonl"))) >= 1
        assert any("Audit log cleared" in s.value for s in at.success)
        assert at.session_state["confirm_clear_audit"] is False  # the next clear needs a fresh confirmation

    def test_rebuild_button_builds_the_dependency_graph(self, app_sandbox):
        from app.graph import portfolio_graph

        portfolio_graph.reset()
        at = _login(_app(), ROLE_ADMIN)
        at.button(key="rebuild_graph").click().run()  # .run() after click
        assert not at.exception, at.exception
        assert any("Dependency graph built from JSON" in s.value for s in at.success)
        assert portfolio_graph.current_metadata()["counts"]["Project"] == 11

    def test_portfolio_shows_no_graph_warning_when_graph_is_ready(self, app_sandbox):
        at = _app()
        assert not any("dependency graph could not be built" in w.value for w in at.warning)

    def test_portfolio_warns_when_graph_cannot_be_initialised(self, app_sandbox, monkeypatch):
        def failing_build():
            raise graph_tools.GraphUnavailable("Building the dependency graph failed: source locked")

        monkeypatch.setattr(graph_tools, "get_graph", failing_build)
        at = _app()
        assert any("dependency graph could not be built" in w.value for w in at.warning)

    def test_system_log_tab_shows_security_events(self, app_sandbox):
        at = _login(_app(), ROLE_ADMIN)
        at.button(key="refresh_system").click().run()
        log_text = "\n".join(c.value for c in at.code)
        assert "SECURITY | Logout | user=PMO-ADMIN" in log_text and "SECURITY | Login ok | user=SYSTEM-ADMIN" in log_text
        assert logs.APP_LOG.exists()


# =========================================================================== scenario steps, analysis log, navigation
class TestPackage3UI:
    def _cut(self, at: AppTest, pid: str, pct: float) -> AppTest:
        at.selectbox(key="sb_action").set_value("budget_change").run()
        at.selectbox(key="sb_project").set_value(pid).run()
        at.number_input(key="sb_value_True").set_value(pct).run()
        at.button(key="sb_run").click().run()
        assert not at.exception, at.exception
        return at

    @staticmethod
    def _last_verdict(at: AppTest) -> str:
        last = [m for m in at.session_state["messages"] if m["role"] == "assistant"][-1]
        return last["meta"]["verdict"]["decision"]

    def test_demo_flow_minus_20_then_minus_10_needs_no_toggle(self, app_sandbox):
        """After a NO-GO the next step starts from the original data by default."""
        at = self._cut(_app("console", login="Priya Raman"), "PRJ-DEMO-1002", 20.0)
        assert self._last_verdict(at) == "NO-GO"
        assert at.toggle(key="sb_continue").value is False
        assert any("Last step was NO-GO – starting from the original data." in c.value for c in at.caption)
        at = self._cut(at, "PRJ-DEMO-1002", 10.0)
        assert self._last_verdict(at) == "CONDITIONAL GO"
        assert at.toggle(key="sb_continue").value is False  # building on a step is always an explicit choice

    def test_analysis_runs_are_listed_for_the_admin_without_names(self, app_sandbox):
        """Run id, user, data version, mode, duration, verdict and plan per analysis – no request text, no names."""
        self._cut(_app("console", login="Priya Raman"), "PRJ-DEMO-1002", 10.0)
        runs = [r for r in logs.read_jsonl(logs.AUDIT_LOG)[0] if r.get("event") == "analysis_run"]
        assert runs and runs[-1]["mode"] == "offline" and runs[-1]["verdict"] == "CONDITIONAL GO"
        assert runs[-1]["plan"][0] == {"type": "simulation", "action": "budget_change", "projects": ["PRJ-DEMO-1002"],
                                       "parameters": {"delta": -10.0, "is_perc": True, "cost_type": "total"}}
        assert runs[-1]["user_id"] == "Priya Raman" and len(runs[-1]["run_id"]) == 32 and runs[-1]["data_version"]
        assert "Ledger Services" not in json.dumps(runs[-1]) and "Cut" not in json.dumps(runs[-1])
        admin = _login(_app(), ROLE_ADMIN)
        assert "Analysis runs" in [h.value for h in admin.subheader]
        assert not admin.exception, admin.exception

    def test_data_steward_checks_run_once_per_data_state(self, app_sandbox, monkeypatch):
        """Reruns without a data change reuse the checks; a change of the source runs them again."""
        from app.ui import workspace

        calls = []
        real = workspace.run_all_checks
        monkeypatch.setattr(workspace, "run_all_checks", lambda ds: calls.append(1) or real(ds))
        workspace.cached_checks.clear()
        at = _app()
        at.run()
        assert len(calls) == 1
        path = app_sandbox / "portfolio.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # new data version
        _app().run()
        assert len(calls) == 2

    def test_project_table_filters_by_status_value(self):
        """The data-quality filter uses the status value, not a display symbol."""
        import pandas as pd

        from app.ui.common import DQ_OK
        from app.ui.projects_ui import filter_projects

        view = pd.DataFrame({"project_number": ["P-1", "P-2", "P-3"], "project_name": ["a", "b", "c"],
                             "data_quality": [DQ_OK, "Warnings", "Blocked"]})
        assert list(filter_projects(view, quality="With findings")["project_number"]) == ["P-2", "P-3"]

    def test_identity_switch_resets_open_report_and_project(self, app_sandbox):
        """A different identity never keeps the previous one's selection open."""
        at = _app("projects", detail_pid="PRJ-DEMO-1002", selected_run=3)
        at = _login(at, "Priya Raman")
        assert "selected_run" not in at.session_state and "detail_pid" not in at.session_state

    def test_go_to_switches_page_from_code(self, app_sandbox):
        """shell.go_to sets the navigation page and reruns (simulate from the project page opens the console)."""
        at = _detail("PRJ-DEMO-1002")
        at.button(key="detail_simulate").click().run()
        assert at.session_state["nav_page"] == "console" and "Steering Console" in [h.value for h in at.header]


class TestExportsAndGraphCache:
    def test_export_menu_offers_three_downloads_each_audited(self, dataset, monkeypatch):
        """SteerCo Pack, Markdown and CSV – every download writes an audit event."""
        from app.ui import report

        buttons = []
        monkeypatch.setattr(report.st, "download_button", lambda label, data, **kw: buttons.append((label, data, kw)))
        message = {"role": "assistant", "content": "**Recommendation:** GO",
                   "meta": {"request": "Cut", "proposed_changes": [{"project_id": "P-1001", "table": "Projects",
                                                                    "field": "budget_cost_eur", "old": 420000,
                                                                    "new": 400000}]}}
        report.export_menu("k", message, {"role": "user", "content": "Cut"}, dataset)
        assert [b[0] for b in buttons] == ["SteerCo Pack (print / PDF)", "This analysis (Markdown)", "Changes (CSV)"]
        assert "Approved budget (€)" in buttons[1][1] and "−€20,000" in buttons[1][1]
        for _label, _data, kw in buttons:
            kw["on_click"](*kw["args"], **kw["kwargs"])
        events = [r["event"] for r in logs.read_jsonl(logs.AUDIT_LOG)[0]]
        assert events == ["steerco_pack_exported", "report_exported", "report_exported"]

    def test_graph_figure_is_cached_per_visible_project_set(self, dataset, monkeypatch):
        """The same report and scope are built once; another RLS scope gets its own entry."""
        from app.ui import report

        built = []
        monkeypatch.setattr(report, "dependency_graph_figure", lambda ds, edges, focus: built.append(1) or len(built))
        st.cache_data.clear()
        edges = '[{"successor":"P-1002","predecessor":"P-1001","type":"Hard","status":"On Track"}]'
        first = report.cached_graph_figure(dataset, edges, ("P-1001",), (("P-1001", "P-1002"), 1, 0.0))
        again = report.cached_graph_figure(dataset, edges, ("P-1001",), (("P-1001", "P-1002"), 1, 0.0))
        other = report.cached_graph_figure(dataset, edges, ("P-1001",), (("P-1001",), 1, 0.0))
        assert first == again and other != first and len(built) == 2
