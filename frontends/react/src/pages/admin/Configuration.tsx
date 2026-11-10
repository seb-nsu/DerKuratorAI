// Admin Console – configuration: models per agent with shadowing, API keys (write-only), the model catalog, MCP
// integrations and environment staging. Business rules and required fields live on the PMO's Rules page.
import { useEffect, useState } from "react";
import { api, ApiError, session, type Answer } from "../../api/client";
import { Card, Icon } from "../../components/ui";
import { Callout, Caption, Details, Loaded, Rich, Table, useAct, useConsole, useLoad } from "./common";
import { loadFiles } from "./Operations";

type Settings = Answer<"/api/v1/admin/settings">;
type Models = Answer<"/api/v1/admin/models">;
type Mcp = Answer<"/api/v1/admin/mcp">;
type Probe = Answer<"/api/v1/admin/mcp/{name}/probe", "post">;
type Environment = Answer<"/api/v1/admin/environment">;

const NO_SHADOW = "— none —";
const KEYS: [string, string, string][] = [["openai", "OpenAI API key", "sk-…"], ["anthropic", "Anthropic API key", "sk-ant-…"],
  ["google", "Google API key (Gemini)", "AIza…"]];

// ------------------------------------------------------------------ models, prompt & shadowing
export function ModelsSection() {
  const state = useLoad(async () => ({ view: await api.get<Settings>("/admin/settings"), files: await loadFiles(),
    catalog: await api.get<Models>("/admin/models") }));
  return <Loaded state={state}>{(d) => <ModelsForm {...d} reload={state.reload} />}</Loaded>;
}

