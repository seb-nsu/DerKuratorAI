"""Tools the agents may call: a plain function plus its name, description and argument schema.

`@tool("name")` wraps a function; the docstring is the description for the model, the signature gives the JSON schema
(built with pydantic, so the arguments are validated before the function runs). Calling the tool object calls the
function directly; `run(arguments)` is what the agent loop uses – it never raises, a failure comes back to the model as
an `ERROR: …` text, like every tool result of this application."""
from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

logger = logging.getLogger("derkurator.tools")


class Tool:
    def __init__(self, name: str, func: Callable[..., Any]):
        self.name = name
        self.func = func
        self.description = inspect.cleandoc(func.__doc__ or name)
        fields: dict[str, Any] = {}
        hints = inspect.get_annotations(func, eval_str=True)
        for param in inspect.signature(func).parameters.values():
            annotation = hints.get(param.name, Any)
            default = ... if param.default is inspect.Parameter.empty else param.default
            fields[param.name] = (annotation, default)
        self.args_model: type[BaseModel] = create_model(f"{name}_args", __config__=ConfigDict(extra="ignore"),
                                                        **fields)
        self.__doc__ = func.__doc__
        self.__name__ = name

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    @property
    def parameters(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def run(self, arguments: dict[str, Any] | None) -> str:
        try:
            args = self.args_model.model_validate(arguments or {})
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return f"ERROR: invalid arguments for {self.name} – {problems}"
        try:
            result = self.func(**args.model_dump())
        except Exception as exc:  # a tool failure is reported to the agent, never raised
            logger.exception("Tool %s failed", self.name)
            return f"ERROR: {self.name} failed ({exc.__class__.__name__}: {exc})"
        return result if isinstance(result, str) else str(result)

    def __repr__(self) -> str:
        return f"Tool({self.name})"


class SchemaTool(Tool):
    """A tool described by a ready JSON schema (e.g. a tool of an MCP server); `func` receives the arguments dict."""

    def __init__(self, name: str, description: str, schema: dict[str, Any], func: Callable[[dict[str, Any]], str]):
        self.name = name
        self.func = func
        self.description = description
        self._schema = schema
        self.__doc__ = description
        self.__name__ = name

    def __call__(self, **kwargs):
        return self.func(kwargs)

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    def run(self, arguments: dict[str, Any] | None) -> str:
        try:
            result = self.func(dict(arguments or {}))
        except Exception as exc:  # reported to the agent, never raised
            logger.exception("Tool %s failed", self.name)
            return f"ERROR: {self.name} failed ({exc.__class__.__name__}: {exc})"
        return result if isinstance(result, str) else str(result)


def tool(name: str) -> Callable[[Callable[..., Any]], Tool]:
    def wrap(func: Callable[..., Any]) -> Tool:
        return Tool(name, func)

    return wrap
