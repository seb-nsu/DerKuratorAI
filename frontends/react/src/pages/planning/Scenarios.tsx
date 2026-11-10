// Planning sections around plan scenarios: the PMO's scenario workshop (inputs, up to three calculated scenarios side
// by side, save as draft), the saved plan scenarios with the submission to the Portfolio Board, and the reports with
// the board pack. The calculated scenarios are component state of the page (this visit); every figure comes from the API.
import { useEffect, useState } from "react";
import { api, type Answer } from "../../api/client";
import { AskBox, PLANNING_PREFILL_KEY } from "../../components/AskBox";
import { PlotlyFigure } from "../../components/PlotlyFigure";
import { Icon } from "../../components/ui";
import {
  Callout, Caption, DataTable, DecisionBadges, ScenarioVerdict, Tabs, download, errorText, type Note, type Schema,
  Loading, useLoad,
} from "./parts";

type Options = Answer<"/api/v1/planning/scenario-options">;
type Body = Schema<"ScenarioBody">;
type Result = Schema<"ScenarioView">;
type ComparisonView = Answer<"/api/v1/planning/scenarios/compare", "post">;
type Saved = Answer<"/api/v1/planning/scenarios">;
type ReportOptions = Answer<"/api/v1/planning/reports">;
type Report = Schema<"MultiYearReport">;
type Message = { message: string; proposal?: unknown };

export type Calculated = { name: string; body: Body; result: Result; comparison: ComparisonView["comparison"] };
type Props = { calculated: Calculated[]; setCalculated: (c: Calculated[]) => void; notify: (n: Note) => void };

const BASE_PLAN = "Base plan (today's data)";
const scopeLabel = (o: string) => (o ? o.replace(/_/g, " ").replace(":", ": ") : "Whole portfolio");

function Picker({ label, labels, options, value, onChange }: {
  label: string; labels: Record<string, string>; options: string[]; value: string[]; onChange: (v: string[]) => void;
}) {
  return (
    <label className="field">{label}
      <select multiple className="multi" value={value} onChange={(e) => onChange(Array.from(e.target.selectedOptions, (o) => o.value))}>
        {options.map((d) => <option key={d} value={d}>{labels[d]}</option>)}
      </select>
    </label>
  );
}

function Inputs({ options, form, setForm }: { options: Options; form: Body; setForm: (f: Body) => void }) {
  const labels = Object.fromEntries(options.candidates.map((c) => [c.demand, c.label]));
  const all = options.candidates.map((c) => c.demand);
  const set = (patch: Partial<Body>) => {
    const next = { ...form, ...patch };
    // a demand is forced in, excluded or moved – the lists offer only what is still possible
    const forced = next.forced_in ?? [];
    next.excluded = (next.excluded ?? []).filter((d) => !forced.includes(d));
    next.later = (next.later ?? []).filter((d) => !next.excluded!.includes(d));
    next.earlier = (next.earlier ?? []).filter((d) => !next.excluded!.includes(d) && !next.later!.includes(d));
    setForm(next);
  };
  const factor = options.budget_factor_pct;
  const pct = Number(form.budget_factor_pct ?? 0);
  return (
    <div className="stack">
      <div className="grid">
        <label className="field">Scenario name<input value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} /></label>
        <label className="field">From year
          <input type="number" min={options.year_from.min} max={options.year_from.max} step={1} value={form.year_from ?? ""}
            onChange={(e) => set({ year_from: Number(e.target.value) })} />
        </label>
        <label className="field">To year
          <input type="number" min={options.year_to.min} max={options.year_to.max} step={1} value={form.year_to ?? ""}
            onChange={(e) => set({ year_to: Number(e.target.value) })} />
        </label>
        <label className="field" title="Every envelope larger or smaller by this share (e.g. a budget cut).">
          Budget envelopes: {pct > 0 ? "+" : pct < 0 ? "−" : "+"}{Math.abs(pct)} %
          <input type="range" min={factor.min} max={factor.max} step={factor.step} value={pct}
            onChange={(e) => set({ budget_factor_pct: Number(e.target.value) })} />
        </label>
      </div>
      <div className="grid-2">
        <label className="field">Scope
          <select value={form.scope ?? ""} onChange={(e) => set({ scope: e.target.value })}>
            {options.scopes.map((o) => <option key={o} value={o}>{scopeLabel(o)}</option>)}
          </select>
        </label>
        <fieldset className="field" style={{ border: "none", padding: 0, margin: 0 }}
          title="Rank order: one demand after the other, as long as it fits. Value-optimal: the combination of demands with the highest score sum within the same limits (mandatory demands first).">
          <legend>Filling</legend>
          <div className="row wrap" style={{ color: "var(--dk-text)" }}>
            {Object.entries(options.methods).map(([id, title]) => (
              <label key={id} className="checkbox">
                <input type="radio" name="method" value={id} checked={form.method === id} onChange={() => set({ method: id })} />{title}
              </label>
            ))}
          </div>
        </fieldset>
      </div>
      <details className="card" style={{ height: "auto" }}>
        <summary><Icon name="tune" />PMO decisions: force in, exclude, move</summary>
        <div className="grid-2">
          <Picker label="Force in (with a reason)" labels={labels} options={all} value={form.forced_in ?? []} onChange={(v) => set({ forced_in: v })} />
          <label className="field">Reason for forcing in
            <input value={form.forced_reason ?? ""} placeholder="e.g. board commitment, regulatory deadline" onChange={(e) => set({ forced_reason: e.target.value })} />
          </label>
          <Picker label="Exclude" labels={labels} options={all.filter((d) => !(form.forced_in ?? []).includes(d))} value={form.excluded ?? []} onChange={(v) => set({ excluded: v })} />
          <Picker label="Move one year later" labels={labels} options={all.filter((d) => !(form.excluded ?? []).includes(d))} value={form.later ?? []} onChange={(v) => set({ later: v })} />
          <Picker label="Move one year earlier" labels={labels}
            options={all.filter((d) => !(form.excluded ?? []).includes(d) && !(form.later ?? []).includes(d))} value={form.earlier ?? []} onChange={(v) => set({ earlier: v })} />
        </div>
      </details>
    </div>
  );
}

