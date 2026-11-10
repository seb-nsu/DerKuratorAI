// Admin Console – system status (with ServiceNow and the write journal) and the operations area: FinOps, snapshots
// with the data lifecycle (demo reset, purge of a user), notifications and the database management.
import { useState } from "react";
import { api, type Answer } from "../../api/client";
import { Card, Icon, Kpi } from "../../components/ui";
import { Callout, Caption, Details, Loaded, Rich, Table, fmt, stamp, useAct, useConsole, useLoad } from "./common";

type Status = Answer<"/api/v1/admin/status">;
export type Files = Answer<"/api/v1/admin/files">;
type Calls = Answer<"/api/v1/admin/logs/llm-calls">;
type ServiceNow = Answer<"/api/v1/admin/servicenow">;
type Journal = Answer<"/api/v1/admin/journal">;
type FinOps = Answer<"/api/v1/admin/finops">;
type Notifications = Answer<"/api/v1/admin/notifications">;
type Snapshots = Answer<"/api/v1/admin/snapshots">;
type Database = Answer<"/api/v1/admin/database">;

export const loadFiles = () => api.get<Files>("/admin/files");

// ------------------------------------------------------------------ system status
export function SystemStatus() {
  const state = useLoad(async () => ({
    status: await api.get<Status>("/admin/status"),
    files: await loadFiles(),
    calls: await api.get<Calls>("/admin/logs/llm-calls"),
  }));
  return (
    <Loaded state={state}>
      {({ status, files, calls }) => (
        <div className="adm-stack">
          <div className="grid">
            <Kpi label="Environment" value={status.environment} caption={`Data source: ${status.data_source}`} help={`Data source: ${status.data_source}`} />
            <Kpi label="PII masking" value={status.pii_masking_enabled ? "On" : "OFF"} />
            <Kpi label="LLM agents" value={status.llm_available ? "Active" : "Offline"} help="Offline = deterministic rule-based parser" />
            <Kpi label="Dependency graph" value={status.graph.ok ? "Ready" : "Unavailable"} help={status.graph.message} />
            <Kpi label="OpenTelemetry export" value={status.tracing.enabled ? "Active" : "Off"} help={status.tracing.message}
              caption={status.tracing.endpoint ? `Collector: ${status.tracing.endpoint}` : undefined} />
          </div>
          <Callout kind={status.graph.ok ? "success" : "warning"} text={`Dependency graph: ${status.graph.message}`} />
          <Callout kind={status.tracing.enabled ? "success" : "info"} text={status.tracing.message} />
          {status.llm_available ? (
            <Callout kind="success" text="LLM agents: active" />
          ) : (
            <Callout kind="warning" text={"LLM agents: offline mode" + (status.missing_keys.length
              ? ` – missing keys or EU endpoints for the selected models: ${status.missing_keys.join(", ")}` : "")} />
          )}
          {!status.pii_masking_enabled && (
            <Callout kind="error" text="PII masking is disabled: real names (PM, sponsor, assignees, stakeholders) are sent to LLM providers." />
          )}
          <h3>LLM routing</h3>
          <Table rows={status.routing} columns={[["agent_role", "Agent role"], ["model", "Model"], ["shadow", "Shadow"]]} />
          <Caption text={`Temperature ${status.temperature.toFixed(2)} · Data Steward checks → deterministic`} />
          <Rich text={`**LLM calls** · last ${calls.window} calls of this process (log: \`${files.llm_calls_log}\`, no prompt or answer text)`} />
          {calls.summary.length === 0 ? (
            <Caption text="No LLM call yet." />
          ) : (
            <>
              <Table rows={calls.summary} columns={[["caller", "Caller"], ["model", "Model"], ["calls", "Calls"], ["errors", "Errors"],
                ["median_s", "Median s"], ["tokens", "Tokens"], ["cost_usd", "Cost USD"]]} />
              <Details summary="Last calls"><Table rows={calls.recent} /></Details>
            </>
          )}
          <Rich text={`**Data residency** · \`LLM_DATA_RESIDENCY=${status.data_residency}\``} />
          <Table rows={status.endpoints} columns={[["provider", "Provider"], ["endpoint", "Endpoint"], ["residency", "Under the residency setting"]]} />
          <Caption text={"Set in `.env` or the secrets: `LLM_DATA_RESIDENCY=eu` calls only providers with an own endpoint "
            + "(`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` – EU gateway, Azure OpenAI or a local model server); the app cannot check "
            + "an endpoint's region, the setting states it. See docs/DATA_PROTECTION.md."} />
          <h3>Sign-in</h3>
          <Table rows={status.auth} columns={[["setting", "Setting"], ["value", "Value"]]} />
          <Caption text={`Section "auth" of \`${files.env_file}\` (read-only here).`} />
          <h3>Graph backend</h3>
          {status.graph_backend.warning && <Callout kind="warning" text={status.graph_backend.warning} />}
          <Table rows={status.graph_backend.rows} columns={[["setting", "Setting"], ["value", "Value"]]} />
          <Caption text={status.graph_backend.note} />
          <ServiceNowSync />
          <WriteJournal />
        </div>
      )}
    </Loaded>
  );
}

