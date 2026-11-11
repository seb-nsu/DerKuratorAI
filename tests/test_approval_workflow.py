"""Separation of duties (PM proposes, PMO approves), approval inbox, SteerCo Pack,
FinOps visibility. Unit level here; the end-to-end UI flow is in test_admin_console.TestFourEyesWorkflow."""
from __future__ import annotations

import pytest

from app.core import logs, proposals
from app.core.proposals import APPROVED, REJECTED, SUBMITTED, ProposalError
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO, approval_mode, can_approve
from app.views.steerco_pack import (
    extract_conditions,
    key_metrics,
    metric_texts,
    pack_data,
    render_pack_html,
    verdict_key,
)

BUDGET_CUT = [{"project_id": "P-1", "table": "Projects", "field": "budget_cost_eur", "old": 800000, "new": 720000}]
SCHEDULE = [{"project_id": "P-1", "table": "Projects", "field": "planned_end_date", "old": "2030-06-30", "new": "2030-09-30"}]
STOP = [{"project_id": "P-1", "table": "Projects", "field": "state", "old": "Open", "new": "Canceled"}]
FTE_ONLY = [{"project_id": "P-1", "table": "Resource_Plans", "field": "allocated_fte", "old": 4, "new": 3}]


class TestSeparationOfDuties:
    def test_only_the_pmo_approves(self):
        assert can_approve(ROLE_PMO) and not can_approve(ROLE_PM) and not can_approve(ROLE_ADMIN)

    @pytest.mark.parametrize("changes", [BUDGET_CUT, STOP, SCHEDULE, BUDGET_CUT + SCHEDULE])  # schedule changes are governed too
    def test_pm_must_propose_governed_changes(self, changes):
        assert approval_mode(ROLE_PM, changes) == "propose"

    def test_pm_may_apply_ungoverned_changes(self):
        assert approval_mode(ROLE_PM, FTE_ONLY) == "apply"

    def test_pmo_proposes_governed_changes_and_admin_never_writes(self):  # four-eyes for the PMO too
        assert approval_mode(ROLE_PMO, BUDGET_CUT) == "propose"
        assert approval_mode(ROLE_PMO, FTE_ONLY) == "apply"
        assert approval_mode(ROLE_ADMIN, SCHEDULE) == "none"


