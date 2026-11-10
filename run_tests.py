"""Runs the test suite and produces the HTML test report data/test_report.html.

Usage:  python run_tests.py [additional pytest arguments, e.g. -k simulator]
The report is available in the Admin Console (area "Quality").
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "data" / "test_report.html"
JUNIT = ROOT / "data" / "test_results.xml"  # results table in the Admin Console (area "Quality")


def main(extra_args: list[str]) -> int:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pytest", "tests/", f"--html={REPORT}", "--self-contained-html",
           f"--junitxml={JUNIT}", "-v", *extra_args]
    print("→", " ".join(cmd))
    code = subprocess.call(cmd, cwd=ROOT)  # noqa: S603 - fixed pytest command
    print(f"\nTest report: {REPORT}  (exit code {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
