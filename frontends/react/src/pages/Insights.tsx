// Decision insights (PMO, a sub-page of the overview): status check of the sources, the portfolio with probabilities,
// the reference class of a project, the review of past decisions with the audit pack and the transfers to ServiceNow.
// Same sections as the Streamlit page; every figure comes from /api/v1/insights (deterministic, no AI).
import { useState, type ReactNode } from "react";
import { type Answer } from "../api/client";
import { download } from "../components/ReportViewer";
import { PageLink } from "../components/Shell";
import { ErrorBox, Icon, PageHeader } from "../components/ui";
import { eur, eurM } from "./overview/format";
import { Caption, Info, Metric, Section, useApi } from "./overview/parts";

type StatusCheck = Answer<"/api/v1/insights/status-check">;
type Reference = Answer<"/api/v1/insights/reference/{project_id}"> & {
  buffer_eur?: number; buffer_days?: number; budget_at_confidence_eur?: number;
};
type Transfers = Answer<"/api/v1/insights/servicenow-transfers">;
// the forecast and the review answer open dictionaries (app/views/insights.py names their keys)
type ForecastSummary = {
  runs: number; seed: number; confidence_pct: number; headline: string;
  probability_within_budget_pct: number; p_conf_cost_eur: number; reserve_needed_eur: number;
};
type ForecastProject = {
  project_id: string; project_name: string; budget_eur: number; probability_within_budget_pct: number;
  probability_on_time_pct?: number | null; reference_size: number; reference_basis: string;
};
type Forecast = { summary: ForecastSummary; projects: ForecastProject[] };
type Decision = {
  proposal_id: string; decided_on: string; verdict: string; review_stage: string | null; next_review: string | null;
  held: boolean | null; outcomes: { text?: string }[];
};
type HitRate = { verdict: string; decisions: number; held: number; hit_rate_pct: number | null };
type Review = { due_for_review: number; conditions_overdue: number; decisions: Decision[]; hit_rate: HitRate[] };

const dash = (value: unknown) => (value === null || value === undefined || value === "" ? "–" : String(value));
const num = (value: number | null | undefined) => (value == null ? "–" : value.toLocaleString("en-US", { maximumFractionDigits: 1 }));

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

function StatusCheckView({ result }: { result: StatusCheck }) {
  return (
    <div className="stack">
      <Caption text={`${result.mismatches} project(s) reported better than their numbers · ${result.stale} without a current status report`} />
      <Table label="Status check of the sources" head={["Project", "Name", "Reported", "Calculated", "Check", "Why", "Source"]}
        rows={result.checks.map((c) => [c.project_id, c.project_name, dash(c.reported), c.calculated, c.label, c.reasons.join("; "), dash(c.source)])} />
    </div>
  );
}

function ForecastView({ result }: { result: Forecast }) {
  const s = result.summary;
  if (!result.projects.length) return <Info text="No open project with a budget to simulate." />;
  return (
    <div className="stack">
      <div className="grid">
        <section className="card"><Metric label="Within budget" value={`${num(s.probability_within_budget_pct)} %`} /></section>
        <section className="card"><Metric label={`Cost at ${num(s.confidence_pct)} %`} value={eurM(s.p_conf_cost_eur)} /></section>
        <section className="card"><Metric label="Reserve needed" value={eurM(s.reserve_needed_eur)} /></section>
      </div>
      <Caption text={`${s.headline} ${s.runs} runs, seed ${s.seed}.`} />
      <Table label="Portfolio with probabilities" head={["Project", "Name", "Budget", "Within budget %", "On time %", "Compared with"]}
        rows={result.projects.map((p) => [p.project_id, p.project_name, eur(p.budget_eur), num(p.probability_within_budget_pct),
          num(p.probability_on_time_pct), `${p.reference_size} (${p.reference_basis})`])} />
    </div>
  );
}

function referenceLines(r: Reference): string[] {
  const lines = r.headline ? [r.headline] : [];
  if (r.buffer_eur != null) {
    const days = r.buffer_days != null ? `, ${r.buffer_days} days` : "";
    lines.push(`Budget at ${num(r.confidence_pct)} % confidence: ${eurM(r.budget_at_confidence_eur)} (buffer ${eurM(r.buffer_eur)}${days}).`);
  }
  return lines.length ? lines : ["Too few comparable projects for a reference class."];
}