function ResultTabs({ result }: { result: Result }) {
  const [tab, setTab] = useState("demands");
  useEffect(() => setTab("demands"), [result]);
  const tabs = [{ id: "demands", title: "Demands" }, { id: "budget", title: "Budget" }, { id: "capacity", title: "Capacity" },
    ...(result.optimizer ? [{ id: "optimizer", title: "Optimizer vs. rank order" }] : [])];
  const optimizer = result.optimizer;
  return (
    <>
      <Tabs label="Scenario views" tabs={tabs} active={tab} onChange={setTab} />
      <div role="tabpanel" className="stack">
        {tab === "demands" && (result.items.length === 0 ? <Caption>No demand in this scenario.</Caption> : (
          <>
            <DecisionBadges counts={result.decision_counts as unknown as Record<string, number>} />
            <DataTable rows={result.items} formats={{ Score: "dec1" }} scroll caption="Decision per demand" />
          </>
        ))}
        {tab === "budget" && (result.budget.length === 0 ? <Callout>No budget envelopes for these years (sheet Budget_Envelopes).</Callout> : (
          <>
            <DataTable rows={result.budget} scroll caption="Budget envelopes" />
            <Caption>Limit = cap minus the reserve. Committed = running projects (cost plans, rest of the forecast to complete) and approved demands; Planned = the demands of this scenario incl. run cost after go-live.</Caption>
          </>
        ))}
        {tab === "capacity" && (!result.capacity_chart ? <Callout>No capacity limits for these years (sheet Capacity_Limits).</Callout> : (
          <>
            <PlotlyFigure figure={result.capacity_chart} />
            <Caption>Share of the usable FTE (available × maximum utilisation) per team and quarter, running projects included. Darker = fuller; above 100 % the team is overbooked.</Caption>
          </>
        ))}
        {tab === "optimizer" && optimizer && (
          <>
            <DataTable rows={optimizer.figures} caption="Rank order and value-optimal" />
            <ul className="list">{optimizer.summary.map((l, i) => <li key={i}>{l}</li>)}</ul>
            {optimizer.changes.length > 0 && <DataTable rows={optimizer.changes} caption="What the value-optimal filling changes" />}
            {!result.solved && <Callout tone="warning" icon="warning">The optimizer found no solution in time – the rank order was used.</Callout>}
            <Caption>Same limits, same bound money and capacity, same verdict rules – only the choice of the free demands differs. The reason of a deferral or an Out names the limit it would break.</Caption>
          </>
        )}
      </div>
    </>
  );
}

let keptForm: Partial<Body> | null = null; // the inputs survive a switch to another section during this visit

/** A planning question of the question box hands its scenario inputs over once (sessionStorage): they become the
 * form. True when there was one. */
