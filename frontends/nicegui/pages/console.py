"""Steering Console: guided scenario builder or free text (with scenario memory and the PMO's forced simulation), the
run of a what-if, the analysis history and the report viewer with the actions on a result (export, save as what-if,
submit for approval, approve & apply or hand over). Same texts and rules as the Streamlit console; every rule and
figure comes from the API. Deep link: /console?action=<guided action>&project=<id> opens that guided scenario."""
from __future__ import annotations

from nicegui import app, ui

from frontends.nicegui import api, report_viewer, theme
from frontends.nicegui.components import badge, error_box, notify, page_header
from frontends.nicegui.pages.console_builder import Builder
from frontends.nicegui.shell import scope_text

HANDOVER_TEXT = "Nothing is written; the resource change is recorded for manual execution."
ACTIONS = {"apply": ("Approve & apply", "task_alt"), "handover": ("Approve & record hand-over", "assignment_turned_in"),
           "submit": ("Submit for approval", "send")}
FOUR_EYES = ("Budget, schedule, phase and stop changes need the approval of a PMO other than the requester (four-eyes "
             "principle).")

_CSS = """
.dk-console { display: grid; gap: 16px; grid-template-columns: minmax(240px, 1fr) minmax(0, 3.2fr); width: 100%;
  align-items: start; }
.dk-console > * { min-width: 0; }
.dk-run-item { border: 1px solid var(--dk-border); border-radius: var(--dk-radius-card); padding: 10px 12px; width: 100%;
  min-width: 0; overflow-wrap: anywhere; }
/* a long status badge ("Proposal … · Waiting for approval") wraps instead of widening a narrow page */
.dk-run-item .dk-badge, .dk-report .dk-badge { white-space: normal; max-width: 100%; border-radius: 12px; }
.dk-run-item .dk-badge > .q-icon, .dk-report .dk-badge > .q-icon { flex-shrink: 0; }
.dk-run-item .dk-badge > div, .dk-report .dk-badge > div { min-width: 0; overflow-wrap: anywhere; }
.dk-run-item.dk-selected { border-color: var(--dk-primary); box-shadow: inset 3px 0 0 var(--dk-primary); }
.dk-actions { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); width: 100%;
  align-items: start; }
@media (max-width: 1023px) {
  .dk-console { grid-template-columns: 1fr; }
  .dk-console .dk-rail { order: 2; }
}
"""


def _runs(messages: list[dict]) -> list[tuple[dict | None, dict]]:
    """(user message, assistant message) per analysis, oldest first."""
    out = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            asked = messages[i - 1] if i > 0 and messages[i - 1].get("role") == "user" else None
            out.append((asked, m))
    return out


def _request_text(asked: dict | None, msg: dict) -> str:
    text = str((msg.get("meta") or {}).get("request") or (asked or {}).get("content") or "Analysis")
    return text.replace("**Scenario:** ", "")


def _run_key(msg: dict) -> str:
    return str((msg.get("meta") or {}).get("run_id") or msg.get("id") or "")


