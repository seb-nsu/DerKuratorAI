"""Enterprise MCP (Model Context Protocol) connector – ready-to-use architecture building block.

NOT wired into the agents yet: the in-memory dependency graph stays the default data backbone. The connector turns the MCP settings of the
active environment (Admin → 🔌 Integrations, stored in data/env_<env>.json) into agent tools – each server tool
becomes a tool the agent may call, executed on the server – so a future release can hand external enterprise tools
(ServiceNow, Jira, …) to the agents with one line:

    connector = EnterpriseMCPConnector.from_environment()
    if connector.active:
        agent_kwargs = connector.agent_kwargs(agent_kwargs)      # extends Agent(tools=[...]) by the server tools
        # or explicitly: tools = connector.list_tools("servicenow")

Security:
- Bearer tokens are secrets: read from environment variables (<SERVER>_MCP_TOKEN, e.g. SERVICENOW_MCP_TOKEN),
  never from the JSON config, never logged.
- Only http(s) URLs are accepted; plain http is only allowed for localhost.
- Data returned by MCP tools must pass the same RLS filter and PII masking as local data before reaching an LLM
  (enforce this in the tool filter / adapter layer when wiring the connector in).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("derkurator.mcp")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_TIMEOUT_S = 10.0


class MCPConnectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPServer:
    name: str
    url: str

    @property
    def transport(self) -> str:
        """'sse' for legacy SSE endpoints (…/sse), otherwise Streamable HTTP (MCP spec 2025-03+)."""
        return "sse" if urlparse(self.url).path.rstrip("/").endswith("/sse") else "http"

    @property
    def token_env(self) -> str:
        return f"{self.name.upper()}_MCP_TOKEN"

    def headers(self) -> dict[str, str] | None:
        token = os.getenv(self.token_env, "").strip()
        return {"Authorization": f"Bearer {token}"} if token else None

    def validate(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MCPConnectorError(f"{self.name}: invalid MCP URL")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
            raise MCPConnectorError(f"{self.name}: plain http is only allowed for localhost – use https")


@dataclass
class EnterpriseMCPConnector:
    """Turns the environment's integration settings into agent tools and can probe the servers."""

    enabled: bool = False
    servers: list[MCPServer] = field(default_factory=list)
    timeout_s: float = DEFAULT_TIMEOUT_S

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, mcp_config: dict[str, Any] | None) -> "EnterpriseMCPConnector":
        cfg = mcp_config or {}
        servers = [MCPServer(name, str(url).strip()) for name, url in (cfg.get("servers") or {}).items() if str(url or "").strip()]
        return cls(enabled=bool(cfg.get("enabled")), servers=servers)

    @classmethod
    def from_environment(cls, env: str | None = None) -> "EnterpriseMCPConnector":
        from app.core.env_config import load_mcp_config
        from app.core.runtime_config import current_environment

        return cls.from_config(load_mcp_config(env or current_environment()))

    @property
    def active(self) -> bool:
        """True only if the master toggle is on and at least one server is configured."""
        return self.enabled and bool(self.servers)

    def server(self, name: str) -> MCPServer:
        match = next((s for s in self.servers if s.name == name), None)
        if match is None:
            raise MCPConnectorError(f"MCP server '{name}' is not configured")
        match.validate()
        return match

    # ------------------------------------------------------------------ agent integration
    def agent_tools(self, tool_filter=None) -> list[Any]:
        """The tools of every configured server as agent tools (`tool_filter(server, tool_name)` selects). Lists the
        tools once per server; each call opens its own short session, bounded by timeout_s."""
        from app.llm.tools import SchemaTool

        tools = []
        for srv in self.servers:
            srv.validate()
            for spec in self._list_specs(srv):
                if tool_filter is not None and not tool_filter(srv.name, spec["name"]):
                    continue
                tools.append(SchemaTool(f"{srv.name}_{spec['name']}", spec["description"] or spec["name"],
                                        spec["input_schema"], self._caller(srv, spec["name"])))
        return tools

    def agent_kwargs(self, kwargs: dict[str, Any], tool_filter=None) -> dict[str, Any]:
        """Returns Agent(...) kwargs with the server tools added – a no-op while MCP is disabled (the in-memory graph
        stays the default)."""
        if not self.active:
            return kwargs
        return {**kwargs, "tools": [*(kwargs.get("tools") or []), *self.agent_tools(tool_filter)]}

    def _caller(self, srv: MCPServer, name: str):
        def call(arguments: dict[str, Any]) -> str:
            async def _bounded():
                return await asyncio.wait_for(self._call_tool_async(srv, name, arguments), timeout=self.timeout_s)

            return asyncio.run(_bounded())

        return call

    # ------------------------------------------------------------------ discovery / health check
    def _transport(self, srv: MCPServer):
        if srv.transport == "sse":
            from mcp.client.sse import sse_client

            return sse_client(srv.url, headers=srv.headers(), timeout=self.timeout_s)
        from mcp.client.streamable_http import streamablehttp_client

        return streamablehttp_client(srv.url, headers=srv.headers(), timeout=self.timeout_s)

    async def _list_specs_async(self, srv: MCPServer) -> list[dict[str, Any]]:
        from mcp import ClientSession

        async with self._transport(srv) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.list_tools()
        return [{"name": t.name, "description": (t.description or "").strip(),
                 "input_schema": dict(t.inputSchema or {"type": "object", "properties": {}})} for t in result.tools]

    async def _call_tool_async(self, srv: MCPServer, name: str, arguments: dict[str, Any]) -> str:
        from mcp import ClientSession

        async with self._transport(srv) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
        text = "\n".join(getattr(part, "text", "") for part in result.content or []).strip()
        return f"ERROR: {text or name + ' failed'}" if result.isError else text

    def _list_specs(self, srv: MCPServer) -> list[dict[str, Any]]:
        async def _bounded():
            return await asyncio.wait_for(self._list_specs_async(srv), timeout=self.timeout_s)

        return asyncio.run(_bounded())

    def list_tools(self, name: str) -> list[dict[str, str]]:
        """Connects to one configured server and lists its tools (blocking, bounded by timeout_s)."""
        return [{"name": t["name"], "description": t["description"]} for t in self._list_specs(self.server(name))]

    def probe(self, name: str) -> tuple[bool, str]:
        """Admin health check: never raises, never returns secrets."""
        try:
            tools = self.list_tools(name)
        except MCPConnectorError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - network/protocol errors are reported to the admin
            logger.warning("MCP probe failed for %s: %s", name, type(exc).__name__)
            return False, f"Connection failed ({type(exc).__name__}): {str(exc)[:200]}"
        names = ", ".join(t["name"] for t in tools[:10]) or "none"
        return True, f"Connected – {len(tools)} tool(s): {names}{' …' if len(tools) > 10 else ''}"
