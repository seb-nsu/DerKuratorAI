"""One line per LLM call – who called which model, how long it took, what it used and cost, how it ended.

No prompt or answer text is ever written: the log is for operations (latency, cost, errors per agent), not for
reading content. Lines go to data/llm_calls.jsonl (rotated at 5 MB); the last calls are also kept in memory for the
Admin Console. When an OpenTelemetry export is configured (app/observability/tracer.py), every call is also a span
and feeds the token and duration metrics – with the same fields, again without content.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.core import clock

logger = logging.getLogger("derkurator.llm_calls")

MAX_BYTES = 5 * 1024**2
GENERATIONS = 5  # rotated files kept: llm_calls.jsonl.1 (newest) … .5 (oldest)
RECENT = 200
_recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
_lock = threading.Lock()
_caller: ContextVar[str] = ContextVar("derkurator_llm_caller", default="")


def log_path():
    from app.core import logs

    return logs.LLM_CALLS_LOG


@contextmanager
def caller(name: str) -> Iterator[None]:
    """Names the agent or feature behind the LLM calls made in this context (orchestrator, assistant …)."""
    token = _caller.set(name)
    try:
        yield
    finally:
        _caller.reset(token)


def current_caller() -> str:
    return _caller.get() or "unnamed"


def _rotate(path) -> None:
    """Shifts llm_calls.jsonl -> .1 -> .2 … and drops the oldest generation."""
    for n in range(GENERATIONS - 1, 0, -1):
        older = path.with_suffix(f".jsonl.{n}")
        if older.exists():
            older.replace(path.with_suffix(f".jsonl.{n + 1}"))
    path.replace(path.with_suffix(".jsonl.1"))


def _write(entry: dict[str, Any]) -> None:
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                _rotate(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # the log must never break an analysis
        logger.warning("LLM call log not written (%s)", exc.__class__.__name__)


def record(model: str, provider: str, kind: str, duration_s: float, prompt_tokens: int, completion_tokens: int,
           outcome: str, tool_calls: int = 0) -> dict[str, Any]:
    """Writes one call. `kind`: chat | structured | stream; `outcome`: text | tool_calls | error:<Type>."""
    from app.core.cost_tracker import estimate_cost
    from app.core.runtime_config import current_environment

    cost = estimate_cost(model, prompt_tokens, completion_tokens)
    entry = {"timestamp": clock.stamp(),
             "environment": current_environment(), "caller": current_caller(), "model": model, "provider": provider,
             "kind": kind, "duration_s": round(duration_s, 3), "prompt_tokens": int(prompt_tokens),
             "completion_tokens": int(completion_tokens), "cost_usd": None if cost is None else round(cost, 6),
             "tool_calls": tool_calls, "outcome": outcome}
    with _lock:
        _recent.append(entry)
    _write(entry)
    from app.observability import tracer

    tracer.record_llm_call(entry)
    return entry


def recent(n: int = 50) -> list[dict[str, Any]]:
    with _lock:
        return list(_recent)[-n:][::-1]


def summary() -> list[dict[str, Any]]:
    """Calls, errors, median duration, tokens and cost per caller and model – over the calls kept in memory."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    with _lock:
        for e in _recent:
            groups.setdefault((e["caller"], e["model"]), []).append(e)
    rows = []
    for (who, model), entries in sorted(groups.items()):
        durations = sorted(e["duration_s"] for e in entries)
        rows.append({"Caller": who, "Model": model, "Calls": len(entries),
                     "Errors": sum(e["outcome"].startswith("error") for e in entries),
                     "Median s": durations[len(durations) // 2],
                     "Tokens": sum(e["prompt_tokens"] + e["completion_tokens"] for e in entries),
                     "Cost USD": round(sum(e["cost_usd"] or 0 for e in entries), 4)})
    return rows


def clear_recent() -> None:
    with _lock:
        _recent.clear()
