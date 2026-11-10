"""Environment staging (env_<env>.json), per-agent models, model registry and thinking-model handling."""
from __future__ import annotations

import json

import pytest

from app.core import env_config, logs, model_registry
from app.core.env_config import env_config_path, load_env_config, promote_env_config
from app.core.runtime_config import (
    get_runtime_config,
    persist_runtime_config,
    switch_environment,
    update_runtime_config,
)
from app.crew import agents


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "DATA_DIR", tmp_path)
    path = tmp_path / "supported_models.json"
    path.write_text(json.dumps({"models": [
        {"model_id": "gpt-4o", "provider": "openai", "is_thinking_model": False},
        {"model_id": "o3", "provider": "openai", "is_thinking_model": True},
        {"model_id": "gemini/gemini-2.5-pro", "provider": "google", "is_thinking_model": True},
        {"model_id": "mystery-model", "provider": "acme", "is_thinking_model": False},
    ]}), encoding="utf-8")
    return path


class TestEnvironmentFiles:
    def test_missing_file_is_created_with_defaults(self):
        config = load_env_config("TEST")
        assert env_config_path("TEST").exists()
        assert set(config["agents"]) == set(env_config.AGENTS) and config["required_fields"]

    def test_corrupt_file_falls_back_and_is_not_overwritten(self):
        env_config_path("PROD").write_text("{broken", encoding="utf-8")
        assert load_env_config("PROD")["environment"] == "PROD"
        assert env_config_path("PROD").read_text(encoding="utf-8") == "{broken"

    def test_settings_are_persisted_per_environment(self):
        update_runtime_config(agents={"analyst": {"main": "gpt-4o", "shadow": "gemini/gemini-2.5-pro"}}, temperature=0.4)
        persist_runtime_config("tester")
        data = json.loads(env_config_path("DEV").read_text(encoding="utf-8"))
        assert data["agents"]["analyst"] == {"main": "gpt-4o", "shadow": "gemini/gemini-2.5-pro"}
        assert data["temperature"] == 0.4 and data["updated_by"] == "tester"

    def test_promote_copies_dev_to_test_and_archives_previous(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logs, "ARCHIVE_DIR", tmp_path / "archive")
        update_runtime_config(agents={"simulator": {"main": "o3"}}, required_fields=["status"])
        persist_runtime_config("tester")
        load_env_config("TEST")  # creates an initial TEST file
        backup = promote_env_config("DEV", "TEST", "tester")
        test_cfg = json.loads(env_config_path("TEST").read_text(encoding="utf-8"))
        assert test_cfg["environment"] == "TEST" and test_cfg["agents"]["simulator"]["main"] == "o3"
        assert "status" in test_cfg["required_fields"]
        assert backup is not None and backup.exists()

    def test_switch_environment_loads_other_file_and_keeps_keys(self):
        update_runtime_config(openai_api_key="sk-live-test")
        update_runtime_config(agents={"orchestrator": {"main": "gpt-4.1"}})
        persist_runtime_config("tester")
        cfg = switch_environment("TEST")
        assert cfg.environment == "TEST" and cfg.openai_api_key == "sk-live-test"
        assert cfg.model_for_role("orchestrator") == load_env_config("TEST")["agents"]["orchestrator"]["main"] != "gpt-4.1"
        assert switch_environment("DEV").model_for_role("orchestrator") == "gpt-4.1"

    def test_unknown_environment_is_rejected(self):
        with pytest.raises(ValueError):
            switch_environment("STAGING")


class TestModelRegistry:
    def test_models_are_loaded_and_unknown_providers_skipped(self, registry):
        ids = model_registry.model_ids()
        assert ids == ["gpt-4o", "o3", "gemini/gemini-2.5-pro"]
        assert model_registry.is_thinking_model("o3") and not model_registry.is_thinking_model("gpt-4o")

    def test_missing_or_corrupt_registry_yields_no_models(self, registry):
        registry.write_text("{broken", encoding="utf-8")
        assert model_registry.load_models() == []
        registry.unlink()
        assert model_registry.load_models() == []

    def test_add_model_appends_and_updates(self, registry):
        model_registry.add_model("claude-sonnet-5", is_thinking=False)
        model_registry.add_model("o3", is_thinking=False)  # update existing entry
        assert "claude-sonnet-5" in model_registry.model_ids()
        assert model_registry.get_model("claude-sonnet-5").provider == "anthropic"
        assert model_registry.is_thinking_model("o3") is False

    def test_add_model_refuses_to_overwrite_corrupt_registry(self, registry):
        registry.write_text("{broken", encoding="utf-8")
        with pytest.raises(OSError):
            model_registry.add_model("gpt-4.1", is_thinking=False)
        assert registry.read_text(encoding="utf-8") == "{broken"


