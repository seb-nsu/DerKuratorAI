// Approvals page: the PMO inbox (pick a proposal, then decide it – approve after a confirmation with the change set, a
// NO-GO only with an explicit override and a reason, reject with a comment), the decided proposals with the revert of
// an approval, the caller's own proposals (withdraw) and the delegation of the approval right. Who is no approver sees
// only their own proposals. Four-eyes principle, scope and deadlines are enforced by the API.
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError, type Answer } from "../api/client";
import { usePageTitle, useWorkspace } from "../components/Shell";
import { Card, ErrorBox, Icon, PageHeader } from "../components/ui";
import { CHANGE_COLUMNS, ProposalReport, type ProposalDetail } from "./approvals/ProposalReport";
import {
  Caption, Confirm, DataTable, Dialog, FlashBox, Notice, PageStyles, type Flash,
} from "./projects/common";

type Overview = Answer<"/api/v1/approvals/overview">;
type ProposalList = Answer<"/api/v1/proposals">;
type Proposal = ProposalList["proposals"][number];
type Delegations = Answer<"/api/v1/delegations">;

const ACTIONS = {
  apply: ["Approve & apply", "task_alt"], handover: ["Approve & record hand-over", "assignment_turned_in"],
  reject: ["Reject", "close"], withdraw: ["Withdraw", "undo"], revert: ["Revert", "history"],
} as const;
const HANDOVER_TEXT = "Nothing is written; the resource change is recorded for manual execution.";
const VIEW_LABELS: Record<string, string> = { pending: "Pending", decided: "Decided", mine: "Mine", delegations: "Delegations" };

const message = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));
const projectsText = (p: Proposal, names: Record<string, string>) =>
  (p.projects ?? []).map((id) => (names[id] ? `${id} ${names[id]}` : id)).join(", ") || "–";
const cols = (labels: string[]) => labels.map((l) => ({ key: l, label: l }));

/** After an action: the outcome, fresh tables and the counts in the navigation. */
type Done = (flash: Flash) => void;

export function Approvals() {
  const { refresh } = useWorkspace();
  const pageTitle = usePageTitle("approvals", "Approvals");
  const [params, setParams] = useSearchParams();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    api.get<Overview>("/approvals/overview").then((o) => {
      setOverview(o);
      const e = o.deadline_events;
      const parts = [
        ...(e.escalated ? [`${e.escalated} proposal(s) escalated past the approval deadline`] : []),
        ...(e.expired ? [`${e.expired} proposal(s) expired without a decision`] : []),
      ];
      if (parts.length) setFlash((f) => f ?? { kind: "info", message: parts.join(" · ") });
    }, (e) => setError(message(e)));
  }, [version]);

  const done: Done = useCallback((f) => {
    setFlash(f);
    setVersion((v) => v + 1);
    refresh();
  }, [refresh]);

  if (error) return <ErrorBox message={error} />;
  if (!overview) return <div className="muted" aria-busy="true">Loading …</div>;
  const box = <FlashBox flash={flash} onClose={() => setFlash(null)} />;
  if (!overview.approver) {
    return (
      <>
        <PageStyles />
        <PageHeader title={pageTitle} subtitle="Your proposals and the PMO's decisions" />
        {box}
        <MyProposals key={version} done={done} />
      </>
    );
  }
  const view = overview.views.includes(params.get("tab") ?? "") ? params.get("tab")! : "pending";
  const deputy = overview.deputy_of;
  return (
    <>
      <PageStyles />
      <PageHeader title={pageTitle} subtitle="Four-eyes principle · a proposal is written only after a second PMO approves it" />
      {box}
      {deputy && (
        <Notice icon="badge">
          You are deputy of <strong>{deputy.delegator}</strong> until {deputy.end} – you decide proposals on their behalf,
          their own proposals in your own name (never your own).
        </Notice>
      )}
      <div className="dk-seg" role="group" aria-label="View">
        {overview.views.map((v) => (
          <button key={v} aria-pressed={v === view} onClick={() => setParams({ tab: v })}>
            {v in overview.counts ? `${VIEW_LABELS[v]} (${overview.counts[v]})` : VIEW_LABELS[v]}
          </button>
        ))}
      </div>
      {view === "mine" && <MyProposals key={version} done={done} />}
      {view === "delegations" && <DelegationsView key={version} done={done} />}
      {view === "decided" && <Decided key={version} done={done} />}
      {view === "pending" && <Pending key={version} done={done} linked={params.get("proposal")} />}
    </>
  );
}

