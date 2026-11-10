// The console's composer: Guided / Free text, "Builds on the previous step" (scenario memory), Options & rules (the
// PMO's forced simulation while the Data Steward blocks a project) and the guided scenario form – its inputs, bounds,
// options and hints come from GET /steering/guided/{action}; the server checks them again on the run.
import { useEffect, useId, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { components } from "../../api/schema";
import { Markdown, Popover } from "../../components/ReportViewer";
import { Icon } from "../../components/ui";

type S = components["schemas"];
export type Catalog = S["GuidedCatalog"];
type Form = S["GuidedForm"];
type Param = S["ScenarioParam"];
type Value = boolean | number | string | null | undefined;
export type RunBody = { request?: string; guided?: { action: string; project: string | null; target: string | null; params: Record<string, Value> } };

const RADIO_PARAMS = new Set(["cost_type", "direction", "is_perc", "method", "funding"]); // radio buttons, as in Streamlit
const CALLOUT_NOTES = ["The Data Steward blocks", "This project is blocked", "This project is not on hold"];
const NO_TRANSFER = "—";

export function Builder({ catalog, continuation, busy, force, setForce, buildOn, setBuildOn, run, initial }: {
  catalog: Catalog;
  continuation: S["Continuation"];
  busy: boolean;
  force: boolean;
  setForce: (v: boolean) => void;
  buildOn: boolean;
  setBuildOn: (v: boolean) => void;
  run: (body: RunBody) => void;
  initial: { action?: string; project?: string; request?: string };
}) {
  const projects = catalog.projects;
  const ids = projects.map((p) => p.project_number);
  const label = (pid: string) => {
    const p = projects.find((x) => x.project_number === pid);
    return p ? `${pid} – ${p.project_name}${p.blocked ? " (blocked)" : ""}` : pid;
  };
  const validAction = catalog.scenarios.some((s) => s.action === initial.action) ? initial.action! : catalog.scenarios[0]?.action;
  const [mode, setMode] = useState<"Guided" | "Free text">(initial.request ? "Free text" : "Guided");
  const [text, setText] = useState(initial.request ?? "");
  const [action, setAction] = useState(validAction);
  const [project, setProject] = useState<string | undefined>(ids.includes(initial.project ?? "") ? initial.project : ids[0]);
  const [target, setTarget] = useState<string | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [params, setParams] = useState<Record<string, Value>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const info = catalog.scenarios.find((s) => s.action === action);
  const needsProject = info?.project !== "none";
  const fieldId = useId();

  useEffect(() => {
    if (!action) return;
    let live = true;
    api.get<Form>(`/steering/guided/${action}`, { project: needsProject ? project : undefined, force: String(force) }).then(
      (f) => {
        if (!live) return;
        setForm(f);
        setFormError(null);
        const key = `${f.action}|${project ?? ""}`; // a new scenario or project: the builder's defaults
        setParams((old) => (old.__key === key ? old : { ...Object.fromEntries(f.params.map((p) => [p.name, p.default])), __key: key }));
        const others = ids.filter((p) => p !== project);
        setTarget((t) => (f.target === "required" ? (t && others.includes(t) ? t : others[0] ?? null) : f.target === "optional" && t && others.includes(t) ? t : null));
      },
      (e: ApiError) => live && setFormError(e.message),
    );
    return () => {
      live = false;
    };
  }, [action, project, force]);

  const simulate = () => {
    if (!action || !info) return;
    const sent = Object.fromEntries(Object.entries(params).filter(([k, v]) => !k.startsWith("__") && v !== null && v !== undefined && v !== ""));
    run({ guided: { action, project: needsProject ? project ?? null : null, target: info.target !== "none" ? target : null, params: sent } });
  };
  const send = () => {
    const request = text.trim();
    if (request && !busy) {
      setText("");
      run({ request });
    }
  };

  const input = (spec: Param) => {
    const id = `${fieldId}-${spec.name}`;
    const value = params[spec.name];
    const set = (v: Value) => setParams({ ...params, [spec.name]: v });
    if (spec.kind === "choice") {
      const options = spec.options ?? [];
      if (RADIO_PARAMS.has(spec.name)) {
        return (
          <fieldset key={spec.name} className="dk-radio">
            <legend>{spec.label}</legend>
            {options.map((o, i) => (
              <label key={i} className="row">
                <input type="radio" name={id} checked={value === o.value} style={{ width: "auto" }}
                  onChange={() => {
                    if (spec.name === "is_perc") {
                      const unit = form?.params.find((p) => p.name === "value")?.unit_defaults?.[o.value ? "%" : "€"];
                      setParams({ ...params, is_perc: o.value, ...(unit ? { value: unit.default } : {}) });
                    } else set(o.value);
                  }} />
                {o.label}
              </label>
            ))}
          </fieldset>
        );
      }
      const index = options.findIndex((o) => o.value === value);
      return (
        <label key={spec.name} className="field" htmlFor={id}>
          {spec.label}
          <select id={id} value={index < 0 ? 0 : index} onChange={(e) => set(options[Number(e.target.value)]?.value)}>
            {options.map((o, i) => <option key={i} value={i}>{o.label}</option>)}
          </select>
        </label>
      );
    }
    if (spec.kind === "integer" && spec.min != null && spec.max != null) {
      return (
        <label key={spec.name} className="field" htmlFor={id} title={spec.help ?? undefined}>
          {spec.label}: {String(value ?? "")}
          <input id={id} type="range" min={spec.min} max={spec.max} step={spec.step ?? 1} value={Number(value ?? 0)}
            onChange={(e) => set(Number(e.target.value))} style={{ padding: 0 }} />
        </label>
      );
    }
    if (spec.kind === "number" || spec.kind === "integer") {
      const unit = spec.unit_defaults?.[params.is_perc === false ? "€" : "%"];
      return (
        <label key={spec.name} className="field" htmlFor={id} title={spec.help ?? undefined}>
          {spec.label}
          <input id={id} type="number" min={spec.min ?? undefined} max={spec.max ?? undefined} step={unit?.step ?? spec.step ?? 1}
            value={value === null || value === undefined ? "" : String(value)} onChange={(e) => set(e.target.value === "" ? null : Number(e.target.value))} />
        </label>
      );
    }
    if (spec.kind === "date") {
      return (
        <label key={spec.name} className="field" htmlFor={id}>
          {spec.label}
          <input id={id} type="date" value={String(value ?? "")} onChange={(e) => set(e.target.value || null)} />
        </label>
      );
    }
    return (
      <label key={spec.name} className="field" htmlFor={id}>
        {spec.label}
        <input id={id} type="text" maxLength={spec.max_length ?? 500} value={String(value ?? "")} onChange={(e) => set(e.target.value)} />
      </label>
    );
  };

  const others = ids.filter((p) => p !== project);
  const turns = continuation.turns.join(" → ");
  return (
    <section className="card dk-stack" aria-label="Composer">
      <div className="dk-composer-top">
        <div className="dk-segmented" role="group" aria-label="Input">
          {(["Guided", "Free text"] as const).map((m) => (
            <button key={m} type="button" className={mode === m ? "primary" : ""} aria-pressed={mode === m} onClick={() => setMode(m)}>{m}</button>
          ))}
        </div>
        <div className="dk-stack-tight" style={{ flex: "2 1 260px" }}>
          {continuation.available ? (
            <>
              <label className="row" title={`The next request builds on the already changed data of this what-if so far: ${turns}`}>
                <input type="checkbox" role="switch" checked={buildOn} onChange={(e) => setBuildOn(e.target.checked)} style={{ width: "auto" }} />
                Builds on the previous step (step {continuation.step})
              </label>
              <div className="muted small">
                {buildOn ? `Building on: ${turns}` : continuation.previous_verdict_blocks
                  ? `Last step was ${continuation.previous_verdict_blocks} – starting from the original data.` : "Starting from the original data."}
              </div>
            </>
          ) : <div className="muted small">New what-if · nothing to build on yet</div>}
        </div>
        <Popover label={catalog.can_force ? "Options & rules" : "Rules in use"} icon="rule" wide className="right">
          <div className="dk-stack">
            {catalog.can_force && (
              <>
                <label className="row" title="PMO only: the Data Steward still reports all findings, but blocked projects are simulated anyway (indicative results). Every forced run is audited.">
                  <input type="checkbox" role="switch" checked={force} onChange={(e) => setForce(e.target.checked)} style={{ width: "auto" }} />
                  Force simulation (ignore data warnings)
                </label>
                <hr />
              </>
            )}
            <Markdown text={catalog.rules_md} />
            <div className="muted small">Set by the PMO Head on the Rules page; the GO / CONDITIONAL GO / NO-GO verdict is computed from these rules – never by the AI.</div>
          </div>
        </Popover>
      </div>
      {force && (
        <div className="callout tone-warning row" role="status"><Icon name="warning" /><span style={{ color: "var(--dk-text)" }}>Force mode active – quality-gate blocks are ignored; results for blocked projects are indicative only.</span></div>
      )}
      {mode === "Free text" ? (
        <form className="row" onSubmit={(e) => { e.preventDefault(); send(); }} style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ flex: 1 }} htmlFor={`${fieldId}-free`}>
            Describe a change or ask a what-if …
            <input id={`${fieldId}-free`} maxLength={4000} value={text} disabled={!ids.length} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" className="primary" aria-label="Send" disabled={busy || !ids.length || !text.trim()}><Icon name="send" /></button>
        </form>
      ) : info && (
        <div className="dk-stack">
          <div className="grid">
            <label className="field" htmlFor={`${fieldId}-action`}>
              Scenario type
              <select id={`${fieldId}-action`} value={action} onChange={(e) => setAction(e.target.value)}>
                {catalog.scenarios.map((s) => <option key={s.action} value={s.action}>{s.label}</option>)}
              </select>
            </label>
            {needsProject ? (
              <label className="field" htmlFor={`${fieldId}-project`}>
                {info.project_label}
                <select id={`${fieldId}-project`} value={project} onChange={(e) => setProject(e.target.value)}>
                  {ids.map((pid) => <option key={pid} value={pid}>{label(pid)}</option>)}
                </select>
              </label>
            ) : <div className="muted small">{info.note}</div>}
            {info.target === "required" && others.length > 0 && (
              <label className="field" htmlFor={`${fieldId}-target`}>
                {info.target_label}
                <select id={`${fieldId}-target`} value={target ?? others[0]} onChange={(e) => setTarget(e.target.value)}>
                  {others.map((pid) => <option key={pid} value={pid}>{label(pid)}</option>)}
                </select>
              </label>
            )}
            {info.target === "optional" && (
              <label className="field" htmlFor={`${fieldId}-target`}>
                {info.target_label}
                <select id={`${fieldId}-target`} value={target ?? NO_TRANSFER} onChange={(e) => setTarget(e.target.value === NO_TRANSFER ? null : e.target.value)}>
                  <option value={NO_TRANSFER}>— no transfer —</option>
                  {others.map((pid) => <option key={pid} value={pid}>{label(pid)}</option>)}
                </select>
              </label>
            )}
          </div>
          {form && form.action === action && form.params.length > 0 && <div className="grid" style={{ alignItems: "end" }}>{form.params.map(input)}</div>}
          {formError && <div className="callout tone-danger" role="alert">{formError}</div>}
          {(form?.action === action ? form.notes : []).filter((n) => !(info.project === "none" && n === info.note)).map((note, i) =>
            CALLOUT_NOTES.some((p) => note.startsWith(p)) ? (
              <div key={i} className="callout tone-info row" style={{ alignItems: "flex-start" }}>
                <Icon name={note.startsWith("This project is blocked") ? "block" : "info"} />
                <span style={{ color: "var(--dk-text)" }}>{note}</span>
              </div>
            ) : <div key={i} className="muted small">{note}</div>,
          )}
          <div>
            <button type="button" className="primary" onClick={simulate} disabled={busy || !ids.length}
              title={ids.length ? undefined : "No project in your scope for this scenario type."}>
              <Icon name="play_arrow" />Simulate
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
