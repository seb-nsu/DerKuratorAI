"""Writes beyond the Projects table: append-only rows (Baselines, Approvals, Reserve_Ledger) and field updates of
existing risks – compare-and-write, RBAC, four-eyes and snapshot restore in both adapters."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core import proposals, snapshots
from app.core.security import ROLE_PM, ROLE_PMO, is_governed_change
from app.crew.tools import context as ctx
from app.data_adapters import (
    DataConflictError,
    ExcelPortfolioAdapter,
    JsonPortfolioAdapter,
    WriteNotAuthorizedError,
)
from app.data_adapters.base import is_append, is_manual_change, is_row_update, net_changes

DATA = {
    "Projects": [{"project_number": "P-1", "project_name": "Alpha", "project_manager": "Priya Raman",
                  "budget_cost_eur": 800000, "state": "Open", "business_case": "Build"},
                 {"project_number": "P-2", "project_name": "Beta", "project_manager": "Rowan Ellis",
                  "budget_cost_eur": 100000, "state": "Open", "business_case": "Build"}],
    "Project_Tasks": [], "Resource_Plans": [], "Stakeholders": [],
    "Risks": [{"risk_number": "RK-1", "project_number": "P-1", "short_description": "Supplier", "probability": "High",
               "impact": "High", "risk_status": "Accept", "mitigation": "none"}],
    "Baselines": [{"baseline_number": "BL-1", "project_number": "P-1", "name": "start", "created_date": "2029-01-01",
                   "version": 1, "baseline_budget_eur": 800000}],
}


def _baseline(number="BL-2", pid="P-1"):
    row = {"baseline_number": number, "project_number": pid, "name": "v2", "created_date": "2029-06-30", "version": 2,
           "baseline_budget_eur": 900000, "reason": "scope", "approved_by": None, "approval_date": None}
    return {"project_id": pid, "table": "Baselines", "op": "append", "key": number, "field": "Baselines", "old": None,
            "new": "Version 2", "row": row, "source_tool": "test"}


def _risk(field, old, new, key="RK-1", pid="P-1"):
    return {"project_id": pid, "table": "Risks", "key": key, "field": field, "old": old, "new": new, "source_tool": "test"}


@pytest.fixture(params=["json", "excel"])
def adapter(request, tmp_path):
    if request.param == "json":
        path = tmp_path / "portfolio.json"
        path.write_text(json.dumps(DATA), encoding="utf-8")
        return JsonPortfolioAdapter(path)
    path = tmp_path / "portfolio.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, rows in DATA.items():
            pd.DataFrame(rows or [{"placeholder": None}]).to_excel(writer, sheet_name=name, index=False)
    return ExcelPortfolioAdapter(path)


def _approve(adapter, changes, requester="Priya Raman", role=ROLE_PM):
    entry = proposals.submit_proposal(requester, role, "test", "", "GO", changes)
    return adapter.apply_changes(entry["changes"], user="PMO-ADMIN", role=ROLE_PMO, proposal_id=entry["id"])


class TestClassification:
    def test_kinds(self):
        assert is_append(_baseline()) and not is_manual_change(_baseline())
        assert is_row_update(_risk("risk_status", "Accept", "Mitigate"))
        assert not is_row_update(_risk("impact", "High", "Low")) and is_manual_change(_risk("impact", "High", "Low"))
        assert is_manual_change({"table": "Stakeholders", "op": "append", "row": {}, "field": "x"})

    def test_new_rows_are_always_four_eyes_risk_updates_are_not(self):
        assert is_governed_change(_baseline())
        assert is_governed_change({"table": "Reserve_Ledger", "op": "append", "row": {}})
        assert not is_governed_change(_risk("risk_status", "Accept", "Mitigate"))

    def test_net_changes_keep_one_change_per_row(self):
        changes = [_risk("risk_status", "Accept", "Mitigate"), _risk("risk_status", "Open", "Avoid", key="RK-2")]
        assert len(net_changes(changes)) == 2


class TestAppend:
    def test_approved_append_lands_with_approver(self, adapter):
        result = _approve(adapter, [_baseline()])
        assert result.applied == 1
        rows = adapter.load_dataset().baselines
        new = rows[rows["baseline_number"] == "BL-2"].iloc[0]
        assert new["baseline_budget_eur"] == 900000 and new["approved_by"] == "PMO-ADMIN"
        assert len(rows) == 2  # the existing row is untouched

    def test_missing_sheet_or_table_is_created(self, adapter):
        row = {"entry_number": "RES-1", "project_number": "P-1", "amount_eur": -50000, "reason": "x"}
        change = {"project_id": "P-1", "table": "Reserve_Ledger", "op": "append", "key": "RES-1",
                  "field": "Reserve_Ledger", "old": None, "new": "draw", "row": row}
        _approve(adapter, [change])
        assert adapter.load_dataset().reserve_ledger["entry_number"].tolist() == ["RES-1"]

    def test_existing_number_is_a_conflict_and_nothing_is_written(self, adapter):
        before = adapter.load_dataset().baselines
        with pytest.raises(DataConflictError):
            _approve(adapter, [_baseline(number="BL-1")])
        assert len(adapter.load_dataset().baselines) == len(before)

    def test_pm_cannot_append_directly(self, adapter):
        with pytest.raises(WriteNotAuthorizedError):
            adapter.apply_changes([_baseline()], user="Priya Raman", role=ROLE_PM)

    def test_row_without_project_is_pmo_only(self, adapter):
        change = {"project_id": "", "table": "Reserve_Ledger", "op": "append", "key": "RES-9", "field": "Reserve_Ledger",
                  "old": None, "new": "top-up", "row": {"entry_number": "RES-9", "amount_eur": 1}}
        with pytest.raises(WriteNotAuthorizedError, match="PMO only"):
            adapter.apply_changes([change], user="Priya Raman", role=ROLE_PM)

    def test_snapshot_restore_removes_the_appended_row(self, adapter):
        result = _approve(adapter, [_baseline()])
        snapshots.restore_snapshot(result.snapshot_id, adapter.path)
        assert adapter.load_dataset().baselines["baseline_number"].tolist() == ["BL-1"]


class TestRiskUpdates:
    def test_owning_pm_updates_own_risk(self, adapter):
        result = adapter.apply_changes([_risk("risk_status", "Accept", "Mitigate"), _risk("probability", "High", "Low")],
                                       user="Priya Raman", role=ROLE_PM)
        assert result.applied == 2
        risk = adapter.load_dataset().risks.iloc[0]
        assert (risk["risk_status"], risk["probability"], risk["impact"]) == ("Mitigate", "Low", "High")

    def test_foreign_pm_is_refused(self, adapter):
        with pytest.raises(WriteNotAuthorizedError):
            adapter.apply_changes([_risk("risk_status", "Accept", "Mitigate", pid="P-1")], user="Rowan Ellis",
                                  role=ROLE_PM)

    def test_changed_meanwhile_is_a_conflict(self, adapter):
        with pytest.raises(DataConflictError):
            adapter.apply_changes([_risk("risk_status", "Avoid", "Mitigate")], user="Priya Raman", role=ROLE_PM)
        assert adapter.load_dataset().risks.iloc[0]["risk_status"] == "Accept"


class TestScenarioLedger:
    def test_row_changes_build_on_each_other(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        ctx.record_row_change("Risks", "P-1001", "RK-1", "t", field="risk_status", new_value="Mitigate")
        ctx.record_row_change("Risks", "P-1001", "RK-1", "t", field="risk_status", new_value="Avoid")
        ledger = ctx.get_ledger()
        assert [(c["old"], c["new"]) for c in ledger] == [("Open", "Mitigate"), ("Mitigate", "Avoid")]
        ctx.record_row_change("Baselines", "P-1001", "BL-9", "t", row={"baseline_number": "BL-9",
                                                                     "project_number": "P-1001"}, summary="v2")
        assert ctx.pending_rows("Baselines") == [{"baseline_number": "BL-9", "project_number": "P-1001"}]
        assert ctx.table_with_pending("Baselines", "P-1001")["baseline_number"].tolist() == ["BL-9"]

    def test_row_changes_survive_the_next_step_without_touching_the_project_view(self, dataset):
        ctx.set_portfolio_context(dataset, {})
        ctx.record_row_change("Baselines", "P-1001", "BL-9", "t", row={"baseline_number": "BL-9"}, summary="v2")
        state = ctx.export_state()
        ctx.set_portfolio_context(dataset, {}, base_state=state)
        assert ctx.pending_rows("Baselines") == [{"baseline_number": "BL-9"}]
        assert "Baselines" not in ctx.get_scenario_df().columns