function ModelsForm({ view, files, catalog, reload }: { view: Settings; files: Awaited<ReturnType<typeof loadFiles>>; catalog: Models; reload: () => Promise<void> }) {
  const act = useAct();
  const { confirm } = useConsole();
  const options = view.model_options;
  const [agents, setAgents] = useState(() => Object.fromEntries(view.agents.map((a) => [a.agent, { main: a.main, shadow: a.shadow || NO_SHADOW }])));
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [pii, setPii] = useState(view.pii_masking_enabled);
  const [temperature, setTemperature] = useState(view.temperature);
  const [prompt, setPrompt] = useState(view.orchestrator_prompt);
  const save = async () => {
    const body: Record<string, unknown> = {
      agents: Object.fromEntries(Object.entries(agents).map(([a, m]) => [a, { main: m.main, shadow: m.shadow === NO_SHADOW ? "" : m.shadow }])),
      temperature, pii_masking_enabled: pii, orchestrator_prompt: prompt,
    };
    // an empty field keeps the stored key (never sent back)
    Object.entries(keys).forEach(([p, v]) => (v.trim() ? (body[`${p}_api_key`] = v.trim()) : null));
    if (await act("PUT", "/admin/settings", body)) {
      setKeys({});
      await reload();
    }
  };
  const withCurrent = (list: string[], value: string) => (list.includes(value) ? list : [value, ...list]);
  return (
    <div className="adm-stack">
      <Card>
        <div className="adm-stack">
          <h2 style={{ marginTop: 0 }}>Models, shadowing &amp; behaviour</h2>
          {options.length === 0 && <Callout kind="error" text={`No models available – check \`${files.model_registry}\`.`} />}
          <section className="card adm-stack" style={{ height: "auto" }}>
            <h3 style={{ margin: 0 }}>API keys</h3>
            <div className="grid">
              {KEYS.map(([provider, label, placeholder]) => (
                <label key={provider} className="field" title={view.api_keys[provider]?.key_hint}>{label}
                  <input type="password" autoComplete="off" placeholder={placeholder} value={keys[provider] ?? ""}
                    onChange={(e) => setKeys({ ...keys, [provider]: e.target.value })} />
                  <span className="small">{view.api_keys[provider]?.key_hint}</span>
                </label>
              ))}
            </div>
            <Rich text={"**Per-agent model matrix** – main LLM runs the pipeline; the shadow LLM runs the same agents in the background "
              + "for comparison (Shadow Logs). The Data Steward's checks stay deterministic; its model writes the executive data-quality interpretation."} />
            <div className="adm-matrix">
              <span className="muted small adm-matrix-head">Agent</span>
              <span className="muted small adm-matrix-head">Main LLM</span>
              <span className="muted small adm-matrix-head">Shadow LLM</span>
              {view.agents.map((row) => (
                <div key={row.agent} style={{ display: "contents" }}>
                  <strong>{row.label}</strong>
                  <select aria-label={`Main LLM of ${row.label}`} value={agents[row.agent].main}
                    onChange={(e) => setAgents({ ...agents, [row.agent]: { ...agents[row.agent], main: e.target.value } })}>
                    {withCurrent(options, row.main).map((o) => <option key={o}>{o}</option>)}
                  </select>
                  <select aria-label={`Shadow LLM of ${row.label}`} value={agents[row.agent].shadow}
                    onChange={(e) => setAgents({ ...agents, [row.agent]: { ...agents[row.agent], shadow: e.target.value } })}>
                    {withCurrent([NO_SHADOW, ...options], row.shadow || NO_SHADOW).map((o) => <option key={o}>{o}</option>)}
                  </select>
                </div>
              ))}
            </div>
            <div className="grid-2">
              <div className="adm-stack">
                <label className="row" title="Pseudonymises person names (PM, sponsor, assignees, stakeholders, benefit and demand owners) and e-mail addresses before every LLM call.">
                  <input type="checkbox" role="switch" style={{ width: "auto" }} checked={pii} onChange={(e) => setPii(e.target.checked)} />
                  Enable PII masking
                </label>
                <label className="field" title={"0 = deterministic wording, 1 = creative. Applies to all agents. Thinking/reasoning models "
                  + "(is_thinking_model in supported_models.json) ignore it and always run with 1.0. Numbers are calculated by the deterministic tools anyway."}>
                  Agent creativity (temperature): {temperature.toFixed(2)}
                  <input type="range" min={0} max={1} step={0.05} value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} style={{ padding: 0 }} />
                </label>
                <Caption text="Not applied to Claude models (not supported by the provider)." />
                {view.thinking_models_in_use.length > 0 && (
                  <Caption text={`Overridden to 1.0 for thinking models: ${view.thinking_models_in_use.join(", ")}`} />
                )}
              </div>
              <label className="field" title="Backstory of the orchestrator agent (intent split and routing). Empty = default prompt.">Orchestrator system prompt
                <textarea rows={7} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
              </label>
            </div>
            <div><button className="primary" onClick={() => void save()}><Icon name="save" />Save settings</button></div>
          </section>
          <div><button onClick={async () => {
            if (await confirm("Reload from .env and environment file", "Discards every setting changed here that is not yet in the .env or environment file.",
              "Discard and reload") && await act("POST", "/admin/settings/reload", { confirm: true })) await reload();
          }}><Icon name="restart_alt" />Reload from .env and environment file …</button></div>
          {view.missing_shadow_keys.length > 0 && (
            <Callout kind="warning" text={`Shadowing is configured but keys are missing: ${view.missing_shadow_keys.join(", ")}`} />
          )}
        </div>
      </Card>
      <AddModel registry={files.model_registry} catalog={catalog} reload={reload} />
    </div>
  );
}

