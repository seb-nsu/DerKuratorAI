// Admin Console – logs (analysis runs, business audit trail and decision log with its hash chain, incidents, shadow
// comparisons, system log) and the quality area with the in-app system tests and their HTML report.
import { useEffect, useState } from "react";
import { api, ApiError, type Answer } from "../../api/client";
import { Card, Icon, Kpi } from "../../components/ui";
import { Callout, Caption, Details, Loaded, Pre, Rich, Table, getText, localStamp, useAct, useConsole, useLoad } from "./common";
import { loadFiles } from "./Operations";

type Runs = Answer<"/api/v1/admin/logs/analysis-runs">;
type Lines = Answer<"/api/v1/admin/logs/system">;
type Audit = Answer<"/api/v1/admin/logs/audit">;
type Chain = Answer<"/api/v1/admin/logs/audit/verify-chain", "post">;
type Incidents = Answer<"/api/v1/admin/logs/incidents">;
type Shadow = Answer<"/api/v1/admin/logs/shadow">;
type Tests = Answer<"/api/v1/admin/system-tests">;
type TestRun = Answer<"/api/v1/admin/system-tests/run", "post">;

const AGENT_LABELS: Record<string, string> = { orchestrator: "Orchestrator", data_steward: "Data Steward", analyst: "Analyst",
  simulator: "Simulator", advisory: "Advisory" };
const RUN_COLUMNS: [string, string][] = [["time", "Time"], ["environment", "Environment"], ["role", "Role"], ["mode", "Mode"],
  ["duration_s", "Duration (s)"], ["verdict", "Verdict"], ["plan", "Plan (JSON)"]];

// Several values of one query parameter (?mode=a&mode=b)
const query = (name: string, values: string[], extra: Record<string, string> = {}) =>
  new URLSearchParams([...values.map((v) => [name, v]), ...Object.entries(extra)]).toString();

