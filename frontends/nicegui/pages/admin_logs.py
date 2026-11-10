"""Admin Console – logs (analysis runs, business audit trail and decision log with its hash chain, incidents, shadow
comparisons, system log) and the quality area with the in-app system tests and their HTML report."""
from __future__ import annotations

from datetime import datetime

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, kpi
from frontends.nicegui.pages.admin_common import (
    Section,
    callout,
    caption,
    confirm,
    heading,
    pre,
    rich,
    table,
    to_sign_in,
)

AGENT_LABELS = {"orchestrator": "Orchestrator", "data_steward": "Data Steward", "analyst": "Analyst",
                "simulator": "Simulator", "advisory": "Advisory"}


def toolbar(section: Section, name: str, label: str, clear_help: str) -> None:
    """Uniform refresh/clear bar. Clearing needs a confirmation."""
    async def clear() -> None:
        if await confirm(f"Clear {label}", clear_help, "Clear"):
            await section.act("POST", f"/admin/logs/{name}/clear", {"confirm": True})

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=section.refresh).props("outline")
        ui.button("Clear …", icon="delete", on_click=clear).props("outline color=negative").tooltip(clear_help)


# =========================================================================== analysis runs
async def analysis_runs(section: Section) -> None:
    files = await api.get("/admin/files")
    heading("Analysis runs")
    caption(f"Source: `{files['audit_log']}` (event `analysis_run`) – role, run mode, duration, rule-based verdict and "
            "the plan the pipeline executed.")
    view = await api.get("/admin/logs/analysis-runs")
    if not view["runs"]:
        callout("info", "No analyses recorded yet.")
        return

    async def show(modes: list[str]) -> None:
        rows = (await api.request("GET", "/admin/logs/analysis-runs", params={"mode": modes}))["runs"] if modes \
            else []
        box.clear()
        with box:
            table(rows, [("time", "Time"), ("environment", "Environment"), ("role", "Role"), ("mode", "Mode"),
                         ("duration_s", "Duration (s)"), ("verdict", "Verdict"), ("plan", "Plan (JSON)")])

    picker = ui.select(view["modes"], value=list(view["modes"]), multiple=True, label="Mode") \
        .props("outlined dense use-chips").classes("w-full").style("max-width: 520px")
    picker.on_value_change(lambda e: show(e.value or []))
    box = ui.column().classes("w-full")
    with box:
        table(view["runs"], [("time", "Time"), ("environment", "Environment"), ("role", "Role"), ("mode", "Mode"),
                             ("duration_s", "Duration (s)"), ("verdict", "Verdict"), ("plan", "Plan (JSON)")])
    if view["median_s"] is not None:
        caption(f"{view['total']} run(s) · median {view['median_s']:.1f} s · p95 {view['p95_s']:.1f} s")


# =========================================================================== audit logs
async def audit_logs(section: Section) -> None:
    files = await api.get("/admin/files")
    with ui.expansion("Business audit trail (project master-data edits)").classes("w-full"):
        caption(f"Source: `{files['business_audit_log']}` – one line per changed field.")
        try:
            lines = (await api.get("/admin/logs/business-audit"))["lines"]
        except api.ApiError as exc:
            if exc.status == 401:
                raise
            callout("error", exc.message)
            lines = None
        if lines:
            pre("\n".join(lines))  # newest first
        elif lines is not None:
            callout("info", "No entries")
    heading("Audit logs")
    caption(f"Source: `{files['audit_log']}` – approved scenarios, project edits and admin changes (no secrets).")
    toolbar(section, "audit", "audit log", "The content is copied to data/archive/ before clearing.")

    async def verify() -> None:
        try:
            check = await api.post("/admin/logs/audit/verify-chain")
        except api.ApiError as exc:
            if exc.status == 401:
                to_sign_in()
                return
            check = None
            message = exc.message
        chain_box.clear()
        with chain_box:
            if check is None:
                callout("error", message)
                return
            legacy = f" · {check['unchained']} older record(s) from before the chain" if check["unchained"] else ""
            if check["ok"]:
                callout("success", f"Hash chain intact: {check['chained']} chained record(s){legacy}.")
            else:
                callout("error", f"Hash chain broken at line {check['broken_at']}: {check['reason']}. Compare with the "
                                 "latest archive or snapshot before trusting later records.")

    ui.button("Verify the hash chain", icon="link", on_click=verify).props("outline")
    chain_box = ui.column().classes("w-full")
    log = await api.get("/admin/logs/audit", limit=1000)
    if not log["total"] and not log["events"]:
        callout("info", "No entries")
        return
    if log["malformed"]:
        callout("warning", f"{log['malformed']} corrupt line(s) skipped.")
    front = ("timestamp", "environment", "event", "role", "user_id", "request", "applied")

    def draw(records: list[dict], total: int) -> None:
        box.clear()
        with box:
            table(records, front=front)
            caption(f"{total} entries")

    async def filter_events(events: list[str]) -> None:
        if not events:
            draw([], 0)
            return
        answer = await api.request("GET", "/admin/logs/audit", params={"event": events, "limit": 1000})
        draw(answer["records"], answer["total"])

    picker = ui.select(log["events"], value=list(log["events"]), multiple=True, label="Event type") \
        .props("outlined dense use-chips").classes("w-full")
    picker.on_value_change(lambda e: filter_events(e.value or []))
    box = ui.column().classes("w-full")
    draw(log["records"], log["total"])


