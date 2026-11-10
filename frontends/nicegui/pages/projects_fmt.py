"""Display helpers of the Projects and Approvals pages: the same cell formats as the Streamlit tables (whole euros,
dates without midnight) and small table and dialog blocks. The Approvals cells (verdict, budget Δ, submitted) come
formatted from the API. Formatting only – every value comes from the API."""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from frontends.nicegui import theme

PAGE_CSS = """
.dk-dialog { width: min(760px, calc(100vw - 32px)); max-width: none !important; background: var(--dk-surface-raised);
  color: var(--dk-text); }
.dk-page > .dk-card, .dk-page > .q-table__container, .dk-page > .dk-callout { width: 100%; min-width: 0; }
.dk-table { max-width: 100%; }
.dk-table th { font-weight: 600; color: var(--dk-text-muted); }
.dk-table td.dk-nowrap, .dk-table th.dk-nowrap { white-space: nowrap; }
.dk-table { background: var(--dk-surface-raised); color: var(--dk-text); }
.dk-callout-text p { margin: 0; color: var(--dk-text); }
.dk-paper { max-width: 100%; overflow-x: auto; }
.dk-paper table { border-collapse: collapse; } .dk-paper td, .dk-paper th { border: 1px solid var(--dk-border);
  padding: 4px 8px; }
.dk-filters { display: grid; gap: 12px; grid-template-columns: 1.6fr 1fr 1.4fr auto; align-items: end; }
.dk-tabs .q-tab { text-transform: none; }
.dk-field-missing { color: var(--dk-danger); font-weight: 600; }
.dk-toggle { flex-wrap: wrap; }
@media (max-width: 900px) { .dk-filters { grid-template-columns: 1fr; } }
"""


def eur(value: Any, signed: bool = False) -> str:
    """"€420,000"; `signed` adds "+" to gains; empty is "–"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "–"
    sign = "+" if signed and number > 0 else "−" if number < 0 else ""
    return f"{sign}€{abs(number):,.0f}"


def cell(value: Any) -> str:
    """A table cell for business readers: empty as "–", a midnight timestamp as its date."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    if text.strip().lower() in ("", "none", "nan", "nat"):
        return "–"
    if len(text) >= 19 and text[10] == "T" and text[11:19] == "00:00:00":
        return text[:10]
    return text


def verdict_badge_tone(label: Any) -> str:
    return theme.tone("verdict", label) if label else "neutral"


# short columns that should never break inside a cell (dates, amounts, states)
NOWRAP = ("Submitted", "Due", "Deadline", "Budget Δ", "Verdict", "Status", "From", "To", "date", "Days left",
          "Required by", "Due date", "Exposure (€)", "Status date", "Created", "End", "Version", "Planned", "Actual",
          "Δ", "Current", "Proposed", "Severity")


def table(columns: list[tuple[str, str]], rows: list[dict], *, row_key: str = "_key", selection: bool = False,
          on_select: Callable[[dict | None], Awaitable[None] | None] | None = None, wrap: bool = True,
          sortable: bool = False, nowrap: tuple[str, ...] = NOWRAP) -> ui.table:
    """A read-only business table; with `selection` one row can be picked (checkbox or a click on the row). Wide
    tables scroll inside their box."""
    cols = [{"name": f, "label": label, "field": f, "align": "left", "sortable": sortable,
             **({"classes": "dk-nowrap", "headerClasses": "dk-nowrap"} if f in nowrap or label in nowrap
                else {"style": "min-width: 160px"})}
            for f, label in columns]
    for i, row in enumerate(rows):
        row.setdefault(row_key, str(i))
    t = ui.table(columns=cols, rows=rows, row_key=row_key, selection="single" if selection else None) \
        .props(f"flat bordered dense {'wrap-cells' if wrap else ''} :rows-per-page-options=[0] hide-pagination "
               "hide-selected-banner hide-bottom") \
        .classes("w-full dk-table")
    if selection and on_select is not None:
        async def picked(row: dict | None) -> None:
            result = on_select(row)
            if result is not None:
                await result

        async def changed(e) -> None:
            await picked(e.selection[0] if e.selection else None)

        async def clicked(e) -> None:
            row = e.args[1] if isinstance(e.args, list) and len(e.args) > 1 else None
            if row is None:
                return
            t.selected = [row]
            t.update()
            await picked(row)

        t.on_select(changed)
        t.on("rowClick", clicked)
        t.classes("cursor-pointer")
    return t


def callout(text: str, tone: str = "info", icon: str | None = None, markdown: bool = False) -> None:
    with ui.element("div").classes(f"dk-callout dk-tone-{tone} w-full"):
        with ui.row().classes("items-start no-wrap gap-2"):
            if icon:
                ui.icon(icon, size="20px")
            if markdown:
                ui.markdown(text).classes("dk-callout-text")
            else:
                ui.label(text).style("color: var(--dk-text)")


def caption(text: str, icon: str | None = None, tone: str | None = None) -> None:
    with ui.row().classes("items-center no-wrap gap-1"):
        if icon:
            ui.icon(icon, size="16px").classes(f"text-{tone}" if tone else "dk-muted")
        label = ui.label(text).classes("text-caption")
        label.classes("dk-muted" if tone is None else "").style(f"color: var(--dk-{tone})" if tone else "")


def confirm(title: str, message: str, confirm_label: str, action: Callable[[], Awaitable[None]],
            icon: str | None = None) -> None:
    """One confirmation for an action that cannot simply be undone: what happens, Cancel / confirm."""
    with ui.dialog() as dialog, ui.card().classes("dk-dialog"):
        ui.label(title).classes("text-h6").props('role="heading" aria-level="2"')
        ui.markdown(message)
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("outline")

            async def go() -> None:
                dialog.close()
                await action()
            ui.button(confirm_label, icon=icon, on_click=go).props("unelevated color=primary")
    dialog.open()
