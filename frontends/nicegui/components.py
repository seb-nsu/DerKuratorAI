"""Small building blocks every page uses, so all pages look alike: page header, card, KPI, badge, verdict callout,
notifications and Plotly figures from the API in the active theme."""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import app, ui

from frontends.nicegui import theme

VERDICT_ICONS = {"success": "check_circle", "warning": "warning", "danger": "block", "neutral": "help"}


def logo(wordmark: bool = True) -> None:
    """The DK mark (and the name), drawn in theme colours so it stays readable in dark mode."""
    with ui.row().classes("items-center no-wrap gap-2"):
        ui.html('<span class="dk-mark" aria-hidden="true">DK</span>')
        if wordmark:
            ui.label("DerKurator AI").classes("text-weight-bold")


def page_header(title: str, subtitle: str = "", link: tuple[str, str, str] | None = None) -> None:
    """`link` = (page id, icon, text): the link to a sub-page or back to its parent, next to the heading."""
    if link is None:
        _heading(title, subtitle)
        return
    with ui.row().classes("w-full items-end justify-between wrap gap-2"):
        _heading(title, subtitle)
        target, icon, text = link
        ui.button(text, icon=icon, on_click=lambda: ui.navigate.to(f"/{target}")) \
            .props("flat dense no-caps color=primary")


def _heading(title: str, subtitle: str) -> None:
    with ui.column().classes("gap-0"):
        ui.label(title).classes("text-h5 text-weight-bold").props('role="heading" aria-level="1"')
        if subtitle:
            ui.label(subtitle).classes("dk-muted")


def section(title: str) -> None:
    ui.label(title).classes("text-h6 text-weight-medium q-mt-md").props('role="heading" aria-level="2"')


@contextmanager
def card(title: str | None = None):
    with ui.element("div").classes("dk-card") as box:
        if title:
            ui.label(title).classes("text-subtitle1 text-weight-medium q-mb-sm")
        yield box


def badge(text: str, tone: str = "neutral", icon: str | None = None) -> None:
    with ui.element("span").classes(f"dk-badge dk-tone-{tone}"):
        if icon:
            ui.icon(icon, size="16px")
        ui.label(text)


def kpi(label: str, value: str, caption: str = "", help_text: str = "") -> None:
    with card():
        with ui.row().classes("items-center gap-1"):
            ui.label(label).classes("dk-muted")
            if help_text:
                ui.icon("help_outline", size="16px").classes("dk-muted").tooltip(help_text)
        ui.label(value).classes("dk-kpi-value")
        if caption:
            ui.label(caption).classes("dk-muted text-caption")


def verdict_callout(verdict: dict | None) -> None:
    """The rule-based verdict as the first thing of a report: label, rule and the reasons."""
    if not verdict:
        return
    tone = theme.tone("verdict", verdict.get("label"))
    with ui.element("div").classes(f"dk-callout dk-tone-{tone} w-full").props('role="status"'):
        with ui.row().classes("items-center gap-2"):
            ui.icon(VERDICT_ICONS.get(tone, "info"), size="24px")
            ui.label(str(verdict.get("label"))).classes("text-h6 text-weight-bold")
        for reason in verdict.get("reasons") or []:
            ui.label(str(reason)).classes("q-mt-xs").style("color: var(--dk-text)")
        for condition in verdict.get("conditions") or []:
            ui.label(f"Condition: {condition}").classes("q-mt-xs").style("color: var(--dk-text)")


def notify(kind: str, message: str) -> None:
    """kind: success | warning | error | info (same meaning as in the React pages)."""
    ui.notify(message, type={"success": "positive", "error": "negative"}.get(kind, kind), position="top",
              multi_line=True, close_button="OK" if kind == "error" else None)


def _recolour(value, colours: dict[str, str]):
    """Every colour of a figure found in `colours` (keys upper case) replaced, in data and layout alike."""
    if isinstance(value, dict):
        return {k: _recolour(v, colours) for k, v in value.items()}
    if isinstance(value, list):
        return [_recolour(v, colours) for v in value]
    if isinstance(value, str):
        return colours.get(value.upper().replace(" ", ""), value)
    return value


def _dark(figure: dict, palette: dict) -> dict:
    """The figure on the dark theme: the light-page colours of tokens.json "chart_dark" swapped, gridlines, axis lines
    and hover labels in theme colours. Light mode draws the API's figure unchanged."""
    colours = {k.upper().replace(" ", ""): v for k, v in theme.tokens()["chart_dark"]["colours"].items()}
    figure = _recolour(figure, colours)
    layout = figure.get("layout") or {}
    lines = {"gridcolor": palette["border"], "zerolinecolor": palette["border"], "linecolor": palette["border"]}
    axes = {k for k in layout if k.startswith(("xaxis", "yaxis"))} | {"xaxis", "yaxis"}
    layout = {**layout, **{k: {**(layout.get(k) or {}), **lines} for k in axes},
              "hoverlabel": {**(layout.get("hoverlabel") or {}), "bgcolor": palette["surface_raised"],
                             "bordercolor": palette["border"], "font": {"color": palette["text"]}}}
    return {**figure, "layout": layout}


def plotly(figure: dict, height: int | None = None) -> None:
    """A Plotly figure from the API, drawn in the active theme (transparent background, theme text colour; in dark
    mode also theme gridlines and the dark replacements of light-page chart colours)."""
    dark = bool(app.storage.user.get("dark"))
    palette = theme.tokens()["dark" if dark else "light"]
    if dark:
        figure = _dark(figure, palette)
    layout = {**(figure.get("layout") or {}), "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
              "font": {"color": palette["text"], "family": theme.tokens()["font"]["family"]}}
    if height:
        layout["height"] = height
    ui.plotly({**figure, "layout": layout}).classes("w-full")


def error_box(message: str) -> None:
    with ui.element("div").classes("dk-callout dk-tone-danger w-full").props('role="alert"'):
        ui.label(message)


def empty(message: str) -> None:
    ui.label(message).classes("dk-muted q-pa-md")


def flash(kind: str, message: str) -> None:
    """A notification for the next page view (after an action that reloads the page)."""
    app.storage.user["flash"] = [kind, message]


def show_flash() -> None:
    entry = app.storage.user.pop("flash", None)
    if entry:
        notify(*entry)