# =========================================================================== incidents
async def incidents(section: Section) -> None:
    files = await api.get("/admin/files")
    heading("Incidents")
    caption(f"Source: `{files['incidents_log']}` – incident ID, section and error type; the traceback is in the system "
            "log under the same ID.")
    view = await api.get("/admin/logs/incidents")
    if view["malformed"]:
        callout("warning", f"{view['malformed']} corrupt line(s) skipped.")
    if not view["incidents"]:
        callout("info", "No incidents recorded.")
        return
    table(view["incidents"], front=("timestamp", "incident_id", "event", "section", "error_type", "role", "user_id"))


# =========================================================================== shadow logs
def _side(entry: dict, key: str) -> dict:
    value = entry.get(key)
    return value if isinstance(value, dict) else {"result": value}


async def shadow_logs(section: Section) -> None:
    files = await api.get("/admin/files")
    heading("Shadow logs")
    caption(f"Source: `{files['shadow_log']}` – main run vs. shadow run (per-agent shadow models) per request.")
    view = await api.get("/admin/logs/shadow")
    with ui.expansion("Current per-agent shadow configuration").classes("w-full"):
        table(view["matrix"], [("agent", "agent"), ("main", "main"), ("shadow", "shadow")])
        caption("Shadowing active." if view["shadow_available"] else
                "Shadowing inactive (no shadow model set or keys missing) – configure it in Settings.")
    toolbar(section, "shadow", "shadow log", "The content is copied to data/archive/ before clearing.")
    if not view["entries"]:
        callout("info", "No entries")
        return
    if view["malformed"]:
        callout("warning", f"{view['malformed']} corrupt line(s) skipped.")
    for entry in view["entries"]:
        primary, shadow = _side(entry, "primary"), _side(entry, "shadow")
        p_t, s_t = primary.get("duration_s"), shadow.get("duration_s")
        delta = f" · Δ {s_t - p_t:+.1f}s" if isinstance(p_t, (int, float)) and isinstance(s_t, (int, float)) else ""
        match = entry.get("verdict_match")
        badge = " · ✓ same verdict" if match is True else " · ⚠ verdicts differ" if match is False else ""
        title = f"{entry.get('timestamp', '?')} · {str(entry.get('request', '–'))[:80]}{delta}{badge}"
        with ui.expansion(title).classes("w-full"):
            rich(f"**Request:** {entry.get('request', '–')}")
            with ui.element("div").classes("dk-grid-2 w-full"):
                for label, data, verdict in (("Main run", primary, entry.get("verdict_primary")),
                                             ("Shadow run", shadow, entry.get("verdict_shadow"))):
                    with ui.column().classes("gap-2"):
                        rich(f"**{label}**" + (f" · recommendation: **{verdict}**" if verdict else ""))
                        models = data.get("models")
                        if isinstance(models, dict):
                            table([{"agent": AGENT_LABELS.get(a, a), "model": m} for a, m in models.items()],
                                  [("agent", "agent"), ("model", "model")])
                        else:
                            caption(f"Model: `{data.get('model', 'n/a')}`")
                        dur = data.get("duration_s")
                        kpi("Runtime", f"{dur:.2f} s" if isinstance(dur, (int, float)) else "–")
                        if data.get("error"):
                            callout("error", str(data["error"]))
                        with ui.element("div").classes("adm-scroll w-full").props('tabindex="0"'):
                            ui.markdown(str(data.get("result") or "_no result_"))
            if entry.get("data_steward_shadow"):
                ds = entry["data_steward_shadow"]
                with card():
                    rich(f"**Data Steward shadow interpretation** (`{ds.get('model')}`, {ds.get('source')})")
                    ui.markdown(str(ds.get("interpretation", "")))


