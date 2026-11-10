"""Deterministic data quality rules of the Data Steward (mock datasets)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.core import required_fields
from app.core.required_fields import DEFAULT_REQUIRED_FIELDS, LOCKED_FIELDS, load_required_fields, save_required_fields
from app.crew.tools.data_steward_tools import run_all_checks
from app.data_adapters.base import ProjectRecord

# pandas 3 knows several "missing" representations – all must be treated the same
MISSING_MARKERS = [
    pytest.param(None, id="None"),
    pytest.param(np.nan, id="np.nan"),
    pytest.param(pd.NA, id="pd.NA"),
    pytest.param("", id="leerstring"),
    pytest.param("   ", id="whitespace"),
]


def _issues(report, project_id: str, severity: str | None = None):
    return [i for i in report.issues if i.project_id == project_id and (severity is None or i.severity == severity)]


class TestMandatoryFields:
    def test_missing_mandatory_field_is_reported_as_error(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(budget_cost_eur=None)]))
        errors = _issues(report, "P-1001", "ERROR")
        assert any(i.column == "budget_cost_eur" and i.rule == "Completeness" for i in errors)
        assert report.blocked_project_ids == ["P-1001"]

    def test_empty_string_counts_as_missing(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(project_manager="   ")]))
        assert any(i.column == "project_manager" and i.severity == "ERROR" for i in report.issues)

    def test_missing_governance_field_is_reported_as_error(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(project_sponsor="")]))
        assert any(i.column == "project_sponsor" and i.rule == "Governance Completeness" and i.severity == "ERROR"
                   for i in report.issues)

    def test_missing_required_column_is_structural_error(self, dataset_factory, project_factory):
        ds = dataset_factory([project_factory()])
        ds.projects = ds.projects.drop(columns=["execution_type"])
        report = run_all_checks(ds)
        assert any(i.project_id == "*" and i.column == "execution_type" for i in report.errors)
        assert report.can_proceed is False


class TestPandasMissingValues:
    @pytest.mark.parametrize("missing", MISSING_MARKERS)
    def test_every_missing_marker_in_text_field_is_error(self, dataset_factory, project_factory, missing):
        report = run_all_checks(dataset_factory([project_factory(project_sponsor=missing)]))
        assert any(i.column == "project_sponsor" and i.severity == "ERROR" for i in report.issues)

    @pytest.mark.parametrize("missing", [m for m in MISSING_MARKERS if m.id in ("None", "np.nan", "pd.NA")])
    def test_every_missing_marker_in_numeric_field_is_error(self, dataset_factory, project_factory, missing):
        report = run_all_checks(dataset_factory([project_factory(budget_cost_eur=missing)]))
        assert any(i.column == "budget_cost_eur" and i.rule == "Completeness" for i in report.errors)

    def test_missing_values_are_normalised_to_real_na(self, dataset_factory, project_factory):
        ds = dataset_factory([project_factory(project_sponsor=pd.NA), project_factory(project_number="P-2", project_sponsor="")])
        sponsors = ds.projects["project_sponsor"]
        # never placeholder strings like "<NA>" or "nan" in the normalized dataset
        assert sponsors.isna().all()
        assert not sponsors.astype(object).isin(["<NA>", "nan", "None", ""]).any()

    @pytest.mark.parametrize("missing", [None, np.nan, pd.NA, pd.NaT, ""], ids=["None", "np.nan", "pd.NA", "pd.NaT", "leer"])
    def test_pydantic_model_treats_missing_optional_fields_as_none(self, project_factory, missing):
        record = ProjectRecord.model_validate(project_factory(planned_benefit_eur=missing, actual_start_date=missing))
        assert record.planned_benefit_eur is None and record.actual_start_date is None


class TestConfigurableRequiredFields:
    def test_configured_field_is_enforced(self, dataset_factory, project_factory):
        save_required_fields([*DEFAULT_REQUIRED_FIELDS, "risk_score"])
        report = run_all_checks(dataset_factory([project_factory(risk_score=None)]))
        assert any(i.column == "risk_score" and i.rule == "Completeness" and i.severity == "ERROR" for i in report.issues)

    def test_removed_field_is_no_longer_checked(self, dataset_factory, project_factory):
        save_required_fields([f for f in DEFAULT_REQUIRED_FIELDS if f != "project_sponsor"])
        report = run_all_checks(dataset_factory([project_factory(project_sponsor="")]))
        assert not any(i.column == "project_sponsor" for i in report.issues)
        assert report.blocked_project_ids == []

    def test_identity_fields_cannot_be_removed(self):
        assert set(LOCKED_FIELDS) <= set(save_required_fields([]))
        assert set(LOCKED_FIELDS) <= set(load_required_fields())

    def test_missing_config_uses_defaults(self):
        assert load_required_fields() == list(DEFAULT_REQUIRED_FIELDS)

    def test_corrupt_config_falls_back_to_defaults(self):
        from app.core.runtime_config import reset_runtime_config

        save_required_fields(["project_number", "status"])
        required_fields.config_path().write_text("{broken", encoding="utf-8")
        reset_runtime_config()  # reload the (corrupt) environment file
        assert load_required_fields() == list(DEFAULT_REQUIRED_FIELDS)
        assert required_fields.config_path().read_text(encoding="utf-8") == "{broken"  # never silently overwritten

    def test_config_is_persisted_as_json(self):
        save_required_fields(["project_number", "status"], updated_by="AI-ADMIN")
        data = json.loads(required_fields.config_path().read_text(encoding="utf-8"))
        assert data["updated_by"] == "AI-ADMIN" and "status" in data["required_fields"]


class TestQualityScores:
    def test_index_is_share_of_unblocked_projects(self, dataset):
        report = run_all_checks(dataset)  # P-1003 is locked
        assert report.quality_index == pytest.approx(66.7, abs=0.1)
        assert 0 < report.completeness_pct < 100
        assert report.warning_free_pct <= report.quality_index

    def test_perfect_portfolio_scores_100(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory()]))
        assert report.quality_index == 100.0 and report.completeness_pct == 100.0

    def test_scores_appear_in_markdown_for_llm_context(self, dataset):
        assert "Data Quality Index 67 %" in run_all_checks(dataset).to_markdown()


class TestLogicAndFinancialRules:
    def test_progress_without_actual_start_is_logic_error(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(percent_complete=30, actual_start_date="")]))
        assert any(i.rule == "Logic Check" and i.severity == "ERROR" for i in report.issues)

    def test_no_progress_without_actual_start_is_fine(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(percent_complete=0, actual_start_date="")]))
        assert not any(i.rule == "Logic Check" for i in report.issues)

    def test_active_project_past_end_date_is_error(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(planned_end_date="2020-01-31")]))
        assert any(i.rule == "Past Due" and i.severity == "ERROR" for i in report.issues)

    def test_eac_above_budget_is_overrun_warning(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(estimate_at_completion_eur=450000)]))
        overrun = [i for i in report.issues if i.rule == "Budget Overrun"]
        assert overrun and all(i.severity == "WARNING" for i in overrun)
        assert report.blocked_project_ids == []  # warnings do not lock

    def test_invalid_status_value_is_error(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(status="Purple")]))
        assert any(i.rule == "Domain" and i.column == "status" for i in report.errors)


class TestQualityProfileAndGate:
    def test_quality_profile_tag_is_reported(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(data_quality_profile="missing_business_case")]))
        assert any(i.rule == "DQ-Profile:missing_business_case" and i.severity == "ERROR" for i in report.issues)

    def test_valid_project_passes_without_errors(self, dataset):
        report = run_all_checks(dataset)
        assert _issues(report, "P-1001", "ERROR") == []
        assert "P-1003" in report.blocked_project_ids and "P-1001" not in report.blocked_project_ids
        assert report.can_proceed is True

    def test_blocked_reasons_name_the_rules(self, dataset):
        reasons = run_all_checks(dataset).blocked_reasons
        assert "Logic Check" in reasons["P-1003"] and "Governance Completeness" in reasons["P-1003"]
