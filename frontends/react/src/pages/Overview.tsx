// Overview: top risks, KPI ribbon, portfolio briefing and questions, risk vs. ROI, overloaded teams and – for the PMO –
// the management reserve with the skill heatmap, for a Project Manager their proposals waiting for the PMO. Each section
// loads on its own, so a failing one never takes the others down.
import { Link } from "react-router-dom";
import { type Answer } from "../api/client";
import { AskBox } from "../components/AskBox";
import { PlotlyFigure } from "../components/PlotlyFigure";
import { Badge, Card, Empty, ErrorBox, Icon, PageHeader } from "../components/ui";
import { eur, eurM, pct, short } from "./overview/format";
import { Caption, Info, Metric, Section, useApi } from "./overview/parts";

type Page = Answer<"/api/v1/overview">;
type Risks = Answer<"/api/v1/overview/top-risks">;
type OpenItems = Answer<"/api/v1/overview/open-items">;
type Reserve = Answer<"/api/v1/overview/reserve">;
type Kpis = NonNullable<Page["kpis"]>;

function TopRisks({ risks }: { risks: Risks }) {
  if (!risks.available) return <Info text={risks.message ?? ""} icon="hourglass_top" />;
  if (!risks.risks.length) return <Caption text="No blocked projects or systemic risks in your scope." icon="check_circle" />;
  return (
    <div className="grid">
      {risks.risks.map((risk, i) => {
        const critical = risk.severity === "critical";
        return (
          <Card key={i}>
            <div className="stack">
              <div className={`callout tone-${critical ? "danger" : "warning"}`}>
                <div className="row" style={{ fontWeight: 700 }}>
                  <Icon name={critical ? "error" : "warning"} label={critical ? "Critical" : "Warning"} />{risk.title}
                </div>
              </div>
              <Metric label={risk.metric_label} value={risk.metric_value} />
              <div className="muted small">{[risk.name, risk.detail].filter(Boolean).join(" · ")}</div>
              {risk.can_open && risk.project && (
                <Link className="row link-button" to={`/projects?project=${encodeURIComponent(risk.project)}`}>
                  <Icon name="open_in_new" />Open project
                </Link>
              )}
            </div>
          </Card>
        );
      })}
    </div>
  );
}

function KpiRibbon({ k }: { k: Kpis }) {
  const dq = k.data_quality_index != null;
  const blocked = k.blocked_projects ?? 0;
  return (
    <>
      <div className="grid">
        {k.show_active && (
          <Card><Metric label="Active projects" value={String(k.active_projects)} caption={`of ${k.projects} projects within your permissions`} /></Card>
        )}
        <Card><Metric label="Total budget" value={eurM(k.total_budget_eur)} caption={`EAC ${eurM(k.total_eac_eur)}`} /></Card>
        <Card>
          <Metric label="Critical projects" value={String(k.critical_projects)} help="Projects with RAG status red within your permissions."
            caption={k.critical_projects ? `${pct(k.critical_share_pct)} of the portfolio` : "No red projects"} />
        </Card>
        <Card>
          <Metric label="Max team utilisation" value={pct(k.max_team_utilisation_pct, 1)}
            caption={k.top_team ? `Top team: ${k.top_team}` : "No team above the capacity limit"} />
        </Card>
        {dq && (
          <Card>
            <Metric label="Data quality index" value={pct(k.data_quality_index)}
              help="Share of projects without ERROR findings, i.e. released for quantitative simulation."
              caption={blocked ? `${blocked} project(s) blocked for simulation` : "No project blocked"} />
          </Card>
        )}
      </div>
      {dq && <Link className="row link-button" to="/data_quality"><Icon name="arrow_forward" />Data quality details</Link>}
    </>
  );
}

