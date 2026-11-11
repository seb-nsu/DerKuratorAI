"""Runs the pytest suite from the Admin Console and evaluates the results.

- Starts pytest with the interpreter of the running app (`sys.executable -m pytest`) instead of a `pytest`
  on the PATH, which is not necessarily set when the app is started via `scripts/serve.py` from a virtual environment.
- Besides the HTML report, produces a JUnit XML from which the UI builds a native results table
  (the pytest-html report renders its table via JavaScript).
- Parallel runs are rejected (one run per process).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core import clock, logs
from app.core.config import PROJECT_ROOT

logger = logging.getLogger("derkurator.tests")
DEFAULT_TIMEOUT_S = 600
_run_lock = threading.Lock()

PYTEST_EXIT_CODES = {
    0: "All tests passed",
    1: "At least one test failed",
    2: "Test run interrupted",
    3: "Internal pytest error",
    4: "Invalid pytest invocation",
    5: "No tests collected",
}


class SystemTestsBusy(RuntimeError):
    """A test run is already in progress."""


@dataclass
class SystemTestRun:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    started_at: str
    command: list[str]
    counts: dict[str, int] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    @property
    def status_text(self) -> str:
        return PYTEST_EXIT_CODES.get(self.returncode, f"Test run failed (exit code {self.returncode})")


def junit_path() -> Path:
    return logs.DATA_DIR / "test_results.xml"


def build_command(report: Path, junit: Path) -> list[str]:
    return [
        sys.executable, "-m", "pytest", "tests/",
        f"--html={report}", "--self-contained-html",
        f"--junitxml={junit}", "-v", "-p", "no:cacheprovider",
    ]


def parse_junit(path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Reads per-test-case results from a JUnit XML. Raises FileNotFoundError if it is missing."""
    root = ET.parse(path).getroot()  # noqa: S314 - our own pytest JUnit report, never uploaded data
    cases: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            node = case.find(tag)
            if node is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                raw = (node.get("message") or node.text or "").strip()
                message = raw.splitlines()[0][:300] if raw else ""
                break
        cases.append({
            "module": (case.get("classname") or "").removeprefix("tests."),
            "test": case.get("name", ""),
            "outcome": outcome,
            "duration_s": round(float(case.get("time") or 0), 3),
            "message": message,
        })
    counts = {k: sum(1 for c in cases if c["outcome"] == k) for k in ("passed", "failed", "error", "skipped")}
    return counts, cases


def run_system_tests(timeout: int = DEFAULT_TIMEOUT_S) -> SystemTestRun:
    if not _run_lock.acquire(blocking=False):
        raise SystemTestsBusy("A test run is already in progress – please wait.")
    try:
        report, junit = logs.TEST_REPORT, junit_path()
        report.parent.mkdir(parents=True, exist_ok=True)
        junit.unlink(missing_ok=True)  # do not show stale results if pytest aborts
        cmd = build_command(report, junit)
        # logs, decisions and proposals of the run go to a temporary folder: the live data/ folder stays untouched
        scratch = tempfile.mkdtemp(prefix="derkurator-tests-")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "DERKURATOR_WRITE_DIR": scratch, "DERKURATOR_LOG_DIR": scratch}
        started, t0 = clock.stamp(), time.perf_counter()
        logger.info("System tests started: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",  # noqa: S603
                                  errors="replace", timeout=timeout, env=env)  # fixed pytest command, no user input
            returncode, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = f"Timeout: test run aborted after {timeout} s."
        except OSError as exc:
            returncode, stdout, stderr = -2, "", f"pytest could not be started: {exc}"
        duration = time.perf_counter() - t0
        shutil.rmtree(scratch, ignore_errors=True)

        try:
            counts, cases = parse_junit(junit)
        except (FileNotFoundError, ET.ParseError):
            counts, cases = {}, []
        run = SystemTestRun(returncode, stdout, stderr, duration, started, cmd, counts, cases)
        log = logger.info if run.passed else logger.error
        log("System tests finished: exit=%s, %s, duration %.1fs", returncode,
            ", ".join(f"{k}={v}" for k, v in counts.items()) or "no results", duration)
        return run
    finally:
        _run_lock.release()
