"""The page assistant: a question box under a page ("Ask about …", "Portfolio briefing and questions").

    await ask_box(page, **context)

page     the box's page in the API (`POST /assist/ask`): "overview", "data_quality", "report", "project", "compare",
         "plan", "demands", "demand" or "scenario". Title and example questions come from `GET /assist/pages`; a page
         the role has no box for draws nothing.
context  what the API needs for the page – `run_id` or `proposal_id` (page "report"), `project_id` ("project"), `a`
         and `b` ("compare": the saved what-ifs), `demand_id` ("demand"), the table filters `business_unit`,
         `status`, `year` ("demands"), `scenarios` (the calculated scenario inputs) and `scenario` (the one shown,
         page "scenario") – and the box's look: `title` (instead of the API's), `expanded` (open from the start),
         `placeholder`. Ids name the box's history; filters and scenario inputs do not.

A change or planning question comes back calculated by the rules (verdict, changes, rule-based paper) with its next
step: "Continue in the Steering Console" stores the request under `app.storage.user["console_request"]` and opens
/console; "Open in Planning › Plan scenarios" stores the scenario inputs under `app.storage.user["planning_prefill"]`
and opens /planning?tab=scenarios. Those pages read and remove the key. Any other question is answered from the facts
the server builds for the caller's scope. The last three answers per box stay in the user's session."""
from __future__ import annotations

from nicegui import app, ui

from frontends.nicegui import api
from frontends.nicegui.components import notify

HISTORY = 3  # answers kept per box, as in the React box
MAX_CHARS = 600
API_IDS = ("run_id", "proposal_id", "demand_id", "project_id", "a", "b", "scenario")  # name the box's history
API_CONTEXT = ("business_unit", "status", "year", "scenarios")  # sent with the question only
CHANGE_COLUMNS = [("project", "Project"), ("change", "Change"), ("current", "Current"), ("proposed", "Proposed"),
                  ("delta", "Δ"), ("approval", "Approval"), ("note", "Note")]


def _readable(facts: str) -> str:
    """Facts for the screen: section titles as bold lines instead of large headings."""
    return "\n".join(f"**{line[3:].strip()}**" if line.startswith("## ") else line
                     for line in (facts or "").splitlines())


def _continue_in_console(request: str) -> None:
    app.storage.user["console_request"] = request
    ui.navigate.to("/console")


def _open_planning(inputs: dict) -> None:
    app.storage.user["planning_prefill"] = inputs
    ui.navigate.to("/planning?tab=scenarios")


def _caption(text: str) -> None:
    ui.label(text).classes("dk-muted text-caption")


def _calculation(result: dict) -> None:
    verdict = result.get("verdict")
    if verdict is None:  # blocked, or a planning question for a role that does not plan
        ui.markdown(result.get("paper") or "")
    else:
        from frontends.nicegui import theme
        from frontends.nicegui.components import VERDICT_ICONS

        tone = theme.tone("verdict", verdict["label"])
        with ui.element("div").classes(f"dk-callout dk-tone-{tone} w-full").props('role="status"'):
            with ui.row().classes("items-start no-wrap gap-2"):
                ui.icon(VERDICT_ICONS.get(tone, "info"), size="22px")
                ui.markdown(f"**Recommendation: {verdict['label']}**"
                            + (f" – {verdict['lead_reason']}" if verdict.get("lead_reason") else "")) \
                    .style("color: var(--dk-text)")
        with ui.row().classes("items-center gap-1"):
            ui.icon("gavel", size="16px").classes("dk-muted")
            _caption("Calculated by the rules – not by the AI")
        bullets = [*verdict["reasons"][2:4], *(f"Condition: {c}" for c in verdict["conditions"][:3])]
        if bullets:
            ui.markdown("\n".join(f"- {b}" for b in bullets))
        rows = result.get("change_rows") or []
        if rows:
            ui.label("Scenario Analysis (quantitative)").classes("text-weight-medium")
            ui.table(columns=[{"name": k, "label": label, "field": k, "align": "left"} for k, label in CHANGE_COLUMNS],
                     rows=[{**r, "_i": i} for i, r in enumerate(rows)], row_key="_i") \
                .props("flat dense wrap-cells").classes("w-full")
        with ui.expansion("Rule-based paper", icon="description").classes("w-full dk-card q-pa-none"):
            ui.markdown(result.get("paper") or "")
    step = result.get("next_step")
    if not step:
        return
    _caption(step["text"])
    if step["target"] == "console":
        ui.button("Continue in the Steering Console", icon="query_stats",
                  on_click=lambda: _continue_in_console(step.get("request") or result["request"])).props("outline")
    elif step["target"] == "planning":
        ui.button("Open in Planning › Plan scenarios", icon="event_note",
                  on_click=lambda: _open_planning(step["inputs"])).props("outline")


