// Projects page: the filterable project table of the caller's scope and, under /projects?project=<id>, the project
// detail page with its tabs. A link to a project outside the scope shows the table, as the API answers such a project
// like a missing one.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type Answer } from "../api/client";
import { PageLink, usePageTitle, useWorkspace } from "../components/Shell";
import { Card, ErrorBox, PageHeader } from "../components/ui";
import { tone } from "../theme";
import { Caption, DataTable, FlashBox, MultiSelect, Notice, PageStyles, cell, eur, type Column, type Flash } from "./projects/common";
import { ProjectDetail, type Detail } from "./projects/ProjectDetail";

type ProjectList = Answer<"/api/v1/projects">;
const DQ_OK = "OK";

export function Projects() {
  const [params] = useSearchParams();
  const pid = params.get("project");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [flash, setFlash] = useState<Flash>(null);
  const { refresh } = useWorkspace();
  const done = (f: Flash) => {
    setFlash(f);
    if (f?.kind === "success") {
      setVersion((v) => v + 1); // the saved values from the API
      refresh(); // and the counts of the page frame
    }
  };

  useEffect(() => {
    setDetail(null);
    setMissing(false);
    setError(null);
    if (!pid) return;
    api.get<Detail>(`/projects/${encodeURIComponent(pid)}`).then(setDetail, (e) => {
      if (e instanceof ApiError && e.status === 404) setMissing(true);
      else setError(e instanceof Error ? e.message : String(e));
    });
  }, [pid, version]);

  if (error) return <ErrorBox message={error} />;
  if (pid && !missing && !detail) return <div className="muted" aria-busy="true">Loading …</div>;
  return (
    <>
      <PageStyles />
      <FlashBox flash={flash} onClose={() => setFlash(null)} />
      {pid && detail ? (
        <ProjectDetail key={`${pid}-${version}`} pid={pid} detail={detail} done={done} />
      ) : (
        <>
          {missing && <Notice tone="neutral" icon="info">Project not found.</Notice>}
          <ProjectTable />
        </>
      )}
    </>
  );
}

function ProjectTable() {
  const { info } = useWorkspace();
  const pageTitle = usePageTitle("projects", "Projects");
  const navigate = useNavigate();
  const [data, setData] = useState<ProjectList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [rag, setRag] = useState<string[]>([]);
  const [pms, setPms] = useState<string[]>([]);
  const [quality, setQuality] = useState("All");
  useEffect(() => {
    api.get<ProjectList>("/projects").then(setData, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);
  const projects = useMemo(() => data?.projects ?? [], [data]);
  const managers = useMemo(() => [...new Set(projects.map((p) => p.project_manager).filter(Boolean).map(String))].sort(), [projects]);
  if (error) return <ErrorBox message={error} />;
  if (!data) return null;
  const needle = text.trim().toLowerCase();
  const shown = projects.filter((p) =>
    (!needle || String(p.project_number).toLowerCase().includes(needle) || String(p.project_name).toLowerCase().includes(needle))
    && (!rag.length || rag.includes(String(p.status)))
    && (!pms.length || pms.includes(String(p.project_manager)))
    && (quality !== "With findings" || String(p.data_quality) !== DQ_OK));
  const num = (v: unknown, digits: number) => (v == null ? "–" : `${Number(v).toFixed(digits)} %`);
  const columns: Column[] = [
    { key: "project_number", label: "ID", render: (r) => <Link to={`/projects?project=${encodeURIComponent(String(r.project_number))}`} onClick={(e) => e.stopPropagation()}>{String(r.project_number)}</Link> },
    { key: "project_name", label: "Project" },
    { key: "project_manager", label: "PM" },
    { key: "status", label: "RAG", render: (r) => <span className={`badge tone-${tone("rag", String(r.status))}`}>{cell(r.status)}</span> },
    { key: "execution_type", label: "Method" },
    { key: "percent_complete", label: "Progress", render: (r) => (
      <span className="row"><span className="dk-progress" aria-hidden="true"><span style={{ width: `${Math.min(100, Number(r.percent_complete) || 0)}%` }} /></span>{num(r.percent_complete, 0)}</span>) },
    { key: "budget_cost_eur", label: "Budget", render: (r) => eur(r.budget_cost_eur) },
    { key: "actual_cost_eur", label: "Actual cost", render: (r) => eur(r.actual_cost_eur) },
    { key: "estimate_at_completion_eur", label: "EAC", render: (r) => eur(r.estimate_at_completion_eur) },
    { key: "planned_roi_pct", label: "ROI", render: (r) => num(r.planned_roi_pct, 1) },
    { key: "risk_score", label: "Risk" },
    { key: "planned_end_date", label: "Planned end", render: (r) => cell(r.planned_end_date).slice(0, 10) },
    { key: "data_quality", label: "Data quality" },
  ];
  return (
    <>
      <PageHeader title={pageTitle} subtitle={`${data.total} projects within your permissions · open a project to see its detail page`}
        action={<PageLink to="data_quality" icon="fact_check" text="Data quality" />} />
      <Card>
        <div className="dk-filters">
          <label className="field">Search
            <input type="search" placeholder="Project ID or name" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <MultiSelect label="RAG" options={["Green", "Yellow", "Red"]} value={rag} onChange={setRag} />
          <MultiSelect label="Project manager" options={managers} value={pms} onChange={setPms} />
          <div className="field">Data quality
            <div className="dk-seg" role="group" aria-label="Data quality">
              {["All", "With findings"].map((q) => <button key={q} aria-pressed={quality === q} onClick={() => setQuality(q)}>{q}</button>)}
            </div>
          </div>
        </div>
        <div className="muted small" style={{ margin: "8px 0" }}>{shown.length} of {projects.length} projects · select a row to open the project</div>
        <DataTable caption="Projects" columns={columns} rows={shown.map((p) => ({ ...p, _key: String(p.project_number) }))}
          radio={false} selected={null} onSelect={(key) => navigate(`/projects?project=${encodeURIComponent(key)}`)} />
      </Card>
      {info.user.role_label === "Project Manager" && (
        <Caption>Tip: select one of your projects to open its detail page and edit its master data.</Caption>
      )}
    </>
  );
}
