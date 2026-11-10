// Report viewer: one decision report on one screen, shared by the Steering Console and the Approvals page.
//
// Layout: header (eyebrow, request, verdict and status badges, the page's actions) → verdict callout → key-figure row
// with the change count → tabs Summary · Decision paper · Changes · Impact · Dependencies · Data quality (the last two
// only when the report has them). Everything comes from the API, already in the caller's scope:
// GET /steering/runs/{run_id}/report (an analysis, type RunReport) or GET /proposals/{id}/report (type ReportView).
//
// <ReportViewer report actions? eyebrow? status? title? />
//   report   the ReportView (or RunReport) answer of one of the routes above
//   actions  the page's own action bar, drawn in the header (e.g. export, save, submit, decide); omitted: none
//   eyebrow  small line above the title; default: report.eyebrow when present (the console's "Decision report · …")
//   status   badge next to the verdict { label, tone, icon }; default: report.status when present
//   title    the bold request line; default: report.request or report.title
//   ask      { runId } or { proposalId }: the report's question box ("Ask about this report", POST /assist/ask page
//            "report") at the end of the report, as in the Streamlit report; omitted: none
//   children drawn below the tabs, above the question box
//
// Also exported: <ExportMenu base /> (SteerCo Pack, Markdown, CSV of base + "/export/{kind}", base being
// "/steering/runs/{run_id}" or "/proposals/{id}"), download(path) (an API file with the user's token as a download),
// <Markdown text /> (safe Markdown without HTML), <DataTable rows columns? />, <VerdictBadge verdict /> and
// VERDICT_STYLE (tone and icon per verdict key).
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { session } from "../api/client";
import type { components } from "../api/schema";
import { type Tone } from "../theme";
import { AskBox } from "./AskBox";
import { PlotlyFigure } from "./PlotlyFigure";
import { Badge, Icon } from "./ui";
import "./ReportViewer.css";

export type ReportView = components["schemas"]["ReportView"];
export type RunReport = components["schemas"]["RunReport"];
export type StatusBadge = { label: string; tone: string; icon: string };

export const VERDICT_STYLE: Record<string, [Tone, string]> = {
  GO: ["success", "check_circle"],
  CONDITIONAL: ["warning", "change_history"],
  "NO-GO": ["danger", "block"],
  INCOMPLETE: ["neutral", "help"],
  NONE: ["neutral", "remove"],
};
export const TONE_ICONS: Record<string, string> = { success: "check_circle", warning: "change_history", danger: "block", neutral: "help", info: "info" };
const CALLOUT_TONES: Record<string, Tone> = { GO: "success", CONDITIONAL: "warning", "NO-GO": "danger", INCOMPLETE: "info" };
const FULL_PAPER = "Full paper";
const EXPORTS: [string, string, string, string?][] = [
  ["steerco-pack", "SteerCo Pack (print / PDF)", "print", "One-page HTML summary – open it and use the browser's 'Print → Save as PDF'."],
  ["markdown", "This analysis (Markdown)", "description"],
  ["csv", "Changes (CSV)", "table_view"],
];

