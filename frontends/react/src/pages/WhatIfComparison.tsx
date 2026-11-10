// What-if comparison: A/B comparison of saved what-ifs, the budget impact side by side, the differences and the list
// with deleting (owner or PMO). A and B are in the address (?a=…&b=…), so a comparison can be linked.
import { useCallback, useEffect, useState } from "react";
import { api, type Answer } from "../api/client";
import { AskBox } from "../components/AskBox";
import { PlotlyFigure } from "../components/PlotlyFigure";
import { ErrorBox, Icon, PageHeader } from "../components/ui";
import { PageLink } from "../components/Shell";
import {
  Callout, Caption, DataTable, Dialog, Notice, Tabs, VerdictBadge, errorText, eur, num, useQueryParam, type Note,
} from "./planning/parts";

type Listing = Answer<"/api/v1/whatifs">;
type Summary = Listing["whatifs"][number];
type Comparison = Answer<"/api/v1/whatifs/compare">;
type Detail = Comparison["a"];

const SUMMARY: [string, keyof Summary["figures"]][] = [
  ["Verdict", "verdict"], ["Net budget impact", "net_budget_impact"], ["Budget released", "budget_released"],
  ["Budget added", "budget_added"], ["Projects stopped", "projects_stopped"], ["Projects on hold", "projects_on_hold"],
  ["FTE moved", "fte_moved"], ["Changes", "changes"], ["Steps", "steps"],
];

const stamp = (v?: string | null) => (v ? v.replace("T", " ").slice(0, 16) : "–");
const label = (s: Summary) => `${s.name} · ${s.owner} · ${stamp(s.timestamp)}`;

function metric(key: keyof Summary["figures"], f: Summary["figures"]): string {
  const v = f[key];
  if (key === "net_budget_impact") return eur(v, true);
  if (key === "budget_released" || key === "budget_added") return eur(v);
  if (key === "verdict") return String(v ?? "–");
  return num(v);
}

function Side({ tag, s }: { tag: string; s: Detail }) {
  return (
    <section className="card stack">
      <strong>{tag}: {s.name}</strong>
      <div><VerdictBadge label={s.verdict ?? "–"} /></div>
      {s.waterfall_figure ? <PlotlyFigure figure={s.waterfall_figure} /> : <Caption>No budget effect (e.g. a schedule or capacity change).</Caption>}
      {s.conditions.map((c, i) => <Caption key={i}>Condition: {c}</Caption>)}
      {s.turns.length > 0 && <Caption>Steps: {s.turns.join(" → ")}</Caption>}
    </section>
  );
}

