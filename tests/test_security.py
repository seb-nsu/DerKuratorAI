"""RBAC (row-level security) and PII pseudonymization."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.runtime_config import update_runtime_config
from app.core.security import (
    ROLE_PM,
    ROLE_PMO,
    apply_rls_to_dataset,
    apply_row_level_security,
    mask_dataframe_pii,
    mask_dataset_pii,
    mask_pii_data,
    register_pii_from_dataset,
    register_pii_names,
    registered_pii,
)


class TestRowLevelSecurity:
    def test_project_manager_sees_only_own_projects(self, dataset):
        visible = apply_row_level_security(dataset.projects, ROLE_PM, "Priya Raman")
        assert set(visible["project_number"]) == {"P-1001", "P-1003"}
        assert (visible["project_manager"] == "Priya Raman").all()

    def test_project_manager_match_is_case_and_whitespace_insensitive(self, dataset):
        visible = apply_row_level_security(dataset.projects, ROLE_PM, "  priya RAMAN ")
        assert set(visible["project_number"]) == {"P-1001", "P-1003"}

    def test_pmo_sees_everything(self, dataset):
        assert len(apply_row_level_security(dataset.projects, ROLE_PMO, "PMO-ADMIN")) == len(dataset.projects)

    def test_unknown_role_is_denied_by_default(self, dataset):
        assert apply_row_level_security(dataset.projects, "Hacker", "Priya Raman").empty

    def test_project_manager_without_user_id_sees_nothing(self, dataset):
        assert apply_row_level_security(dataset.projects, ROLE_PM, None).empty

    def test_missing_rls_column_sees_nothing(self, dataset):
        df = dataset.projects.drop(columns=["project_manager"])
        assert apply_row_level_security(df, ROLE_PM, "Priya Raman").empty

    def test_rls_filters_all_related_tables(self, dataset):
        scoped = apply_rls_to_dataset(dataset, ROLE_PM, "Rowan Ellis")
        assert scoped.project_numbers == ["P-1002"]
        for table in (scoped.tasks, scoped.resource_plans, scoped.stakeholders):
            assert set(table["project_number"]) <= {"P-1002"}
        assert scoped.risks.empty and scoped.cost_plans.empty


class TestPiiMasking:
    def test_full_names_are_masked_with_role_placeholder(self, dataset):
        register_pii_from_dataset(dataset)
        text = mask_pii_data("Priya Raman stimmt sich mit Marco Bianchi und Clara Dubois ab.")
        assert text == "[PM_MASKED] stimmt sich mit [SPONSOR_MASKED] und [STAKEHOLDER_MASKED] ab."

    def test_last_names_and_emails_are_masked(self, dataset):
        register_pii_from_dataset(dataset)
        text = mask_pii_data("Frau Raman, bitte an priya.raman@example.com senden.")
        assert "Raman" not in text and "@" not in text
        assert "[PM_MASKED]" in text and "[EMAIL_MASKED]" in text

    def test_masking_respects_word_boundaries(self, dataset):
        register_pii_from_dataset(dataset)
        assert mask_pii_data("Kimberly prüft das Budget.") == "Kimberly prüft das Budget."

    def test_person_with_multiple_roles_gets_highest_priority_placeholder(self, dataset):
        # Rowan Ellis is PM (P-1002) and additional_assignee (P-1001) -> PM wins
        register_pii_from_dataset(dataset)
        assert registered_pii()["Rowan Ellis"] == "[PM_MASKED]"

    def test_masking_can_be_disabled_by_admin_toggle(self, dataset):
        register_pii_from_dataset(dataset)
        update_runtime_config(pii_masking_enabled=False)
        assert mask_pii_data("Priya Raman") == "Priya Raman"

    def test_missing_markers_are_never_registered_as_names(self):
        register_pii_names([None, np.nan, pd.NA, pd.NaT, "", "  ", "nan", "<NA>", "Tomasz Wolski"], "[PM_MASKED]")
        assert registered_pii() == {"Tomasz Wolski": "[PM_MASKED]"}
        assert mask_pii_data("Wert <NA> bleibt, nan bleibt") == "Wert <NA> bleibt, nan bleibt"

    def test_dataframe_masking_keeps_missing_values_missing(self):
        df = pd.DataFrame({"project_manager": ["Priya Raman", None, pd.NA], "project_name": ["A", "B", "C"]})
        masked = mask_dataframe_pii(df)
        assert masked["project_manager"].iloc[0] == "[PM_MASKED]"
        assert masked["project_manager"].iloc[1:].isna().all()  # missing stays missing, no placeholder

    def test_masked_dataset_contains_no_registered_names(self, dataset):
        register_pii_from_dataset(dataset)
        masked = mask_dataset_pii(dataset)
        frames = pd.concat([masked.projects, masked.tasks, masked.stakeholders, masked.risks])
        cells = [str(v) for v in frames.values.ravel()]  # pandas 3: astype(str) leaves NaN as float
        leaked = [name for name in registered_pii() if any(name in cell for cell in cells)]
        assert leaked == []
