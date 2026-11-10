"""Report viewer: one decision report on one screen, shared by the Steering Console and the Approvals page.

Layout: header (eyebrow, request, verdict and status badges, the page's actions) → verdict callout → key-figure row
with the change count → tabs Summary · Decision paper · Changes · Impact · Dependencies · Data quality (the last two
only when the report has them). Everything comes from the API, already in the caller's scope:
`GET /steering/runs/{run_id}/report` (an analysis) or `GET /proposals/{id}/report` (a proposal).

    render(report, *, actions=None, eyebrow=None, status=None, title=None, key="")
        report   the ReportView answer of one of the routes above
        actions  callable that draws the page's own action bar into the header (e.g. export, save, submit, decide);
                 None: no actions
        eyebrow  small line above the title; default: report["eyebrow"] (the console's "Decision report · …")
        status   badge next to the verdict: {"label", "tone", "icon"}; default: report["status"] if present
        title    the bold request line; default: report["request"] or report["title"]
        key      distinguishes several viewers on one page
        returns  the report's card, e.g. for ask_about()

    await ask_about(card, run_id=… | proposal_id=…)
                                the report's question box ("Ask about this report", `POST /assist/ask` page "report")
                                at the end of the card render() returned, as in the Streamlit report
    export_menu(base)           the "Export" menu of a report: SteerCo Pack (print / PDF), Markdown, CSV;
                                base is "/steering/runs/{run_id}" or "/proposals/{id}" (+ /export/{kind})
    await download(path)        fetches a file of the API with the user's token and hands it to the browser
    VERDICT_STYLE[key]          (tone, icon) of a verdict key: GO, CONDITIONAL, NO-GO, INCOMPLETE, NONE
"""
from __future__ import annotations

import re
from collections.abc import Callable

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import badge, notify, plotly

VERDICT_STYLE = {"GO": ("success", "check_circle"), "CONDITIONAL": ("warning", "change_history"),
                 "NO-GO": ("danger", "block"), "INCOMPLETE": ("neutral", "help"), "NONE": ("neutral", "remove")}
TONE_ICONS = {"success": "check_circle", "warning": "change_history", "danger": "block", "neutral": "help", "info": "info"}
CALLOUT_TONES = {"GO": "success", "CONDITIONAL": "warning", "NO-GO": "danger", "INCOMPLETE": "info"}
FULL_PAPER = "Full paper"
EXPORTS = (("steerco-pack", "SteerCo Pack (print / PDF)", "print",
            "One-page HTML summary – open it and use the browser's 'Print → Save as PDF'."),
           ("markdown", "This analysis (Markdown)", "description", None),
           ("csv", "Changes (CSV)", "table_view", None))

_CSS = """
.dk-report .q-tab { text-transform: none; min-height: 40px; padding: 0 10px; }
.dk-report .q-tabs { border-bottom: 1px solid var(--dk-border); }
.dk-report .q-tab-panels { background: transparent; }
.dk-report .q-tab-panel { padding: 16px 0 0; }
.dk-table-wrap { overflow-x: auto; width: 100%; border: 1px solid var(--dk-border); border-radius: var(--dk-radius-card);
  background: var(--dk-surface-raised); }
.dk-table-wrap table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 0.88rem; }
.dk-table-wrap th, .dk-table-wrap td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--dk-border);
  vertical-align: top; }
.dk-table-wrap th { color: var(--dk-text-muted); font-weight: 600; white-space: nowrap; }
.dk-report .nicegui-markdown table { display: block; overflow-x: auto; border-collapse: collapse; }
.dk-report .nicegui-markdown th, .dk-report .nicegui-markdown td { border: 1px solid var(--dk-border); padding: 4px 8px; }
.dk-report-head { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 16px; width: 100%; }
@media (max-width: 900px) { .dk-report-head { grid-template-columns: 1fr; } }
/* a long status badge ("Proposal … · Waiting for approval") wraps instead of widening a narrow page */
.dk-run-item .dk-badge, .dk-report .dk-badge { white-space: normal; max-width: 100%; border-radius: 12px; }
.dk-run-item .dk-badge > .q-icon, .dk-report .dk-badge > .q-icon { flex-shrink: 0; }
.dk-run-item .dk-badge > div, .dk-report .dk-badge > div { min-width: 0; overflow-wrap: anywhere; }
"""


def table(rows: list[dict], columns: list[str] | None = None) -> None:
    """A read-only table that scrolls inside its box on a narrow screen (text is escaped by NiceGUI)."""
    columns = columns or (list(rows[0]) if rows else [])
    with ui.element("div").classes("dk-table-wrap"):
        with ui.element("table"):
            with ui.element("thead"), ui.element("tr"):
                for col in columns:
                    with ui.element("th").props('scope="col"'):
                        ui.label(col)
            with ui.element("tbody"):
                for row in rows:
                    with ui.element("tr"):
                        for col in columns:
                            with ui.element("td"):
                                ui.label(str(row.get(col, "")))