function AddModel({ registry, catalog, reload }: { registry: string; catalog: Models; reload: () => Promise<void> }) {
  const { notify, signInAgain } = useConsole();
  const [modelId, setModelId] = useState("");
  const [thinking, setThinking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      const answer = await api.post<{ message: string }>("/admin/models", { model_id: modelId.trim(), is_thinking_model: thinking });
      notify("success", answer.message);
      setModelId("");
      await reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return signInAgain();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Details summary="Add new LLM">
      <div className="adm-stack">
        <Caption text={`Adds a model to \`${registry}\` after a successful live test call.`} />
        <label className="field" style={{ maxWidth: 520 }}>Model-ID (optionally with provider prefix)
          <input value={modelId} placeholder="e.g. gemini/gemini-2.5-pro or gpt-4.1" onChange={(e) => setModelId(e.target.value)} />
        </label>
        <label className="row" title="Thinking models run with temperature 1.0 and without a separate system prompt.">
          <input type="checkbox" style={{ width: "auto" }} checked={thinking} onChange={(e) => setThinking(e.target.checked)} />Is a reasoning/thinking model
        </label>
        <div><button className="primary" disabled={!modelId.trim() || busy} aria-busy={busy} onClick={() => void add()}>
          {busy ? `Testing ${modelId.trim()} …` : "Test & add model"}</button></div>
        {error && <Callout kind="error" text={error} />}
        <div style={{ maxHeight: 260, overflow: "auto" }}>
          <Table pageSize={500} rows={catalog.models.map((m) => ({ ...m, thinking: m.thinking ? "yes" : "" }))}
            columns={[["model_id", "model_id"], ["provider", "provider"], ["thinking", "thinking"]]} />
        </div>
      </div>
    </Details>
  );
}

