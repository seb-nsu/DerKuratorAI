"""Look of the NiceGUI front end from the shared design tokens (frontends/design/tokens.json): CSS variables for the
light and dark theme, Quasar brand colours, focus ring, mobile gutters."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from nicegui import ui

TOKENS_FILE = Path(__file__).resolve().parents[1] / "design" / "tokens.json"


@lru_cache(maxsize=1)
def tokens() -> dict:
    return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))


def _vars(palette: dict) -> str:
    return " ".join(f"--dk-{name.replace('_', '-')}: {value};" for name, value in palette.items())


def _quasar(palette: dict) -> str:
    """Quasar's brand colours as CSS variables per theme (set here, not with ui.colors, which would pin them inline
    on <body> and ignore the dark theme)."""
    names = {"primary": "primary", "secondary": "accent", "positive": "success", "negative": "danger",
             "warning": "warning", "info": "info"}
    return " ".join(f"--q-{q}: {palette[k]} !important;" for q, k in names.items())


def css() -> str:
    t = tokens()
    space, radius = t["space"], t["radius"]
    return f"""
:root {{ {_vars(t['light'])} --dk-radius-card: {radius['card']}; --dk-radius-control: {radius['control']};
  --dk-page-x: {space['page_x']}; --dk-max-width: {space['max_width']}; --dk-font: {t['font']['family']}; }}
body.body--dark {{ {_vars(t['dark'])} {_quasar(t['dark'])} }}
body.body--dark .q-btn.bg-primary {{ color: var(--dk-on-primary) !important; }}
body {{ background: var(--dk-background); color: var(--dk-text); font-family: var(--dk-font);
  {_quasar(t['light'])} }}
:focus-visible {{ outline: 2px solid var(--dk-focus) !important; outline-offset: 2px; }}
.dk-page {{ width: 100%; max-width: var(--dk-max-width); margin: 0 auto; padding: 16px var(--dk-page-x) 48px; gap: 16px; }}
.dk-card {{ background: var(--dk-surface-raised); border: 1px solid var(--dk-border); border-radius: var(--dk-radius-card);
  padding: 16px; height: 100%; }}
.dk-muted {{ color: var(--dk-text-muted); }}
.dk-kpi-value {{ font-size: 1.7rem; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.2; }}
.dk-badge {{ display: inline-flex; align-items: center; gap: 4px; padding: 2px 10px; border-radius: 999px;
  font-size: 0.8rem; font-weight: 600; max-width: 100%; overflow-wrap: anywhere; }}
{''.join(f'.dk-tone-{k} {{ color: var(--dk-{k}); background: var(--dk-{k}-bg); }}' for k in ('success', 'warning', 'danger', 'info', 'neutral'))}
.dk-callout {{ border-radius: var(--dk-radius-card); padding: 14px 16px; border-left: 4px solid currentColor; }}
.dk-header {{ background: var(--dk-surface-raised) !important; color: var(--dk-text) !important;
  border-bottom: 1px solid var(--dk-border); }}
.dk-nav .q-tab {{ text-transform: none; font-weight: 500; min-height: 44px; padding: 0 10px; }}
@media (max-width: 1535px) {{ .dk-hide-narrow {{ display: none; }} }}  /* the account name: the navigation keeps its labels */
/* 1024–1279 px: the navigation drops its icons, so all pages fit next to the buttons (same rule in the React frame) */
@media (min-width: 1024px) and (max-width: 1279px) {{
  .dk-nav .q-tab {{ padding: 0 6px; }}
  .dk-nav .q-tab__icon {{ display: none; }}
}}
.dk-mark {{ display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px;
  border-radius: 6px; background: var(--dk-primary); color: var(--dk-on-primary); font-weight: 700; font-size: 12px; }}
.dk-subbar {{ border-bottom: 1px solid var(--dk-border); background: var(--dk-surface-raised); }}
.dk-subbar .dk-badge {{ white-space: nowrap; overflow-wrap: normal; flex-shrink: 0; }}  /* "DEV" never wraps to "DE / V" on a phone */
.q-btn {{ text-transform: none; }}
.dk-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); align-items: stretch; }}
.dk-grid-2 {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); align-items: stretch; }}
.q-table__container {{ background: var(--dk-surface-raised); border-radius: var(--dk-radius-card); }}
@media (max-width: {t['breakpoints']['mobile']}px) {{
  :root {{ --dk-page-x: {space['page_x_mobile']}; }}
  .dk-grid-2 {{ grid-template-columns: 1fr; }}
  .dk-kpi-value {{ font-size: 1.4rem; }}
}}
"""


def apply() -> None:
    """Once per page: CSS variables and Quasar brand colours of both themes."""
    ui.add_css(css())


def tone(kind: str, value: str | None) -> str:
    """Tone (success/warning/danger/neutral/info) of a verdict, RAG status or environment."""
    return tokens().get(kind, {}).get(str(value or ""), "neutral")
