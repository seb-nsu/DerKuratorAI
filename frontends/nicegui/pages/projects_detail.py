"""The project detail page: header with status badges, the buttons into the Steering Console, the key figures and one
tab per question (master data, status history, costs & baselines, benefits, schedule & dependencies, risks, data
quality and – for the owning Project Manager – the edit form)."""
from __future__ import annotations

from urllib.parse import urlencode

from nicegui import ui

from frontends.nicegui import api, theme
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import badge, card, flash, kpi, notify, page_header, plotly
from frontends.nicegui.pages.projects_edit import render_edit_form
from frontends.nicegui.pages.projects_fmt import caption, cell, eur, table

DQ_BADGES = {"blocked": ("Blocked for simulation", "danger", "block"), "warnings": ("Data warnings", "warning", "warning"),
             "ok": ("Data ok", "success", "check")}


def console_url(pid: str, action: str = "budget_change", **extra: str) -> str:
    """The Steering Console with the guided scenario builder prefilled: scenario (a guided action id), project and
    its inputs (e.g. risk)."""
    params = {"action": action, "project": pid, **extra}
    return "/console?" + urlencode(params)


def _bold(text: str) -> None:
    ui.label(text).classes("text-weight-bold q-mt-sm").props('role="heading" aria-level="3"')


def _titled(rows: list[dict], labels: dict[str, str]) -> tuple[list[tuple[str, str]], list[dict]]:
    """Columns of the source with readable titles (snake_case -> "Status date"; explicit labels win)."""
    keys = list(rows[0]) if rows else []
    columns = [(k, labels.get(k) or k.replace("_", " ").capitalize()) for k in keys]
    return columns, [{k: cell(r.get(k)) for k in keys} for r in rows]


async def render_detail(pid: str, d: dict, tab: str | None) -> None:
    project, head = d["project"], d["header"]
    ui.button("All projects", icon="arrow_back", on_click=lambda: ui.navigate.to("/projects")).props("flat no-caps")
    with card():
        with ui.row().classes("w-full items-start gap-4"):
            with ui.column().classes("col-grow gap-2").style("min-width: 260px"):
                page_header(f"{pid} – {project.get('project_name')}")
                with ui.row().classes("items-center gap-2"):
                    badge(str(head.get("rag") or "–"), theme.tone("rag", head.get("rag")))
                    badge(str(head.get("state") or "–"), "neutral")
                    badge(str(head.get("execution_type") or "–"), "neutral")
                    badge(f"Phase: {head.get('phase') or '–'}", "info", "flag")
                    badge(*DQ_BADGES.get(head["data_quality"], DQ_BADGES["ok"]))
            with ui.column().classes("gap-2").style("min-width: 240px"):
                ui.button("Simulate a change for this project", icon="query_stats",
                          on_click=lambda: ui.navigate.to(console_url(pid))).props("outline").classes("w-full")
                ui.button("Check stage gate", icon="flag",
                          on_click=lambda: ui.navigate.to(console_url(pid, "stage_gate"))).props("outline") \
                    .classes("w-full")
        _key_figures(head)
        if d["missing_required"]:
            with ui.row().classes("items-center gap-1 q-mt-sm").style("color: var(--dk-danger)"):
                ui.icon("error", size="18px")
                ui.label("Required fields missing: " + ", ".join(d["missing_required"])).classes("text-weight-bold")

        findings = d["data_quality_findings"]
        names = {t: (f"Data quality ({len(findings)})" if t == "Data quality" else t) for t in d["tabs"]}
        current = tab if tab in names else d["tabs"][0]
        with ui.tabs(value=current).props("dense no-caps align=left outside-arrows mobile-arrows active-color=primary "
                                          "indicator-color=primary").classes("w-full q-mt-md dk-tabs") as tabs:
            for key, label in names.items():
                ui.tab(key, label=label)
        with ui.tab_panels(tabs, value=current).classes("w-full").style("background: transparent"):
            with ui.tab_panel("Master data"):
                _master_data(d)
            with ui.tab_panel("Status history"):
                _status_history(d)
            with ui.tab_panel("Costs & baselines"):
                _costs(pid, d)
            with ui.tab_panel("Benefits"):
                _benefits(pid, d)
            with ui.tab_panel("Schedule & dependencies"):
                _schedule(d)
            with ui.tab_panel("Risks"):
                _risks(pid, d)
            with ui.tab_panel("Data quality"):
                _data_quality(findings)
            if "Edit" in names:
                with ui.tab_panel("Edit"):
                    caption("Master data can be edited by the owning Project Manager; budget changes go through the "
                            "Steering Console; governed fields are submitted as a proposal to the PMO inbox.")
                    await render_edit_form(pid, findings)
        if d["access"] != "edit":
            caption("Read-only – master data is maintained by the owning Project Manager.", "lock")
    await ask_box("project", project_id=pid, title="Ask about this project")


