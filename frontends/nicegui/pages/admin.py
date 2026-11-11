"""System Admin Console: system status, operations (snapshots, database, notifications, environment staging), logs and
the developer tools (models and shadowing, MCP, FinOps, shadow logs, in-app system tests). Same areas, texts and
confirmations as the React console; every value and action comes from the /admin API. Deep link:
/admin?tab=<area> or ?tab=<area>.<section>, e.g. ?tab=developer.models."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import page_header
from frontends.nicegui.pages import admin_config, admin_logs, admin_ops
from frontends.nicegui.pages.admin_common import CSS, Section, callout

# area id -> (title, [(section id, tab label, builder)]); one section = no inner tabs
LAYOUT = {  # business tasks first, developer tools last; business rules and required fields are on the Rules page
    "status": ("System status", [("status", "System status", admin_ops.system_status)]),
    "operations": ("Operations", [
        ("snapshots", "Snapshots", admin_ops.snapshots),
        ("database", "Database", admin_ops.database),
        ("notifications", "Notifications", admin_ops.notifications),
        ("environment", "Environment staging", admin_config.environment),
    ]),
    "logs": ("Logs", [
        ("runs", "Analysis runs", admin_logs.analysis_runs),
        ("audit", "Audit logs", admin_logs.audit_logs),
        ("incidents", "Incidents", admin_logs.incidents),
        ("system", "System logs", admin_logs.system_logs),
    ]),
    "developer": ("Developer", [
        ("models", "Models, prompt & shadowing", admin_config.models),
        ("mcp", "Integrations (MCP)", admin_config.mcp),
        ("finops", "FinOps", admin_ops.finops),
        ("shadow", "Shadow logs", admin_logs.shadow_logs),
        ("tests", "System tests", admin_logs.system_tests),
    ]),
}


def _parse(tab: str | None) -> tuple[str, str | None]:
    area, _, section = (tab or "").partition(".")
    if area not in LAYOUT:
        return "status", None
    ids = [s[0] for s in LAYOUT[area][1]]
    return area, section if section in ids else None


def _remember(area: str, section: str | None) -> None:
    """Keeps the open tab in the address bar, so a reload or a shared link opens it again."""
    tab = f"{area}.{section}" if section else area
    ui.run_javascript(f"history.replaceState(null, '', '/admin?tab={tab}')")


async def render(info: dict, query: dict) -> None:
    ui.add_css(CSS)
    page_header("System Admin Console", "System-wide AI settings and logs. Non-secret settings live in "
                "data/env_<environment>.json; API keys come from .env and are kept in memory only.")
    status = await api.get("/admin/status")
    if status.get("config_issue"):
        callout("error", f"**Environment config not applied – running on defaults.** {status['config_issue']}. Fix the "
                "file or save the settings below to write a valid configuration.")

    area_id, section_id = _parse(query.get("tab"))
    sections: dict[str, dict[str, Section]] = {}
    inner_tabs: dict[str, ui.tabs] = {}

    async def open_area(area: str) -> None:
        inner = inner_tabs.get(area)
        current = inner.value if inner is not None else LAYOUT[area][1][0][0]
        await sections[area][current].show()
        _remember(area, current if inner is not None else None)

    async def open_section(area: str, section: str) -> None:
        await sections[area][section].show()
        _remember(area, section)

    with ui.tabs(value=area_id, on_change=lambda e: open_area(e.value)) \
            .props('dense no-caps align="left" active-color=primary indicator-color=primary outside-arrows mobile-arrows') \
            .classes("adm-tabs w-full") as areas:
        for aid, (title, _) in LAYOUT.items():
            ui.tab(aid, label=title)
    with ui.tab_panels(areas, value=area_id).props("animated=false keep-alive").classes("w-full"):
        for aid, (_, parts) in LAYOUT.items():
            with ui.tab_panel(aid):
                sections[aid] = {}
                if len(parts) == 1:
                    sid, _, build = parts[0]
                    sections[aid][sid] = Section(build)
                    continue
                first = section_id if aid == area_id and section_id else parts[0][0]
                with ui.tabs(value=first, on_change=lambda e, a=aid: open_section(a, e.value)) \
                        .props('dense no-caps align="left" active-color=primary indicator-color=primary '
                               'outside-arrows mobile-arrows').classes("adm-tabs w-full") as inner:
                    for sid, label, _ in parts:
                        ui.tab(sid, label=label)
                inner_tabs[aid] = inner
                with ui.tab_panels(inner, value=first).props("animated=false keep-alive").classes("w-full"):
                    for sid, _, build in parts:
                        with ui.tab_panel(sid):
                            sections[aid][sid] = Section(build)
    current = section_id or LAYOUT[area_id][1][0][0]
    await sections[area_id][current].show()