// ------------------------------------------------------------------ MCP integrations
export function McpSection() {
  const state = useLoad(async () => ({ view: await api.get<Mcp>("/admin/mcp"), files: await loadFiles() }));
  const act = useAct();
  const { signInAgain } = useConsole();
  const [enabled, setEnabled] = useState(false);
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [probes, setProbes] = useState<Record<string, Probe>>({});
  useEffect(() => {
    if (!state.data) return;
    setEnabled(state.data.view.enabled);
    setUrls(Object.fromEntries(state.data.view.servers.map((s) => [s.name, s.url])));
  }, [state.data]);
  const probe = async (name: string) => {
    try {
      const answer = await api.post<Probe>(`/admin/mcp/${encodeURIComponent(name)}/probe`);
      setProbes({ ...probes, [name]: answer });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return signInAgain();
      setProbes({ ...probes, [name]: { ok: false, message: e instanceof Error ? e.message : String(e) } });
    }
  };
  return (
    <Loaded state={state}>
      {({ view, files }) => (
        <div className="adm-stack">
          <h2>Integrations (MCP)</h2>
          <Caption text={"External enterprise data via the Model Context Protocol. Settings are stored per environment in "
            + `\`${files.env_file}\` (promoted with the rest of the config). Access tokens are secrets: set them as environment variables `
            + "(e.g. `SERVICENOW_MCP_TOKEN`) – they are never stored or shown."} />
          <Card>
            <div className="adm-stack">
              <label className="row" title="Master switch for a future release: agents load their data tools from the MCP servers instead of the in-memory dependency graph.">
                <input type="checkbox" role="switch" style={{ width: "auto" }} checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                Enable MCP enterprise data (overrides the local DB)
              </label>
              {view.servers.map((s) => (
                <label key={s.name} className="field">{s.label}
                  <input value={urls[s.name] ?? ""} placeholder={`https://${s.name}.example.com/mcp`} onChange={(e) => setUrls({ ...urls, [s.name]: e.target.value })} />
                </label>
              ))}
              <div><button className="primary" onClick={async () => {
                const servers = Object.fromEntries(Object.entries(urls).map(([k, v]) => [k, v.trim()]));
                if (await act("PUT", "/admin/mcp", { enabled, servers })) await state.reload();
              }}><Icon name="save" />Save integrations</button></div>
            </div>
          </Card>
          {view.enabled ? (
            <Callout kind="warning" text={"MCP is enabled in this environment's config. Note: the connector is an architecture building block – the "
              + "agents still use the in-memory dependency graph until it is wired in (see below)."} />
          ) : (
            <Callout kind="info" text="MCP disabled – agents use the in-memory dependency graph (default)." />
          )}
          {view.servers.map((s) => (
            <Card key={s.name}>
              <div className="row adm-wrap" style={{ justifyContent: "space-between" }}>
                <div>
                  <Rich text={`**${s.label.replace(" URL", "")}** · \`${s.url || "not configured"}\``} />
                  <div className="row"><Rich text={`Token (\`${s.token_env}\`):`} />
                    {s.token_set ? <><span className="tone-success" style={{ background: "none" }}><Icon name="check" size={18} /></span>set</> : "— not set"}</div>
                </div>
                <button disabled={!s.url} onClick={() => void probe(s.name)}><Icon name="network_check" />Test connection</button>
              </div>
              {probes[s.name] && <div style={{ marginTop: 8 }}><Callout kind={probes[s.name].ok ? "success" : "error"} text={probes[s.name].message} /></div>}
            </Card>
          ))}
          <Details summary="Architecture – how the connector plugs into the agents">
            <Rich text={"`app/crew/tools/mcp_client.py` → `EnterpriseMCPConnector` turns these settings into agent tools (Streamable HTTP, or SSE "
              + "for `…/sse` URLs). Wiring it in is one line per agent:"} />
            <pre className="adm-pre">{"connector = EnterpriseMCPConnector.from_environment()\nagent = Agent(**connector.agent_kwargs(agent_kwargs))  # no-op while MCP is disabled"}</pre>
            <Caption text="Before enabling in PROD: route MCP results through the same RLS filter and PII masking as local data." />
          </Details>
        </div>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ environment staging
const PROMOTION_TEXTS: Record<string, [string, string]> = {
  TEST: ["Promote config to TEST", "Copies all DEV settings into env_test.json (previous TEST config is archived)."],
  PROD: ["Promote TEST to PROD", "Copies all TEST settings into env_prod.json (previous PROD config is archived)."],
};

export function EnvironmentSection() {
  const state = useLoad(async () => ({ view: await api.get<Environment>("/admin/environment"), files: await loadFiles() }));
  const act = useAct();
  const { confirm, notify, signInAgain } = useConsole();
  const [target, setTarget] = useState("");
  return (
    <Loaded state={state}>
      {({ view, files }) => {
        const chosen = target || view.environment;
        return (
          <Card>
            <h2 style={{ marginTop: 0 }}>Environment staging</h2>
            <div className="grid-2" style={{ marginTop: 8 }}>
              <div className="adm-stack">
                <label className="field" title="Switching loads data/env_<environment>.json (models, shadowing, required fields). PROD needs real accounts (AUTH_USERS_FILE).">
                  System Environment (Staging)
                  <select value={chosen} onChange={(e) => setTarget(e.target.value)}>
                    {view.environments.map((e) => <option key={e}>{e}</option>)}
                  </select>
                </label>
                <Caption text={`Active file: \`${files.env_file}\``} />
                {view.demo_accounts_active && <Caption text="PROD needs real accounts: the switch is refused while the demo accounts are active." />}
                <div><button disabled={chosen === view.environment} onClick={async () => {
                  if (!(await confirm("Environment switch", `Switches the system environment from ${view.environment} to ${chosen}: loads its settings `
                    + "file. Every user's sign-in ends – you sign in again afterwards.", `Switch to ${chosen}`))) return;
                  const answer = await act("POST", "/admin/environment", { environment: chosen, confirm: true });
                  if (answer?.kind === "success") { // tokens are bound to the environment: sign in again
                    notify("info", "Your sign-in ended with the environment switch – please sign in again.");
                    session.clear();
                    setTimeout(signInAgain, 2500);
                  } else if (answer) {
                    await state.reload();
                  }
                }}><Icon name="swap_horiz" />Switch environment …</button></div>
              </div>
              <div className="adm-stack">
                <h3 style={{ margin: 0 }}>Promotion</h3>
                <div className="grid">
                  {view.promotions.map((p) => {
                    const [label, help] = PROMOTION_TEXTS[p.target] ?? [`Promote ${p.source} to ${p.target}`, `Copies all ${p.source} settings into the ${p.target} config.`];
                    return (
                      <button key={p.target} title={help} onClick={async () => {
                        if (await confirm(label, `Confirm promotion to ${p.target}. ${help}`)
                          && await act("POST", "/admin/environment/promote", { source: p.source, target: p.target, confirm: true })) await state.reload();
                      }}><Icon name="upgrade" />{label} …</button>
                    );
                  })}
                </div>
              </div>
            </div>
          </Card>
        );
      }}
    </Loaded>
  );
}
