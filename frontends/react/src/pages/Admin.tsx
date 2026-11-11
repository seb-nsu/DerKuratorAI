// System Admin Console: system status, operations (snapshots, database, notifications, environment staging), logs and
// the developer tools (models and shadowing, MCP, FinOps, shadow logs, in-app system tests). Same
// areas, texts and confirmations as the NiceGUI page; every value and action comes from the
// /admin API.
// Deep link: /admin?tab=<area> or ?tab=<area>.<section>, e.g. ?tab=developer.models.
import { useEffect, useRef, useState, type ComponentType, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Answer } from "../api/client";
import { PageHeader } from "../components/ui";
import { Callout, ConsoleProvider } from "./admin/common";
import { EnvironmentSection, McpSection, ModelsSection } from "./admin/Configuration";
import { AuditSection, IncidentsSection, RunsSection, ShadowSection, SystemLogSection, TestsSection } from "./admin/Logs";
import { DatabaseSection, FinOpsSection, NotificationsSection, SnapshotsSection, SystemStatus } from "./admin/Operations";
import "./admin/admin.css";

type Part = [string, string, ComponentType];
// Business tasks first, developer tools last; the business rules and required fields are the PMO Head's (Rules page).
const LAYOUT: [string, string, Part[]][] = [
  ["status", "System status", [["status", "System status", SystemStatus]]],
  ["operations", "Operations", [
    ["snapshots", "Snapshots", SnapshotsSection], ["database", "Database", DatabaseSection],
    ["notifications", "Notifications", NotificationsSection], ["environment", "Environment staging", EnvironmentSection],
  ]],
  ["logs", "Logs", [
    ["runs", "Analysis runs", RunsSection], ["audit", "Audit logs", AuditSection], ["incidents", "Incidents", IncidentsSection],
    ["system", "System logs", SystemLogSection],
  ]],
  ["developer", "Developer", [
    ["models", "Models, prompt & shadowing", ModelsSection], ["mcp", "Integrations (MCP)", McpSection],
    ["finops", "FinOps", FinOpsSection], ["shadow", "Shadow logs", ShadowSection], ["tests", "System tests", TestsSection],
  ]],
];

// An accessible tab list: arrow keys move between tabs, the panel is labelled by its tab.
function Tabs({ id, tabs, value, onChange }: { id: string; tabs: [string, string][]; value: string; onChange: (v: string) => void }) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const onKey = (e: KeyboardEvent) => {
    const ids = tabs.map(([t]) => t);
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    const next = ids[(ids.indexOf(value) + step + ids.length) % ids.length];
    onChange(next);
    refs.current[next]?.focus();
  };
  return (
    <div className="adm-tabs" role="tablist" onKeyDown={onKey}>
      {tabs.map(([t, label]) => (
        <button key={t} ref={(el) => { refs.current[t] = el; }} role="tab" id={`${id}-${t}`} aria-selected={t === value}
          aria-controls={`${id}-panel`} tabIndex={t === value ? 0 : -1} className={t === value ? "active" : ""} onClick={() => onChange(t)}>
          {label}
        </button>
      ))}
    </div>
  );
}

function parse(tab: string | null): [string, string] {
  const [area, section] = (tab ?? "").split(".");
  const entry = LAYOUT.find(([a]) => a === area) ?? LAYOUT[0];
  const parts = entry[2];
  return [entry[0], parts.some(([s]) => s === section) ? section : parts[0][0]];
}

export function Admin() {
  const [params, setParams] = useSearchParams();
  const [area, section] = parse(params.get("tab"));
  const [issue, setIssue] = useState<string | null>(null);
  useEffect(() => {
    api.get<Answer<"/api/v1/admin/status">>("/admin/status").then((s) => setIssue(s.config_issue ?? null), () => setIssue(null));
  }, []);
  // the open tab stays in the address bar, so a reload or a shared link opens it again
  const open = (a: string, s?: string) => {
    const parts = LAYOUT.find(([x]) => x === a)![2];
    setParams({ tab: parts.length > 1 ? `${a}.${s ?? parts[0][0]}` : a }, { replace: true });
  };
  const parts = LAYOUT.find(([a]) => a === area)![2];
  const Section = parts.find(([s]) => s === section)![2];
  return (
    <ConsoleProvider>
      <PageHeader title="System Admin Console" subtitle={"System-wide AI settings and logs. Non-secret settings live in "
        + "data/env_<environment>.json; API keys come from .env and are kept in memory only."} />
      {issue && <Callout kind="error" text={`**Environment config not applied – running on defaults.** ${issue}. Fix the file or save the settings below to write a valid configuration.`} />}
      <Tabs id="adm-area" tabs={LAYOUT.map(([a, t]) => [a, t])} value={area} onChange={(a) => open(a)} />
      <div role="tabpanel" id="adm-area-panel" aria-labelledby={`adm-area-${area}`} className="adm-stack">
        {parts.length > 1 && <Tabs id="adm-section" tabs={parts.map(([s, t]) => [s, t])} value={section} onChange={(s) => open(area, s)} />}
        <div role={parts.length > 1 ? "tabpanel" : undefined} id="adm-section-panel" aria-labelledby={parts.length > 1 ? `adm-section-${section}` : undefined}>
          <Section key={`${area}.${section}`} />
        </div>
      </div>
    </ConsoleProvider>
  );
}
