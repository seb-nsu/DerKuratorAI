// Building blocks of the Admin Console: data loading with reload, admin actions with their message, the confirmation
// dialog of destructive actions, callouts, tables and log blocks. Same set as frontends/nicegui/pages/admin_common.py.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, session } from "../../api/client";
import { ErrorBox, Icon } from "../../components/ui";

export type Kind = "success" | "info" | "warning" | "error";
const TONES: Record<Kind, [string, string, string]> = {
  success: ["success", "check_circle", "Success"],
  info: ["info", "info", "Note"],
  warning: ["warning", "warning", "Warning"],
  error: ["danger", "error", "Error"],
};

// `code` and **bold** of the API texts
export function Inline({ text }: { text: string }) {
  const parts = text.split("`");
  return (
    <>
      {parts.map((part, i) =>
        i % 2 ? <code key={i} className="adm-code">{part}</code> : part.split("**").map((chunk, j) => (j % 2 ? <strong key={`${i}.${j}`}>{chunk}</strong> : chunk)),
      )}
    </>
  );
}

export function Callout({ kind, text, children }: { kind: Kind; text?: string; children?: ReactNode }) {
  const [tone, icon, label] = TONES[kind];
  return (
    <div className={`callout tone-${tone} adm-callout`} role={kind === "error" ? "alert" : "status"}>
      <Icon name={icon} label={label} />
      <div>{text !== undefined ? <Inline text={text} /> : children}</div>
    </div>
  );
}

export const Caption = ({ text }: { text: string }) => <div className="muted small"><Inline text={text} /></div>;
export const Rich = ({ text }: { text: string }) => <div><Inline text={text} /></div>;
export const Pre = ({ text }: { text: string }) => <pre className="adm-pre" tabIndex={0}>{text}</pre>;

export function Details({ summary, children, onOpen, icon }: { summary: string; children: ReactNode; onOpen?: () => void; icon?: string }) {
  return (
    <details className="adm-details" onToggle={(e) => (e.currentTarget.open ? onOpen?.() : undefined)}>
      <summary>{icon && <Icon name={icon} />}{summary}</summary>
      <div className="adm-details-body">{children}</div>
    </details>
  );
}

const cell = (value: unknown): string =>
  value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);

// A read-only table; wide tables scroll inside their box. Without `columns` every key is a column (`front` first).
export function Table({ rows, columns, front = [], pageSize = 25 }: {
  rows: Record<string, unknown>[];
  columns?: [string, string][];
  front?: string[];
  pageSize?: number;
}) {
  const [page, setPage] = useState(0);
  let cols = columns;
  if (!cols) {
    const keys: string[] = [];
    rows.forEach((r) => Object.keys(r).forEach((k) => (keys.includes(k) ? null : keys.push(k))));
    cols = [...front.filter((k) => keys.includes(k)), ...keys.filter((k) => !front.includes(k))].map((k) => [k, k]);
  }
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const shown = rows.slice(page * pageSize, (page + 1) * pageSize);
  return (
    <div>
      <div className="table-wrap">
        <table className="adm-table">
          <thead><tr>{cols.map(([k, label]) => <th key={k} scope="col">{label}</th>)}</tr></thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={i}>{cols!.map(([k]) => <td key={k} title={cell(row[k])}>{cell(row[k])}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="row small muted" style={{ justifyContent: "flex-end", marginTop: 4 }}>
          <button className="flat" aria-label="Previous page" disabled={page === 0} onClick={() => setPage(page - 1)}><Icon name="chevron_left" /></button>
          {page * pageSize + 1}–{Math.min(rows.length, (page + 1) * pageSize)} of {rows.length}
          <button className="flat" aria-label="Next page" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}><Icon name="chevron_right" /></button>
        </div>
      )}
    </div>
  );
}

export const stamp = (value: unknown) => {
  const text = String(value ?? "").replace("T", " ");
  return text ? text.slice(0, 16) : "–";
};

