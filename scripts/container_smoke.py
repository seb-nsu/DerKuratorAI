"""Smoke test of a running container (CI job "Container image"): health, sign-in page and one demo sign-in.

    python scripts/container_smoke.py <base url> <streamlit|nicegui|react>

Run the container in TEST (`-e ENVIRONMENT=TEST -e FORCE_OFFLINE_MODE=true`), so the demo accounts are active. NiceGUI
and React sign in through the API of the container (POST /api/v1/auth/token, then GET /api/v1/me). Streamlit has no
HTTP sign-in: this checks its health and start page; the demo sign-in runs inside the container with
`docker exec <container> python scripts/smoke_test.py` (Streamlit AppTest). Standard library only."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEMO_USER = "Priya Raman"
TIMEOUT_S = 90


def _request(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - the container under test
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310
            return answer.status, answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_healthy(url: str) -> None:
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        try:
            status, _ = _request(url)
            if status == 200:
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"{url} did not answer within {TIMEOUT_S} s")
        time.sleep(1)


def main(base: str, frontend: str) -> None:
    base = base.rstrip("/")
    if frontend == "streamlit":
        wait_healthy(f"{base}/_stcore/health")
        status, body = _request(f"{base}/")
        if status != 200 or "<html" not in body.lower():
            raise SystemExit(f"start page: HTTP {status}")
        print(f"streamlit: health ok, start page HTTP {status}")
        return
    wait_healthy(f"{base}/api/v1/health")
    status, body = _request(f"{base}/sign-in")
    if status != 200 or "<html" not in body.lower():
        raise SystemExit(f"sign-in page: HTTP {status}")
    root = Path(__file__).resolve().parents[1]
    password = json.loads((root / "data" / "demo_users.json").read_text(encoding="utf-8"))["public_demo_password"]
    status, body = _request(f"{base}/api/v1/auth/token", {"user_id": DEMO_USER, "password": password})
    if status != 200:
        raise SystemExit(f"demo sign-in: HTTP {status} {body[:200]}")
    status, body = _request(f"{base}/api/v1/me", token=json.loads(body)["access_token"])
    if status != 200 or json.loads(body).get("user_id") != DEMO_USER:
        raise SystemExit(f"/me after the sign-in: HTTP {status} {body[:200]}")
    print(f"{frontend}: health ok, sign-in page HTTP 200, demo sign-in as {DEMO_USER} ok")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ("streamlit", "nicegui", "react"):
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
