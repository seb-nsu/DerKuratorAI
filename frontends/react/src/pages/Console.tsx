// Steering Console: guided scenario builder or free text (with scenario memory and the PMO's forced simulation), the
// run of a what-if, the analysis history and the report viewer with the actions on a result (export, save as what-if,
// submit for approval, approve & apply or hand over). Same texts and rules as the NiceGUI console; every rule and
// figure comes from the API. Deep link: /console?action=<guided action>&project=<id> opens that guided scenario.
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { components } from "../api/schema";
import { Badge, ErrorBox, Icon, PageHeader } from "../components/ui";
import { PageLink, scopeText, useWorkspace } from "../components/Shell";
import { CONSOLE_REQUEST_KEY } from "../components/AskBox"; // the question box hands a request over to the console
import { DataTable, ExportMenu, Markdown, Popover, ReportViewer, toneOf, TONE_ICONS, type RunReport } from "../components/ReportViewer";
import { tone } from "../theme";
import { Builder, type Catalog, type RunBody } from "./console/Builder";
import "./console/console.css";

type S = components["schemas"];
type Chat = S["ChatHistory"];
type Message = S["ChatMessage"];
type Toast = { kind: "success" | "warning" | "error" | "info"; text: string };

const HANDOVER_TEXT = "Nothing is written; the resource change is recorded for manual execution.";
const ACTIONS: Record<string, [string, string]> = {
  apply: ["Approve & apply", "task_alt"],
  handover: ["Approve & record hand-over", "assignment_turned_in"],
  submit: ["Submit for approval", "send"],
};
const FOUR_EYES = "Budget, schedule, phase and stop changes need the approval of a PMO other than the requester (four-eyes principle).";

const runsOf = (messages: Message[]) =>
  messages.flatMap((m, i) => (m.role === "assistant" ? [{ asked: messages[i - 1]?.role === "user" ? messages[i - 1] : null, msg: m }] : []));
const requestText = (asked: Message | null, msg: Message) => String(msg.meta?.request || asked?.content || "Analysis").replace("**Scenario:** ", "");
const runKey = (msg: Message) => String(msg.meta?.run_id || msg.id || "");

function takeHandedRequest(): string | undefined {
  try {
    const text = sessionStorage.getItem(CONSOLE_REQUEST_KEY) ?? undefined;
    sessionStorage.removeItem(CONSOLE_REQUEST_KEY);
    return text || undefined;
  } catch {
    return undefined;
  }
}