export function takePrefill(): boolean {
  let raw: string | null = null;
  try {
    raw = sessionStorage.getItem(PLANNING_PREFILL_KEY);
    sessionStorage.removeItem(PLANNING_PREFILL_KEY);
  } catch {
    return false;
  }
  if (!raw) return false;
  try {
    const given = JSON.parse(raw) as Record<string, unknown>;
    keptForm = { ...(keptForm ?? {}), ...Object.fromEntries(Object.entries(given).filter(([, v]) => v !== null && v !== undefined)) };
    return true;
  } catch {
    return false;
  }
}

export function Workshop({ calculated, setCalculated, notify }: Props) {
  const { data: options, error } = useLoad(() => api.get<Options>("/planning/scenario-options"), []);
  const [form, setFormState] = useState<Partial<Body> | null>(keptForm);
  const [show, setShow] = useState<string | null>(calculated.length ? calculated[calculated.length - 1].name : null);
  const [busy, setBusy] = useState(false);
  if (!options) return <Loading error={error}>{null}</Loading>;
  const known = new Set(options.candidates.map((c) => c.demand));
  const merged: Body = {
    name: "Base plan", year_from: options.year_from.default, year_to: options.year_to.default, budget_factor_pct: 0, scope: "",
    method: "rank", forced_in: [], forced_reason: "", excluded: [], later: [], earlier: [], ...(form ?? {}),
  };
  // choices the inputs no longer offer fall back to the default
  const current: Body = {
    ...merged,
    scope: options.scopes.includes(merged.scope ?? "") ? merged.scope : "",
    method: merged.method && merged.method in options.methods ? merged.method : "rank",
    forced_in: (merged.forced_in ?? []).filter((d) => known.has(d)),
    excluded: (merged.excluded ?? []).filter((d) => known.has(d)),
    later: (merged.later ?? []).filter((d) => known.has(d)),
    earlier: (merged.earlier ?? []).filter((d) => known.has(d)),
  };
  const setForm = (f: Body) => { keptForm = f; setFormState(f); };
  const calculate = async () => {
    setBusy(true);
    try {
      const body = { ...current };
      const result = await api.post<Result>("/planning/scenarios/calculate", body);
      const next = [...calculated.filter((c) => c.name !== result.name), { name: result.name, body, result, comparison: [] }]
        .slice(-options.max_compared);
      const comparison = next.length > 1
        ? (await api.post<ComparisonView>("/planning/scenarios/compare", { scenarios: next.map((c) => c.body) })).comparison : [];
      next[next.length - 1] = { ...next[next.length - 1], comparison };
      setCalculated(next);
      setShow(result.name);
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    } finally {
      setBusy(false);
    }
  };
  const save = async (entry: Calculated) => {
    try {
      notify({ kind: "success", message: (await api.post<Message>("/planning/scenarios", entry.body)).message });
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  const shown = calculated.find((c) => c.name === show) ?? calculated[calculated.length - 1];
  const comparison = calculated.length > 1 ? calculated[calculated.length - 1].comparison : [];
  return (
    <div className="stack">
      <Inputs options={options} form={current} setForm={setForm} />
      <div><button className="primary" disabled={busy} aria-busy={busy} onClick={() => void calculate()}><Icon name="calculate" />Calculate plan scenario</button></div>
      {!shown ? (
        <Caption>Calculate a plan scenario: running projects bind their money and capacity first, then the demands fill the envelopes in rank order – or value-optimal, as a second proposal. Up to three scenarios stay side by side for comparison.</Caption>
      ) : (
        <>
          {comparison.length > 0 && (
            <>
              <h3 style={{ margin: 0 }}>Comparison</h3>
              <DataTable rows={comparison} formats={{ Verdict: "verdict" }} caption="Comparison of the calculated scenarios" />
            </>
          )}
          {calculated.length > 1 && (
            <fieldset className="field" style={{ border: "none", padding: 0, margin: 0 }}>
              <legend>Show scenario</legend>
              <div className="row wrap" style={{ color: "var(--dk-text)" }}>
                {calculated.map((c) => (
                  <label key={c.name} className="checkbox">
                    <input type="radio" name="show" checked={c.name === shown.name} onChange={() => setShow(c.name)} />{c.name}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
          <ScenarioVerdict header={shown.result} />
          <div>
            <button onClick={() => void save(shown)}
              title="Stores the input and the decision per demand; the approval follows separately (Saved plan scenarios › Submit to the Portfolio Board).">
              <Icon name="save" />Save as draft
            </button>
          </div>
          <ResultTabs result={shown.result} />
          <AskBox page="scenario" scenarios={calculated.map((c) => c.body)} scenario={shown.name} title="Ask about this plan scenario" />
        </>
      )}
    </div>
  );
}

const COLUMNS = ["scenario_id", "scenario_name", "year_from", "year_to", "scenario_status", "verdict", "created_by", "created_on"];

export function SavedScenarios({ notify }: { notify: (n: Note) => void }) {
  const { data, error, reload } = useLoad(() => api.get<Saved>("/planning/scenarios"), []);
  const [pick, setPick] = useState("");
  if (!data) return <Loading error={error}>{null}</Loading>;
  if (!data.scenarios.length) {
    return <Caption>No saved plan scenario yet. Calculate one under <strong>Plan scenarios</strong> and save it as a draft; it then appears here for the submission to the Portfolio Board.</Caption>;
  }
  const drafts = data.scenarios.filter((s) => s.submittable);
  const chosen = drafts.some((s) => s.scenario_id === pick) ? pick : drafts[0]?.scenario_id ?? "";
  const submit = async () => {
    try {
      notify({ kind: "success", message: (await api.post<Message>(`/planning/scenarios/${chosen}/submit`)).message });
      reload();
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  return (
    <div className="stack">
      <DataTable rows={data.scenarios} columns={COLUMNS} formats={{ verdict: "verdict" }} caption="Saved plan scenarios"
        labels={Object.fromEntries(COLUMNS.map((c) => [c, c.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase())]))} />
      {drafts.length > 0 && (
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <label className="field" style={{ flex: "1 1 240px" }}>Draft
            <select value={chosen} onChange={(e) => setPick(e.target.value)}>
              {drafts.map((s) => <option key={s.scenario_id} value={s.scenario_id}>{`${s.scenario_id} · ${s.scenario_name}`}</option>)}
            </select>
          </label>
          <button title="Recalculated on today's data; the Portfolio Board approves it (four-eyes)." onClick={() => void submit()}>
            <Icon name="send" />Submit to the Portfolio Board
          </button>
        </div>
      )}
    </div>
  );
}

export function Reports({ calculated, notify }: { calculated: Calculated[]; notify: (n: Note) => void }) {
  const { data, error } = useLoad(() => api.get<ReportOptions>("/planning/reports"), []);
  const [pick, setPick] = useState<string | null>(null);
  const own = Object.fromEntries(calculated.map((c) => [`Calculated: ${c.name}`, c.body]));
  const listed = data?.options ?? [];
  const choices = [...listed.filter((o) => o !== BASE_PLAN), ...Object.keys(own).filter((o) => !listed.includes(o)),
    ...(listed.includes(BASE_PLAN) ? [BASE_PLAN] : [])];
  const option = pick && choices.includes(pick) ? pick : choices[0];
  const request = (format: string) => ({
    ...(option in own && !listed.includes(option) ? { scenario: own[option] } : { option }),
    ...(format === "xlsx" && calculated.length ? { compared: calculated.map((c) => c.body) } : {}),
    format,
  });
  const report = useLoad(() => (option ? api.post<Report>("/planning/reports", request("json")) : Promise.resolve(null)), [option]);
  if (!data) return <Loading error={error}>{null}</Loading>;
  const get = async (format: string) => {
    try {
      await download("/planning/reports", request(format), `board_pack.${format}`);
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  const rows = report.data?.multi_year ?? [];
  return (
    <div className="stack">
      <label className="field">Plan scenario
        <select value={option} onChange={(e) => setPick(e.target.value)}>{choices.map((c) => <option key={c}>{c}</option>)}</select>
      </label>
      <div className="grid-2">
        <button onClick={() => void get("xlsx")}><Icon name="table_view" />Board pack (Excel)</button>
        <button onClick={() => void get("html")}><Icon name="print" />Board pack (print → PDF)</button>
      </div>
      <Caption>Excel: summary, ranking, decision per demand, budget, capacity, multi-year view, the comparison of the calculated scenarios and, for a value-optimal plan, the optimizer sheet. The HTML page prints to PDF in any browser.</Caption>
      <Loading error={report.error}>{report.data && rows.length > 0 && (
        <DataTable rows={rows} caption="Multi-year view"
          formats={Object.fromEntries(Object.keys(rows[0]).filter((c) => c !== "Business unit").map((c) => [c, "eur" as const]))} />
      )}</Loading>
    </div>
  );
}
