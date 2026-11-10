"""A scripted chat client for tests: no network, no SDK – replies come from a list, every call is recorded."""
from __future__ import annotations

from app.llm.client import ChatClient, Reply, ToolCall


class FakeClient(ChatClient):
    """`replies`: Reply objects (or plain strings) returned in order; the last one repeats. `fail` raises on every
    call. `chunks` are what `stream` sends."""

    provider = "fake"

    def __init__(self, replies=None, fail: bool = False, chunks=None, model: str = "test-model"):
        self.model = self.name = model
        self.thinking = False
        self.temperature = 0.1
        self.replies = [r if isinstance(r, Reply) else Reply(r) for r in (replies or ["ok"])]
        self.fail = fail
        self.chunks = list(chunks or [])
        self.calls: list[dict] = []

    def chat(self, system, messages, tools=None, force_tool=None, max_tokens=4096, no_tools=False) -> Reply:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools,
                           "force_tool": force_tool, "no_tools": no_tools})
        if self.fail:
            raise RuntimeError("model down")
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

    def stream(self, system, messages, on_text, max_tokens=4096) -> str:
        self.calls.append({"system": system, "messages": list(messages), "stream": True})
        for chunk in self.chunks:
            on_text(chunk)
        return "".join(self.chunks)

    def add_turn(self, messages, reply, results) -> None:
        messages.append({"role": "assistant", "tool_calls": [c.name for c in reply.tool_calls]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "id": c.id, "content": r}
                                                     for c, r in results]})

    @property
    def prompts(self) -> list[str]:
        """The first user message of every call."""
        return [c["messages"][0]["content"] for c in self.calls]


def call(name: str, arguments: dict | None = None, call_id: str = "c1") -> Reply:
    """A reply that calls one tool."""
    return Reply("", [ToolCall(call_id, name, arguments or {})])


def install(monkeypatch, client: FakeClient) -> FakeClient:
    """Every client lookup (agents, single calls, streaming) returns `client`."""
    import app.llm.client as llm_client

    monkeypatch.setattr(llm_client, "client_for", lambda model, purpose="": client)
    return client
