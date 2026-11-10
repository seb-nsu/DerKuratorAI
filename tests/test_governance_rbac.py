"""Fact superseding, input errors, missing EAC, server-side RBAC and four-eyes also for the PMO, blocking audit,
snapshot retention."""
from __future__ import annotations

import json

import pytest

from app.core import logs, proposals, snapshots
from app.core.security import PMO_IDENTITIES, ROLE_ADMIN, ROLE_PM, ROLE_PMO, is_governed_change
from app.crew.pipeline import run_steering_pipeline
from app.crew.scenarios import build_guided_scenario
from app.crew.tools import context as ctx
from app.crew.tools import simulator_tools as sim
from app.crew.verdict import compute_verdict
from app.data_adapters import DataSourceError, JsonPortfolioAdapter, WriteNotAuthorizedError
from app.data_adapters.project_edit import split_governed

NAMES = {"P-1001": "Alpha Platform", "P-1002": "Beta Billing", "P-1003": "Gamma Governance"}


@pytest.fixture
def no_graph(monkeypatch):
    from app.crew.tools import graph_tools

    def unavailable():
        raise graph_tools.GraphUnavailable("Graph disabled in test")

    monkeypatch.setattr(graph_tools, "get_graph", unavailable)


def _budget(pid, pct, direction):
    return build_guided_scenario("budget_change", pid, NAMES, value=pct, is_perc=True, direction=direction,
                                 cost_type="total")


