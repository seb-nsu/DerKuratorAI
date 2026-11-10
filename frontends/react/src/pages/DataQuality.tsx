// Data quality: the deterministic Data Steward checks of the reader's scope – index, quality bars, the executive
// interpretation (loaded on its own, it may take an AI a moment), the findings with their filters and the question box.
import { useEffect, useState } from "react";
import { api, ApiError, type Answer } from "../api/client";
import { AskBox } from "../components/AskBox";
import { PageLink } from "../components/Shell";
import { Markdown } from "../components/Markdown";
import { ErrorBox, Icon, PageHeader } from "../components/ui";
import { tone } from "../theme";
import { pct } from "./overview/format";
import { Caption, Metric, Section, useApi } from "./overview/parts";

type Page = Answer<"/api/v1/data-quality">;
type Interpretation = Answer<"/api/v1/data-quality/interpretation">;
const SEVERITIES = ["All", "Errors", "Warnings"] as const;
const COLUMNS: [keyof Page["findings"][number], string][] = [
  ["severity", "Severity"], ["project", "Project"], ["rule", "Rule"], ["field", "Field"], ["finding", "Finding"],
];

// colour with status word and value as text, so colour never carries the meaning alone
function QualityBar({ bar }: { bar: Page["bars"][number] }) {
  const value = Math.round(bar.value);
  return (
    <div style={{ margin: "2px 0 12px" }}>
      <div className="row" style={{ justifyContent: "space-between", fontSize: "0.9rem" }}>
        <span>{bar.label}</span><strong style={{ whiteSpace: "nowrap" }}>{value} % · {bar.word}</strong>
      </div>
      <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value} aria-label={`${bar.label}: ${value} %, ${bar.word}`}
        style={{ background: "var(--dk-neutral-bg)", borderRadius: 4, height: 10, overflow: "hidden" }}>
        <div style={{ width: `${Math.max(0, Math.min(100, bar.value))}%`, background: `var(--dk-${tone("rag", bar.status)})`, height: 10, borderRadius: 4 }} />
      </div>
      <div className="muted small" style={{ marginTop: 2 }}>{bar.detail}</div>
    </div>
  );
}

function InterpretationCard() {
  const state = useApi<Interpretation>("/data-quality/interpretation");
  return (
    <section className="card">
      <div style={{ fontWeight: 700, marginBottom: 8 }}>Executive interpretation</div>
      {!state ? (
        <div className="row muted" role="status"><span className="spinner" aria-hidden="true" />AI is interpreting the data risks ...</div>
      ) : (
        <Section state={state}>
          {(r) => (
            <>
              <Markdown text={r.text} />
              <Caption text={r.ai_generated ? `Written by ${r.model} based on deterministic Data Steward findings`
                : "Rule-based interpretation (no LLM available) based on deterministic Data Steward findings"} />
            </>
          )}
        </Section>
      )}
    </section>
  );
}

function Findings({ page }: { page: Page }) {
  const summary = page.summary;
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>("All");
  const [projects, setProjects] = useState<string[]>([]);
  const [shown, setShown] = useState({ findings: page.findings, count: page.findings_shown });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (severity === "All" && !projects.length) return setShown({ findings: page.findings, count: page.findings_shown });
    const query = new URLSearchParams({ severity });
    projects.forEach((p) => query.append("project", p));
    let live = true;
    api.get<Page>(`/data-quality?${query}`).then(
      (answer) => live && (setShown({ findings: answer.findings, count: answer.findings_shown }), setError(null)),
      (e) => live && setError(e instanceof ApiError ? e.message : String(e)),
    );
    return () => {
      live = false;
    };
  }, [severity, projects, page]);

  const toggle = (p: string) => setProjects(projects.includes(p) ? projects.filter((x) => x !== p) : [...projects, p]);
  return (
    <section className="card stack">
      <div><strong>Findings</strong> · {summary.errors} errors · {summary.warnings} warnings · blocked for simulation: {summary.blocked_projects.join(", ") || "–"}</div>
      {!page.findings_total ? (
        <Caption text={`${summary.projects_checked} projects checked – no findings.`} icon="check_circle" />
      ) : (
        <>
          <div className="filters">
            <fieldset className="segmented">
              <legend className="muted small">Severity</legend>
              {SEVERITIES.map((s) => (
                <button key={s} type="button" className={s === severity ? "primary" : ""} aria-pressed={s === severity} onClick={() => setSeverity(s)}>{s}</button>
              ))}
            </fieldset>
            <div className="field">
              <span className="muted small" id="dq-projects">Projects</span>
              <details className="multiselect">
                <summary aria-label={`Projects: ${projects.length ? projects.join(", ") : "All projects"}`}>{projects.length ? projects.join(", ") : "All projects"}</summary>
                <div className="menu-list">
                  {page.projects.map((p) => (
                    <label key={p} className="row"><input type="checkbox" checked={projects.includes(p)} onChange={() => toggle(p)} style={{ width: "auto" }} />{p}</label>
                  ))}
                  {projects.length > 0 && <button type="button" className="flat" onClick={() => setProjects([])}><Icon name="close" />Clear</button>}
                </div>
              </details>
            </div>
          </div>
          {error && <ErrorBox message={error} />}
          <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
            <table style={{ minWidth: 760 }}>
              <thead><tr>{COLUMNS.map(([k, label]) => <th key={k}>{label}</th>)}</tr></thead>
              <tbody>
                {shown.findings.map((f, i) => <tr key={i}>{COLUMNS.map(([k]) => <td key={k} style={k === "project" || k === "severity" ? { whiteSpace: "nowrap" } : undefined}>{f[k]}</td>)}</tr>)}
              </tbody>
            </table>
          </div>
          <Caption text={`${shown.count} finding(s) shown`} />
        </>
      )}
    </section>
  );
}

export function DataQuality() {
  const state = useApi<Page>("/data-quality");
  const page = state?.data;
  return (
    <>
      <PageHeader title="Data quality" subtitle={page ? `Deterministic Data Steward · ${page.summary.projects_checked} projects checked` : undefined}
        action={<PageLink to="projects" icon="arrow_back" text="Back to the projects" />} />
      <Section state={state}>
        {(p) => (
          <>
            <div className="dq-top">
              <section className="card">
                <Metric label="Data quality index" value={pct(p.summary.quality_index)} help={p.index_help}
                  caption={p.summary.blocked_projects.length ? `${p.summary.blocked_projects.length} project(s) blocked for simulation` : "No project blocked"} />
                <div style={{ marginTop: 16 }}>{p.bars.map((b) => <QualityBar key={b.label} bar={b} />)}</div>
              </section>
              <InterpretationCard />
            </div>
            <Findings page={p} />
            <AskBox page="data_quality" />
          </>
        )}
      </Section>
    </>
  );
}