class TestThinkingModels:
    def test_thinking_models_get_temperature_one(self, registry):
        update_runtime_config(openai_api_key="sk-test", temperature=0.2, agents={"simulator": {"main": "o3"}})
        assert agents.effective_temperature("o3", 0.2) == 1.0
        assert agents.effective_temperature("gpt-4o", 0.2) == 0.2
        from app.llm.client import client_for

        assert client_for(agents.build_simulator_agent().model).temperature == 1.0

    def test_thinking_models_get_the_system_prompt_in_the_first_message(self, registry):
        from app.llm.client import client_for

        update_runtime_config(openai_api_key="sk-test")
        msgs = [{"role": "user", "content": "question"}]
        system, merged = client_for("o3")._merged("rules", msgs)
        assert system == "" and len(merged) == 1 and merged[0]["content"].startswith("rules\n\nquestion")
        assert client_for("gpt-4o")._merged("rules", msgs) == ("rules", msgs)

    def test_shadow_variant_uses_shadow_model(self, registry):
        update_runtime_config(openai_api_key="sk-test", agents={"analyst": {"main": "gpt-4o", "shadow": "o3"}})
        assert agents.build_analyst_agent("shadow").model == "o3"
        assert agents.build_analyst_agent().model == "gpt-4o"


class TestModelConnectionTest:
    @staticmethod
    def _fake_sdk(monkeypatch, fail=False):
        """The OpenAI SDK replaced: records what would be sent, answers OK (or fails like a wrong key)."""
        from types import SimpleNamespace

        from app.llm.client import OpenAIClient

        sent: list[dict] = []

        def create(**kwargs):
            sent.append(kwargs)
            if fail:
                raise RuntimeError("401 invalid api key")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK", tool_calls=None))],
                                   usage=None)

        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(OpenAIClient, "_make_sdk", lambda self, key, url: sdk)
        return sent

    def test_missing_key_is_reported_without_call(self):
        ok, message = agents.test_model_connection("gpt-4.1", False)
        assert not ok and "OPENAI_API_KEY" in message

    def test_successful_call(self, monkeypatch):
        update_runtime_config(google_api_key="test-key")
        sent = self._fake_sdk(monkeypatch)
        ok, message = agents.test_model_connection("gemini/gemini-3.5-pro", is_thinking=True)
        assert ok and "succeeded" in message
        assert len(sent[0]["messages"]) == 1  # thinking: no separate system prompt
        assert "temperature" not in sent[0] and sent[0]["model"] == "gemini-3.5-pro"

    def test_failed_call_is_reported(self, monkeypatch):
        update_runtime_config(openai_api_key="sk-test")
        self._fake_sdk(monkeypatch, fail=True)
        ok, message = agents.test_model_connection("gpt-4.1", False)
        assert not ok and "401" in message

    def test_runtime_reports_missing_keys_per_agent_matrix(self):
        update_runtime_config(agents={"analyst": {"main": "gemini/gemini-2.5-pro"}})
        assert "GOOGLE_API_KEY" in get_runtime_config().missing_keys_for_models()


def test_default_models_are_listed_in_the_shipped_catalog(monkeypatch):
    """The per-agent defaults in app/core/config.py are the only model names in code – each must be in the catalog."""
    from app.core.config import PROJECT_ROOT, get_settings

    for name in ("ORCHESTRATOR_MODEL", "ANALYST_MODEL", "SIMULATOR_MODEL", "ADVISORY_MODEL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        defaults = {settings.orchestrator_model, settings.analyst_model, settings.simulator_model, settings.advisory_model}
    finally:
        get_settings.cache_clear()
    catalog = json.loads((PROJECT_ROOT / "data" / "supported_models.json").read_text(encoding="utf-8"))["models"]
    assert defaults <= {m["model_id"] for m in catalog}
