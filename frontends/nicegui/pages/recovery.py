"""Recovery (PMO, a sub-page of the overview): the red trigger's recommendations. Per recommendation: trigger, causes,
gap, impact, the options side by side (package verdict, gap closure, collateral, cost, Monte Carlo before/after), the
rejected measures and the actions Adopt / Dismiss / Recalculate; plus 'Analyse now' for a project that is red already.
Same sections as the React page; every figure comes from /api/v1/recovery. Deep link: /recovery?id=<recommendation>."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, notify, page_header, section
from frontends.nicegui.pages.overview import boundary, caption, info
from frontends.nicegui.pages.overview_format import eur_m
from frontends.nicegui.pages.planning_parts import install, table


def _pct(value) -> str:
    return "–" if value is None else f"{value:g} %"


def option_rows(rec: dict) -> list[dict]:
    rows = []
    for o in rec["options"]:
        r = o.get("result") or {}
        closure = r.get("closure") or {}
        cost = r.get("cost") or {}
        rows.append({
            "Option": f"{o['key']} {o['label']}" + (" ★" if o["key"] == rec.get("recommended") else ""),
            "Verdict": ((r.get("verdict") or {}).get("label") or r.get("decision") or "–")
            + (" ⚠ just below the board threshold" if o.get("threshold_flag") else ""),
            "Cost gap closed": _pct(closure.get("cost_pct")), "Schedule gap closed": _pct(closure.get("schedule_pct")),
            "Status after": r.get("calculated_after") or "–",
            "Collateral": ", ".join(r.get("collateral") or []) or "none",
            "Exit / hold cost": eur_m((cost.get("exit_cost_eur") or 0) + (cost.get("hold_cost_eur") or 0)),
            "Portfolio within budget": f"{_pct(r.get('probability_before_pct'))} → {_pct(r.get('probability_after_pct'))}",
            "Measures": "; ".join(f"{s['action']} {s['project']}" + (f" → {s['target']}" if s.get("target") else "")
                                  for s in o.get("steps") or []) or "none",
        })
    return rows


def detail(rec: dict, pmo: bool, reload) -> None:
    impact = rec.get("impact") or {}
    caption(f"Trigger: {rec.get('trigger_label')} · reported {rec.get('reported') or '–'}, calculated "
            f"{rec.get('calculated')} · as of {rec.get('as_of')} · status {rec['status']}")
    with card("Causes"):
        for c in rec.get("causes") or []:
            ui.label(c["text"])
        if rec.get("report_hint"):
            caption(f"Latest status report (free text, a hint only): {rec['report_hint']}")
    with card("Impact on the portfolio"):
        deps = impact.get("dependents") or []
        ui.label("Dependents: " + (", ".join(d["id"] + (" (red)" if d.get("red") else "") for d in deps) or "none"))
        if impact.get("upstream_red"):
            ui.label(f"Held back by red predecessor(s): {', '.join(impact['upstream_red'])}")
        for b in impact.get("deadline_breaches") or []:
            ui.label(f"{b['successor']} needs the result by {b['required_by']} – {b['days_late']} days late")
        budget = impact.get("budget") or {}
        if budget:
            ui.label(f"Cost gap {eur_m(budget.get('gap_eur'))} · reserve free above the floor "
                     f"{eur_m(budget.get('reserve_free_eur'))}")
        for t in impact.get("teams") or []:
            if t.get("red_neighbours"):
                caption(f"{t['team']} is shared with red project(s) {', '.join(t['red_neighbours'])}")
    section("Options (each calculated as one package)")
    table(option_rows(rec), scroll=True, caption="Options")
    explanation = rec.get("explanation") or {}
    if explanation.get("llm"):
        with card("Explanation (AI-written, figures checked)"):
            ui.markdown(explanation["llm"])
            for finding in explanation.get("findings") or []:
                caption(finding, icon="warning")
            for idea in explanation.get("ideas") or []:
                caption(idea, icon="lightbulb")
    if rec.get("rejected"):
        table([{"Measure": r["label"], "Why rejected": r["why"]} for r in rec["rejected"]], scroll=True,
              caption="Checked and rejected")
    if rec.get("not_possible"):
        table([{"Measure": n["label"], "Why not possible": n["reason"]} for n in rec["not_possible"]], scroll=True,
              caption="Not possible with the data")
    adoptable = [o["key"] for o in rec["options"] if o.get("steps") and (pmo or o.get("adoptable_by_pm"))]
    if rec["status"] in ("open", "adopted") and adoptable:
        with ui.row().classes("items-end gap-2 q-mt-sm"):
            choice = ui.select(adoptable, label="Option", value=rec.get("recommended") if rec.get("recommended")
                               in adoptable else adoptable[0]).props("outlined dense").classes("w-32")

            async def adopt() -> None:
                async with boundary():
                    result = await api.post(f"/recovery/recommendations/{rec['id']}/adopt", {"option": choice.value})
                    notify("success", f"Option {result['option']} is in the Steering Console – check it and submit "
                                      "the proposal there.")
                    ui.navigate.to("/console")

            ui.button("Adopt", icon="input", on_click=adopt).props("no-caps color=primary") \
                .tooltip("Runs the option as a what-if in your name; you submit it, a second PMO approves")
            if pmo and rec["status"] == "open":
                reason = ui.input("Reason for dismissing").props("outlined dense").classes("w-64")

                async def dismiss() -> None:
                    if not (reason.value or "").strip():
                        notify("warning", "Give the reason for dismissing.")
                        return
                    async with boundary():
                        await api.post(f"/recovery/recommendations/{rec['id']}/dismiss", {"reason": reason.value})
                        notify("success", "Recommendation dismissed.")
                        await reload()

                ui.button("Dismiss", icon="block", on_click=dismiss).props("outline no-caps")
    if pmo:
        async def recalc() -> None:
            async with boundary():
                fresh = await api.post("/recovery/analyse", {"project_id": rec["project_id"]})
                ui.navigate.to(f"/recovery?id={fresh['id']}")

        ui.button("Recalculate", icon="refresh", on_click=recalc).props("flat no-caps")


async def render(info_: dict, query: dict) -> None:
    install()
    pmo = str((info_.get("user") or {}).get("role", "")).startswith("PMO")
    page_header("Recovery", "Red trigger · calculated options, no automatic change",
                link=("overview", "arrow_back", "Back to the overview") if pmo
                else ("projects", "arrow_back", "Back to my projects"))
    container = ui.column().classes("w-full gap-2")

    async def reload() -> None:
        container.clear()
        with container:
            await body()

    async def body() -> None:
        async with boundary():
            answer = await api.get("/recovery/recommendations")
            if not answer["enabled"]:
                info("The red trigger is off (Rules › Red trigger). 'Analyse now' still works for a red project.")
            items = answer["recommendations"]
            if items:
                table([{"Project": i["project_id"], "Name": i["project_name"], "Status": i["status"],
                        "Trigger": i["trigger_label"], "Causes": ", ".join(i["causes"]), "Gap": eur_m(i["gap_eur"]),
                        "Options": i["options"], "Recommended": i.get("recommended") or "–", "Id": i["id"]}
                       for i in items], scroll=True, caption="Recommendations")
            else:
                info("No recommendation yet.")
            ids = [i["id"] for i in items]
            chosen = query.get("id") if query.get("id") in ids else (ids[0] if ids else None)
            if pmo:
                with ui.row().classes("items-end gap-2"):
                    project = ui.input("Project number for 'Analyse now'").props("outlined dense").classes("w-64")

                    async def analyse() -> None:
                        async with boundary():
                            rec = await api.post("/recovery/analyse", {"project_id": (project.value or "").strip()})
                            ui.navigate.to(f"/recovery?id={rec['id']}")

                    ui.button("Analyse now", icon="healing", on_click=analyse).props("outline no-caps")
            if chosen:
                select = ui.select({i["id"]: f"{i['project_id']} · {i['status']} · {i['created_at'][:16]}"
                                    for i in items}, label="Recommendation", value=chosen) \
                    .props("outlined dense").classes("w-96")
                select.on_value_change(lambda e: ui.navigate.to(f"/recovery?id={e.value}"))
                section(next(i["headline"] for i in items if i["id"] == chosen))
                detail(await api.get(f"/recovery/recommendations/{chosen}"), pmo, reload)

    with container:
        await body()


def alert_band(items: list[dict]) -> None:
    """The overview's alert band: one line per open recommendation with a link to it."""
    for i in items[:3]:
        with ui.row().classes("items-center gap-2 w-full dk-card").style("border-left: 4px solid #c62828"):
            ui.label(i["headline"]).classes("text-weight-medium")
            ui.button("Open", on_click=lambda i=i: ui.navigate.to(f"/recovery?id={i['id']}")) \
                .props("flat dense no-caps color=primary")
    if len(items) > 3:
        caption(f"… and {len(items) - 3} more on the Recovery page.")
