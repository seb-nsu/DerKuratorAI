// Recovery (PMO, a sub-page of the overview): the red trigger's recommendations – trigger, causes, gap, impact, the
// options side by side (package verdict, gap closure, collateral, cost, Monte Carlo before/after), the rejected measures
// and Adopt / Dismiss / Recalculate; 'Analyse now' for a project that is red already. Same sections as the NiceGUI page;
// every figure comes from /api/v1/recovery. Deep link: /recovery?id=<recommendation>.
import { useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { Markdown } from "../components/Markdown";
import { PageLink, useWorkspace } from "../components/Shell";
import { ErrorBox, Icon, PageHeader } from "../components/ui";
import { eurM } from "./overview/format";
import { Caption, Info, Section, useApi } from "./overview/parts";

export type RecSummary = {
  id: string; project_id: string; project_name?: string | null; status: string; trigger_label?: string | null;
  reported?: string | null; calculated?: string | null; as_of?: string | null; created_at?: string | null;
  causes: string[]; gap_eur: number; gap_days: number; options: number; recommended?: string | null; headline: string;
};
export type RecList = { enabled: boolean; recommendations: RecSummary[] };
type Step = { action: string; project: string; target?: string | null };
type Result = {
  decision?: string; verdict?: { label?: string } | null; closure?: { cost_pct?: number | null; schedule_pct?: number | null };
  calculated_after?: string; collateral?: string[]; cost?: { exit_cost_eur?: number; hold_cost_eur?: number };
  probability_before_pct?: number | null; probability_after_pct?: number | null;
};
type Option = { key: string; label: string; steps: Step[]; result: Result; threshold_flag?: boolean; adoptable_by_pm?: boolean };
type Rec = {
  id: string; project_id: string; project_name?: string; status: string; trigger_label?: string; reported?: string | null;
  calculated?: string; as_of?: string; recommended?: string | null; report_hint?: string | null;
  causes: { text: string }[]; options: Option[];
  impact: {
    dependents?: { id: string; red?: boolean }[]; upstream_red?: string[];
    deadline_breaches?: { successor: string; required_by: string; days_late: number }[];
    budget?: { gap_eur?: number; reserve_free_eur?: number };
    teams?: { team: string; red_neighbours?: string[] }[];
  };
  rejected: { label: string; why: string }[]; not_possible: { label: string; reason: string }[];
  explanation: { llm?: string | null; findings?: string[]; ideas?: string[] };
};

const pct = (v: number | null | undefined) => (v == null ? "–" : `${v.toLocaleString("en-US", { maximumFractionDigits: 1 })} %`);

function Table({ label, head, rows }: { label: string; head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="table-wrap" role="region" aria-label={label} tabIndex={0}>
      <table>
        <thead><tr>{head.map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

export function AlertBand({ items }: { items: RecSummary[] }) {
  if (!items.length) return null;
  return (
    <div className="stack" role="region" aria-label="Red trigger">
      {items.slice(0, 3).map((i) => (
        <div key={i.id} className="card row" style={{ borderLeft: "4px solid #c62828", justifyContent: "space-between" }}>
          <strong>{i.headline}</strong>
          <Link className="link-button" to={`/recovery?id=${encodeURIComponent(i.id)}`}>Open</Link>
        </div>
      ))}
      {items.length > 3 && <Caption text={`… and ${items.length - 3} more on the Recovery page.`} />}
    </div>
  );
}

function Detail({ rec, pmo, onChanged }: { rec: Rec; pmo: boolean; onChanged: (id?: string) => void }) {
  const navigate = useNavigate();
  const adoptable = rec.options.filter((o) => o.steps.length && (pmo || o.adoptable_by_pm)).map((o) => o.key);
  const [option, setOption] = useState(rec.recommended && adoptable.includes(rec.recommended) ? rec.recommended : adoptable[0] ?? "");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<void>) => {
    setError(null); setBusy(true);
    try { await fn(); } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); } finally { setBusy(false); }
  };
  const impact = rec.impact;
  return (
    <div className="stack">
      <Caption text={`Trigger: ${rec.trigger_label} · reported ${rec.reported ?? "–"}, calculated ${rec.calculated} · as of ${rec.as_of} · status ${rec.status}`} />
      <section className="card"><h3>Causes</h3>{rec.causes.map((c, i) => <p key={i} style={{ margin: 0 }}>{c.text}</p>)}
        {rec.report_hint && <Caption text={`Latest status report (free text, a hint only): ${rec.report_hint}`} />}</section>
      <section className="card"><h3>Impact on the portfolio</h3>
        <p style={{ margin: 0 }}>Dependents: {(impact.dependents ?? []).map((d) => d.id + (d.red ? " (red)" : "")).join(", ") || "none"}</p>
        {!!impact.upstream_red?.length && <p style={{ margin: 0 }}>Held back by red predecessor(s): {impact.upstream_red.join(", ")}</p>}
        {(impact.deadline_breaches ?? []).map((b) => <p key={b.successor} style={{ margin: 0 }}>{b.successor} needs the result by {b.required_by} – {b.days_late} days late</p>)}
        {impact.budget && <p style={{ margin: 0 }}>Cost gap {eurM(impact.budget.gap_eur)} · reserve free above the floor {eurM(impact.budget.reserve_free_eur)}</p>}
        {(impact.teams ?? []).filter((t) => t.red_neighbours?.length).map((t) => <Caption key={t.team} text={`${t.team} is shared with red project(s) ${t.red_neighbours?.join(", ")}`} />)}
      </section>
      <h2>Options (each calculated as one package)</h2>
      <Table label="Options" head={["Option", "Verdict", "Cost gap closed", "Schedule gap closed", "Status after", "Collateral", "Exit / hold cost", "Portfolio within budget", "Measures"]}
        rows={rec.options.map((o) => [
          `${o.key} ${o.label}${o.key === rec.recommended ? " ★" : ""}`,
          `${o.result.verdict?.label ?? o.result.decision ?? "–"}${o.threshold_flag ? " ⚠ just below the board threshold" : ""}`,
          pct(o.result.closure?.cost_pct), pct(o.result.closure?.schedule_pct), o.result.calculated_after ?? "–",
          (o.result.collateral ?? []).join(", ") || "none",
          eurM((o.result.cost?.exit_cost_eur ?? 0) + (o.result.cost?.hold_cost_eur ?? 0)),
          `${pct(o.result.probability_before_pct)} → ${pct(o.result.probability_after_pct)}`,
          o.steps.map((s) => `${s.action} ${s.project}${s.target ? ` → ${s.target}` : ""}`).join("; ") || "none",
        ])} />
      {rec.explanation.llm && (
        <section className="card"><h3>Explanation (AI-written, figures checked)</h3><Markdown text={rec.explanation.llm} />
          {(rec.explanation.findings ?? []).map((f, i) => <Caption key={i} icon="warning" text={f} />)}
          {(rec.explanation.ideas ?? []).map((f, i) => <Caption key={`i${i}`} icon="lightbulb" text={f} />)}</section>
      )}
      {rec.rejected.length > 0 && <Table label="Checked and rejected" head={["Measure", "Why rejected"]} rows={rec.rejected.map((r) => [r.label, r.why])} />}
      {rec.not_possible.length > 0 && <Table label="Not possible with the data" head={["Measure", "Why not possible"]} rows={rec.not_possible.map((n) => [n.label, n.reason])} />}
      {(rec.status === "open" || rec.status === "adopted") && adoptable.length > 0 && (
        <div className="row" style={{ alignItems: "end", flexWrap: "wrap" }}>
          <label className="field" style={{ maxWidth: 120 }}>
            <span className="muted small">Option</span>
            <select value={option} onChange={(e) => setOption(e.target.value)}>{adoptable.map((k) => <option key={k} value={k}>{k}</option>)}</select>
          </label>
          <button type="button" className="primary" disabled={busy} title="Runs the option as a what-if in your name; you submit it, a second PMO approves"
            onClick={() => act(async () => { await api.post(`/recovery/recommendations/${rec.id}/adopt`, { option }); navigate("/console"); })}>
            <Icon name="input" />Adopt
          </button>
          {pmo && rec.status === "open" && (
            <>
              <label className="field" style={{ maxWidth: 260 }}>
                <span className="muted small">Reason for dismissing</span>
                <input value={reason} onChange={(e) => setReason(e.target.value)} />
              </label>
              <button type="button" disabled={busy || !reason.trim()}
                onClick={() => act(async () => { await api.post(`/recovery/recommendations/${rec.id}/dismiss`, { reason }); onChanged(rec.id); })}>
                <Icon name="block" />Dismiss
              </button>
            </>
          )}
        </div>
      )}
      {pmo && (
        <button type="button" disabled={busy}
          onClick={() => act(async () => { const fresh = await api.post<{ id: string }>("/recovery/analyse", { project_id: rec.project_id }); onChanged(fresh.id); })}>
          <Icon name="refresh" />Recalculate
        </button>
      )}
      {error && <ErrorBox message={error} />}
    </div>
  );
}

export function Recovery() {
  const pmo = useWorkspace().info.user.role.startsWith("PMO");
  const [params, setParams] = useSearchParams();
  const [version, setVersion] = useState(0);
  const list = useApi<RecList>(`/recovery/recommendations?v=${version}`);
  const items = list?.data?.recommendations ?? [];
  const chosen = items.find((i) => i.id === params.get("id"))?.id ?? items[0]?.id;
  const detail = useApi<Rec>(chosen ? `/recovery/recommendations/${encodeURIComponent(chosen)}?v=${version}` : null);
  const [project, setProject] = useState("");
  const [error, setError] = useState<string | null>(null);
  const changed = (id?: string) => { if (id) setParams({ id }); setVersion((v) => v + 1); };
  return (
    <>
      <PageHeader title="Recovery" subtitle="Red trigger · calculated options, no automatic change"
        action={<PageLink to={pmo ? "overview" : "projects"} icon="arrow_back" text={pmo ? "Back to the overview" : "Back to my projects"} />} />
      <Section state={list}>{(l) => (
        <div className="stack">
          {!l.enabled && <Info text="The red trigger is off (Rules › Red trigger). 'Analyse now' still works for a red project." />}
          {l.recommendations.length ? (
            <Table label="Recommendations" head={["Project", "Name", "Status", "Trigger", "Causes", "Gap", "Options", "Recommended", ""]}
              rows={l.recommendations.map((i) => [i.project_id, i.project_name ?? "", i.status, i.trigger_label ?? "", i.causes.join(", "),
                eurM(i.gap_eur), i.options, i.recommended ?? "–",
                <button key={i.id} type="button" className="link-button" onClick={() => setParams({ id: i.id })}>Open</button>])} />
          ) : <Info text="No recommendation yet." />}
          {pmo && (
            <div className="row" style={{ alignItems: "end", flexWrap: "wrap" }}>
              <label className="field" style={{ maxWidth: 260 }}>
                <span className="muted small">Project number for 'Analyse now'</span>
                <input value={project} onChange={(e) => setProject(e.target.value)} />
              </label>
              <button type="button" disabled={!project.trim()} onClick={() => {
                setError(null);
                api.post<{ id: string }>("/recovery/analyse", { project_id: project.trim() })
                  .then((r) => changed(r.id), (e) => setError(e instanceof ApiError ? e.message : String(e)));
              }}><Icon name="healing" />Analyse now</button>
            </div>
          )}
          {error && <ErrorBox message={error} />}
        </div>
      )}</Section>
      {chosen && (
        <>
          <h2>{items.find((i) => i.id === chosen)?.headline}</h2>
          <Section state={detail}>{(d) => <Detail key={d.id} rec={d} pmo={pmo} onChanged={changed} />}</Section>
        </>
      )}
    </>
  );
}

export function ProjectRecommendations({ projectId }: { projectId: string }) {
  const page = useWorkspace().info.pages.some((p) => p.id === "recovery");
  const state = useApi<RecList>(`/recovery/projects/${encodeURIComponent(projectId)}`);
  return (
    <Section state={state}>{(l) => l.recommendations.length ? (
      <div className="stack">
        <Table label="Recommendations" head={["Created", "Status", "Trigger", "Causes", "Options", "Recommended"]}
          rows={l.recommendations.map((i) => [(i.created_at ?? "").slice(0, 16), i.status, i.trigger_label ?? "", i.causes.join(", "), i.options, i.recommended ?? "–"])} />
        {page && <Link className="link-button" to={`/recovery?id=${encodeURIComponent(l.recommendations[0].id)}`}>Open on the Recovery page</Link>}
      </div>
    ) : <Caption text="No recommendation of the red trigger for this project." />}</Section>
  );
}