# =========================================================================== system logs
async def system_logs(section: Section) -> None:
    files = await api.get("/admin/files")
    heading("System logs")
    caption(f"Source: `{files['system_log']}` – last 100 lines, newest first.")
    toolbar(section, "system", "system log", "Clears data/app.log (the clearing itself is logged).")
    lines = (await api.get("/admin/logs/system"))["lines"]
    if not lines:
        callout("info", "No entries")
        return

    async def show(level: str) -> None:
        shown = lines if level == "ALL" else (await api.get("/admin/logs/system", level=level))["lines"]
        box.clear()
        with box:
            pre("\n".join(shown) or "(no lines for this filter)")

    ui.toggle(["ALL", "INFO", "WARNING", "ERROR"], value="ALL", on_change=lambda e: show(e.value or "ALL")) \
        .props('aria-label="Level" no-caps')
    box = ui.column().classes("w-full")
    with box:
        pre("\n".join(lines))


# =========================================================================== system tests
def _local(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso or "–")


def test_results(counts: dict, cases: list[dict], header: str) -> None:
    if not counts:
        return
    caption(header)
    with ui.element("div").classes("dk-grid w-full"):
        kpi("Passed", str(counts.get("passed", 0)))
        kpi("Failed", str(counts.get("failed", 0)))
        kpi("Errors", str(counts.get("error", 0)))
        kpi("Skipped", str(counts.get("skipped", 0)))
    if cases:
        order = {"failed": 0, "error": 1, "skipped": 2, "passed": 3}
        rows = sorted(cases, key=lambda c: (order.get(c["outcome"], 4), c["module"], c["test"]))
        table([{**c, "duration_s": f"{c['duration_s']:.3f}"} for c in rows],
              [("module", "module"), ("test", "test"), ("outcome", "Outcome"), ("duration_s", "Duration (s)"),
               ("message", "message")])


async def system_tests(section: Section) -> None:
    view = await api.get("/admin/system-tests")
    files = await api.get("/admin/files")
    heading("System tests")
    caption("Runs the pytest suite (security, simulator guardrails, Data Steward, config, admin console) on a "
            "deterministic mock portfolio – no network access; logs, decisions and proposals of the run go to a "
            "temporary folder, so the live data stays unchanged.")
    if not view["enabled"]:
        callout("info", "System tests are disabled on this instance (FORCE_OFFLINE_MODE, e.g. the hosted demo). Run "
                        "them locally with `python run_tests.py`.")

    async def run() -> None:
        button.props("loading")
        try:
            section.last_run = await api.post("/admin/system-tests/run")
        except api.ApiError as exc:
            button.props(remove="loading")
            if exc.status == 401:
                to_sign_in()
                return
            callout_box.clear()
            with callout_box:
                callout("warning", exc.message)
            return
        await section.refresh()

    button = ui.button("Run system tests now", on_click=run).props("unelevated color=primary")
    button.set_enabled(view["enabled"])
    callout_box = ui.column().classes("w-full")
    last = getattr(section, "last_run", None)
    if last is not None:
        callout("success" if last["passed"] else "error",
                f"{last['status']} (exit code {last['returncode']}) · duration {last['duration_s']:.1f} s · started "
                f"{last['started_at']}")
        test_results(last["counts"], last["cases"], "Results of this run")
        with ui.expansion("Console output").classes("w-full"):
            pre(last["stdout"] or "(no output on stdout)")
            if last["stderr"].strip():
                rich("**stderr**")
                pre(last["stderr"])
    elif view["last_run"]:
        saved = view["last_run"]
        test_results(saved["counts"], saved["cases"], f"Last saved run: {_local(saved['saved_at'])}")
    ui.separator()
    heading("HTML test report (pytest-html)", 3)
    if not view["report_saved_at"]:
        callout("info", "No test report yet – click 'Run system tests now' (or run `python run_tests.py`).")
        return
    caption(f"As of: {_local(view['report_saved_at'])} · file: `{files['test_report']}`")

    async def download() -> None:
        html = await _report()
        if html is not None:
            ui.download.content(html, "test_report.html", "text/html")

    ui.button("Download test report (HTML)", icon="download", on_click=download).props("outline")

    async def show_report(e) -> None:
        if not e.value or frame_box.default_slot.children:
            return
        html = await _report()
        if html is None:
            return
        with frame_box:
            frame = ui.element("iframe").props('title="Test report"').classes("w-full") \
                .style("height: 650px; border: 1px solid var(--dk-border); border-radius: 6px; background: #fff")
            frame._props["srcdoc"] = html  # the report renders its table with its own JS and CSS, kept isolated
            frame.update()

    with ui.expansion("Show test report", on_value_change=show_report).classes("w-full"):
        frame_box = ui.column().classes("w-full")


async def _report() -> str | None:
    try:
        return await api.get_text("/admin/system-tests/report")
    except api.ApiError as exc:
        if exc.status == 401:
            to_sign_in()
        else:
            callout("warning", f"Test report could not be read: {exc.message}")
        return None
