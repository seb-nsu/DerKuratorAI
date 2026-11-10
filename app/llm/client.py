"""Direct calls to the model providers through their own SDKs – no agent framework in between.

Two clients cover every supported model: the Anthropic SDK for Claude, and the OpenAI SDK for OpenAI models, Azure
OpenAI (v1 API), local OpenAI-compatible servers and Gemini (Google's OpenAI-compatible endpoint). Each client offers
the same three things: one chat turn (with tools and an optional forced tool for structured output), a streamed text
answer, and the bookkeeping of a tool conversation. Model, key and endpoint come from the runtime config on every
call; token usage goes to the FinOps tracker.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.core import cost_tracker
from app.core.config import detect_provider, provider_endpoint
from app.core.model_registry import is_thinking_model
from app.core.runtime_config import get_runtime_config
from app.observability import llm_calls

logger = logging.getLogger("derkurator.llm")

MAX_TOKENS = 4096
TIMEOUT_S = 120
MAX_RETRIES = 2
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_PREFIXES = {"openai", "anthropic", "gemini", "google", "models"}


class LLMConfigError(RuntimeError):
    pass


def bare_model_name(model: str) -> str:
    """'gemini/gemini-2.5-pro' -> 'gemini-2.5-pro' (registry ids may carry a provider prefix)."""
    prefix, _, rest = model.strip().partition("/")
    return rest if rest and prefix.lower() in _PREFIXES else model.strip()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # the provider's message, needed to continue a tool conversation
    usage: tuple[int, int] = (0, 0)  # prompt and completion tokens


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "")
                   for b in content or [])


class ChatClient:
    """One model of one provider. `model` is the registry id (pricing, flags); the SDK gets the bare name."""

    provider = ""

    def __init__(self, model: str, api_key: str, base_url: str = "", temperature: float = 0.1):
        self.model = model
        self.name = bare_model_name(model)
        self.thinking = is_thinking_model(model)
        self.temperature = temperature
        self._sdk = self._make_sdk(api_key, base_url)

    # -- to implement per provider
    def _make_sdk(self, api_key: str, base_url: str) -> Any:
        raise NotImplementedError

    def _chat(self, system, messages, tools, force_tool, max_tokens, no_tools) -> Reply:
        raise NotImplementedError

    def _stream(self, system, messages, on_text, max_tokens) -> tuple[str, tuple[int, int]]:
        raise NotImplementedError

    # -- every call is timed, costed and logged (without content)
    def chat(self, system: str, messages: list[dict], tools: list | None = None, force_tool: str | None = None,
             max_tokens: int = MAX_TOKENS, no_tools: bool = False) -> Reply:
        """`force_tool`: the model must call this tool (structured output); `no_tools`: it may call none."""
        kind, started = ("structured" if force_tool else "chat"), time.perf_counter()
        try:
            reply = self._chat(system, messages, tools, force_tool, max_tokens, no_tools)
        except Exception as exc:
            self._log(kind, started, (0, 0), f"error:{exc.__class__.__name__}")
            raise
        self._log(kind, started, reply.usage, "tool_calls" if reply.tool_calls else "text", len(reply.tool_calls))
        return reply

    def stream(self, system: str, messages: list[dict], on_text, max_tokens: int = MAX_TOKENS) -> str:
        started = time.perf_counter()
        try:
            text, usage = self._stream(system, messages, on_text, max_tokens)
        except Exception as exc:
            self._log("stream", started, (0, 0), f"error:{exc.__class__.__name__}")
            raise
        self._log("stream", started, usage, "text")
        return text

    def add_turn(self, messages: list[dict], reply: Reply, results: list[tuple[ToolCall, str]]) -> None:
        """Appends the model's turn and the tool results, in the provider's own message format."""
        raise NotImplementedError

    # -- shared
    @staticmethod
    def user(text: str) -> dict:
        return {"role": "user", "content": text}

    def ask_final(self, messages: list[dict], text: str) -> None:
        """Adds an instruction after the last tool results (in the same user turn where the provider needs that)."""
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            last["content"].append({"type": "text", "text": text})
        else:
            messages.append(self.user(text))

    def _log(self, kind: str, started: float, usage: tuple[int, int], outcome: str, tool_calls: int = 0) -> None:
        cost_tracker.record(self.model, usage[0], usage[1], source="sdk")
        llm_calls.record(self.model, self.provider, kind, time.perf_counter() - started, usage[0], usage[1], outcome,
                         tool_calls)

    def _merged(self, system: str, messages: list[dict]) -> tuple[str, list[dict]]:
        """Thinking models may reject a system prompt: it goes in front of the first user message."""
        if not self.thinking or not system:
            return system, messages
        first, rest = messages[0], messages[1:]
        if first.get("role") == "user" and isinstance(first.get("content"), str):
            return "", [{**first, "content": f"{system}\n\n{first['content']}"}, *rest]
        return "", [self.user(system), *messages]


class AnthropicClient(ChatClient):
    provider = "anthropic"

    def _make_sdk(self, api_key: str, base_url: str) -> Any:
        import anthropic

        return anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES,
                                   **({"base_url": base_url} if base_url else {}))

    @staticmethod
    def _tools(tools: list | None) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools or []]

    def _chat(self, system, messages, tools, force_tool, max_tokens, no_tools) -> Reply:
        system, messages = self._merged(system, messages)
        # no sampling parameters: current Claude models reject non-default values at API level
        kwargs: dict[str, Any] = {"model": self.name, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._tools(tools)
            if force_tool:
                kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
            elif no_tools:
                kwargs["tool_choice"] = {"type": "none"}
        response = self._sdk.messages.create(**kwargs)
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in response.content
                 if getattr(b, "type", "") == "tool_use"]
        return Reply(_text_of([b for b in response.content if getattr(b, "type", "") == "text"]), calls, response,
                     cost_tracker.usage_tokens(getattr(response, "usage", None)))

    def _stream(self, system, messages, on_text, max_tokens) -> tuple[str, tuple[int, int]]:
        system, messages = self._merged(system, messages)
        kwargs: dict[str, Any] = {"model": self.name, "max_tokens": max_tokens, "messages": messages}
        if system:
            kwargs["system"] = system
        parts: list[str] = []
        with self._sdk.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                parts.append(text)
                on_text(text)
            usage = cost_tracker.usage_tokens(getattr(stream.get_final_message(), "usage", None))
        return "".join(parts), usage

    def add_turn(self, messages, reply, results) -> None:
        content = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in reply.raw.content]
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": result} for call, result in results]})


class OpenAIClient(ChatClient):
    """OpenAI, Azure OpenAI (v1 API), local OpenAI-compatible servers and Gemini's OpenAI-compatible endpoint."""

    provider = "openai"

    def _make_sdk(self, api_key: str, base_url: str) -> Any:
        import openai

        return openai.OpenAI(api_key=api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES,
                             **({"base_url": base_url} if base_url else {}))

    @staticmethod
    def _tools(tools: list | None) -> list[dict]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}} for t in tools or []]

    def _kwargs(self, system: str, messages: list[dict], max_tokens: int) -> dict[str, Any]:
        system, messages = self._merged(system, messages)
        kwargs: dict[str, Any] = {"model": self.name, "messages": ([{"role": "system", "content": system}]
                                                                   if system else []) + messages}
        if self.thinking:  # reasoning models: only the default temperature, completion budget instead of max_tokens
            kwargs["max_completion_tokens"] = max_tokens * 4
        else:
            kwargs["temperature"] = self.temperature
            kwargs["max_tokens"] = max_tokens
        return kwargs

    def _chat(self, system, messages, tools, force_tool, max_tokens, no_tools) -> Reply:
        kwargs = self._kwargs(system, messages, max_tokens)
        if tools:
            kwargs["tools"] = self._tools(tools)
            if force_tool:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
            elif no_tools:
                kwargs["tool_choice"] = "none"
        response = self._sdk.chat.completions.create(**kwargs)
        message = response.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_unparsable": call.function.arguments}
            calls.append(ToolCall(call.id, call.function.name, arguments if isinstance(arguments, dict) else {}))
        return Reply(message.content or "", calls, message, cost_tracker.usage_tokens(getattr(response, "usage", None)))

    def _stream(self, system, messages, on_text, max_tokens) -> tuple[str, tuple[int, int]]:
        kwargs = self._kwargs(system, messages, max_tokens)
        parts: list[str] = []
        usage = None
        for chunk in self._sdk.chat.completions.create(**kwargs, stream=True,
                                                       stream_options={"include_usage": True}):
            usage = getattr(chunk, "usage", None) or usage
            for choice in chunk.choices or []:
                text = getattr(choice.delta, "content", None)
                if text:
                    parts.append(text)
                    on_text(text)
        return "".join(parts), cost_tracker.usage_tokens(usage)

    def add_turn(self, messages, reply, results) -> None:
        messages.append({"role": "assistant", "content": reply.text or None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in reply.tool_calls]})
        messages.extend({"role": "tool", "tool_call_id": call.id, "content": result} for call, result in results)


