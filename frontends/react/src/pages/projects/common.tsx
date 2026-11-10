// Display helpers of the Projects and Approvals pages: the same cell formats as the Streamlit tables (whole euros,
// dates without midnight) and small blocks (table, dialog, notice). The Approvals cells (verdict, budget Δ,
// submitted) come formatted from the API. Formatting only – every value comes from the API.
import { useEffect, useRef, type ReactNode } from "react";
import { Icon } from "../../components/ui";
import { type Tone } from "../../theme";

export function eur(value: unknown, signed = false): string {
  const n = typeof value === "number" ? value : value == null || value === "" ? NaN : Number(value);
  if (Number.isNaN(n)) return "–";
  const sign = signed && n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}€${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

/** A table cell for business readers: empty as "–", a midnight timestamp as its date. */
export function cell(value: unknown): string {
  if (value === null || value === undefined || (typeof value === "number" && Number.isNaN(value))) return "–";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  const text = String(value);
  if (["", "none", "nan", "nat"].includes(text.trim().toLowerCase())) return "–";
  if (/^\d{4}-\d{2}-\d{2}T00:00:00/.test(text)) return text.slice(0, 10);
  return text;
}

/** The Steering Console with the guided scenario builder prefilled: scenario (a guided action id), project and its
 * inputs (e.g. risk). */
export function consoleUrl(project: string, action = "budget_change", extra: Record<string, string> = {}): string {
  const params = new URLSearchParams({ action, project, ...extra });
  return `/console?${params.toString()}`;
}

/** Readable column titles: snake_case -> "Status date"; explicit labels win. */
export const titled = (keys: string[], labels: Record<string, string> = {}): Column[] =>
  keys.map((k) => ({ key: k, label: labels[k] ?? k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) }));

// short columns that never break inside a cell (dates, amounts, states)
const NOWRAP = new Set(["Submitted", "Due", "Deadline", "Budget Δ", "Verdict", "Status", "From", "To", "Date", "Days left",
  "Required by", "Due date", "Exposure (€)", "Status date", "Created", "End", "Version", "Planned", "Actual", "Δ",
  "Current", "Proposed", "Severity", "ID", "Budget", "Actual cost", "EAC", "ROI", "Planned end", "RAG", "Progress"]);

export type Column = { key: string; label: string; render?: (row: Record<string, unknown>) => ReactNode };

/** A read-only business table; with `onSelect` one row can be picked (radio button or a click on the row). Wide
 * tables scroll inside their box. */
