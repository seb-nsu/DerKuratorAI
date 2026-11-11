// Rules (PMO): the business rules with their bounds and the Data Steward's required fields. They belong to the PMO
// Head, who changes them here; every other PMO account reads them. Same texts and confirmations as the NiceGUI
// page; values and actions come from the /governance API, which checks the PMO Head again.
import { useEffect, useState, type ReactElement } from "react";
import { api, type Answer } from "../api/client";
import { useWorkspace } from "../components/Shell";
import { Card, Icon, PageHeader } from "../components/ui";
import { Callout, Caption, ConsoleProvider, Details, Loaded, Rich, Table, useAct, useConsole, useLoad } from "./admin/common";
import "./admin/admin.css";

type Rules = Answer<"/api/v1/governance/business-rules">;
type Required = Answer<"/api/v1/governance/required-fields">;

export function RulesPage() {
  const { info } = useWorkspace();
  const editable = info.user.pmo_head;
  return (
    <ConsoleProvider home="/rules">
      <PageHeader title="Rules" subtitle="Business rules and Data Steward required fields · they apply to every new analysis, verdict and data check" />
      {!editable && <Callout kind="info" text="Read-only – only the PMO Head changes these rules." />}
      <div className="adm-stack">
        <RulesSection editable={editable} />
        <RequiredSection editable={editable} />
      </div>
    </ConsoleProvider>
  );
}

// ------------------------------------------------------------------ business rules
const bound = (v: number | null | undefined) => (v === null || v === undefined ? "–" : Number.isInteger(v) ? v.toLocaleString("en-US") : String(v));

function RulesSection({ editable }: { editable: boolean }) {
  const state = useLoad(async () => ({ view: await api.get<Rules>("/governance/business-rules") }));
  const act = useAct();
  const { confirm } = useConsole();
  const [values, setValues] = useState<Record<string, string>>({});
  useEffect(() => {
    if (state.data) setValues(Object.fromEntries(state.data.view.rules.map((r) => [r.name, String(r.value)])));
  }, [state.data]);
  return (
    <Loaded state={state}>
      {({ view }) => {
        const rules = Object.fromEntries(view.rules.map((r) => [r.name, r]));
        return (
          <div className="adm-stack">
            <h2>Business rules</h2>
            <Caption text={"The organisation's business assumptions and verdict thresholds, owned by the PMO Head. Stored per environment in "
              + `\`${view.env_file}\` and promoted with it. Used by the calculation tools, the Data Steward and the deterministic verdict `
              + "engine, and given to the agents as 'business assumptions'."} />
            <Card>
              <form className="adm-stack" onSubmit={async (e) => {
                e.preventDefault();
                const body = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, Number(v)]));
                if (await act("PUT", "/governance/business-rules", { values: body })) await state.reload();
              }}>
                <fieldset className="adm-fieldset" disabled={!editable} style={{ border: 0, padding: 0, margin: 0 }}>
                {Object.entries(view.groups).map(([title, names]) => (
                  <fieldset key={title} className="adm-fieldset">
                    <legend><h3 style={{ margin: 0 }}>{title}</h3></legend>
                    <div className="grid">
                      {names.map((name) => {
                        const rule = rules[name];
                        const step = rule.integer ? 1 : rule.unit.startsWith("€") ? 100 : 0.5;
                        return (
                          <label key={name} className="field adm-rule" title={rule.help}>
                            <span style={{ color: "var(--dk-text)" }}>{rule.label} ({rule.unit})</span>
                            <input type="number" required value={values[name] ?? ""} step={step} min={rule.min ?? undefined} max={rule.max ?? undefined}
                              onChange={(e) => setValues({ ...values, [name]: e.target.value })} />
                            <span className="small">{bound(rule.min)} – {bound(rule.max)} · default {bound(rule.default)}</span>
                          </label>
                        );
                      })}
                    </div>
                  </fieldset>
                ))}
                </fieldset>
                {editable && <Caption text="Changing the team capacity limit rebuilds the graph database (overloaded months are pre-computed)." />}
                {editable && <div><button className="primary" type="submit"><Icon name="save" />Save business rules</button></div>}
              </form>
            </Card>
            {editable && <div><button onClick={async () => {
              if (await confirm("Reset to defaults", "Replaces every business rule threshold with its default. Verdicts from now on use the defaults; "
                + "your values are lost (the audit trail keeps the change).", "Reset business rules")
                && await act("POST", "/governance/business-rules/reset", { confirm: true })) await state.reload();
            }}><Icon name="restart_alt" />Reset to defaults …</button></div>}
            <Details summary="How the verdict engine decides"><Markdown text={view.verdict_rules_md} /></Details>
          </div>
        );
      }}
    </Loaded>
  );
}

