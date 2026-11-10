"""Approvals page logic shared by every front end: the views of the page (pending, decided, mine), deadline columns,
the decision hints (own proposal, acting for a delegator), withdraw, revert and the delegation of the approval right.
The rules live in app.core.proposals; this module applies them the way the page does and writes the same audit
entries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.approval_flow import audit
from app.core import clock
from app.core.proposals import (
    APPROVED,
    PENDING_APPLY,
    PLAN,
    SUBMITTED,
    ProposalError,
    active_delegation,
    grant_delegation,
    list_proposals,
    requires_override,
    revert_of,
    revoke_delegation,
    sla_state,
    submit_revert,
    withdraw_proposal,
)
from app.core.security import ROLE_PMO, can_approve, log_security_event

# Proposal states as business words (tables, badges, history rail)
STATUS_LABELS = {"submitted": "Waiting for approval", "approved": "Approved", "rejected": "Rejected",
                 "stale": "Outdated", "pending_apply": "Being applied", "withdrawn": "Withdrawn", "expired": "Expired"}
SLA_LABELS = {"on time": "On time", "overdue": "Overdue · escalated", "overdue x2": "Overdue · escalated twice",
              "expires": "Expired", "closed": "–", "unknown": "–"}


@dataclass(frozen=True)
class Result:
    """Outcome of an action of the page. `kind`: success | error; `entry`: the stored record (if any)."""
    kind: str
    message: str
    entry: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "success"


# =========================================================================== lists and columns
def pmo_inbox(pending: list[dict]) -> list[dict]:
    """The annual plan waits for the Portfolio Board, not in the PMO inbox."""
    return [p for p in pending if p.get("kind") != PLAN]


def sort_by_urgency(pending: list[dict]) -> list[dict]:
    """Escalated proposals first (highest level, then oldest), then by submission time."""
    return sorted(pending, key=lambda p: (-int(p.get("escalation_level") or 0), str(p.get("timestamp", ""))))


def queues(user_id: str, role: str) -> dict[str, list[dict]]:
    """The views of the Approvals page: the PMO inbox (most urgent first), the decided proposals and the caller's own.
    Who is no approver sees only their own proposals."""
    mine = list_proposals(requester=user_id)
    if not can_approve(role):
        return {"mine": mine}
    return {"pending": sort_by_urgency(pmo_inbox(list_proposals(status=SUBMITTED))),
            "decided": [p for p in list_proposals() if p["status"] not in (SUBMITTED, PENDING_APPLY)],
            "mine": mine}


def sla_columns(proposal: dict) -> dict:
    """Due date and deadline state for the inbox and 'My proposals' tables."""
    state = sla_state(proposal)
    due = state.get("due")
    return {"Due": due.strftime("%Y-%m-%d") if due is not None and proposal["status"] == SUBMITTED else "–",
            "Deadline": SLA_LABELS.get(state["state"], state["state"])}


def budget_delta(proposal: dict) -> float:
    """Net budget change of a proposal's change set (0 without a budget change)."""
    total = 0.0
    for c in proposal.get("changes") or []:
        if c.get("field") == "budget_cost_eur":
            try:
                total += float(c.get("new") or 0) - float(c.get("old") or 0)
            except (TypeError, ValueError):
                continue
    return total


def shown_cells(proposal: dict) -> dict[str, str]:
    """The formatted Verdict, Budget Δ and Submitted cells of the Approvals tables – one place for every front end."""
    from app.core.formatting import eur, status_text, timestamp

    total = budget_delta(proposal)
    return {"verdict_label": status_text(proposal.get("verdict") or "No verdict"),
            "budget_delta_label": eur(total, signed=True) if total else "–",
            "submitted_label": timestamp(proposal.get("timestamp"))}


def status_label(proposal: dict, with_override: bool = False) -> str:
    label = STATUS_LABELS.get(proposal["status"], str(proposal["status"]))
    return label + (" with override" if with_override and proposal.get("override") else "")


def decided_by_text(proposal: dict) -> str:
    """Who decided – a deputy with the delegator they acted for."""
    by = proposal.get("decided_by") or "–"
    return f"{by} (deputy of {proposal['on_behalf_of']})" if proposal.get("on_behalf_of") else by


# =========================================================================== one proposal
def _same_user(a: Any, b: Any) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def deputy_info(user_id: str, role: str) -> dict | None:
    """The delegation under which a PMO deputises today (the page's banner), or None."""
    return active_delegation(user_id) if can_approve(role) else None


