"""Smoke test of a deployable build (CI job "Smoke test"): the app starts headless and serves its pages.

1. Starts `streamlit run app/main.py` headless on a free port and waits up to 60 s for `/_stcore/health`.
2. Fetches the start page (HTTP 200).
3. Renders the script itself (Streamlit AppTest): the sign-in page, then the Overview after a demo sign-in.
4. Fails if the server output contains a Python traceback.

Run it like the hosted demo: ENVIRONMENT=TEST FORCE_OFFLINE_MODE=true python scripts/smoke_test.py
System log and incidents go to a temporary folder (DERKURATOR_LOG_DIR), so a smoke run leaves data/ unchanged.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"
TIMEOUT_S = 60


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - localhost only
        return response.status, response.read().decode("utf-8", "replace")


def check_server() -> None:
    port = _free_port()
    log = tempfile.TemporaryFile(mode="w+")
    cmd = [sys.executable, "-m", "streamlit", "run", str(MAIN), "--server.headless", "true",
           "--server.port", str(port), "--server.address", "127.0.0.1"]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603 - fixed command
    try:
        deadline = time.time() + TIMEOUT_S
        while True:
            try:
                status, body = _get(f"http://127.0.0.1:{port}/_stcore/health")
                if status == 200 and body.strip() == "ok":
                    break
            except OSError:
                pass
            if proc.poll() is not None or time.time() > deadline:
                raise SystemExit(f"Server did not become healthy within {TIMEOUT_S} s")
            time.sleep(1)
        status, _ = _get(f"http://127.0.0.1:{port}/")
        if status != 200:
            raise SystemExit(f"Start page returned HTTP {status}")
        print(f"server: health ok, start page HTTP {status}")
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        log.seek(0)
        output = log.read()
    if "Traceback (most recent call last)" in output:
        print(output)
        raise SystemExit("Traceback in the server output")


def check_pages() -> None:
    from streamlit.testing.v1 import AppTest

    sys.path.insert(0, str(ROOT))
    from app.core.auth import DEMO_USERS_FILE

    at = AppTest.from_file(str(MAIN), default_timeout=120)
    at.run()
    if at.exception or [t.value for t in at.title] != ["Sign in"]:
        raise SystemExit(f"Sign-in page failed: {at.exception}")
    if os.getenv("ENVIRONMENT", "DEV").upper() in ("DEV", "TEST"):
        password = json.loads(DEMO_USERS_FILE.read_text(encoding="utf-8"))["public_demo_password"]
        at.text_input(key="login_user").input("PMO-ADMIN")
        at.text_input(key="login_password").input(password)
        next(b for b in at.button if b.label == "Sign in").click().run()
        if at.exception or "auth_user" not in at.session_state:
            raise SystemExit(f"Demo sign-in failed: {at.exception}")
        print("pages: sign-in page ok, overview after demo sign-in ok")
    else:
        print("pages: sign-in page ok (no demo sign-in outside DEV/TEST)")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="derkurator-smoke-") as log_dir:
        os.environ["DERKURATOR_LOG_DIR"] = log_dir  # inherited by the server process, read by check_pages
        os.environ["NOTIFY_ENABLED"] = "false"  # a smoke run never sends mails or Teams messages
        check_server()
        check_pages()
    print("smoke test passed")