def _answer(answer: dict) -> None:
    ui.markdown(answer["text"])
    _caption(answer["note"] + (" · facts shortened" if answer.get("truncated") else ""))
    handoff = answer.get("handoff")
    if handoff:
        _caption(handoff["text"])
    with ui.row().classes("items-center gap-2"):
        if handoff:
            ui.button("Calculate in the Steering Console", icon="query_stats",
                      on_click=lambda: _continue_in_console(handoff["request"])).props("outline")
        with ui.button("Facts", icon="fact_check").props("flat dense no-caps"):
            with ui.menu().classes("q-pa-md").style("max-width: min(560px, 92vw)"):
                ui.markdown(_readable(answer.get("facts") or "") or "–")


async def ask_box(page: str, **context) -> None:
    """Draws the question box of `page` (see the module docstring for the parameters)."""
    try:
        meta = await api.get("/assist/pages")
    except api.ApiError:
        return  # no assistant for this role
    box = next((p for p in meta["pages"] if p["page"] == page), None)
    if box is None:
        return
    ids = {k: context[k] for k in API_IDS if context.get(k)}
    key = "ask:" + ":".join([page, *(str(v) for v in ids.values())])
    sent = {**ids, **{k: context[k] for k in API_CONTEXT if context.get(k)}}
    history: list[dict] = list(app.storage.user.get(key) or [])
    title = context.get("title") or box["title"]

    with ui.expansion(title, icon="forum", value=bool(context.get("expanded") or history)) \
            .classes("w-full dk-card q-pa-none").props('header-class="text-weight-medium"'):
        with ui.row().classes("w-full items-start no-wrap gap-2"):
            question = ui.input("Question", placeholder=context.get("placeholder") or "Ask a question …") \
                .props(f'outlined dense maxlength={MAX_CHARS} aria-label="Question"').classes("col")
            send = ui.button("Ask", icon="send").props("unelevated color=primary")
        if box["examples"]:
            with ui.row().classes("w-full gap-1"):
                example_buttons = [ui.button(example, on_click=lambda e=example: submit(e))
                                   .props("flat dense no-caps color=primary") for example in box["examples"]]
        else:
            example_buttons = []
        busy = ui.row().classes("items-center gap-2")
        with busy:
            ui.spinner(size="sm")
            ui.label("Calculating ...").classes("dk-muted")
        busy.set_visibility(False)

        @ui.refreshable
        def answers() -> None:
            if not history:
                _caption(meta["hint"])
                return
            for i, entry in enumerate(history):
                ui.label(entry["question"]).classes("text-weight-bold q-mt-sm")
                (_calculation if entry.get("kind") == "calculation" else _answer)(entry)
                if i < len(history) - 1:
                    ui.separator().classes("q-my-sm")

        answers()

        async def submit(text: str | None = None) -> None:
            asked = (text if text is not None else question.value or "").strip()
            if not asked:
                return
            for control in (send, question, *example_buttons):
                control.disable()
            busy.set_visibility(True)
            try:
                result = await api.post("/assist/ask", {"page": page, "question": asked, **sent})
            except api.ApiError as exc:
                notify("error", exc.message)
                return
            finally:
                busy.set_visibility(False)
                for control in (send, question, *example_buttons):
                    control.enable()
            question.set_value("")
            history.insert(0, result)
            del history[HISTORY:]
            app.storage.user[key] = history
            answers.refresh()

        send.on_click(lambda: submit())
        question.on("keydown.enter", lambda: submit())
