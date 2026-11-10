// The project detail page: header with status badges, the buttons into the Steering Console, the key figures and one
// tab per question (master data, status history, costs & baselines, benefits, schedule & dependencies, risks, data
// quality and – for the owning Project Manager – the edit form).
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, type Answer } from "../../api/client";
import { AskBox } from "../../components/AskBox";
import { PlotlyFigure } from "../../components/PlotlyFigure";
import { Badge, Card, Icon, Kpi } from "../../components/ui";
import { tone, type Tone } from "../../theme";
import { EditForm } from "./EditForm";
import { Caption, DataTable, cell, consoleUrl, eur, titled, type Flash } from "./common";

export type Detail = Answer<"/api/v1/projects/{project_id}">;
type Row = Record<string, unknown>;

const DQ_BADGES: Record<string, [string, Tone, string]> = {
  blocked: ["Blocked for simulation", "danger", "block"], warnings: ["Data warnings", "warning", "warning"], ok: ["Data ok", "success", "check"],
};

function Table({ rows, labels = {}, caption }: { rows: Row[]; labels?: Record<string, string>; caption: string }) {
  const keys = rows.length ? Object.keys(rows[0]) : [];
  return <DataTable caption={caption} columns={titled(keys, labels)} rows={rows.map((r, i) => ({ ...Object.fromEntries(keys.map((k) => [k, cell(r[k])])), _key: String(i) }))} />;
}

export function ProjectDetail({ pid, detail: d, done }: { pid: string; detail: Detail; done: (f: Flash) => void }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const h = d.header;
  const findings = d.data_quality_findings;
  const names: Record<string, string> = Object.fromEntries(d.tabs.map((t) => [t, t === "Data quality" ? `Data quality (${findings.length})` : t]));
  const current = d.tabs.includes(params.get("tab") ?? "") ? params.get("tab")! : d.tabs[0];
  const tabs = useRef<HTMLDivElement>(null);
  useEffect(() => { // a deep link to a later tab: keep it in view on a phone
    tabs.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [current]);
  const go = (action?: string, extra?: Record<string, string>) => navigate(consoleUrl(pid, action, extra));
  const over = h.eac_vs_budget;
  const [dqText, dqTone, dqIcon] = DQ_BADGES[h.data_quality] ?? DQ_BADGES.ok;

  return (
    <>
      <div><Link to="/projects" className="row" style={{ textDecoration: "none", display: "inline-flex" }}><Icon name="arrow_back" />All projects</Link></div>
      <Card>
        <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-start", gap: 16 }}>
          <div style={{ flex: "1 1 280px" }}>
            <h1>{pid} – {String(d.project.project_name ?? "")}</h1>
            <div className="row" style={{ flexWrap: "wrap", marginTop: 8 }}>
              <Badge text={h.rag ?? "–"} tone={tone("rag", h.rag)} />
              <Badge text={h.state ?? "–"} />
              <Badge text={h.execution_type ?? "–"} />
              <Badge text={`Phase: ${h.phase || "–"}`} tone="info" icon="flag" />
              <Badge text={dqText} tone={dqTone} icon={dqIcon} />
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: "0 1 300px" }}>
            <button onClick={() => go()}><Icon name="query_stats" />Simulate a change for this project</button>
            <button onClick={() => go("stage_gate")}><Icon name="flag" />Check stage gate</button>
          </div>
        </div>
        <div className="grid" style={{ marginTop: 16 }}>
          <Kpi label="Budget" value={eur(h.budget)} />
          <Kpi label="Actual" value={eur(h.actual)} caption={h.actual_pct_of_budget != null ? `${h.actual_pct_of_budget.toFixed(0)} % of budget` : undefined} />
          <Card>
            <div className="muted">EAC</div>
            <div className="kpi-value">{eur(h.eac)}</div>
            {over != null && (
              <div className="small" style={{ color: over >= 1 ? "var(--dk-danger)" : "var(--dk-text-muted)", fontWeight: over >= 1 ? 600 : 400 }}>
                {Math.abs(over) < 1 ? "On budget" : `${eur(over, true)} vs budget`}
              </div>
            )}
          </Card>
          <Kpi label="Progress" value={`${(h.progress ?? 0).toFixed(0)} %`} />
          <Card>
            <div className="muted">Planned end</div>
            <div className="kpi-value">{h.planned_end ?? "–"}</div>
            {h.planned_end && h.days_to_end != null && (
              <div className="small" style={h.days_to_end >= 0 ? { color: "var(--dk-text-muted)" } : { color: "var(--dk-danger)", fontWeight: 600 }}>
                {h.days_to_end >= 0 ? `in ${h.days_to_end} days` : `${-h.days_to_end} days overdue`}
              </div>
            )}
          </Card>
        </div>
        {d.missing_required.length > 0 && (
          <div className="row" style={{ color: "var(--dk-danger)", fontWeight: 600, marginTop: 8 }}>
            <Icon name="error" />Required fields missing: {d.missing_required.join(", ")}
          </div>
        )}
        <div ref={tabs} className="dk-tabs" role="tablist" aria-label="Project detail" style={{ marginTop: 16 }}>
          {d.tabs.map((t) => (
            <button key={t} role="tab" id={`tab-${t}`} aria-selected={t === current} aria-controls="tab-panel"
              onClick={() => setParams({ project: pid, tab: t }, { replace: true })}>{names[t]}</button>
          ))}
        </div>
        <div role="tabpanel" id="tab-panel" aria-labelledby={`tab-${current}`} style={{ paddingTop: 12 }}>
          {current === "Master data" && <MasterData d={d} />}
          {current === "Status history" && (d.status_reports.length
            ? <Table caption="Status history" rows={d.status_reports} labels={{ issues_decisions: "Issues & decisions" }} />
            : <Caption>No status reports recorded.</Caption>)}
          {current === "Costs & baselines" && <Costs d={d} onRebaseline={() => go("rebaseline")} />}
          {current === "Benefits" && <Benefits pid={pid} d={d} done={done} onReview={() => go("benefit_review")} />}
          {current === "Schedule & dependencies" && <Schedule d={d} />}
          {current === "Risks" && <Risks d={d} onPlan={(risk) => go("risk_response", { risk })} />}
          {current === "Data quality" && (findings.length ? (
            <DataTable caption="Data Steward findings" columns={titled(["Severity", "Rule", "Field", "Finding"])}
              rows={findings.map((f, i) => ({ _key: String(i), Severity: f.severity, Rule: f.rule_label, Field: f.field_label || "–", Finding: f.message }))} />
          ) : <Caption icon="check_circle">No Data Steward findings for this project.</Caption>)}
          {current === "Edit" && (
            <>
              <Caption>Master data can be edited by the owning Project Manager; budget changes go through the Steering Console;
                governed fields are submitted as a proposal to the PMO inbox.</Caption>
              <EditForm pid={pid} findings={findings} done={done} />
            </>
          )}
        </div>
        {d.access !== "edit" && <div style={{ marginTop: 12 }}><Caption icon="lock">Read-only – master data is maintained by the owning Project Manager.</Caption></div>}
      </Card>
      <AskBox page="project" projectId={pid} title="Ask about this project" />
    </>
  );
}

