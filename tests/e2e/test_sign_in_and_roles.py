"""Sign-in, navigation per role, row-level scope and the role-bound pages (Planning for the Board, Admin Console for
the System Admin) – the same steps against NiceGUI and React."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.ui import DEMO_PASSWORD, PM_PAGES, PMO_PAGES, heading, nav_titles

pytestmark = pytest.mark.e2e

PM, PMO, BOARD = "Priya Raman", "PMO-ADMIN", "BOARD-CHAIR"
OWN_PROJECT, OTHER_PROJECT = "PRJ-DEMO-1002", "PRJ-DEMO-1001"  # Priya Raman manages 1002, not 1001


def test_sign_in_page_lists_demo_accounts_and_signs_in_with_one(open_page):
    page = open_page()
    page.goto("/sign-in")
    expect(heading(page, "Sign in")).to_be_visible()
    expect(page.get_by_text("Demo environment – demo accounts")).to_be_visible()
    for account in ("PMO Head · PMO", "Priya Raman · Project Manager", "Portfolio Board · Portfolio Board"):
        expect(page.get_by_role("button", name=account, exact=True)).to_be_visible()
    page.get_by_role("button", name="Priya Raman · Project Manager", exact=True).click()
    expect(heading(page, "Overview")).to_be_visible()
    expect(page.get_by_role("banner")).to_contain_text("Priya Raman · Project Manager")
    expect(page.get_by_text("2 projects visible")).to_be_visible()  # a PM never sees the portfolio's size


def test_wrong_password_is_refused_with_a_message(open_page):
    page = open_page()
    page.goto("/sign-in")
    page.get_by_label("User", exact=True).fill(PM)
    page.get_by_label("Password", exact=True).fill("not-the-password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("Unknown user or wrong password.")
    expect(page).to_have_url(re.compile(r"/sign-in"))


@pytest.mark.parametrize(("user", "pages"), [
    (PM, PM_PAGES), (PMO, PMO_PAGES), (BOARD, ["Planning"])], ids=["pm", "pmo", "board"])
def test_navigation_shows_the_pages_of_the_role(open_page, user, pages):
    page = open_page(user)
    assert nav_titles(page) == pages
    assert "Admin Console" not in nav_titles(page)


def test_pm_sees_only_own_projects_and_a_deep_link_outside_the_scope_is_not_found(open_page):
    page = open_page()
    # a deep link before sign-in returns to the link afterwards
    page.goto(f"/projects?project={OTHER_PROJECT}")
    expect(heading(page, "Sign in")).to_be_visible()
    page.get_by_label("User", exact=True).fill(PM)
    page.get_by_label("Password", exact=True).fill(DEMO_PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/projects\?project={OTHER_PROJECT}$"))
    expect(page.get_by_text("Project not found.")).to_be_visible()

    page.goto("/projects")
    table = page.get_by_role("table").first
    expect(table.get_by_role("row").filter(has_text=OWN_PROJECT)).to_have_count(1)
    expect(table).not_to_contain_text(OTHER_PROJECT)


def test_board_lands_on_planning_and_has_no_other_page(open_page):
    page = open_page(BOARD)
    expect(page).to_have_url(re.compile(r"/planning"))
    expect(heading(page, "Planning")).to_be_visible()
    expect(page.get_by_role("tab", name="Plan approval")).to_be_visible()
    expect(page.get_by_text("Reference date 2026-10-01.")).to_be_visible()  # the server runs on the demo's date
    page.goto("/console")  # a page the role does not have: its first page instead
    expect(page).to_have_url(re.compile(r"/planning"))


def test_admin_console_is_reachable_only_for_the_admin(open_page, server):
    page = open_page(PMO)
    page.goto("/admin")
    expect(heading(page, "Overview")).to_be_visible()
    expect(page.get_by_role("heading", name="System Admin Console")).to_have_count(0)

    admin = open_page("ADMIN", server.admin_password)
    expect(admin).to_have_url(re.compile(r"/admin"))
    expect(heading(admin, "System Admin Console")).to_be_visible()
    expect(admin.get_by_role("tab", name="System status")).to_be_visible()
    assert nav_titles(admin) == ["Admin Console"]



def test_sub_pages_open_from_their_parent_page(open_page):
    page = open_page(PM)
    page.goto("/projects")
    expect(heading(page, "My projects")).to_be_visible()
    page.get_by_role("button", name="Data quality").or_(page.get_by_role("link", name="Data quality")).first.click()
    expect(heading(page, "Data quality")).to_be_visible()
    page.goto("/console")
    page.get_by_role("button", name="Compare saved what-ifs").or_(
        page.get_by_role("link", name="Compare saved what-ifs")).first.click()
    expect(heading(page, "What-if comparison")).to_be_visible()
    assert "What-if comparison" not in nav_titles(page) and "Data quality" not in nav_titles(page)


def test_only_the_pmo_head_changes_the_rules(open_page):
    deputy = open_page("PMO-DEPUTY")
    deputy.goto("/rules")
    expect(heading(deputy, "Rules")).to_be_visible()
    expect(deputy.get_by_text("Read-only – only the PMO Head changes these rules.")).to_be_visible()
    expect(deputy.get_by_role("button", name="Save business rules")).to_have_count(0)

    head = open_page(PMO)
    head.goto("/rules")
    expect(head.get_by_role("button", name="Save business rules")).to_be_visible()
    expect(head.get_by_role("button", name="Save required fields")).to_be_visible()

    pm = open_page(PM)
    pm.goto("/rules")  # not a page of the role: its first page instead
    expect(heading(pm, "Overview")).to_be_visible()