function ReferenceView({ projects }: { projects: string[] }) {
  const [project, setProject] = useState(projects[0] ?? "");
  const state = useApi<Reference>(project ? `/insights/reference/${encodeURIComponent(project)}` : null);
  if (!projects.length) return <Caption text="No open project." />;
  return (
    <div className="stack">
      <label className="field" style={{ maxWidth: 260 }}>
        <span className="muted small">Project</span>
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          {projects.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </label>
      <Section state={state}>{(r) => <>{referenceLines(r).map((line, i) => <p key={i} style={{ margin: 0 }}>{line}</p>)}</>}</Section>
    </div>
  );
}

function ReviewView({ review }: { review: Review }) {
  const [chosen, setChosen] = useState(review.decisions[0]?.proposal_id ?? "");
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="stack">
      <Caption text={`${review.due_for_review} decision(s) due for review · ${review.conditions_overdue} condition(s) past their due date`} />
      {review.hit_rate.length > 0 && (
        <Table label="Hit rate per verdict" head={["Verdict", "Decisions", "Held", "Hit rate %"]}
          rows={review.hit_rate.map((h) => [h.verdict, h.decisions, h.held, num(h.hit_rate_pct)])} />
      )}
      {!review.decisions.length ? (
        <Info text="No approved decision yet." />
      ) : (
        <>
          <Table label="Approved decisions" head={["Proposal", "Decided", "Verdict", "Review", "Held", "Outcome"]}
            rows={review.decisions.map((d) => [d.proposal_id, d.decided_on, d.verdict, d.review_stage ?? `next ${d.next_review}`,
              d.held === null ? "–" : d.held ? "yes" : "no", d.outcomes.map((o) => o.text ?? "").join("; ")])} />
          <div className="row" style={{ alignItems: "end", flexWrap: "wrap" }}>
            <label className="field" style={{ maxWidth: 260 }}>
              <span className="muted small">Approved proposal</span>
              <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
                {review.decisions.map((d) => <option key={d.proposal_id} value={d.proposal_id}>{d.proposal_id}</option>)}
              </select>
            </label>
            <button type="button" disabled={!chosen}
              onClick={() => { setError(null); download(`/insights/audit-pack/${encodeURIComponent(chosen)}`).catch((e: Error) => setError(e.message)); }}>
              <Icon name="inventory_2" />Download audit pack
            </button>
          </div>
          {error && <ErrorBox message={error} />}
        </>
      )}
    </div>
  );
}

function TransfersView({ rows }: { rows: Transfers }) {
  if (!rows.length) return <Caption text="No transfer to ServiceNow yet (write-back is off unless the mapping and the host switch it on)." />;
  return (
    <Table label="Transfers to ServiceNow" head={["Proposal", "Project", "Field", "Value", "Status", "Check", "Note"]}
      rows={rows.map((r) => [dash(r.proposal_id), dash(r.key), dash(r.field), dash(r.value), r.status, dash(r.check), r.reason ?? ""])} />
  );
}

export function Insights() {
  const checks = useApi<StatusCheck>("/insights/status-check");
  const forecast = useApi<Forecast>("/insights/forecast");
  const review = useApi<Review>("/insights/decision-review");
  const transfers = useApi<Transfers>("/insights/servicenow-transfers");
  return (
    <>
      <PageHeader title="Decision insights" subtitle="Independent of the source tools · deterministic, no AI"
        action={<PageLink to="overview" icon="arrow_back" text="Back to the overview" />} />
      <h2>Status check of the sources</h2>
      <Section state={checks}>{(c) => <StatusCheckView result={c} />}</Section>
      <h2>Portfolio with probabilities</h2>
      <Section state={forecast}>{(f) => <ForecastView result={f} />}</Section>
      <h2>Reference class of a project</h2>
      <Section state={checks}>{(c) => <ReferenceView projects={c.checks.map((x) => x.project_id)} />}</Section>
      <h2>Review of past decisions</h2>
      <Section state={review}>{(r) => <ReviewView review={r} />}</Section>
      <h2>Transfers to ServiceNow</h2>
      <Section state={transfers}>{(t) => <TransfersView rows={t} />}</Section>
    </>
  );
}