function MasterData({ d }: { d: Detail }) {
  const md = d.master_data;
  const cols = titled(["Field", "Value"]);
  return (
    <>
      <div className="grid-2">
        <div>
          <h3 className="dk-h3">Master data</h3>
          <DataTable caption="Master data" columns={cols} rows={md.direct.map((r, i) => ({ ...r, _key: String(i) }))} />
        </div>
        <div>
          <h3 className="dk-h3">Governed fields · changes need PMO approval</h3>
          <DataTable caption="Governed fields" columns={cols} rows={md.governed.map((r, i) => ({ ...r, _key: String(i) }))} />
        </div>
      </div>
      <h3 className="dk-h3">Business case</h3>
      {md.business_case ? <p style={{ whiteSpace: "pre-wrap" }}>{md.business_case}</p> : <p className="muted"><em>No business case recorded.</em></p>}
    </>
  );
}

function Costs({ d, onRebaseline }: { d: Detail; onRebaseline: () => void }) {
  const c = d.costs;
  return (
    <>
      {!c.curve.length || !c.figure ? <Caption>No cost plan recorded.</Caption> : (
        <>
          <PlotlyFigure figure={c.figure as { data?: unknown[]; layout?: Record<string, unknown> }} />
          {c.caption && <Caption>{c.caption}</Caption>}
          <h3 className="dk-h3">Baselines</h3>
        </>
      )}
      {!c.baselines.length ? <Caption>No baseline recorded.</Caption> : (
        <>
          <Table caption="Baselines" rows={c.baselines as Row[]} />
          {c.planned_vs_approved_end_days != null && (
            <Caption>Planned end vs. approved end: {c.planned_vs_approved_end_days >= 0 ? "+" : ""}{c.planned_vs_approved_end_days} days.</Caption>
          )}
        </>
      )}
      <div style={{ marginTop: 8 }}><button onClick={onRebaseline}><Icon name="restart_alt" />Re-baseline</button></div>
    </>
  );
}

