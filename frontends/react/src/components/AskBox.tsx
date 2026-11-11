// The page assistant: a question box under a page ("Ask about …", "Portfolio briefing and questions").
//
//   <AskBox page="overview" />
//
// page        the box's page in the API (POST /assist/ask): "overview", "data_quality", "report", "project", "compare",
//             "plan", "demands", "demand" or "scenario". Title and example questions come from GET /assist/pages; a
//             page the role has no box for renders nothing.
// runId, proposalId   page "report": the what-if or the proposal the report shows.
// projectId   page "project": the project (a change question naming no project is about it).
// a, b        page "compare": the two saved what-ifs.
// demandId    page "demand": the demand.
// filters     page "demands": the table's filters { business_unit, status, year }.
// scenarios, scenario   page "scenario": the calculated scenario inputs (client state) and the name of the one shown.
// Ids name the box's history; filters and scenario inputs do not.
// title       instead of the API's title; expanded: open from the start; placeholder: the input's placeholder.
//
// A change or planning question comes back calculated by the rules (verdict, changes, rule-based paper) with its next
// step: "Continue in the Steering Console" stores the request in sessionStorage under CONSOLE_REQUEST_KEY and opens
// /console; "Open in Planning › Plan scenarios" stores the scenario inputs (JSON) under PLANNING_PREFILL_KEY and opens
// /planning?tab=scenarios. Those pages read and remove the key. Any other question is answered from the facts the
// server builds for the caller's scope. The last three answers per box stay for the browser tab's session.
import { useEffect, useId, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type Answer } from "../api/client";
import type { components } from "../api/schema";
import { tone } from "../theme";
import { Markdown } from "./Markdown";
import { ErrorBox, Icon } from "./ui";

export const CONSOLE_REQUEST_KEY = "dk.console.request";
export const PLANNING_PREFILL_KEY = "dk.planning.prefill";
const HISTORY = 3; // answers kept per box, as in the NiceGUI box
const MAX_CHARS = 600;

type Pages = Answer<"/api/v1/assist/pages">;
type Calculation = components["schemas"]["AssistCalculation"];
type PlainAnswer = components["schemas"]["AssistAnswer"];
type Result = Calculation | PlainAnswer;
type Question = components["schemas"]["AssistQuestion"];
type AssistPage = Question["page"];

export type AskBoxProps = {
  page: AssistPage;
  runId?: string;
  proposalId?: string;
  demandId?: string;
  projectId?: string;
  a?: string;
  b?: string;
  filters?: Pick<Question, "business_unit" | "status" | "year">;
  scenarios?: Question["scenarios"];
  scenario?: string;
  title?: string;
  expanded?: boolean;
  placeholder?: string;
};

const VERDICT_ICONS = { success: "check_circle", warning: "warning", danger: "block", neutral: "help", info: "info" } as const;
const CHANGE_COLUMNS: [keyof Calculation["change_rows"][number], string][] = [
  ["project", "Project"], ["change", "Change"], ["current", "Current"], ["proposed", "Proposed"], ["delta", "Δ"],
  ["approval", "Approval"], ["note", "Note"],
];

// facts for the screen: section titles as bold lines instead of large headings
const readable = (facts: string) =>
  (facts ?? "").split("\n").map((l) => (l.startsWith("## ") ? `**${l.slice(3).trim()}**` : l)).join("\n");

function load(key: string): Result[] {
  try {
    return JSON.parse(sessionStorage.getItem(key) ?? "[]") as Result[];
  } catch {
    return [];
  }
}

function store(key: string, value: unknown) {
  try {
    sessionStorage.setItem(key, typeof value === "string" ? value : JSON.stringify(value));
  } catch {
    /* private mode: the hand-over or history lasts for this page only */
  }
}

function useHandover() {
  const navigate = useNavigate();
  return {
    console: (request: string) => {
      store(CONSOLE_REQUEST_KEY, request);
      navigate("/console");
    },
    planning: (inputs: unknown) => {
      store(PLANNING_PREFILL_KEY, inputs);
      navigate("/planning?tab=scenarios");
    },
  };
}

