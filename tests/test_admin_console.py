"""Admin Console: test run integration (without a real pytest subprocess) and the data-quality findings of a report.
The pages themselves are covered by the API tests (test_api_admin.py) and the browser end-to-end tests (tests/e2e)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.core import logs, system_tests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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


def test_report_findings_cover_only_the_report_projects():
    from app.views.reports import report_findings

    rows = [{"Severity": "ERROR", "Project": "P-1", "Rule": "r", "Field": "f", "Finding": "x"},
            {"Severity": "WARNING", "Project": "P-2", "Rule": "r", "Field": "f", "Finding": "y"}]
    assert report_findings({"dq_findings": rows}, ["P-2"])["Project"].tolist() == ["P-2"]
    # older reports: read from the stored Data Steward table
    md = "**Data Steward:** ...\n\n| Severity | Project | Rule | Field | Finding |\n|---|---|---|---|---|\n" \
         "| ERROR | P-1 | r | f | x |\n| WARNING | P-2 | r | f | y |"
    assert report_findings({"steward_md": md}, ["P-1"])["Finding"].tolist() == ["x"]
    assert report_findings({"steward_md": md}, []).empty
