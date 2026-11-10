// Planning: demands and their detail, the base plan analysis, plan scenarios, saved scenarios, reports, the PMO tools
// and the Portfolio Board's plan approval – the sections of the caller's role come from the API. The open section and
// demand are in the address (?tab=…&demand=…), so a view can be linked.
import { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api, type Answer } from "../api/client";
import { AskBox } from "../components/AskBox";
import { usePageTitle } from "../components/Shell";
import { Badge, Icon, Kpi, PageHeader } from "../components/ui";
import {
  Callout, Caption, DataTable, Dialog, Markdown, Notice, ScenarioVerdict, STATUS_TONES, Tabs, TitleLine, VerdictBadge,
  Loading, decision, eur, num, useLoad, useQueryParam, useSetQuery, type Note, type Row,
} from "./planning/parts";
import { Reports, SavedScenarios, Workshop, takePrefill, type Calculated } from "./planning/Scenarios";
import { PmoTools } from "./planning/Tools";

type Context = Answer<"/api/v1/planning">;
type Demands = Answer<"/api/v1/planning/demands">;
type Detail = Answer<"/api/v1/planning/demands/{demand_id}">;
type Slip = Answer<"/api/v1/planning/demands/{demand_id}/slip">;
type Analysis = Answer<"/api/v1/planning/analysis">;
type Board = Answer<"/api/v1/planning/board">;
type Plans = Answer<"/api/v1/planning/plans">;
type Plan = Answer<"/api/v1/planning/plans/{proposal_id}">;

const TITLES: Record<string, string> = {
  plan_approval: "Plan approval", demands: "Demands", analysis: "Analysis", scenarios: "Plan scenarios",
  saved_scenarios: "Saved plan scenarios", reports: "Reports", pmo_tools: "PMO tools",
};
const OUTSIDE_SCOPE_PREDECESSORS = "Further predecessors lie outside your scope – the PMO coordinates.";

// ------------------------------------------------------------------------- demands
function SlipView({ demand }: { demand: string }) {
  const [quarters, setQuarters] = useState(1);
  const { data, error } = useLoad(() => api.get<Slip>(`/planning/demands/${demand}/slip`, { quarters: String(quarters) }), [demand, quarters]);
  return (
    <div className="stack">
      <label className="field" style={{ maxWidth: 420 }}>If this demand slips by … quarters: {quarters}
        <input type="range" min={1} max={4} step={1} value={quarters} onChange={(e) => setQuarters(Number(e.target.value))} />
      </label>
      <Loading error={error}>{data && (data.moves.length === 0 ? <Caption>Nothing moves – every successor starts late enough.</Caption> : (
        <>
          <DataTable rows={data.moves} caption="What a delay moves along" />
          <Caption>{data.late ? `${data.moved} item(s) move; ${data.late} would start after their latest start.`
            : `${data.moved} item(s) move; all still start before their latest start.`}</Caption>
        </>
      ))}</Loading>
    </div>
  );
}