// A multi-select of a log filter: one checkbox per value
function Filter({ label, values, chosen, onChange }: { label: string; values: string[]; chosen: string[]; onChange: (v: string[]) => void }) {
  return (
    <fieldset className="adm-fieldset">
      <legend className="muted small">{label}</legend>
      <div className="adm-checks">
        {values.map((v) => (
          <label key={v} className="row small">
            <input type="checkbox" style={{ width: "auto" }} checked={chosen.includes(v)}
              onChange={(e) => onChange(e.target.checked ? [...chosen, v] : chosen.filter((x) => x !== v))} />{v}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

// Uniform refresh/clear bar. Clearing needs a confirmation.
function Toolbar({ name, label, help, reload }: { name: string; label: string; help: string; reload: () => Promise<void> }) {
  const act = useAct();
  const { confirm } = useConsole();
  return (
    <div className="row adm-wrap">
      <button onClick={() => void reload()}><Icon name="refresh" />Refresh</button>
      <button className="adm-danger-outline" title={help} onClick={async () => {
        if (await confirm(`Clear ${label}`, help, "Clear") && await act("POST", `/admin/logs/${name}/clear`, { confirm: true })) await reload();
      }}><Icon name="delete" />Clear …</button>
    </div>
  );
}

// ------------------------------------------------------------------ analysis runs
export function RunsSection() {
  const state = useLoad(async () => ({ view: await api.get<Runs>("/admin/logs/analysis-runs"), files: await loadFiles() }));
  const [modes, setModes] = useState<string[] | null>(null);
  const [rows, setRows] = useState<Runs["runs"] | null>(null);
  useEffect(() => {
    if (modes === null) return;
    if (!modes.length) return setRows([]);
    void api.get<Runs>(`/admin/logs/analysis-runs?${query("mode", modes)}`).then((v) => setRows(v.runs));
  }, [modes]);
  return (
    <Loaded state={state}>
      {({ view, files }) => (
        <div className="adm-stack">
          <h2>Analysis runs</h2>
          <Caption text={`Source: \`${files.audit_log}\` (event \`analysis_run\`) – role, run mode, duration, rule-based verdict and the plan the pipeline executed.`} />
          {view.runs.length === 0 ? <Callout kind="info" text="No analyses recorded yet." /> : (
            <>
              <Filter label="Mode" values={view.modes} chosen={modes ?? view.modes} onChange={setModes} />
              <Table rows={rows ?? view.runs} columns={RUN_COLUMNS} />
              {view.median_s != null && (
                <Caption text={`${view.total} run(s) · median ${view.median_s.toFixed(1)} s · p95 ${(view.p95_s ?? 0).toFixed(1)} s`} />
              )}
            </>
          )}
        </div>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ audit logs
const FRONT = ["timestamp", "environment", "event", "role", "user_id", "request", "applied"];

export function AuditSection() {
  const state = useLoad(async () => ({
    files: await loadFiles(),
    log: await api.get<Audit>("/admin/logs/audit", { limit: "1000" }),
    business: await api.get<Lines>("/admin/logs/business-audit").then((v) => ({ lines: v.lines, error: null as string | null }),
      (e) => { if (e instanceof ApiError && e.status === 401) throw e; return { lines: [] as string[], error: String(e.message ?? e) }; }),
  }));
  const [chain, setChain] = useState<Chain | string | null>(null);
  const [events, setEvents] = useState<string[] | null>(null);
  const [filtered, setFiltered] = useState<Audit | null>(null);
  const { signInAgain } = useConsole();
  useEffect(() => {
    if (events === null) return setFiltered(null);
    if (!events.length) return setFiltered({ records: [], total: 0, events: [], malformed: 0 });
    void api.get<Audit>(`/admin/logs/audit?${query("event", events, { limit: "1000" })}`).then(setFiltered);
  }, [events]);
  const verify = async () => {
    try {
      setChain(await api.post<Chain>("/admin/logs/audit/verify-chain"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return signInAgain();
      setChain(e instanceof Error ? e.message : String(e));
    }
  };
  const reload = async () => {
    setEvents(null);
    setChain(null);
    await state.reload();
  };
  return (
    <Loaded state={state}>
      {({ files, log, business }) => {
        const shown = filtered ?? log;
        return (
          <div className="adm-stack">
            <Details summary="Business audit trail (project master-data edits)">
              <Caption text={`Source: \`${files.business_audit_log}\` – one line per changed field.`} />
              {business.error ? <Callout kind="error" text={business.error} />
                : business.lines.length ? <Pre text={business.lines.join("\n")} /> : <Callout kind="info" text="No entries" />}
            </Details>
            <h2>Audit logs</h2>
            <Caption text={`Source: \`${files.audit_log}\` – approved scenarios, project edits and admin changes (no secrets).`} />
            <Toolbar name="audit" label="audit log" help="The content is copied to data/archive/ before clearing." reload={reload} />
            <div><button onClick={() => void verify()}><Icon name="link" />Verify the hash chain</button></div>
            {typeof chain === "string" && <Callout kind="error" text={chain} />}
            {chain && typeof chain === "object" && (() => {
              const legacy = chain.unchained ? ` · ${chain.unchained} older record(s) from before the chain` : "";
              return chain.ok ? <Callout kind="success" text={`Hash chain intact: ${chain.chained} chained record(s)${legacy}.`} />
                : <Callout kind="error" text={`Hash chain broken at line ${chain.broken_at}: ${chain.reason}. Compare with the latest archive or snapshot before trusting later records.`} />;
            })()}
            {!log.total && !log.events.length ? <Callout kind="info" text="No entries" /> : (
              <>
                {log.malformed > 0 && <Callout kind="warning" text={`${log.malformed} corrupt line(s) skipped.`} />}
                <Filter label="Event type" values={log.events} chosen={events ?? log.events} onChange={setEvents} />
                <Table rows={shown.records} front={FRONT} />
                <Caption text={`${shown.total} entries`} />
              </>
            )}
          </div>
        );
      }}
    </Loaded>
  );
}

// ------------------------------------------------------------------ incidents
export function IncidentsSection() {
  const state = useLoad(async () => ({ view: await api.get<Incidents>("/admin/logs/incidents"), files: await loadFiles() }));
  return (
    <Loaded state={state}>
      {({ view, files }) => (
        <div className="adm-stack">
          <h2>Incidents</h2>
          <Caption text={`Source: \`${files.incidents_log}\` – incident ID, section and error type; the traceback is in the system log under the same ID.`} />
          {view.malformed > 0 && <Callout kind="warning" text={`${view.malformed} corrupt line(s) skipped.`} />}
          {view.incidents.length === 0 ? <Callout kind="info" text="No incidents recorded." />
            : <Table rows={view.incidents} front={["timestamp", "incident_id", "event", "section", "error_type", "role", "user_id"]} />}
        </div>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ shadow logs
type Side = { result?: unknown; models?: unknown; model?: string; duration_s?: unknown; error?: string };
const side = (entry: Record<string, unknown>, key: string): Side => {
  const value = entry[key];
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Side) : { result: value };
};

export function ShadowSection() {
  const state = useLoad(async () => ({ view: await api.get<Shadow>("/admin/logs/shadow"), files: await loadFiles() }));
  return (
    <Loaded state={state}>
      {({ view, files }) => (
        <div className="adm-stack">
          <h2>Shadow logs</h2>
          <Caption text={`Source: \`${files.shadow_log}\` – main run vs. shadow run (per-agent shadow models) per request.`} />
          <Details summary="Current per-agent shadow configuration">
            <Table rows={view.matrix} columns={[["agent", "agent"], ["main", "main"], ["shadow", "shadow"]]} />
            <Caption text={view.shadow_available ? "Shadowing active." : "Shadowing inactive (no shadow model set or keys missing) – configure it in Settings."} />
          </Details>
          <Toolbar name="shadow" label="shadow log" help="The content is copied to data/archive/ before clearing." reload={state.reload} />
          {view.entries.length === 0 ? <Callout kind="info" text="No entries" /> : (
            <>
              {view.malformed > 0 && <Callout kind="warning" text={`${view.malformed} corrupt line(s) skipped.`} />}
              {view.entries.map((entry, n) => {
                const primary = side(entry, "primary"), shadow = side(entry, "shadow");
                const pt = primary.duration_s, st = shadow.duration_s;
                const delta = typeof pt === "number" && typeof st === "number" ? ` · Δ ${st - pt >= 0 ? "+" : ""}${(st - pt).toFixed(1)}s` : "";
                const match = entry.verdict_match;
                const badge = match === true ? " · ✓ same verdict" : match === false ? " · ⚠ verdicts differ" : "";
                const ds = entry.data_steward_shadow as Record<string, unknown> | undefined;
                return (
                  <Details key={n} summary={`${entry.timestamp ?? "?"} · ${String(entry.request ?? "–").slice(0, 80)}${delta}${badge}`}>
                    <Rich text={`**Request:** ${entry.request ?? "–"}`} />
                    <div className="grid-2" style={{ marginTop: 8 }}>
                      {([["Main run", primary, entry.verdict_primary], ["Shadow run", shadow, entry.verdict_shadow]] as [string, Side, unknown][]).map(([label, data, verdict]) => (
                        <div key={label} className="adm-stack">
                          <Rich text={`**${label}**` + (verdict ? ` · recommendation: **${verdict}**` : "")} />
                          {data.models && typeof data.models === "object" ? (
                            <Table rows={Object.entries(data.models as Record<string, string>).map(([a, m]) => ({ agent: AGENT_LABELS[a] ?? a, model: m }))}
                              columns={[["agent", "agent"], ["model", "model"]]} />
                          ) : <Caption text={`Model: \`${data.model ?? "n/a"}\``} />}
                          <Kpi label="Runtime" value={typeof data.duration_s === "number" ? `${data.duration_s.toFixed(2)} s` : "–"} />
                          {data.error && <Callout kind="error" text={String(data.error)} />}
                          <div className="adm-scroll" tabIndex={0}>{String(data.result || "no result")}</div>
                        </div>
                      ))}
                    </div>
                    {ds && (
                      <div style={{ marginTop: 8 }}><Card>
                        <Rich text={`**Data Steward shadow interpretation** (\`${ds.model}\`, ${ds.source})`} />
                        <div style={{ whiteSpace: "pre-wrap" }}>{String(ds.interpretation ?? "")}</div>
                      </Card></div>
                    )}
                  </Details>
                );
              })}
            </>
          )}
        </div>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ system logs
const LEVELS = ["ALL", "INFO", "WARNING", "ERROR"];

export function SystemLogSection() {
  const state = useLoad(async () => ({ lines: (await api.get<Lines>("/admin/logs/system")).lines, files: await loadFiles() }));
  const [level, setLevel] = useState("ALL");
  const [filtered, setFiltered] = useState<string[] | null>(null);
  useEffect(() => {
    if (level === "ALL") return setFiltered(null);
    void api.get<Lines>("/admin/logs/system", { level }).then((v) => setFiltered(v.lines));
  }, [level]);
  return (
    <Loaded state={state}>
      {({ lines, files }) => (
        <div className="adm-stack">
          <h2>System logs</h2>
          <Caption text={`Source: \`${files.system_log}\` – last 100 lines, newest first.`} />
          <Toolbar name="system" label="system log" help="Clears data/app.log (the clearing itself is logged)." reload={async () => { setLevel("ALL"); await state.reload(); }} />
          {lines.length === 0 ? <Callout kind="info" text="No entries" /> : (
            <>
              <div className="adm-segmented" role="radiogroup" aria-label="Level">
                {LEVELS.map((l) => (
                  <button key={l} role="radio" aria-checked={level === l} className={level === l ? "primary" : ""} onClick={() => setLevel(l)}>{l}</button>
                ))}
              </div>
              <Pre text={(filtered ?? lines).join("\n") || "(no lines for this filter)"} />
            </>
          )}
        </div>
      )}
    </Loaded>
  );
}

// ------------------------------------------------------------------ system tests
function TestResults({ counts, cases, header }: { counts: Record<string, number>; cases: TestRun["cases"]; header: string }) {
  if (!Object.keys(counts).length) return null;
  const order: Record<string, number> = { failed: 0, error: 1, skipped: 2, passed: 3 };
  const rows = [...cases].sort((a, b) => (order[a.outcome] ?? 4) - (order[b.outcome] ?? 4) || a.module.localeCompare(b.module) || a.test.localeCompare(b.test));
  return (
    <>
      <Caption text={header} />
      <div className="grid">
        <Kpi label="Passed" value={String(counts.passed ?? 0)} />
        <Kpi label="Failed" value={String(counts.failed ?? 0)} />
        <Kpi label="Errors" value={String(counts.error ?? 0)} />
        <Kpi label="Skipped" value={String(counts.skipped ?? 0)} />
      </div>
      {rows.length > 0 && (
        <Table rows={rows.map((c) => ({ ...c, duration_s: c.duration_s.toFixed(3) }))}
          columns={[["module", "module"], ["test", "test"], ["outcome", "Outcome"], ["duration_s", "Duration (s)"], ["message", "message"]]} />
      )}
    </>
  );
}

let lastRun: TestRun | null = null; // the run of this page visit

export function TestsSection() {
  const state = useLoad(async () => ({ view: await api.get<Tests>("/admin/system-tests"), files: await loadFiles() }));
  const { signInAgain } = useConsole();
  const [run, setRun] = useState<TestRun | null>(lastRun);
  const [busy, setBusy] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const start = async () => {
    setBusy(true);
    setWarning(null);
    try {
      lastRun = await api.post<TestRun>("/admin/system-tests/run");
      setRun(lastRun);
      setReport(null);
      await state.reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return signInAgain();
      setWarning(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const loadReport = async (): Promise<string | null> => {
    if (report) return report;
    try {
      const html = await getText("/admin/system-tests/report");
      setReport(html);
      return html;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) signInAgain();
      else setReportError(`Test report could not be read: ${e instanceof Error ? e.message : String(e)}`);
      return null;
    }
  };
  const download = async () => {
    const html = await loadReport();
    if (!html) return;
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "test_report.html";
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <Loaded state={state}>
      {({ view, files }) => (
        <div className="adm-stack">
          <h2>System tests</h2>
          <Caption text={"Runs the pytest suite (security, simulator guardrails, Data Steward, config, admin console) on a deterministic mock "
            + "portfolio – no network access; logs, decisions and proposals of the run go to a temporary folder, so the live data stays unchanged."} />
          {!view.enabled && <Callout kind="info" text={"System tests are disabled on this instance (FORCE_OFFLINE_MODE, e.g. the hosted demo). Run them "
            + "locally with `python run_tests.py`."} />}
          <div><button className="primary" disabled={!view.enabled || busy} aria-busy={busy} onClick={() => void start()}>
            {busy ? "Running tests..." : "Run system tests now"}</button></div>
          {warning && <Callout kind="warning" text={warning} />}
          {run ? (
            <>
              <Callout kind={run.passed ? "success" : "error"} text={`${run.status} (exit code ${run.returncode}) · duration ${run.duration_s.toFixed(1)} s · started ${run.started_at}`} />
              <TestResults counts={run.counts} cases={run.cases} header="Results of this run" />
              <Details summary="Console output">
                <Pre text={run.stdout || "(no output on stdout)"} />
                {run.stderr.trim() && <><strong>stderr</strong><Pre text={run.stderr} /></>}
              </Details>
            </>
          ) : view.last_run && (
            <TestResults counts={view.last_run.counts} cases={view.last_run.cases} header={`Last saved run: ${localStamp(view.last_run.saved_at)}`} />
          )}
          <hr className="adm-hr" />
          <h3>HTML test report (pytest-html)</h3>
          {!view.report_saved_at ? (
            <Callout kind="info" text="No test report yet – click 'Run system tests now' (or run `python run_tests.py`)." />
          ) : (
            <>
              <Caption text={`As of: ${localStamp(view.report_saved_at)} · file: \`${files.test_report}\``} />
              <div><button onClick={() => void download()}><Icon name="download" />Download test report (HTML)</button></div>
              {reportError && <Callout kind="warning" text={reportError} />}
              <Details summary="Show test report" onOpen={() => void loadReport()}>
                {/* the report renders its table with its own JS and CSS, kept isolated in the frame */}
                {report ? <iframe title="Test report" srcDoc={report} className="adm-frame" sandbox="allow-scripts" />
                  : <div className="muted" aria-busy="true">Loading …</div>}
              </Details>
            </>
          )}
        </div>
      )}
    </Loaded>
  );
}