async def render(info: dict, query: dict) -> None:
    ui.add_css(_CSS)
    scope = info.get("scope") or {"visible": 0, "total": None}
    page_header("Steering Console", f"Scope: {scope_text(scope)} · the verdict is rule-based; the AI writes the "
                                    "decision paper", link=("scenarios", "compare_arrows", "Compare saved what-ifs"))
    catalog = await api.get("/steering/guided")
    state: dict = {"chat": await api.get("/steering/chat"), "selected": None, "reports": {}, "busy": False,
                   "error": None}

    def runs() -> list[tuple[dict | None, dict]]:
        return _runs(state["chat"]["messages"])

    def selected() -> tuple[dict | None, dict] | None:
        items = runs()
        if not items:
            return None
        return next((r for r in items if _run_key(r[1]) == state["selected"]), items[-1])

    async def reload_chat(select: str | None = None, drop: str | None = None) -> None:
        state["chat"] = builder.chat = await api.get("/steering/chat")
        if drop:
            state["reports"].pop(drop, None)
        if select:
            state["selected"] = select
        rail.refresh()
        builder.continuation.refresh()
        await viewer.refresh()

    async def run(body: dict) -> None:
        """One what-if: the API answers in one call (verdict and paper); until then the page shows a busy state."""
        if state["busy"]:
            return
        continuation = state["chat"]["continuation"]
        if builder.build_on and continuation.get("available"):
            body["base_run_id"] = continuation["base_run_id"]
        body["force"] = builder.force
        state.update(busy=True, error=None)
        builder.set_busy(True)
        await viewer.refresh()
        try:
            answer = await api.post("/steering/runs", body)
        except api.ApiError as exc:
            state["error"] = exc.message
            if exc.status == 422:  # an input the builder would not offer: the message says which
                notify("warning", exc.message)
            answer = None
        finally:
            state["busy"] = False
            builder.set_busy(False)
        if answer is None:
            await viewer.refresh()
            return
        notify("success", "Analysis complete")
        await reload_chat(select=answer["run_id"])

    # deep link /console?action=<guided action>&project=<id>; a request handed over by the question box
    params = ui.context.client.request.query_params if ui.context.client.request else {}
    handed = app.storage.user.pop("console_request", None)
    builder = Builder(catalog, state["chat"], run, action=params.get("action"), project=query.get("project"),
                      request=str(handed) if handed else None)

    # ------------------------------------------------------------------ actions of the selected report
    def actions(report: dict) -> None:
        act = report["actions"]
        run_id = report["run_id"]
        with ui.element("div").classes("dk-actions"):
            with ui.column().classes("gap-2"):
                report_viewer.export_menu(f"/steering/runs/{run_id}")
                if act["can_save"]:
                    _save_menu(run_id)
                elif act.get("saved_as"):
                    _caption("bookmark", f"Saved as {act['saved_as']}")
            with ui.column().classes("gap-2"):
                if act.get("note"):
                    ui.label(act["note"]).classes("dk-muted text-caption")
                if act["primary"] == "submit":
                    _submit(report)
                elif act["primary"] in ("apply", "handover"):
                    label, icon = ACTIONS[act["primary"]]
                    button = ui.button(f"{label} …", icon=icon, on_click=lambda: _confirm(report)) \
                        .props("unelevated color=primary no-caps").classes("w-full")
                    button.set_enabled(act["primary_enabled"])
                    if act.get("primary_help"):
                        button.tooltip(act["primary_help"])

    def _caption(icon: str, text: str) -> None:
        with ui.row().classes("items-center no-wrap gap-1 dk-muted text-caption"):
            ui.icon(icon, size="16px").props('aria-hidden="true"')
            ui.label(text)

    def _save_menu(run_id: str) -> None:
        with ui.button("Save as what-if", icon="bookmark_add").props("outline no-caps"):
            with ui.menu().props("max-width=360px"), ui.column().classes("q-pa-md gap-2"):
                name = ui.input("What-if name", placeholder="e.g. A – hard cut").props("outlined dense maxlength=80") \
                    .classes("w-full")
                save = ui.button("Save what-if", icon="bookmark_add",
                                 on_click=lambda: _save(run_id, name.value or "")) \
                    .props("unelevated color=primary no-caps")
                save.bind_enabled_from(name, "value", backward=lambda v: bool((v or "").strip()))

    async def _save(run_id: str, name: str) -> None:
        try:
            entry = await api.post(f"/steering/runs/{run_id}/save", {"name": name.strip()})
        except api.ApiError as exc:
            notify("error", exc.message if exc.message.startswith("What-if not saved") else
                   f"What-if not saved: {exc.message}")
            return
        notify("success", f"What-if '{entry['name']}' saved – compare it under What-if comparison.")
        await reload_chat(drop=run_id)

    def _submit(report: dict) -> None:
        act = report["actions"]
        label, icon = ACTIONS["submit"]
        if act["needs_override"]:  # a NO-GO is no recommendation to submit: secondary, with a reason
            question = f"Why submit despite {act['verdict_label']}? (required)"
            ui.label(question).classes("dk-muted text-caption")
            reason = ui.input(placeholder="e.g. Board asked for the cut anyway; risks accepted") \
                .props(f'outlined dense maxlength=2000 aria-label="{question}"').classes("w-full")
            button = ui.button(f"{label} anyway", icon=icon,
                               on_click=lambda: _send_proposal(report["run_id"], reason.value or "")) \
                .props("outline no-caps").classes("w-full")
            button.bind_enabled_from(reason, "value", backward=lambda v: bool((v or "").strip()))
            button.tooltip("Give the reason first – the PMO sees it with the proposal.")
        else:
            ui.button(label, icon=icon, on_click=lambda: _send_proposal(report["run_id"], "")) \
                .props("unelevated color=primary no-caps").classes("w-full")
        _caption("lock", FOUR_EYES)

    async def _send_proposal(run_id: str, justification: str) -> None:
        try:
            entry = await api.post("/proposals", {"run_id": run_id, "justification": justification})
        except api.ApiError as exc:
            notify("error", exc.message if exc.message.startswith("Proposal not submitted") else
                   f"Proposal not submitted: {exc.message}")
            return
        notify("success", f"Proposal {entry['id']} submitted – the PMO will review it in the approval inbox. You can "
                          "follow its status under Approvals.")
        await reload_chat(drop=run_id)

    def _confirm(report: dict) -> None:
        """Writing to the data source is irreversible for the user, so it requires explicit confirmation."""
        act = report["actions"]
        manual = act["primary"] == "handover"
        label, icon = ACTIONS[act["primary"]]
        with ui.dialog() as dialog, ui.card().classes("dk-card").style("max-width: 760px; width: 100%"):
            ui.label(f"Confirm: {label.lower()}").classes("text-h6").props('role="heading" aria-level="2"')
            if manual:
                with ui.element("div").classes("dk-callout dk-tone-info w-full"):
                    ui.label(HANDOVER_TEXT).style("color: var(--dk-text)")
            else:
                with ui.element("div").classes("dk-callout dk-tone-warning w-full"):
                    ui.markdown(f"Write **{act['change_count']} change(s)** to the portfolio data source now? A "
                                "snapshot is taken first; the System Admin can restore it.")
            report_viewer.table(act["confirm_rows"], ["Project", "Change", "Current", "Proposed", "Note"])
            override = reason = None
            if act["needs_override"]:
                verdict = act["verdict_label"]
                with ui.element("div").classes("dk-callout dk-tone-danger w-full").props('role="alert"'):
                    ui.markdown(f"The rule-based verdict is **{verdict}**. Approving means overriding it – the "
                                "override and your reason are recorded in the audit trail.")
                override = ui.checkbox(f"Override the {verdict} verdict")
                reason = ui.textarea("Reason for the override (required)").props("outlined maxlength=2000") \
                    .classes("w-full")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("outline no-caps")
                confirm = ui.button(f"Confirm: {label.lower()}", icon=icon,
                                    on_click=lambda: _apply(dialog, report["run_id"], override, reason)) \
                    .props("unelevated color=primary no-caps")
                confirm.set_enabled(act["primary_enabled"] and override is None)
                if override is not None:
                    def ready() -> None:
                        confirm.set_enabled(bool(override.value) and bool((reason.value or "").strip()))
                    override.on_value_change(ready)
                    reason.on_value_change(ready)
        dialog.open()

    async def _apply(dialog, run_id: str, override, reason) -> None:
        body = {"override": bool(override and override.value), "comment": (reason.value or "") if reason else ""}
        try:
            outcome = await api.post(f"/steering/runs/{run_id}/apply", body)
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        dialog.close()
        notify(outcome["kind"], outcome["message"])
        await reload_chat(drop=run_id)

    # ------------------------------------------------------------------ history rail
    @ui.refreshable
    def rail() -> None:
        items = runs()
        current = selected()
        with ui.element("div").classes("dk-card dk-rail w-full"):
            ui.label("Analyses").classes("text-subtitle1 text-weight-bold").props('role="heading" aria-level="2"')
            if not items:
                ui.label("No analyses yet.").classes("dk-muted text-caption")
            with ui.column().classes("w-full gap-2 q-mt-sm"):
                for asked, msg in reversed(items):
                    _rail_item(asked, msg, current is not None and current[1] is msg)
            if items:
                with ui.column().classes("w-full gap-0 q-mt-sm"):
                    ui.button("Clear history", icon="delete", on_click=_clear).props("flat no-caps").classes("w-full")

    def _rail_item(asked: dict | None, msg: dict, is_selected: bool) -> None:
        meta = msg.get("meta") or {}
        verdict = (meta.get("verdict") or {}).get("label") if isinstance(meta.get("verdict"), dict) else None
        turns = len(meta.get("turns") or [])
        step = f"Step {turns} · " if meta.get("continued") and turns else ""
        with ui.element("div").classes("dk-run-item" + (" dk-selected" if is_selected else "")):
            if verdict:
                tone = theme.tone("verdict", verdict)
                badge(verdict, tone, report_viewer.TONE_ICONS.get(tone, "remove"))
            else:
                badge("Analysis", "info", "insights")
            ui.label(f"{step}{_request_text(asked, msg)}").classes("q-mt-xs")
            with ui.row().classes("items-center gap-1 dk-muted text-caption q-mt-xs"):
                if msg.get("ts"):
                    ui.label(f"{msg['ts']} ·")
                shown = (msg.get("status") or {}).get("badge")
                if shown:
                    badge(shown["label"], shown["tone"], shown["icon"])
            key = _run_key(msg)
            button = ui.button("Viewing" if is_selected else "Open", on_click=lambda k=key: _open(k)) \
                .props(f'flat no-caps dense aria-label="{"Viewing" if is_selected else "Open"}: '
                       f'{_request_text(asked, msg)[:80].replace(chr(34), "")}"').classes("w-full q-mt-xs")
            button.set_enabled(not is_selected)

    async def _open(key: str) -> None:
        state["selected"] = key
        rail.refresh()
        await viewer.refresh()

    def _clear() -> None:
        with ui.dialog() as dialog, ui.card().classes("dk-card").style("max-width: 520px"):
            ui.label("Clear analysis history").classes("text-h6").props('role="heading" aria-level="2"')
            with ui.element("div").classes("dk-callout dk-tone-warning w-full"):
                ui.label("Delete the complete history including all unsaved what-ifs? This cannot be undone.") \
                    .style("color: var(--dk-text)")

            async def confirm() -> None:
                await api.request("DELETE", "/steering/chat")
                dialog.close()
                state.update(selected=None, reports={})
                await reload_chat()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("outline no-caps")
                ui.button("Yes, clear history", on_click=confirm).props("unelevated color=primary no-caps")
        dialog.open()

    # ------------------------------------------------------------------ viewer
    @ui.refreshable
    async def viewer() -> None:
        if state["busy"]:
            with ui.element("div").classes("dk-card w-full").props('role="status" aria-live="polite" aria-busy="true"'):
                with ui.row().classes("items-center gap-3"):
                    ui.spinner(size="lg")
                    ui.label("Analysing the what-if ...").classes("text-subtitle1 text-weight-medium")
                ui.label("Checking data quality (deterministic rules) ...").classes("dk-muted q-mt-sm")
                ui.label("Calculating the change · checking dependencies · writing the decision paper ...") \
                    .classes("dk-muted")
                ui.label("The verdict comes first in the report; the decision paper follows below it.") \
                    .classes("dk-muted text-caption q-mt-sm")
            return
        if state["error"]:
            error_box(f"Analysis failed – {state['error']}")
        current = selected()
        if current is None:
            with ui.element("div").classes("dk-card w-full"):
                ui.markdown("**No analyses yet.** Use the composer above or start with an example:")
                for example in catalog["examples"]:
                    ui.button(example, icon="arrow_forward", on_click=lambda e=example: run({"request": e})) \
                        .props("flat no-caps align=left").classes("w-full")
            return
        _, msg = current
        meta = msg.get("meta") or {}
        if not meta.get("mode") or not meta.get("run_id"):  # e.g. a system-error message with an incident ID
            with ui.element("div").classes("dk-card w-full"):
                ui.markdown(str(msg.get("content") or ""))
            return
        key = meta["run_id"]
        if key not in state["reports"]:
            try:
                state["reports"][key] = await api.get(f"/steering/runs/{key}/report")
            except api.ApiError as exc:
                error_box(exc.message)
                return
        report = state["reports"][key]
        card = report_viewer.render(report, actions=lambda: actions(report), key="console")
        await report_viewer.ask_about(card, run_id=key)

    with ui.element("div").classes("dk-console"):
        with ui.column().classes("dk-rail w-full"):
            rail()
        with ui.column().classes("w-full gap-4"):
            await builder.render()
            await viewer()