def _key_figures(h: dict) -> None:
    with ui.element("div").classes("dk-grid w-full q-mt-md"):
        kpi("Budget", eur(h.get("budget")))
        pct = h.get("actual_pct_of_budget")
        kpi("Actual", eur(h.get("actual")), f"{pct:.0f} % of budget" if pct is not None else "")
        over = h.get("eac_vs_budget")
        with card():
            ui.label("EAC").classes("dk-muted")
            ui.label(eur(h.get("eac"))).classes("dk-kpi-value")
            if over is not None:
                text = "On budget" if abs(over) < 1 else f"{eur(over, signed=True)} vs budget"
                ui.label(text).classes("text-caption " + ("" if over <= 0 or abs(over) < 1 else "text-weight-bold")) \
                    .style("color: var(--dk-danger)" if over >= 1 else "color: var(--dk-text-muted)")
        kpi("Progress", f"{h.get('progress') or 0:.0f} %")
        with card():
            ui.label("Planned end").classes("dk-muted")
            ui.label(h.get("planned_end") or "–").classes("dk-kpi-value")
            left = h.get("days_to_end")
            if h.get("planned_end") and left is not None:
                ui.label(f"in {left} days" if left >= 0 else f"{-left} days overdue").classes("text-caption") \
                    .style("color: var(--dk-text-muted)" if left >= 0 else "color: var(--dk-danger); font-weight: 600")


def _master_data(d: dict) -> None:
    md = d["master_data"]
    with ui.element("div").classes("dk-grid-2 w-full"):
        with ui.column().classes("gap-1"):
            _bold("Master data")
            table([("Field", "Field"), ("Value", "Value")], [dict(r) for r in md["direct"]])
        with ui.column().classes("gap-1"):
            _bold("Governed fields · changes need PMO approval")
            table([("Field", "Field"), ("Value", "Value")], [dict(r) for r in md["governed"]])
    _bold("Business case")
    if md.get("business_case"):
        ui.label(md["business_case"]).style("white-space: pre-wrap")
    else:
        ui.label("No business case recorded.").classes("text-italic dk-muted")


def _status_history(d: dict) -> None:
    if not d["status_reports"]:
        caption("No status reports recorded.")
        return
    table(*_titled(d["status_reports"], {"issues_decisions": "Issues & decisions"}))


def _costs(pid: str, d: dict) -> None:
    costs = d["costs"]
    if not costs["curve"] or not costs.get("figure"):
        caption("No cost plan recorded.")
    else:
        plotly(costs["figure"])
        if costs.get("caption"):  # the chart in one sentence (text alternative)
            caption(costs["caption"])
        _bold("Baselines")
    if not costs["baselines"]:
        caption("No baseline recorded.")
    else:
        table(*_titled(costs["baselines"], {}))
        days = costs.get("planned_vs_approved_end_days")
        if days is not None:
            caption(f"Planned end vs. approved end: {days:+d} days.")
    ui.button("Re-baseline", icon="restart_alt", on_click=lambda: ui.navigate.to(console_url(pid, "rebaseline"))) \
        .props("outline").classes("q-mt-sm")


