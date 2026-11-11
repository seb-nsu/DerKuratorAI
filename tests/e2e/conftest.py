"""End-to-end fixtures: one app server per front end (NiceGUI, React) for the whole session, one Chromium for all, a
fresh browser context per signed-in user. Every test runs against both front ends with the same selectors – visible
labels and ARIA roles, which both front ends share.

Run: `pytest -m e2e` (excluded from the default run). Needs Playwright with Chromium and, for React, the build in
frontends/react/dist; without them the tests are skipped. PLAYWRIGHT_CHROMIUM points to a Chromium binary (else
Playwright's own download is used); E2E_ARTIFACTS names a folder for screenshots of failed tests; E2E_REQUIRED=1 (CI)
turns a missing browser or build into a failure instead of a skip."""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.server import PORTFOLIO, REACT_DIST, Server, free_port

try:
    from tests.e2e.ui import DEMO_PASSWORD, sign_in
except ImportError:  # Playwright not installed: the suite is not collected at all (the default run never needs it)
    collect_ignore_glob = ["test_*.py"]

FRONTENDS = ("nicegui", "react")
DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium"
AS_OF = "2026-10-01"  # reference date of the demo portfolio's expected verdicts (scripts/run_evals.EVAL_AS_OF)
DESKTOP = {"width": 1400, "height": 1000}
TIMEOUT_MS = 30_000

_ports: set[int] = set()


def _unavailable(reason: str) -> None:
    if os.getenv("E2E_REQUIRED", "").strip().lower() in {"1", "true", "yes"}:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def playwright_api():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_api):
    configured = os.getenv("PLAYWRIGHT_CHROMIUM", DEFAULT_CHROMIUM)
    path = configured if configured and Path(configured).exists() else None
    try:
        browser = playwright_api.chromium.launch(executable_path=path)
    except Exception as exc:  # noqa: BLE001 – any launch failure means "no browser here"
        _unavailable(f"Chromium not available ({configured or 'Playwright download'}): {str(exc).splitlines()[0]}")
    yield browser
    browser.close()


@pytest.fixture(scope="session", params=FRONTENDS)
def server(request, tmp_path_factory) -> Iterator[Server]:
    frontend = request.param
    if not PORTFOLIO.exists():
        _unavailable("Demo portfolio not available")
    if frontend == "react" and not REACT_DIST.exists():
        _unavailable("React build missing – run `npm ci && npm run build` in frontends/react")
    port = free_port(_ports)
    _ports.add(port)
    instance = Server(frontend, tmp_path_factory.mktemp(f"e2e-{frontend}"), port, AS_OF)
    instance.start()
    yield instance
    instance.stop()
    _ports.discard(port)


@pytest.fixture
def open_page(browser, server, request):
    """open_page(user=None, password=None, viewport=DESKTOP) -> a page in a fresh browser context (light colour
    scheme), signed in as `user` when given."""
    contexts = []

    def factory(user: str | None = None, password: str | None = None, viewport: dict = DESKTOP):
        context = browser.new_context(base_url=server.url, viewport=viewport, color_scheme="light")
        context.set_default_timeout(TIMEOUT_MS)
        contexts.append(context)
        page = context.new_page()
        if user:
            sign_in(page, user, DEMO_PASSWORD if password is None else password)
        return page

    yield factory
    failed = getattr(request.node, "rep_call", None)
    target = os.getenv("E2E_ARTIFACTS")
    if failed is not None and failed.failed and target:
        Path(target).mkdir(parents=True, exist_ok=True)
        for i, context in enumerate(contexts):
            for j, page in enumerate(context.pages):
                name = re.sub(r"[^\w.-]+", "_", request.node.name)
                page.screenshot(path=str(Path(target) / f"{name}-{i}-{j}.png"), full_page=True)
    for context in contexts:
        context.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item.rep_call = report
    return report

