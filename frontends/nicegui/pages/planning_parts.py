"""Building blocks of the Planning and What-if comparison pages: business tables from API rows (money, percent bars,
verdict badges), callouts, the rule-based verdict block of a plan and file downloads. Presentation only – every
figure and verdict comes from the API."""
from __future__ import annotations

import html
import math
from typing import Any

from nicegui import ui

from frontends.nicegui import api, theme
from frontends.nicegui.components import VERDICT_ICONS

CSS = """
.dk-table-wrap { overflow-x: auto; max-width: 100%; background: var(--dk-surface-raised); border: 1px solid var(--dk-border);
  border-radius: var(--dk-radius-card); }
.dk-table-wrap.dk-scroll { max-height: 420px; overflow-y: auto; }
.dk-table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 0.9rem; }
.dk-table th, .dk-table td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--dk-border); vertical-align: top; }
.dk-table th { font-weight: 600; color: var(--dk-text-muted); font-size: 0.82rem; position: sticky; top: 0;
  background: var(--dk-surface-raised); white-space: nowrap; }
.dk-table td { white-space: nowrap; }
.dk-table td.dk-num { text-align: right; }
.dk-table td.dk-wrap { white-space: normal; min-width: 260px; }
.dk-table tr:last-child td { border-bottom: none; }
.dk-bar { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
.dk-bar > span.dk-track { display: inline-block; width: 90px; height: 6px; border-radius: 3px; background: var(--dk-neutral-bg); overflow: hidden; }
.dk-bar > span.dk-track > span { display: block; height: 100%; background: var(--dk-primary); }
.dk-bar.dk-over > span.dk-track > span { background: var(--dk-danger); }
.dk-caption { color: var(--dk-text-muted); font-size: 0.85rem; }
.dk-badges { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.dk-badges .dk-badge { white-space: normal; }
.dk-grid > *, .dk-grid-2 > * { min-width: 0; }
.dk-stack { display: flex; flex-direction: column; gap: 12px; width: 100%; }
.dk-tabpanel { padding: 16px 0 0 !important; background: transparent !important; }
.dk-panels { background: transparent !important; }
.dk-paper p, .dk-paper li { color: var(--dk-text); }
ul.dk-list { list-style: disc; margin: 4px 0; padding-left: 22px; }
"""

# columns that hold euros and columns shown as a filled bar (share of 100 %)
MONEY = {"Cap", "Limit", "Committed", "Planned", "Free", "This demand", "CapEx", "OpEx", "Benefit", "NPV planned"}
MONEY_PREFIXES = ("CapEx ", "OpEx ", "NPV")
PERCENT_BARS = {"Used %"}
STATUS_TONES = {"Draft": "neutral", "Submitted": "info", "Assessed": "info", "Planned": "warning", "Approved": "success",
                "Deferred": "warning", "Rejected": "danger", "Converted": "success"}
DECISION_BADGES = {"In": ("success", "check_circle"), "Forced-in": ("info", "push_pin"),
                   "Deferred": ("warning", "schedule"), "Out": ("danger", "block")}


def install() -> None:
    ui.add_css(CSS)


def missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value.strip().lower() in ("", "none", "nan", "nat")


def eur(value: Any, signed: bool = False) -> str:
    """Whole euros with thousands separators, as everywhere in the product: "€420,000", "−€5,000"."""
    if missing(value):
        return "–"
    number = float(value)
    sign = "+" if signed and number > 0 else "−" if number < 0 else ""
    return f"{sign}€{abs(number):,.0f}"


def number(value: Any, digits: int | None = None) -> str:
    if missing(value):
        return "–"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if digits is not None:
        return f"{value:.{digits}f}"
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def verdict_tone(label: str | None) -> str:
    text = str(label or "").upper()
    if "INCOMPLETE" in text:
        return "neutral"
    if "NO-GO" in text or "NO GO" in text:
        return "danger"
    if "CONDITIONAL" in text:
        return "warning"
    return theme.tone("verdict", "GO") if "GO" in text.split() else "neutral"