// ------------------------------------------------------------------ small parts
/** Fetches an API file with the user's token and hands it to the browser as a download. Throws with the API's text. */
export async function download(path: string): Promise<void> {
  const token = session.token();
  const response = await fetch(`/api/v1${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!response.ok) {
    let detail = `Download failed (${response.status}).`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? path.split("/").pop() ?? "download";
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const toneOf = (value: string): Tone => (["success", "warning", "danger", "info", "neutral"].includes(value) ? (value as Tone) : "neutral");

export function VerdictBadge({ verdict }: { verdict: { label: string; key: string } }) {
  const [t, icon] = VERDICT_STYLE[verdict.key] ?? VERDICT_STYLE.NONE;
  return <Badge text={verdict.label || "No verdict"} tone={t} icon={icon} />;
}

export function DataTable({ rows, columns }: { rows: Record<string, string>[]; columns?: string[] }) {
  const cols = columns ?? (rows[0] ? Object.keys(rows[0]) : []);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{cols.map((c) => <th key={c} scope="col">{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>{cols.map((c) => <td key={c}>{row[c] ?? ""}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A menu that opens below its button (closes on Escape and on a click outside). */
export function Popover({ label, icon, children, wide, className }: { label: string; icon?: string; children: ReactNode; wide?: boolean; className?: string }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const key = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [open]);
  return (
    <div className={`dk-popover ${className ?? ""}`} onBlur={(e) => !e.currentTarget.contains(e.relatedTarget as Node) && setOpen(false)}>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {icon && <Icon name={icon} />}
        {label}
        <Icon name={open ? "expand_less" : "expand_more"} size={18} />
      </button>
      {open && <div className={`dk-popover-body${wide ? " wide" : ""}`} tabIndex={-1}>{children}</div>}
    </div>
  );
}

export function ExportMenu({ base, onError }: { base: string; onError?: (message: string) => void }) {
  return (
    <Popover label="Export" icon="download">
      <div className="dk-menu-list" role="menu">
        {EXPORTS.map(([kind, label, icon, help]) => (
          <button key={kind} type="button" role="menuitem" className="flat" title={help}
            onClick={() => download(`${base}/export/${kind}`).catch((e: Error) => onError?.(e.message))}>
            <Icon name={icon} />
            {label}
          </button>
        ))}
      </div>
    </Popover>
  );
}

// ------------------------------------------------------------------ Markdown (no HTML: everything is text)
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) out.push(<strong key={i++}>{token.slice(2, -2)}</strong>);
    else if (token.startsWith("`")) out.push(<code key={i++}>{token.slice(1, -1)}</code>);
    else out.push(<em key={i++}>{token.slice(1, -1)}</em>);
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line: string) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));

