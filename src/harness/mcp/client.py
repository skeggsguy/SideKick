"""One stdio connection to one MCP server.

Wraps the MCP SDK's stdio transport + ClientSession behind the two calls the
harness needs: list the server's tools, and call one. The connection's
lifetime is owned by an AsyncExitStack held by the manager.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import mcp.types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Per-call ceiling so a wedged server can't hang the agent loop forever.
CALL_TIMEOUT = timedelta(seconds=60)


class MCPConnection:
    def __init__(self, name: str, params: StdioServerParameters) -> None:
        self.name = name
        self.params = params
        self.session: ClientSession | None = None

    async def start(self, stack: AsyncExitStack) -> None:
        try:
            read, write = await stack.enter_async_context(stdio_client(self.params))
            self.session = await stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
        except Exception as e:
            raise RuntimeError(
                f"MCP server {self.name!r} failed to start "
                f"(command: {self.params.command} {' '.join(self.params.args)}): {e}"
            ) from e

    async def list_tools(self) -> list[mcp.types.Tool]:
        assert self.session is not None, "connection not started"
        result = await self.session.list_tools()
        return result.tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Call a remote tool; flatten the result to text for the model."""
        assert self.session is not None, "connection not started"
        result = await self.session.call_tool(
            name, args, read_timeout_seconds=CALL_TIMEOUT
        )
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, mcp.types.TextContent):
                parts.append(block.text)
            else:
                parts.append(f"[unsupported content block: {block.type}]")
        text = "\n".join(parts) or "(empty result)"
        if result.isError:
            return f"Error from {self.name}.{name}: {text}"
        return text