function Benefits({ pid, d, done, onReview }: { pid: string; d: Detail; done: (f: Flash) => void; onReview: () => void }) {
  const b = d.benefits;
  const r = b.realisation;
  const [plan, setPlan] = useState(b.plan_options[0] ?? "");
  const [amount, setAmount] = useState("0");
  const [currency, setCurrency] = useState(b.currencies[0] ?? "EUR");
  const [month, setMonth] = useState(b.default_month ?? ""); // booked to the first of the month
  const [comment, setComment] = useState("");
  const colour: Tone = ({ red: "danger", orange: "warning", green: "success" } as Record<string, Tone>)[b.colour ?? ""] ?? "neutral";
  const save = async () => {
    const value = Number(amount) || 0;
    if (value <= 0) return done({ kind: "error", message: "Please enter the actual benefit (> 0)." });
    try {
      done(await api.post<{ kind: string; message: string }>(`/projects/${encodeURIComponent(pid)}/benefit-actuals`,
        { amount: value, plan, currency, month: month || null, comment }));
    } catch (e) {
      done({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };
  return (
    <>
      {!b.tranches.length ? <Caption>No planned benefit recorded.</Caption> : (
        <>
          <Table caption="Benefit tranches" rows={b.tranches as Row[]} />
          {r.pct == null ? <Caption>No benefit tranche is due yet.</Caption> : (
            <p><strong style={{ color: `var(--dk-${colour})` }}>{r.pct} % realised</strong> – {eur(r.actual)} of {eur(r.planned_due)} due
              (corrective plan below {b.min_pct} %).</p>
          )}
        </>
      )}
      {b.currency !== "EUR" && <Caption>Amounts of this project are kept in {b.currency} and shown in EUR (rate {b.fx_rate}).</Caption>}
      <div style={{ marginTop: 8 }}><button onClick={onReview}><Icon name="insights" />Review benefit realisation</button></div>
      {b.can_report && (
        <details className="card" style={{ marginTop: 12, height: "auto" }}>
          <summary><Icon name="add_chart" /> Report an actual benefit</summary>
          <div className="dk-form-grid">
            <label className="field">Tranche
              <select value={plan} onChange={(e) => setPlan(e.target.value)}>
                {b.plan_options.map((o) => <option key={o} value={o}>{o || "not assigned"}</option>)}
              </select>
            </label>
            <label className="field">Actual benefit<input type="number" min={0} step={10000} value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
            <label className="field">Currency
              <select value={currency} onChange={(e) => setCurrency(e.target.value)}>{b.currencies.map((c) => <option key={c}>{c}</option>)}</select>
            </label>
            <label className="field">Month<input type="date" value={month} onChange={(e) => setMonth(e.target.value)} /></label>
          </div>
          <label className="field" style={{ marginTop: 12 }}>Comment<input value={comment} onChange={(e) => setComment(e.target.value)} /></label>
          <div style={{ marginTop: 12 }}><button className="primary" onClick={() => void save()}><Icon name="save" />Save actual benefit</button></div>
        </details>
      )}
    </>
  );
}

function Schedule({ d }: { d: Detail }) {
  const labels = { type: "Dependency type", required_by: "Required by" };
  return (
    <>
      <h3 className="dk-h3">Deadlines</h3>
      {!d.deadlines.length ? <Caption>No dated milestones.</Caption> : (
        <Table caption="Deadlines" labels={{ days: "Days left", item: "Milestone / project" }}
          rows={d.deadlines.map((r) => ({ ...r, days: r.days < 0 ? `${r.days} (overdue)` : String(r.days) }))} />
      )}
      <h3 className="dk-h3">Dependencies</h3>
      <div style={{ fontWeight: 500, margin: "8px 0 4px" }}>Waits for (incoming)</div>
      {!d.dependencies.incoming.length ? <Caption>None.</Caption> : <Table caption="Waits for" rows={d.dependencies.incoming} labels={labels} />}
      <div style={{ fontWeight: 500, margin: "8px 0 4px" }}>Is needed by (outgoing)</div>
      {!d.dependencies.outgoing.length ? <Caption>None.</Caption> : <Table caption="Is needed by" rows={d.dependencies.outgoing} labels={labels} />}
    </>
  );
}

function Risks({ d, onPlan }: { d: Detail; onPlan: (risk: string) => void }) {
  const ids = d.risks.map((r) => String(r.risk_number));
  const [risk, setRisk] = useState(ids[0] ?? "");
  if (!d.risks.length) return <Caption>No risks recorded.</Caption>;
  return (
    <>
      <Table caption="Risks" rows={d.risks.map((r) => ({ ...r, exposure_eur: eur(r.exposure_eur) }))}
        labels={{ risk_number: "Risk ID", short_description: "Risk", risk_status: "Status", systemic_risk_cluster: "Risk cluster", exposure_eur: "Exposure (€)" }} />
      <Caption>Total exposure: {eur(d.total_risk_exposure_eur)} · probability × impact on the risk scale of the business rules.</Caption>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-end", marginTop: 8 }}>
        <label className="field" style={{ flex: "0 1 320px" }}>Risk
          <select value={risk} onChange={(e) => setRisk(e.target.value)}>{ids.map((i) => <option key={i}>{i}</option>)}</select>
        </label>
        <button onClick={() => onPlan(risk)}><Icon name="shield" />Plan response</button>
      </div>
    </>
  );
}