export function Markdown({ text }: { text: string }) {
  const lines = (text ?? "").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = blocks.length;
    if (!line.trim()) {
      i++;
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(6, Math.max(3, line.match(/^#+/)![0].length + 1)); // below the page and report headings
      const Tag = `h${level}` as "h3";
      blocks.push(<Tag key={key}>{inline(line.replace(/^#+\s*/, ""))}</Tag>);
      i++;
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push(<hr key={key} />);
      i++;
    } else if (line.trim().startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        if (!/^\s*\|?\s*:?-{2,}/.test(lines[i])) rows.push(cells(lines[i]));
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <div className="table-wrap" key={key}>
          <table>
            <thead><tr>{head.map((c, j) => <th key={j} scope="col">{inline(c)}</th>)}</tr></thead>
            <tbody>{body.map((r, n) => <tr key={n}>{r.map((c, j) => <td key={j}>{inline(c)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
    } else if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
        i++;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(<List key={key}>{items.map((t, n) => <li key={n}>{inline(t)}</li>)}</List>);
    } else if (line.startsWith(">")) {
      const quote: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) quote.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(<blockquote key={key}>{inline(quote.join(" "))}</blockquote>);
    } else {
      const para: string[] = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*([-*•]|\d+[.)])\s+|\s*\||>)/.test(lines[i])) para.push(lines[i++]);
      blocks.push(<p key={key}>{para.map((p, n) => <Fragment key={n}>{n > 0 && <br />}{inline(p)}</Fragment>)}</p>);
    }
  }
  return <div className="dk-markdown">{blocks}</div>;
}

// ------------------------------------------------------------------ tabs
const Caption = ({ icon, children }: { icon: string; children: ReactNode }) => (
  <div className="muted small row dk-caption"><Icon name={icon} size={16} />{children}</div>
);

function Summary({ summary }: { summary: ReportView["summary"] }) {
  return (
    <div className="dk-stack">
      {summary.warnings.map((w, i) => (
        <div key={i} className="callout tone-warning row" role="note"><Icon name="warning" /><span style={{ color: "var(--dk-text)" }}>{w}</span></div>
      ))}
      {summary.steps.map((s, i) => <Caption key={i} icon="link">{s}</Caption>)}
      {summary.forced && <Caption icon="warning">Forced simulation – results for blocked projects are indicative only.</Caption>}
      <div className="grid-2">
        <div>
          <p><strong>Why this verdict</strong> · rule-based, not AI{summary.as_of ? ` · As of ${summary.as_of}` : ""}</p>
          {summary.reasons.length ? <ul>{summary.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            : <div className="muted small">No rule-based reasons recorded (e.g. an analysis without a verdict).</div>}
          {summary.rules.length > 0 && (
            <Popover label="Thresholds applied" icon="rule" wide>
              <div className="muted small" style={{ marginBottom: 8 }}>Business rules in force when this verdict was computed (stored with the decision).</div>
              <DataTable rows={summary.rules} columns={["Rule", "Value"]} />
            </Popover>
          )}
        </div>
        <div>
          <p><strong>Conditions &amp; next steps</strong></p>
          {summary.conditions.length ? <ol>{summary.conditions.map((c, i) => <li key={i}>{c}</li>)}</ol>
            : <div className="muted small">No conditions.</div>}
          {summary.flags.map((f, i) => <Caption key={i} icon="flag">{f}</Caption>)}
        </div>
      </div>
      {summary.offline && (
        <Caption icon="rule">Written by the rule engine (offline mode, no AI agents) – the verdict and figures are the same as with AI agents.</Caption>
      )}
    </div>
  );
}

function Paper({ report }: { report: ReportView }) {
  const sections = report.paper_sections;
  const [choice, setChoice] = useState(sections[0]?.title ?? FULL_PAPER);
  useEffect(() => setChoice(report.paper_sections[0]?.title ?? FULL_PAPER), [report]);
  if (!sections.length) return <Markdown text={report.paper || "_No decision paper stored._"} />;
  const body = sections.find((s) => s.title === choice);
  return (
    <div className="dk-stack">
      <div className="dk-chips" role="group" aria-label="Section">
        {[...sections.map((s) => s.title), FULL_PAPER].map((t) => (
          <button key={t} type="button" className={t === choice ? "primary" : ""} aria-pressed={t === choice} onClick={() => setChoice(t)}>{t}</button>
        ))}
      </div>
      {body ? (
        <>
          <h3>{body.title}</h3>
          <Markdown text={body.body || "_Empty section._"} />
        </>
      ) : <Markdown text={report.paper} />}
    </div>
  );
}

function Dependencies({ deps }: { deps: NonNullable<ReportView["dependencies"]> }) {
  const [all, setAll] = useState(false);
  const level = all && deps.all ? deps.all : deps.direct;
  return (
    <div className="dk-stack">
      {deps.toggle_label && (
        <label className="row" title="By default only the direct links of this report's projects are drawn.">
          <input type="checkbox" role="switch" checked={all} onChange={(e) => setAll(e.target.checked)} style={{ width: "auto" }} />
          {deps.toggle_label}
        </label>
      )}
      <div className="muted small">{deps.caption}</div>
      {level.figure && <PlotlyFigure figure={level.figure} />}
      <DataTable rows={level.rows} columns={["Project (waits)", "Depends on", "Type", "Status"]} />
    </div>
  );
}

// ------------------------------------------------------------------ the viewer
export function ReportViewer({ report, actions, eyebrow, status, title, ask, children }: {
  report: ReportView | RunReport;
  actions?: ReactNode;
  eyebrow?: string;
  status?: StatusBadge | null;
  title?: string;
  ask?: { runId?: string; proposalId?: string };
  children?: ReactNode;
}) {
  const [tab, setTab] = useState(report.tabs[0]?.id ?? "summary");
  useEffect(() => setTab(report.tabs[0]?.id ?? "summary"), [report]);
  const run = report as Partial<RunReport>;
  const line = eyebrow ?? run.eyebrow;
  const shown = status === undefined ? (run.status ?? null) : status;
  const calloutTone = CALLOUT_TONES[report.verdict.key];
  const [, verdictIcon] = VERDICT_STYLE[report.verdict.key] ?? VERDICT_STYLE.NONE;
  const select = (id: string) => setTab(id as typeof tab);
  const keyNav = (e: React.KeyboardEvent, index: number) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    const next = report.tabs[(index + step + report.tabs.length) % report.tabs.length];
    select(next.id);
    document.getElementById(`dk-tab-${next.id}`)?.focus();
  };
  return (
    <section className="card dk-report" aria-label="Decision report">
      <div className="dk-report-head">
        <div className="dk-stack-tight">
          {line && <div className="muted small">{line}</div>}
          <h2 className="dk-report-title">{title ?? run.request ?? report.title}</h2>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <VerdictBadge verdict={report.verdict} />
            {shown && <Badge text={shown.label} tone={toneOf(shown.tone)} icon={shown.icon} />}
          </div>
        </div>
        {actions && <div>{actions}</div>}
      </div>
      <div className="dk-stack" style={{ marginTop: 16 }}>
        {calloutTone && (
          <div className={`callout tone-${calloutTone} row`} role="status" style={{ alignItems: "flex-start" }}>
            <Icon name={verdictIcon} />
            <span><strong>Recommendation: {report.verdict.label}</strong>{report.verdict.lead ? ` – ${report.verdict.lead}` : ""}</span>
          </div>
        )}
        <div className="grid">
          {report.metrics.map((m) => (
            <div key={m.label} className="dk-kpi-cell">
              <div className="card"><div className="muted">{m.label}</div><div className="kpi-value">{m.value}</div></div>
              <div className="muted small dk-under">{m.detail}</div>
            </div>
          ))}
          <div className="dk-kpi-cell">
            <div className="card">
              <div className="row muted">Changes<span title={report.changes.help} tabIndex={0} role="img" aria-label={report.changes.help}><Icon name="help" size={16} /></span></div>
              <div className="kpi-value">{report.changes.decisions}</div>
            </div>
            <div className="muted small dk-under">{report.changes.caption}</div>
          </div>
        </div>
        <div className="dk-tabs" role="tablist" aria-label="Report sections">
          {report.tabs.map((t, index) => (
            <button key={t.id} id={`dk-tab-${t.id}`} type="button" role="tab" className="flat" aria-selected={tab === t.id}
              aria-controls={`dk-panel-${t.id}`} tabIndex={tab === t.id ? 0 : -1} onClick={() => select(t.id)} onKeyDown={(e) => keyNav(e, index)}>
              {t.label}
            </button>
          ))}
        </div>
        <div role="tabpanel" id={`dk-panel-${tab}`} aria-labelledby={`dk-tab-${tab}`}>
          {tab === "summary" && <Summary summary={report.summary} />}
          {tab === "paper" && <Paper report={report} />}
          {tab === "changes" && (report.change_table.length ? <DataTable rows={report.change_table} /> : <div className="muted small">No data changes proposed.</div>)}
          {tab === "impact" && (
            <div className="dk-stack">
              <div className="muted small">{report.impact.caption}</div>
              {report.impact.figures.map((f, i) => <PlotlyFigure key={i} figure={f} />)}
            </div>
          )}
          {tab === "dependencies" && report.dependencies && <Dependencies deps={report.dependencies} />}
          {tab === "data_quality" && report.data_quality && (
            <div className="dk-stack">
              <div className="muted small">{report.data_quality.caption}</div>
              <DataTable rows={report.data_quality.rows} columns={["Severity", "Project", "Rule", "Field", "Finding"]} />
            </div>
          )}
        </div>
        {children}
        {ask && <AskBox page="report" title="Ask about this report" runId={ask.runId} proposalId={ask.proposalId} />}
      </div>
    </section>
  );
}
