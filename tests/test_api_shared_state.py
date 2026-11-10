"""Several API workers on one shared data volume (STATE_STORE=file): a run started on one worker is answered by the
other, a sign-out on one is honoured by the other, the per-user calculation limit counts across both, and a write of
one worker reaches the other's data cache at once. Two real server processes, plus the store itself."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.api import deps, limits, runs, state_store, tokens
from app.core.config import PROJECT_ROOT, get_settings
from scripts import run_evals
from tests.test_api import DEMO_PASSWORD, ERP, PM, PMO

KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY")


class TestFileStore:
    def test_two_store_instances_on_one_folder_see_each_other(self, tmp_path):
        a, b = state_store.FileStore(tmp_path), state_store.FileStore(tmp_path)
        a.put("runs", "r1", {"user_id": PM, "n": 1}, time.time() + 60)
        assert b.get("runs", "r1") == {"user_id": PM, "n": 1}
        assert b.get("runs", "r1", now=time.time() + 61) is None  # expired
        b.delete("runs", "r1")
        assert a.get("runs", "r1") is None

    def test_update_is_atomic_and_a_refused_change_writes_nothing(self, tmp_path):
        a, b = state_store.FileStore(tmp_path), state_store.FileStore(tmp_path)
        for _ in range(3):
            a.update("limits", PM, lambda v: ((v or 0) + 1, time.time() + 60))
        assert b.get("limits", PM) == 3

        def refuse(_value):
            raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            b.update("limits", PM, refuse)
        assert a.get("limits", PM) == 3
        b.update("limits", PM, lambda v: (None, 0))  # None removes the entry
        assert a.get("limits", PM) is None

    def test_runs_are_capped_and_clear_keeps_other_spaces(self, tmp_path, monkeypatch):
        monkeypatch.setitem(state_store.CAPS, "runs", 2)
        store = state_store.FileStore(tmp_path)
        now = time.time()
        for i in range(3):
            store.put("runs", f"r{i}", i, now + 60 + i)
        store.put("revoked", "sid", True, now + 60)
        assert [store.get("runs", f"r{i}") for i in range(3)] == [None, 1, 2]  # the first to expire went
        store.clear("runs")
        assert store.get("runs", "r2") is None and store.get("revoked", "sid") is True

    def test_api_state_goes_through_the_selected_store(self, monkeypatch):
        monkeypatch.setenv("STATE_STORE", "file")
        runs.put("run-1", {"user_id": PM})
        runs.set_latest(PM, "run-1")
        tokens.revoke("sid-1", 60)
        files = list((state_store.store().root).rglob("*.json"))
        assert len(files) == 3
        assert runs.get("run-1", PM) == {"user_id": PM, "stored_at": pytest.approx(time.time(), abs=60)}
        assert runs.get("run-1", PMO) is None and runs.is_latest(PM, "run-1") and tokens._is_revoked("sid-1", time.time())
        monkeypatch.setenv("STATE_STORE", "memory")  # the in-memory store knows nothing of them
        assert runs.get("run-1", PM) is None and not tokens._is_revoked("sid-1", time.time())

    def test_unknown_store_is_refused(self, monkeypatch):
        monkeypatch.setenv("STATE_STORE", "redis")
        with pytest.raises(RuntimeError, match="STATE_STORE must be one of memory, file"):
            state_store.store()

    def test_calculation_limit_in_the_file_store(self, monkeypatch):
        monkeypatch.setenv("STATE_STORE", "file")
        monkeypatch.setenv("API_HEAVY_PER_MINUTE", "2")
        with limits.heavy(PM):
            # another worker sees the running mark (it is in the store, not in this process)
            with pytest.raises(Exception) as running, limits.heavy(PM):
                pass
        assert running.value.detail == limits.RUNNING
        with limits.heavy(PM):
            pass
        with pytest.raises(Exception) as too_many, limits.heavy(PM):
            pass
        assert too_many.value.detail == limits.TOO_MANY


def test_data_cache_notices_a_source_change_of_another_worker(tmp_path, monkeypatch):
    if not run_evals.PORTFOLIO.exists():
        pytest.skip("Demo portfolio not available")
    path = tmp_path / "portfolio.json"
    shutil.copy(run_evals.PORTFOLIO, path)
    monkeypatch.setenv("JSON_PATH", str(path))
    monkeypatch.setenv("DATA_SOURCE", "json")
    get_settings.cache_clear()
    deps.clear_data_cache()
    try:
        before = deps.full_dataset()
        assert deps.full_dataset() is before  # cached within the window
        data = json.loads(path.read_text(encoding="utf-8"))
        project = next(p for p in data["Projects"] if p["project_number"] == ERP)
        project["budget_cost_eur"] = 123_456
        path.write_text(json.dumps(data), encoding="utf-8")  # written by another worker: no clear_data_cache here
        after = deps.full_dataset()
        assert after is not before
        assert after.projects.set_index("project_number").loc[ERP, "budget_cost_eur"] == 123_456
    finally:
        get_settings.cache_clear()
        deps.clear_data_cache()


# --------------------------------------------------------------------------- two server processes
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _call(base: str, method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base}/api/v1{path}", data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=120) as answer:  # noqa: S310 - local test server
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def two_workers(tmp_path):
    """Two API server processes on one data folder (as two workers, or two replicas on one volume)."""
    if not run_evals.PORTFOLIO.exists():
        pytest.skip("Demo portfolio not available")
    shutil.copy(run_evals.PORTFOLIO, tmp_path / "portfolio.json")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTH_", "API_", "STATE_", "WEB_CONC"))}
    env.update({name: "" for name in KEY_NAMES})
    env.update({"JSON_PATH": str(tmp_path / "portfolio.json"), "DATA_SOURCE": "json", "ENVIRONMENT": "DEV",
                "FORCE_OFFLINE_MODE": "true", "DERKURATOR_WRITE_DIR": str(tmp_path / "write"),
                "DERKURATOR_LOG_DIR": str(tmp_path / "log"), "STATE_STORE": "file",
                "API_TOKEN_SECRET": secrets.token_urlsafe(48), "API_HEAVY_PER_MINUTE": "3",
                "PYTHONUNBUFFERED": "1"})
    env.pop("FRONTEND", None)
    procs, bases = [], []
    for i in range(2):
        port = _free_port()
        with (tmp_path / f"worker{i}.out").open("wb") as out:  # the child keeps its own handle
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=PROJECT_ROOT, env=env, stdout=out, stderr=subprocess.STDOUT))
        bases.append(f"http://127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + 90
        for proc, base in zip(procs, bases, strict=True):
            while True:
                try:
                    if _call(base, "GET", "/health")[0] == 200:
                        break
                except OSError:
                    pass
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("API worker did not start:\n" + "\n".join(
                        Path(p).read_text(errors="replace")[-2000:] for p in tmp_path.glob("worker*.out")))
                time.sleep(0.3)
        yield bases[0], bases[1], tmp_path
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_two_workers_share_runs_sign_outs_limits_and_data(two_workers):
    a, b, folder = two_workers
    status, signed_in = _call(a, "POST", "/auth/token", body={"user_id": PM, "password": DEMO_PASSWORD})
    assert status == 200
    pm = signed_in["access_token"]
    assert _call(b, "GET", "/me", pm)[1]["user_id"] == PM  # one secret: B accepts A's token

    # a run started on A is read, and submitted as the latest step, on B
    request = f"Cut the budget of General Ledger Consolidation ({ERP}) by 10 %"
    status, run = _call(a, "POST", "/steering/runs", pm, {"request": request})
    assert status == 200, run
    status, report = _call(b, "GET", f"/steering/runs/{run['run_id']}/report", pm)
    assert status == 200, report
    status, proposal = _call(b, "POST", "/proposals", pm, {"run_id": run["run_id"], "justification": "Test"})
    assert status == 201, proposal
    assert _call(a, "POST", "/proposals", pm, {"run_id": run["run_id"]})[0] == 404  # one proposal per run, on A too

    # a write approved on A is in B's data at once (no waiting for the 60-s cache)
    status, detail = _call(b, "GET", f"/projects/{ERP}", pm)
    assert status == 200
    budget_before = json.dumps(detail)
    pmo = _call(a, "POST", "/auth/token", body={"user_id": PMO, "password": DEMO_PASSWORD})[1]["access_token"]
    status, decided = _call(a, "POST", f"/proposals/{proposal['id']}/approve", pmo,
                            {"override": True, "comment": "Accepted in the test"})
    assert status == 200 and decided["kind"] == "success", decided
    assert json.dumps(_call(b, "GET", f"/projects/{ERP}", pm)[1]) != budget_before

    # the per-minute limit (3 here) counts the calculations on both workers
    for base in (b, a):
        assert _call(base, "POST", "/steering/runs", pm, {"request": request})[0] == 200
    status, refused = _call(b, "POST", "/steering/runs", pm, {"request": request})
    assert status == 429 and refused["detail"] == limits.TOO_MANY

    # a sign-out on A is honoured by B
    assert _call(a, "POST", "/auth/sign-out", pm)[0] == 200
    status, answer = _call(b, "GET", "/me", pm)
    assert status == 401 and "Signed out" in answer["detail"]
    assert list((folder / "write" / "state" / "api").glob("*/*.json"))  # the shared state lives on the volume