def _benefits(pid: str, d: dict) -> None:
    b = d["benefits"]
    result = b["realisation"]
    if not b["tranches"]:
        caption("No planned benefit recorded.")
    else:
        table(*_titled(b["tranches"], {}))
        if result.get("pct") is None:
            caption("No benefit tranche is due yet.")
        else:
            tone = {"red": "danger", "orange": "warning", "green": "success"}.get(b.get("colour") or "", "neutral")
            with ui.row().classes("items-center gap-1 q-mt-sm"):
                ui.label(f"{result['pct']:g} % realised").classes("text-weight-bold") \
                    .style(f"color: var(--dk-{tone})")
                ui.label(f"– {eur(result['actual'])} of {eur(result['planned_due'])} due (corrective plan below "
                         f"{b['min_pct']:g} %).")
    if b["currency"] != "EUR":
        caption(f"Amounts of this project are kept in {b['currency']} and shown in EUR (rate {b.get('fx_rate')}).")
    ui.button("Review benefit realisation", icon="insights",
              on_click=lambda: ui.navigate.to(console_url(pid, "benefit_review"))).props("outline").classes("q-mt-sm")
    if not b["can_report"]:
        return
    with ui.expansion("Report an actual benefit", icon="add_chart").classes("w-full q-mt-sm dk-card") \
            .style("height: auto"):
        with ui.element("div").classes("dk-grid w-full"):
            plan = ui.select({o: o or "not assigned" for o in b["plan_options"]}, label="Tranche",
                             value=b["plan_options"][0]).props("outlined dense")
            amount = ui.number("Actual benefit", value=0, min=0, step=10_000, format="%.0f").props("outlined dense")
            currency = ui.select(b["currencies"], label="Currency", value=b["currencies"][0]).props("outlined dense")
            month = ui.input("Month", value=b.get("default_month") or "") \
                .props("outlined dense type=date stack-label")  # booked to the first of the month
        comment = ui.input("Comment").props("outlined dense").classes("w-full q-mt-sm")

        async def save() -> None:
            value = float(amount.value or 0)
            if value <= 0:
                notify("error", "Please enter the actual benefit (> 0).")
                return
            try:
                result = await api.post(f"/projects/{pid}/benefit-actuals", {
                    "amount": value, "plan": plan.value or "", "currency": currency.value,
                    "month": month.value or None, "comment": comment.value or ""})
            except api.ApiError as exc:
                notify("error", exc.message)
                return
            flash(result["kind"], result["message"])
            ui.navigate.to(f"/projects?{urlencode({'project': pid, 'tab': 'Benefits'})}")
        ui.button("Save actual benefit", icon="save", on_click=save).props("unelevated color=primary") \
            .classes("q-mt-sm")


def _schedule(d: dict) -> None:
    _bold("Deadlines")
    if not d["deadlines"]:
        caption("No dated milestones.")
    else:
        rows = [{**r, "days": f"{r['days']} (overdue)" if r["days"] < 0 else str(r["days"])} for r in d["deadlines"]]
        table(*_titled(rows, {"days": "Days left", "item": "Milestone / project"}))
    _bold("Dependencies")
    labels = {"type": "Dependency type", "required_by": "Required by"}
    for title, key in (("Waits for (incoming)", "incoming"), ("Is needed by (outgoing)", "outgoing")):
        ui.label(title).classes("text-weight-medium q-mt-sm")
        rows = d["dependencies"][key]
        if not rows:
            caption("None.")
        else:
            table(*_titled(rows, labels))


RISK_LABELS = {"risk_number": "Risk ID", "short_description": "Risk", "risk_status": "Status",
               "systemic_risk_cluster": "Risk cluster", "exposure_eur": "Exposure (€)"}


def _risks(pid: str, d: dict) -> None:
    risks = d["risks"]
    if not risks:
        caption("No risks recorded.")
        return
    rows = [{**r, "exposure_eur": eur(r.get("exposure_eur"))} for r in risks]
    table(*_titled(rows, RISK_LABELS))
    caption(f"Total exposure: {eur(d['total_risk_exposure_eur'])} · probability × impact on the risk scale of the "
            "business rules.")
    ids = [str(r["risk_number"]) for r in risks]
    with ui.row().classes("w-full items-end gap-2 q-mt-sm"):
        pick = ui.select(ids, label="Risk", value=ids[0]).props("outlined dense").classes("col-grow") \
            .style("min-width: 200px; max-width: 420px")
        ui.button("Plan response", icon="shield",
                  on_click=lambda: ui.navigate.to(console_url(pid, "risk_response", risk=pick.value))) \
            .props("outline")


def _data_quality(findings: list[dict]) -> None:
    if not findings:
        caption("No Data Steward findings for this project.", "check_circle")
        return
    table([(c, c) for c in ("Severity", "Rule", "Field", "Finding")], [
        {"Severity": f["severity"], "Rule": f["rule_label"], "Field": f.get("field_label") or "–",
         "Finding": f["message"]} for f in findings])