// =========================================================================== inbox
function Pending({ done, linked }: { done: Done; linked: string | null }) {
  const [list, setList] = useState<ProposalList | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.get<ProposalList>("/proposals", { view: "pending" }).then((l) => {
      setList(l);
      // a link to one proposal (?proposal=… from a notification or another page) opens it directly
      if (linked && l.proposals.some((p) => p.id === linked)) setPicked(linked);
    }, (e) => setError(message(e)));
  }, [linked]);
  useEffect(() => {
    setDetail(null);
    if (picked) api.get<ProposalDetail>(`/proposals/${picked}`).then(setDetail, (e) => setError(message(e)));
  }, [picked]);

  if (error) return <ErrorBox message={error} />;
  if (!list) return null;
  const pending = list.proposals;
  const rows = pending.map((p) => ({
    _key: p.id, Request: p.request || "–", Projects: projectsText(p, list.project_names),
    Verdict: p.verdict_label, "Budget Δ": p.budget_delta_label, Requester: p.requester || "–",
    Submitted: p.submitted_label, Due: p.due_date || "–", Deadline: p.deadline_label,
  }));
  const level = detail?.escalation_level ?? 0;
  return (
    <>
      <h2>Pending approvals ({pending.length})</h2>
      {!pending.length ? (
        <Caption icon="check_circle">No proposals waiting for approval – the inbox is empty.</Caption>
      ) : (
        <>
          <DataTable caption="Pending approvals" columns={cols(["Request", "Projects", "Verdict", "Budget Δ", "Requester", "Submitted", "Due", "Deadline"])}
            rows={rows} selected={picked} onSelect={setPicked} />
          {!picked && <Notice icon="touch_app">Select a proposal in the table to review and decide it.</Notice>}
          {detail && (
            <>
              <ProposalReport proposal={detail} status={level
                ? { text: `Escalated${level > 1 ? " twice" : ""} – overdue`, tone: "danger", icon: "schedule" }
                : { text: "Waiting for your decision", tone: "info", icon: "pending_actions" }} />
              <DecisionBar key={detail.id} proposal={detail} done={done} />
            </>
          )}
        </>
      )}
    </>
  );
}

function DecisionBar({ proposal, done }: { proposal: ProposalDetail; done: Done }) {
  const [comment, setComment] = useState("");
  const [confirming, setConfirming] = useState(false);
  const hints = proposal.decision;
  const blocked = !proposal.can_decide;
  const [label, icon] = ACTIONS[hints.manual ? "handover" : "apply"];
  const reject = async () => {
    try {
      const outcome = await api.post<{ kind: string; message: string }>(`/proposals/${proposal.id}/reject`, { comment: comment.trim() });
      done(outcome);
    } catch (e) {
      done({ kind: "error", message: message(e) });
    }
  };
  return (
    <Card>
      <h3 className="dk-h3" style={{ marginTop: 0 }}>Decision on proposal {proposal.id}</h3>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
        <label className="field" style={{ flex: "1 1 240px" }}>Comment (required for a rejection)
          <input value={comment} onChange={(e) => setComment(e.target.value)} />
        </label>
        <button disabled={blocked || !comment.trim()} onClick={() => void reject()}
          title={hints.own ? "Four-eyes principle: another PMO decides on your own proposal." : !comment.trim() ? "Enter a comment first – a rejection needs a reason." : undefined}>
          <Icon name={ACTIONS.reject[1]} />{ACTIONS.reject[0]}
        </button>
        <button className="primary" disabled={blocked} onClick={() => setConfirming(true)}
          title={hints.own ? "Four-eyes principle: you cannot approve your own proposal." : "Opens a confirmation with the change set."}>
          <Icon name={icon} />{label} …
        </button>
      </div>
      {hints.own && (
        <div style={{ marginTop: 8 }}>
          <Notice icon="lock">Four-eyes principle: you cannot approve or reject your own proposal – a second PMO decides.</Notice>
        </div>
      )}
      {!hints.own && hints.proxy_own && (
        <Caption icon="badge">This is a proposal of {hints.delegator}, whom you deputise for – you decide it in your own name.</Caption>
      )}
      {!hints.own && hints.needs_override && (
        <Caption icon="gpp_maybe">Rule-based verdict {proposal.verdict} – approving needs an explicit override with a reason.</Caption>
      )}
      {confirming && !blocked && <ApproveDialog proposal={proposal} comment={comment} done={done} onClose={() => setConfirming(false)} />}
    </Card>
  );
}

