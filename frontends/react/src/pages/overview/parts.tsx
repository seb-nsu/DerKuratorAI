// Pieces the Overview and Data quality pages share: one API answer per section (loading, error box, data) and the
// small text blocks of the Streamlit pages (caption, info callout, metric).
import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "../../api/client";
import { ErrorBox, Icon } from "../../components/ui";

export type Loaded<T> = { data?: T; error?: string };

export function useApi<T>(path: string | null): Loaded<T> | null {
  const [state, setState] = useState<Loaded<T> | null>(null);
  useEffect(() => {
    if (!path) return;
    let live = true;
    setState(null);
    api.get<T>(path).then(
      (data) => live && setState({ data }),
      (e) => live && setState({ error: e instanceof ApiError ? e.message : String(e) }),
    );
    return () => {
      live = false;
    };
  }, [path]);
  return state;
}

export function Section<T>({ state, children }: { state: Loaded<T> | null; children: (data: T) => ReactNode }) {
  if (!state) return <div className="muted" aria-busy="true">Loading …</div>;
  if (state.error !== undefined) return <ErrorBox message={state.error} />;
  return <>{children(state.data as T)}</>;
}

export const Caption = ({ text, icon }: { text: string; icon?: string }) => (
  <div className="row muted small">{icon && <Icon name={icon} size={16} />}{text}</div>
);

export const Info = ({ text, icon = "info" }: { text: string; icon?: string }) => (
  <div className="callout tone-info" role="status"><div className="row"><Icon name={icon} /><span style={{ color: "var(--dk-text)" }}>{text}</span></div></div>
);

export function Metric({ label, value, caption, help }: { label: string; value: string; caption?: string; help?: string }) {
  return (
    <div>
      <div className="row muted">
        {label}
        {help && <span title={help}><Icon name="help" size={16} label={help} /></span>}
      </div>
      <div className="kpi-value">{value}</div>
      {caption && <div className="muted small">{caption}</div>}
    </div>
  );
}