# =========================================================================== fact superseding
class TestFactSuperseding:
    def test_corrective_step_clears_an_earlier_no_go(self, dataset, no_graph):
        cut = _budget("P-1002", 20, "decrease")  # 800k -> 640k, EAC 800k -> 25 % overrun -> NO-GO
        step1 = run_steering_pipeline(cut.request, dataset, ROLE_PMO, "PMO", plan=cut.plan)
        assert step1.verdict.decision == "NO-GO"
        restore = _budget("P-1002", 25, "increase")  # 640k -> 800k: the forecast is covered again
        step2 = run_steering_pipeline(restore.request, dataset, ROLE_PMO, "PMO", plan=restore.plan,
                                      base_state=step1.scenario_state)
        assert step2.verdict.decision != "NO-GO"
        assert not any("forecast" in r for r in step2.verdict.reasons)
        facts = step2.scenario_state["facts"]
        assert not [f for f in facts if f["type"] == "budget_overrun"]
        change = [f for f in facts if f["type"] == "budget_change" and f["project"] == "P-1002"]
        assert len(change) == 1 and change[0]["delta"] == 0  # accumulated over both steps

    def test_same_fact_identity_is_replaced_not_appended(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        ctx.record_fact("domino", project="P-1", affected=5)
        ctx.record_fact("domino", project="P-1", affected=2)
        ctx.record_fact("deadline_breach", project="P-1", successor="P-2", days_late=3)
        ctx.record_fact("deadline_breach", project="P-1", successor="P-3", days_late=4)
        facts = ctx.get_facts()
        assert [f["affected"] for f in facts if f["type"] == "domino"] == [2]
        assert len([f for f in facts if f["type"] == "deadline_breach"]) == 2  # one per successor

    def test_resimulated_field_drops_carried_facts_of_that_project_only(self, dataset):
        base = {"changes": [], "results": [], "turns": ["step 1"], "facts": [
            {"type": "deadline_breach", "project": "P-1001", "successor": "P-9", "new": True},
            {"type": "deadline_breach", "project": "P-1002", "successor": "P-9", "new": True}]}
        ctx.set_portfolio_context(dataset, {}, base_state=base)
        ctx.record_change("P-1001", "planned_end_date", "2030-01-31", "test")
        assert [f["project"] for f in ctx.get_facts()] == ["P-1002"]
        assert all("carried_over" not in f for f in ctx.export_state()["facts"])


# =========================================================================== verdict inputs
class TestVerdictInputs:
    def test_unknown_project_is_incomplete_not_a_guardrail_no_go(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        text = sim.budget_change("P-9999", 1000)
        verdict = compute_verdict([text], [], [])
        assert verdict.decision == "ANALYSIS INCOMPLETE" and verdict.rule == "input not understood"

    def test_parser_error_is_incomplete(self):
        verdict = compute_verdict(["INPUT ERROR: No amount or percentage recognised for the budget change."], [], [])
        assert verdict.decision == "ANALYSIS INCOMPLETE"

    def test_real_guardrail_stays_no_go(self):
        assert compute_verdict(["ERROR: Budget undercut – x"], [], []).label == "NO-GO"

    def test_missing_eac_is_never_a_silent_pass(self, dataset_factory, project_factory):
        ds = dataset_factory([project_factory(estimate_at_completion_eur=None)])
        ctx.set_portfolio_context(ds, {})
        text = sim.budget_change("P-1001", -10000)
        assert "No estimate at completion" in text
        assert any(f["type"] == "eac_missing" for f in ctx.get_facts())
        verdict = compute_verdict([text], [], ctx.get_facts())
        assert verdict.decision == "CONDITIONAL GO"
        assert any("EAC" in r for r in verdict.reasons)
        assert not any(r.startswith("Tool warning") and "estimate at completion" in r for r in verdict.reasons)


# =========================================================================== backend RBAC
@pytest.fixture
def source(tmp_path):
    path = tmp_path / "portfolio.json"
    data = {"Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                          "budget_cost_eur": 800000, "planned_end_date": "2030-06-30", "state": "Open", "business_case": "Build"},
                         {"project_number": "P-2", "project_name": "Beta", "project_manager": "Rowan Ellis",
                          "budget_cost_eur": 100000, "planned_end_date": "2030-06-30", "state": "Open", "business_case": "Build"}],
            "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [], "Risks": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _c(field, old, new, pid="P-1"):
    return {"project_id": pid, "table": "Projects", "field": field, "old": old, "new": new, "source_tool": "test"}


class TestBackendRBAC:
    @pytest.mark.parametrize("field", ["planned_end_date", "estimate_at_completion_eur", "actual_cost_eur", "state",
                                       "planned_benefit_eur"])
    def test_audit_fields_are_governed(self, field):
        assert is_governed_change({"table": "Projects", "field": field})

    @pytest.mark.parametrize("field", ["business_case", "project_name", "project_sponsor", "status",
                                       "percent_complete", "risk_score"])
    def test_plain_master_data_is_not_governed(self, field):  # PMs keep direct access to these
        assert not is_governed_change({"table": "Projects", "field": field})

    def test_phase_is_governed(self):  # a phase change passes a stage gate – four-eyes like budgets and dates
        assert is_governed_change({"table": "Projects", "field": "phase"})

    def test_pm_writes_own_ungoverned_fields(self, source):
        result = JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "Test")], user="Priya Raman", role=ROLE_PM)
        assert result.applied == 1

    @pytest.mark.parametrize("change", [_c("state", "Open", "Canceled"), _c("planned_end_date", "2030-06-30", "2031-01-31")])
    def test_pm_can_never_write_governed_fields(self, source, change):
        with pytest.raises(WriteNotAuthorizedError, match="Governed fields"):
            JsonPortfolioAdapter(source).apply_changes([change], user="Priya Raman", role=ROLE_PM)

    def test_pm_cannot_write_foreign_projects(self, source):
        with pytest.raises(WriteNotAuthorizedError, match="P-2"):
            JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "x", pid="P-2")], user="Priya Raman", role=ROLE_PM)

    def test_admin_never_writes(self, source):
        with pytest.raises(WriteNotAuthorizedError):
            JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "x")], user="SYSTEM-ADMIN", role=ROLE_ADMIN)

    def test_pmo_governed_write_without_proposal_is_refused(self, source):
        before = source.read_text(encoding="utf-8")
        with pytest.raises(WriteNotAuthorizedError, match="approved proposal"):
            JsonPortfolioAdapter(source).apply_changes([_c("budget_cost_eur", 800000, 1)], user="PMO-ADMIN", role=ROLE_PMO)
        assert source.read_text(encoding="utf-8") == before

    def test_pmo_cannot_approve_own_proposal_but_deputy_can(self, source):
        changes = [_c("budget_cost_eur", 800000, 700000)]
        entry = proposals.submit_proposal("PMO-ADMIN", ROLE_PMO, "cut", "", "GO", changes)
        adapter = JsonPortfolioAdapter(source)
        with pytest.raises(WriteNotAuthorizedError, match="Four-eyes"):
            adapter.apply_changes(changes, user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"])
        assert "PMO-DEPUTY" in PMO_IDENTITIES.values()
        assert adapter.apply_changes(changes, user="PMO-DEPUTY", role=ROLE_PMO, proposal_id=entry["id"]).applied == 1

    def test_change_set_must_match_the_proposal(self, source):
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO", [_c("budget_cost_eur", 800000, 700000)])
        with pytest.raises(WriteNotAuthorizedError, match="differs"):
            JsonPortfolioAdapter(source).apply_changes([_c("budget_cost_eur", 800000, 1)], user="PMO-ADMIN",
                                                       role=ROLE_PMO, proposal_id=entry["id"])

    def test_decided_proposal_cannot_be_replayed(self, source):
        changes = [_c("budget_cost_eur", 800000, 700000)]
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO", changes)
        proposals.decide(entry["id"], proposals.REJECTED, "PMO-ADMIN", ROLE_PMO, comment="no")
        with pytest.raises(WriteNotAuthorizedError, match="already rejected"):
            JsonPortfolioAdapter(source).apply_changes(changes, user="PMO-DEPUTY", role=ROLE_PMO, proposal_id=entry["id"])

    def test_master_data_edit_routes_governed_fields_to_a_proposal(self):
        direct, governed = split_governed([_c("business_case", "a", "b"), _c("state", "Open", "On Hold"),
                                           _c("estimate_at_completion_eur", 1, 2)])
        assert [c["field"] for c in direct] == ["business_case"]
        assert [c["field"] for c in governed] == ["state", "estimate_at_completion_eur"]


# =========================================================================== blocking audit
class TestBlockingAudit:
    def test_failed_audit_write_aborts_the_data_write(self, source, monkeypatch, tmp_path):
        monkeypatch.setattr(logs, "AUDIT_LOG", tmp_path)  # a directory -> OSError
        before = source.read_text(encoding="utf-8")
        with pytest.raises(DataSourceError, match="Audit trail could not be written"):
            JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "Test")], user="Priya Raman", role=ROLE_PM)
        assert source.read_text(encoding="utf-8") == before

    def test_every_write_leaves_an_audit_record(self, source):
        JsonPortfolioAdapter(source).apply_changes([_c("business_case", "Build", "Test")], user="Priya Raman", role=ROLE_PM)
        records, _ = logs.read_jsonl(logs.AUDIT_LOG)
        write = [r for r in records if r["event"] == "data_write"][-1]
        assert write["user_id"] == "Priya Raman" and write["role"] == ROLE_PM and write["snapshot_id"]

    def test_app_log_is_archived_before_it_is_cleared(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("SECURITY | User login switched\n", encoding="utf-8")
        archived = logs.clear_app_log(log, archive_dir=tmp_path / "archive")
        assert log.read_text(encoding="utf-8") == ""
        assert "SECURITY | User login switched" in archived.read_text(encoding="utf-8")


# =========================================================================== snapshot retention
class TestSnapshotRetention:
    def test_restoring_the_oldest_snapshot_at_the_retention_limit(self, source, monkeypatch):
        monkeypatch.setattr(snapshots, "MAX_SNAPSHOTS", 3)
        monkeypatch.setattr(snapshots.prune, "__defaults__", (3,))
        oldest = snapshots.create_snapshot(source, "first")
        for i in range(2):
            snapshots.create_snapshot(source, f"n{i}")
        restored = snapshots.restore_snapshot(oldest["id"], source)  # used to raise FileNotFoundError
        assert restored["restored"]["id"] == oldest["id"]

    def test_snapshots_referenced_by_a_proposal_are_never_pruned(self, source):
        evidence = snapshots.create_snapshot(source, "approve")
        entry = proposals.submit_proposal("Priya Raman", ROLE_PM, "cut", "", "GO", [_c("budget_cost_eur", 800000, 1)])
        proposals.decide(entry["id"], proposals.APPROVED, "PMO-ADMIN", ROLE_PMO, snapshot_id=evidence["id"])
        for i in range(3):
            snapshots.create_snapshot(source, f"n{i}")
        snapshots.prune(keep=1)
        ids = {s["id"] for s in snapshots.list_snapshots()}
        assert evidence["id"] in ids and len(ids) == 2

    def test_snapshots_referenced_by_a_direct_apply_audit_are_never_pruned(self, source):
        from app.core import logs

        evidence = snapshots.create_snapshot(source, "approve_apply")
        logs.append_jsonl(logs.AUDIT_LOG, {"event": "scenario_applied", "user_id": "PMO-ADMIN",
                                           "snapshot_id": evidence["id"]})
        for i in range(3):
            snapshots.create_snapshot(source, f"n{i}")
        snapshots.prune(keep=1)
        ids = {s["id"] for s in snapshots.list_snapshots()}
        assert evidence["id"] in ids and len(ids) == 2