/** Writing to the data source cannot simply be undone, so approving needs an explicit confirmation with the change
 * set (and, against a NO-GO, the override with its reason). */
function ApproveDialog({ proposal, comment, done, onClose }: { proposal: ProposalDetail; comment: string; done: Done; onClose: () => void }) {
  const [override, setOverride] = useState(false);
  const [reason, setReason] = useState("");
  const hints = proposal.decision;
  const [label, icon] = ACTIONS[hints.manual ? "handover" : "apply"];
  const changes = proposal.change_rows ?? [];
  const ready = !hints.needs_override || (override && !!reason.trim());
  const approve = async () => {
    onClose();
    const body = hints.needs_override ? { comment: reason.trim(), override: true } : { comment: comment.trim(), override: false };
    try {
      done(await api.post<{ kind: string; message: string }>(`/proposals/${proposal.id}/approve`, body));
    } catch (e) {
      done({ kind: "error", message: message(e) });
    }
  };
  return (
    <Dialog title={`Confirm: ${label.toLowerCase()}`} onClose={onClose}>
      {hints.manual ? (
        <Notice icon="assignment_turned_in">Approve proposal <strong>{proposal.id}</strong> of {proposal.requester}. {HANDOVER_TEXT}</Notice>
      ) : (
        <Notice tone="warning" icon="warning">
          Approve proposal <strong>{proposal.id}</strong> of {proposal.requester} and write <strong>{changes.length} change(s)</strong> to
          the portfolio data source now? A snapshot is taken first; the System Admin can restore it.
        </Notice>
      )}
      <DataTable columns={CHANGE_COLUMNS} rows={changes} caption="Changes to approve" />
      {hints.needs_override && (
        <>
          <Notice tone="danger" icon="gpp_maybe">
            The rule-based verdict is <strong>{proposal.verdict}</strong>. Approving means overriding it – the override and your
            reason are recorded in the audit trail.
          </Notice>
          <label className="row"><input type="checkbox" style={{ width: "auto" }} checked={override} onChange={(e) => setOverride(e.target.checked)} />
            Override the {proposal.verdict} verdict</label>
          <label className="field">Reason for the override (required)
            <textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        </>
      )}
      <div className="row" style={{ justifyContent: "flex-end", flexWrap: "wrap" }}>
        <button onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!ready} onClick={() => void approve()}><Icon name={icon} />Confirm: {label.toLowerCase()}</button>
      </div>
    </Dialog>
  );
}

