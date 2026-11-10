"""Project detail editing: RBAC (PMO read-only, owner PM editable), governed budgets and validation."""
from __future__ import annotations

from app.core.required_fields import DEFAULT_REQUIRED_FIELDS, save_required_fields
from app.core.security import ROLE_ADMIN, ROLE_PM, ROLE_PMO
from app.data_adapters.project_edit import access_level, diff_and_validate, field_editable, missing_required


def _project(dataset, pid="P-1001") -> dict:
    return dataset.project_view().set_index("project_number").loc[pid].to_dict() | {"project_number": pid}


class TestAccess:
    def test_pmo_is_read_only(self, dataset):
        project = _project(dataset)
        assert access_level(ROLE_PMO, "PMO-ADMIN", project) == "read"
        changes, errors = diff_and_validate(project, {"phase": "Closing"}, ROLE_PMO, "PMO-ADMIN")
        assert not changes and "Read-only" in errors[0]

    def test_owning_pm_can_edit(self, dataset):
        project = _project(dataset)  # owned by Priya Raman
        assert access_level(ROLE_PM, "priya raman", project) == "edit"
        changes, errors = diff_and_validate(project, {"phase": "Closing", "percent_complete": 60}, ROLE_PM, "Priya Raman")
        assert not errors and {c["field"] for c in changes} == {"phase", "percent_complete"}
        assert all(c["table"] == "Projects" and c["project_id"] == "P-1001" for c in changes)

    def test_other_pm_and_admin_cannot_edit(self, dataset):
        project = _project(dataset)
        assert access_level(ROLE_PM, "Rowan Ellis", project) == "read"
        assert access_level(ROLE_ADMIN, "SYSTEM-ADMIN", project) == "none"
        assert diff_and_validate(project, {"phase": "x"}, ROLE_PM, "Rowan Ellis")[1]


class TestValidation:
    def test_unchanged_values_produce_no_changes(self, dataset):
        project = _project(dataset)
        changes, errors = diff_and_validate(project, {"project_name": "Alpha Platform", "percent_complete": 50.0}, ROLE_PM, "Priya Raman")
        assert changes == [] and errors == []

    def test_governed_budget_only_editable_while_empty(self, dataset, dataset_factory, project_factory):
        project = _project(dataset)
        assert not field_editable("budget_cost_eur", project, ROLE_PM, "Priya Raman")
        _, errors = diff_and_validate(project, {"budget_cost_eur": 1}, ROLE_PM, "Priya Raman")
        assert "Steering Console" in errors[0]
        empty = _project(dataset_factory([project_factory(budget_cost_eur=None)]))
        assert field_editable("budget_cost_eur", empty, ROLE_PM, "Priya Raman")
        changes, errors = diff_and_validate(empty, {"budget_cost_eur": 420000}, ROLE_PM, "Priya Raman")
        assert not errors and changes[0]["new"] == 420000.0

    def test_required_field_cannot_be_cleared(self, dataset):
        _, errors = diff_and_validate(_project(dataset), {"project_sponsor": ""}, ROLE_PM, "Priya Raman")
        assert "required field" in errors[0]

    def test_not_required_field_can_be_cleared(self, dataset):
        save_required_fields([f for f in DEFAULT_REQUIRED_FIELDS if f != "project_sponsor"])
        changes, errors = diff_and_validate(_project(dataset), {"project_sponsor": ""}, ROLE_PM, "Priya Raman")
        assert not errors and changes[0]["new"] is None

    def test_range_and_option_checks(self, dataset):
        project = _project(dataset)
        _, errors = diff_and_validate(project, {"percent_complete": 120, "status": "Purple", "risk_score": 9,
                                                "actual_cost_eur": -5}, ROLE_PM, "Priya Raman")
        assert len(errors) == 4

    def test_missing_required_fields_are_listed(self, dataset):
        assert "project_sponsor" in missing_required(_project(dataset, "P-1003"))
        assert missing_required(_project(dataset, "P-1001")) == []
