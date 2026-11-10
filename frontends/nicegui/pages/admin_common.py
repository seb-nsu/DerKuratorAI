"""Building blocks of the Admin Console: lazily loaded sections that redraw after an action, the confirmation dialog
of destructive actions, callouts, tables and log blocks."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import error_box, notify

CSS = """
.adm-pre { font-family: var(--dk-font-mono, ui-monospace, monospace); font-size: 0.8rem; white-space: pre; overflow: auto;
  max-height: 480px; margin: 0; padding: 12px; background: var(--dk-surface); color: var(--dk-text);
  border: 1px solid var(--dk-border); border-radius: var(--dk-radius-control); }
.adm-scroll { max-height: 260px; overflow: auto; border: 1px solid var(--dk-border); border-radius: var(--dk-radius-control);
  padding: 8px 12px; }
.adm-code { font-family: ui-monospace, monospace; font-size: 0.85em; background: var(--dk-surface); padding: 1px 4px;
  border-radius: 4px; overflow-wrap: anywhere; }
.adm-matrix { display: grid; grid-template-columns: minmax(120px, 180px) 1fr 1fr; gap: 8px 12px; align-items: center; }
.adm-tabs .q-tab { text-transform: none; min-height: 40px; padding: 0 12px; }
.adm-tabs .q-tabs__content { flex-wrap: nowrap; }
.dk-page .q-tab-panels { background: transparent; }
.dk-page .q-tab-panel { padding: 16px 0 0; }
.dk-page .q-expansion-item { border: 1px solid var(--dk-border); border-radius: var(--dk-radius-card); }
.dk-page .dk-card { width: 100%; }
.adm-rule { display: flex; flex-direction: column; justify-content: space-between; gap: 4px; }
.adm-table { width: 100%; }
.adm-table th { white-space: nowrap; }
.adm-table td { white-space: nowrap; max-width: 520px; overflow: hidden; text-overflow: ellipsis; }
@media (max-width: 600px) {
  .adm-matrix { grid-template-columns: 1fr; }
  .adm-matrix .adm-matrix-head { display: none; }
}
"""

TONES = {"success": ("success", "check_circle"), "info": ("info", "info"), "warning": ("warning", "warning"),
         "error": ("danger", "error")}
ICON_LABELS = {"success": "Success", "info": "Note", "warning": "Warning", "error": "Error"}


def callout(kind: str, text: str) -> None:
    """success | info | warning | error, with icon and text (colour never alone); `code` and **bold** as in the
    Streamlit texts."""
    tone, icon = TONES.get(kind, TONES["info"])
    with ui.element("div").classes(f"dk-callout dk-tone-{tone} w-full") \
            .props(f'role="{"alert" if kind == "error" else "status"}"'):
        with ui.row().classes("items-start no-wrap gap-2"):
            ui.icon(icon, size="20px").props(f'aria-label="{ICON_LABELS.get(kind, "Note")}" role="img"')
            ui.html(_inline(text)).style("white-space: pre-line")


def heading(text: str, level: int = 2) -> None:
    size = {2: "text-h6", 3: "text-subtitle1"}.get(level, "text-subtitle2")
    ui.label(text).classes(f"{size} text-weight-medium").props(f'role="heading" aria-level="{level}"')


def caption(text: str) -> None:
    """A muted caption; `code` spans are written as `…` in the text."""
    ui.html(_inline(text)).classes("dk-muted text-caption")


def rich(text: str, classes: str = "") -> None:
    ui.html(_inline(text)).classes(classes)


def _inline(text: str) -> str:
    """`code` and **bold** of the Streamlit captions as HTML (everything else escaped)."""
    out, code, bold = [], False, False
    parts = escape(text).split("`")
    for i, part in enumerate(parts):
        if i:
            code = not code
        if code:
            out.append(f'<code class="adm-code">{part}</code>')
            continue
        chunks = part.split("**")
        for j, chunk in enumerate(chunks):
            if j:
                bold = not bold
                out.append("<strong>" if bold else "</strong>")
            out.append(chunk)
    return "".join(out) + ("</strong>" if bold else "")


def pre(text: str) -> None:
    ui.html(f'<pre class="adm-pre" tabindex="0">{escape(text)}</pre>').classes("w-full")


def cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return value


def table(rows: list[dict], columns: list[tuple[str, str]] | None = None, page_size: int = 25,
          front: tuple[str, ...] = ()) -> None:
    """A read-only table; wide tables scroll inside their box. Without `columns` every key is a column (`front` first)."""
    if columns is None:
        keys: list[str] = []
        for row in rows:
            keys += [k for k in row if k not in keys]
        keys = [k for k in front if k in keys] + [k for k in keys if k not in front]
        columns = [(k, k) for k in keys]
    data = [{"_i": i, **{f"c{n}": cell(row.get(key)) for n, (key, _) in enumerate(columns)}} for i, row in enumerate(rows)]
    cols = [{"name": f"c{n}", "label": label, "field": f"c{n}", "align": "left", "sortable": True}
            for n, (_, label) in enumerate(columns)]
    with ui.element("div").classes("w-full").style("overflow-x: auto"):
        ui.table(rows=data, columns=cols, row_key="_i",
                 pagination={"rowsPerPage": page_size} if len(rows) > page_size else None) \
            .props("flat bordered dense wrap-cells=false").classes("adm-table")


async def confirm(label: str, message: str, confirm_label: str | None = None) -> bool:
    """The confirmation of an action that discards data or settings: what is lost, Cancel / confirm."""
    with ui.dialog() as dialog, ui.element("div").classes("dk-card").style("max-width: 520px; height: auto") \
            .props('role="alertdialog" aria-modal="true"'):
        heading(f"Confirm: {label}")
        callout("warning", message)
        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("outline")
            ui.button(confirm_label or label, on_click=lambda: dialog.submit(True)) \
                .props("unelevated color=negative")
    result = await dialog
    dialog.delete()
    return bool(result)


def to_sign_in(page: str = "admin") -> None:
    ui.navigate.to(f"/sign-in?next=%2F{page}")


class Section:
    """A part of the console that loads on first view and redraws after an action. An error shows in the section
    only (a broken section never hides the others)."""

    def __init__(self, build: Callable[[Section], Awaitable[None]], page: str = "admin"):
        self.build, self.page = build, page
        self.box = ui.column().classes("w-full gap-3")
        self.built = False

    async def show(self) -> None:
        if not self.built:
            await self.refresh()

    async def refresh(self) -> None:
        self.built = True
        self.box.clear()
        with self.box:
            try:
                await self.build(self)
            except api.ApiError as exc:
                if exc.status == 401:
                    to_sign_in(self.page)
                    return
                error_box(exc.message)

    async def act(self, method: str, path: str, body: Any = None, *, refresh: bool = True) -> dict | None:
        """Runs an admin action, shows its message and redraws the section. None when it failed."""
        try:
            answer = await api.request(method, path, json=body if body is not None else {})
        except api.ApiError as exc:
            if exc.status == 401:
                to_sign_in(self.page)
                return None
            notify("error", exc.message)
            return None
        if isinstance(answer, dict) and answer.get("message"):
            notify(answer.get("kind", "success"), answer["message"])
        if refresh:
            await self.refresh()
        return answer if isinstance(answer, dict) else {}


def stamp(value: Any) -> str:
    """One timestamp format everywhere: "2026-10-02 18:40"."""
    text = str(value or "").replace("T", " ")
    return text[:16] if text else "–"
