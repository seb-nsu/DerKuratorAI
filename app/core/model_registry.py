"""Config-driven model whitelist (data/supported_models.json) – no hard-coded model lists in code.

Each entry: {"model_id": "gemini/gemini-2.5-pro", "provider": "google", "is_thinking_model": true}.
Reads never raise: a missing or corrupt file yields an empty list (logged), so the app keeps running.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core import clock, logs
from app.core.config import PROVIDER_KEY_NAMES, detect_provider

logger = logging.getLogger("derkurator.models")
FILENAME = "supported_models.json"


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    provider: str
    is_thinking_model: bool = False
    input_cost_per_1m: float | None = None  # USD per 1M prompt tokens (FinOps estimate; None = unknown)
    output_cost_per_1m: float | None = None  # USD per 1M completion tokens


def _price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def registry_path() -> Path:
    return logs.DATA_DIR / FILENAME  # resolved at call time (tests redirect DATA_DIR)


def load_models() -> list[ModelSpec]:
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("models", []) if isinstance(data, dict) else []
    except FileNotFoundError:
        logger.error("Model registry %s not found – no models available", path)
        return []
    except (OSError, ValueError) as exc:
        logger.error("Model registry %s unreadable (%s) – no models available", path, exc)
        return []
    models, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("model_id", "")).strip():
            continue
        model_id = str(entry["model_id"]).strip()
        if model_id in seen:
            continue
        provider = str(entry.get("provider") or detect_provider(model_id)).strip().lower()
        if provider not in PROVIDER_KEY_NAMES:
            logger.warning("Model %s skipped: unknown provider '%s'", model_id, provider)
            continue
        seen.add(model_id)
        models.append(ModelSpec(model_id, provider, bool(entry.get("is_thinking_model", False)),
                                _price(entry.get("input_cost_per_1m")), _price(entry.get("output_cost_per_1m"))))
    return models


def model_ids() -> list[str]:
    return [m.model_id for m in load_models()]


def get_model(model_id: str) -> ModelSpec | None:
    return next((m for m in load_models() if m.model_id == (model_id or "").strip()), None)


def is_thinking_model(model_id: str) -> bool:
    spec = get_model(model_id)
    return bool(spec and spec.is_thinking_model)


def add_model(model_id: str, is_thinking: bool, provider: str | None = None) -> ModelSpec:
    """Adds (or updates) a model entry atomically. Raises OSError on write failure."""
    model_id = model_id.strip()
    if not model_id:
        raise ValueError("Model ID must not be empty.")
    spec = ModelSpec(model_id, (provider or detect_provider(model_id)).lower(), bool(is_thinking))
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("models", []) if isinstance(data, dict) else []
        extra = {k: v for k, v in data.items() if k not in ("models", "updated_at")} if isinstance(data, dict) else {}
    except FileNotFoundError:
        entries, extra = [], {}
    except ValueError as exc:
        raise OSError(f"Model registry is corrupt, refusing to overwrite it: {exc}") from exc
    previous = next((e for e in entries if isinstance(e, dict) and e.get("model_id") == model_id), {})
    record = {k: v for k, v in asdict(spec).items() if v is not None}
    record.update({k: previous[k] for k in ("input_cost_per_1m", "output_cost_per_1m") if k in previous})  # keep pricing
    entries = [e for e in entries if not (isinstance(e, dict) and e.get("model_id") == model_id)] + [record]
    payload = {**extra, "models": entries, "updated_at": clock.stamp()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("Model added to registry: %s (thinking=%s)", model_id, spec.is_thinking_model)
    return spec