class TestProposalStore:
    def _submit(self, requester="Priya Raman"):
        return proposals.submit_proposal(requester, ROLE_PM, "Cut CapEx", "**Recommendation:** GO", "GO", BUDGET_CUT)

    def test_submit_and_list(self):
        entry = self._submit()
        assert entry["status"] == SUBMITTED and entry["projects"] == ["P-1"]
        assert [p["id"] for p in proposals.list_proposals(status=SUBMITTED)] == [entry["id"]]
        assert proposals.list_proposals(requester="Someone else") == []

    def test_pmo_approval_is_recorded(self):
        entry = self._submit()
        proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO, snapshot_id="abc123")
        stored = proposals.get_proposal(entry["id"])
        assert stored["status"] == APPROVED and stored["decided_by"] == "PMO-ADMIN" and stored["snapshot_id"] == "abc123"
        assert proposals.list_proposals(status=SUBMITTED) == []

    def test_requester_can_never_approve_own_proposal(self):
        entry = self._submit(requester="PMO-ADMIN")
        with pytest.raises(ProposalError, match="Four-eyes"):
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)

    def test_pm_cannot_decide(self):
        entry = self._submit()
        with pytest.raises(ProposalError, match="Only the PMO"):
            proposals.decide(entry["id"], APPROVED, "Rowan Ellis", ROLE_PM)

    def test_rejection_needs_a_reason_and_decisions_are_final(self):
        entry = self._submit()
        with pytest.raises(ProposalError, match="reason"):
            proposals.decide(entry["id"], REJECTED, "PMO-ADMIN", ROLE_PMO)
        proposals.decide(entry["id"], REJECTED, "PMO-ADMIN", ROLE_PMO, comment="Budget freeze Q4")
        with pytest.raises(ProposalError, match="already rejected"):
            proposals.decide(entry["id"], APPROVED, "PMO-2", ROLE_PMO)
        assert proposals.get_proposal(entry["id"])["comment"] == "Budget freeze Q4"

    def test_no_go_needs_override_and_reason(self):
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "Cut", "paper", "NO-GO", BUDGET_CUT)
        with pytest.raises(ProposalError, match="override"):
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)
        with pytest.raises(ProposalError, match="override"):  # override without a reason
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO, override=True)
        with pytest.raises(ProposalError, match="override"):  # checked before any write, too
            proposals.validate_decision(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)
        proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO, comment="Board decision", override=True)
        stored = proposals.get_proposal(entry["id"])
        assert stored["status"] == APPROVED and stored["override"] is True
        records, _ = logs.read_jsonl(proposals.proposals_path())
        decided = next(r for r in records if r.get("type") == "decided")
        assert decided["override"] is True and decided["verdict"].startswith("NO-GO")

    def test_incomplete_verdict_needs_override_too(self):
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "Cut", "paper", "ANALYSIS INCOMPLETE",
                                          BUDGET_CUT)
        with pytest.raises(ProposalError, match="override"):
            proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO, comment="ok")

    def test_go_needs_no_override(self):
        entry = self._submit()
        result = proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)
        assert result["override"] is False

    def test_requester_can_never_reject_own_proposal(self):
        entry = self._submit(requester="PMO-ADMIN")
        with pytest.raises(ProposalError, match="reject your own"):
            proposals.decide(entry["id"], REJECTED, "PMO-ADMIN", ROLE_PMO, comment="changed my mind")
        assert proposals.get_proposal(entry["id"])["status"] == SUBMITTED

    def test_verdict_record_with_its_rules_is_kept_in_both_events(self):
        from app.crew.verdict import compute_verdict

        record = compute_verdict(["RESULT"], [], []).to_dict()
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "Cut CapEx", "paper", record["label"], BUDGET_CUT,
                                          verdict_record=record)
        proposals.decide(entry["id"], APPROVED, "PMO-ADMIN", ROLE_PMO)
        events, _ = logs.read_jsonl(proposals.proposals_path())
        for event in events:  # submitted and decided
            assert "eac_overrun_nogo_pct" in event["verdict_record"]["rules"]
        assert proposals.get_proposal(entry["id"])["verdict_record"]["label"] == "GO"

    def test_corrupt_lines_are_skipped(self):
        entry = self._submit()
        with proposals.proposals_path().open("a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        assert proposals.get_proposal(entry["id"])["status"] == SUBMITTED


class TestSteerCoPack:
    PAPER = """# Executive Decision Paper
**Request:** x | **Date:** 2026-09-27 | **Recommendation:** CONDITIONAL GO

## 5. Recommendation & Conditions
- **Condition 1:** freeze scope of P-2 (owner: PMO)
- Re-baseline P-1 by 31.10.

## 6. Next Steps
1. Inform the project managers
"""

    def test_transfer_metrics_are_net_zero_with_moved_volume(self):
        changes = [
            {"project_id": "P-1", "field": "budget_cost_eur", "old": 800000, "new": 700000},
            {"project_id": "P-2", "field": "budget_cost_eur", "old": "400000.0", "new": "500000.0"},  # JSON round-trip
            {"project_id": "P-1", "field": "planned_roi_pct", "old": 50.0, "new": 42.5},
            {"project_id": "P-1", "field": "allocated_fte", "old": 4, "new": 3},
            {"project_id": "P-2", "field": "allocated_fte", "old": 2, "new": 3},
        ]
        m = key_metrics(changes)
        assert m["budget_net"] == 0 and m["budget_moved"] == 100000 and m["fte_moved"] == 1
        texts = dict((label, (value, detail)) for label, value, detail in metric_texts(m))
        assert texts["Budget Δ"] == ("€0", "€100,000 moved between projects")
        assert texts["ROI Δ"][0] == "-7.5 pp" and "50.0 % → 42.5 %" in texts["ROI Δ"][1]

    def test_chained_changes_collapse_to_net_effect(self):
        m = key_metrics([{"project_id": "P-1", "field": "budget_cost_eur", "old": 800000, "new": 750000},
                         {"project_id": "P-1", "field": "budget_cost_eur", "old": 750000, "new": 700000}])
        assert m["budget_net"] == -100000

    def test_conditions_and_verdict(self):
        assert extract_conditions(self.PAPER) == ["Condition 1: freeze scope of P-2 (owner: PMO)", "Re-baseline P-1 by 31.10.",
                                                  "Inform the project managers"]
        assert verdict_key("CONDITIONAL GO") == "CONDITIONAL" and verdict_key("NO-GO (guardrail)") == "NO-GO"
        assert verdict_key("ANALYSIS INCOMPLETE") == "INCOMPLETE" and verdict_key("GO") == "GO"

    def test_pack_html_contains_everything_and_escapes_data(self):
        message = {"content": self.PAPER + "\n- Condition: freeze scope of P-2 (owner: PMO)\n",
                   "meta": {"request": "<script>alert(1)</script> Cut", "mode": "offline",
                            "forced": True, "proposed_changes": BUDGET_CUT}}
        page = render_pack_html(pack_data(message, requester="Priya Raman", environment="DEV"))
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
        for expected in ("CONDITIONAL GO", "Budget Δ", "FTE Δ", "ROI Δ", "−€80,000", "freeze scope of P-2",
                         "Forced simulation", "Approved budget (€)", "Four-eyes (PMO)", "€720,000", "Priya Raman", "@page"):
            assert expected in page
        assert "budget_cost_eur" not in page and "mode offline" not in page  # business columns only
