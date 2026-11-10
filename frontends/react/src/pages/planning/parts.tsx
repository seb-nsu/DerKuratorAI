// Building blocks of the Planning and What-if comparison pages: business tables from API rows (money, percent bars,
// verdict badges), callouts, the rule-based verdict block of a plan, tabs with a deep link and file downloads.
// Presentation only – every figure and verdict comes from the API (same set as frontends/nicegui/pages/planning_parts.py).
import { useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, session } from "../../api/client";
import type { components } from "../../api/schema";
import { Badge, ErrorBox, Icon } from "../../components/ui";
import type { Tone } from "../../theme";
import "./planning.css";

export type Schema<K extends keyof components["schemas"]> = components["schemas"][K];
export type Row = Record<string, unknown>;

const MONEY = new Set(["Cap", "Limit", "Committed", "Planned", "Free", "This demand", "CapEx", "OpEx", "Benefit", "NPV planned"]);
const MONEY_PREFIXES = ["CapEx ", "OpEx ", "NPV"];
export const STATUS_TONES: Record<string, Tone> = {
  Draft: "neutral", Submitted: "info", Assessed: "info", Planned: "warning", Approved: "success",
  Deferred: "warning", Rejected: "danger", Converted: "success",
};
const DECISION_BADGES: [string, Tone, string][] = [
  ["In", "success", "check_circle"], ["Forced-in", "info", "push_pin"], ["Deferred", "warning", "schedule"], ["Out", "danger", "block"],
];
const VERDICT_ICONS: Record<Tone, string> = { success: "check_circle", warning: "warning", danger: "block", neutral: "help", info: "info" };

export const missing = (v: unknown) =>
  v === null || v === undefined || (typeof v === "number" && Number.isNaN(v)) ||
  (typeof v === "string" && ["", "none", "nan", "nat"].includes(v.trim().toLowerCase()));