async def download(path: str) -> None:
    """Fetches an API file with the user's token and hands it to the browser as a download."""
    headers = {"Authorization": f"Bearer {api.token()}"} if api.token() else {}
    async with api._client() as client:  # raw bytes: the JSON helpers do not fit a file
        response = await client.get(path, headers=headers)
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        notify("error", str(detail or f"Download failed ({response.status_code})."))
        return
    match = re.search(r'filename="([^"]+)"', response.headers.get("content-disposition", ""))
    ui.download(response.content, match.group(1) if match else path.rsplit("/", 1)[-1],
                media_type=response.headers.get("content-type", "application/octet-stream").split(";")[0])


def export_menu(base: str) -> None:
    """The "Export" button with its menu (SteerCo Pack, Markdown, CSV) of one report."""
    with ui.button("Export", icon="download").props("outline no-caps"):
        with ui.menu():
            for kind, label, icon, help_text in EXPORTS:
                item = ui.menu_item(on_click=lambda k=kind: download(f"{base}/export/{k}"))
                with item, ui.row().classes("items-center no-wrap gap-2"):
                    ui.icon(icon, size="20px")
                    ui.label(label)
                if help_text:
                    item.tooltip(help_text)


def verdict_badge(verdict: dict) -> None:
    tone, icon = VERDICT_STYLE.get(verdict.get("key") or "NONE", VERDICT_STYLE["NONE"])
    badge(verdict.get("label") or "No verdict", tone, icon)


def _callout(verdict: dict) -> None:
    tone = CALLOUT_TONES.get(verdict.get("key") or "")
    if not tone:
        return
    icon = VERDICT_STYLE[verdict["key"]][1]
    with ui.element("div").classes(f"dk-callout dk-tone-{tone} w-full").props('role="status"'):
        with ui.row().classes("items-start no-wrap gap-2"):
            ui.icon(icon, size="22px").props('aria-hidden="true"')
            ui.markdown(f"**Recommendation: {verdict['label']}**" + (f" – {verdict['lead']}" if verdict.get("lead")
                                                                       else ""))


def _metrics(report: dict) -> None:
    changes = report["changes"]
    with ui.element("div").classes("dk-grid w-full"):
        for metric in report["metrics"]:
            with ui.column().classes("gap-1"):
                with ui.element("div").classes("dk-card w-full"):
                    ui.label(metric["label"]).classes("dk-muted")
                    ui.label(metric["value"]).classes("dk-kpi-value")
                ui.label(metric["detail"]).classes("dk-muted text-caption")
        with ui.column().classes("gap-1"):
            with ui.element("div").classes("dk-card w-full"):
                with ui.row().classes("items-center gap-1"):
                    ui.label("Changes").classes("dk-muted")
                    ui.icon("help_outline", size="16px").classes("dk-muted") \
                        .props(f'aria-label="{changes["help"]}" role="img" tabindex="0"').tooltip(changes["help"])
                ui.label(str(changes["decisions"])).classes("dk-kpi-value")
            ui.label(changes["caption"]).classes("dk-muted text-caption")


def _summary(summary: dict) -> None:
    for warning in summary["warnings"]:
        with ui.element("div").classes("dk-callout dk-tone-warning w-full q-mb-sm").props('role="note"'):
            with ui.row().classes("items-start no-wrap gap-2"):
                ui.icon("warning", size="20px").props('aria-hidden="true"')
                ui.label(warning).style("color: var(--dk-text)")
    for step in summary["steps"]:
        _caption("link", step)
    if summary["forced"]:
        _caption("warning", "Forced simulation – results for blocked projects are indicative only.")
    with ui.element("div").classes("dk-grid-2 w-full"):
        with ui.column().classes("gap-1"):
            ui.markdown("**Why this verdict** · rule-based, not AI"
                        + (f" · As of {summary['as_of']}" if summary.get("as_of") else ""))
            if summary["reasons"]:
                ui.markdown("\n".join(f"- {r}" for r in summary["reasons"]))
            else:
                ui.label("No rule-based reasons recorded (e.g. an analysis without a verdict).") \
                    .classes("dk-muted text-caption")
            if summary["rules"]:
                with ui.button("Thresholds applied", icon="rule").props("outline no-caps"):
                    with ui.menu().props("max-width=560px"), ui.column().classes("q-pa-md gap-2"):
                        ui.label("Business rules in force when this verdict was computed (stored with the "
                                 "decision).").classes("dk-muted text-caption")
                        table(summary["rules"], ["Rule", "Value"])
        with ui.column().classes("gap-1"):
            ui.markdown("**Conditions & next steps**")
            if summary["conditions"]:
                ui.markdown("\n".join(f"{i}. {c}" for i, c in enumerate(summary["conditions"], start=1)))
            else:
                ui.label("No conditions.").classes("dk-muted text-caption")
            for flag in summary["flags"]:
                _caption("flag", flag)
    if summary["offline"]:
        _caption("rule", "Written by the rule engine (offline mode, no AI agents) – the verdict and figures are the "
                         "same as with AI agents.")


