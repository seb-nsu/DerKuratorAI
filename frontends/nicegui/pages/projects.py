"""Projects page: the filterable project table of the caller's scope and, under /projects?project=<id>, the project
detail page with its tabs (projects_detail). A link to a project outside the scope shows the table, as the API answers
such a project like a missing one."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api, theme
from frontends.nicegui.components import card, page_header, show_flash
from frontends.nicegui.pages.projects_detail import render_detail
from frontends.nicegui.pages.projects_fmt import PAGE_CSS, callout, caption, cell, eur
from frontends.nicegui.shell import page_title

DQ_OK = "OK"
COLUMNS = [("project_number", "ID"), ("project_name", "Project"), ("project_manager", "PM"), ("status", "RAG"),
           ("execution_type", "Method"), ("percent_complete", "Progress"), ("budget_cost_eur", "Budget"),
           ("actual_cost_eur", "Actual cost"), ("estimate_at_completion_eur", "EAC"), ("planned_roi_pct", "ROI"),
           ("risk_score", "Risk"), ("planned_end_date", "Planned end"), ("data_quality", "Data quality")]


async def render(info: dict, query: dict) -> None:
    ui.add_css(PAGE_CSS)
    show_flash()
    pid = query.get("project")
    if pid:
        try:
            detail = await api.get(f"/projects/{pid}")
        except api.ApiError as exc:
            if exc.status != 404:
                raise
            detail = None
        if detail is not None:
            await render_detail(pid, detail, query.get("tab"))
            return
        callout("Project not found.", "neutral", "info")
    await _table(info)


def _row(p: dict) -> dict:
    progress = p.get("percent_complete")
    roi = p.get("planned_roi_pct")
    return {"_key": p["project_number"], "project_number": p["project_number"],
            "project_name": cell(p.get("project_name")), "project_manager": cell(p.get("project_manager")),
            "status": cell(p.get("status")), "rag_tone": theme.tone("rag", p.get("status")),
            "execution_type": cell(p.get("execution_type")),
            "percent_complete": f"{float(progress):.0f} %" if progress is not None else "–",
            "progress_value": float(progress or 0) / 100,
            "budget_cost_eur": eur(p.get("budget_cost_eur")), "actual_cost_eur": eur(p.get("actual_cost_eur")),
            "estimate_at_completion_eur": eur(p.get("estimate_at_completion_eur")),
            "planned_roi_pct": f"{float(roi):.1f} %" if roi is not None else "–",
            "risk_score": cell(p.get("risk_score")), "planned_end_date": cell(p.get("planned_end_date"))[:10],
            "data_quality": cell(p.get("data_quality"))}


async def _table(info: dict) -> None:
    answer = await api.get("/projects")
    projects = answer["projects"]
    page_header(page_title(info, "projects", "Projects"),
                f"{answer['total']} projects within your permissions · open a project to see its detail page",
                link=("data_quality", "fact_check", "Data quality"))
    rows = [_row(p) for p in projects]
    managers = sorted({str(p.get("project_manager")) for p in projects if p.get("project_manager")})
    with card():
        with ui.element("div").classes("dk-filters"):
            search = ui.input("Search", placeholder="Project ID or name").props("outlined dense clearable") \
                .classes("w-full")
            with search.add_slot("prepend"):
                ui.icon("search")
            rag = ui.select(["Green", "Yellow", "Red"], label="RAG", multiple=True, value=[]) \
                .props('outlined dense use-chips placeholder="All"').classes("w-full")
            pm = ui.select(managers, label="Project manager", multiple=True, value=[]) \
                .props('outlined dense use-chips placeholder="All"').classes("w-full")
            quality = ui.toggle(["All", "With findings"], value="All") \
                .props('no-caps unelevated toggle-color=primary aria-label="Data quality"')
        count = ui.label("").classes("dk-muted text-caption q-mt-sm")
        cols = [{"name": f, "label": label, "field": f, "align": "left", "sortable": True} for f, label in COLUMNS]
        grid = ui.table(columns=cols, rows=rows, row_key="_key") \
            .props("flat bordered dense :rows-per-page-options=[0] hide-pagination hide-bottom").classes("w-full dk-table q-mt-xs")
        grid.add_slot("body-cell-project_number", '''
            <q-td :props="props"><a :href="'/projects?project=' + encodeURIComponent(props.value)"
               style="color: var(--dk-accent)">{{ props.value }}</a></q-td>''')
        grid.add_slot("body-cell-status", '''
            <q-td :props="props"><span :class="'dk-badge dk-tone-' + props.row.rag_tone">{{ props.value }}</span></q-td>''')
        grid.add_slot("body-cell-percent_complete", '''
            <q-td :props="props"><div class="row items-center no-wrap" style="gap:6px; min-width: 110px">
              <q-linear-progress :value="props.row.progress_value" color="primary" track-color="grey-4"
                 style="width:60px" aria-hidden="true" /><span>{{ props.value }}</span></div></q-td>''')
        grid.on("rowClick", lambda e: ui.navigate.to(f"/projects?project={e.args[1]['_key']}"))
        grid.classes("cursor-pointer")

        def apply() -> None:
            needle = (search.value or "").strip().lower()
            shown = [r for r in rows
                     if (not needle or needle in r["project_number"].lower() or needle in r["project_name"].lower())
                     and (not rag.value or r["status"] in rag.value)
                     and (not pm.value or r["project_manager"] in pm.value)
                     and (quality.value != "With findings" or r["data_quality"] != DQ_OK)]
            grid.rows = shown
            grid.update()
            count.set_text(f"{len(shown)} of {len(rows)} projects · select a row to open the project")

        for control in (search, rag, pm, quality):
            control.on_value_change(apply)
        apply()
    if info["user"].get("role_label") == "Project Manager":
        caption("Tip: select one of your projects to open its detail page and edit its master data.")

