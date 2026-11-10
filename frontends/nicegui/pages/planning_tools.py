"""The PMO's maintenance work on demands in one Planning section: assessment (written directly), scoring weights and
the conversion of approved demands into projects (both proposals a second PMO approves)."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import notify
from frontends.nicegui.pages import planning_parts as parts

TOOLS = {"assessment": "Assessment", "weights": "Scoring weights", "conversion": "Projects from demands"}


async def _assessment_form(demand: str, box: ui.element) -> None:
    form = await api.get(f"/planning/demands/{demand}/assessment")
    box.clear()
    with box, ui.element("div").classes("dk-card w-full").style("height:auto"):
        fields = []
        for crit in form["criteria"]:
            with ui.element("div").classes("dk-grid-2 w-full items-start q-mb-sm"):
                score = ui.number(f"{crit['criterion']} ({crit['direction']})", value=crit["value"],
                                  min=crit["scale_min"], max=crit["scale_max"], step=1).props("outlined")
                score.tooltip(f"Weight {parts.number(crit['weight'])}")
                comment = ui.input("Comment", placeholder="Comment (optional)").props("outlined")
            fields.append((crit["criterion"], score, comment))

        async def save() -> None:
            body = {"scores": {name: float(score.value if score.value is not None else 0) for name, score, _ in fields},
                    "comments": {name: comment.value for name, _, comment in fields if (comment.value or "").strip()}}
            try:
                answer = await api.post(f"/planning/demands/{demand}/assessment", body)
            except api.ApiError as exc:
                notify("error", exc.message)
                return
            notify("success", answer["message"])

        ui.button("Save assessment", icon="save", on_click=save).props("unelevated color=primary")
    with box:
        parts.caption("Each save appends new score rows; the latest assessment per criterion counts (history stays).")


async def _assessment() -> None:
    model = (await api.get("/planning/criteria"))["criteria"]
    if not model:
        parts.callout("No scoring model in the data (sheet Scoring_Model).")
        return
    candidates = (await api.get("/planning/assessment"))["demands"]
    if not candidates:
        parts.caption("No demand to assess.")
        return
    labels = {d["demand_number"]: f"{d['demand_number']} · {d.get('demand_name')}" for d in candidates}
    box = ui.column().classes("w-full")
    pick = ui.select(labels, value=next(iter(labels)), label="Demand",
                     on_change=lambda e: _assessment_form(e.value, box)).props("outlined").classes("w-full")
    box.move(target_index=-1)
    await _assessment_form(pick.value, box)


async def _weights() -> None:
    model = (await api.get("/planning/criteria"))["criteria"]
    parts.caption("A weight change is a proposal: a second PMO approves it, the write journal records it.")
    if not model:
        parts.callout("No scoring model in the data (sheet Scoring_Model).")
        return
    inputs = {}
    with ui.element("div").classes("dk-table-wrap w-full"):
        with ui.element("table").classes("dk-table"):
            with ui.element("thead"), ui.element("tr"):
                for title in ("Criterion", "Weight", "Lowest score", "Highest score", "Direction"):
                    with ui.element("th").props('scope="col"'):
                        ui.label(title)
            with ui.element("tbody"):
                for crit in model:
                    with ui.element("tr"):
                        with ui.element("td"):
                            ui.label(crit["criterion"])
                        with ui.element("td"):
                            inputs[crit["criterion"]] = ui.number(value=crit["weight"], min=0, step=1, precision=0) \
                                .props(f'dense outlined aria-label="Weight of {crit["criterion"]}"') \
                                .style("min-width: 90px")
                        for key in ("scale_min", "scale_max"):
                            with ui.element("td").classes("dk-num"):
                                ui.label(parts.number(crit[key]))
                        with ui.element("td"):
                            ui.label(crit["direction"])

    async def submit() -> None:
        weights = {name: float(field.value if field.value is not None else 0) for name, field in inputs.items()}
        try:
            answer = await api.post("/planning/weights", {"weights": weights})
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        notify("success" if answer.get("proposal") else "info", answer["message"])

    ui.button("Submit weight change", icon="send", on_click=submit).props("outline")


async def _conversion() -> None:
    data = await api.get("/planning/conversions")
    if data["converted"]:
        parts.table([{"Demand": c["Demand"], "Name": c.get("Name"), "Project": c.get("Project")}
                     for c in data["converted"]], caption="Converted demands")
    if not data["approved"]:
        parts.caption("No approved demand waits for its conversion into a project. Demands become convertible once the "
                      "Portfolio Board approves a plan that contains them.")
        return
    labels = {d["demand_number"]: f"{d['demand_number']} · {d.get('demand_name')}" for d in data["approved"]}

    async def convert() -> None:
        try:
            answer = await api.post("/planning/conversions", {"demand": pick.value})
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        notify("success", answer["message"])

    with ui.row().classes("w-full items-end gap-2"):
        pick = ui.select(labels, value=next(iter(labels)), label="Approved demand").props("outlined") \
            .classes("col-grow").style("min-width: 240px")
        ui.button("Convert into a project", icon="rocket_launch", on_click=convert).props("outline") \
            .tooltip("Master data 1:1, quarterly money → cost plans, capacity → resource plans, first baseline. "
                     "A second PMO approves.")


async def render() -> None:
    box = ui.column().classes("dk-stack")

    async def show(tool: str) -> None:
        box.clear()
        with box:
            await {"assessment": _assessment, "weights": _weights, "conversion": _conversion}[tool]()

    ui.toggle(TOOLS, value="assessment", on_change=lambda e: show(e.value or "assessment")) \
        .props('no-caps unelevated toggle-color=primary aria-label="PMO tool"')
    box.move(target_index=-1)
    await show("assessment")