// =========================================================================== decided
function Decided({ done }: { done: Done }) {
  const [list, setList] = useState<ProposalList | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProposalDetail | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<ProposalList>("/proposals", { view: "decided" }).then(setList, (e) => setError(message(e)));
  }, []);
  useEffect(() => {
    setDetail(null);
    if (picked) api.get<ProposalDetail>(`/proposals/${picked}`).then(setDetail, (e) => setError(message(e)));
  }, [picked]);
  if (error) return <ErrorBox message={error} />;
  if (!list) return null;
  const decided = list.proposals;
  const rows = decided.map((p) => ({
    _key: p.id, Request: p.request || "–", Projects: projectsText(p, list.project_names),
    Verdict: p.verdict_label, "Budget Δ": p.budget_delta_label, Requester: p.requester || "–",
    Submitted: p.submitted_label, Status: p.status_label, "Decided by": p.decided_by_label || "–", Comment: p.comment || "–",
  }));
  const state = detail?.revert;
  const [label, icon] = ACTIONS.revert;
  const revert = async () => {
    try {
      done(await api.post<{ kind: string; message: string }>(`/proposals/${picked}/revert`));
    } catch (e) {
      done({ kind: "error", message: message(e) });
    }
  };
  return (
    <>
      <h2>Decided proposals</h2>
      {!decided.length ? <Caption>No decided proposals yet.</Caption> : (
        <>
          <DataTable caption="Decided proposals" rows={rows} selected={picked} onSelect={setPicked}
            columns={cols(["Request", "Projects", "Verdict", "Budget Δ", "Requester", "Submitted", "Status", "Decided by", "Comment"])} />
          {!picked && <Caption>Select an approved proposal to take its approval back.</Caption>}
          {state?.existing && <Caption icon="undo">{state.message}</Caption>}
          {state && !state.existing && !state.possible && <Caption>{state.message}</Caption>}
          {state?.possible && (
            <div>
              <button onClick={() => setAsking(true)} title="Proposes the values before this approval as a new proposal. Fields changed since then block the revert; the Admin restore stays the emergency path.">
                <Icon name={icon} />{label} this approval …
              </button>
            </div>
          )}
          {asking && (
            <Confirm title={`${label} this approval`} confirmLabel="Propose the revert" icon={icon} onClose={() => setAsking(false)}
              onConfirm={() => void revert()}
              message={<>Propose the values before proposal <strong>{picked}</strong> as a new proposal? A second PMO approves it
                (four-eyes principle). Fields changed since then block the revert.</>} />
          )}
        </>
      )}
    </>
  );
}

// =========================================================================== mine
function MyProposals({ done }: { done: Done }) {
  const [mine, setMine] = useState<Proposal[] | null>(null);
  const [pick, setPick] = useState("");
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<ProposalList>("/proposals", { view: "mine" }).then((l) => setMine(l.proposals), (e) => setError(message(e)));
  }, []);
  if (error) return <ErrorBox message={error} />;
  if (!mine) return null;
  const open = mine.filter((p) => p.status === "submitted");
  const [label, icon] = ACTIONS.withdraw;
  const withdraw = async () => {
    try {
      done(await api.post<{ kind: string; message: string }>(`/proposals/${pick}/withdraw`, { reason }));
    } catch (e) {
      done({ kind: "error", message: message(e) });
    }
  };
  return (
    <>
      <h2>My proposals</h2>
      {!mine.length ? (
        <Caption>You have not submitted a proposal yet. Proposals are created from a Steering Console report, governed fields in
          the project detail, or (PMO) the Planning page and reverts.</Caption>
      ) : (
        <>
          <DataTable caption="My proposals" columns={cols(["Proposal", "Submitted", "Status", "Due", "Deadline", "Decided by", "Comment", "Request", "Projects"])}
            rows={mine.map((p) => ({
              _key: p.id, Proposal: p.id, Submitted: p.submitted_label, Status: p.status_label, Due: p.due_date || "–",
              Deadline: p.deadline_label, "Decided by": p.decided_by || "–", Comment: p.comment || "–", Request: p.request || "–",
              Projects: (p.projects ?? []).join(", "),
            }))} />
          {open.length > 0 && (
            <Card>
              <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
                <label className="field" style={{ flex: "2 1 240px" }}>Withdraw an open proposal
                  <select value={pick} onChange={(e) => setPick(e.target.value)}>
                    <option value="">Pick a proposal …</option>
                    {open.map((p) => <option key={p.id} value={p.id}>{`${p.id} · ${p.request || "–"}`.slice(0, 80)}</option>)}
                  </select>
                </label>
                {pick && (
                  <label className="field" style={{ flex: "2 1 200px" }}>Reason (optional)
                    <input value={reason} onChange={(e) => setReason(e.target.value)} />
                  </label>
                )}
                <button disabled={!pick} onClick={() => setAsking(true)}><Icon name={icon} />{label} …</button>
              </div>
            </Card>
          )}
          {asking && pick && (
            <Confirm title={`${label} ${pick}`} confirmLabel={`Withdraw ${pick}`} icon={icon} onClose={() => setAsking(false)}
              onConfirm={() => void withdraw()}
              message={<>Withdraw proposal <strong>{pick}</strong>? The PMO no longer sees it; nothing is written. Submit it again
                from the report if needed.</>} />
          )}
        </>
      )}
    </>
  );
}

// =========================================================================== delegations
const plusDays = (day: string, days: number) => {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};