def decision_hints(proposal: dict, user_id: str, role: str) -> dict[str, Any]:
    """What the decision bar shows: `own` (four-eyes: never decided by its requester), `blocked`, `proxy_own` (a
    proposal of the delegator the caller deputises for – decided in the caller's own name), `acting_for` (the
    delegator a decision is recorded for), `needs_override` (NO-GO / incomplete) and `manual` (only hand-over
    changes: the approval records a hand-over instead of a write)."""
    from app.data_adapters.base import is_manual_change

    own = _same_user(proposal.get("requester"), user_id)
    approver = can_approve(role)
    delegation = active_delegation(user_id) if approver else None
    proxy_own = bool(delegation) and str(delegation["delegator"]).casefold() == str(proposal.get("requester")).casefold()
    return {"own": own, "approver": approver, "blocked": own or not approver, "proxy_own": proxy_own,
            "delegator": delegation["delegator"] if delegation else None,
            "acting_for": delegation["delegator"] if delegation and not proxy_own else None,
            "needs_override": requires_override(proposal.get("verdict")),
            "manual": all(is_manual_change(c) for c in proposal.get("changes") or [])}


def revert_state(proposal: dict) -> dict[str, Any]:
    """Whether an approval can be taken back: only an applied proposal (with a snapshot), and only once."""
    pid = proposal["id"]
    if proposal.get("status") != APPROVED or not proposal.get("snapshot_id"):
        return {"possible": False, "existing": None,
                "message": f"Proposal {pid} wrote no data – there is nothing to revert."}
    existing = revert_of(pid)
    if existing:
        return {"possible": False, "existing": {"id": existing["id"], "status": existing["status"]},
                "message": f"Proposal {pid} is being reverted by proposal {existing['id']} ({existing['status']})."}
    return {"possible": True, "existing": None, "message": None}


# =========================================================================== actions
def withdraw(proposal_id: str, user_id: str, role: str, reason: str) -> Result:
    """The requester takes back an open proposal; nothing is written."""
    try:
        withdraw_proposal(proposal_id, user_id, role, reason)
    except ProposalError as exc:
        return Result("error", str(exc))
    audit("proposal_withdrawn", role=role, user_id=user_id, proposal_id=proposal_id, comment=reason)
    return Result("success", f"Proposal {proposal_id} withdrawn – nothing was written.")


def revert(proposal_id: str, user_id: str, role: str, provider: Any) -> Result:
    """Proposes the values before an approval as a new proposal (four-eyes again)."""
    try:
        entry = submit_revert(proposal_id, user_id, role, provider)
    except ProposalError as exc:
        return Result("error", f"Revert not proposed: {exc}")
    audit("proposal_submitted", role=role, user_id=user_id, proposal_id=entry["id"], request=entry["request"],
          changes=entry["changes"], reverts=proposal_id)
    log_security_event("Revert of an approval proposed", role=role, user=user_id, proposal=entry["id"],
                       reverts=proposal_id)
    return Result("success", f"Revert proposed as proposal {entry['id']} – a second PMO approves it "
                             "(four-eyes principle).", entry)


def grant(user_id: str, role: str, deputy: str, start: date | None, end: date | None, reason: str) -> Result:
    """Hands the approval right to a deputy for a period (a one-day period when `end` is empty)."""
    end = end or start
    try:
        entry = grant_delegation(user_id, role, deputy or "", start, end, reason)
    except (ProposalError, TypeError) as exc:
        return Result("error", f"Delegation not saved: {exc}")
    audit("approval_delegated", role=role, user_id=user_id, delegate=entry["delegate"], start=entry["start"],
          end=entry["end"], delegation_id=entry["delegation_id"])
    return Result("success", f"{entry['delegate']} decides proposals on your behalf from "
                             f"{entry['start']} to {entry['end']}.", entry)


def revoke(delegation_id: str, user_id: str, role: str) -> Result:
    try:
        revoke_delegation(delegation_id, user_id, role)
    except ProposalError as exc:
        return Result("error", str(exc))
    audit("approval_delegation_revoked", role=role, user_id=user_id, delegation_id=delegation_id)
    return Result("success", "Delegation ended.")


# =========================================================================== delegations
def delegation_status(delegation: dict, today: date | None = None) -> str:
    day = (today or clock.today()).isoformat()
    if delegation["status"] == "revoked":
        return "Revoked"
    if delegation["start"] <= day <= delegation["end"]:
        return "Active"
    return "Planned" if delegation["start"] > day else "Ended"


def open_delegations(items: list[dict], today: date | None = None) -> list[dict]:
    """Delegations that can still be ended early (active, not yet over)."""
    day = (today or clock.today()).isoformat()
    return [d for d in items if d["status"] == "active" and d["end"] >= day]


def deputy_candidates(user_id: str) -> list[str]:
    """The other PMO accounts (only a PMO can deputise); empty without a user file – then the deputy is typed in."""
    try:
        from app.core.auth import load_users

        users = load_users().values()
    except Exception:  # noqa: BLE001 - without a user file the deputy is typed in
        return []
    return sorted(u.user_id for u in users if u.role == ROLE_PMO
                  and u.user_id.casefold() != str(user_id).casefold())