function CalculationView({ result }: { result: Calculation }) {
  const go = useHandover();
  const verdict = result.verdict;
  const step = result.next_step;
  const t = tone("verdict", verdict?.label);
  return (
    <>
      {verdict == null ? (
        <Markdown text={result.paper} />
      ) : (
        <>
          <div className={`callout tone-${t}`} role="status">
            <div className="row" style={{ alignItems: "flex-start" }}>
              <Icon name={VERDICT_ICONS[t]} size={22} />
              <span style={{ color: "var(--dk-text)" }}>
                <strong>Recommendation: {verdict.label}</strong>
                {verdict.lead_reason ? ` – ${verdict.lead_reason}` : ""}
              </span>
            </div>
          </div>
          <div className="row muted small"><Icon name="gavel" size={16} />Calculated by the rules – not by the AI</div>
          {(verdict.reasons.slice(2, 4).length > 0 || verdict.conditions.length > 0) && (
            <ul>
              {verdict.reasons.slice(2, 4).map((r, i) => <li key={`r${i}`}>{r}</li>)}
              {verdict.conditions.slice(0, 3).map((c, i) => <li key={`c${i}`}>Condition: {c}</li>)}
            </ul>
          )}
          {result.change_rows.length > 0 && (
            <>
              <div style={{ fontWeight: 600 }}>Scenario Analysis (quantitative)</div>
              <div className="table-wrap">
                <table>
                  <thead><tr>{CHANGE_COLUMNS.map(([k, label]) => <th key={k}>{label}</th>)}</tr></thead>
                  <tbody>
                    {result.change_rows.map((row, i) => (
                      <tr key={i}>{CHANGE_COLUMNS.map(([k]) => <td key={k}>{row[k]}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
          <details className="card expander">
            <summary><Icon name="description" />Rule-based paper</summary>
            <Markdown text={result.paper} />
          </details>
        </>
      )}
      {step && (
        <>
          <div className="muted small">{step.text}</div>
          {step.target === "console" && (
            <button onClick={() => go.console(step.request ?? result.request)}>
              <Icon name="query_stats" />Continue in the Steering Console
            </button>
          )}
          {step.target === "planning" && (
            <button onClick={() => go.planning(step.inputs)}>
              <Icon name="event_note" />Open in Planning › Plan scenarios
            </button>
          )}
        </>
      )}
    </>
  );
}

function AnswerView({ answer }: { answer: PlainAnswer }) {
  const go = useHandover();
  const [facts, setFacts] = useState(false);
  return (
    <>
      <Markdown text={answer.text} />
      <div className="muted small">{answer.note + (answer.truncated ? " · facts shortened" : "")}</div>
      {answer.handoff && <div className="muted small">{answer.handoff.text}</div>}
      <div className="row" style={{ flexWrap: "wrap" }}>
        {answer.handoff && (
          <button onClick={() => go.console(answer.handoff!.request)}>
            <Icon name="query_stats" />Calculate in the Steering Console
          </button>
        )}
        <button className="flat" aria-expanded={facts} onClick={() => setFacts(!facts)}>
          <Icon name="fact_check" />Facts
        </button>
      </div>
      {facts && <div className="card"><Markdown text={readable(answer.facts) || "–"} /></div>}
    </>
  );
}

export function AskBox({ page, runId, proposalId, demandId, projectId, a, b, filters, scenarios, scenario, title, expanded, placeholder }: AskBoxProps) {
  const ids = { run_id: runId, proposal_id: proposalId, demand_id: demandId, project_id: projectId, a, b, scenario };
  const key = `dk.ask:${[page, ...Object.values(ids)].filter(Boolean).join(":")}`;
  const [meta, setMeta] = useState<Pages | null>(null);
  const [history, setHistory] = useState<Result[]>(() => load(key));
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();

  useEffect(() => {
    api.get<Pages>("/assist/pages").then(setMeta, () => setMeta(null));
  }, []);
  useEffect(() => setHistory(load(key)), [key]);

  const box = meta?.pages.find((p) => p.page === page);
  if (!meta || !box) return null;

  const ask = async (text: string) => {
    const asked = text.trim();
    if (!asked || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<Result>("/assist/ask", { page, question: asked, ...ids, ...filters, scenarios });
      const next = [result, ...history].slice(0, HISTORY);
      setHistory(next);
      store(key, next);
      setQuestion("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void ask(question);
  };

  return (
    <details className="card expander ask-box" open={expanded || history.length > 0}>
      <summary><Icon name="forum" />{title ?? box.title}</summary>
      <form className="row" onSubmit={submit} style={{ alignItems: "stretch" }}>
        <label htmlFor={inputId} className="visually-hidden">Question</label>
        <input id={inputId} value={question} maxLength={MAX_CHARS} placeholder={placeholder ?? "Ask a question …"}
          onChange={(e) => setQuestion(e.target.value)} disabled={busy} />
        <button className="primary" type="submit" disabled={busy}><Icon name="send" />Ask</button>
      </form>
      {box.examples.length > 0 && (
        <div className="row" style={{ flexWrap: "wrap", gap: 4 }}>
          {box.examples.map((example) => (
            <button key={example} className="flat link" type="button" disabled={busy} onClick={() => void ask(example)}>
              {example}
            </button>
          ))}
        </div>
      )}
      {busy && <div className="muted row" role="status"><span className="spinner" aria-hidden="true" />Calculating ...</div>}
      {error && <ErrorBox message={error} />}
      {history.length === 0 ? (
        <div className="muted small">{meta.hint}</div>
      ) : (
        history.map((entry, i) => (
          <div key={`${i}-${entry.question}`} className="ask-entry">
            <strong>{entry.question}</strong>
            {entry.kind === "calculation" ? <CalculationView result={entry} /> : <AnswerView answer={entry} />}
          </div>
        ))
      )}
    </details>
  );
}
