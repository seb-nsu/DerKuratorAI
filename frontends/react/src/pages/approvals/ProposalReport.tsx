// The report of one proposal in the PMO inbox: the shared report viewer on GET /proposals/{id}/report with the
// proposal's eyebrow and status badge, the export menu (SteerCo Pack, Markdown, CSV) and the report's question box –
// as the Streamlit inbox shows it. The decision bar stays on the Approvals page.
import { useEffect, useState } from "react";
import { api, ApiError, type Answer } from "../../api/client";
import { ExportMenu, ReportViewer, type ReportView } from "../../components/ReportViewer";
import { ErrorBox } from "../../components/ui";
import { type Tone } from "../../theme";

export type ProposalDetail = Answer<"/api/v1/proposals/{proposal_id}">;
export const CHANGE_COLUMNS = ["Project", "Change", "Current", "Proposed", "Δ", "Approval", "Note"].map((c) => ({ key: c, label: c }));

export function ProposalReport({ proposal, status }: { proposal: ProposalDetail; status?: { text: string; tone: Tone; icon: string } }) {
  const [report, setReport] = useState<ReportView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  useEffect(() => {
    setReport(null);
    setError(null);
    setExportError(null);
    api.get<ReportView>(`/proposals/${proposal.id}/report`).then(setReport, (e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [proposal.id]);

  if (error) return <ErrorBox message={error} />;
  if (!report) return <div className="muted" aria-busy="true">Loading the report …</div>;
  return (
    <ReportViewer report={report} ask={{ proposalId: proposal.id }}
      eyebrow={`Proposal ${proposal.id} · from ${proposal.requester} · ${proposal.submitted_label}`}
      status={status ? { label: status.text, tone: status.tone, icon: status.icon } : null}
      title={proposal.request || "–"} actions={<ExportMenu base={`/proposals/${proposal.id}`} onError={setExportError} />}>
      {exportError && <ErrorBox message={exportError} />}
      {proposal.justification && <p>Reason given by the requester: {proposal.justification}</p>}
    </ReportViewer>
  );
}