export function Console() {
  const { info } = useWorkspace();
  const [query] = useSearchParams();
  const [initial] = useState(() => ({ action: query.get("action") ?? undefined, project: query.get("project") ?? undefined, request: takeHandedRequest() }));
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [chat, setChat] = useState<Chat | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<RunReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [force, setForce] = useState(false);
  const [buildOn, setBuildOn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [refresh, setRefresh] = useState(0);
  const continueStep = useRef<number | null | undefined>(undefined);

  const notify = (kind: Toast["kind"], text: string) => setToast({ kind, text });
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), toast.kind === "error" ? 12000 : 6000);
    return () => clearTimeout(timer);
  }, [toast]);

  const loadChat = useCallback(async () => {
    const next = await api.get<Chat>("/steering/chat");
    setChat(next);
    if (next.continuation.step !== continueStep.current) { // a new result -> fresh default for the toggle
      continueStep.current = next.continuation.step;
      setBuildOn(next.continuation.default_continue);
    }
    return next;
  }, []);

  useEffect(() => {
    Promise.all([api.get<Catalog>("/steering/guided").then(setCatalog), loadChat()]).catch((e: Error) => setError(e.message));
  }, [loadChat]);

  const runs = chat ? runsOf(chat.messages) : [];
  const current = runs.find((r) => runKey(r.msg) === selected) ?? runs[runs.length - 1] ?? null;
  const currentRunId = current?.msg.meta?.mode ? current.msg.meta.run_id ?? null : null;

  useEffect(() => {
    setReport(null);
    setReportError(null);
    if (!currentRunId) return;
    let live = true;
    api.get<RunReport>(`/steering/runs/${currentRunId}/report`).then((r) => live && setReport(r), (e: ApiError) => live && setReportError(e.message));
    return () => {
      live = false;
    };
  }, [currentRunId, refresh]);

  const afterChange = async (select?: string) => {
    await loadChat();
    if (select) setSelected(select);
    setRefresh((n) => n + 1);
  };

  const run = async (body: RunBody) => {
    if (busy || !chat) return;
    const sent: Record<string, unknown> = { ...body, force };
    if (buildOn && chat.continuation.available) sent.base_run_id = chat.continuation.base_run_id;
    setBusy(true);
    setRunError(null);
    try {
      const answer = await api.post<{ run_id: string }>("/steering/runs", sent);
      notify("success", "Analysis complete");
      await afterChange(answer.run_id);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (e instanceof ApiError && e.status === 422) notify("warning", message);
      else setRunError(message);
    } finally {
      setBusy(false);
    }
  };

  if (error) return <ErrorBox message={error} />;
  if (!catalog || !chat) return <div className="muted" aria-busy="true">Loading …</div>;
  const scope = info.scope ?? { visible: 0, total: null };

  return (
    <>
      <PageHeader title="Steering Console" subtitle={`Scope: ${scopeText(scope)} · the verdict is rule-based; the AI writes the decision paper`}
        action={<PageLink to="scenarios" icon="compare_arrows" text="Compare saved what-ifs" />} />
      <div className="dk-toast-region" aria-live="polite">
        {toast && (
          <div className={`callout tone-${toast.kind === "error" ? "danger" : toast.kind} row dk-toast`} role={toast.kind === "error" ? "alert" : "status"}>
            <span style={{ flex: 1 }}>{toast.text}</span>
            <button type="button" className="flat" aria-label="Dismiss" onClick={() => setToast(null)}><Icon name="close" /></button>
          </div>
        )}
      </div>
      <div className="dk-console">
        <Rail runs={runs} current={current?.msg ?? null} onOpen={setSelected} onCleared={() => { setSelected(null); void afterChange(); }} notify={notify} />
        <div className="dk-stack">
          <Builder catalog={catalog} continuation={chat.continuation} busy={busy} force={force} setForce={setForce} buildOn={buildOn}
            setBuildOn={setBuildOn} run={(b) => void run(b)} initial={initial} />
          {busy ? (
            <section className="card dk-stack" role="status" aria-live="polite" aria-busy="true">
              <div className="row"><span className="dk-spinner" aria-hidden="true" /><strong>Analysing the what-if ...</strong></div>
              <div className="muted">Checking data quality (deterministic rules) ...</div>
              <div className="muted">Calculating the change · checking dependencies · writing the decision paper ...</div>
              <div className="muted small">The verdict comes first in the report; the decision paper follows below it.</div>
            </section>
          ) : (
            <>
              {runError && <ErrorBox message={`Analysis failed – ${runError}`} />}
              {!current ? (
                <section className="card">
                  <p style={{ marginTop: 0 }}><strong>No analyses yet.</strong> Use the composer above or start with an example:</p>
                  <div className="dk-menu-list">
                    {catalog.examples.map((example) => (
                      <button key={example} type="button" className="flat" style={{ justifyContent: "flex-start", textAlign: "left" }} onClick={() => void run({ request: example })}>
                        <Icon name="arrow_forward" />{example}
                      </button>
                    ))}
                  </div>
                </section>
              ) : !currentRunId ? (
                <section className="card"><Markdown text={current.msg.content} /></section>
              ) : reportError ? <ErrorBox message={reportError} /> : !report ? (
                <div className="muted" aria-busy="true">Loading the report …</div>
              ) : (
                <ReportViewer report={report} ask={{ runId: report.run_id }}
                  actions={<Actions report={report} notify={notify} done={(id) => void afterChange(id)} />} />
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ history rail
function Rail({ runs, current, onOpen, onCleared, notify }: {
  runs: { asked: Message | null; msg: Message }[];
  current: Message | null;
  onOpen: (key: string) => void;
  onCleared: () => void;
  notify: (kind: Toast["kind"], text: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const clear = async () => {
    try {
      await api.delete("/steering/chat");
    } catch (e) {
      notify("error", e instanceof Error ? e.message : String(e));
    }
    dialog.current?.close();
    onCleared();
  };
  return (
    <section className="card dk-rail" aria-label="Analyses">
      <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>Analyses</h2>
      {!runs.length && <div className="muted small">No analyses yet.</div>}
      <div className="dk-stack" style={{ marginTop: 8 }}>
        {[...runs].reverse().map(({ asked, msg }) => {
          const meta = msg.meta;
          const verdict = (meta?.verdict as { label?: string } | null | undefined)?.label;
          const turns = meta?.turns?.length ?? 0;
          const step = meta?.continued && turns ? `Step ${turns} · ` : "";
          const isCurrent = msg === current;
          const badge = msg.status?.badge;
          const t = verdict ? tone("verdict", verdict) : "info";
          return (
            <div key={runKey(msg) || msg.ts} className={`dk-run-item${isCurrent ? " selected" : ""}`}>
              <Badge text={verdict ?? "Analysis"} tone={t} icon={verdict ? TONE_ICONS[t] : "insights"} />
              <div style={{ marginTop: 4 }}>{step}{requestText(asked, msg)}</div>
              <div className="row muted small" style={{ flexWrap: "wrap", marginTop: 4 }}>
                {msg.ts && <span>{msg.ts} ·</span>}
                {badge && <Badge text={badge.label} tone={toneOf(badge.tone)} icon={badge.icon} />}
              </div>
              <button type="button" className="flat" style={{ width: "100%", marginTop: 4 }} disabled={isCurrent}
                aria-label={`${isCurrent ? "Viewing" : "Open"}: ${requestText(asked, msg).slice(0, 80)}`} onClick={() => onOpen(runKey(msg))}>
                {isCurrent ? "Viewing" : "Open"}
              </button>
            </div>
          );
        })}
      </div>
      {runs.length > 0 && (
        <div className="dk-menu-list" style={{ marginTop: 8 }}>
          <button type="button" className="flat" onClick={() => dialog.current?.showModal()}><Icon name="delete" />Clear history</button>
        </div>
      )}
      <dialog ref={dialog} className="dk-dialog" aria-labelledby="dk-clear-title">
        <h2 id="dk-clear-title" style={{ marginTop: 0 }}>Clear analysis history</h2>
        <div className="callout tone-warning"><span style={{ color: "var(--dk-text)" }}>Delete the complete history including all unsaved what-ifs? This cannot be undone.</span></div>
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 16 }}>
          <button type="button" onClick={() => dialog.current?.close()}>Cancel</button>
          <button type="button" className="primary" onClick={() => void clear()}>Yes, clear history</button>
        </div>
      </dialog>
    </section>
  );
}

// ------------------------------------------------------------------ actions of the selected report
function Actions({ report, notify, done }: { report: RunReport; notify: (kind: Toast["kind"], text: string) => void; done: (runId?: string) => void }) {
  const act = report.actions;
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [override, setOverride] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

  const save = async () => {
    try {
      const entry = await api.post<{ name: string }>(`/steering/runs/${report.run_id}/save`, { name: name.trim() });
      notify("success", `What-if '${entry.name}' saved – compare it under What-if comparison.`);
      done();
    } catch (e) {
      const text = message(e);
      notify("error", text.startsWith("What-if not saved") ? text : `What-if not saved: ${text}`);
    }
  };
  const submit = async (justification: string) => {
    try {
      const entry = await api.post<{ id: string }>("/proposals", { run_id: report.run_id, justification });
      notify("success", `Proposal ${entry.id} submitted – the PMO will review it in the approval inbox. You can follow its status under Approvals.`);
      done();
    } catch (e) {
      const text = message(e);
      notify("error", text.startsWith("Proposal not submitted") ? text : `Proposal not submitted: ${text}`);
    }
  };
  const apply = async () => {
    try {
      const outcome = await api.post<{ kind: "success" | "warning"; message: string }>(`/steering/runs/${report.run_id}/apply`,
        { override: act.needs_override && override, comment: act.needs_override ? overrideReason : "" });
      dialog.current?.close();
      notify(outcome.kind, outcome.message);
      done();
    } catch (e) {
      notify("error", message(e));
    }
  };

  const primary = act.primary;
  const [label, icon] = primary ? ACTIONS[primary] : ["", ""];
  const ready = act.primary_enabled && (!act.needs_override || (override && overrideReason.trim().length > 0));
  return (
    <div className="dk-actions">
      <div className="dk-stack-tight">
        <ExportMenu base={`/steering/runs/${report.run_id}`} onError={(m) => notify("error", m)} />
        {act.can_save ? (
          <Popover label="Save as what-if" icon="bookmark_add">
            <form className="dk-stack" onSubmit={(e) => { e.preventDefault(); void save(); }}>
              <label className="field">What-if name<input maxLength={80} placeholder="e.g. A – hard cut" value={name} onChange={(e) => setName(e.target.value)} /></label>
              <button type="submit" className="primary" disabled={!name.trim()}><Icon name="bookmark_add" />Save what-if</button>
            </form>
          </Popover>
        ) : act.saved_as ? <div className="row muted small"><Icon name="bookmark" size={16} />Saved as {act.saved_as}</div> : null}
      </div>
      <div className="dk-stack-tight">
        {act.note && <div className="muted small">{act.note}</div>}
        {primary === "submit" && (
          <>
            {act.needs_override ? (
              <>
                <label className="field">Why submit despite {act.verdict_label}? (required)
                  <input maxLength={2000} placeholder="e.g. Board asked for the cut anyway; risks accepted" value={reason} onChange={(e) => setReason(e.target.value)} />
                </label>
                <button type="button" disabled={!reason.trim()} title={reason.trim() ? undefined : "Give the reason first – the PMO sees it with the proposal."}
                  onClick={() => void submit(reason)}><Icon name={icon} />{label} anyway</button>
              </>
            ) : <button type="button" className="primary" onClick={() => void submit("")}><Icon name={icon} />{label}</button>}
            <div className="row muted small" style={{ alignItems: "flex-start" }}><Icon name="lock" size={16} />{FOUR_EYES}</div>
          </>
        )}
        {(primary === "apply" || primary === "handover") && (
          <>
            <button type="button" className="primary" disabled={!act.primary_enabled} title={act.primary_help ?? undefined}
              onClick={() => { setOverride(false); setOverrideReason(""); dialog.current?.showModal(); }}>
              <Icon name={icon} />{label} …
            </button>
            <dialog ref={dialog} className="dk-dialog" aria-labelledby={`dk-confirm-${report.run_id}`}>
              <h2 id={`dk-confirm-${report.run_id}`} style={{ marginTop: 0 }}>Confirm: {label.toLowerCase()}</h2>
              <div className="dk-stack">
                {primary === "handover" ? (
                  <div className="callout tone-info"><span style={{ color: "var(--dk-text)" }}>{HANDOVER_TEXT}</span></div>
                ) : (
                  <div className="callout tone-warning"><span style={{ color: "var(--dk-text)" }}>Write <strong>{act.change_count} change(s)</strong> to the portfolio data source now? A snapshot is taken first; the System Admin can restore it.</span></div>
                )}
                <DataTable rows={act.confirm_rows} columns={["Project", "Change", "Current", "Proposed", "Note"]} />
                {act.needs_override && (
                  <>
                    <div className="callout tone-danger" role="alert"><span style={{ color: "var(--dk-text)" }}>The rule-based verdict is <strong>{act.verdict_label}</strong>. Approving means overriding it – the override and your reason are recorded in the audit trail.</span></div>
                    <label className="row"><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} style={{ width: "auto" }} />Override the {act.verdict_label} verdict</label>
                    <label className="field">Reason for the override (required)<textarea rows={3} maxLength={2000} value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} /></label>
                  </>
                )}
                <div className="row" style={{ justifyContent: "flex-end" }}>
                  <button type="button" onClick={() => dialog.current?.close()}>Cancel</button>
                  <button type="button" className="primary" disabled={!ready} onClick={() => void apply()}><Icon name={icon} />Confirm: {label.toLowerCase()}</button>
                </div>
              </div>
            </dialog>
          </>
        )}
      </div>
    </div>
  );
}
