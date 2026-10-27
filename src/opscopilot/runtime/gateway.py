"""MCP client side: connects to the MCP servers and exposes their tools to agents.

The gateway owns the boundary between model-visible tool schemas and the real ones:
arguments only the runtime may set (approval ids, idempotency keys) are removed from the
schema the model sees and injected at call time.
"""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from opscopilot.mcp_servers.approvals import RUNTIME_ARGS

ServerTarget = str | StdioServerParameters | MCPServer


@dataclass
class ToolInfo:
    qualified_name: str  # "crm.issue_refund"
    server: str
    name: str
    description: str
    model_schema: dict[str, Any]
    read_only: bool


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any]
    text: str


def stdio_target(module: str, env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", module], env=env)


def _model_schema(schema: dict[str, Any]) -> dict[str, Any]:
    props = {k: v for k, v in schema.get("properties", {}).items() if k not in RUNTIME_ARGS}
    required = [r for r in schema.get("required", []) if r not in RUNTIME_ARGS]
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


class ToolGateway:
    def __init__(self, targets: dict[str, ServerTarget]) -> None:
        self._targets = targets
        self._clients: dict[str, Client] = {}
        self._stack = AsyncExitStack()
        self.tools: dict[str, ToolInfo] = {}

    async def __aenter__(self) -> ToolGateway:
        for server_name, target in self._targets.items():
            client = await self._stack.enter_async_context(Client(target))
            self._clients[server_name] = client
            listing = await client.list_tools()
            for t in listing.tools:
                q = f"{server_name}.{t.name}"
                self.tools[q] = ToolInfo(
                    qualified_name=q,
                    server=server_name,
                    name=t.name,
                    description=t.description or "",
                    model_schema=_model_schema(t.input_schema),
                    read_only=bool(t.annotations and t.annotations.read_only_hint),
                )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def call(
        self, qualified_name: str, args: dict[str, Any], *, runtime_args: dict[str, Any]
    ) -> ToolResult:
        info = self.tools.get(qualified_name)
        if info is None:
            return ToolResult(ok=False, data={}, text=f"unknown tool {qualified_name}")
        # The model cannot smuggle runtime-only arguments in: they are overwritten here.
        clean = {k: v for k, v in args.items() if k not in RUNTIME_ARGS}
        extra = {k: v for k, v in runtime_args.items() if v is not None}
        result = await self._clients[info.server].call_tool(info.name, {**clean, **extra})
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        data = result.structured_content or {}
        if not data and text and not result.is_error:
            try:
                parsed = json.loads(text)
                data = parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                data = {}
        return ToolResult(ok=not result.is_error, data=data, text=text)
