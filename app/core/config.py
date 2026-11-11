"""Central configuration: loads .env and provides typed settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENV_FILE = PROJECT_ROOT / ".env"
# Settings come from .env; environment variables that are already set take precedence and are not overwritten.
_REAL_ENV = frozenset(os.environ)  # set before .env was read (shell, container, host secrets) – never replaced
load_dotenv(ENV_FILE, override=False)
_from_env_file: set[str] = {k for k in dotenv_values(ENV_FILE) if k not in _REAL_ENV} if ENV_FILE.exists() else set()


def reload_env() -> list[str]:
    """Re-reads .env at runtime (new keys, another environment) without a restart: values set outside .env still win,
    a variable removed from .env is removed again. Clears the cached settings. Returns the names that changed."""
    global _from_env_file
    values = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None} if ENV_FILE.exists() else {}
    changed = []
    for name in sorted(_from_env_file - set(values)):
        if os.environ.pop(name, None) is not None:
            changed.append(name)
    for name, value in values.items():
        if name in _REAL_ENV:
            continue
        if os.environ.get(name) != value:
            os.environ[name] = value
            changed.append(name)
    _from_env_file = set(values) - _REAL_ENV
    get_settings.cache_clear()
    return changed




def detect_provider(model_name: str) -> str:
    """Provider of a model: anthropic for 'claude*'/'anthropic/*', google for 'gemini*'/'google/*', else openai."""
    m = (model_name or "").strip().lower()
    if m.startswith("anthropic/") or m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gemini/", "google/", "gemini-")):
        return "google"
    return "openai"


PROVIDER_KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY"}
# An own endpoint per provider: an EU gateway, Azure OpenAI (v1 API) or a local model server (OpenAI-compatible).
# Google has none: with data residency "eu" its models are not used.
PROVIDER_ENDPOINT_NAMES = {"openai": "OPENAI_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL"}
RESIDENCIES = ("any", "eu")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


ENVIRONMENTS = ("DEV", "TEST", "PROD")


def _environment() -> str:
    value = _env("ENVIRONMENT", "DEV").upper()
    return value if value in ENVIRONMENTS else "DEV"  # never silently interpret unknown values as PROD


@dataclass(frozen=True)
class Settings:
    environment: str
    openai_api_key: str
    anthropic_api_key: str
    google_api_key: str
    orchestrator_model: str
    analyst_model: str
    simulator_model: str
    advisory_model: str
    data_source: str
    json_path: Path
    excel_path: Path
    mapping_path: Path
    servicenow_mapping_path: Path
    force_offline_mode: bool
    llm_data_residency: str = "any"  # "eu": only providers with an own (EU) endpoint are called
    openai_base_url: str = ""
    anthropic_base_url: str = ""
    graph_backend: str = "networkx"  # "neo4j": the dependency graph is also projected into Neo4j and queried there
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = field(default="", repr=False)  # secret: never shown, logged or written to a file
    neo4j_database: str = "neo4j"
    # Key checks and model routing: see app/core/runtime_config.py (Admin Console overrides)


def _path(name: str, default: str) -> Path:
    path = Path(_env(name, default))
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        environment=_environment(),
        openai_api_key=_env("OPENAI_API_KEY"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        google_api_key=_env("GOOGLE_API_KEY") or _env("GEMINI_API_KEY"),
        orchestrator_model=_env("ORCHESTRATOR_MODEL", "gpt-4o-mini"),
        analyst_model=_env("ANALYST_MODEL", "claude-sonnet-5"),
        simulator_model=_env("SIMULATOR_MODEL", "gpt-4o"),
        advisory_model=_env("ADVISORY_MODEL", "claude-sonnet-5"),
        data_source=_env("DATA_SOURCE", "json").lower(),
        json_path=_path("JSON_PATH", "data/DerKurator_Demo_Portfolio.json"),
        excel_path=_path("EXCEL_PATH", "data/DerKurator_Demo_Portfolio.xlsx"),
        mapping_path=_path("MAPPING_FILE", "data/mappings/servicenow_demo.yaml"),
        servicenow_mapping_path=_path("SERVICENOW_MAPPING_FILE", "data/mappings/servicenow_spm.yaml"),
        force_offline_mode=_env_bool("FORCE_OFFLINE_MODE", False),
        llm_data_residency=_residency(),
        openai_base_url=_env("OPENAI_BASE_URL").rstrip("/"),
        anthropic_base_url=_env("ANTHROPIC_BASE_URL").rstrip("/"),
        graph_backend=_graph_backend(),
        neo4j_uri=_env("NEO4J_URI"),
        neo4j_user=_env("NEO4J_USER", "neo4j"),
        neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
        neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
    )


GRAPH_BACKENDS = ("networkx", "neo4j")


def _graph_backend() -> str:
    value = _env("GRAPH_BACKEND", "networkx").lower()
    return value if value in GRAPH_BACKENDS else "networkx"  # an unknown value keeps the in-memory default


def _residency() -> str:
    # PROD without a setting keeps every call in the EU; DEV/TEST without a setting may use any provider
    value = _env("LLM_DATA_RESIDENCY", "eu" if _environment() == "PROD" else "any").lower()
    return value if value in RESIDENCIES else "eu"  # an unknown value is read as the stricter setting


def provider_endpoint(provider: str) -> str:
    """The own endpoint configured for a provider ('' = the provider's public API)."""
    s = get_settings()
    return {"openai": s.openai_base_url, "anthropic": s.anthropic_base_url}.get(provider, "")


def residency_block(provider: str) -> str | None:
    """Why a provider may not be called under the data residency setting, else None. The region of an endpoint cannot
    be checked by the app: setting "eu" is the operator's statement that the configured endpoints process in the EU."""
    if get_settings().llm_data_residency != "eu" or provider_endpoint(provider):
        return None
    name = PROVIDER_ENDPOINT_NAMES.get(provider)
    return f"{name} (EU endpoint)" if name else f"no EU endpoint for {provider}"


def endpoint_rows() -> list[dict[str, str]]:
    """Where each provider's calls go and whether the residency setting allows them (host only, never a key)."""
    from urllib.parse import urlparse

    rows = []
    for provider in PROVIDER_KEY_NAMES:
        url = provider_endpoint(provider)
        block = residency_block(provider)
        rows.append({"Provider": provider, "Endpoint": urlparse(url).netloc or url if url else "public API",
                     "Under the residency setting": "blocked – " + block if block else "allowed"})
    return rows
