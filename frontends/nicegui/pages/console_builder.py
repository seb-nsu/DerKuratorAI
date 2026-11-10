"""The console's composer: Guided / Free text, "Builds on the previous step" (scenario memory), Options & rules (the
PMO's forced simulation while the Data Steward blocks a project) and the guided scenario form – its inputs, bounds,
options and hints come from GET /steering/guided/{action}; the server checks them again on the run."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from nicegui import ui

from frontends.nicegui import api

RADIO_PARAMS = {"cost_type", "direction", "is_perc", "method", "funding"}  # shown as radio buttons, as in Streamlit
CALLOUT_NOTES = ("The Data Steward blocks", "This project is blocked", "This project is not on hold")
NO_TRANSFER = "—"


class Builder:
    """State and drawing of the composer. `run(body)` starts a what-if with a request body for POST /steering/runs
    (the console adds base_run_id and force)."""

    def __init__(self, catalog: dict, chat: dict, run: Callable[[dict], Awaitable[None]], *, action: str | None = None,
                 project: str | None = None, request: str | None = None):
        self.catalog, self.chat, self.run = catalog, chat, run
        self.scenarios = {s["action"]: s for s in catalog["scenarios"]}
        self.projects = {p["project_number"]: p for p in catalog["projects"]}
        self.mode = "Free text" if request else "Guided"
        self.action = action if action in self.scenarios else \
            catalog["scenarios"][0]["action"] if catalog["scenarios"] else None
        self.project = project if project in self.projects else next(iter(self.projects), None)
        self.target: str | None = None
        self.params: dict = {}
        self.form: dict | None = None
        self.form_key: tuple | None = None
        self.force = False
        self.build_on: bool = bool(chat["continuation"].get("default_continue"))
        self.continue_step = chat["continuation"].get("step")
        self.busy = False
        self.text = request or ""  # a request handed over by the question box
        self._buttons: list = []

    # ------------------------------------------------------------------ helpers
    def label(self, pid: str) -> str:
        p = self.projects.get(pid)
        return f"{pid} – {p['project_name']}{' (blocked)' if p['blocked'] else ''}" if p else pid

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for button in self._buttons:
            button.set_enabled(not busy and button.dk_ready)

    async def load_form(self) -> None:
        info = self.scenarios[self.action]
        project = self.project if info["project"] != "none" else None
        key = (self.action, project)
        try:
            form = await api.get(f"/steering/guided/{self.action}", project=project, force=str(self.force).lower())
        except api.ApiError as exc:
            self.form = {"params": [], "notes": [exc.message]}
            return
        if key != self.form_key:  # a new scenario or project: the builder's defaults
            self.params = {p["name"]: p["default"] for p in form["params"]}
            others = [pid for pid in self.projects if pid != project]
            self.target = others[0] if info["target"] == "required" and others else None
        self.form, self.form_key = form, key

    # ------------------------------------------------------------------ drawing
    async def render(self) -> None:
        await self.load_form()
        with ui.element("div").classes("dk-card w-full"):
            with ui.row().classes("w-full items-start gap-4"):
                ui.toggle(["Guided", "Free text"], value=self.mode, on_change=self._mode) \
                    .props('no-caps unelevated toggle-color=primary aria-label="Input"')
                with ui.column().classes("gap-0").style("flex: 2 1 260px"):
                    self.continuation()
                with ui.column().style("flex: 1 1 200px"):
                    self._rules()
            self.force_note()
            with ui.column().classes("w-full q-mt-md gap-2"):
                self.body()

    def _mode(self, event) -> None:
        self.mode = event.value
        self.body.refresh()

    @ui.refreshable
    def continuation(self) -> None:
        cont = self.chat_continuation()
        if not cont.get("available"):
            ui.label("New what-if · nothing to build on yet").classes("dk-muted text-caption")
            return
        if cont.get("step") != self.continue_step:  # a new result -> fresh default for the toggle
            self.continue_step, self.build_on = cont.get("step"), bool(cont.get("default_continue"))
        turns = " → ".join(cont.get("turns") or [])
        switch = ui.switch(f"Builds on the previous step (step {cont['step']})", value=self.build_on,
                           on_change=self._build_on)
        switch.tooltip("The next request builds on the already changed data of this what-if so far: " + turns)
        if self.build_on:
            ui.label(f"Building on: {turns}").classes("dk-muted text-caption")
        elif cont.get("previous_verdict_blocks"):
            ui.label(f"Last step was {cont['previous_verdict_blocks']} – starting from the original data.") \
                .classes("dk-muted text-caption")
        else:
            ui.label("Starting from the original data.").classes("dk-muted text-caption")

    def chat_continuation(self) -> dict:
        return self.chat["continuation"]

    def _build_on(self, event) -> None:
        self.build_on = bool(event.value)
        self.continuation.refresh()

    def _rules(self) -> None:
        can_force = self.catalog["can_force"]
        with ui.button("Options & rules" if can_force else "Rules in use", icon="rule") \
                .props("outline no-caps").classes("w-full"):
            with ui.menu().props("max-width=620px"), ui.column().classes("q-pa-md gap-2"):
                if can_force:
                    ui.switch("Force simulation (ignore data warnings)", value=self.force, on_change=self._force) \
                        .tooltip("PMO only: the Data Steward still reports all findings, but blocked projects are "
                                 "simulated anyway (indicative results). Every forced run is audited.")
                    ui.separator()
                ui.markdown(self.catalog["rules_md"])
                ui.label("Set by the PMO Head on the Rules page; the GO / CONDITIONAL GO / "
                         "NO-GO verdict is computed from these rules – never by the AI.").classes("dk-muted text-caption")

    async def _force(self, event) -> None:
        self.force = bool(event.value)
        self.force_note.refresh()
        await self.load_form()
        self.body.refresh()

    @ui.refreshable
    def force_note(self) -> None:
        if self.force:
            with ui.element("div").classes("dk-callout dk-tone-warning w-full q-mt-sm").props('role="status"'):
                with ui.row().classes("items-center no-wrap gap-2"):
                    ui.icon("warning", size="20px").props('aria-hidden="true"')
                    ui.label("Force mode active – quality-gate blocks are ignored; results for blocked projects are "
                             "indicative only.").style("color: var(--dk-text)")

    @ui.refreshable
    def body(self) -> None:
        self._buttons = []
        if self.mode == "Free text":
            self._free_text()
        else:
            self._guided()

    def _free_text(self) -> None:
        disabled = not self.projects

        async def send() -> None:
            text = (box.value or "").strip()
            if text and not self.busy:
                box.value = ""
                await self.run({"request": text})

        with ui.row().classes("w-full items-end no-wrap gap-2"):
            box = ui.input("Describe a change or ask a what-if …", value=self.text).props("outlined maxlength=4000 clearable") \
                .classes("w-full")
            box.on("keydown.enter", send)
            button = ui.button(icon="send", on_click=send).props('unelevated color=primary aria-label="Send"')
        button.dk_ready = not disabled
        box.set_enabled(not disabled)
        button.set_enabled(not disabled and not self.busy)
        self._buttons.append(button)

    def _guided(self) -> None:
        info = self.scenarios.get(self.action)
        if info is None:
            return
        ids = list(self.projects)
        with ui.element("div").classes("dk-grid w-full"):
            ui.select({s["action"]: s["label"] for s in self.catalog["scenarios"]}, label="Scenario type",
                      value=self.action, on_change=self._action).props("outlined dense options-dense")
            if info["project"] == "none":
                ui.label(info.get("note") or "").classes("dk-muted text-caption")
            else:
                ui.select({pid: self.label(pid) for pid in ids}, label=info["project_label"], value=self.project,
                          on_change=self._project).props("outlined dense options-dense")
            if info["target"] != "none":
                others = [pid for pid in ids if pid != self.project]
                if info["target"] == "required":
                    if others:
                        ui.select({pid: self.label(pid) for pid in others}, label=info["target_label"],
                                  value=self.target if self.target in others else others[0],
                                  on_change=lambda e: setattr(self, "target", e.value)) \
                            .props("outlined dense options-dense")
                        self.target = self.target if self.target in others else others[0]
                else:
                    options = {NO_TRANSFER: "— no transfer —", **{pid: self.label(pid) for pid in others}}
                    ui.select(options, label=info["target_label"], value=self.target or NO_TRANSFER,
                              on_change=lambda e: setattr(self, "target", None if e.value == NO_TRANSFER else e.value)) \
                        .props("outlined dense options-dense")
        params = (self.form or {}).get("params") or []
        if params:
            with ui.element("div").classes("dk-grid w-full items-end"):
                for spec in params:
                    self._input(spec)
        for note in (self.form or {}).get("notes") or []:
            if info["project"] == "none" and note == info.get("note"):
                continue  # already shown next to the scenario type
            if note.startswith(CALLOUT_NOTES):
                with ui.element("div").classes("dk-callout dk-tone-info w-full"):
                    with ui.row().classes("items-start no-wrap gap-2"):
                        ui.icon("block" if note.startswith("This project is blocked") else "info", size="20px") \
                            .props('aria-hidden="true"')
                        ui.label(note).style("color: var(--dk-text)")
            else:
                ui.label(note).classes("dk-muted text-caption")
        button = ui.button("Simulate", icon="play_arrow", on_click=self._simulate) \
            .props("unelevated color=primary no-caps")
        button.dk_ready = bool(ids)
        button.set_enabled(bool(ids) and not self.busy)
        if not ids:
            button.tooltip("No project in your scope for this scenario type.")
        self._buttons.append(button)

    def _input(self, spec: dict) -> None:
        name, label, kind = spec["name"], spec["label"], spec["kind"]
        value = self.params.get(name, spec.get("default"))

        def store(event, n=name) -> None:
            self.params[n] = event.value

        if kind == "choice":
            options = {o["value"]: o["label"] for o in spec["options"] or []}
            if name in RADIO_PARAMS:
                with ui.column().classes("gap-0"):
                    ui.label(label).classes("dk-muted text-caption")
                    on_change = self._unit if name == "is_perc" else store
                    ui.radio(options, value=value, on_change=on_change).props(f'inline dense aria-label="{label}"')
            else:
                ui.select(options, label=label, value=value, on_change=store).props("outlined dense options-dense")
        elif kind == "integer" and spec.get("min") is not None and spec.get("max") is not None:
            with ui.column().classes("gap-0 w-full"):
                caption = ui.label(f"{label}: {value}").classes("dk-muted text-caption")
                if spec.get("help"):
                    caption.tooltip(spec["help"])

                def slide(event, n=name, c=caption, text=label) -> None:
                    self.params[n] = int(event.value)
                    c.set_text(f"{text}: {int(event.value)}")

                ui.slider(min=spec["min"], max=spec["max"], step=spec.get("step") or 1, value=value,
                          on_change=slide).props(f'label markers=false aria-label="{label}"')
        elif kind in ("number", "integer"):
            step = spec.get("step") or 1
            if name == "value" and spec.get("unit_defaults"):
                step = spec["unit_defaults"]["%" if self.params.get("is_perc", True) else "€"]["step"]
            field = ui.number(label, value=value, min=spec.get("min"), max=spec.get("max"), step=step,
                              on_change=store).props("outlined dense")
            if spec.get("help"):
                field.tooltip(spec["help"])
        elif kind == "date":
            ui.input(label, value=value or "", on_change=lambda e, n=name: self.params.__setitem__(n, e.value or None)) \
                .props("outlined dense type=date stack-label clearable")
        else:
            ui.input(label, value=value or "", on_change=store) \
                .props(f"outlined dense maxlength={spec.get('max_length') or 500}")

    def _unit(self, event) -> None:
        """The value's default and step follow the unit (% or €), as in the Streamlit builder."""
        self.params["is_perc"] = event.value
        spec = next((p for p in (self.form or {}).get("params") or [] if p["name"] == "value"), None)
        if spec and spec.get("unit_defaults"):
            self.params["value"] = spec["unit_defaults"]["%" if event.value else "€"]["default"]
        self.body.refresh()

    async def _action(self, event) -> None:
        self.action = event.value
        await self.load_form()
        self.body.refresh()

    async def _project(self, event) -> None:
        self.project = event.value
        await self.load_form()
        self.body.refresh()

    async def _simulate(self) -> None:
        info = self.scenarios[self.action]
        params = {k: v for k, v in self.params.items() if v is not None}  # empty: the server's default
        body = {"guided": {"action": self.action, "project": self.project if info["project"] != "none" else None,
                           "target": self.target if info["target"] != "none" else None, "params": params}}
        await self.run(body)
