"""One app server per front end for the end-to-end suite: a subprocess of tests/e2e/serve_frozen.py on a temp copy of
the demo portfolio, with proposals, decisions and logs in a temp folder (the repository's data/ stays unchanged),
offline (no API keys) and with the clock on the demo's reference date. stop() ends exactly the process it started."""
from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
REACT_DIST = ROOT / "frontends" / "react" / "dist" / "index.html"
PORTS = range(8720, 8740)  # the ports this suite may use
KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY")


def free_port(taken: set[int]) -> int:
    for port in PORTS:
        if port in taken:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"No free port in {PORTS.start}-{PORTS.stop - 1}")


@dataclass
class Server:
    frontend: str
    workdir: Path
    port: int
    as_of: str
    admin_password: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def portfolio(self) -> Path:
        return self.workdir / "portfolio.json"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTH_", "API_TOKEN", "NICEGUI_"))}
        env.update({name: "" for name in KEY_NAMES})  # set but empty: .env cannot add a key, no LLM is called
        env["NOTIFY_ENABLED"] = "false"  # a developer .env never sends real mails or Teams messages from a test run
        env.update({
            "FRONTEND": self.frontend, "HOST": "127.0.0.1", "PORT": str(self.port),
            "JSON_PATH": str(self.portfolio), "DATA_SOURCE": "json", "ENVIRONMENT": "DEV",
            "FORCE_OFFLINE_MODE": "true", "DERKURATOR_WRITE_DIR": str(self.workdir / "write"),
            "DERKURATOR_LOG_DIR": str(self.workdir / "log"), "E2E_AS_OF": self.as_of,
            "AUTH_ADMIN_PASSWORD": self.admin_password, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        })
        env.pop("API_BASE_URL", None)
        return env

    def start(self, timeout: float = 90) -> None:
        for sub in ("write", "log"):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        shutil.copy(PORTFOLIO, self.portfolio)
        with (self.workdir / "server.out").open("wb") as out:  # the child keeps its own handle
            self.process = subprocess.Popen(  # own process group: stop() ends it and its children only
                [sys.executable, str(Path(__file__).with_name("serve_frozen.py"))], cwd=ROOT, env=self.env(),
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.frontend} server exited:\n{self.log_tail()}")
            try:
                with urllib.request.urlopen(f"{self.url}/api/v1/health", timeout=2) as answer:  # noqa: S310
                    if answer.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"{self.frontend} server did not answer within {timeout:.0f} s:\n{self.log_tail()}")

    def stop(self) -> None:
        """Ends the server's own process group (the process this object started and its children), nothing else."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    def log_tail(self, lines: int = 40) -> str:
        path = self.workdir / "server.out"
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) \
            if path.exists() else ""
