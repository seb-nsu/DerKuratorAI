"""LLM operation in the EU: with LLM_DATA_RESIDENCY=eu only providers with an own endpoint are called, every client
goes to that endpoint, and the status shows the host – never a key."""
from __future__ import annotations

import pytest

from app.core import config
from app.core.runtime_config import get_runtime_config, reset_runtime_config
from app.crew import agents
from app.llm.client import client_for


@pytest.fixture
def env(monkeypatch):
    def set_env(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        config.get_settings.cache_clear()
        reset_runtime_config()
        agents.clear_llm_caches()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-openai")
    for key in ("LLM_DATA_RESIDENCY", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    yield set_env
    config.get_settings.cache_clear()
    reset_runtime_config()
    agents.clear_llm_caches()


def test_prod_without_a_residency_setting_keeps_every_call_in_the_eu(env):
    env(ENVIRONMENT="PROD")
    assert config.get_settings().llm_data_residency == "eu"
    env(ENVIRONMENT="PROD", LLM_DATA_RESIDENCY="any")
    assert config.get_settings().llm_data_residency == "any"


def test_the_prod_model_set_runs_under_eu_residency():
    """data/env_prod.json names only models whose provider has an own endpoint setting (no Google model)."""
    import json

    agents_cfg = json.loads((config.PROJECT_ROOT / "data" / "env_prod.json").read_text(encoding="utf-8"))["agents"]
    assert all(not a["main"].startswith("gemini") for a in agents_cfg.values())


def test_without_residency_every_provider_with_a_key_is_allowed(env):
    env()
    assert get_runtime_config().missing_key_for_model("claude-sonnet-5") is None


def test_eu_blocks_providers_without_an_own_endpoint(env):
    env(LLM_DATA_RESIDENCY="eu", OPENAI_BASE_URL="https://eu-gateway.example.com/v1")
    cfg = get_runtime_config()
    assert cfg.missing_key_for_model("claude-sonnet-5") == "ANTHROPIC_BASE_URL (EU endpoint)"
    assert cfg.missing_key_for_model("gpt-4o") is None
    assert cfg.missing_key_for_model("gemini-2.5-pro") in ("GOOGLE_API_KEY", "no EU endpoint for google")


def test_an_unknown_residency_value_is_read_as_eu(env):
    env(LLM_DATA_RESIDENCY="germany")
    assert config.get_settings().llm_data_residency == "eu"


def test_the_clients_go_to_the_own_endpoint(env):
    env(LLM_DATA_RESIDENCY="eu", ANTHROPIC_BASE_URL="https://claude.eu-gateway.example.com",
        OPENAI_BASE_URL="http://localhost:8000/v1")
    claude = client_for("claude-sonnet-5")
    assert str(claude._sdk.base_url).rstrip("/") == "https://claude.eu-gateway.example.com"
    local = client_for("gpt-4o")
    assert str(local._sdk.base_url).rstrip("/") == "http://localhost:8000/v1"


def test_status_rows_show_hosts_and_never_keys(env):
    env(LLM_DATA_RESIDENCY="eu", ANTHROPIC_BASE_URL="https://claude.eu-gateway.example.com/path")
    rows = {r["Provider"]: r for r in config.endpoint_rows()}
    assert rows["anthropic"]["Endpoint"] == "claude.eu-gateway.example.com"
    assert rows["openai"]["Under the residency setting"].startswith("blocked")
    assert "test-key" not in str(rows)
