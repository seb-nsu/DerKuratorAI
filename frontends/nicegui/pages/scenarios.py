"""What-if comparison: A/B comparison of saved what-ifs, the budget impact side by side, the differences and the list
with deleting (owner or PMO). A and B are in the address (?a=…&b=…), so a comparison can be linked."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import notify, page_header, plotly
from frontends.nicegui.pages import planning_parts as parts

SUMMARY = (("Verdict", "verdict"), ("Net budget impact", "net_budget_impact"), ("Budget released", "budget_released"),
           ("Budget added", "budget_added"), ("Projects stopped", "projects_stopped"),
           ("Projects on hold", "projects_on_hold"), ("FTE moved", "fte_moved"), ("Changes", "changes"),
           ("Steps", "steps"))


def _stamp(value: str | None) -> str:
    return str(value or "").replace("T", " ")[:16] or "–"


def _label(s: dict) -> str:
    return f"{s['name']} · {s.get('owner')} · {_stamp(s.get('timestamp'))}"


def _metric(key: str, figures: dict) -> str:
    value = figures.get(key)
    if key == "net_budget_impact":
        return parts.eur(value, signed=True)
    if key in ("budget_released", "budget_added"):
        return parts.eur(value)
    if key == "verdict":
        return str(value or "–")
    return parts.number(value)


def _summary_rows(a: dict, b: dict) -> list[dict]:
    return [{"Metric": title, f"A – {a['name']}": _metric(key, a["figures"]), f"B – {b['name']}": _metric(key, b["figures"])}
            for title, key in SUMMARY]


def _side(tag: str, s: dict) -> None:
    with ui.element("div").classes("dk-card"):
        ui.label(f"{tag}: {s['name']}").classes("text-weight-bold")
        parts.verdict_badge(s.get("verdict") or "–")
        if s.get("waterfall_figure"):
            plotly(s["waterfall_figure"])
        else:
            parts.caption("No budget effect (e.g. a schedule or capacity change).")
        for condition in s.get("conditions") or []:
            parts.caption(f"Condition: {condition}")
        if s.get("turns"):
            parts.caption("Steps: " + " → ".join(s["turns"]))


async def _saved_list(saved: list[dict], refresh) -> None:
    parts.table([{"What-if": s["name"], "Owner": s.get("owner"), "Saved": _stamp(s.get("timestamp")),
                  "Verdict": s.get("verdict"), "Changes": s["figures"]["changes"], "Steps": s["figures"]["steps"]}
                 for s in saved], formats={"Verdict": "verdict"}, caption="Saved what-ifs")
    own = [s for s in saved if s.get("can_delete")]
    if not own:
        return
    labels = {s["id"]: _label(s) for s in own}

    async def delete(scenario_id: str) -> None:
        try:
            await api.request("DELETE", f"/whatifs/{scenario_id}")
        except api.ApiError as exc:
            notify("error", exc.message)
            return
        notify("success", "Saved what-if deleted.")
        await refresh()

    async def confirm() -> None:
        dialog.close()
        await delete(pick.value)

    with ui.dialog() as dialog, ui.card().classes("q-pa-md").style("max-width: 520px"):
        ui.label("Delete saved what-if").classes("text-h6").props('role="heading" aria-level="2"')
        question = ui.label("").classes("dk-callout dk-tone-warning w-full")
        with ui.row().classes("w-full no-wrap gap-2 q-mt-md"):
            ui.button("Yes, delete", on_click=confirm).props("unelevated color=primary").classes("col")
            ui.button("Cancel", on_click=dialog.close).props("outline").classes("col")

    def ask() -> None:
        question.set_text(f"Delete the saved what-if “{labels[pick.value]}”? This cannot be undone.")
        dialog.open()

    with ui.row().classes("w-full items-end gap-2 no-wrap"):
        pick = ui.select({"": "—", **labels}, value="", label="Delete a saved what-if").props("outlined") \
            .classes("col")
        button = ui.button("Delete scenario", icon="delete", on_click=ask).props("outline")
    button.bind_enabled_from(pick, "value", backward=bool)


async def render(info: dict, query: dict) -> None:
    parts.install()
    root = ui.column().classes("dk-stack")

    async def draw() -> None:
        root.clear()
        with root:
            await _draw(refresh=draw)

    await draw()


async def _draw(refresh) -> None:
    listing = await api.get("/whatifs")
    saved = listing["whatifs"]
    page_header("What-if comparison", f"A/B comparison · {len(saved)} saved what-if(s) in your scope",
                link=("console", "arrow_back", "Back to the Steering Console"))
    if not saved:
        parts.callout("No saved what-ifs yet. Save a report in the Steering Console with 'Save as what-if'.")
        return
    if len(saved) < 2:
        parts.caption(f"1 saved what-if ('{saved[0]['name']}'). Save a second one to compare them side by side.")
        await _saved_list(saved, refresh)
        return
    ids = [s["id"] for s in saved]
    labels = {s["id"]: _label(s) for s in saved}
    pair = listing["default_pair"] or {"a": ids[1], "b": ids[0]}
    a_id = parts.query_param("a") if parts.query_param("a") in ids else pair["a"]
    b_id = parts.query_param("b") if parts.query_param("b") in ids else pair["b"]
    body = ui.column().classes("dk-stack")

    async def compare() -> None:
        parts.set_query(a=pick_a.value, b=pick_b.value)
        body.clear()
        with body:
            if pick_a.value == pick_b.value:
                parts.callout("Choose two different what-ifs.")
                await _saved_list(saved, refresh)
                return
            try:
                result = await api.get("/whatifs/compare", a=pick_a.value, b=pick_b.value)
            except api.ApiError as exc:
                parts.callout(exc.message, "danger", "error")
                return
            a, b = result["a"], result["b"]
            parts.table(_summary_rows(a, b), caption="Key figures A and B")
            with ui.element("div").classes("dk-grid-2 w-full"):
                _side("A", a)
                _side("B", b)
            with ui.tabs().props("dense no-caps align=left active-color=primary indicator-color=primary") \
                    .classes("w-full") as tabs:
                t_bars = ui.tab("Budget impact A vs B")
                t_diff = ui.tab("Change-by-change differences")
                t_saved = ui.tab("Saved what-ifs")
            with ui.tab_panels(tabs, value=t_bars).classes("w-full dk-panels").props("animated=false"):
                with ui.tab_panel(t_bars).classes("dk-tabpanel"):
                    if result.get("budget_impact_figure"):
                        plotly(result["budget_impact_figure"])
                    else:
                        parts.caption("Neither what-if changes a budget.")
                with ui.tab_panel(t_diff).classes("dk-tabpanel"):
                    parts.table([{"Project": d["project"], "Change": d["field_label"], f"A – {a['name']}": d["A"],
                                  f"B – {b['name']}": d["B"], "Differs": d["differs"]}
                                 for d in result["differences"]], caption="Change-by-change differences")
                with ui.tab_panel(t_saved).classes("dk-tabpanel"):
                    await _saved_list(saved, refresh)
            await ask_box("compare", a=pick_a.value, b=pick_b.value, title="Ask about A vs B")

    with ui.element("div").classes("dk-grid-2 w-full"):
        pick_a = ui.select(labels, value=a_id, label="Scenario A (older)", on_change=compare).props("outlined")
        pick_b = ui.select(labels, value=b_id, label="Scenario B (newer)", on_change=compare).props("outlined")
    body.move(target_index=-1)
    await compare()