def badge_html(text: str, tone: str, icon: str | None = None) -> str:
    symbol = f'<span class="material-icons" aria-hidden="true" style="font-size:16px">{icon}</span>' if icon else ""
    return f'<span class="dk-badge dk-tone-{tone}">{symbol}{html.escape(text)}</span>'


def verdict_badge_html(label: str | None) -> str:
    tone = verdict_tone(label)
    return badge_html(str(label) if not missing(label) else "No verdict", tone, VERDICT_ICONS.get(tone, "remove"))


def verdict_badge(label: str | None) -> None:
    ui.html(verdict_badge_html(label), tag="span")


def _bar(value: Any, text: str, top: float = 100.0) -> str:
    if missing(value):
        return "–"
    share = max(0.0, min(float(value) / top * 100, 100.0))
    over = " dk-over" if float(value) > top else ""
    return (f'<span class="dk-bar{over}"><span class="dk-track" aria-hidden="true"><span style="width:{share:.0f}%">'
            f"</span></span>{html.escape(text)}</span>")


def cell(column: str, value: Any, kind: str | None = None) -> tuple[str, bool]:
    """(cell html, right-aligned) of one value; kind: eur | bar | score | fte | dec1 | verdict | status | text."""
    if kind is None:
        if column in MONEY or column.startswith(MONEY_PREFIXES):
            kind = "eur"
        elif column in PERCENT_BARS:
            kind = "bar"
    if kind == "eur":
        return html.escape(eur(value)), True
    if kind == "bar":
        return _bar(value, "–" if missing(value) else f"{float(value):.0f} %"), False
    if kind == "score":
        return _bar(value, "–" if missing(value) else f"{float(value):.1f}"), False
    if kind == "fte":
        return ("–" if missing(value) else f"{float(value):.1f} FTE"), True
    if kind == "dec1":
        return ("–" if missing(value) else f"{float(value):.1f}"), True
    if kind == "verdict":
        return ("–" if missing(value) else verdict_badge_html(value)), False
    if kind == "status":
        return ("–" if missing(value) else badge_html(str(value), STATUS_TONES.get(str(value), "neutral"))), False
    numeric = isinstance(value, int | float) and not isinstance(value, bool) and not missing(value)
    text = number(value) if numeric else ("–" if missing(value) else str(value))
    if not numeric and len(text) >= 19 and text[10:11] == "T" and text.endswith(("00:00:00", "00:00:00.000")):
        text = text[:10]  # midnight timestamps as dates
    return html.escape(text), numeric