function ServiceNowSync() {
  const state = useLoad(() => api.get<ServiceNow>("/admin/servicenow"));
  const act = useAct();
  const { confirm } = useConsole();
  const sync = async (full: boolean) => {
    if (full && !(await confirm("Full sync", "Reads every ServiceNow table completely and replaces the local snapshot. This takes "
      + "much longer than a change sync and counts against the instance's API limits."))) return;
    if (await act("POST", "/admin/servicenow/sync", full ? { full: true, confirm: true } : { full: false })) await state.reload();
  };
  return (
    <Loaded state={state}>
      {(view) => view.error ? <Callout kind="error" text={`ServiceNow: ${view.error}`} /> : !view.configured ? null : (
        <>
          <h3>ServiceNow</h3>
          <Table rows={view.tables} columns={[["instance", "Instance"], ["table", "Table"], ["rows", "Rows"], ["changed_up_to", "Changed up to"], ["last_sync", "Last sync"]]} />
          <div className="grid">
            <button title="Reads only the records changed since the last sync." onClick={() => void sync(false)}><Icon name="sync" />Sync ServiceNow (changes)</button>
            <button title="Reads every table completely (after a mapping change)." onClick={() => void sync(true)}><Icon name="cloud_download" />Full sync …</button>
          </div>
          <Caption text={"Read-only: approved changes stay in DerKurator's own store and are never written to ServiceNow. Credentials "
            + "come from the secrets SERVICENOW_INSTANCE, SERVICENOW_USER/SERVICENOW_PASSWORD or SERVICENOW_TOKEN."} />
        </>
      )}
    </Loaded>
  );
}

function WriteJournal() {
  const state = useLoad(() => api.get<Journal>("/admin/journal"));
  const act = useAct();
  const [comments, setComments] = useState<Record<string, string>>({});
  return (
    <>
      <h3>Write journal</h3>
      <Loaded state={state}>
        {({ findings }) => findings.length === 0 ? (
          <Callout kind="success" text="Every data write has its intent and committed record, and every written proposal its decision." />
        ) : (
          <>
            <Callout kind="error" text={`${findings.length} write journal finding(s) – check the data against the snapshot, then mark as resolved.`} />
            {findings.map((f) => {
              const key = `${f.operation_id}|${f.kind}`;
              const comment = comments[key] ?? "";
              return (
                <Card key={key}>
                  <div className="error"><Rich text={`**${f.label}** · operation \`${f.operation_id}\` · snapshot \`${f.snapshot_id || "–"}\` · proposal `
                    + `\`${f.proposal_id || "–"}\` · ${f.user_id || "–"} · ${stamp(f.timestamp)}`} /></div>
                  <Caption text={f.detail || ""} />
                  <div className="row adm-wrap" style={{ alignItems: "flex-end", marginTop: 8 }}>
                    <label className="field" style={{ flex: 1, minWidth: 200 }}>Comment (required)
                      <input value={comment} onChange={(e) => setComments({ ...comments, [key]: e.target.value })} />
                    </label>
                    <button disabled={!comment.trim()} onClick={async () => {
                      if (await act("POST", "/admin/journal/resolve", { operation_id: f.operation_id, kind: f.kind, comment: comment.trim() })) await state.reload();
                    }}>Mark as resolved</button>
                  </div>
                </Card>
              );
            })}
          </>
        )}
      </Loaded>
    </>
  );
}

