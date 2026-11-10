"""Admin Console – configuration: models per agent with shadowing, API keys (write-only), the model catalog, MCP
integrations and environment staging. Business rules and required fields live on the PMO's Rules page (rules.py)."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, notify
from frontends.nicegui.pages.admin_common import (
    Section,
    callout,
    caption,
    confirm,
    heading,
    rich,
    table,
    to_sign_in,
)

NO_SHADOW = "— none —"
KEYS = (("openai", "OpenAI API key", "sk-…"), ("anthropic", "Anthropic API key", "sk-ant-…"),
        ("google", "Google API key (Gemini)", "AIza…"))


# =========================================================================== models, prompt & shadowing
async def models(section: Section) -> None:
    view = await api.get("/admin/settings")
    files = await api.get("/admin/files")
    options = view["model_options"]
    with card():
        heading("Models, shadowing & behaviour")
        if not options:
            callout("error", f"No models available – check `{files['model_registry']}`.")
        with ui.element("div").classes("dk-card q-mt-sm"):
            heading("API keys", 3)
            keys = {}
            with ui.element("div").classes("dk-grid w-full"):
                for provider, label, placeholder in KEYS:
                    hint = view["api_keys"][provider]["key_hint"]
                    with ui.column().classes("gap-1"):
                        # write-only: the field starts empty, an empty field keeps the stored key
                        keys[provider] = ui.input(label, placeholder=placeholder, password=True,
                                                  password_toggle_button=True) \
                            .props('outlined dense autocomplete="off"').classes("w-full").tooltip(hint)
                        caption(hint)
            rich("**Per-agent model matrix** – main LLM runs the pipeline; the shadow LLM runs the same agents in the "
                 "background for comparison (Shadow Logs). The Data Steward's checks stay deterministic; its model "
                 "writes the executive data-quality interpretation.", "q-mt-md")
            choices: dict[str, tuple[ui.select, ui.select]] = {}
            with ui.element("div").classes("adm-matrix w-full q-mt-sm"):
                for head in ("Agent", "Main LLM", "Shadow LLM"):
                    ui.label(head).classes("dk-muted text-caption adm-matrix-head")
                for row in view["agents"]:
                    ui.label(row["label"]).classes("text-weight-medium")
                    main_options = options if row["main"] in options else [row["main"], *options]
                    main = ui.select(main_options, value=row["main"], label="Main LLM") \
                        .props(f'outlined dense aria-label="Main LLM of {row["label"]}"').classes("w-full")
                    shadow_options = [NO_SHADOW, *options]
                    if row["shadow"] and row["shadow"] not in shadow_options:
                        shadow_options.append(row["shadow"])
                    shadow = ui.select(shadow_options, value=row["shadow"] or NO_SHADOW, label="Shadow LLM") \
                        .props(f'outlined dense aria-label="Shadow LLM of {row["label"]}"').classes("w-full")
                    choices[row["agent"]] = (main, shadow)
            with ui.element("div").classes("dk-grid-2 w-full q-mt-md"):
                with ui.column().classes("gap-2"):
                    pii = ui.switch("Enable PII masking", value=view["pii_masking_enabled"]) \
                        .tooltip("Pseudonymises person names (PM, sponsor, assignees, stakeholders, benefit and demand "
                                 "owners) and e-mail addresses before every LLM call.")
                    ui.label("Agent creativity (temperature)").classes("q-mt-sm")
                    temperature = ui.slider(min=0.0, max=1.0, step=0.05, value=view["temperature"]) \
                        .props('label-always aria-label="Agent creativity (temperature)"').classes("q-mt-lg") \
                        .tooltip("0 = deterministic wording, 1 = creative. Applies to all agents. Thinking/reasoning "
                                 "models (is_thinking_model in supported_models.json) ignore it and always run with "
                                 "1.0. Numbers are calculated by the deterministic tools anyway.")
                    caption("Not applied to Claude models (not supported by the provider).")
                    if view["thinking_models_in_use"]:
                        caption(f"Overridden to 1.0 for thinking models: {', '.join(view['thinking_models_in_use'])}")
                prompt = ui.textarea("Orchestrator system prompt", value=view["orchestrator_prompt"]) \
                    .props("outlined autogrow").classes("w-full") \
                    .tooltip("Backstory of the orchestrator agent (intent split and routing). Empty = default prompt.")

            async def save() -> None:
                body = {"agents": {a: {"main": m.value, "shadow": "" if s.value in (None, NO_SHADOW) else s.value}
                                   for a, (m, s) in choices.items()},
                        "temperature": temperature.value, "pii_masking_enabled": pii.value,
                        "orchestrator_prompt": prompt.value or ""}
                for provider, field in keys.items():  # an empty field keeps the stored key (never sent back)
                    if (field.value or "").strip():
                        body[f"{provider}_api_key"] = field.value.strip()
                await section.act("PUT", "/admin/settings", body)

            ui.button("Save settings", icon="save", on_click=save).props("unelevated color=primary").classes("q-mt-md")

        async def reload() -> None:
            if await confirm("Reload from .env and environment file", "Discards every setting changed here that is "
                             "not yet in the .env or environment file.", "Discard and reload"):
                await section.act("POST", "/admin/settings/reload", {"confirm": True})

        ui.button("Reload from .env and environment file …", icon="restart_alt", on_click=reload) \
            .props("outline").classes("q-mt-md")
        if view["missing_shadow_keys"]:
            callout("warning", f"Shadowing is configured but keys are missing: {', '.join(view['missing_shadow_keys'])}")
    await add_model(section, files)


async def add_model(section: Section, files: dict) -> None:
    catalog = await api.get("/admin/models")
    with ui.expansion("Add new LLM").classes("w-full"):
        caption(f"Adds a model to `{files['model_registry']}` after a successful live test call.")
        model_id = ui.input("Model-ID (optionally with provider prefix)",
                            placeholder="e.g. gemini/gemini-2.5-pro or gpt-4.1").props("outlined dense") \
            .classes("w-full").style("max-width: 520px")
        thinking = ui.checkbox("Is a reasoning/thinking model") \
            .tooltip("Thinking models run with temperature 1.0 and without a separate system prompt.")
        result = ui.column().classes("w-full")

        async def add() -> None:
            mid = (model_id.value or "").strip()
            button.props("loading")
            result.clear()
            try:
                answer = await api.post("/admin/models", {"model_id": mid, "is_thinking_model": thinking.value})
            except api.ApiError as exc:
                button.props(remove="loading")
                if exc.status == 401:
                    to_sign_in()
                    return
                with result:
                    callout("error", exc.message)
                return
            notify("success", answer["message"])
            await section.refresh()

        button = ui.button("Test & add model", on_click=add).props("unelevated color=primary") \
            .bind_enabled_from(model_id, "value", backward=lambda v: bool((v or "").strip()))
        with ui.element("div").classes("w-full").style("max-height: 260px; overflow: auto"):
            table([{**m, "thinking": "yes" if m["thinking"] else ""} for m in catalog["models"]],
                  [("model_id", "model_id"), ("provider", "provider"), ("thinking", "thinking")], page_size=500)


# =========================================================================== MCP integrations
async def mcp(section: Section) -> None:
    view = await api.get("/admin/mcp")
    files = await api.get("/admin/files")
    heading("Integrations (MCP)")
    caption("External enterprise data via the Model Context Protocol. Settings are stored per environment in "
            f"`{files['env_file']}` (promoted with the rest of the config). Access tokens are secrets: set them as "
            "environment variables (e.g. `SERVICENOW_MCP_TOKEN`) – they are never stored or shown.")
    urls: dict[str, ui.input] = {}
    with card():
        enabled = ui.switch("Enable MCP enterprise data (overrides the local DB)", value=view["enabled"]) \
            .tooltip("Master switch for a future release: agents load their data tools from the MCP servers instead "
                     "of the in-memory dependency graph.")
        for server in view["servers"]:
            urls[server["name"]] = ui.input(server["label"], value=server["url"],
                                            placeholder=f"https://{server['name']}.example.com/mcp") \
                .props("outlined dense").classes("w-full")
        ui.button("Save integrations", icon="save", on_click=lambda: section.act(
            "PUT", "/admin/mcp", {"enabled": enabled.value, "servers": {n: (f.value or "").strip()
                                                                        for n, f in urls.items()}})) \
            .props("unelevated color=primary").classes("q-mt-sm")
    if view["enabled"]:
        callout("warning", "MCP is enabled in this environment's config. Note: the connector is an architecture "
                           "building block – the agents still use the in-memory dependency graph until it is wired in "
                           "(see below).")
    else:
        callout("info", "MCP disabled – agents use the in-memory dependency graph (default).")
    for server in view["servers"]:
        with card():
            with ui.row().classes("w-full items-center justify-between gap-2"):
                with ui.column().classes("gap-1"):
                    rich(f"**{server['label'].replace(' URL', '')}** · `{server['url'] or 'not configured'}`")
                    with ui.row().classes("items-center gap-1"):
                        rich(f"Token (`{server['token_env']}`):")
                        if server["token_set"]:
                            ui.icon("check", size="18px").classes("text-positive").props('aria-hidden="true"')
                            ui.label("set")
                        else:
                            ui.label("— not set")
                button = ui.button("Test connection", icon="network_check").props("outline")
                button.set_enabled(bool(server["url"]))
            result = ui.column().classes("w-full")

            async def probe(name: str = server["name"], box: ui.column = result) -> None:
                try:
                    answer = await api.post(f"/admin/mcp/{name}/probe")
                except api.ApiError as exc:
                    if exc.status == 401:
                        to_sign_in()
                        return
                    answer = {"ok": False, "message": exc.message}
                box.clear()
                with box:
                    callout("success" if answer["ok"] else "error", answer["message"])

            button.on_click(probe)
    with ui.expansion("Architecture – how the connector plugs into the agents").classes("w-full"):
        rich("`app/crew/tools/mcp_client.py` → `EnterpriseMCPConnector` turns these settings into agent tools "
             "(Streamable HTTP, or SSE for `…/sse` URLs). Wiring it in is one line per agent:")
        ui.code("connector = EnterpriseMCPConnector.from_environment()\n"
                "agent = Agent(**connector.agent_kwargs(agent_kwargs))  # no-op while MCP is disabled",
                language="python").classes("w-full")
        caption("Before enabling in PROD: route MCP results through the same RLS filter and PII masking as local data.")


# =========================================================================== environment staging
async def environment(section: Section) -> None:
    view = await api.get("/admin/environment")
    files = await api.get("/admin/files")
    with card():
        heading("Environment staging")
        with ui.element("div").classes("dk-grid-2 w-full q-mt-sm"):
            with ui.column().classes("gap-2"):
                target = ui.select(view["environments"], value=view["environment"],
                                   label="System Environment (Staging)").props("outlined dense").classes("w-full") \
                    .tooltip("Switching loads data/env_<environment>.json (models, shadowing, required fields). PROD "
                             "needs real accounts (AUTH_USERS_FILE).")
                caption(f"Active file: `{files['env_file']}`")
                if view["demo_accounts_active"]:
                    caption("PROD needs real accounts: the switch is refused while the demo accounts are active.")

                async def switch() -> None:
                    chosen = target.value
                    if not await confirm("Environment switch", f"Switches the system environment from "
                                         f"{view['environment']} to {chosen}: loads its settings file. Every user's "
                                         "sign-in ends – you sign in again afterwards.", f"Switch to {chosen}"):
                        return
                    answer = await section.act("POST", "/admin/environment",
                                               {"environment": chosen, "confirm": True}, refresh=False)
                    if answer is None:
                        return
                    if answer.get("kind") == "success":  # tokens are bound to the environment: sign in again
                        notify("info", "Your sign-in ended with the environment switch – please sign in again.")
                        api.sign_out()
                        ui.timer(2.5, to_sign_in, once=True)
                    else:
                        await section.refresh()

                ui.button("Switch environment …", icon="swap_horiz", on_click=switch).props("outline") \
                    .bind_enabled_from(target, "value", backward=lambda v: v != view["environment"])
            with ui.column().classes("gap-2"):
                heading("Promotion", 3)
                texts = {"TEST": ("Promote config to TEST", "Copies all DEV settings into env_test.json (previous TEST "
                                  "config is archived)."),
                         "PROD": ("Promote TEST to PROD", "Copies all TEST settings into env_prod.json (previous PROD "
                                  "config is archived).")}
                with ui.element("div").classes("dk-grid w-full"):
                    for pair in view["promotions"]:
                        label, help_text = texts.get(pair["target"], (
                            f"Promote {pair['source']} to {pair['target']}",
                            f"Copies all {pair['source']} settings into the {pair['target']} config."))

                        async def promote(p: dict = pair, label: str = label, help_text: str = help_text) -> None:
                            if await confirm(label, f"Confirm promotion to {p['target']}. {help_text}"):
                                await section.act("POST", "/admin/environment/promote",
                                                  {"source": p["source"], "target": p["target"], "confirm": True})

                        ui.button(f"{label} …", icon="upgrade", on_click=promote).props("outline").classes("w-full") \
                            .tooltip(help_text)
