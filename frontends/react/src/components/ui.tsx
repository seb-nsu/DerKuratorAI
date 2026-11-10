// Small building blocks every page uses, so all pages look alike (same set as the NiceGUI front end).
import { type ReactNode } from "react";
import { tone, type Tone } from "../theme";

export const Icon = ({ name, size = 20, label }: { name: string; size?: number; label?: string }) => (
  <span className="icon material-symbols-outlined" style={{ fontSize: size }} aria-hidden={label ? undefined : true} aria-label={label}>
    {name}
  </span>
);

export const Mark = () => <span className="mark" aria-hidden="true">DK</span>;

export function Logo({ wordmark = true }: { wordmark?: boolean }) {
  return (
    <span className="row">
      <Mark />
      {wordmark && <strong>DerKurator AI</strong>}
    </span>
  );
}

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  const head = (
    <div>
      <h1>{title}</h1>
      {subtitle && <div className="muted">{subtitle}</div>}
    </div>
  );
  if (!action) return head;
  // a sub-page link (Data quality, What-if comparison) sits next to the heading
  return <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap" }}>{head}{action}</div>;
}

export function Card({ title, children }: { title?: string; children?: ReactNode }) {
  return (
    <section className="card">
      {title && <div style={{ fontWeight: 600, marginBottom: 8 }}>{title}</div>}
      {children}
    </section>
  );
}

export function Badge({ text, tone: t = "neutral", icon }: { text: string; tone?: Tone; icon?: string }) {
  return (
    <span className={`badge tone-${t}`}>
      {icon && <Icon name={icon} size={16} />}
      {text}
    </span>
  );
}

export function Kpi({ label, value, caption, help }: { label: string; value: string; caption?: string; help?: string }) {
  return (
    <Card>
      <div className="row muted">
        {label}
        {help && <span title={help}><Icon name="help" size={16} label={help} /></span>}
      </div>
      <div className="kpi-value">{value}</div>
      {caption && <div className="muted small">{caption}</div>}
    </Card>
  );
}

const VERDICT_ICONS: Record<Tone, string> = { success: "check_circle", warning: "warning", danger: "block", neutral: "help", info: "info" };

export type Verdict = { label?: string; reasons?: string[]; conditions?: string[] } | null | undefined;

export function VerdictCallout({ verdict }: { verdict: Verdict }) {
  if (!verdict) return null;
  const t = tone("verdict", verdict.label);
  return (
    <div className={`callout tone-${t}`} role="status">
      <div className="row" style={{ fontSize: "1.2rem", fontWeight: 700 }}>
        <Icon name={VERDICT_ICONS[t]} size={24} />
        {verdict.label}
      </div>
      {(verdict.reasons ?? []).map((r, i) => <p key={`r${i}`}>{r}</p>)}
      {(verdict.conditions ?? []).map((c, i) => <p key={`c${i}`}>Condition: {c}</p>)}
    </div>
  );
}

export const ErrorBox = ({ message }: { message: string }) => (
  <div className="callout tone-danger" role="alert">{message}</div>
);

export const Empty = ({ message }: { message: string }) => <div className="muted" style={{ padding: 16 }}>{message}</div>;
