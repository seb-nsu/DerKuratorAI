"""Planning sections around plan scenarios: the PMO's scenario workshop (inputs, up to three calculated scenarios side
by side, save as draft), the saved plan scenarios with the submission to the Portfolio Board, and the reports with the
board pack. The calculated scenarios are client state (this browser session), as on the React page; every figure
is calculated by the API."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from nicegui import app, ui

from frontends.nicegui import api
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import notify, plotly
from frontends.nicegui.pages import planning_parts as parts

BASE_PLAN = "Base plan (today's data)"


def _state() -> dict:
    """This user's workshop state in the browser session: the calculated scenarios ([{name, body, result}], oldest
    first), their comparison, the scenario shown and the form. Another identity in the same browser starts empty."""
    user = (app.storage.user.get("user") or {}).get("user_id")
    state = app.storage.user.get("plan_workshop")
    if not isinstance(state, dict) or state.get("user") != user:
        state = {"user": user, "scenarios": [], "comparison": [], "show": None, "form": {}}
        app.storage.user["plan_workshop"] = state
    return state


def _update(**values) -> None:
    app.storage.user["plan_workshop"] = {**_state(), **values}


def take_prefill() -> bool:
    """A planning question of the question box hands its scenario inputs over once: they become the form."""
    prefill = app.storage.user.pop("planning_prefill", None)
    if not isinstance(prefill, dict):
        return False
    _update(form={**(_state().get("form") or {}), **{k: v for k, v in prefill.items() if v is not None}})
    return True


def calculated() -> list[dict]:
    return list(_state()["scenarios"])


def scope_label(option: str) -> str:
    return "Whole portfolio" if not option else option.replace("_", " ").replace(":", ": ")


def _form_defaults(options: dict) -> dict:
    return {"name": "Base plan", "year_from": options["year_from"]["default"],
            "year_to": options["year_to"]["default"], "budget_factor_pct": 0, "scope": "", "method": "rank",
            "forced_in": [], "forced_reason": "", "excluded": [], "later": [], "earlier": []}


def _form(options: dict) -> dict:
    """The inputs of a plan scenario, kept for this browser session (choices no longer offered fall back)."""
    stored = {**_form_defaults(options), **(_state().get("form") or {})}
    known = {c["demand"] for c in options["candidates"]}
    for key in ("forced_in", "excluded", "later", "earlier"):
        stored[key] = [d for d in stored[key] if d in known]
    for key in ("year_from", "year_to"):
        bounds = options[key]
        if not isinstance(stored[key], int) or not bounds["min"] <= stored[key] <= bounds["max"]:
            stored[key] = bounds["default"]
    if stored["scope"] not in options["scopes"]:
        stored["scope"] = ""
    if stored["method"] not in options["methods"]:
        stored["method"] = "rank"
    return stored


def _inputs(options: dict) -> dict:
    """Draws the scenario inputs and returns the live form (a dict updated on every change)."""
    form = _form(options)
    labels = {c["demand"]: c["label"] for c in options["candidates"]}

    def keep() -> None:
        _update(form=dict(form))

    def setter(key: str, cast=lambda v: v):
        def apply(e) -> None:
            form[key] = cast(e.value) if e.value is not None else form[key]
            keep()
        return apply

    with ui.element("div").classes("dk-grid w-full"):
        ui.input("Scenario name", value=form["name"], on_change=setter("name", str)).props("outlined")
        ui.number("From year", value=form["year_from"], min=options["year_from"]["min"],
                  max=options["year_from"]["max"], step=1, precision=0,
                  on_change=setter("year_from", int)).props("outlined")
        ui.number("To year", value=form["year_to"], min=options["year_to"]["min"], max=options["year_to"]["max"],
                  step=1, precision=0, on_change=setter("year_to", int)).props("outlined")
        factor = options["budget_factor_pct"]
        with ui.column().classes("gap-0"):
            shown = ui.label("").classes("dk-muted")
            slider = ui.slider(min=factor["min"], max=factor["max"], step=factor["step"],
                               value=form["budget_factor_pct"], on_change=setter("budget_factor_pct", int)) \
                .props('markers aria-label="Budget envelopes"')
            shown.bind_text_from(slider, "value", backward=lambda v: f"Budget envelopes: {int(v):+d} %")
            ui.tooltip("Every envelope larger or smaller by this share (e.g. a budget cut).")
    with ui.element("div").classes("dk-grid-2 w-full"):
        ui.select({o: scope_label(o) for o in options["scopes"]}, value=form["scope"], label="Scope",
                  on_change=setter("scope")).props("outlined")
        with ui.column().classes("gap-0"):
            ui.label("Filling").classes("dk-muted")
            ui.radio(options["methods"], value=form["method"], on_change=setter("method")).props("inline") \
                .tooltip("Rank order: one demand after the other, as long as it fits. Value-optimal: the combination "
                         "of demands with the highest score sum within the same limits (mandatory demands first).")
    with ui.expansion("PMO decisions: force in, exclude, move", icon="tune").classes("w-full dk-card") \
            .style("height:auto; padding:0 8px"):
        def multi(label: str, key: str) -> ui.select:
            return ui.select(labels, multiple=True, label=label, value=form[key]) \
                .props("outlined use-chips clearable").classes("w-full")

        forced = multi("Force in (with a reason)", "forced_in")
        ui.input("Reason for forcing in", value=form["forced_reason"], on_change=setter("forced_reason", str),
                 placeholder="e.g. board commitment, regulatory deadline").props("outlined").classes("w-full")
        excluded = multi("Exclude", "excluded")
        later = multi("Move one year later", "later")
        earlier = multi("Move one year earlier", "earlier")

        def narrow() -> None:
            """A demand is forced in, excluded or moved – the lists offer only what is still possible."""
            for select, key, blocked in ((forced, "forced_in", set()),
                                         (excluded, "excluded", set(forced.value or [])),
                                         (later, "later", set(excluded.value or [])),
                                         (earlier, "earlier", set(excluded.value or []) | set(later.value or []))):
                allowed = {d: text for d, text in labels.items() if d not in blocked}
                value = [d for d in select.value or [] if d in allowed]
                select.set_options(allowed, value=value)
                form[key] = value
            keep()

        for select in (forced, excluded, later, earlier):
            select.on_value_change(narrow)
        narrow()
    return form


def _body(form: dict) -> dict:
    return {key: form[key] for key in ("name", "year_from", "year_to", "budget_factor_pct", "scope", "method",
                                        "forced_reason")} | {
        "forced_in": list(form["forced_in"]), "excluded": list(form["excluded"]), "later": list(form["later"]),
        "earlier": list(form["earlier"])}


def _items(result: dict) -> None:
    if not result["items"]:
        parts.caption("No demand in this scenario.")
        return
    parts.decision_badges(result["decision_counts"])
    parts.table(result["items"], formats={"Score": "dec1"}, scroll=True, caption="Decision per demand")


def _budget(result: dict) -> None:
    if not result["budget"]:
        parts.callout("No budget envelopes for these years (sheet Budget_Envelopes).")
        return
    parts.table(result["budget"], scroll=True, caption="Budget envelopes")
    parts.caption("Limit = cap minus the reserve. Committed = running projects (cost plans, rest of the forecast to "
                  "complete) and approved demands; Planned = the demands of this scenario incl. run cost after go-live.")


def _capacity(result: dict) -> None:
    if not result.get("capacity_chart"):
        parts.callout("No capacity limits for these years (sheet Capacity_Limits).")
        return
    plotly(result["capacity_chart"])
    parts.caption("Share of the usable FTE (available × maximum utilisation) per team and quarter, running projects "
                  "included. Darker = fuller; above 100 % the team is overbooked.")


def _optimizer(result: dict) -> None:
    optimizer = result.get("optimizer") or {}
    parts.table(optimizer.get("figures") or [], caption="Rank order and value-optimal")
    with ui.element("ul").classes("dk-list"):
        for line in optimizer.get("summary") or []:
            with ui.element("li"):
                ui.label(line)
    if optimizer.get("changes"):
        parts.table(optimizer["changes"], caption="What the value-optimal filling changes")
    if not result.get("solved", True):
        parts.callout("The optimizer found no solution in time – the rank order was used.", "warning", "warning")
    parts.caption("Same limits, same bound money and capacity, same verdict rules – only the choice of the free demands "
                  "differs. The reason of a deferral or an Out names the limit it would break.")


async def render_workshop(ctx: dict) -> None:
    options = await api.get("/planning/scenario-options")
    form = _inputs(options)
    results = ui.column().classes("dk-stack")

    async def save(name: str) -> None:
        entry = next((s for s in calculated() if s["name"] == name), None)
        if entry is None:
            return
        try:
            answer = await api.post("/planning/scenarios", entry["body"])
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        notify("success", answer["message"])

    async def draw() -> None:
        results.clear()
        scenarios = calculated()
        with results:
            if not scenarios:
                parts.caption("Calculate a plan scenario: running projects bind their money and capacity first, then "
                              "the demands fill the envelopes in rank order – or value-optimal, as a second proposal. "
                              "Up to three scenarios stay side by side for comparison.")
                return
            names = [s["name"] for s in scenarios]
            if len(scenarios) > 1 and _state()["comparison"]:
                parts.subheading("Comparison", level=3)
                parts.table(_state()["comparison"], formats={"Verdict": "verdict"},
                            caption="Comparison of the calculated scenarios")
            show = _state()["show"]
            show = show if show in names else names[-1]
            if len(scenarios) > 1:
                with ui.column().classes("gap-0"):
                    ui.label("Show scenario").classes("dk-muted")
                    ui.radio(names, value=show, on_change=lambda e: shown(e.value)).props("inline")
            result = next(s for s in scenarios if s["name"] == show)["result"]
            parts.scenario_verdict(result)
            ui.button("Save as draft", icon="save", on_click=lambda: save(show)).props("outline") \
                .tooltip("Stores the input and the decision per demand; the approval follows separately "
                         "(Saved plan scenarios › Submit to the Portfolio Board).")
            names_tabs = ["Demands", "Budget", "Capacity"] + (["Optimizer vs. rank order"]
                                                               if result.get("optimizer") else [])
            with ui.tabs().props("dense no-caps align=left active-color=primary indicator-color=primary "
                                 "outside-arrows mobile-arrows").classes("w-full") as tabs:
                tab = {n: ui.tab(n) for n in names_tabs}
            with ui.tab_panels(tabs, value=tab["Demands"]).classes("w-full dk-panels").props("animated=false"):
                for title, draw_tab in (("Demands", _items), ("Budget", _budget), ("Capacity", _capacity),
                                        ("Optimizer vs. rank order", _optimizer)):
                    if title in tab:
                        with ui.tab_panel(tab[title]).classes("dk-tabpanel"):
                            draw_tab(result)
            await ask_box("scenario", scenarios=[s["body"] for s in scenarios], scenario=show,
                          title="Ask about this plan scenario")

    async def shown(name: str) -> None:
        _update(show=name)
        await draw()

    async def calculate() -> None:
        body = _body(form)
        button.props("loading")
        try:
            result = await api.post("/planning/scenarios/calculate", body)
            scenarios = [s for s in calculated() if s["name"] != result["name"]]
            scenarios.append({"name": result["name"], "body": body, "result": result})
            scenarios = scenarios[-options["max_compared"]:]
            comparison = (await api.post("/planning/scenarios/compare", {"scenarios": [s["body"] for s in scenarios]})
                          )["comparison"] if len(scenarios) > 1 else []
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        finally:
            button.props(remove="loading")
        _update(scenarios=scenarios, comparison=comparison, show=result["name"])
        await draw()

    button = ui.button("Calculate plan scenario", icon="calculate", on_click=calculate) \
        .props("unelevated color=primary")
    results.move(target_index=-1)
    await draw()


async def render_saved(redraw: Callable[[], Awaitable[None]]) -> None:
    saved = (await api.get("/planning/scenarios"))["scenarios"]
    if not saved:
        ui.markdown("No saved plan scenario yet. Calculate one under **Plan scenarios** and save it as a draft; it then "
                    "appears here for the submission to the Portfolio Board.").classes("dk-caption")
        return
    columns = ["scenario_id", "scenario_name", "year_from", "year_to", "scenario_status", "verdict", "created_by",
               "created_on"]
    parts.table(saved, columns=columns, formats={"verdict": "verdict"},
                labels={c: c.replace("_", " ").capitalize() for c in columns}, caption="Saved plan scenarios")
    drafts = {s["scenario_id"]: f"{s['scenario_id']} · {s.get('scenario_name')}" for s in saved if s["submittable"]}
    if not drafts:
        return

    async def submit() -> None:
        try:
            answer = await api.post(f"/planning/scenarios/{pick.value}/submit")
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        notify("success", answer["message"])
        await redraw()

    with ui.row().classes("w-full items-end gap-2"):
        pick = ui.select(drafts, value=next(iter(drafts)), label="Draft").props("outlined").classes("col-grow") \
            .style("min-width: 240px")
        ui.button("Submit to the Portfolio Board", icon="send", on_click=submit).props("outline") \
            .tooltip("Recalculated on today's data; the Portfolio Board approves it (four-eyes).")


async def render_reports() -> None:
    listed = (await api.get("/planning/reports"))["options"]
    scenarios = calculated()
    own = {f"Calculated: {s['name']}": s["body"] for s in scenarios}
    choices = [o for o in listed if o != BASE_PLAN] + [o for o in own if o not in listed] + \
              ([BASE_PLAN] if BASE_PLAN in listed else [])
    compared = [s["body"] for s in scenarios]

    def request(kind: str) -> dict:
        option = pick.value
        body: dict = {"scenario": own[option]} if option in own and option not in listed else {"option": option}
        if kind == "xlsx" and compared:
            body["compared"] = compared
        return {**body, "format": kind}

    async def download(kind: str) -> None:
        try:  # the API names the file (board_pack_<reference date>)
            await parts.download("/planning/reports", request(kind), f"board_pack.{kind}")
        except api.ApiError as exc:
            notify("error", exc.message)

    async def show() -> None:
        multi.clear()
        with multi:
            try:
                report = await api.post("/planning/reports", request("json"))
            except api.ApiError as exc:
                parts.callout(exc.message, "danger", "error")
                return
            rows = report["multi_year"]
            if rows:
                parts.table(rows, formats={c: "eur" for c in rows[0] if c != "Business unit"},
                            caption="Multi-year view")

    pick = ui.select(choices, value=choices[0], label="Plan scenario", on_change=show).props("outlined") \
        .classes("w-full")
    with ui.element("div").classes("dk-grid-2 w-full"):
        ui.button("Board pack (Excel)", icon="table_view", on_click=lambda: download("xlsx")).props("outline")
        ui.button("Board pack (print → PDF)", icon="print", on_click=lambda: download("html")).props("outline")
    parts.caption("Excel: summary, ranking, decision per demand, budget, capacity, multi-year view, the comparison of "
                  "the calculated scenarios and, for a value-optimal plan, the optimizer sheet. The HTML page prints to "
                  "PDF in any browser.")
    multi = ui.column().classes("w-full")
    await show()
