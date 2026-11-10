"""Steering Console verdicts (offline parser and rule engine, demo reference date) and the four-eyes proposal flow:
a Project Manager submits, a second PMO approves after a confirmation or rejects with a comment, and a PMO cannot
decide their own proposal. Each test works on its own user and project, so the tests do not depend on their order."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.ui import heading, inbox_row, report_for, run_what_if, select_row, submit_proposal

pytestmark = pytest.mark.e2e

PMO = "PMO-ADMIN"


def test_console_free_text_and_guided_give_the_rule_based_verdicts(open_page):
    page = open_page("Priya Raman")
    page.goto("/console")
    expect(heading(page, "Steering Console")).to_be_visible()

    verdict = run_what_if(page, "Cut the budget of General Ledger Consolidation (PRJ-DEMO-1002) by 20 %")
    expect(verdict).to_contain_text("Recommendation: NO-GO")
    expect(verdict).to_contain_text("above the NO-GO threshold")

    # guided: Adjust budget · General Ledger Consolidation (the PM's first project) · total budget · cut · 10 %
    page.get_by_role("button", name="Guided", exact=True).click()
    page.get_by_role("radio", name="Cut", exact=True).check()
    page.get_by_role("radio", name="%", exact=True).check()
    page.get_by_role("spinbutton", name="Value").fill("10")
    page.get_by_role("button", name="Simulate", exact=True).click()
    verdict = report_for(page, re.compile(r"^Cut Total budget of PRJ-DEMO-1002 .* by 10 %$"))
    expect(verdict).to_contain_text("Recommendation: CONDITIONAL GO")


def test_pm_proposal_is_approved_by_the_pmo_after_confirmation_and_leaves_the_inbox(open_page):
    request = "Increase the budget of Tariff Invoicing Overhaul (PRJ-DEMO-1007) by 5 %"
    submit_proposal(open_page("Rowan Ellis"), request)

    page = open_page(PMO)
    page.goto("/approvals")
    expect(heading(page, "Approvals")).to_be_visible()
    row = inbox_row(page, request)
    expect(row).to_have_count(1)
    select_row(row)
    page.get_by_role("button", name=re.compile(r"^Approve & apply")).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("heading", name="Confirm: approve & apply")).to_be_visible()
    dialog.get_by_role("button", name="Confirm: approve & apply").click()
    expect(dialog).to_have_count(0)
    expect(inbox_row(page, request)).to_have_count(0)
    page.get_by_role("button", name=re.compile(r"^Decided")).click()
    expect(page.get_by_role("table").first).to_contain_text(request)


def test_rejection_needs_a_comment(open_page):
    request = "Increase the budget of Vendor Interface Exchange (PRJ-DEMO-1009) by 5 %"
    submit_proposal(open_page("Tomasz Wolski"), request)

    page = open_page(PMO)
    page.goto("/approvals")
    select_row(inbox_row(page, request))
    reject = page.get_by_role("button", name="Reject", exact=True)
    expect(reject).to_be_disabled()
    page.get_by_label("Comment (required for a rejection)").fill("Not in this quarter's budget.")
    expect(reject).to_be_enabled()
    reject.click()
    expect(inbox_row(page, request)).to_have_count(0)


def test_pmo_cannot_decide_own_proposal(open_page):
    request = "Increase the budget of Plant Network Hardening (PRJ-DEMO-1011) by 5 %"
    page = open_page(PMO)
    submit_proposal(page, request)

    page.goto("/approvals")
    select_row(inbox_row(page, request))
    expect(page.get_by_text("you cannot approve or reject your own proposal")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Approve & apply"))).to_be_disabled()
    page.get_by_label("Comment (required for a rejection)").fill("Trying to reject my own proposal.")
    expect(page.get_by_role("button", name="Reject", exact=True)).to_be_disabled()