// ------------------------------------------------------------------ FinOps
export function FinOpsSection() {
  const state = useLoad(() => api.get<FinOps>("/admin/finops"));
  const act = useAct();
  const { confirm } = useConsole();
  const reset = async () => {
    if (await confirm("Reset counters", "Sets the cost, token and call counters back to zero. The per-call log file stays.")
      && await act("POST", "/admin/finops/reset", { confirm: true })) await state.reload();
  };
  return (
    <Loaded state={state}>
      {(usage) => (
        <Card>
          <h2 style={{ marginTop: 0 }}>FinOps – estimated LLM costs</h2>
          <div className="grid" style={{ marginTop: 8 }}>
            <Kpi label="Estimated costs (USD)" value={`$${usage.cost_usd.toFixed(4)}`} help={"Since app start / last reset, all users incl. "
              + "background shadow runs. List prices from supported_models.json (input/output_cost_per_1m) – an estimate, not an invoice."} />
            <Kpi label="Tokens" value={fmt(usage.total_tokens)} help={`${fmt(usage.prompt_tokens)} in · ${fmt(usage.completion_tokens)} out`} />
            <Kpi label="LLM calls" value={String(usage.calls)} />
            <div className="row"><button onClick={() => void reset()}><Icon name="restart_alt" />Reset counters …</button></div>
          </div>
          <div className="adm-stack" style={{ marginTop: 12 }}>
            {usage.unpriced_models.length > 0 && (
              <Callout kind="warning" text={`No price configured for: ${usage.unpriced_models.join(", ")} – their costs are not included.`} />
            )}
            {usage.by_model.length > 0 && (
              <Table rows={usage.by_model.map((r) => ({ ...r, thinking: r.thinking ? "yes" : "", cost_usd: `$${r.cost_usd.toFixed(4)}` }))}
                columns={[["model", "model"], ["thinking", "thinking"], ["calls", "calls"], ["prompt_tokens", "prompt_tokens"],
                  ["completion_tokens", "completion_tokens"], ["cost_usd", "Cost (USD)"]]} />
            )}
            {usage.thinking_share >= 0.5 && (
              <div className="row muted small"><Icon name="psychology" size={18} />
                Thinking models cause {Math.round(usage.thinking_share * 100)}% of the estimated costs – consider a cheaper main model for routine agents (e.g. Orchestrator).
              </div>
            )}
          </div>
        </Card>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ notifications
const NOTIFY_TEXT = "Mail (SMTP) and Microsoft Teams messages about the approval inbox: a proposal waiting for a decision, an overdue or "
  + "expired proposal, the decision back to the requester. Set in `.env` or the secrets (`NOTIFY_*`, docs/PILOT_OPERATIONS.md); "
  + "secrets are never shown here, and the outbox only in counts.";

export function NotificationsSection() {
  const state = useLoad(() => api.get<Notifications>("/admin/notifications"));
  const act = useAct();
  const [to, setTo] = useState("");
  const test = async () => {
    if (await act("POST", "/admin/notifications/test", { to: to.trim() })) await state.reload();
  };
  return (
    <Loaded state={state}>
      {(view) => (
        <Card>
          <h2 style={{ marginTop: 0 }}>Notifications</h2>
          <Caption text={NOTIFY_TEXT} />
          <div className="grid" style={{ marginTop: 8 }}>
            <Kpi label="Notifications" value={view.enabled ? "On" : "Off"} />
            <Kpi label="Sent" value={String(view.sent)} help={view.last_sent_at ? `Last sent ${stamp(view.last_sent_at)}` : undefined} />
            <Kpi label="Waiting" value={String(view.queued)} help={`${view.retrying} retrying · ${view.events_waiting} event(s) not resolved yet`} />
            <Kpi label="Failed" value={String(view.failed)} help="Given up after the last attempt" />
          </div>
          <div className="adm-stack" style={{ marginTop: 12 }}>
            {!view.enabled && <Callout kind="info" text="Notifications are off – set `NOTIFY_ENABLED=true` and at least one channel." />}
            {view.last_error && (
              <Callout kind="warning" text={`Last delivery error (${stamp(view.last_error.at)}, ${view.last_error.channel}): ${view.last_error.error}`} />
            )}
            <Table rows={view.channels} columns={[["channel", "Channel"], ["status", "Status"]]} />
            <div className="row" style={{ alignItems: "flex-end", gap: 8 }}>
              <label className="field" style={{ flex: 1, minWidth: 200 }}>Test recipient (mail)
                <input value={to} placeholder="empty: the role mailboxes" onChange={(e) => setTo(e.target.value)} />
              </label>
              <button onClick={() => void test()}><Icon name="send" />Send test notification</button>
            </div>
            <Caption text={"Without SSO the addresses come from the `email` of the accounts in the user file and the role mailboxes; "
              + "SSO will supply them later."} />
          </div>
        </Card>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ snapshots and data lifecycle
const RESET_TEXT = "Puts the portfolio back to the state this instance started with and archives open proposals, chats and saved "
  + "what-ifs (data/archive/). The current state is saved as a snapshot first.";
const PURGE_TEXT = "Erasure request: removes the user's chats and saved what-ifs and replaces the user id in proposals and archives "
  + "by a pseudonym. The decision log and the snapshots stay – they are approval evidence.";
const RESTORE_TEXT = "Restoring replaces the complete data source with this snapshot – all later changes are undone. The current state "
  + "is saved as a new snapshot first; the graph database is rebuilt.";

export function SnapshotsSection() {
  const state = useLoad(async () => ({ view: await api.get<Snapshots>("/admin/snapshots"), files: await loadFiles() }));
  const act = useAct();
  const { confirm } = useConsole();
  const [selected, setSelected] = useState<string>("");
  const [userId, setUserId] = useState("");
  const run = async (path: string, body: unknown = {}) => {
    if (await act("POST", path, body)) await state.reload();
  };
  return (
    <Loaded state={state}>
      {({ view, files }) => {
        const snaps = view.snapshots;
        const label = (s: (typeof snaps)[number]) => `${String(s.created_at ?? "").replace("T", " ")} · ${s.reason} · ${s.user || "–"} (${s.id})`;
        const chosen = snaps.find((s) => s.id === selected) ?? snaps[0];
        return (
          <div className="adm-stack">
            <h2>Snapshots</h2>
            <Caption text={`A copy of the data source is saved to \`${files.snapshot_dir}\` before every write (Approve & Apply, project edits, `
              + `restores). The newest ${view.max_snapshots} are kept. Active source: \`${files.data_source ?? "n/a"}\`.`} />
            <div><button disabled={view.source === null} onClick={() => void run("/admin/snapshots")}><Icon name="photo_camera" />Create snapshot now</button></div>
            {snaps.length === 0 ? (
              <Callout kind="info" text="No snapshots yet – the first one is created with the next data change." />
            ) : (
              <>
                <Table pageSize={10} rows={snaps.map((s) => ({ id: s.id, created: stamp(s.created_at), reason: s.reason, user: s.user || "–",
                  env: s.environment, changes: s.changes.length, size_kb: s.size_kb }))} />
                <Card>
                  <h3 style={{ marginTop: 0 }}>Restore</h3>
                  <label className="field">Snapshot
                    <select value={chosen.id} onChange={(e) => setSelected(e.target.value)}>
                      {snaps.map((s) => <option key={s.id} value={s.id}>{label(s)}</option>)}
                    </select>
                  </label>
                  {chosen.changes.length > 0 && (
                    <Details summary={`Changes written after this snapshot (${chosen.changes.length})`}>
                      <Table rows={chosen.changes} columns={["project_id", "table", "field", "old", "new", "source_tool"].map((k) => [k, k])} />
                    </Details>
                  )}
                  <div style={{ margin: "12px 0" }}><Callout kind="warning" text={RESTORE_TEXT} /></div>
                  <button className="primary" onClick={async () => {
                    if (await confirm("Restore snapshot", `I want to restore this snapshot: ${label(chosen)}. ${RESTORE_TEXT}`))
                      await run(`/admin/snapshots/${encodeURIComponent(chosen.id)}/restore`, { confirm: true });
                  }}><Icon name="restore" />Restore snapshot …</button>
                </Card>
              </>
            )}
            {view.demo_reset_available && (
              <>
                <h2>Reset demo data</h2>
                <Caption text={RESET_TEXT} />
                <div><button onClick={async () => {
                  if (await confirm("Reset demo data", `I want to reset the demo data. ${RESET_TEXT}`)) await run("/admin/demo-reset", { confirm: true });
                }}><Icon name="settings_backup_restore" />Reset demo data …</button></div>
              </>
            )}
            <h2>Purge user</h2>
            <Caption text={PURGE_TEXT} />
            <label className="field" style={{ maxWidth: 420 }}>User id
              <input value={userId} placeholder="exactly as in the user file" onChange={(e) => setUserId(e.target.value)} />
            </label>
            <div><button disabled={!userId.trim()} onClick={async () => {
              const target = userId.trim();
              if (await confirm("Purge user", `I want to purge the personal data of ${target}. ${PURGE_TEXT}`)) {
                if (await act("POST", "/admin/purge-user", { user_id: target, confirm: true })) {
                  setUserId("");
                  await state.reload();
                }
              }
            }}><Icon name="person_remove" />Purge user …</button></div>
          </div>
        );
      }}
    </Loaded>
  );
}

// ------------------------------------------------------------------ database
export function DatabaseSection() {
  const state = useLoad(() => api.get<Database>("/admin/database"));
  const act = useAct();
  const [busy, setBusy] = useState(false);
  return (
    <Loaded state={state}>
      {(view) => {
        const source = ({ json: "JSON", excel: "Excel", mapped: "the mapped sources" } as Record<string, string>)[view.data_source] ?? view.data_source;
        const meta = view.graph;
        const report = view.import_report;
        return (
          <Card>
            <div className="adm-stack">
              <h2 style={{ marginTop: 0 }}>Database management</h2>
              <Caption text={`Dependency graph (NetworkX, in memory) · source: ${source}. The app builds it on first use and rebuilds it `
                + "automatically when the source data changes – the button forces an immediate rebuild."} />
              {meta === null ? (
                <Caption text="The graph has not been built in this process yet – it is built on the first analysis." />
              ) : (
                <div className="row small muted"><span className="tone-success" style={{ background: "none" }}><Icon name="check_circle" size={18} /></span>
                  Built {stamp(meta.built_at)} in {meta.build_seconds} s (source: {meta.source || "?"}) · {meta.counts.Project ?? 0} projects,{" "}
                  {meta.counts.Task ?? 0} tasks, {meta.counts.Team ?? 0} teams, {meta.counts.Risk ?? 0} risks, {meta.counts.DEPENDS_ON ?? 0} project dependencies
                </div>
              )}
              {view.import_report_error && <Callout kind="error" text={`The data source could not be loaded: ${view.import_report_error}`} />}
              {report && (
                <Details icon="fact_check" summary={`Import report · ${report.mapping} · ${report.findings} finding(s)`}>
                  <Table rows={report.tables} columns={[["table", "Table"], ["source", "Source"], ["read", "Read"], ["loaded", "Loaded"],
                    ["skipped", "Skipped"], ["notes", "Notes"]]} />
                  {report.notes.map((n, i) => <Caption key={i} text={`${n.table}: ${n.note}`} />)}
                  <Caption text="Findings also appear in the Data Steward report. Mapping files: docs/DATA_MAPPING.md" />
                </Details>
              )}
              <div><button disabled={busy} aria-busy={busy} onClick={async () => {
                setBusy(true);
                if (await act("POST", "/admin/database/rebuild-graph")) await state.reload();
                setBusy(false);
              }}><Icon name="restart_alt" />{busy ? "Rebuilding the dependency graph …" : "Rebuild dependency graph"}</button></div>
            </div>
          </Card>
        );
      }}
    </Loaded>
  );
}
