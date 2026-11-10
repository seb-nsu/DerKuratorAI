// The master-data form of the project detail page (owning Project Manager): two sections, two buttons – the direct
// section is saved at once, the governed section becomes one proposal for the PMO. Only changed fields are sent; the API
// validates them, so a field it refuses is reported with the API's own words.
import { useEffect, useState } from "react";
import { api, type Answer } from "../../api/client";
import { ErrorBox, Icon } from "../../components/ui";
import { Caption, type Flash } from "./common";

type Form = Answer<"/api/v1/projects/{project_id}/edit-form">;
type Field = Form["sections"][string][number];
type Finding = Answer<"/api/v1/projects/{project_id}">["data_quality_findings"][number];

const SECTIONS: Record<string, [string, string, string, string, string]> = {
  direct: ["Master data", "saved directly", "Save changes", "save", "Change a master-data field first."],
  governed: ["Governed fields", "require PMO approval (four-eyes principle)", "Submit for approval", "send", "Change a governed field first."],
};
const NUMERIC = new Set(["number", "percent", "int"]);

const same = (f: Field, value: string) => {
  const shown = f.value == null ? "" : String(f.value);
  if (NUMERIC.has(f.kind)) return (value === "" && shown === "") || (value !== "" && shown !== "" && Number(value) === Number(shown));
  return value === shown;
};

function Input({ f, value, onChange }: { f: Field; value: string; onChange: (v: string) => void }) {
  const label = f.label + (f.missing ? " · required – missing" : "");
  const common = { disabled: !f.editable, value, "aria-invalid": f.missing || undefined };
  let control;
  if (f.kind === "select") {
    control = (
      <select {...common} onChange={(e) => onChange(e.target.value)}>
        {value === "" && <option value="">–</option>}
        {(f.options ?? []).map((o) => <option key={o}>{o}</option>)}
      </select>
    );
  } else if (f.kind === "textarea") {
    control = <textarea {...common} rows={4} onChange={(e) => onChange(e.target.value)} />;
  } else if (f.kind === "date") {
    control = <input {...common} type="date" min={String(f.min ?? "")} max={String(f.max ?? "")} onChange={(e) => onChange(e.target.value)} />;
  } else if (NUMERIC.has(f.kind)) {
    control = <input {...common} type="number" min={f.min ?? undefined} max={f.max ?? undefined} step={f.step ?? undefined} onChange={(e) => onChange(e.target.value)} />;
  } else {
    control = <input {...common} onChange={(e) => onChange(e.target.value)} />;
  }
  const original = f.stored_out_of_range;
  return (
    <div style={f.kind === "textarea" ? { gridColumn: "1 / -1" } : undefined}>
      <label className={`field ${f.missing ? "field-missing" : ""}`}>
        <span className="row">{f.missing && <Icon name="error" size={16} />}{label}</span>
        {control}
      </label>
      {original != null && (
        <Caption icon="error" tone="danger">
          Stored value {String(original)} is outside the valid range ({f.min ?? "–"} to {f.max ?? "–"}) – shown as {String(f.value)}; please correct it.
        </Caption>
      )}
    </div>
  );
}

function Section({ pid, name, fields, editable, done }: { pid: string; name: string; fields: Field[]; editable: boolean; done: (f: Flash) => void }) {
  const [title, note, buttonLabel, icon, hint] = SECTIONS[name];
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(fields.map((f) => [f.field, f.value == null ? "" : String(f.value)])));
  const [errors, setErrors] = useState<string | null>(null);
  const changed = fields.filter((f) => f.editable && !same(f, values[f.field]));
  const save = async () => {
    const body = Object.fromEntries(changed.map((f) => {
      const v = values[f.field];
      return [f.field, NUMERIC.has(f.kind) ? (v === "" ? null : Number(v)) : v === "" ? null : v];
    }));
    try {
      const outcome = await api.post<{ kind: string; message: string }>(`/projects/${encodeURIComponent(pid)}/master-data`, { section: name, values: body });
      setErrors(null);
      done(outcome);
    } catch (e) {
      setErrors(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <section style={{ marginTop: 12 }}>
      <h4 style={{ margin: "8px 0" }}>{title} <span className="muted" style={{ fontWeight: 400 }}>· {note}</span></h4>
      {name === "governed" && editable && (
        <Caption>Changes in this section are submitted as a proposal to the PMO inbox and written after approval. Approved budget and
          CapEx/OpEx plan can be filled in here only while empty – to change an existing budget, simulate a budget scenario in the
          Steering Console.</Caption>
      )}
      <div className="dk-form-grid" style={{ marginTop: 8 }}>
        {fields.map((f) => <Input key={f.field} f={f} value={values[f.field]} onChange={(v) => setValues({ ...values, [f.field]: v })} />)}
      </div>
      {errors && <div style={{ marginTop: 8, whiteSpace: "pre-wrap" }}><ErrorBox message={errors} /></div>}
      {editable && (
        <div style={{ marginTop: 12 }}>
          <button className="primary" disabled={!changed.length} title={changed.length ? undefined : hint} onClick={() => void save()}>
            <Icon name={icon} />{buttonLabel}
          </button>
        </div>
      )}
    </section>
  );
}

export function EditForm({ pid, findings, done }: { pid: string; findings: Finding[]; done: (f: Flash) => void }) {
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<Form>(`/projects/${encodeURIComponent(pid)}/edit-form`).then(setForm, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [pid]);
  if (error) return <ErrorBox message={error} />;
  if (!form) return null;
  return (
    <div>
      <h3 className="dk-h3">Edit master data – {pid}</h3>
      {form.access === "read" && <Caption>PMO view is read-only. Only the owning Project Manager can edit project master data.</Caption>}
      {form.missing_required.length > 0 && <ErrorBox message={`Required fields missing: ${form.missing_required.join(", ")}`} />}
      {findings.length > 0 && (
        <details>
          <summary>Data Steward findings for this project ({findings.length})</summary>
          <ul>
            {findings.map((f, i) => <li key={i}><strong>{f.severity}</strong> · {f.rule_label} · {f.field_label || "–"} – {f.message}</li>)}
          </ul>
        </details>
      )}
      {Object.entries(form.sections).map(([name, fields], i) => (
        <div key={name}>
          {i > 0 && <hr style={{ border: 0, borderTop: "1px solid var(--dk-border)", margin: "16px 0" }} />}
          <Section pid={pid} name={name} fields={fields} editable={form.access === "edit"} done={done} />
        </div>
      ))}
    </div>
  );
}
