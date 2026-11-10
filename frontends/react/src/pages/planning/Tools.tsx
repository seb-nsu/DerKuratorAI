// The PMO's maintenance work on demands in one Planning section: assessment (written directly), scoring weights and
// the conversion of approved demands into projects (both proposals a second PMO approves).
import { useEffect, useState } from "react";
import { api, type Answer } from "../../api/client";
import { Icon } from "../../components/ui";
import { Callout, Caption, DataTable, Loading, errorText, num, useLoad, type Note } from "./parts";

type Criteria = Answer<"/api/v1/planning/criteria">;
type Candidates = Answer<"/api/v1/planning/assessment">;
type Form = Answer<"/api/v1/planning/demands/{demand_id}/assessment">;
type Conversions = Answer<"/api/v1/planning/conversions">;
type Message = { message: string; proposal?: unknown };
type Notify = { notify: (n: Note) => void };

const TOOLS: [string, string][] = [["assessment", "Assessment"], ["weights", "Scoring weights"], ["conversion", "Projects from demands"]];

function AssessmentForm({ demand, notify }: { demand: string } & Notify) {
  const { data, error } = useLoad(() => api.get<Form>(`/planning/demands/${demand}/assessment`), [demand]);
  const [scores, setScores] = useState<Record<string, number>>({});
  const [comments, setComments] = useState<Record<string, string>>({});
  useEffect(() => {
    if (data) setScores(Object.fromEntries(data.criteria.map((c) => [c.criterion, c.value])));
    setComments({});
  }, [data]);
  if (!data) return <Loading error={error}>{null}</Loading>;
  const save = async () => {
    try {
      const body = { scores, comments: Object.fromEntries(Object.entries(comments).filter(([, v]) => v.trim())) };
      notify({ kind: "success", message: (await api.post<Message>(`/planning/demands/${demand}/assessment`, body)).message });
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  return (
    <form className="card stack" style={{ height: "auto" }} onSubmit={(e) => { e.preventDefault(); void save(); }}>
      {data.criteria.map((c) => (
        <div key={c.criterion} className="grid-2" style={{ alignItems: "end" }}>
          <label className="field" title={`Weight ${num(c.weight)}`}>{c.criterion} ({c.direction})
            <input type="number" min={c.scale_min} max={c.scale_max} step={1} value={scores[c.criterion] ?? c.value}
              onChange={(e) => setScores({ ...scores, [c.criterion]: Number(e.target.value) })} />
          </label>
          <label className="field">Comment
            <input placeholder="Comment (optional)" value={comments[c.criterion] ?? ""}
              onChange={(e) => setComments({ ...comments, [c.criterion]: e.target.value })} />
          </label>
        </div>
      ))}
      <div><button className="primary" type="submit"><Icon name="save" />Save assessment</button></div>
    </form>
  );
}

function Assessment({ notify }: Notify) {
  const criteria = useLoad(() => api.get<Criteria>("/planning/criteria"), []);
  const open = useLoad(() => api.get<Candidates>("/planning/assessment"), []);
  const [pick, setPick] = useState("");
  if (!criteria.data || !open.data) return <Loading error={criteria.error ?? open.error}>{null}</Loading>;
  if (!criteria.data.criteria.length) return <Callout>No scoring model in the data (sheet Scoring_Model).</Callout>;
  const demands = open.data.demands;
  if (!demands.length) return <Caption>No demand to assess.</Caption>;
  const chosen = demands.some((d) => d.demand_number === pick) ? pick : demands[0].demand_number;
  return (
    <div className="stack">
      <label className="field">Demand
        <select value={chosen} onChange={(e) => setPick(e.target.value)}>
          {demands.map((d) => <option key={d.demand_number} value={d.demand_number}>{`${d.demand_number} · ${d.demand_name}`}</option>)}
        </select>
      </label>
      <AssessmentForm demand={chosen} notify={notify} />
      <Caption>Each save appends new score rows; the latest assessment per criterion counts (history stays).</Caption>
    </div>
  );
}

function Weights({ notify }: Notify) {
  const { data, error } = useLoad(() => api.get<Criteria>("/planning/criteria"), []);
  const [weights, setWeights] = useState<Record<string, number>>({});
  if (!data) return <Loading error={error}>{null}</Loading>;
  const submit = async () => {
    const body = { weights: Object.fromEntries(data.criteria.map((c) => [c.criterion, weights[c.criterion] ?? c.weight])) };
    try {
      const answer = await api.post<Message>("/planning/weights", body);
      notify({ kind: answer.proposal ? "success" : "info", message: answer.message });
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  return (
    <div className="stack">
      <Caption>A weight change is a proposal: a second PMO approves it, the write journal records it.</Caption>
      {!data.criteria.length ? <Callout>No scoring model in the data (sheet Scoring_Model).</Callout> : (
        <>
          <div className="table-wrap" tabIndex={0} role="region" aria-label="Scoring weights">
            <table className="dk-table">
              <thead><tr>{["Criterion", "Weight", "Lowest score", "Highest score", "Direction"].map((t) => <th key={t} scope="col">{t}</th>)}</tr></thead>
              <tbody>
                {data.criteria.map((c) => (
                  <tr key={c.criterion}>
                    <td>{c.criterion}</td>
                    <td><input type="number" min={0} step={1} style={{ width: 100 }} aria-label={`Weight of ${c.criterion}`}
                      value={weights[c.criterion] ?? c.weight} onChange={(e) => setWeights({ ...weights, [c.criterion]: Number(e.target.value) })} /></td>
                    <td className="num">{num(c.scale_min)}</td>
                    <td className="num">{num(c.scale_max)}</td>
                    <td>{c.direction}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div><button onClick={() => void submit()}><Icon name="send" />Submit weight change</button></div>
        </>
      )}
    </div>
  );
}

function Conversion({ notify }: Notify) {
  const { data, error } = useLoad(() => api.get<Conversions>("/planning/conversions"), []);
  const [pick, setPick] = useState("");
  if (!data) return <Loading error={error}>{null}</Loading>;
  const chosen = data.approved.some((d) => d.demand_number === pick) ? pick : data.approved[0]?.demand_number ?? "";
  const convert = async () => {
    try {
      notify({ kind: "success", message: (await api.post<Message>("/planning/conversions", { demand: chosen })).message });
    } catch (e) {
      notify({ kind: "error", message: errorText(e) });
    }
  };
  return (
    <div className="stack">
      {data.converted.length > 0 && (
        <DataTable caption="Converted demands" rows={data.converted.map((c) => ({ Demand: c.Demand, Name: c.Name, Project: c.Project }))} />
      )}
      {!data.approved.length ? (
        <Caption>No approved demand waits for its conversion into a project. Demands become convertible once the Portfolio Board approves a plan that contains them.</Caption>
      ) : (
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <label className="field" style={{ flex: "1 1 240px" }}>Approved demand
            <select value={chosen} onChange={(e) => setPick(e.target.value)}>
              {data.approved.map((d) => <option key={d.demand_number} value={d.demand_number}>{`${d.demand_number} · ${d.demand_name}`}</option>)}
            </select>
          </label>
          <button title="Master data 1:1, quarterly money → cost plans, capacity → resource plans, first baseline. A second PMO approves."
            onClick={() => void convert()}><Icon name="rocket_launch" />Convert into a project</button>
        </div>
      )}
    </div>
  );
}

export function PmoTools({ notify }: Notify) {
  const [tool, setTool] = useState("assessment");
  return (
    <div className="stack">
      <div className="segmented" role="group" aria-label="PMO tool">
        {TOOLS.map(([id, title]) => (
          <button key={id} aria-pressed={tool === id} onClick={() => setTool(id)}>{title}</button>
        ))}
      </div>
      {tool === "assessment" && <Assessment notify={notify} />}
      {tool === "weights" && <Weights notify={notify} />}
      {tool === "conversion" && <Conversion notify={notify} />}
    </div>
  );
}
