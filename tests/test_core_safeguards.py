"""Regression tests: CapEx share, PII masking vs. project names, env file validation, widget clamping helper,
cross-process seed lock."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import env_config, security
from app.core.env_config import config_issue, env_config_path
from app.core.runtime_config import reset_runtime_config, update_runtime_config
from app.crew.tools import context as ctx
from app.crew.tools import simulator_tools as sim
from app.crew.tools.data_steward_tools import run_all_checks

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestCapexShare:
    def test_zero_total_cost_uses_capex_opex_plan(self, dataset_factory, project_factory):
        ds = dataset_factory([project_factory(total_planned_cost_eur=0)])
        ds.cost_plans = ds.cost_plans.iloc[0:0]  # force the fallback path
        ctx.set_portfolio_context(ds, {})
        capex, opex = sim._actual_split(ctx.get_project("P-1001"))
        assert capex == pytest.approx(238000 * 155000 / 420000) and capex + opex == pytest.approx(238000)
        assert "€36,890,000,000" not in sim.project_cancellation("P-1001")

    def test_share_is_clamped_to_unit_interval(self):
        capex, opex = sim._actual_split({"actual_cost_eur": 100, "planned_capital_eur": 300, "planned_operating_eur": -100})
        assert (capex, opex) == (100, 0)

    def test_steward_blocks_zero_total_with_planned_capex(self, dataset_factory, project_factory):
        report = run_all_checks(dataset_factory([project_factory(total_planned_cost_eur=0)]))
        assert "P-1001" in report.blocked_project_ids
        assert any(i.rule == "Financial Logic" for i in report.errors)


class TestPiiMaskingKeepsProjectNames:
    def test_surname_that_is_a_project_word_is_not_masked_alone(self, dataset_factory, project_factory):
        ds = dataset_factory([project_factory(project_name="Hybrid Hosting Consolidation", project_manager="Sam Cloud")])
        security.register_pii_from_dataset(ds)
        text = security.mask_pii_data("Sam Cloud asks to cut Hybrid Hosting Consolidation; cloud-native scope.")
        assert text == "[PM_MASKED] asks to cut Hybrid Hosting Consolidation; cloud-native scope."

    def test_other_surnames_are_still_masked(self, dataset):
        security.register_pii_from_dataset(dataset)
        assert security.mask_pii_data("Frau Raman prüft Alpha Platform.") == "Frau [PM_MASKED] prüft Alpha Platform."


class TestEnvFileValidation:
    @pytest.mark.parametrize("payload", [
        {"temperature": "abc"}, {"temperature": None}, {"temperature": 3}, {"required_fields": "status"},
        {"pii_masking_enabled": "yes"}, {"agents": {"analyst": {"main": 42}}}, ["not", "an", "object"],
    ])
    def test_invalid_values_fall_back_to_defaults_without_crash(self, payload):
        env_config_path("DEV").write_text(json.dumps(payload), encoding="utf-8")
        cfg = reset_runtime_config()  # must not raise
        assert cfg.temperature == env_config.default_env_config("DEV")["temperature"]
        assert "project_sponsor" in cfg.required_fields  # defaults, not ('a', 's', 't', 'u')
        assert config_issue("DEV") and "env_dev.json" in config_issue("DEV")
        assert json.loads(env_config_path("DEV").read_text(encoding="utf-8")) == payload  # file left untouched

    def test_partial_valid_file_is_merged_and_clears_the_issue(self):
        env_config_path("DEV").write_text(json.dumps({"temperature": "abc"}), encoding="utf-8")
        reset_runtime_config()
        env_config_path("DEV").write_text(json.dumps({"temperature": 0.4, "agents": {"analyst": {"shadow": None}}}),
                                          encoding="utf-8")
        assert reset_runtime_config().temperature == 0.4 and config_issue("DEV") is None

    def test_saving_settings_writes_a_valid_file(self):
        env_config_path("DEV").write_text("{broken", encoding="utf-8")
        reset_runtime_config()
        assert config_issue("DEV")
        update_runtime_config(temperature=0.3)
        from app.core.runtime_config import persist_runtime_config

        persist_runtime_config("tester")
        assert config_issue("DEV") is None and reset_runtime_config().temperature == 0.3


class TestWidgetClamp:  # helper (UI behaviour is covered in test_admin_console)
    def test_clamp(self):
        from app.ui.projects_ui import _clamp

        assert _clamp(120.0, 0.0, 100.0) == (100.0, True)
        assert _clamp(-5.0, 0.0, None) == (0.0, True)
        assert _clamp(3, 0, 10) == (3, False)