function DelegationsView({ done }: { done: Done }) {
  const [data, setData] = useState<Delegations | null>(null);
  const [deputy, setDeputy] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [reason, setReason] = useState("");
  const [revokePick, setRevokePick] = useState("");
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<Delegations>("/delegations").then((d) => {
      setData(d);
      setDeputy(d.deputy_candidates[0] ?? "");
      setStart(d.today);
      setEnd(plusDays(d.today, 6));
      setRevokePick(d.delegations.find((x) => x.revocable)?.delegation_id ?? "");
    }, (e) => setError(message(e)));
  }, []);
  if (error) return <ErrorBox message={error} />;
  if (!data) return null;
  const rules = data.rules;
  const grant = async () => {
    try {
      done(await api.post<{ kind: string; message: string }>("/delegations", { deputy: deputy.trim(), start, end: end || null, reason: reason.trim() }));
    } catch (e) {
      const text = message(e);
      done({ kind: "error", message: text.startsWith("Delegation") ? text : `Delegation not saved: ${text}` });
    }
  };
  const revoke = async () => {
    try {
      done(await api.post<{ kind: string; message: string }>(`/delegations/${revokePick}/revoke`));
    } catch (e) {
      done({ kind: "error", message: message(e) });
    }
  };
  const open = data.delegations.filter((d) => d.revocable);
  const chosen = open.find((d) => d.delegation_id === revokePick);
  return (
    <>
      <h2>Delegation of the approval right</h2>
      <div className="muted small">
        Another PMO decides proposals on your behalf for at most {rules.delegation_max_days} days (only PMO accounts can deputise – a
        deputy sees the whole inbox). The four-eyes principle stays: a deputy never decides their own proposal; yours they decide in
        their own name. Open proposals are escalated after {rules.approval_sla_days} days and expire
        after {rules.approval_expiry_days || "no"} days (Rules page, set by the PMO Head).
      </div>
      <Card>
        <form className="dk-form-grid" onSubmit={(e) => { e.preventDefault(); void grant(); }}>
          {data.deputy_candidates.length ? (
            <label className="field">Deputy
              <select value={deputy} onChange={(e) => setDeputy(e.target.value)}>
                {data.deputy_candidates.map((c) => <option key={c}>{c}</option>)}
              </select>
            </label>
          ) : (
            <label className="field">Deputy (user id)<input value={deputy} onChange={(e) => setDeputy(e.target.value)} /></label>
          )}
          <label className="field">Period – from<input type="date" min={data.today} value={start} onChange={(e) => setStart(e.target.value)} /></label>
          <label className="field">Period – to<input type="date" min={data.today} value={end} onChange={(e) => setEnd(e.target.value)} /></label>
          <label className="field">Reason (required)<input placeholder="e.g. vacation" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <div><button className="primary" type="submit"><Icon name="badge" />Delegate</button></div>
        </form>
      </Card>
      {!data.delegations.length ? <Caption>No delegations yet.</Caption> : (
        <DataTable caption="Delegations" columns={cols(["Delegator", "Deputy", "From", "To", "Status", "Reason"])}
          rows={data.delegations.map((d) => ({
            _key: d.delegation_id, Delegator: d.delegator || "–", Deputy: d.deputy || "–", From: d.start || "–", To: d.end || "–",
            Status: d.status_label, Reason: d.reason || "–",
          }))} />
      )}
      {open.length > 0 && (
        <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
          <label className="field" style={{ flex: "1 1 260px" }}>End a delegation early
            <select value={revokePick} onChange={(e) => setRevokePick(e.target.value)}>
              {open.map((d) => <option key={d.delegation_id} value={d.delegation_id}>{`${d.delegator} → ${d.deputy} (${d.start} – ${d.end})`}</option>)}
            </select>
          </label>
          <button onClick={() => setAsking(true)}><Icon name="event_busy" />End delegation …</button>
        </div>
      )}
      {asking && chosen && (
        <Confirm title="End delegation" confirmLabel="End delegation" icon="event_busy" onClose={() => setAsking(false)} onConfirm={() => void revoke()}
          message={`End the delegation ${chosen.delegator} → ${chosen.deputy} now? The deputy can no longer decide on your behalf.`} />
      )}
    </>
  );
}