function Position({ position }: { position: NonNullable<Detail["position"]> }) {
  return (
    <div className="stack">
      {position.decision && <div>Base plan (rank order, today's data): <strong>{position.decision}</strong>{position.reason ? ` · ${position.reason}` : ""}</div>}
      <strong>Budget envelopes of its business unit and portfolio</strong>
      {position.budget.length === 0 ? <Caption>No annual figures or no envelopes.</Caption> : (
        <>
          <DataTable rows={position.budget} caption="Budget envelopes" />
          <Caption>Committed = running projects, Planned = demands in the plan. If the demand is not in the plan, “This demand” shows what it would add.</Caption>
        </>
      )}
      <strong>Team capacity</strong>
      {position.capacity.length === 0 ? <Caption>No capacity demand recorded.</Caption> : <DataTable rows={position.capacity} caption="Team capacity" />}
    </div>
  );
}

function DemandDetail({ id }: { id: string }) {
  const { data: d, error } = useLoad(() => api.get<Detail>(`/planning/demands/${id}`), [id]);
  const [tab, setTab] = useState("master");
  useEffect(() => setTab("master"), [id]);
  if (!d) return <Loading error={error}>{null}</Loading>;
  const head = d.demand;
  const status = head.demand_status ?? "–";
  const kind = head.demand_type ?? "–";
  const tabs = [
    { id: "master", title: "Master data" }, { id: "annual", title: "Annual figures" }, { id: "capacity", title: "Capacity" },
    { id: "assessment", title: "Assessment" }, { id: "dependencies", title: "Dependencies" },
    ...(d.position ? [{ id: "plan", title: "In the plan" }] : []),
  ];
  const capacity = d.capacity as Row[];
  return (
    <section className="card stack" style={{ height: "auto" }}>
      <h3 style={{ margin: 0 }}>{id} · {head.demand_name}</h3>
      <div className="badges">
        <Badge text={status} tone={STATUS_TONES[status] ?? "neutral"} />
        <Badge text={kind} tone={kind === "Mandatory" ? "danger" : "neutral"} />
        <Badge text={`${head.business_unit} · ${head.portfolio}`} />
      </div>
      <Caption>Owner: {head.demand_owner || "–"}</Caption>
      <div className="grid">
        <Kpi label="Duration" value={`${num(head.duration_months ?? 0)} months`} />
        <Kpi label="NPV" value={eur(head.net_present_value_eur ?? 0)} />
        <Kpi label="Run cost after go-live" value={`${eur(head.run_cost_after_go_live_eur ?? 0)} p.a.`} />
      </div>
      <Tabs label="Demand detail" tabs={tabs} active={tab} onChange={setTab} />
      <div role="tabpanel" className="stack">
        {tab === "master" && <DataTable rows={d.master_data.map((r) => ({ Field: r.label, Value: r.Value }))} caption="Master data" />}
        {tab === "annual" && (d.annual_figures.length === 0 ? <Caption>No annual figures.</Caption> : (
          <>
            <DataTable rows={d.annual_figures} caption="Annual figures" />
            <Caption>Total {eur(d.annual_totals.capex_eur)} CapEx and {eur(d.annual_totals.opex_eur)} OpEx.</Caption>
          </>
        ))}
        {tab === "capacity" && (capacity.length === 0 ? <Caption>No capacity demand recorded.</Caption> : (
          <DataTable rows={capacity} caption="Capacity" formats={Object.fromEntries(Object.keys(capacity[0]).filter((c) => c !== "Team").map((c) => [c, "fte" as const]))} />
        ))}
        {tab === "assessment" && (d.assessment.mandatory ? (
          <Callout icon="gavel">Mandatory demand ({head.mandatory_reason || "reason missing"}) – marked, not scored; it is planned before every scored demand.</Callout>
        ) : d.assessment.parts.length === 0 ? <Caption>Not assessed yet.</Caption> : (
          <>
            <DataTable rows={d.assessment.parts} caption="Assessment" formats={{ Score: "dec1", Points: "dec1", Weight: "dec1", Contribution: "dec1" }} />
            <Caption>Score {(d.assessment.score ?? 0).toFixed(1)} of 100 = sum of the weighted contributions.</Caption>
          </>
        ))}
        {tab === "dependencies" && (
          <>
            {d.dependencies.length === 0 ? <Caption>No dependencies within your view.</Caption>
              : <DataTable rows={d.dependencies} caption="Dependencies" labels={{ Steps: "Steps (1 = direct)" }} />}
            {d.outside_scope_links && <Caption>{OUTSIDE_SCOPE_PREDECESSORS}</Caption>}
            {d.can_slip && <SlipView demand={id} />}
          </>
        )}
        {tab === "plan" && d.position && <Position position={d.position} />}
      </div>
      <AskBox page="demand" demandId={id} title="Ask about this demand" />
    </section>
  );
}

function MultiSelect({ label, options, value, onChange }: { label: string; options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <label className="field">{label}
      <select multiple className="multi" value={value} onChange={(e) => onChange(Array.from(e.target.selectedOptions, (o) => o.value))}>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}

function DemandsSection() {
  const [units, setUnits] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [year, setYear] = useState("any year");
  const [demand, setDemand] = useQueryParam("demand");
  const all = useLoad(() => api.get<Demands>("/planning/demands"), []);
  const query = new URLSearchParams();
  units.forEach((u) => query.append("business_unit", u));
  statuses.forEach((s) => query.append("status", s));
  if (year !== "any year") query.set("year", year);
  const qs = query.toString();
  const shown = useLoad(() => api.get<Demands>(`/planning/demands${qs ? `?${qs}` : ""}`), [qs]);
  if (!all.data) return <Loading error={all.error}>{null}</Loading>;
  if (!all.data.demands.length) return <Callout>No demands within your permissions.</Callout>;
  const filters = all.data.filters;
  const years = new Set(filters.years);
  const rows = shown.data?.demands ?? [];
  const pick = demand && rows.some((r) => r.Demand === demand) ? demand : "";
  const findings = shown.data?.data_quality_findings ?? [];
  return (
    <div className="stack">
      <div className="grid">
        <MultiSelect label="Business unit" options={filters.business_units} value={units} onChange={setUnits} />
        <MultiSelect label="Status" options={filters.statuses} value={statuses} onChange={setStatuses} />
        <label className="field">Has money in
          <select value={year} onChange={(e) => setYear(e.target.value)}>
            {["any year", ...filters.years].map((y) => <option key={y}>{y}</option>)}
          </select>
        </label>
      </div>
      <Loading error={shown.error}>{shown.data && (
        <>
          {rows.length ? (
            <DataTable rows={rows} scroll caption="Demands"
              formats={{ Score: "score", ...Object.fromEntries(Object.keys(rows[0]).filter((c) => years.has(c)).map((c) => [c, "eur" as const])) }} />
          ) : <Caption>No demand matches the filters.</Caption>}
          {shown.data.rank_shown && <Caption>Rank: mandatory demands first, then score, then NPV, then the latest start, then the number.</Caption>}
          {findings.length > 0 && (
            <details className="card" style={{ height: "auto" }}>
              <summary><Icon name="fact_check" />Data quality of the demands · {findings.length} finding(s)</summary>
              <DataTable caption="Data quality findings" rows={findings.map((f) => ({ Demand: f.demand, Severity: f.severity, Field: f.field, Finding: f.message }))} />
            </details>
          )}
          <AskBox page="demands" title="Ask about the demands"
            filters={{ business_unit: units, status: statuses, year: year === "any year" ? null : year }} />
          <label className="field">Open demand
            <select value={pick} onChange={(e) => setDemand(e.target.value || null)}>
              <option value="">Select a demand ...</option>
              {rows.map((r) => <option key={String(r.Demand)} value={String(r.Demand)}>{`${r.Demand} · ${r.Name}`}</option>)}
            </select>
          </label>
          {pick && <DemandDetail id={pick} />}
        </>
      )}</Loading>
    </div>
  );
}

// ------------------------------------------------------------------------- analysis
function AnalysisSection() {
  const { data: plan, error } = useLoad(() => api.get<Analysis>("/planning/analysis"), []);
  if (!plan) return <Loading error={error}>{null}</Loading>;
  const share = `${plan.hot_share.toFixed(0)} %`;
  return (
    <div className="stack">
      <ScenarioVerdict header={plan} />
      <div className="grid-2">
        <div className="stack">
          <TitleLine title="Budget hot spots" rest={`envelopes used to ${share} or more`} />
          {plan.budget_hot_spots.length ? <DataTable rows={plan.budget_hot_spots} caption="Budget hot spots" /> : <Caption>None.</Caption>}
        </div>
        <div className="stack">
          <TitleLine title="Capacity hot spots" rest={`team quarters used to ${share} or more`} />
          {plan.capacity_hot_spots.length ? <DataTable rows={plan.capacity_hot_spots} caption="Capacity hot spots" /> : <Caption>None.</Caption>}
        </div>
      </div>
      <TitleLine title="Critical chains" rest="demands other demands or projects wait for – a delay here moves them too" />
      {plan.critical_chains.length ? <DataTable rows={plan.critical_chains} caption="Critical chains" /> : <Caption>No demand has successors.</Caption>}
      <AskBox page="plan" title="Ask about the plan" expanded />
    </div>
  );
}

// ------------------------------------------------------------------------- the Board's plan approval
function PlanCard({ summary, notify, done }: { summary: Plans["plans"][number]; notify: (n: Note) => void; done: () => void }) {
  const { data: p, error } = useLoad(() => api.get<Plan>(`/planning/plans/${summary.id}`), [summary.id]);
  const [comment, setComment] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [override, setOverride] = useState(false);
  const [reason, setReason] = useState("");
  if (!p) return <Loading error={error}>{null}</Loading>;
  const decide = async (approve: boolean) => {
    setConfirming(false);
    const body = approve
      ? { comment: p.requires_override ? reason.trim() : comment.trim(), override: p.requires_override && override }
      : { comment: comment.trim() };
    const answer = await decision(`/planning/plans/${p.id}/${approve ? "approve" : "reject"}`, body);
    notify({ kind: answer.kind, message: answer.message });
    if (answer.kind !== "error") done();
  };
  const ready = !p.requires_override || (override && reason.trim().length > 0);
  return (
    <section className="card stack" style={{ height: "auto" }}>
      <div className="row wrap">
        <VerdictBadge label={p.verdict} />
        <strong>{p.request}</strong>
        <span>· submitted by {p.requester} on {(p.timestamp ?? "").slice(0, 10)}</span>
      </div>
      <Markdown text={p.paper ?? ""} />
      <details>
        <summary>Change set · {summary.change_count} row(s)</summary>
        <DataTable rows={p.change_set} scroll caption="Change set" />
      </details>
      <label className="field">Comment (required to send the plan back)
        <input value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
      {p.requires_override && (
        <div className="row muted small"><Icon name="gpp_maybe" size={18} />Rule-based verdict {p.verdict} – approving needs an explicit override with a reason.</div>
      )}
      <div className="grid-2">
        <button className="primary" title="Opens a confirmation before the plan is written." onClick={() => setConfirming(true)}>
          <Icon name="verified" />Approve the plan …
        </button>
        <button disabled={!comment.trim()} title={comment.trim() ? undefined : "Enter a comment first – sending back needs a reason."}
          onClick={() => void decide(false)}><Icon name="reply" />Send back</button>
      </div>
      {confirming && (
        <Dialog title="Confirm plan approval" onClose={() => setConfirming(false)}>
          <div className="callout tone-warning">
            Approve <strong>{p.request}</strong> and write <strong>{summary.change_count} plan row(s)</strong> to the portfolio data source now? A snapshot is taken first; the System Admin can restore it.
          </div>
          {p.requires_override && (
            <>
              <div className="callout tone-danger" role="alert">
                The rule-based verdict is <strong>{p.verdict}</strong>. Approving means overriding it – the override and your reason are recorded in the audit trail.
              </div>
              <label className="checkbox"><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />Override the {p.verdict} verdict</label>
              <label className="field">Reason for the override (required)
                <textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
              </label>
            </>
          )}
          <div className="grid-2">
            <button className="primary" disabled={!ready} onClick={() => void decide(true)}><Icon name="verified" />Confirm: approve the plan</button>
            <button onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

function PlanApproval({ notify }: { notify: (n: Note) => void }) {
  const board = useLoad(() => api.get<Board>("/planning/board"), []);
  const plans = useLoad(() => api.get<Plans>("/planning/plans", { status: "submitted" }), []);
  if (!board.data || !plans.data) return <Loading error={board.error ?? plans.error}>{null}</Loading>;
  const s = board.data;
  const plan = s.plan_in_force;
  const pending = plans.data.plans;
  const reload = () => { board.reload(); plans.reload(); };
  return (
    <div className="stack">
      <div className="grid">
        <Kpi label="Waiting for your decision" value={String(s.waiting)} />
        <Kpi label="Plan in force" value={plan ? plan.scenario_id : "none"}
          caption={plan ? `“${plan.scenario_name}” ${plan.year_from}–${plan.year_to}` : "No annual plan approved yet"} />
        <section className="card">
          <div className="muted">Base plan today</div>
          <div style={{ margin: "8px 0" }}><VerdictBadge label={s.base_plan_verdict} /></div>
          <div className="muted small">Rank order on today's data</div>
        </section>
        <Kpi label="Demands" value={String(s.demands)} caption={`${s.in_base_plan} in the base plan`} />
      </div>
      {pending.length === 0 && (
        <Callout icon="inbox">
          No annual plan waits for your decision. The PMO submits one from its saved plan scenarios. Meanwhile: <strong>Analysis</strong> shows what drops out and why, <strong>Reports</strong> has the board pack (Excel, print → PDF).
        </Callout>
      )}
      {pending.map((p) => <PlanCard key={p.id} summary={p} notify={notify} done={reload} />)}
      {plan && <Caption>Approved plan in force: {plan.scenario_id} “{plan.scenario_name}” ({plan.year_from}–{plan.year_to}), approved by {plan.approved_by || "–"}.</Caption>}
    </div>
  );
}

// ------------------------------------------------------------------------- page
export function Planning() {
  const pageTitle = usePageTitle("planning", "Planning");
  const { data: ctx, error } = useLoad(() => api.get<Context>("/planning"), []);
  const [tab] = useQueryParam("tab");
  const setQuery = useSetQuery();
  const [note, setNote] = useState<Note>(null);
  const [calculated, setCalculated] = useState<Calculated[]>([]); // up to three scenarios of this visit (client state)
  const [visits, setVisits] = useState(0); // sections that show writes elsewhere are drawn fresh on each visit
  const notify = useCallback((n: Note) => setNote(n), []);
  const [prefilled] = useState(takePrefill); // a question box handover opens the scenarios with its inputs
  const toScenarios = prefilled && !!ctx?.sections.includes("scenarios");
  useEffect(() => {
    if (toScenarios) setQuery({ tab: "scenarios", demand: null });
  }, [toScenarios]);
  const location = useLocation();
  const [prefills, setPrefills] = useState(0);
  useEffect(() => { // a hand-over from a question box on this page itself: the page stays, the form takes the inputs
    if (takePrefill()) setPrefills((n) => n + 1);
  }, [location.key]);
  if (!ctx) return <Loading error={error}>{null}</Loading>;
  const sections = ctx.sections;
  const active = toScenarios && !tab ? "scenarios" : tab && sections.includes(tab) ? tab : sections[0];
  const open = (id: string) => {
    setQuery(id === "demands" ? { tab: id } : { tab: id, demand: null });
    setVisits((v) => v + 1);
  };
  return (
    <>
      <PageHeader title={pageTitle} subtitle={`Annual and multi-year planning ${ctx.first_year}–${ctx.last_year} · ${ctx.demands} demand(s) within your permissions · ranks and verdicts are rule-based`} />
      <Notice note={note} onClose={() => setNote(null)} />
      <Tabs label="Planning sections" active={active} onChange={open} tabs={sections.map((s) => ({ id: s, title: TITLES[s] ?? s }))} />
      <div role="tabpanel" aria-labelledby={`tab-Planning sections-${active}`}>
        {active === "plan_approval" && <PlanApproval key={visits} notify={notify} />}
        {active === "demands" && <DemandsSection />}
        {active === "analysis" && <AnalysisSection />}
        {active === "scenarios" && <Workshop key={prefills} calculated={calculated} setCalculated={setCalculated} notify={notify} />}
        {active === "saved_scenarios" && <SavedScenarios key={visits} notify={notify} />}
        {active === "reports" && <Reports key={visits} calculated={calculated} notify={notify} />}
        {active === "pmo_tools" && <PmoTools notify={notify} />}
      </div>
      <Caption>Reference date {ctx.reference_date}.</Caption>
    </>
  );
}