export function DataTable({ columns, rows, caption, selected, onSelect, rowKey = "_key", radio = true }: {
  columns: Column[];
  rows: Record<string, unknown>[];
  caption?: string;
  selected?: string | null;
  onSelect?: (key: string) => void;
  rowKey?: string;
  radio?: boolean;
}) {
  return (
    <div className="table-wrap">
      <table className="dk-table">
        {caption && <caption className="visually-hidden">{caption}</caption>}
        <thead>
          <tr>
            {onSelect && radio && <th scope="col"><span className="visually-hidden">Select</span></th>}
            {columns.map((c) => <th key={c.key} scope="col" className={NOWRAP.has(c.label) ? "nowrap" : undefined}>{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const key = String(row[rowKey] ?? i);
            const isSel = selected === key;
            return (
              <tr key={key} className={`${onSelect ? "selectable" : ""} ${isSel ? "selected" : ""}`} onClick={onSelect ? () => onSelect(key) : undefined}>
                {onSelect && radio && (
                  <td>
                    <input type="radio" name={caption ?? "row"} checked={isSel} onChange={() => onSelect(key)}
                      aria-label={`Select row ${i + 1}`} style={{ width: "auto" }} />
                  </td>
                )}
                {columns.map((c) => (
                  <td key={c.key} className={NOWRAP.has(c.label) ? "nowrap" : "wrap"}>{c.render ? c.render(row) : cell(row[c.key])}</td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function Notice({ tone = "info", icon, children }: { tone?: Tone; icon?: string; children: ReactNode }) {
  return (
    <div className={`callout tone-${tone}`}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        {icon && <Icon name={icon} />}
        <div style={{ color: "var(--dk-text)" }}>{children}</div>
      </div>
    </div>
  );
}

export function Caption({ icon, tone, children }: { icon?: string; tone?: Tone; children: ReactNode }) {
  return (
    <div className="row small" style={{ color: tone ? `var(--dk-${tone})` : "var(--dk-text-muted)" }}>
      {icon && <Icon name={icon} size={16} />}
      <span>{children}</span>
    </div>
  );
}

/** Several values of a list ("All" while none is picked): checkboxes in a drop-down. */
export function MultiSelect({ label, options, value, onChange }: { label: string; options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="field">
      <span id={`ms-${label}`}>{label}</span>
      <details className="dk-multi">
        <summary aria-labelledby={`ms-${label}`}>{value.length ? value.join(", ") : "All"}</summary>
        <div className="dk-multi-list" role="group" aria-label={label}>
          {options.map((o) => (
            <label key={o} className="row">
              <input type="checkbox" style={{ width: "auto" }} checked={value.includes(o)}
                onChange={(e) => onChange(e.target.checked ? [...value, o] : value.filter((v) => v !== o))} />
              {o}
            </label>
          ))}
        </div>
      </details>
    </div>
  );
}

/** The outcome of an action, shown at the top of the page until the next one (with the API's own words). */
export type Flash = { kind: string; message: string } | null;
export function FlashBox({ flash, onClose }: { flash: Flash; onClose: () => void }) {
  if (!flash) return null;
  const tone: Tone = flash.kind === "success" ? "success" : flash.kind === "error" ? "danger" : flash.kind === "warning" ? "warning" : "info";
  return (
    <div className={`callout tone-${tone}`} role={flash.kind === "error" ? "alert" : "status"}>
      <div className="row">
        <Icon name={tone === "success" ? "check_circle" : tone === "danger" ? "error" : "info"} />
        <span style={{ color: "var(--dk-text)", flex: 1, whiteSpace: "pre-wrap" }}>{flash.message}</span>
        <button className="flat" aria-label="Close message" onClick={onClose}><Icon name="close" /></button>
      </div>
    </div>
  );
}

/** A modal dialog (Escape or Cancel closes it). */
export function Dialog({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && !el.open) el.showModal();
    return () => el?.close();
  }, []);
  return (
    <dialog ref={ref} className="dk-dialog" aria-label={title} onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <h2 style={{ marginTop: 0 }}>{title}</h2>
      {children}
    </dialog>
  );
}

/** One confirmation for an action that cannot simply be undone: what happens, Cancel / confirm. */
export function Confirm({ title, message, confirmLabel, icon, onConfirm, onClose }: {
  title: string; message: ReactNode; confirmLabel: string; icon?: string; onConfirm: () => void; onClose: () => void;
}) {
  return (
    <Dialog title={title} onClose={onClose}>
      <p>{message}</p>
      <div className="row" style={{ justifyContent: "flex-end", flexWrap: "wrap" }}>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={() => { onClose(); onConfirm(); }}>{icon && <Icon name={icon} />}{confirmLabel}</button>
      </div>
    </Dialog>
  );
}

/** Page-local styles of the two pages (colours only through the theme variables). */
export const PAGE_CSS = `
.dk-table td.wrap { min-width: 200px; }
.dk-form-grid :disabled { opacity: 0.6; cursor: not-allowed; }
.dk-table td.nowrap, .dk-table th.nowrap { white-space: nowrap; }
.dk-table tr.selectable { cursor: pointer; }
.dk-table tr.selected td { background: var(--dk-info-bg); }
.dk-table tr.selectable:hover td { background: var(--dk-surface); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.dk-dialog { width: min(760px, calc(100vw - 32px)); border: 1px solid var(--dk-border); border-radius: var(--dk-radius-card);
  background: var(--dk-surface-raised); color: var(--dk-text); padding: 20px; }
.dk-dialog::backdrop { background: rgba(0,0,0,0.45); }
.dk-dialog > * + * { margin-top: 12px; }
.dk-seg { align-self: flex-start; display: inline-flex; flex-wrap: wrap; border: 1px solid var(--dk-border); border-radius: var(--dk-radius-control); overflow: hidden; }
.dk-seg button { border: 0; border-radius: 0; color: var(--dk-text); }
.dk-seg button[aria-pressed="true"] { background: var(--dk-primary); color: var(--dk-on-primary); }
.dk-tabs { display: flex; gap: 2px; overflow-x: auto; border-bottom: 1px solid var(--dk-border); }
.dk-tabs button { border: 0; border-bottom: 2px solid transparent; border-radius: 0; color: var(--dk-text); white-space: nowrap; }
.dk-tabs button[aria-selected="true"] { color: var(--dk-primary); border-bottom-color: var(--dk-primary); }
.dk-form-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); align-items: end; }
.dk-filters { display: grid; gap: 12px; grid-template-columns: 1.6fr 1fr 1.4fr auto; align-items: end; }
@media (max-width: 900px) { .dk-filters { grid-template-columns: 1fr; } }
.dk-multi { position: relative; }
.dk-multi > summary { list-style: none; border: 1px solid var(--dk-border); border-radius: var(--dk-radius-control);
  padding: 8px 10px; background: var(--dk-surface-raised); color: var(--dk-text); font-weight: 400; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.dk-multi-list { position: absolute; z-index: 5; left: 0; right: 0; background: var(--dk-surface-raised); color: var(--dk-text);
  border: 1px solid var(--dk-border); border-radius: var(--dk-radius-control); padding: 8px; display: flex; flex-direction: column;
  gap: 6px; max-height: 260px; overflow: auto; box-shadow: 0 6px 24px rgba(0,0,0,0.18); }
.field-missing { color: var(--dk-danger); font-weight: 600; }
.dk-paper { overflow-x: auto; white-space: pre-wrap; font-family: inherit; margin: 0; }
details > summary { cursor: pointer; padding: 8px 0; font-weight: 600; }
.dk-progress { width: 60px; height: 6px; background: var(--dk-border); border-radius: 3px; overflow: hidden; }
.dk-progress > span { display: block; height: 100%; background: var(--dk-primary); }
.dk-h3 { font-size: 1rem; margin: 12px 0 6px; }
`;

export function PageStyles() {
  return <style>{PAGE_CSS}</style>;
}