// The verdict rules arrive as small Markdown: headings, bullet lists, tables, **bold** and `code`.
function Markdown({ text }: { text: string }) {
  const blocks: ReactElement[] = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-+:?$/.test(c))) rows.push(cells);
        i++;
      }
      i--;
      const [head, ...body] = rows;
      blocks.push(
        <div key={i} className="table-wrap"><table className="adm-table">
          <thead><tr>{head.map((c, j) => <th key={j}><Rich text={c} /></th>)}</tr></thead>
          <tbody>{body.map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j} style={{ whiteSpace: "normal" }}><Rich text={c} /></td>)}</tr>)}</tbody>
        </table></div>,
      );
    } else if (/^#{1,6}\s/.test(line)) {
      blocks.push(<h4 key={i} style={{ margin: "12px 0 4px" }}><Rich text={line.replace(/^#+\s/, "")} /></h4>);
    } else if (/^\s*[-*]\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s/, ""));
      i--;
      blocks.push(<ul key={i}>{items.map((t, k) => <li key={k}><Rich text={t} /></li>)}</ul>);
    } else if (line.trim()) {
      blocks.push(<p key={i} style={{ margin: "6px 0" }}><Rich text={line} /></p>);
    }
  }
  return <div>{blocks}</div>;
}

// ------------------------------------------------------------------ required fields
function RequiredSection({ editable }: { editable: boolean }) {
  const state = useLoad(async () => ({ view: await api.get<Required>("/governance/required-fields") }));
  const act = useAct();
  const { confirm } = useConsole();
  const [chosen, setChosen] = useState<string[]>([]);
  useEffect(() => {
    if (state.data) setChosen(state.data.view.required_fields);
  }, [state.data]);
  return (
    <Loaded state={state}>
      {({ view }) => {
        const label = (f: string) => view.labels[f] ?? f;
        const locked = view.locked.map(label).join(", ");
        return (
          <div className="adm-stack">
            <Card>
              <div className="adm-stack">
                <h2 style={{ marginTop: 0 }}>Data Steward required fields</h2>
                <fieldset className="adm-fieldset" disabled={!editable} title={"Empty values in these fields are reported as ERROR and block the project for simulation (PMO can "
                  + `force). Always required: ${locked}. If the estimate at completion (EAC) is required, a missing EAC blocks the project already `
                  + "here – before the verdict's own EAC-missing rule can apply."}>
                  <legend className="muted small">Required fields (columns of the Projects table)</legend>
                  <div className="adm-checks">
                    {view.options.map((o) => (
                      <label key={o} className="row small">
                        <input type="checkbox" style={{ width: "auto" }} checked={chosen.includes(o)}
                          onChange={(e) => setChosen(e.target.checked ? [...chosen, o] : chosen.filter((x) => x !== o))} />
                        {label(o)}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <Caption text={`Always required: ${locked}.`} />
                {editable && <div><button className="primary" onClick={async () => {
                  if (await act("PUT", "/governance/required-fields", { fields: chosen })) await state.reload();
                }}><Icon name="save" />Save required fields</button></div>}
              </div>
            </Card>
            {editable && <div><button onClick={async () => {
              if (await confirm("Restore default required fields", "Replaces the required-field list of this environment with the defaults. "
                + "Your own selection is lost.", "Restore defaults") && await act("POST", "/governance/required-fields/reset", { confirm: true })) await state.reload();
            }}><Icon name="restart_alt" />Restore default required fields …</button></div>}
            <Caption text={`Stored per environment in \`${view.env_file}\` · ${view.required_fields.length} fields active.`
              + (view.missing_calc_fields.length ? ` Note: ${view.missing_calc_fields.map(label).join(", ")} are not required – budget simulations `
                + "still reject projects with missing values." : "")} />
            <Details summary="Other Data Steward rules (deterministic, read-only)">
              <Table rows={view.data_quality_profiles} columns={[["tag", "data_quality_profile"], ["severity", "Severity"], ["meaning", "Meaning"]]} />
              <p>Also: logic check (progress without actual start), past due, budget overrun (EAC &gt; budget), CapEx/OpEx split, resource overload, task integrity.</p>
            </Details>
          </div>
        );
      }}
    </Loaded>
  );
}

