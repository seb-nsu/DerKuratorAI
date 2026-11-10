"""Decision insights (PMO sub-page of the overview) in NiceGUI and React: opened from the overview, all five sections
drawn from the API, the reference class follows the chosen project, and a Project Manager cannot open it."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.ui import heading

pytestmark = pytest.mark.e2e

PM, PMO = "Priya Raman", "PMO-ADMIN"
SECTIONS = ("Status check of the sources", "Portfolio with probabilities", "Reference class of a project",
            "Review of past decisions", "Transfers to ServiceNow")


def test_pmo_opens_decision_insights_from_the_overview(open_page):
    page = open_page(PMO)
    expect(heading(page, "Overview")).to_be_visible()
    page.get_by_role("link", name="Decision insights").or_(page.get_by_role("button", name="Decision insights")) \
        .first.click()
    expect(heading(page, "Decision insights")).to_be_visible()
    for title in SECTIONS:
        expect(page.get_by_role("heading", name=title, level=2)).to_be_visible()
    expect(page.get_by_text("This page is not available in this front end yet.")).to_have_count(0)
    expect(page.get_by_role("region", name="Status check of the sources")).to_contain_text("PRJ-DEMO-")
    expect(page.get_by_text("Within budget", exact=True)).to_be_visible()
    expect(page.get_by_text("No approved decision yet.")).to_be_visible()  # the demo starts without approvals
    expect(page.get_by_text("No transfer to ServiceNow yet", exact=False)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)


def test_pm_cannot_open_decision_insights(open_page):
    page = open_page(PM)
    page.goto("/insights")
    expect(page.get_by_role("heading", name="Status check of the sources", level=2)).to_have_count(0)