function Charts({ page }: { page: Page }) {
  const bubble = page.risk_value?.figure;
  const teams = page.overloaded_teams?.figure;
  return (
    <div className="grid-2">
      <Card>
        <h3>Risk vs. ROI</h3>
        <div className="muted small">Bubble size = budget · shape and colour = RAG status</div>
        {bubble ? <PlotlyFigure figure={bubble} /> : <Info text="No projects with risk score, ROI and budget for the risk/value matrix." />}
      </Card>
      <Card>
        <h3>Top 5 overloaded teams</h3>
        {teams ? <PlotlyFigure figure={teams} />
          : <Info text={`No team above the capacity limit (${short(page.kpis?.capacity_limit_pct ?? 0)} %) in your project scope.`} />}
      </Card>
    </div>
  );
}

function OpenItemsView({ items }: { items: OpenItems }) {
  return (
    <>
      {!items.proposals.length ? (
        <Caption text="No proposal of yours is waiting for the PMO." icon="check_circle" />
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Proposal</th><th>Request</th><th>Submitted</th><th>Due</th><th>Deadline</th></tr></thead>
            <tbody>
              {items.proposals.map((p) => (
                <tr key={p.id}>
                  <td>{p.id}</td><td>{p.request || "–"}</td><td>{p.submitted_label}</td><td>{p.due_date ?? "–"}</td><td>{p.deadline_label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Link className="row link-button" to="/approvals"><Icon name="arrow_forward" />Open Approvals</Link>
    </>
  );
}

function ReserveView({ reserve }: { reserve: Reserve }) {
  return (
    <div className="reserve">
      <div className="card" style={{ height: "auto" }}>
        <div className="stack">
          <Metric label="Management reserve" value={eur(reserve.balance_eur)}
            help="Configured start (business rules) plus the bookings in Reserve_Ledger." />
          <span><Badge text={`floor ${eur(reserve.floor_eur)}`} icon="vertical_align_bottom" /></span>
          {reserve.below_floor && <ErrorBox message="The reserve is below its floor." />}
        </div>
      </div>
      <div style={{ minWidth: 0 }}>
        {!reserve.skills.length || !reserve.figure ? (
          <Caption text="No skill data recorded." />
        ) : (
          <>
            <div className="scroll-x"><div style={{ minWidth: 640 }}><PlotlyFigure figure={reserve.figure} /></div></div>
            <Caption text={`Utilisation per skill across all teams; alert above ${short(reserve.overload_alert_pct)} %.`} />
          </>
        )}
      </div>
    </div>
  );
}

export function Overview() {
  const page = useApi<Page>("/overview");
  const risks = useApi<Risks>("/overview/top-risks");
  const own = page?.data?.own_view;
  const items = useApi<OpenItems>(own === true ? "/overview/open-items" : null);
  const reserve = useApi<Reserve>(own === false && page?.data?.kpis ? "/overview/reserve" : null);

  if (!page) return <><PageHeader title="Overview" /><div className="muted" aria-busy="true">Loading …</div></>;
  if (page.error !== undefined || !page.data) return <><PageHeader title="Overview" /><ErrorBox message={page.error ?? ""} /></>;
  const data = page.data;
  return (
    <>
      <PageHeader title="Overview" subtitle={data.scope_line} />
      {data.message || !data.kpis ? (
        <Empty message={data.message ?? ""} />
      ) : (
        <>
          <h2>Top risks</h2>
          <Section state={risks}>{(r) => <TopRisks risks={r} />}</Section>
          <KpiRibbon k={data.kpis} />
          <AskBox page="overview" />
          <h2>Portfolio analysis</h2>
          <Charts page={data} />
          {data.own_view ? (
            <>
              <h2>Your open items</h2>
              <Section state={items}>{(i) => <OpenItemsView items={i} />}</Section>
            </>
          ) : (
            <>
              <h2>Skills and management reserve</h2>
              <Section state={reserve}>{(r) => <ReserveView reserve={r} />}</Section>
              <Link className="row link-button" to="/insights"
                title="Status check of the sources, the portfolio with probabilities and the review of past decisions">
                <Icon name="insights" />Decision insights
              </Link>
            </>
          )}
        </>
      )}
    </>
  );
}
