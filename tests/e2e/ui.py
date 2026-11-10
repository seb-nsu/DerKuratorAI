"""Steps the end-to-end tests share. Selectors use only what both front ends show alike: visible labels, headings and
ARIA roles (NiceGUI draws the navigation as tabs, React as links in a navigation landmark; both sit in the banner)."""
from __future__ import annotations

import json
import re

from playwright.sync_api import Locator, Page, expect

from tests.e2e.server import ROOT

DEMO_PASSWORD = json.loads((ROOT / "data" / "demo_users.json").read_text(encoding="utf-8"))["public_demo_password"]
PHONE = {"width": 390, "height": 844}
# the navigation per role (app/views/navigation.py); Data quality and the What-if comparison are sub-pages
PMO_PAGES = ["Overview", "Projects", "Steering Console", "Approvals", "Planning", "Rules"]
PM_PAGES = ["Overview", "My projects", "Steering Console", "My proposals", "My demands"]
WORKSPACE_PAGES = PMO_PAGES  # the most pages
WORKSPACE_PATHS = ["overview", "console", "approvals", "projects", "scenarios", "planning", "data_quality"]  # a PM's

expect.set_options(timeout=20_000)  # a page draws after one or two API calls; a what-if waits longer itself


def sign_in(page: Page, user: str, password: str) -> None:
    """Signs in on the sign-in page and waits for the page frame of the landing page."""
    if "/sign-in" not in page.url:
        page.goto("/sign-in")
    page.get_by_label("User", exact=True).fill(user)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_url(lambda url: "/sign-in" not in url)
    page.get_by_role("button", name="Switch light/dark theme").wait_for()


def nav_entries(page: Page) -> Locator:
    banner = page.get_by_role("banner")
    return banner.get_by_role("tab").or_(banner.get_by_role("navigation").get_by_role("link"))


def nav_titles(page: Page) -> list[str]:
    """Accessible names of the top navigation (icons are not part of them), without the open-items count
    ("Approvals (3)" -> "Approvals")."""
    expect(nav_entries(page).first).to_be_visible()
    snapshot = page.get_by_role("banner").aria_snapshot()
    names = re.findall(r'- (?:tab|link) "([^"]+)"', snapshot)
    return [re.sub(r"\s*\(\d+\)$", "", n) for n in names if n != "DerKurator AI home"]


def heading(page: Page, text: str) -> Locator:
    return page.get_by_role("heading", name=text, level=1)


def run_what_if(page: Page, request: str) -> Locator:
    """Steering Console, free text: runs one what-if from the original data and returns the verdict callout."""
    page.get_by_role("button", name="Free text", exact=True).click()
    _start_from_original_data(page)
    box = page.get_by_role("textbox", name="Describe a change or ask a what-if …")
    box.fill(request)
    page.get_by_role("button", name="Send", exact=True).click()
    return report_for(page, request)


def report_for(page: Page, request: str | re.Pattern) -> Locator:
    """The report of `request` once it is shown: waits for its heading, returns the verdict callout."""
    expect(page.get_by_role("heading", name=request, level=2)).to_be_visible(timeout=60_000)
    return page.get_by_role("status").filter(has_text=re.compile(r"\b(GO|NO-GO|CONDITIONAL GO)\b")).first


def _start_from_original_data(page: Page) -> None:
    """A what-if after a GO / CONDITIONAL GO builds on it by default; the tests always start from the data."""
    switch = page.get_by_role("switch", name=re.compile("Builds on the previous step"))
    if switch.count() and switch.is_checked():
        switch.click()
        expect(switch).not_to_be_checked()


def submit_proposal(page: Page, request: str) -> None:
    """Runs `request` (expected GO) and submits it to the PMO inbox."""
    page.goto("/console")
    expect(run_what_if(page, request)).to_contain_text("GO")
    page.get_by_role("button", name="Submit for approval", exact=True).click()
    expect(page.get_by_text(re.compile(r"Proposal \w+ submitted")).first).to_be_visible()


def inbox_row(page: Page, text: str) -> Locator:
    return page.get_by_role("table").first.get_by_role("row").filter(has_text=text)


def select_row(row: Locator) -> None:
    """The selection control of a table row (NiceGUI: checkbox, React: radio)."""
    row.get_by_role("checkbox").or_(row.get_by_role("radio")).first.click()


def horizontal_overflow(page: Page) -> int:
    """How many pixels the page scrolls sideways (0 = none)."""
    return page.evaluate("() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)"
                         " - window.innerWidth")


def body_background(page: Page) -> str:
    return page.evaluate("() => getComputedStyle(document.body).backgroundColor")
