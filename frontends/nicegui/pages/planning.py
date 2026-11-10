"""Planning: demands and their detail, the base plan analysis, plan scenarios, saved scenarios, reports, the PMO tools
and the Portfolio Board's plan approval – the sections of the caller's role come from the API. The open section and
demand are in the address (?tab=…&demand=…), so a view can be linked."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.ask_box import ask_box
from frontends.nicegui.components import error_box, kpi, notify, page_header
from frontends.nicegui.pages import planning_parts as parts
from frontends.nicegui.pages import planning_scenarios, planning_tools
from frontends.nicegui.shell import page_title

TITLES = {"plan_approval": "Plan approval", "demands": "Demands", "analysis": "Analysis",
          "scenarios": "Plan scenarios", "saved_scenarios": "Saved plan scenarios", "reports": "Reports",
          "pmo_tools": "PMO tools"}
FRESH = {"saved_scenarios", "reports", "plan_approval"}  # drawn again each time they are opened (writes elsewhere)
OUTSIDE_SCOPE_PREDECESSORS = "Further predecessors lie outside your scope – the PMO coordinates."


# --------------------------------------------------------------------------- demands
async def _slip(demand: str, quarters: int, box: ui.element) -> None:
    impact = await api.get(f"/planning/demands/{demand}/slip", quarters=quarters)
    box.clear()
    with box:
        if not impact["moves"]:
            parts.caption("Nothing moves – every successor starts late enough.")
            return
        parts.table(impact["moves"], caption="What a delay moves along")
        moved, late = impact["moved"], impact["late"]
        parts.caption(f"{moved} item(s) move; {late} would start after their latest start." if late else
                      f"{moved} item(s) move; all still start before their latest start.")


async def _demand_detail(demand_id: str) -> None:
    d = await api.get(f"/planning/demands/{demand_id}")
    head = d["demand"]
    parts.subheading(f"{demand_id} · {head.get('demand_name')}", level=3)
    status, kind = head.get("demand_status") or "–", head.get("demand_type") or "–"
    with ui.element("div").classes("dk-badges"):
        ui.html(parts.badge_html(status, parts.STATUS_TONES.get(status, "neutral")), tag="span")
        ui.html(parts.badge_html(kind, "danger" if kind == "Mandatory" else "neutral"), tag="span")
        ui.html(parts.badge_html(f"{head.get('business_unit')} · {head.get('portfolio')}", "neutral"), tag="span")
    parts.caption(f"Owner: {head.get('demand_owner') or '–'}")
    with ui.element("div").classes("dk-grid w-full"):
        kpi("Duration", f"{parts.number(head.get('duration_months') or 0)} months")
        kpi("NPV", parts.eur(head.get("net_present_value_eur") or 0))
        kpi("Run cost after go-live", f"{parts.eur(head.get('run_cost_after_go_live_eur') or 0)} p.a.")
    names = ["Master data", "Annual figures", "Capacity", "Assessment", "Dependencies"]
    if d.get("position") is not None:
        names.append("In the plan")
    with ui.tabs().props("dense no-caps align=left active-color=primary indicator-color=primary outside-arrows "
                         "mobile-arrows").classes("w-full") as tabs:
        tab = {name: ui.tab(name) for name in names}
    with ui.tab_panels(tabs, value=tab["Master data"]).classes("w-full dk-panels").props("animated=false"):
        with ui.tab_panel(tab["Master data"]).classes("dk-tabpanel"):
            parts.table([{"Field": r["label"], "Value": r["Value"]} for r in d["master_data"]])
        with ui.tab_panel(tab["Annual figures"]).classes("dk-tabpanel"):
            if not d["annual_figures"]:
                parts.caption("No annual figures.")
            else:
                parts.table(d["annual_figures"])
                totals = d["annual_totals"]
                parts.caption(f"Total {parts.eur(totals['capex_eur'])} CapEx and {parts.eur(totals['opex_eur'])} OpEx.")
        with ui.tab_panel(tab["Capacity"]).classes("dk-tabpanel"):
            if not d["capacity"]:
                parts.caption("No capacity demand recorded.")
            else:
                parts.table(d["capacity"], formats={c: "fte" for c in d["capacity"][0] if c != "Team"})
        with ui.tab_panel(tab["Assessment"]).classes("dk-tabpanel"):
            assessment = d["assessment"]
            if assessment["mandatory"]:
                parts.callout(f"Mandatory demand ({head.get('mandatory_reason') or 'reason missing'}) – marked, not "
                              "scored; it is planned before every scored demand.", icon="gavel")
            elif not assessment["parts"]:
                parts.caption("Not assessed yet.")
            else:
                parts.table(assessment["parts"], formats={c: "dec1" for c in ("Score", "Points", "Weight",
                                                                              "Contribution")})
                parts.caption(f"Score {assessment['score']:.1f} of 100 = sum of the weighted contributions.")
        with ui.tab_panel(tab["Dependencies"]).classes("dk-tabpanel"):
            if not d["dependencies"]:
                parts.caption("No dependencies within your view.")
            else:
                parts.table(d["dependencies"], labels={"Steps": "Steps (1 = direct)"})
            if d["outside_scope_links"]:
                parts.caption(OUTSIDE_SCOPE_PREDECESSORS)
            if d["can_slip"]:
                ui.label("If this demand slips by … quarters").classes("q-mt-md")
                slider = ui.slider(min=1, max=4, step=1, value=1).props('label-always markers '
                                                                         'aria-label="Quarters of delay"') \
                    .classes("q-px-md").style("max-width: 420px")
                box = ui.column().classes("w-full")
                slider.on("change", lambda: _slip(demand_id, int(slider.value), box))
                await _slip(demand_id, 1, box)
        if "In the plan" in tab:
            with ui.tab_panel(tab["In the plan"]).classes("dk-tabpanel"):
                _position(d["position"])
    await ask_box("demand", demand_id=demand_id, title="Ask about this demand")


def _position(position: dict) -> None:
    if position.get("decision"):
        reason = f" · {position['reason']}" if position.get("reason") else ""
        ui.markdown(f"Base plan (rank order, today's data): **{position['decision']}**{reason}")
    parts.strong("Budget envelopes of its business unit and portfolio")
    if not position["budget"]:
        parts.caption("No annual figures or no envelopes.")
    else:
        parts.table(position["budget"])
        parts.caption("Committed = running projects, Planned = demands in the plan. "
                      "If the demand is not in the plan, “This demand” shows what it would add.")
    parts.strong("Team capacity")
    if not position["capacity"]:
        parts.caption("No capacity demand recorded.")
    else:
        parts.table(position["capacity"])


async def _demands(info: dict) -> None:
    first = await api.get("/planning/demands")
    if not first["demands"]:
        parts.callout("No demands within your permissions.")
        return
    filters = first["filters"]
    with ui.element("div").classes("dk-grid w-full"):
        units = ui.select(filters["business_units"], multiple=True, label="Business unit", value=[]) \
            .props("outlined use-chips clearable")
        statuses = ui.select(filters["statuses"], multiple=True, label="Status", value=[]) \
            .props("outlined use-chips clearable")
        year = ui.select(["any year", *filters["years"]], label="Has money in", value="any year").props("outlined")
    listing = ui.column().classes("dk-stack")
    detail = ui.column().classes("w-full")
    state = {"pick": parts.query_param("demand") or "", "shown": ""}

    async def open_demand(demand_id: str | None) -> None:
        state["pick"] = demand_id or ""
        parts.set_query(demand=demand_id or None)
        if state["shown"] == state["pick"]:
            return
        state["shown"] = state["pick"]
        detail.clear()
        if not demand_id:
            return
        with detail, ui.element("div").classes("dk-card w-full").style("height:auto"):
            try:
                await _demand_detail(demand_id)
            except api.ApiError as exc:
                error_box(exc.message)

    async def refresh() -> None:
        answer = await api.get("/planning/demands", business_unit=units.value or None, status=statuses.value or None,
                               year=None if year.value == "any year" else year.value)
        listing.clear()
        with listing:
            rows = answer["demands"]
            if rows:
                years = set(filters["years"])
                parts.table(rows, formats={"Score": "score", **{c: "eur" for c in rows[0] if c in years}},
                            scroll=True, caption="Demands")
            else:
                parts.caption("No demand matches the filters.")
            if answer["rank_shown"]:
                parts.caption("Rank: mandatory demands first, then score, then NPV, then the latest start, then the "
                              "number.")
            findings = answer["data_quality_findings"]
            if findings:
                with ui.expansion(f"Data quality of the demands · {len(findings)} finding(s)", icon="fact_check") \
                        .classes("w-full dk-card").style("height:auto; padding:0 8px"):
                    parts.table([{"Demand": f.get("demand"), "Severity": f.get("severity"), "Field": f.get("field"),
                                  "Finding": f["message"]} for f in findings])
            await ask_box("demands", business_unit=units.value or None, status=statuses.value or None,
                          year=None if year.value == "any year" else year.value, title="Ask about the demands")
            options = {"": "Select a demand ...", **{r["Demand"]: f"{r['Demand']} · {r['Name']}" for r in rows}}
            current = state["pick"] if state["pick"] in options else ""
            ui.select(options, value=current, label="Open demand",
                      on_change=lambda e: open_demand(e.value)).props("outlined").classes("w-full")
        await open_demand(current)

    for element in (units, statuses, year):
        element.on_value_change(refresh)
    await refresh()


# --------------------------------------------------------------------------- analysis
def _hot_spot_title(title: str, rest: str) -> None:
    with ui.row().classes("gap-1 items-baseline"):
        ui.label(title).classes("text-weight-bold")
        ui.label(f"· {rest}")


async def _analysis(info: dict) -> None:
    plan = await api.get("/planning/analysis")
    parts.scenario_verdict(plan)
    share = f"{plan['hot_share']:.0f} %"
    with ui.element("div").classes("dk-grid-2 w-full"):
        with ui.column().classes("gap-2"):
            _hot_spot_title("Budget hot spots", f"envelopes used to {share} or more")
            if plan["budget_hot_spots"]:
                parts.table(plan["budget_hot_spots"], caption="Budget hot spots")
            else:
                parts.caption("None.")
        with ui.column().classes("gap-2"):
            _hot_spot_title("Capacity hot spots", f"team quarters used to {share} or more")
            if plan["capacity_hot_spots"]:
                parts.table(plan["capacity_hot_spots"], caption="Capacity hot spots")
            else:
                parts.caption("None.")
    _hot_spot_title("Critical chains", "demands other demands or projects wait for – a delay here moves them too")
    if plan["critical_chains"]:
        parts.table(plan["critical_chains"], caption="Critical chains")
    else:
        parts.caption("No demand has successors.")
    await ask_box("plan", title="Ask about the plan", expanded=True)


# --------------------------------------------------------------------------- the Board's plan approval
def _board_start(summary: dict, pending: list) -> None:
    plan = summary.get("plan_in_force")
    with ui.element("div").classes("dk-grid w-full"):
        kpi("Waiting for your decision", str(summary["waiting"]))
        kpi("Plan in force", plan["scenario_id"] if plan else "none",
            f"“{plan['scenario_name']}” {plan['year_from']}–{plan['year_to']}" if plan
            else "No annual plan approved yet")
        with ui.element("div").classes("dk-card"):
            ui.label("Base plan today").classes("dk-muted")
            with ui.element("div").classes("q-my-sm").style("font-size:1.05rem"):
                parts.verdict_badge(summary["base_plan_verdict"])
            parts.caption("Rank order on today's data")
        kpi("Demands", str(summary["demands"]), f"{summary['in_base_plan']} in the base plan")
    if not pending:
        parts.callout("No annual plan waits for your decision. The PMO submits one from its saved plan scenarios. "
                      "Meanwhile: **Analysis** shows what drops out and why, **Reports** has the board pack (Excel, "
                      "print → PDF).", icon="inbox", markdown=True)


def _override_controls(verdict: str | None) -> tuple[ui.checkbox, ui.textarea]:
    with ui.element("div").classes("dk-callout dk-tone-danger w-full").props('role="alert"'):
        ui.markdown(f"The rule-based verdict is **{verdict}**. Approving means overriding it – the override and your "
                    "reason are recorded in the audit trail.").style("color: inherit")
    confirmed = ui.checkbox(f"Override the {verdict} verdict")
    reason = ui.textarea("Reason for the override (required)").props("outlined autogrow").classes("w-full")
    return confirmed, reason


async def _plan_card(p: dict, redraw: Callable[[], Awaitable[None]]) -> None:
    detail = await api.get(f"/planning/plans/{p['id']}")

    async def decide(approve: bool, override: bool = False, reason: str = "") -> None:
        action = "approve" if approve else "reject"
        body = {"comment": reason or comment.value or ""}
        if approve:
            body["override"] = override
        answer = await parts.decision(f"/planning/plans/{p['id']}/{action}", body)
        if answer.get("kind") == "error":
            notify("error", answer["message"])
            return
        notify(answer.get("kind", "success"), answer["message"])
        await redraw()

    with ui.element("div").classes("dk-card w-full").style("height:auto"):
        with ui.row().classes("items-center gap-2"):
            parts.verdict_badge(detail.get("verdict"))
            ui.label(detail.get("request") or "").classes("text-weight-bold")
            ui.label(f"· submitted by {detail.get('requester')} on {str(detail.get('timestamp') or '')[:10]}")
        ui.markdown(detail.get("paper") or "", extras=["cuddled-lists", "tables", "fenced-code-blocks"]) \
            .classes("dk-paper w-full")
        with ui.expansion(f"Change set · {p['change_count']} row(s)").classes("w-full"):
            parts.table(detail["change_set"], scroll=True, caption="Change set")
        comment = ui.input("Comment (required to send the plan back)").props("outlined").classes("w-full")
        if detail["requires_override"]:
            with ui.row().classes("items-center gap-1 dk-caption"):
                ui.icon("gpp_maybe", size="18px").props('aria-hidden="true"')
                ui.label(f"Rule-based verdict {detail.get('verdict')} – approving needs an explicit override with a "
                         "reason.")

        with ui.dialog() as dialog, ui.card().classes("q-pa-md").style("max-width: 560px; width: 100%"):
            ui.label("Confirm plan approval").classes("text-h6").props('role="heading" aria-level="2"')
            with ui.element("div").classes("dk-callout dk-tone-warning w-full"):
                ui.markdown(f"Approve **{detail.get('request')}** and write **{p['change_count']} plan row(s)** to "
                            "the portfolio data source now? A snapshot is taken first; the System Admin can restore "
                            "it.").style("color: inherit")
            override = _override_controls(detail.get("verdict")) if detail["requires_override"] else None

            async def confirm() -> None:
                dialog.close()
                if override:
                    await decide(True, True, override[1].value or "")
                else:
                    await decide(True)

            with ui.row().classes("w-full no-wrap gap-2 q-mt-md"):
                ok = ui.button("Confirm: approve the plan", icon="verified", on_click=confirm) \
                    .props("unelevated color=primary").classes("col")
                ui.button("Cancel", on_click=dialog.close).props("outline").classes("col")
            if override:
                def ready() -> None:
                    ok.set_enabled(bool(override[0].value) and bool((override[1].value or "").strip()))
                override[0].on_value_change(ready)
                override[1].on_value_change(ready)
                ready()

        with ui.element("div").classes("dk-grid-2 w-full"):
            ui.button("Approve the plan …", icon="verified", on_click=dialog.open) \
                .props("unelevated color=primary").tooltip("Opens a confirmation before the plan is written.")
            back = ui.button("Send back", icon="reply", on_click=lambda: decide(False)).props("outline")
            back.bind_enabled_from(comment, "value", backward=lambda v: bool((v or "").strip()))
            back.tooltip("Enter a comment first – sending back needs a reason.")


async def _plan_approval(info: dict, redraw: Callable[[], Awaitable[None]]) -> None:
    summary = await api.get("/planning/board")
    pending = (await api.get("/planning/plans", status="submitted"))["plans"]
    _board_start(summary, pending)
    for p in pending:
        await _plan_card(p, redraw)
    plan = summary.get("plan_in_force")
    if plan:
        parts.caption(f"Approved plan in force: {plan['scenario_id']} “{plan['scenario_name']}” "
                      f"({plan['year_from']}–{plan['year_to']}), approved by {plan.get('approved_by') or '–'}.")


# --------------------------------------------------------------------------- page
async def render(info: dict, query: dict) -> None:
    parts.install()
    ctx = await api.get("/planning")
    page_header(page_title(info, "planning", "Planning"), f"Annual and multi-year planning {ctx['first_year']}–{ctx['last_year']} · "
                            f"{ctx['demands']} demand(s) within your permissions · ranks and verdicts are rule-based")
    sections = ctx["sections"]
    active = query.get("tab") if query.get("tab") in sections else sections[0]
    if "scenarios" in sections and planning_scenarios.take_prefill():
        active = "scenarios"
        parts.set_query(tab=active)
    with ui.tabs(value=active).props("dense no-caps align=left active-color=primary indicator-color=primary "
                                     "outside-arrows mobile-arrows").classes("w-full") as tabs:
        for section in sections:
            ui.tab(section, label=TITLES.get(section, section))
    panels: dict[str, ui.element] = {}
    drawn: set[str] = set()
    with ui.tab_panels(tabs, value=active).classes("w-full dk-panels").props("animated=false keep-alive"):
        for section in sections:
            with ui.tab_panel(section).classes("dk-tabpanel"):
                panels[section] = ui.column().classes("dk-stack")
    ui.label(f"Reference date {ctx['reference_date']}.").classes("dk-caption")

    async def draw(section: str) -> None:
        box = panels[section]
        box.clear()
        drawn.add(section)
        with box:
            try:
                await renderers[section]()
            except api.ApiError as exc:
                error_box(exc.message)

    renderers: dict[str, Callable[[], Awaitable[None]]] = {
        "plan_approval": lambda: _plan_approval(info, lambda: draw("plan_approval")),
        "demands": lambda: _demands(info),
        "analysis": lambda: _analysis(info),
        "scenarios": lambda: planning_scenarios.render_workshop(ctx),
        "saved_scenarios": lambda: planning_scenarios.render_saved(lambda: draw("saved_scenarios")),
        "reports": lambda: planning_scenarios.render_reports(),
        "pmo_tools": lambda: planning_tools.render(),
    }

    async def opened(e) -> None:
        section = e.value
        if section == "demands":
            parts.set_query(tab=section)
        else:
            parts.set_query(tab=section, demand=None)
        if section not in drawn or section in FRESH:
            await draw(section)

    tabs.on_value_change(opened)
    await draw(active)