_clients: dict[str, ChatClient] = {}
_clients_lock = threading.Lock()


def client_for(model: str, purpose: str = "") -> ChatClient:
    """The client for a registry model id. Raises LLMConfigError when its key (or, under EU residency, its endpoint)
    is missing. One client per model, key, endpoint and temperature – the key itself is never a cache key."""
    cfg = get_runtime_config()
    missing = cfg.missing_key_for_model(model)
    if missing:
        raise LLMConfigError(f"{missing} missing for {purpose or 'model'} ({model})")
    provider = detect_provider(model)
    key = cfg.api_key_for(provider)
    base_url = provider_endpoint(provider) or (GEMINI_OPENAI_URL if provider == "google" else "")
    temperature = 1.0 if is_thinking_model(model) else cfg.temperature
    cache_key = hashlib.sha256(f"{model}|{provider}|{key}|{base_url}|{temperature}".encode()).hexdigest()
    with _clients_lock:
        client = _clients.get(cache_key)
        if client is None:
            cls = AnthropicClient if provider == "anthropic" else OpenAIClient
            client = _clients[cache_key] = cls(model, key, base_url, temperature)
    return client


def clear_clients() -> None:
    with _clients_lock:
        _clients.clear()


def complete(model: str, system: str, prompt: str, purpose: str = "", max_tokens: int = MAX_TOKENS) -> str:
    """One question, one text answer – for the single-call features (assistant, data quality, estimates). The part of
    `purpose` before a colon names the caller in the call log."""
    with llm_calls.caller(purpose.split(":")[0].strip() or "single call"):
        return client_for(model, purpose).chat(system, [ChatClient.user(prompt)], max_tokens=max_tokens).text.strip()
