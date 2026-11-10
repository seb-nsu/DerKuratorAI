"""The master-data form of the project detail page (owning Project Manager): two sections, two buttons – the direct
section is saved at once, the governed section becomes one proposal for the PMO. Only changed fields are sent; the API
validates them, so a field it refuses is reported with the API's own words."""
from __future__ import annotations

from urllib.parse import urlencode

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import flash, notify
from frontends.nicegui.pages.projects_fmt import caption

SECTIONS = {
    "direct": ("Master data", "saved directly", ("Save changes", "save"), "Change a master-data field first."),
    "governed": ("Governed fields", "require PMO approval (four-eyes principle)", ("Submit for approval", "send"),
                 "Change a governed field first."),
}


def _same(kind: str, a, b) -> bool:
    if kind in ("number", "percent", "int"):
        try:
            return (a is None and b is None) or (a is not None and b is not None and float(a) == float(b))
        except (TypeError, ValueError):
            return False
    return str(a or "") == str(b or "")


def _widget(spec: dict):
    label = spec["label"] + (" · required – missing" if spec["missing"] else "")
    kind, value = spec["kind"], spec["value"]
    if kind == "select":
        w = ui.select(spec["options"] or [], label=label, value=value).props("outlined dense")
    elif kind == "textarea":
        w = ui.textarea(label, value=value or "").props("outlined autogrow").classes("w-full")
    elif kind == "text":
        w = ui.input(label, value=value or "").props("outlined dense")
    elif kind == "date":
        w = ui.input(label, value=value or "").props(f"outlined dense type=date stack-label min={spec['min']} "
                                                     f"max={spec['max']}")
    else:
        w = ui.number(label, value=value, min=spec["min"], max=spec["max"], step=spec["step"],
                      format="%d" if kind == "int" else "%.0f").props("outlined dense")
    if spec["missing"]:
        w.classes("dk-field-missing").props("error")
    w.set_enabled(bool(spec["editable"]))
    original = spec.get("stored_out_of_range")
    if original is not None:  # shown clamped; sent back unchanged it keeps its stored value (the API checks it)
        low = spec["min"] if spec["min"] is not None else "–"
        high = spec["max"] if spec["max"] is not None else "–"
        text = f"{original:g}" if isinstance(original, float) else original
        caption(f"Stored value {text} is outside the valid range ({low} to {high}) – shown as {value}; please "
                "correct it.", "error", "danger")
    return w


async def render_edit_form(pid: str, findings: list[dict]) -> None:
    form = await api.get(f"/projects/{pid}/edit-form")
    access = form["access"]
    ui.label(f"Edit master data – {pid}").classes("text-subtitle1 text-weight-bold q-mt-sm") \
        .props('role="heading" aria-level="3"')
    if access == "read":
        caption("PMO view is read-only. Only the owning Project Manager can edit project master data.")
    if form["missing_required"]:
        with ui.element("div").classes("dk-callout dk-tone-danger w-full").props('role="alert"'):
            ui.label("Required fields missing: " + ", ".join(form["missing_required"]))
    if findings:
        with ui.expansion(f"Data Steward findings for this project ({len(findings)})").classes("w-full"):
            ui.markdown("\n".join(f"- **{f['severity']}** · {f['rule_label']} · {f.get('field_label') or '–'} – "
                                  f"{f['message']}" for f in findings))
    for name, fields in form["sections"].items():
        if name == "governed":
            ui.separator().classes("q-my-md")
        _section(pid, name, fields, access == "edit")


def _section(pid: str, name: str, fields: list[dict], editable: bool) -> None:
    title, note, (button_label, icon), hint = SECTIONS[name]
    with ui.row().classes("items-baseline gap-1 q-mt-sm"):
        ui.label(title).classes("text-weight-bold").props('role="heading" aria-level="4"')
        ui.label(f"· {note}").classes("dk-muted")
    if name == "governed" and editable:
        caption("Changes in this section are submitted as a proposal to the PMO inbox and written after approval. "
                "Approved budget and CapEx/OpEx plan can be filled in here only while empty – to change an existing "
                "budget, simulate a budget scenario in the Steering Console.")
    inputs: dict[str, tuple[dict, object]] = {}
    with ui.element("div").classes("dk-grid w-full"):
        for spec in fields:
            if spec["kind"] == "textarea":
                continue
            with ui.column().classes("gap-0 w-full"):
                inputs[spec["field"]] = (spec, _widget(spec).classes("w-full"))
    for spec in fields:
        if spec["kind"] == "textarea":
            inputs[spec["field"]] = (spec, _widget(spec))
    errors = ui.label("").classes("dk-callout dk-tone-danger w-full").style("white-space: pre-wrap") \
        .props('role="alert"')
    errors.set_visibility(False)
    if not editable:
        return

    def changed() -> dict:
        return {f: w.value for f, (spec, w) in inputs.items() if spec["editable"]
                and not _same(spec["kind"], w.value, spec["value"])}

    async def save() -> None:
        values = changed()
        if not values:
            return
        try:
            outcome = await api.post(f"/projects/{pid}/master-data", {"section": name, "values": values})
        except api.ApiError as exc:
            errors.set_text(exc.message)
            errors.set_visibility(True)
            return
        if outcome["kind"] != "success":
            notify(outcome["kind"], outcome["message"])
            return
        flash("success", outcome["message"])
        ui.navigate.to(f"/projects?{urlencode({'project': pid, 'tab': 'Edit'})}")

    button = ui.button(button_label, icon=icon, on_click=save).props("unelevated color=primary").classes("q-mt-sm")
    tip = button.tooltip(hint)

    def sync() -> None:
        dirty = bool(changed())
        button.set_enabled(dirty)
        tip.set_visibility(not dirty)
    for _spec, w in inputs.values():
        w.on_value_change(sync)
    sync()
