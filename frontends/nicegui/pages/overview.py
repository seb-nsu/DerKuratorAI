"""Overview: top risks, KPI ribbon, portfolio briefing and questions, risk vs. ROI, overloaded teams and – for the PMO –
the management reserve with the skill heatmap, for a Project Manager their proposals waiting for the PMO. Each section
loads on its own, so a failing one never takes the others down."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import badge, card, empty, error_box, page_header, plotly, section
from frontends.nicegui.pages.overview_format import eur, eur_m, pct, short


@asynccontextmanager
async def boundary():
    """One error box per section instead of a broken page."""
    try:
        yield
    except api.ApiError as exc:
        if exc.status == 401:
            raise
        error_box(exc.message)


def heading3(text: str) -> None:
    ui.label(text).classes("text-subtitle1 text-weight-bold").props('role="heading" aria-level="3"')


def caption(text: str, icon: str | None = None) -> None:
    with ui.row().classes("items-center no-wrap gap-1"):
        if icon:
            ui.icon(icon, size="16px").classes("dk-muted")
        ui.label(text).classes("dk-muted text-caption")


def info(text: str, icon: str = "info") -> None:
    with ui.element("div").classes("dk-callout dk-tone-info w-full").props('role="status"'):
        with ui.row().classes("items-center no-wrap gap-2"):
            ui.icon(icon, size="20px")
            ui.label(text).style("color: var(--dk-text)")


def metric(label: str, value: str, caption_text: str = "", help_text: str = "") -> None:
    with ui.column().classes("gap-0"):
        with ui.row().classes("items-center gap-1"):
            ui.label(label).classes("dk-muted")
            if help_text:
                ui.icon("help_outline", size="16px").classes("dk-muted").props(f'aria-label="{help_text}"') \
                    .tooltip(help_text)
        ui.label(value).classes("dk-kpi-value")
        if caption_text:
            ui.label(caption_text).classes("dk-muted text-caption")


def top_risks(answer: dict) -> None:
    section("Top risks")
    if not answer["available"]:
        info(answer.get("message") or "", "hourglass_top")
        return
    if not answer["risks"]:
        caption("No blocked projects or systemic risks in your scope.", "check_circle")
        return
    with ui.element("div").classes("dk-grid w-full"):
        for risk in answer["risks"]:
            critical = risk["severity"] == "critical"
            with card(), ui.column().classes("gap-3 h-full"):
                with ui.element("div").classes(f"dk-callout dk-tone-{'danger' if critical else 'warning'} w-full"):
                    with ui.row().classes("items-center no-wrap gap-2"):
                        ui.icon("error" if critical else "warning", size="20px") \
                            .props(f'aria-label="{"Critical" if critical else "Warning"}"')
                        ui.label(risk["title"]).classes("text-weight-bold")
                metric(risk["metric_label"], risk["metric_value"])
                ui.label(" · ".join(x for x in (risk.get("name"), risk["detail"]) if x)).classes("dk-muted text-caption")
                if risk["can_open"] and risk.get("project"):
                    ui.space()
                    ui.button("Open project", icon="open_in_new",
                              on_click=lambda p=risk["project"]: ui.navigate.to(f"/projects?project={p}")) \
                        .props("flat dense no-caps color=primary").classes("self-start")


def kpi_ribbon(k: dict) -> None:
    with ui.element("div").classes("dk-grid w-full"):
        if k["show_active"]:
            with card():
                metric("Active projects", str(k["active_projects"]),
                       f"of {k['projects']} projects within your permissions")
        with card():
            metric("Total budget", eur_m(k["total_budget_eur"]), f"EAC {eur_m(k['total_eac_eur'])}")
        with card():
            critical = k["critical_projects"]
            metric("Critical projects", str(critical),
                   f"{pct(k['critical_share_pct'])} of the portfolio" if critical else "No red projects",
                   "Projects with RAG status red within your permissions.")
        with card():
            metric("Max team utilisation", pct(k["max_team_utilisation_pct"], 1),
                   f"Top team: {k['top_team']}" if k.get("top_team") else "No team above the capacity limit")
        if k.get("data_quality_index") is not None:
            with card():
                blocked = k.get("blocked_projects") or 0
                metric("Data quality index", pct(k["data_quality_index"]),
                       f"{blocked} project(s) blocked for simulation" if blocked else "No project blocked",
                       "Share of projects without ERROR findings, i.e. released for quantitative simulation.")
    if k.get("data_quality_index") is not None:
        ui.button("Data quality details", icon="arrow_forward", on_click=lambda: ui.navigate.to("/data_quality")) \
            .props("flat dense no-caps color=primary")


def charts(page: dict) -> None:
    section("Portfolio analysis")
    with ui.element("div").classes("dk-grid-2 w-full"):
        with card():
            heading3("Risk vs. ROI")
            caption("Bubble size = budget · shape and colour = RAG status")
            figure = (page.get("risk_value") or {}).get("figure")
            if figure:
                plotly(figure)
            else:
                info("No projects with risk score, ROI and budget for the risk/value matrix.")
        with card():
            heading3("Top 5 overloaded teams")
            figure = (page.get("overloaded_teams") or {}).get("figure")
            if figure:
                plotly(figure)
            else:
                info(f"No team above the capacity limit ({short(page['kpis']['capacity_limit_pct'])} %) "
                     "in your project scope.")


def open_items(answer: dict) -> None:
    rows = answer["proposals"]
    if not rows:
        caption("No proposal of yours is waiting for the PMO.", "check_circle")
    else:
        columns = [("id", "Proposal"), ("request", "Request"), ("submitted", "Submitted"), ("due", "Due"),
                   ("deadline", "Deadline")]
        ui.table(columns=[{"name": k, "label": label, "field": k, "align": "left"} for k, label in columns],
                 rows=[{"id": p["id"], "request": p.get("request") or "–", "submitted": p["submitted_label"],
                        "due": p.get("due_date") or "–", "deadline": p["deadline_label"]} for p in rows],
                 row_key="id").props("flat wrap-cells").classes("w-full")
    ui.button("Open Approvals", icon="arrow_forward", on_click=lambda: ui.navigate.to("/approvals")) \
        .props("flat dense no-caps color=primary")


def reserve(answer: dict) -> None:
    with ui.element("div").classes("dk-reserve w-full"):
        with card() as box, ui.column().classes("gap-2"):
            box.style("height: auto")
            metric("Management reserve", eur(answer["balance_eur"]),
                   help_text="Configured start (business rules) plus the bookings in Reserve_Ledger.")
            badge(f"floor {eur(answer['floor_eur'])}", "neutral", "vertical_align_bottom")
            if answer["below_floor"]:
                error_box("The reserve is below its floor.")
        with ui.column().classes("gap-1 w-full").style("min-width: 0"):
            if not answer["skills"] or not answer.get("figure"):
                caption("No skill data recorded.")
                return
            with ui.element("div").classes("w-full").style("overflow-x:auto"):  # a phone scrolls inside the box
                with ui.element("div").style("min-width:640px"):
                    plotly(answer["figure"])
            caption(f"Utilisation per skill across all teams; alert above {short(answer['overload_alert_pct'])} %.")


async def _fetch(path: str):
    try:
        return await api.get(path)
    except api.ApiError as exc:
        if exc.status == 401:
            raise
        return exc


def _ok(result, draw) -> None:
    if isinstance(result, api.ApiError):
        error_box(result.message)
    else:
        draw(result)


async def render(info_: dict, query: dict) -> None:
    ui.add_css(".dk-reserve { display: grid; gap: 16px; grid-template-columns: minmax(220px, 1fr) 3fr; "
               "align-items: start; } @media (max-width: 1024px) { .dk-reserve { grid-template-columns: 1fr; } }")
    page, risks = await asyncio.gather(_fetch("/overview"), _fetch("/overview/top-risks"))
    if isinstance(page, api.ApiError):
        page_header("Overview")
        error_box(page.message)
        return
    page_header("Overview", page["scope_line"])
    if page.get("message"):
        empty(page["message"])
        return
    _ok(risks, top_risks)
    _ok(page["kpis"], kpi_ribbon)
    async with boundary():
        await ask_box("overview")
    _ok(page, charts)
    if page["own_view"]:  # the management reserve is the PMO's lever; a PM sees what waits on them instead
        section("Your open items")
        _ok(await _fetch("/overview/open-items"), open_items)
        return
    section("Skills and management reserve")
    _ok(await _fetch("/overview/reserve"), reserve)
    ui.button("Decision insights", icon="insights", on_click=lambda: ui.navigate.to("/insights")) \
        .props("flat dense no-caps color=primary") \
        .tooltip("Status check of the sources, the portfolio with probabilities and the review of past decisions")
