"""Data quality: the deterministic Data Steward checks of the reader's scope – index, quality bars, the executive
interpretation (loaded after the page, it may take an AI a moment), the findings with their filters and the question
box."""
from __future__ import annotations

from html import escape

from nicegui import ui

from frontends.nicegui import api, theme
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import card, error_box, notify, page_header
from frontends.nicegui.pages.overview import boundary, caption, metric
from frontends.nicegui.pages.overview_format import pct

SEVERITIES = ["All", "Errors", "Warnings"]
COLUMNS = [("severity", "Severity"), ("project", "Project"), ("rule", "Rule"), ("field", "Field"),
           ("finding", "Finding")]


def quality_bar(bar: dict) -> None:
    """Coloured bar with status word and value as text, so colour never carries the meaning alone."""
    tone = theme.tone("rag", bar["status"])
    value, label = bar["value"], escape(bar["label"])
    width = max(0.0, min(100.0, value))
    ui.html(
        f'<div style="margin:2px 0 12px 0">'
        f'<div style="display:flex;justify-content:space-between;gap:8px;font-size:0.9rem"><span>{label}</span>'
        f'<strong style="white-space:nowrap">{value:.0f} % · {escape(bar["word"])}</strong></div>'
        f'<div role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{value:.0f}" '
        f'aria-label="{label}: {value:.0f} %, {escape(bar["word"])}" '
        f'style="background:var(--dk-neutral-bg);border-radius:4px;height:10px;overflow:hidden">'
        f'<div style="width:{width:.1f}%;background:var(--dk-{tone});height:10px;border-radius:4px"></div></div>'
        f'<div class="dk-muted" style="font-size:0.8rem;margin-top:2px">{escape(bar["detail"])}</div></div>'
    ).classes("w-full")


async def interpretation(box) -> None:
    with box:
        box.clear()
        ui.label("Executive interpretation").classes("text-weight-bold")
        try:
            result = await api.get("/data-quality/interpretation")
        except api.ApiError as exc:
            error_box(exc.message)
            return
        ui.markdown(result["text"])
        caption(f"Written by {result['model']} based on deterministic Data Steward findings" if result["ai_generated"]
                else "Rule-based interpretation (no LLM available) based on deterministic Data Steward findings")


def findings(page: dict) -> None:
    summary = page["summary"]
    with card() as box:
        box.classes("w-full")
        ui.markdown(f"**Findings** · {summary['errors']} errors · {summary['warnings']} warnings · blocked for "
                    f"simulation: {', '.join(summary['blocked_projects']) or '–'}")
        if not page["findings_total"]:
            caption(f"{summary['projects_checked']} projects checked – no findings.", "check_circle")
            return
        with ui.element("div").classes("dk-grid-2 w-full q-mb-sm").style("align-items:end"):
            with ui.column().classes("gap-1"):
                ui.label("Severity").classes("text-caption dk-muted")
                severity = ui.toggle(SEVERITIES, value="All").props('no-caps unelevated aria-label="Severity"')
            projects = ui.select(page["projects"], label="Projects", multiple=True, value=[]) \
                .props('outlined dense use-chips clearable placeholder="All projects"').classes("w-full")

        @ui.refreshable
        def table(rows: list[dict], shown: int) -> None:
            with ui.element("div").classes("w-full").style("overflow-x:auto"):
                ui.table(columns=[{"name": k, "label": label, "field": k, "align": "left", "sortable": True,
                                   "style": "white-space: nowrap" if k in ("severity", "project") else ""}
                                  for k, label in COLUMNS],
                         rows=[{**r, "_i": i} for i, r in enumerate(rows)], row_key="_i",
                         pagination={"rowsPerPage": 0}) \
                    .props("flat dense wrap-cells hide-bottom virtual-scroll").classes("w-full") \
                    .style("max-height: 420px; min-width: 760px")
            ui.element("div").classes("q-mt-xs")
            caption(f"{shown} finding(s) shown")

        table(page["findings"], page["findings_shown"])

        async def refilter() -> None:
            try:
                answer = await api.get("/data-quality", severity=severity.value or "All",
                                       project=list(projects.value or []) or None)
            except api.ApiError as exc:
                notify("error", exc.message)
                return
            table.refresh(answer["findings"], answer["findings_shown"])

        severity.on_value_change(refilter)
        projects.on_value_change(refilter)


async def render(info: dict, query: dict) -> None:
    page = await api.get("/data-quality")
    summary = page["summary"]
    page_header("Data quality", f"Deterministic Data Steward · {summary['projects_checked']} projects checked",
                link=("projects", "arrow_back", "Back to the projects"))
    ui.add_css(".dk-dq-top { display: grid; gap: 16px; grid-template-columns: 1fr 2fr; align-items: stretch; } "
               "@media (max-width: 1024px) { .dk-dq-top { grid-template-columns: 1fr; } }")
    with ui.element("div").classes("dk-dq-top w-full"):
        with card():
            blocked = summary["blocked_projects"]
            metric("Data quality index", pct(summary["quality_index"]),
                   f"{len(blocked)} project(s) blocked for simulation" if blocked else "No project blocked",
                   page["index_help"])
            ui.element("div").classes("q-mt-md")
            for bar in page["bars"]:
                quality_bar(bar)
        with card() as box:
            ui.label("Executive interpretation").classes("text-weight-bold")
            with ui.row().classes("items-center gap-2"):
                ui.spinner(size="sm")
                ui.label("AI is interpreting the data risks ...").classes("dk-muted")
    ui.timer(0.05, lambda: interpretation(box), once=True)
    findings(page)
    async with boundary():
        await ask_box("data_quality")
