"""Admin Console – system status (with ServiceNow and the write journal) and the operations area: FinOps,
notifications, snapshots with the data lifecycle (demo reset, purge of a user) and the database management."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, kpi
from frontends.nicegui.pages.admin_common import Section, callout, caption, confirm, heading, rich, stamp, table


# =========================================================================== system status
async def system_status(section: Section) -> None:
    status = await api.get("/admin/status")
    files = await api.get("/admin/files")
    calls = await api.get("/admin/logs/llm-calls")
    graph, tracing = status["graph"], status["tracing"]
    with ui.element("div").classes("dk-grid w-full"):
        kpi("Environment", status["environment"], caption=f"Data source: {status['data_source']}",
            help_text=f"Data source: {status['data_source']}")
        kpi("PII masking", "On" if status["pii_masking_enabled"] else "OFF")
        kpi("LLM agents", "Active" if status["llm_available"] else "Offline",
            help_text="Offline = deterministic rule-based parser")
        kpi("Dependency graph", "Ready" if graph["ok"] else "Unavailable", help_text=graph["message"])
        kpi("OpenTelemetry export", "Active" if tracing["enabled"] else "Off",
            caption=f"Collector: {tracing['endpoint']}" if tracing["endpoint"] else "", help_text=tracing["message"])
    callout("success" if graph["ok"] else "warning", f"Dependency graph: {graph['message']}")
    callout("success" if tracing["enabled"] else "info", tracing["message"])
    if status["llm_available"]:
        callout("success", "LLM agents: active")
    else:
        missing = status["missing_keys"]
        callout("warning", "LLM agents: offline mode" + (
            f" – missing keys or EU endpoints for the selected models: {', '.join(missing)}" if missing else ""))
    if not status["pii_masking_enabled"]:
        callout("error", "PII masking is disabled: real names (PM, sponsor, assignees, stakeholders) are sent to LLM "
                         "providers.")
    heading("LLM routing", 3)
    table(status["routing"], [("agent_role", "Agent role"), ("model", "Model"), ("shadow", "Shadow")])
    caption(f"Temperature {status['temperature']:.2f} · Data Steward checks → deterministic")

    rich(f"**LLM calls** · last {calls['window']} calls of this process (log: `{files['llm_calls_log']}`, no prompt or "
         "answer text)")
    if not calls["summary"]:
        caption("No LLM call yet.")
    else:
        table(calls["summary"], [("caller", "Caller"), ("model", "Model"), ("calls", "Calls"), ("errors", "Errors"),
                                 ("median_s", "Median s"), ("tokens", "Tokens"), ("cost_usd", "Cost USD")])
        with ui.expansion("Last calls").classes("w-full"):
            table(calls["recent"])

    rich(f"**Data residency** · `LLM_DATA_RESIDENCY={status['data_residency']}`")
    table(status["endpoints"], [("provider", "Provider"), ("endpoint", "Endpoint"),
                                ("residency", "Under the residency setting")])
    caption("Set in `.env` or the secrets: `LLM_DATA_RESIDENCY=eu` calls only providers with an own endpoint "
            "(`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` – EU gateway, Azure OpenAI or a local model server); the app "
            "cannot check an endpoint's region, the setting states it. See docs/DATA_PROTECTION.md.")
    heading("Sign-in", 3)
    table(status["auth"], [("setting", "Setting"), ("value", "Value")])
    caption(f"Section \"auth\" of `{files['env_file']}` (read-only here).")
    backend = status["graph_backend"]
    heading("Graph backend", 3)
    if backend["warning"]:
        callout("warning", backend["warning"])
    table(backend["rows"], [("setting", "Setting"), ("value", "Value")])
    caption(backend["note"])
    await servicenow(section)
    await write_journal(section)


async def servicenow(section: Section) -> None:
    """Last sync per ServiceNow source and the buttons to read the changes (or everything) now."""
    view = await api.get("/admin/servicenow")
    if view["error"]:
        callout("error", f"ServiceNow: {view['error']}")
        return
    if not view["configured"]:
        return
    heading("ServiceNow", 3)
    table(view["tables"], [("instance", "Instance"), ("table", "Table"), ("rows", "Rows"),
                           ("changed_up_to", "Changed up to"), ("last_sync", "Last sync")])

    async def full() -> None:
        if await confirm("Full sync", "Reads every ServiceNow table completely and replaces the local snapshot. This "
                         "takes much longer than a change sync and counts against the instance's API limits."):
            await section.act("POST", "/admin/servicenow/sync", {"full": True, "confirm": True})

    with ui.element("div").classes("dk-grid w-full"):
        ui.button("Sync ServiceNow (changes)", icon="sync",
                  on_click=lambda: section.act("POST", "/admin/servicenow/sync", {"full": False})) \
            .props("outline").classes("w-full").tooltip("Reads only the records changed since the last sync.")
        ui.button("Full sync …", icon="cloud_download", on_click=full).props("outline").classes("w-full") \
            .tooltip("Reads every table completely (after a mapping change).")
    caption("Read-only: approved changes stay in DerKurator's own store and are never written to ServiceNow. "
            "Credentials come from the secrets SERVICENOW_INSTANCE, SERVICENOW_USER/SERVICENOW_PASSWORD or "
            "SERVICENOW_TOKEN.")


async def write_journal(section: Section) -> None:
    """Gaps between the intent and committed records of data writes and the proposal decisions. Nothing is repaired
    automatically; the admin marks a finding as resolved with a comment (audited)."""
    heading("Write journal", 3)
    findings = (await api.get("/admin/journal"))["findings"]
    if not findings:
        callout("success", "Every data write has its intent and committed record, and every written proposal its "
                           "decision.")
        return
    callout("error", f"{len(findings)} write journal finding(s) – check the data against the snapshot, then mark as "
                     "resolved.")
    for f in findings:
        with card():
            rich(f"**{f['label']}** · operation `{f['operation_id']}` · snapshot `{f.get('snapshot_id') or '–'}` · "
                 f"proposal `{f.get('proposal_id') or '–'}` · {f.get('user_id') or '–'} · {stamp(f.get('timestamp'))}",
                 "text-negative")
            caption(f.get("detail") or "")
            with ui.row().classes("w-full items-end no-wrap gap-2"):
                comment = ui.input("Comment (required)").props("outlined dense").classes("col-grow")
                button = ui.button("Mark as resolved", on_click=lambda f=f, c=comment: section.act(
                    "POST", "/admin/journal/resolve",
                    {"operation_id": f["operation_id"], "kind": f["kind"], "comment": c.value.strip()})) \
                    .props("outline")
                button.bind_enabled_from(comment, "value", backward=lambda v: bool((v or "").strip()))


# =========================================================================== FinOps
async def finops(section: Section) -> None:
    usage = await api.get("/admin/finops")
    with card():
        heading("FinOps – estimated LLM costs")
        with ui.element("div").classes("dk-grid w-full q-mt-sm"):
            kpi("Estimated costs (USD)", f"${usage['cost_usd']:.4f}",
                help_text="Since app start / last reset, all users incl. background shadow runs. List prices from "
                          "supported_models.json (input/output_cost_per_1m) – an estimate, not an invoice.")
            kpi("Tokens", f"{usage['total_tokens']:,}",
                help_text=f"{usage['prompt_tokens']:,} in · {usage['completion_tokens']:,} out")
            kpi("LLM calls", str(usage["calls"]))

            async def reset() -> None:
                if await confirm("Reset counters", "Sets the cost, token and call counters back to zero. The per-call "
                                 "log file stays."):
                    await section.act("POST", "/admin/finops/reset", {"confirm": True})

            with ui.column().classes("justify-center"):
                ui.button("Reset counters …", icon="restart_alt", on_click=reset).props("outline")
        if usage["unpriced_models"]:
            callout("warning", f"No price configured for: {', '.join(usage['unpriced_models'])} – their costs are not "
                               "included.")
        if usage["by_model"]:
            rows = [{**r, "thinking": "yes" if r["thinking"] else "", "cost_usd": f"${r['cost_usd']:.4f}"}
                    for r in usage["by_model"]]
            table(rows, [("model", "model"), ("thinking", "thinking"), ("calls", "calls"),
                         ("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                         ("cost_usd", "Cost (USD)")])
            if usage["thinking_share"] >= 0.5:
                with ui.row().classes("items-center gap-1"):
                    ui.icon("psychology", size="18px").classes("dk-muted").props('aria-hidden="true"')
                    caption(f"Thinking models cause {usage['thinking_share']:.0%} of the estimated costs – consider a "
                            "cheaper main model for routine agents (e.g. Orchestrator).")


# =========================================================================== notifications
NOTIFY_TEXT = ("Mail (SMTP) and Microsoft Teams messages about the approval inbox: a proposal waiting for a decision, "
               "an overdue or expired proposal, the decision back to the requester. Set in `.env` or the secrets "
               "(`NOTIFY_*`, docs/PILOT_OPERATIONS.md); secrets are never shown here, and the outbox only in counts.")


async def notifications(section: Section) -> None:
    """On/off, the configured channels, the outbox in counts and a test message (audited)."""
    view = await api.get("/admin/notifications")
    with card():
        heading("Notifications")
        caption(NOTIFY_TEXT)
        with ui.element("div").classes("dk-grid w-full q-mt-sm"):
            kpi("Notifications", "On" if view["enabled"] else "Off")
            kpi("Sent", str(view["sent"]),
                help_text=f"Last sent {stamp(view['last_sent_at'])}" if view["last_sent_at"] else "")
            kpi("Waiting", str(view["queued"]),
                help_text=f"{view['retrying']} retrying · {view['events_waiting']} event(s) not resolved yet")
            kpi("Failed", str(view["failed"]), help_text="Given up after the last attempt")
        if not view["enabled"]:
            callout("info", "Notifications are off – set `NOTIFY_ENABLED=true` and at least one channel.")
        error = view["last_error"]
        if error:
            callout("warning", f"Last delivery error ({stamp(error['at'])}, {error['channel']}): {error['error']}")
        table(view["channels"], [("channel", "Channel"), ("status", "Status")])
        with ui.row().classes("w-full items-end no-wrap gap-2"):
            address = ui.input("Test recipient (mail)", placeholder="empty: the role mailboxes") \
                .props("outlined dense").classes("col-grow")
            ui.button("Send test notification", icon="send", on_click=lambda: section.act(
                "POST", "/admin/notifications/test", {"to": (address.value or "").strip()})).props("outline")
        caption("Without SSO the addresses come from the `email` of the accounts in the user file and the role "
                "mailboxes; SSO will supply them later.")


# =========================================================================== snapshots and data lifecycle
async def snapshots(section: Section) -> None:
    view = await api.get("/admin/snapshots")
    files = await api.get("/admin/files")
    heading("Snapshots")
    caption(f"A copy of the data source is saved to `{files['snapshot_dir']}` before every write (Approve & Apply, "
            f"project edits, restores). The newest {view['max_snapshots']} are kept. Active source: "
            f"`{files['data_source'] or 'n/a'}`.")
    ui.button("Create snapshot now", icon="photo_camera",
              on_click=lambda: section.act("POST", "/admin/snapshots")) \
        .props("outline").set_enabled(view["source"] is not None)
    snaps = view["snapshots"]
    if not snaps:
        callout("info", "No snapshots yet – the first one is created with the next data change.")
    else:
        table([{"id": s["id"], "created": stamp(s["created_at"]), "reason": s["reason"], "user": s["user"] or "–",
                "env": s["environment"], "changes": len(s["changes"]), "size_kb": s["size_kb"]} for s in snaps],
              [(k, k) for k in ("id", "created", "reason", "user", "env", "changes", "size_kb")], page_size=10)
        labels = {s["id"]: f"{str(s.get('created_at') or '').replace('T', ' ')} · {s['reason']} · {s['user'] or '–'} "
                           f"({s['id']})" for s in snaps}
        with card():
            heading("Restore", 3)
            select = ui.select(labels, value=snaps[0]["id"], label="Snapshot").props("outlined dense") \
                .classes("w-full")
            changes_box = ui.column().classes("w-full")

            def show_changes() -> None:
                changes_box.clear()
                chosen = next((s for s in snaps if s["id"] == select.value), None)
                if chosen and chosen["changes"]:
                    with changes_box, ui.expansion(f"Changes written after this snapshot ({len(chosen['changes'])})") \
                            .classes("w-full"):
                        table(chosen["changes"], [(k, k) for k in ("project_id", "table", "field", "old", "new",
                                                                     "source_tool")])

            select.on_value_change(show_changes)
            show_changes()
            message = ("Restoring replaces the complete data source with this snapshot – all later changes are undone. "
                       "The current state is saved as a new snapshot first; the graph database is rebuilt.")
            callout("warning", message)

            async def restore() -> None:
                if await confirm("Restore snapshot", f"I want to restore this snapshot: {labels[select.value]}. "
                                 + message):
                    await section.act("POST", f"/admin/snapshots/{select.value}/restore", {"confirm": True})

            ui.button("Restore snapshot …", icon="restore", on_click=restore).props("unelevated color=primary")
    if view["demo_reset_available"]:  # never offered in PROD
        heading("Reset demo data")
        reset_text = ("Puts the portfolio back to the state this instance started with and archives open proposals, "
                      "chats and saved what-ifs (data/archive/). The current state is saved as a snapshot first.")
        caption(reset_text)

        async def reset_demo() -> None:
            if await confirm("Reset demo data", f"I want to reset the demo data. {reset_text}"):
                await section.act("POST", "/admin/demo-reset", {"confirm": True})

        ui.button("Reset demo data …", icon="settings_backup_restore", on_click=reset_demo).props("outline")
    heading("Purge user")
    purge_text = ("Erasure request: removes the user's chats and saved what-ifs and replaces the user id in proposals "
                  "and archives by a pseudonym. The decision log and the snapshots stay – they are approval evidence.")
    caption(purge_text)
    user_id = ui.input("User id", placeholder="exactly as in the user file").props("outlined dense") \
        .classes("w-full").style("max-width: 420px")

    async def purge() -> None:
        target = (user_id.value or "").strip()
        if await confirm("Purge user", f"I want to purge the personal data of {target}. {purge_text}"):
            await section.act("POST", "/admin/purge-user", {"user_id": target, "confirm": True})

    ui.button("Purge user …", icon="person_remove", on_click=purge).props("outline") \
        .bind_enabled_from(user_id, "value", backward=lambda v: bool((v or "").strip()))


# =========================================================================== database
async def database(section: Section) -> None:
    view = await api.get("/admin/database")
    source = {"json": "JSON", "excel": "Excel", "mapped": "the mapped sources"}.get(view["data_source"],
                                                                                    view["data_source"])
    with card():
        heading("Database management")
        caption(f"Dependency graph (NetworkX, in memory) · source: {source}. The app builds it on first use and "
                "rebuilds it automatically when the source data changes – the button forces an immediate rebuild.")
        meta = view["graph"]
        if meta is None:
            caption("The graph has not been built in this process yet – it is built on the first analysis.")
        else:
            counts = meta["counts"]
            with ui.row().classes("items-center no-wrap gap-1"):
                ui.icon("check_circle", size="18px").classes("text-positive").props('aria-hidden="true"')
                caption(f"Built {stamp(meta['built_at'])} in {meta['build_seconds']:g} s (source: "
                        f"{meta.get('source') or '?'}) · {counts.get('Project', 0)} projects, {counts.get('Task', 0)} "
                        f"tasks, {counts.get('Team', 0)} teams, {counts.get('Risk', 0)} risks, "
                        f"{counts.get('DEPENDS_ON', 0)} project dependencies")
        if view["import_report_error"]:
            callout("error", f"The data source could not be loaded: {view['import_report_error']}")
        report = view["import_report"]
        if report:
            with ui.expansion(f"Import report · {report['mapping']} · {report['findings']} finding(s)",
                              icon="fact_check").classes("w-full"):
                table(report["tables"], [("table", "Table"), ("source", "Source"), ("read", "Read"),
                                         ("loaded", "Loaded"), ("skipped", "Skipped"), ("notes", "Notes")])
                for note in report["notes"]:
                    caption(f"{note['table']}: {note['note']}")
                caption("Findings also appear in the Data Steward report. Mapping files: docs/DATA_MAPPING.md")

        async def rebuild() -> None:
            button.props("loading")
            await section.act("POST", "/admin/database/rebuild-graph")

        button = ui.button("Rebuild dependency graph", icon="restart_alt", on_click=rebuild) \
            .props("outline").classes("q-mt-sm")
