"""FinOps: token usage and estimated LLM costs (process-wide, thread-safe).

Usage source: every call through app/llm/client.py reports the token counts of the provider's response (agents,
shadow crew, streamed paper, assistant, data quality, estimates, model tests).
Prices (USD per 1M tokens) come from data/supported_models.json; models without a price count as "unpriced".
Never raises into the caller – cost tracking must not break an analysis.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("derkurator.finops")

_lock = threading.Lock()
_usage: dict[str, "ModelUsage"] = {}


@dataclass
class RequestUsage:
    """Usage of a single steering request. Filled via a ContextVar, so parallel sessions and background shadow
    threads (which start with an empty context) never leak into it, and an admin reset of the process counters
    cannot make it negative."""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, model: str, prompt: int, completion: int, cost: float | None) -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            if cost is None:
                self.unpriced_models.add(model)
            else:
                self.cost_usd += cost

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"calls": self.calls, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                    "total_tokens": self.prompt_tokens + self.completion_tokens, "cost_usd": round(self.cost_usd, 6),
                    "unpriced_models": sorted(self.unpriced_models)}


_request_usage: ContextVar[RequestUsage | None] = ContextVar("derkurator_request_usage", default=None)


@contextmanager
def track_request() -> Iterator[RequestUsage]:
    """Collects the LLM usage of everything executed in this context (threads started from it run on copies of the
    caller's context and count too)."""
    usage = RequestUsage()
    token = _request_usage.set(usage)
    try:
        yield usage
    finally:
        _request_usage.reset(token)


@dataclass
class ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True


def _bare(model: str) -> str:
    return str(model or "").strip().split("/")[-1].lower()


def resolve_model(model: str) -> tuple[str, Any]:
    """Maps provider-specific names ('gemini-2.5-pro', 'models/gemini-2.5-pro') to the registry entry."""
    from app.core.model_registry import load_models

    model = str(model or "").strip()
    specs = load_models()
    spec = next((m for m in specs if m.model_id == model), None) or next(
        (m for m in specs if _bare(m.model_id) == _bare(model)), None)
    return (spec.model_id if spec else model or "unknown"), spec


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    _, spec = resolve_model(model)
    if spec is None or spec.input_cost_per_1m is None or spec.output_cost_per_1m is None:
        return None
    return (prompt_tokens * spec.input_cost_per_1m + completion_tokens * spec.output_cost_per_1m) / 1_000_000


def usage_tokens(usage: Any) -> tuple[int, int]:
    """Extracts (prompt, completion) tokens from the usage shapes of OpenAI, Anthropic, Gemini and LangChain."""
    if usage is None:
        return 0, 0
    get = usage.get if isinstance(usage, dict) else (lambda k, d=None: getattr(usage, k, d))
    for p_key, c_key in (("prompt_tokens", "completion_tokens"), ("input_tokens", "output_tokens"),
                         ("prompt_token_count", "candidates_token_count")):
        p, c = get(p_key), get(c_key)
        if p is not None or c is not None:
            try:
                return int(p or 0), int(c or 0)
            except (TypeError, ValueError):
                return 0, 0
    return 0, 0


def record(model: str, prompt_tokens: int, completion_tokens: int, source: str = "") -> None:
    if not prompt_tokens and not completion_tokens:
        return
    try:
        key, _ = resolve_model(model)
        cost = estimate_cost(key, prompt_tokens, completion_tokens)
        with _lock:
            entry = _usage.setdefault(key, ModelUsage())
            entry.calls += 1
            entry.prompt_tokens += prompt_tokens
            entry.completion_tokens += completion_tokens
            if cost is None:
                entry.priced = False
            else:
                entry.cost_usd += cost
        request = _request_usage.get()
        if request is not None:
            request.add(key, prompt_tokens, completion_tokens, cost)
        logger.debug("FinOps | %s | %s | in=%d out=%d cost=%s", source or "-", key, prompt_tokens, completion_tokens, cost)
    except Exception:  # tracking is best effort
        logger.exception("Cost tracking failed")


def snapshot() -> dict[str, Any]:
    with _lock:
        by_model = {k: asdict(v) for k, v in _usage.items()}
    prompt = sum(v["prompt_tokens"] for v in by_model.values())
    completion = sum(v["completion_tokens"] for v in by_model.values())
    return {
        "calls": sum(v["calls"] for v in by_model.values()),
        "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
        "cost_usd": round(sum(v["cost_usd"] for v in by_model.values()), 6),
        "unpriced_models": sorted(k for k, v in by_model.items() if not v["priced"]),
        "by_model": by_model,
    }


def reset() -> None:
    with _lock:
        _usage.clear()
