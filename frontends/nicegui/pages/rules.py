"""Rules (PMO): the business rules with their bounds and the Data Steward's required fields. They belong to the PMO
Head, who changes them here; every other PMO account reads them. Same texts and confirmations as the React
page; values and actions come from the /governance API, which checks the PMO Head again."""
from __future__ import annotations

from functools import partial

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, page_header
from frontends.nicegui.pages.admin_common import CSS, Section, callout, caption, confirm, heading, table


async def render(info: dict, query: dict) -> None:
    ui.add_css(CSS)
    editable = bool(info["user"].get("pmo_head"))
    page_header("Rules", "Business rules and Data Steward required fields · they apply to every new analysis, "
                         "verdict and data check")
    if not editable:
        callout("info", "Read-only – only the PMO Head changes these rules.")
    for build in (business_rules, required_fields):
        await Section(partial(build, editable=editable), page="rules").show()


# =========================================================================== business rules
async def business_rules(section: Section, editable: bool) -> None:
    view = await api.get("/governance/business-rules")
    rules = {r["name"]: r for r in view["rules"]}
    heading("Business rules")
    caption("The organisation's business assumptions and verdict thresholds, owned by the PMO Head. Stored per "
            f"environment in `{view['env_file']}` and promoted with it. Used by the calculation tools, the Data Steward "
            "and the deterministic verdict engine, and given to the agents as 'business assumptions'.")
    fields: dict[str, ui.number] = {}
    with card():
        for title, names in view["groups"].items():
            heading(title, 3)
            with ui.element("div").classes("dk-grid w-full q-mb-md"):
                for name in names:
                    rule = rules[name]
                    step = 1 if rule["integer"] else 100.0 if rule["unit"].startswith("€") else 0.5
                    text = f"{rule['label']} ({rule['unit']})"
                    with ui.element("div").classes("adm-rule"):
                        ui.label(text).classes("text-body2")
                        fields[name] = ui.number(value=rule["value"], min=rule["min"], max=rule["max"], step=step,
                                                 precision=0 if rule["integer"] else None) \
                            .props(f'outlined dense aria-label="{text}" '
                                   f'hint="{_bound(rule["min"])} – {_bound(rule["max"])} · default {_bound(rule["default"])}"').classes("w-full") \
                            .tooltip(f"{rule['help']} Allowed: {_bound(rule['min'])} – {_bound(rule['max'])}; default "
                                 f"{_bound(rule['default'])}.")
                        if not editable:
                            fields[name].props("readonly")
        if editable:
            caption("Changing the team capacity limit rebuilds the graph database (overloaded months are "
                    "pre-computed).")

            async def save() -> None:
                values = {n: f.value for n, f in fields.items() if f.value is not None}
                await section.act("PUT", "/governance/business-rules", {"values": values})

            ui.button("Save business rules", icon="save", on_click=save).props("unelevated color=primary")

    async def reset() -> None:
        if await confirm("Reset to defaults", "Replaces every business rule threshold with its default. Verdicts from "
                         "now on use the defaults; your values are lost (the audit trail keeps the change).",
                         "Reset business rules"):
            await section.act("POST", "/governance/business-rules/reset", {"confirm": True})

    if editable:
        ui.button("Reset to defaults …", icon="restart_alt", on_click=reset).props("outline")
    with ui.expansion("How the verdict engine decides").classes("w-full"):
        ui.markdown(view["verdict_rules_md"])


def _bound(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, (int, float)):
        return f"{int(value):,}" if float(value).is_integer() else f"{value:g}"
    return str(value)


# =========================================================================== required fields
async def required_fields(section: Section, editable: bool) -> None:
    view = await api.get("/governance/required-fields")
    label = view["labels"].get
    with card():
        heading("Data Steward required fields")
        locked = ", ".join(label(f, f) for f in view["locked"])
        select = ui.select({o: label(o, o) for o in view["options"]}, value=list(view["required_fields"]),
                           multiple=True, label="Required fields (columns of the Projects table)") \
            .props("outlined use-chips" + ("" if editable else " readonly")).classes("w-full") \
            .tooltip("Empty values in these fields are reported as ERROR and block the project for simulation (PMO "
                     f"can force). Always required: {locked}. If the estimate at completion (EAC) is required, a "
                     "missing EAC blocks the project already here – before the verdict's own EAC-missing rule can "
                     "apply.")
        caption(f"Always required: {locked}.")
        if editable:
            ui.button("Save required fields", icon="save",
                      on_click=lambda: section.act("PUT", "/governance/required-fields", {"fields": select.value or []})) \
                .props("unelevated color=primary")

    async def reset() -> None:
        if await confirm("Restore default required fields", "Replaces the required-field list of this environment "
                         "with the defaults. Your own selection is lost.", "Restore defaults"):
            await section.act("POST", "/governance/required-fields/reset", {"confirm": True})

    if editable:
        ui.button("Restore default required fields …", icon="restart_alt", on_click=reset).props("outline")
    missing = view["missing_calc_fields"]
    caption(f"Stored per environment in `{view['env_file']}` · {len(view['required_fields'])} fields active."
            + (f" Note: {', '.join(label(f, f) for f in missing)} are not required – budget simulations still reject "
               "projects with missing values." if missing else ""))
    with ui.expansion("Other Data Steward rules (deterministic, read-only)").classes("w-full"):
        table(view["data_quality_profiles"], [("tag", "data_quality_profile"), ("severity", "Severity"),
                                              ("meaning", "Meaning")])
        ui.label("Also: logic check (progress without actual start), past due, budget overrun (EAC > budget), "
                 "CapEx/OpEx split, resource overload, task integrity.").classes("q-mt-sm")


