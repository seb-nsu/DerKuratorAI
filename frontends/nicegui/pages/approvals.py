"""Approvals page: the PMO inbox (pick a proposal, then decide it – approve after a confirmation with the change set, a
NO-GO only with an explicit override and a reason, reject with a comment), the decided proposals with the revert of an
approval, the caller's own proposals (withdraw) and the delegation of the approval right. Who is no approver sees only
their own proposals. Four-eyes principle, scope and deadlines are enforced by the API."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, flash, notify, page_header, section, show_flash
from frontends.nicegui.pages.approvals_report import CHANGE_COLUMNS, proposal_report
from frontends.nicegui.pages.projects_fmt import (
    PAGE_CSS,
    callout,
    caption,
    confirm,
    table,
)
from frontends.nicegui.shell import page_title

ACTIONS = {"apply": ("Approve & apply", "task_alt"), "handover": ("Approve & record hand-over", "assignment_turned_in"),
           "reject": ("Reject", "close"), "withdraw": ("Withdraw", "undo"), "revert": ("Revert", "history")}
HANDOVER_TEXT = "Nothing is written; the resource change is recorded for manual execution."
VIEW_LABELS = {"pending": "Pending", "decided": "Decided", "mine": "Mine", "delegations": "Delegations"}



def _reload(view: str, kind: str, message: str, proposal: str | None = None) -> None:
    """After an action: the page again (counts in the navigation and the tables), the outcome as a notification."""
    flash(kind, message)
    ui.navigate.to(f"/approvals?tab={view}" + (f"&proposal={proposal}" if proposal else ""))


def _projects(p: dict, names: dict[str, str]) -> str:
    return ", ".join(f"{pid} {names[pid]}" if pid in names else pid for pid in p.get("projects") or []) or "–"


# =========================================================================== page
async def render(info: dict, query: dict) -> None:
    ui.add_css(PAGE_CSS)
    show_flash()
    overview = await api.get("/approvals/overview")
    events = overview["deadline_events"]
    parts = ([f"{events['escalated']} proposal(s) escalated past the approval deadline"] if events["escalated"]
             else []) + ([f"{events['expired']} proposal(s) expired without a decision"] if events["expired"] else [])
    if parts:
        notify("info", " · ".join(parts))
    if not overview["approver"]:
        page_header(page_title(info, "approvals", "Approvals"), "Your proposals and the PMO's decisions")
        await _my_proposals((await api.get("/proposals", view="mine"))["proposals"])
        return

    page_header(page_title(info, "approvals", "Approvals"), "Four-eyes principle · a proposal is written only after a second PMO approves it")
    deputy = overview.get("deputy_of")
    if deputy:
        callout(f"You are deputy of **{deputy['delegator']}** until {deputy['end']} – you decide proposals on their "
                "behalf, their own proposals in your own name (never your own).", "info", "badge", markdown=True)
    counts, views = overview["counts"], overview["views"]
    view = query.get("tab") if query.get("tab") in views else "pending"
    ui.toggle({v: f"{VIEW_LABELS[v]} ({counts[v]})" if v in counts else VIEW_LABELS[v] for v in views}, value=view,
              on_change=lambda e: ui.navigate.to(f"/approvals?tab={e.value}")) \
        .props('no-caps unelevated toggle-color=primary aria-label="View"').classes("dk-toggle")
    if view == "mine":
        await _my_proposals((await api.get("/proposals", view="mine"))["proposals"])
    elif view == "delegations":
        await _delegations()
    elif view == "decided":
        await _decided()
    else:
        await _pending(query.get("proposal"))


# =========================================================================== inbox
async def _pending(linked: str | None) -> None:
    answer = await api.get("/proposals", view="pending")
    pending, names = answer["proposals"], answer["project_names"]
    section(f"Pending approvals ({len(pending)})")
    if not pending:
        caption("No proposals waiting for approval – the inbox is empty.", "check_circle")
        return
    rows = [{"_key": p["id"], "Request": p.get("request") or "–", "Projects": _projects(p, names),
             "Verdict": p["verdict_label"], "Budget Δ": p["budget_delta_label"],
             "Requester": p.get("requester") or "–", "Submitted": p["submitted_label"],
             "Due": p.get("due_date") or "–", "Deadline": p["deadline_label"]} for p in pending]
    columns = ["Request", "Projects", "Verdict", "Budget Δ", "Requester", "Submitted", "Due", "Deadline"]

    @ui.refreshable
    async def detail(pid: str | None) -> None:
        if not pid:
            callout("Select a proposal in the table to review and decide it.", "info", "touch_app")
            return
        proposal = await api.get(f"/proposals/{pid}")
        level = int(proposal.get("escalation_level") or 0)
        status = (f"Escalated{' twice' if level > 1 else ''} – overdue", "danger", "schedule") if level \
            else ("Waiting for your decision", "info", "pending_actions")
        await proposal_report(proposal, status)
        _decision_bar(proposal)

    async def picked(row: dict | None) -> None:
        await detail.refresh(row["_key"] if row else None)

    t = table([(c, c) for c in columns], rows, selection=True, on_select=picked)
    # a link to one proposal (?proposal=… from a notification or another page) opens it directly
    start = linked if linked and any(p["id"] == linked for p in pending) else None
    if start:
        t.selected = [r for r in rows if r["_key"] == start]
    await detail(start)


def _decision_bar(proposal: dict) -> None:
    pid, hints = proposal["id"], proposal["decision"]
    own, blocked = hints["own"], not proposal["can_decide"]
    label, icon = ACTIONS["handover" if hints["manual"] else "apply"]
    with card():
        ui.label(f"Decision on proposal {pid}").classes("text-weight-bold").props('role="heading" aria-level="3"')
        with ui.row().classes("w-full items-end gap-2"):
            comment = ui.input("Comment (required for a rejection)").props("outlined dense") \
                .classes("col-grow").style("min-width: 220px")
            reject = ui.button(ACTIONS["reject"][0], icon=ACTIONS["reject"][1],
                               on_click=lambda: _reject(pid, comment.value or "")).props("outline")
            approve = ui.button(f"{label} …", icon=icon, on_click=lambda: _confirm_approve(proposal, comment.value or "")) \
                .props("unelevated color=primary")
        if own:
            reject.tooltip("Four-eyes principle: another PMO decides on your own proposal.")
            approve.tooltip("Four-eyes principle: you cannot approve your own proposal.")
        else:
            approve.tooltip("Opens a confirmation with the change set.")
        approve.set_enabled(not blocked)

        def sync() -> None:
            reject.set_enabled(not blocked and bool((comment.value or "").strip()))
        comment.on_value_change(sync)
        sync()
        if own:
            callout("Four-eyes principle: you cannot approve or reject your own proposal – a second PMO decides.",
                    "info", "lock")
        elif hints["proxy_own"]:
            caption(f"This is a proposal of {hints['delegator']}, whom you deputise for – you decide it in your own "
                    "name.", "badge")
        if not own and hints["needs_override"]:
            caption(f"Rule-based verdict {proposal.get('verdict')} – approving needs an explicit override with a "
                    "reason.", "gpp_maybe")


async def _reject(pid: str, comment: str) -> None:
    if not comment.strip():
        notify("warning", "Enter a comment first – a rejection needs a reason.")
        return
    try:
        outcome = await api.post(f"/proposals/{pid}/reject", {"comment": comment.strip()})
    except api.ApiError as exc:
        notify("error", exc.message)
        return
    _reload("pending", outcome["kind"], outcome["message"])


def _confirm_approve(proposal: dict, comment: str) -> None:
    """Writing to the data source cannot simply be undone, so approving needs an explicit confirmation with the
    change set (and, against a NO-GO, the override with its reason)."""
    pid, hints = proposal["id"], proposal["decision"]
    manual = hints["manual"]
    label, icon = ACTIONS["handover" if manual else "apply"]
    changes = proposal.get("change_rows") or []
    verdict = proposal.get("verdict")
    with ui.dialog() as dialog, ui.card().classes("dk-dialog gap-3"):
        ui.label(f"Confirm: {label.lower()}").classes("text-h6").props('role="heading" aria-level="2"')
        if manual:
            callout(f"Approve proposal **{pid}** of {proposal.get('requester')}. {HANDOVER_TEXT}", "info",
                    "assignment_turned_in", markdown=True)
        else:
            callout(f"Approve proposal **{pid}** of {proposal.get('requester')} and write **{len(changes)} change(s)** "
                    "to the portfolio data source now? A snapshot is taken first; the System Admin can restore it.",
                    "warning", "warning", markdown=True)
        table([(c, c) for c in CHANGE_COLUMNS], [dict(r) for r in changes])
        override = reason = None
        if hints["needs_override"]:
            callout(f"The rule-based verdict is **{verdict}**. Approving means overriding it – the override and your "
                    "reason are recorded in the audit trail.", "danger", "gpp_maybe", markdown=True)
            override = ui.checkbox(f"Override the {verdict} verdict")
            reason = ui.textarea("Reason for the override (required)").props("outlined").classes("w-full")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("outline")

            async def go() -> None:
                body = {"comment": comment.strip(), "override": False}
                if override is not None:
                    body = {"comment": (reason.value or "").strip(), "override": True}
                dialog.close()
                try:
                    outcome = await api.post(f"/proposals/{pid}/approve", body)
                except api.ApiError as exc:
                    notify("error", exc.message)
                    return
                _reload("pending", outcome["kind"], outcome["message"])
            yes = ui.button(f"Confirm: {label.lower()}", icon=icon, on_click=go).props("unelevated color=primary")
        if override is not None:
            def ready() -> None:
                yes.set_enabled(bool(override.value) and bool((reason.value or "").strip()))
            override.on_value_change(ready)
            reason.on_value_change(ready)
            ready()
    dialog.open()


# =========================================================================== decided
async def _decided() -> None:
    answer = await api.get("/proposals", view="decided")
    decided, names = answer["proposals"], answer["project_names"]
    section("Decided proposals")
    if not decided:
        caption("No decided proposals yet.")
        return
    rows = [{"_key": p["id"], "Request": p.get("request") or "–", "Projects": _projects(p, names),
             "Verdict": p["verdict_label"], "Budget Δ": p["budget_delta_label"],
             "Requester": p.get("requester") or "–", "Submitted": p["submitted_label"],
             "Status": p["status_label"], "Decided by": p.get("decided_by_label") or "–",
             "Comment": p.get("comment") or "–"} for p in decided]
    columns = ["Request", "Projects", "Verdict", "Budget Δ", "Requester", "Submitted", "Status", "Decided by",
               "Comment"]

    @ui.refreshable
    async def revert_area(pid: str | None) -> None:
        if not pid:
            caption("Select an approved proposal to take its approval back.")
            return
        state = (await api.get(f"/proposals/{pid}")).get("revert") or {}
        if state.get("existing"):
            caption(state.get("message") or "", "undo")
            return
        if not state.get("possible"):
            caption(state.get("message") or "")
            return
        label, icon = ACTIONS["revert"]

        async def revert() -> None:
            try:
                result = await api.post(f"/proposals/{pid}/revert")
            except api.ApiError as exc:
                notify("error", exc.message)
                return
            _reload("decided", result["kind"], result["message"])

        ui.button(f"{label} this approval …", icon=icon, on_click=lambda: confirm(
            f"{label} this approval", f"Propose the values before proposal **{pid}** as a new proposal? A second PMO "
            "approves it (four-eyes principle). Fields changed since then block the revert.", "Propose the revert",
            revert, icon)).props("outline").tooltip(
            "Proposes the values before this approval as a new proposal. Fields changed since then block the revert; "
            "the Admin restore stays the emergency path.")

    async def picked(row: dict | None) -> None:
        await revert_area.refresh(row["_key"] if row else None)

    table([(c, c) for c in columns], rows, selection=True, on_select=picked)
    await revert_area(None)


# =========================================================================== mine
async def _my_proposals(mine: list[dict]) -> None:
    section("My proposals")
    if not mine:
        caption("You have not submitted a proposal yet. Proposals are created from a Steering Console report, "
                "governed fields in the project detail, or (PMO) the Planning page and reverts.")
        return
    columns = ["Proposal", "Submitted", "Status", "Due", "Deadline", "Decided by", "Comment", "Request", "Projects"]
    table([(c, c) for c in columns], [{
        "_key": p["id"], "Proposal": p["id"], "Submitted": p["submitted_label"], "Status": p["status_label"],
        "Due": p.get("due_date") or "–", "Deadline": p["deadline_label"], "Decided by": p.get("decided_by") or "–",
        "Comment": p.get("comment") or "–", "Request": p.get("request") or "–",
        "Projects": ", ".join(p.get("projects") or [])} for p in mine])
    open_items = [p for p in mine if p["status"] == "submitted"]
    if not open_items:
        return
    with card():
        with ui.row().classes("w-full items-end gap-2"):
            pick = ui.select({p["id"]: f"{p['id']} · {p.get('request') or '–'}"[:80] for p in open_items},
                             label="Withdraw an open proposal").props('outlined dense clearable') \
                .classes("col-grow").style("min-width: 220px")
            reason = ui.input("Reason (optional)").props("outlined dense").classes("col-grow") \
                .style("min-width: 200px")
            label, icon = ACTIONS["withdraw"]

            async def withdraw(pid: str) -> None:
                try:
                    result = await api.post(f"/proposals/{pid}/withdraw", {"reason": reason.value or ""})
                except api.ApiError as exc:
                    notify("error", exc.message)
                    return
                _reload("mine", result["kind"], result["message"])

            button = ui.button(f"{label} …", icon=icon, on_click=lambda: confirm(
                f"{label} {pick.value}", f"Withdraw proposal **{pick.value}**? The PMO no longer sees it; nothing is "
                "written. Submit it again from the report if needed.", f"Withdraw {pick.value}",
                lambda: withdraw(pick.value), icon)).props("outline")
            pick.props('placeholder="Pick a proposal …"')
            button.bind_enabled_from(pick, "value", backward=bool)
            reason.bind_visibility_from(pick, "value", backward=bool)


# =========================================================================== delegations
async def _delegations() -> None:
    answer = await api.get("/delegations")
    rules = answer["rules"]
    section("Delegation of the approval right")
    ui.label(f"Another PMO decides proposals on your behalf for at most {rules['delegation_max_days']} days (only PMO "
             "accounts can deputise – a deputy sees the whole inbox). The four-eyes principle stays: a deputy never "
             "decides their own proposal; yours they decide in their own name. Open proposals are escalated after "
             f"{rules['approval_sla_days']} days and expire after {rules['approval_expiry_days'] or 'no'} days "
             "(Rules page, set by the PMO Head).").classes("dk-muted text-caption")
    today = answer["today"]
    with card():
        with ui.element("div").classes("dk-grid-2 w-full"):
            candidates = answer["deputy_candidates"]
            deputy = (ui.select(candidates, label="Deputy", value=candidates[0]) if candidates
                      else ui.input("Deputy (user id)")).props("outlined dense")
            with ui.row().classes("items-end no-wrap gap-2"):
                start = ui.input("Period – from", value=today).props(f'outlined dense type=date min={today}') \
                    .classes("col")
                end = ui.input("Period – to", value=_plus_days(today, 6)).props(f'outlined dense type=date min={today}') \
                    .classes("col")
        reason = ui.input("Reason (required)", placeholder="e.g. vacation").props("outlined dense").classes("w-full")

        async def grant() -> None:
            try:
                result = await api.post("/delegations", {"deputy": (deputy.value or "").strip(),
                                                         "start": start.value or today, "end": end.value or None,
                                                         "reason": (reason.value or "").strip()})
            except api.ApiError as exc:
                notify("error", exc.message if exc.status != 422 or "Delegation" in exc.message
                       else f"Delegation not saved: {exc.message}")
                return
            _reload("delegations", result["kind"], result["message"])
        ui.button("Delegate", icon="badge", on_click=grant).props("unelevated color=primary").classes("q-mt-sm")

    items = answer["delegations"]
    if not items:
        caption("No delegations yet.")
        return
    table([(c, c) for c in ("Delegator", "Deputy", "From", "To", "Status", "Reason")], [{
        "_key": d["delegation_id"], "Delegator": d.get("delegator") or "–", "Deputy": d.get("deputy") or "–",
        "From": d.get("start") or "–", "To": d.get("end") or "–", "Status": d["status_label"],
        "Reason": d.get("reason") or "–"} for d in items])
    open_items = [d for d in items if d["revocable"]]
    if not open_items:
        return
    with ui.row().classes("w-full items-end gap-2"):
        pick = ui.select({d["delegation_id"]: f"{d['delegator']} → {d['deputy']} ({d['start']} – {d['end']})"
                          for d in open_items}, label="End a delegation early",
                         value=open_items[0]["delegation_id"]).props("outlined dense").classes("col-grow") \
            .style("min-width: 240px")

        async def revoke() -> None:
            try:
                result = await api.post(f"/delegations/{pick.value}/revoke")
            except api.ApiError as exc:
                notify("error", exc.message)
                return
            _reload("delegations", result["kind"], result["message"])

        def ask() -> None:
            chosen = next(d for d in open_items if d["delegation_id"] == pick.value)
            confirm("End delegation", f"End the delegation {chosen['delegator']} → {chosen['deputy']} now? The deputy "
                    "can no longer decide on your behalf.", "End delegation", revoke, "event_busy")
        ui.button("End delegation …", icon="event_busy", on_click=ask).props("outline")


def _plus_days(day: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()