def table(rows: list[dict], columns: list[str] | None = None, formats: dict[str, str] | None = None,
          labels: dict[str, str] | None = None, scroll: bool = False, caption: str | None = None) -> None:
    """A business table of API rows, scrolling inside its box on a narrow screen."""
    if not rows:
        return
    columns = columns or list(rows[0])
    formats, labels = formats or {}, labels or {}
    head = "".join(f'<th scope="col">{html.escape(labels.get(c, c))}</th>' for c in columns)
    body = []
    for row in rows:
        cells = []
        for c in columns:
            content, right = cell(c, row.get(c), formats.get(c))
            css = "dk-num" if right else "dk-wrap" if len(str(row.get(c) or "")) > 40 else ""
            cells.append(f'<td{f" class={css}" if css else ""}>{content}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    label = f' aria-label="{html.escape(caption)}"' if caption else ""
    ui.html(f'<div class="dk-table-wrap{" dk-scroll" if scroll else ""}" tabindex="0"{label} role="region">'
            f'<table class="dk-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>') \
        .classes("w-full")


def caption(text: str) -> None:
    ui.label(text).classes("dk-caption")


def strong(text: str) -> None:
    ui.label(text).classes("text-weight-bold")


def callout(text: str, tone: str = "info", icon: str = "info", markdown: bool = False) -> None:
    with ui.element("div").classes(f"dk-callout dk-tone-{tone} w-full").props('role="status"'):
        with ui.row().classes("items-start no-wrap gap-2"):
            ui.icon(icon, size="20px").props('aria-hidden="true"')
            if markdown:
                ui.markdown(text).classes("dk-paper").style("color: inherit")
            else:
                ui.label(text)


def subheading(text: str, level: int = 2) -> None:
    ui.label(text).classes("text-h6 text-weight-medium").props(f'role="heading" aria-level="{level}"')


def scenario_verdict(header: dict) -> None:
    """The rule-based verdict of a plan: badge, name, years, binding year, reasons, conditions with owner and due."""
    verdict = header.get("verdict") or {}
    years = header.get("years") or []
    with ui.element("div").classes("dk-card w-full").style("height:auto"):
        with ui.row().classes("items-center gap-2"):
            verdict_badge(verdict.get("label"))
            ui.label(header.get("name") or "").classes("text-weight-bold")
            if years:
                ui.label(f"· {years[0]}–{years[-1]} · binding year {header.get('binding_year')}")
        with ui.element("ul").classes("dk-list"):
            for reason in verdict.get("reasons") or []:
                with ui.element("li"):
                    ui.label(reason)
        details = verdict.get("condition_details") or []
        if details:
            table([{"Condition": c["condition"], "Owner": c["owner_role"], "Due": c.get("due")} for c in details])
        caption(f"Rule-based verdict (not AI-generated) · reference date {header.get('as_of')}"
                + (" · committed money partly estimated from the forecast to complete"
                   if header.get("estimated_commitments") else ""))


def decision_badges(counts: dict) -> None:
    with ui.element("div").classes("dk-badges"):
        for key, (tone, icon) in DECISION_BADGES.items():
            ui.html(badge_html(f"{counts.get(key, 0)} {key}", tone, icon), tag="span")


async def download(path: str, body: dict, filename: str) -> None:
    """A file the API builds (POST answer) handed to the browser."""
    headers = {"Authorization": f"Bearer {api.token()}"} if api.token() else {}
    async with api._client() as client:  # the same client, but the answer is a file, not JSON
        response = await client.post(path, json=body, headers=headers)
    if not response.is_success:
        try:
            message = response.json().get("detail")
        except ValueError:
            message = None
        raise api.ApiError(response.status_code, str(message or f"Request failed ({response.status_code})."))
    disposition = response.headers.get("content-disposition", "")
    if 'filename="' in disposition:  # the API's own name (it carries the reference date)
        filename = disposition.split('filename="', 1)[1].split('"', 1)[0] or filename
    ui.download.content(response.content, filename, response.headers.get("content-type", ""))


async def decision(path: str, body: dict) -> dict:
    """A decision route that answers {kind, message} also when it refuses (409): the answer to show either way."""
    headers = {"Authorization": f"Bearer {api.token()}"} if api.token() else {}
    async with api._client() as client:  # one call; a refusal carries its own message
        response = await client.post(path, json=body, headers=headers)
    if response.status_code == 401:
        api.sign_out()
    try:
        answer = response.json()
    except ValueError:
        answer = {}
    if response.is_success:
        return answer
    detail = answer.get("detail")
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) for d in detail[:3])
    return {"kind": answer.get("kind", "error"),
            "message": answer.get("message") or detail or f"Request failed ({response.status_code}).",
            "snapshot_id": None}


def set_query(**params: str | None) -> None:
    """Keeps the address bar in step with the page state, so a link opens the same view (deep link)."""
    import json

    ui.run_javascript(
        "const u = new URL(window.location.href);"
        f"for (const [k, v] of Object.entries({json.dumps(params)})) {{ if (v) u.searchParams.set(k, v); "
        "else u.searchParams.delete(k); }"
        "window.history.replaceState(null, '', u.toString());")


def query_param(name: str) -> str | None:
    try:
        return ui.context.client.request.query_params.get(name)
    except (AttributeError, RuntimeError):
        return None
