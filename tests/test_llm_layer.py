"""The own LLM layer: tools with a schema from their signature, the agent loop (tool rounds, round and time limit,
structured results with a retry), the sequential crew, and the message formats of both SDK clients."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.llm import agent as llm_agent
from app.llm.agent import Agent, AgentTimeout, Crew, Task
from app.llm.client import AnthropicClient, OpenAIClient, Reply, ToolCall, bare_model_name
from app.llm.tools import tool
from tests.llm_fakes import FakeClient, call, install


@tool("add_numbers")
def add_numbers(a: int, b: int = 1) -> str:
    """Adds two numbers."""
    return f"RESULT {a + b}"


@tool("broken")
def broken(project_id: str) -> str:
    """Always fails."""
    raise ValueError("no data")


class Plan(BaseModel):
    summary: str
    steps: list[str]


def _agent(*tools, max_iter=3, max_execution_time=60.0) -> Agent:
    return Agent(role="Tester", goal="test", backstory="You test.", model="test-model", tools=list(tools),
                 max_iter=max_iter, max_execution_time=max_execution_time, name="tester")


class TestTools:
    def test_schema_and_description_come_from_the_function(self):
        assert add_numbers.description == "Adds two numbers."
        assert add_numbers.parameters == {"type": "object", "properties": {
            "a": {"type": "integer"}, "b": {"type": "integer", "default": 1}}, "required": ["a"]}
        assert add_numbers(2, 3) == "RESULT 5" and add_numbers.func(a=1) == "RESULT 2"

    def test_run_validates_and_never_raises(self):
        assert add_numbers.run({"a": "4", "unknown": 1}) == "RESULT 5"
        assert add_numbers.run({}).startswith("ERROR: invalid arguments for add_numbers – a:")
        assert broken.run({"project_id": "P-1"}) == "ERROR: broken failed (ValueError: no data)"

    def test_the_application_tools_all_have_a_schema(self):
        from app.crew.tools.simulator_tools import simulate_budget_change

        schema = simulate_budget_change.parameters
        assert {"project_id", "delta"} <= set(schema["required"]) and "is_perc" in schema["properties"]


class TestAgentLoop:
    def test_tool_rounds_then_text_answer(self, monkeypatch):
        fake = install(monkeypatch, FakeClient([call("add_numbers", {"a": 2}), "The sum is 3."]))
        out = llm_agent.run_task(Task("Add 2.", "a number", _agent(add_numbers)))
        assert out.raw == "The sum is 3." and out.tool_calls == ["add_numbers"]
        assert fake.calls[0]["system"].startswith("You are Tester. You test.")
        assert "expected criteria for your final answer: a number" in fake.prompts[0]
        assert fake.calls[1]["messages"][-1]["content"][0]["content"] == "RESULT 3"

    def test_unknown_tool_is_reported_to_the_model(self, monkeypatch):
        fake = install(monkeypatch, FakeClient([call("delete_everything"), "done"]))
        llm_agent.run_task(Task("x", "y", _agent(add_numbers)))
        assert fake.calls[1]["messages"][-1]["content"][0]["content"] == "ERROR: unknown tool delete_everything"

    def test_round_limit_asks_for_the_final_answer_without_tools(self, monkeypatch):
        fake = install(monkeypatch, FakeClient([call("add_numbers", {"a": 1})]))
        out = llm_agent.run_task(Task("x", "y", _agent(add_numbers, max_iter=2)))
        assert len(fake.calls) == 3 and fake.calls[-1]["no_tools"] is True
        assert fake.calls[-1]["messages"][-1]["content"][-1] == {"type": "text", "text": llm_agent.FINAL_ANSWER}
        assert out.tool_calls == ["add_numbers", "add_numbers"]

    def test_time_limit_raises(self, monkeypatch):
        install(monkeypatch, FakeClient([call("add_numbers", {"a": 1})]))
        with pytest.raises(AgentTimeout, match="tester exceeded"):
            llm_agent.run_task(Task("x", "y", _agent(add_numbers, max_execution_time=-1)))

    def test_structured_result_is_validated_and_retried_once_invalid(self, monkeypatch):
        bad = Reply("", [ToolCall("s1", "submit_plan", {"summary": "s"})])
        good = Reply("", [ToolCall("s2", "submit_plan", {"summary": "s", "steps": ["a"]})])
        fake = install(monkeypatch, FakeClient([bad, good]))
        out = llm_agent.run_task(Task("plan", "a plan", _agent(), output_pydantic=Plan))
        assert out.pydantic == Plan(summary="s", steps=["a"]) and fake.calls[0]["force_tool"] == "submit_plan"
        assert fake.calls[1]["messages"][-1]["content"][0]["content"].startswith("ERROR: invalid result")

    def test_structured_result_gives_up_after_the_rounds(self, monkeypatch):
        install(monkeypatch, FakeClient(["no tool call"]))
        with pytest.raises(ValueError, match="no structured result"):
            llm_agent.run_task(Task("plan", "a plan", _agent(max_iter=2), output_pydantic=Plan))

    def test_crew_runs_in_order_and_passes_the_earlier_results(self, monkeypatch):
        fake = install(monkeypatch, FakeClient(["first result", "second result"]))
        out = Crew(tasks=[Task("one", "x", _agent()), Task("two", "y", _agent())]).kickoff()
        assert [t.raw for t in out.tasks_output] == ["first result", "second result"] and out.raw == "second result"
        assert "Results of the previous work in this analysis:\nfirst result" in fake.prompts[1]
        assert "previous work" not in fake.prompts[0]


class TestClientFormats:
    def test_bare_model_names(self):
        assert bare_model_name("gemini/gemini-2.5-pro") == "gemini-2.5-pro"
        assert bare_model_name("claude-sonnet-5") == "claude-sonnet-5" and bare_model_name("org/model") == "org/model"

    def test_anthropic_tool_turn(self):
        client = AnthropicClient("claude-haiku-4-5", "test-key")
        block = SimpleNamespace(type="tool_use", id="t1", name="add_numbers", input={"a": 1},
                                model_dump=lambda exclude_none: {"type": "tool_use", "id": "t1"})
        sent = {}
        client._sdk.messages.create = lambda **kw: sent.update(kw) or SimpleNamespace(content=[block], usage=None)
        reply = client.chat("sys", [client.user("q")], tools=[add_numbers], force_tool="add_numbers")
        assert sent["tool_choice"] == {"type": "tool", "name": "add_numbers"}
        assert sent["tools"][0]["input_schema"]["required"] == ["a"]
        assert reply.tool_calls == [ToolCall("t1", "add_numbers", {"a": 1})]
        messages = [client.user("q")]
        client.add_turn(messages, reply, [(reply.tool_calls[0], "RESULT 2")])
        assert messages[1] == {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}
        assert messages[2]["content"] == [{"type": "tool_result", "tool_use_id": "t1", "content": "RESULT 2"}]
        client.ask_final(messages, "final")
        assert messages[2]["content"][-1] == {"type": "text", "text": "final"} and len(messages) == 3

    def test_openai_tool_turn_and_no_tools(self):
        client = OpenAIClient("gpt-4o", "test-key", base_url="http://localhost:9/v1")
        function = SimpleNamespace(name="add_numbers", arguments='{"a": 4}')
        message = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="c1", function=function)])
        sent = {}
        client._sdk.chat.completions.create = lambda **kw: sent.update(kw) or SimpleNamespace(
            choices=[SimpleNamespace(message=message)], usage=None)
        reply = client.chat("sys", [client.user("q")], tools=[add_numbers], no_tools=True)
        assert sent["tool_choice"] == "none" and sent["tools"][0]["function"]["name"] == "add_numbers"
        assert reply.tool_calls == [ToolCall("c1", "add_numbers", {"a": 4})] and reply.text == ""
        messages = [client.user("q")]
        client.add_turn(messages, reply, [(reply.tool_calls[0], "RESULT 5")])
        assert messages[1]["tool_calls"][0]["function"] == {"name": "add_numbers", "arguments": '{"a": 4}'}
        assert messages[2] == {"role": "tool", "tool_call_id": "c1", "content": "RESULT 5"}

    def test_openai_reasoning_models_get_a_completion_budget(self):
        client = OpenAIClient("o3", "test-key", base_url="http://localhost:9/v1")
        client.thinking = True
        kwargs = client._kwargs("rules", [client.user("q")], 1000)
        assert kwargs["max_completion_tokens"] == 4000 and "temperature" not in kwargs and "max_tokens" not in kwargs
        assert kwargs["messages"] == [{"role": "user", "content": "rules\n\nq"}]

    def test_a_missing_key_never_builds_a_client(self, monkeypatch):
        from app.core.runtime_config import update_runtime_config
        from app.llm.client import LLMConfigError, client_for

        update_runtime_config(openai_api_key="")
        with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
            client_for("gpt-4o", purpose="test")
