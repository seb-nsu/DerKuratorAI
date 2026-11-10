"""Light/dark switch, the phone layout (390 px: no page scrolls sideways, the navigation folds into a menu) and narrow
desktops (1024-1536 px: the whole navigation fits next to the header buttons)."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.ui import (
    PHONE,
    WORKSPACE_PAGES,
    WORKSPACE_PATHS,
    body_background,
    heading,
    horizontal_overflow,
    nav_entries,
    submit_proposal,
)

pytestmark = pytest.mark.e2e


def _wait_for_background_change(page: Page, before: str) -> str:
    """NiceGUI reloads the page on a theme switch, React re-themes in place: poll until the background changed."""
    for _ in range(50):
        try:
            now = body_background(page)
        except Exception:  # noqa: BLE001 – the page is reloading; try again
            now = before
        if now != before:
            return now
        page.wait_for_timeout(200)
    raise AssertionError(f"The page background stayed {before} after switching the theme")


def test_dark_mode_switch_changes_the_theme_and_keeps_it(open_page):
    page = open_page("Lukas Berger")
    expect(heading(page, "Overview")).to_be_visible()
    light = body_background(page)
    page.get_by_role("button", name="Switch light/dark theme").click()
    dark = _wait_for_background_change(page, light)
    page.reload()
    expect(heading(page, "Overview")).to_be_visible()
    assert body_background(page) == dark  # the choice outlives a reload
    page.get_by_role("button", name="Switch light/dark theme").click()
    assert _wait_for_background_change(page, dark) == light


def test_no_page_scrolls_sideways_on_a_phone(open_page):
    page = open_page(viewport=PHONE)
    page.goto("/sign-in")
    expect(heading(page, "Sign in")).to_be_visible()
    assert horizontal_overflow(page) <= 0, "sign-in"

    page = open_page("Lukas Berger", viewport=PHONE)
    expect(page.get_by_role("button", name="Open navigation")).to_be_visible()
    overflow = {}
    for path in WORKSPACE_PATHS:
        page.goto(f"/{path}")
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        page.wait_for_load_state("networkidle")
        overflow[path] = horizontal_overflow(page)
    assert {p: px for p, px in overflow.items() if px > 0} == {}


def test_console_with_a_submitted_proposal_fits_a_phone(open_page):
    request = "Increase the budget of Hybrid Hosting Consolidation (PRJ-DEMO-1001) by 5 %"
    submit_proposal(open_page("Rowan Ellis"), request)

    page = open_page("Rowan Ellis", viewport=PHONE)
    page.goto("/console")
    expect(page.get_by_text("Waiting for approval").first).to_be_visible()
    page.wait_for_load_state("networkidle")
    assert horizontal_overflow(page) <= 0


def test_navigation_fits_on_narrow_desktops(open_page):
    """1024-1536 px: every page of the navigation is visible next to the header buttons, without a scrolling
    navigation or a page that scrolls sideways (the PMO has the most pages)."""
    page = open_page("PMO-ADMIN")
    theme_button = page.get_by_role("button", name="Switch light/dark theme")
    problems = {}
    for width in (1024, 1100, 1200, 1280, 1536):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(300)  # layout after the resize
        entries = nav_entries(page)
        expect(entries).to_have_count(len(WORKSPACE_PAGES))
        buttons_left = theme_button.bounding_box()["x"]
        for i in range(entries.count()):
            entry = entries.nth(i)
            box = entry.bounding_box()
            # the entry's centre must show the entry itself: not scrolled away, clipped or under a button
            shown = entry.evaluate("(e) => { const r = e.getBoundingClientRect(); "
                                   "const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
                                   " return !!hit && (e === hit || e.contains(hit)); }")
            if not shown or box["x"] < 0 or box["x"] + box["width"] > buttons_left:
                problems.setdefault(width, []).append(entry.inner_text().strip())
        if horizontal_overflow(page) > 0:
            problems.setdefault(width, []).append("page scrolls sideways")
        header = page.get_by_role("banner")
        if header.evaluate("(e) => e.scrollWidth - e.clientWidth") > 0 \
                or buttons_left + theme_button.bounding_box()["width"] > width:
            problems.setdefault(width, []).append("header buttons pushed out")
    assert problems == {}