export const localStamp = (iso: string | null | undefined) => {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

// ------------------------------------------------------------------ messages, confirmation, actions
type Toast = { id: number; kind: Kind; message: string };
type Confirm = { label: string; message: string; confirmLabel?: string; resolve: (ok: boolean) => void };
type Console = {
  notify: (kind: Kind, message: string) => void;
  confirm: (label: string, message: string, confirmLabel?: string) => Promise<boolean>;
  signInAgain: () => void;
};
const ConsoleContext = createContext<Console | null>(null);

export function useConsole(): Console {
  const value = useContext(ConsoleContext);
  if (!value) throw new Error("useConsole outside the Admin Console");
  return value;
}

export function ConsoleProvider({ children, home = "/admin" }: { children: ReactNode; home?: string }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [pending, setPending] = useState<Confirm | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const navigate = useNavigate();
  const notify = useCallback((kind: Kind, message: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 12000 : 6000);
  }, []);
  const confirm = useCallback(
    (label: string, message: string, confirmLabel?: string) => new Promise<boolean>((resolve) => setPending({ label, message, confirmLabel, resolve })),
    [],
  );
  const signInAgain = useCallback(() => navigate(`/sign-in?next=${encodeURIComponent(home)}`), [navigate, home]);
  useEffect(() => {
    if (pending && dialog.current && !dialog.current.open) dialog.current.showModal();
  }, [pending]);
  const close = (ok: boolean) => {
    dialog.current?.close();
    pending?.resolve(ok);
    setPending(null);
  };
  return (
    <ConsoleContext.Provider value={{ notify, confirm, signInAgain }}>
      {children}
      <div className="adm-toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="adm-toast">
            <Callout kind={t.kind} text={t.message} />
          </div>
        ))}
      </div>
      <dialog ref={dialog} className="adm-dialog" aria-labelledby="adm-dialog-title" onCancel={(e) => { e.preventDefault(); close(false); }}>
        {pending && (
          <div className="card" style={{ height: "auto" }}>
            <h2 id="adm-dialog-title" style={{ marginTop: 0 }}>Confirm: {pending.label}</h2>
            <div style={{ margin: "12px 0" }}><Callout kind="warning" text={pending.message} /></div>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button autoFocus onClick={() => close(false)}>Cancel</button>
              <button className="adm-danger" onClick={() => close(true)}>{pending.confirmLabel ?? pending.label}</button>
            </div>
          </div>
        )}
      </dialog>
    </ConsoleContext.Provider>
  );
}

export type Action = { kind: Kind; message: string; [key: string]: unknown };

// Loads a section's data; `reload` after an action. 401 (token ended, e.g. after an environment switch): sign in again.
export function useLoad<T>(load: () => Promise<T>): { data: T | null; error: string | null; reload: () => Promise<void> } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { signInAgain } = useConsole();
  const ref = useRef(load);
  ref.current = load;
  const reload = useCallback(async () => {
    try {
      setData(await ref.current());
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return signInAgain();
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [signInAgain]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, reload };
}

// Runs an admin action and shows its message. Returns null when it failed.
export function useAct() {
  const { notify, signInAgain } = useConsole();
  return useCallback(
    async (method: "POST" | "PUT", path: string, body: unknown = {}): Promise<Action | null> => {
      try {
        const answer = await (method === "PUT" ? api.put<Action>(path, body) : api.post<Action>(path, body));
        if (answer?.message) notify(answer.kind ?? "success", answer.message);
        return answer ?? ({ kind: "success", message: "" } as Action);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          signInAgain();
          return null;
        }
        notify("error", e instanceof Error ? e.message : String(e));
        return null;
      }
    },
    [notify, signInAgain],
  );
}

// A section body: loading, the API's error in the section only (a broken section never hides the others), content.
export function Loaded<T>({ state, children }: { state: { data: T | null; error: string | null }; children: (data: T) => ReactNode }) {
  if (state.error) return <ErrorBox message={state.error} />;
  if (state.data === null) return <div className="muted" aria-busy="true">Loading …</div>;
  return <>{children(state.data)}</>;
}

// A non-JSON answer (the HTML test report) with the signed-in user's token.
export async function getText(path: string): Promise<string> {
  const token = session.token();
  const response = await fetch(`/api/v1${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (response.status === 401) session.clear();
  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return response.text();
}

export const fmt = (n: number) => n.toLocaleString("en-US");