def _caption(icon: str, text: str) -> None:
    with ui.row().classes("items-center no-wrap gap-1 dk-muted text-caption"):
        ui.icon(icon, size="16px").props('aria-hidden="true"')
        ui.label(text)


def _paper(report: dict) -> None:
    sections = report["paper_sections"]
    if not sections:
        ui.markdown(report["paper"] or "_No decision paper stored._")
        return
    titles = [s["title"] for s in sections]
    bodies = {s["title"]: s["body"] for s in sections}
    buttons: dict = {}
    with ui.row().classes("w-full gap-1").props('role="group" aria-label="Section"'):
        for choice in [*titles, FULL_PAPER]:
            buttons[choice] = ui.button(choice, on_click=lambda c=choice: show(c)).props("no-caps dense outline") \
                .classes("q-px-sm")
    box = ui.column().classes("w-full gap-2")

    def show(choice: str) -> None:
        for name, button in buttons.items():
            button.props(f'aria-pressed={"true" if name == choice else "false"} '
                         + ("unelevated color=primary" if name == choice else "outline color=primary"),
                         remove="unelevated outline" if name != choice else "outline")
        box.clear()
        with box:
            if choice == FULL_PAPER:
                ui.markdown(report["paper"])
            else:
                ui.label(choice).classes("text-subtitle1 text-weight-bold").props('role="heading" aria-level="3"')
                ui.markdown(bodies[choice] or "_Empty section._")

    show(titles[0])


def _dependencies(deps: dict) -> None:
    def show(all_levels: bool) -> None:
        level = deps["all"] if all_levels and deps.get("all") else deps["direct"]
        box.clear()
        with box:
            ui.label(deps["caption"]).classes("dk-muted text-caption")
            if level.get("figure"):
                plotly(level["figure"])
            table(level["rows"], ["Project (waits)", "Depends on", "Type", "Status"])

    if deps.get("toggle_label"):
        ui.switch(deps["toggle_label"], on_change=lambda e: show(bool(e.value))) \
            .tooltip("By default only the direct links of this report's projects are drawn.")
    box = ui.column().classes("w-full gap-2")
    show(False)


def render(report: dict, *, actions: Callable[[], None] | None = None, eyebrow: str | None = None,
           status: dict | None = None, title: str | None = None, key: str = "") -> ui.element:
    """Draws one report (see the module header for the parameters) and returns its card."""
    ui.add_css(_CSS)
    status = status if status is not None else report.get("status")
    with ui.element("div").classes("dk-card dk-report w-full") \
            .props(f'aria-label="Decision report" id="report{key}"') as card:
        with ui.element("div").classes("dk-report-head"):
            with ui.column().classes("gap-1"):
                line = eyebrow if eyebrow is not None else report.get("eyebrow")
                if line:
                    ui.label(line).classes("dk-muted text-caption")
                ui.label(title or report.get("request") or report["title"]).classes("text-subtitle1 text-weight-bold") \
                    .props('role="heading" aria-level="2"')
                with ui.row().classes("items-center gap-2"):
                    verdict_badge(report["verdict"])
                    if status:
                        badge(status["label"], status.get("tone", "neutral"), status.get("icon"))
            with ui.column().classes("gap-2 w-full"):
                if actions is not None:
                    actions()
        with ui.column().classes("w-full gap-3 q-mt-md"):
            _callout(report["verdict"])
            _metrics(report)
            tabs = report["tabs"]
            with ui.tabs().props("dense no-caps align=left inline-label outside-arrows mobile-arrows") \
                    .classes("w-full") as bar:
                for tab in tabs:
                    ui.tab(tab["id"], label=tab["label"])
            with ui.tab_panels(bar, value=tabs[0]["id"]).classes("w-full"):
                with ui.tab_panel("summary"):
                    _summary(report["summary"])
                with ui.tab_panel("paper"):
                    _paper(report)
                with ui.tab_panel("changes"):
                    if report["change_table"]:
                        table(report["change_table"])
                    else:
                        ui.label("No data changes proposed.").classes("dk-muted text-caption")
                with ui.tab_panel("impact"):
                    ui.label(report["impact"]["caption"]).classes("dk-muted text-caption")
                    for figure in report["impact"]["figures"]:
                        plotly(figure)
                if report.get("dependencies"):
                    with ui.tab_panel("dependencies"):
                        _dependencies(report["dependencies"])
                if report.get("data_quality"):
                    with ui.tab_panel("data_quality"):
                        ui.label(report["data_quality"]["caption"]).classes("dk-muted text-caption")
                        table(report["data_quality"]["rows"], ["Severity", "Project", "Rule", "Field", "Finding"])
    return card


async def ask_about(card: ui.element, **ids: str) -> None:
    """The question box of a report (`run_id` or `proposal_id`) at the end of its card."""
    with card, ui.element("div").classes("w-full q-mt-md"):
        await ask_box("report", title="Ask about this report", **ids)
