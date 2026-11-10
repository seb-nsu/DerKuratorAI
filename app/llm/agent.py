"""The agent loop: a role, a task, the tools it may call – and nothing else.

`Crew(tasks).kickoff()` runs the tasks one after the other (each sees the results of the earlier ones, as the
specialists did before) and returns their outputs. A task either answers in text, after as many tool rounds as its
agent allows, or – with `output_pydantic` – fills a structured result through a forced tool call that is validated
against the model. Time limit and round limit end a run: the round limit by asking for the final answer without
tools, the time limit with `AgentTimeout`, which the pipeline answers with its deterministic fallback.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm import client as llm_client
from app.llm.client import ChatClient
from app.llm.tools import Tool
from app.observability import llm_calls
from app.observability.tracer import trace_span

logger = logging.getLogger("derkurator.agent")
FINAL_ANSWER = "Give your final answer now, without calling any more tools."


class AgentTimeout(RuntimeError):
    pass


@dataclass
class Agent:
    role: str
    goal: str
    backstory: str
    model: str
    tools: list[Tool] = field(default_factory=list)
    max_iter: int = 5
    max_execution_time: float = 120.0
    name: str = ""  # agent role key for logs and traces (orchestrator, simulator …)

    @property
    def system_prompt(self) -> str:
        return f"You are {self.role}. {self.backstory}\nYour personal goal is: {self.goal}"


@dataclass
class Task:
    description: str
    expected_output: str
    agent: Agent
    output_pydantic: type[BaseModel] | None = None


@dataclass
class TaskOutput:
    raw: str
    pydantic: BaseModel | None = None
    tool_calls: list[str] = field(default_factory=list)


@dataclass
class CrewOutput:
    tasks_output: list[TaskOutput]

    @property
    def raw(self) -> str:
        return self.tasks_output[-1].raw if self.tasks_output else ""

    @property
    def pydantic(self) -> BaseModel | None:
        return self.tasks_output[-1].pydantic if self.tasks_output else None


def _prompt(task: Task, context: list[str]) -> str:
    text = f"{task.description}\n\nThis is the expected criteria for your final answer: {task.expected_output}"
    if context:
        text += "\n\nResults of the previous work in this analysis:\n" + "\n\n".join(context)
    return text


class _Submit:
    """The structured result as a tool the model is forced to call."""

    def __init__(self, model: type[BaseModel]):
        self.name = f"submit_{model.__name__.lower()}"
        self.description = f"Submit the result as a {model.__name__}."
        self.parameters = model.model_json_schema()


def run_task(task: Task, context: list[str] | None = None) -> TaskOutput:
    agent = task.agent
    client: ChatClient = llm_client.client_for(agent.model, purpose=f"agent '{agent.name or agent.role}'")
    deadline = time.monotonic() + agent.max_execution_time
    messages = [client.user(_prompt(task, context or []))]
    with llm_calls.caller(agent.name or "agent"), \
            trace_span(f"agent.{agent.name or 'task'}", model=agent.model, tools=len(agent.tools)):
        if task.output_pydantic is not None:
            return _structured(client, agent, task.output_pydantic, messages)
        return _tool_loop(client, agent, messages, deadline)


def _structured(client: ChatClient, agent: Agent, model: type[BaseModel], messages: list[dict]) -> TaskOutput:
    submit = _Submit(model)
    last_error = ""
    for _ in range(max(agent.max_iter, 1)):
        reply = client.chat(agent.system_prompt, messages, tools=[submit], force_tool=submit.name)
        call = next((c for c in reply.tool_calls if c.name == submit.name), None)
        if call is None:
            last_error = "no structured result"
            continue
        try:
            value = model.model_validate(call.arguments)
            return TaskOutput(value.model_dump_json(), value)
        except ValidationError as exc:
            last_error = str(exc).splitlines()[0]
            client.add_turn(messages, reply, [(call, f"ERROR: invalid result – {exc}. Submit a corrected result.")])
    raise ValueError(f"{agent.name or agent.role}: {last_error}")


def _tool_loop(client: ChatClient, agent: Agent, messages: list[dict], deadline: float) -> TaskOutput:
    tools = {t.name: t for t in agent.tools}
    called: list[str] = []
    for _ in range(max(agent.max_iter, 1)):
        if time.monotonic() > deadline:
            raise AgentTimeout(f"{agent.name or agent.role} exceeded {agent.max_execution_time:.0f} s")
        reply = client.chat(agent.system_prompt, messages, tools=list(tools.values()) or None)
        if not reply.tool_calls:
            return TaskOutput(reply.text.strip(), None, called)
        results = []
        for call in reply.tool_calls:
            tool = tools.get(call.name)
            result = tool.run(call.arguments) if tool else f"ERROR: unknown tool {call.name}"
            called.append(call.name)
            results.append((call, result))
        client.add_turn(messages, reply, results)
    # round limit reached: one last turn in which no tool may be called
    client.ask_final(messages, FINAL_ANSWER)
    reply = client.chat(agent.system_prompt, messages, tools=list(tools.values()) or None, no_tools=True)
    return TaskOutput(reply.text.strip(), None, called)


class Crew:
    """Runs its tasks in order; each task sees the outputs of the tasks before it."""

    def __init__(self, agents: list[Agent] | None = None, tasks: list[Task] | None = None, **_: Any):
        self.agents = agents or []
        self.tasks = tasks or []

    def kickoff(self) -> CrewOutput:
        outputs: list[TaskOutput] = []
        for task in self.tasks:
            outputs.append(run_task(task, [o.raw for o in outputs if o.raw]))
        return CrewOutput(outputs)