/** Whole euros with thousands separators, as everywhere in the product: "€420,000", "−€5,000". */
export function eur(v: unknown, signed = false): string {
  if (missing(v)) return "–";
  const n = Number(v);
  const sign = signed && n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}€${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

export function num(v: unknown, digits?: number): string {
  if (missing(v)) return "–";
  if (typeof v !== "number") return String(v);
  if (digits !== undefined) return v.toFixed(digits);
  return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

export function verdictTone(label: string | null | undefined): Tone {
  const t = String(label ?? "").toUpperCase();
  if (t.includes("INCOMPLETE")) return "neutral";
  if (t.includes("NO-GO") || t.includes("NO GO")) return "danger";
  if (t.includes("CONDITIONAL")) return "warning";
  return t.split(/\s+/).includes("GO") ? "success" : "neutral";
}

export function VerdictBadge({ label }: { label: string | null | undefined }) {
  const tone = verdictTone(label);
  return <Badge text={missing(label) ? "No verdict" : String(label)} tone={tone} icon={VERDICT_ICONS[tone]} />;
}

type Kind = "eur" | "bar" | "score" | "fte" | "dec1" | "verdict" | "status" | "text";

function Bar({ value, text }: { value: unknown; text: string }) {
  if (missing(value)) return <>–</>;
  const n = Number(value);
  return (
    <span className={`bar${n > 100 ? " over" : ""}`}>
      <span className="track" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(n, 100))}%` }} /></span>
      {text}
    </span>
  );
}

function cell(column: string, value: unknown, kind?: Kind): [ReactNode, boolean] {
  const k = kind ?? (MONEY.has(column) || MONEY_PREFIXES.some((p) => column.startsWith(p)) ? "eur" : column === "Used %" ? "bar" : undefined);
  switch (k) {
    case "eur": return [eur(value), true];
    case "bar": return [<Bar value={value} text={missing(value) ? "–" : `${Number(value).toFixed(0)} %`} />, false];
    case "score": return [<Bar value={value} text={missing(value) ? "–" : Number(value).toFixed(1)} />, false];
    case "fte": return [missing(value) ? "–" : `${Number(value).toFixed(1)} FTE`, true];
    case "dec1": return [missing(value) ? "–" : Number(value).toFixed(1), true];
    case "verdict": return [missing(value) ? "–" : <VerdictBadge label={String(value)} />, false];
    case "status": return [missing(value) ? "–" : <Badge text={String(value)} tone={STATUS_TONES[String(value)] ?? "neutral"} />, false];
  }
  const numeric = typeof value === "number" && !missing(value);
  let text = numeric ? num(value) : missing(value) ? "–" : String(value);
  if (!numeric && /^\d{4}-\d{2}-\d{2}T00:00:00/.test(text)) text = text.slice(0, 10); // midnight timestamps as dates
  return [text, numeric];
}

/** A business table of API rows, scrolling inside its box on a narrow screen. */
export function DataTable({ rows, columns, formats = {}, labels = {}, scroll, caption }: {
  rows: Row[]; columns?: string[]; formats?: Record<string, Kind>; labels?: Record<string, string>; scroll?: boolean; caption?: string;
}) {
  if (!rows.length) return null;
  // JavaScript lists integer-like keys ("2027") first; the API sends them after the named columns
  const keys = Object.keys(rows[0]);
  const cols = columns ?? [...keys.filter((k) => !/^\d+$/.test(k)), ...keys.filter((k) => /^\d+$/.test(k))];
  return (
    <div className={`table-wrap${scroll ? " scroll" : ""}`} tabIndex={0} role="region" aria-label={caption}>
      <table className="dk-table">
        <thead><tr>{cols.map((c) => <th key={c} scope="col">{labels[c] ?? c}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {cols.map((c) => {
                const [content, right] = cell(c, row[c], formats[c]);
                const long = String(row[c] ?? "").length > 40;
                return <td key={c} className={right ? "num" : long ? "wrap" : undefined}>{content}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const Caption = ({ children }: { children: ReactNode }) => <div className="muted small">{children}</div>;

export function Callout({ children, tone = "info", icon = "info" }: { children: ReactNode; tone?: Tone; icon?: string }) {
  return (
    <div className={`callout tone-${tone} row-start`} role="status">
      <Icon name={icon} />
      <div style={{ color: "var(--dk-text)" }}>{children}</div>
    </div>
  );
}

export function TitleLine({ title, rest }: { title: string; rest: string }) {
  return <div><strong>{title}</strong> · {rest}</div>;
}

type Header = Schema<"ScenarioHeader">;

/** The rule-based verdict of a plan: badge, name, years, binding year, reasons, conditions with owner and due. */
export function ScenarioVerdict({ header }: { header: Header }) {
  const v = header.verdict;
  const years = header.years;
  return (
    <section className="card" style={{ height: "auto" }}>
      <div className="row wrap">
        <VerdictBadge label={v.label} />
        <strong>{header.name}</strong>
        {years.length > 0 && <span>· {years[0]}–{years[years.length - 1]} · binding year {header.binding_year}</span>}
      </div>
      <ul className="list">{(v.reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}</ul>
      {(v.condition_details ?? []).length > 0 && (
        <DataTable rows={(v.condition_details ?? []).map((c) => ({ Condition: c.condition, Owner: c.owner_role, Due: c.due }))} caption="Conditions" />
      )}
      <Caption>
        Rule-based verdict (not AI-generated) · reference date {header.as_of}
        {header.estimated_commitments ? " · committed money partly estimated from the forecast to complete" : ""}
      </Caption>
    </section>
  );
}

export function DecisionBadges({ counts }: { counts: Record<string, number> }) {
  return (
    <div className="badges">
      {DECISION_BADGES.map(([k, tone, icon]) => <Badge key={k} text={`${counts[k] ?? 0} ${k}`} tone={tone} icon={icon} />)}
    </div>
  );
}

/** Tabs (WAI-ARIA tab pattern); the panel of the active tab only. */
export function Tabs({ tabs, active, onChange, label }: {
  tabs: { id: string; title: string }[]; active: string; onChange: (id: string) => void; label: string;
}) {
  const move = (e: KeyboardEvent, i: number) => {
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
    if (next === null) return;
    const t = tabs[(next + tabs.length) % tabs.length];
    onChange(t.id);
    document.getElementById(`tab-${label}-${t.id}`)?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t, i) => (
        <button key={t.id} id={`tab-${label}-${t.id}`} role="tab" type="button" aria-selected={t.id === active}
          tabIndex={t.id === active ? 0 : -1} className={t.id === active ? "active" : ""} onClick={() => onChange(t.id)}
          onKeyDown={(e) => move(e, i)}>
          {t.title}
        </button>
      ))}
    </div>
  );
}

/** Query parameters kept in the address (deep link), replaced without a new history entry; null removes one. */
export function useSetQuery(): (updates: Record<string, string | null>) => void {
  const [, setParams] = useSearchParams();
  return (updates) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(updates)) {
        if (v) next.set(k, v);
        else next.delete(k);
      }
      return next;
    }, { replace: true });
}

export function useQueryParam(name: string): [string | null, (value: string | null) => void] {
  const [params] = useSearchParams();
  const setQuery = useSetQuery();
  return [params.get(name), (value) => setQuery({ [name]: value })];
}

async function raw(method: string, path: string, body: unknown): Promise<Response> {
  const token = session.token();
  return fetch(`/api/v1${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
}

async function problem(response: Response): Promise<string> {
  try {
    const data = await response.json();
    let detail = data?.message ?? data?.detail;
    if (Array.isArray(detail)) detail = detail.slice(0, 3).map((d: { msg?: string }) => d.msg ?? String(d)).join("; ");
    return String(detail ?? `Request failed (${response.status}).`);
  } catch {
    return `Request failed (${response.status}).`;
  }
}

/** A file the API builds (POST answer), handed to the browser under the API's own file name. */
export async function download(path: string, body: unknown, fallback: string): Promise<void> {
  const response = await raw("POST", path, body);
  if (response.status === 401) session.clear();
  if (!response.ok) throw new ApiError(response.status, await problem(response));
  const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? fallback;
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export type Outcome = { kind: string; message: string };

/** A decision route that answers {kind, message} also when it refuses (409): the answer to show either way. */
export async function decision(path: string, body: unknown): Promise<Outcome> {
  const response = await raw("POST", path, body);
  if (response.status === 401) session.clear();
  if (response.ok) return (await response.json()) as Outcome;
  return { kind: "error", message: await problem(response) };
}

/** A short-lived message at the top of the page (success, info, warning, error), read out by screen readers. */
export type Note = { kind: string; message: string } | null;

export function Notice({ note, onClose }: { note: Note; onClose: () => void }) {
  if (!note) return null;
  const tone: Tone = note.kind === "error" ? "danger" : note.kind === "success" ? "success" : note.kind === "warning" ? "warning" : "info";
  return (
    <div className={`callout tone-${tone} row-start notice`} role={note.kind === "error" ? "alert" : "status"}>
      <Icon name={tone === "danger" ? "error" : tone === "success" ? "check_circle" : "info"} />
      <div style={{ color: "var(--dk-text)", flex: 1 }}>{note.message}</div>
      <button className="flat" aria-label="Close message" onClick={onClose}><Icon name="close" /></button>
    </div>
  );
}

export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <strong key={i}>{part.slice(2, -2)}</strong> : part);
}

/** The few Markdown forms the API's texts use (paragraphs, bullet lists, headings, bold), as React elements. */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let items: string[] = [];
  const flushParagraph = () => {
    if (paragraph.length) blocks.push(<p key={blocks.length}>{inline(paragraph.join(" "))}</p>);
    paragraph = [];
  };
  const flushItems = () => {
    if (items.length) blocks.push(<ul key={blocks.length} className="list">{items.map((t, i) => <li key={i}>{inline(t)}</li>)}</ul>);
    items = [];
  };
  for (const line of text.split("\n")) {
    const t = line.trim();
    const bullet = /^[-*] (.*)$/.exec(t);
    const heading = /^#{1,6} (.*)$/.exec(t);
    if (!t) {
      flushParagraph();
      flushItems();
    } else if (bullet) {
      flushParagraph();
      items.push(bullet[1]);
    } else if (heading) {
      flushParagraph();
      flushItems();
      blocks.push(<p key={blocks.length}><strong>{inline(heading[1])}</strong></p>);
    } else {
      flushItems();
      paragraph.push(t);
    }
  }
  flushParagraph();
  flushItems();
  return <div className="paper">{blocks}</div>;
}

/** A modal dialog: focus moves into it, Escape closes it. */
export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.key === "Escape" && onClose()}
        ref={(el) => { if (el && !el.contains(document.activeElement)) el.querySelector<HTMLElement>("input, textarea, button")?.focus(); }}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

/** Loads an API answer for a section; `reload` fetches it again. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [turn, setTurn] = useState(0);
  useEffect(() => {
    let live = true;
    setError(null);
    load().then((d) => live && setData(d), (e) => live && setError(errorText(e)));
    return () => { live = false; };
  }, [...deps, turn]);
  return { data, error, reload: () => setTurn((t) => t + 1) };
}

export function Loading({ error, children }: { error: string | null; children: ReactNode | null }) {
  if (error) return <ErrorBox message={error} />;
  return children === null ? <div className="muted" aria-busy="true">Loading …</div> : <>{children}</>;
}