function SavedList({ saved, onDeleted, notify }: { saved: Summary[]; onDeleted: () => void; notify: (n: Note) => void }) {
  const own = saved.filter((s) => s.can_delete);
  const [pick, setPick] = useState("");
  const [asking, setAsking] = useState(false);
  const remove = async () => {
    setAsking(false);
    try {
      await api.delete(`/whatifs/${pick}`);
      notify({ kind: "success", message: "Saved what-if deleted." });
      setPick("");
      onDeleted();
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  const chosen = own.find((s) => s.id === pick);
  return (
    <div className="stack">
      <DataTable caption="Saved what-ifs" formats={{ Verdict: "verdict" }} rows={saved.map((s) => ({
        "What-if": s.name, Owner: s.owner, Saved: stamp(s.timestamp), Verdict: s.verdict, Changes: s.figures.changes, Steps: s.figures.steps,
      }))} />
      {own.length > 0 && (
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <label className="field" style={{ flex: "1 1 240px" }}>Delete a saved what-if
            <select value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">—</option>
              {own.map((s) => <option key={s.id} value={s.id}>{label(s)}</option>)}
            </select>
          </label>
          <button disabled={!pick} onClick={() => setAsking(true)}><Icon name="delete" />Delete scenario</button>
        </div>
      )}
      {asking && chosen && (
        <Dialog title="Delete saved what-if" onClose={() => setAsking(false)}>
          <div className="callout tone-warning">Delete the saved what-if <strong>{label(chosen)}</strong>? This cannot be undone.</div>
          <div className="grid-2">
            <button className="primary" onClick={() => void remove()}>Yes, delete</button>
            <button onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

export function WhatIfComparison() {
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [qa, setA] = useQueryParam("a");
  const [qb, setB] = useQueryParam("b");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [tab, setTab] = useState("bars");

  const load = useCallback(() => {
    api.get<Listing>("/whatifs").then(setListing, (e) => setError(errorText(e)));
  }, []);
  useEffect(load, [load]);

  const saved = listing?.whatifs ?? [];
  const ids = saved.map((s) => s.id);
  const pair = listing?.default_pair ?? (ids.length > 1 ? { a: ids[1], b: ids[0] } : null);
  const a = qa && ids.includes(qa) ? qa : pair?.a;
  const b = qb && ids.includes(qb) ? qb : pair?.b;

  useEffect(() => {
    setComparison(null);
    setCompareError(null);
    if (!a || !b || a === b) return;
    api.get<Comparison>("/whatifs/compare", { a, b }).then(setComparison, (e) => setCompareError(errorText(e)));
  }, [a, b, listing]);

  if (error) return <ErrorBox message={error} />;
  if (!listing) return <div className="muted" aria-busy="true">Loading …</div>;
  const header = <PageHeader title="What-if comparison" subtitle={`A/B comparison · ${saved.length} saved what-if(s) in your scope`}
    action={<PageLink to="console" icon="arrow_back" text="Back to the Steering Console" />} />;
  const savedList = <SavedList saved={saved} onDeleted={load} notify={setNote} />;
  const notice = <Notice note={note} onClose={() => setNote(null)} />;
  if (!saved.length) {
    return <>{header}{notice}<Callout>No saved what-ifs yet. Save a report in the Steering Console with 'Save as what-if'.</Callout></>;
  }
  if (saved.length < 2) {
    return (
      <>{header}{notice}
        <Caption>1 saved what-if ('{saved[0].name}'). Save a second one to compare them side by side.</Caption>
        {savedList}
      </>
    );
  }
  const ca = comparison?.a;
  const cb = comparison?.b;
  return (
    <>
      {header}
      {notice}
      <div className="grid-2">
        <label className="field">Scenario A (older)
          <select value={a} onChange={(e) => setA(e.target.value)}>{saved.map((s) => <option key={s.id} value={s.id}>{label(s)}</option>)}</select>
        </label>
        <label className="field">Scenario B (newer)
          <select value={b} onChange={(e) => setB(e.target.value)}>{saved.map((s) => <option key={s.id} value={s.id}>{label(s)}</option>)}</select>
        </label>
      </div>
      {a === b ? (
        <><Callout>Choose two different what-ifs.</Callout>{savedList}</>
      ) : compareError ? (
        <ErrorBox message={compareError} />
      ) : !comparison || !ca || !cb ? (
        <div className="muted" aria-busy="true">Loading …</div>
      ) : (
        <>
          <DataTable caption="Key figures A and B" rows={SUMMARY.map(([title, key]) => ({
            Metric: title, [`A – ${ca.name}`]: metric(key, ca.figures), [`B – ${cb.name}`]: metric(key, cb.figures),
          }))} />
          <div className="grid-2"><Side tag="A" s={ca} /><Side tag="B" s={cb} /></div>
          <Tabs label="Comparison views" active={tab} onChange={setTab} tabs={[
            { id: "bars", title: "Budget impact A vs B" }, { id: "diff", title: "Change-by-change differences" }, { id: "saved", title: "Saved what-ifs" },
          ]} />
          <div role="tabpanel" aria-labelledby={`tab-Comparison views-${tab}`}>
            {tab === "bars" && (comparison.budget_impact_figure
              ? <PlotlyFigure figure={comparison.budget_impact_figure} />
              : <Caption>Neither what-if changes a budget.</Caption>)}
            {tab === "diff" && (
              <DataTable caption="Change-by-change differences" rows={comparison.differences.map((d) => ({
                Project: d.project, Change: d.field_label, [`A – ${ca.name}`]: d.A, [`B – ${cb.name}`]: d.B, Differs: d.differs,
              }))} />
            )}
            {tab === "saved" && savedList}
          </div>
          <AskBox page="compare" a={a} b={b} title="Ask about A vs B" />
        </>
      )}
    </>
  );
}
