"""MCP server manager: config → connections → Tool-ABC adapters.

Reads servers.toml, spawns each enabled server over stdio, and adapts every
remote tool to the harness Tool ABC so the registry and policy engine treat
them exactly like in-process tools.

Safety: a remote tool's ActionClass comes from the server's config lists
(read_only / mutating_local / destructive). Any tool NOT listed defaults to
OUTWARD — unknown remote actions get an approval gate, never a free pass.
"""

from __future__ import annotations

import os
import tomllib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

from ..core.paths import roots_env_value
from ..core.policy import ActionClass
from ..tools.base import Tool
from .client import MCPConnection

DEFAULT_CONFIG = Path(__file__).parent / "servers.toml"


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str]
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    classifications: dict[str, ActionClass] = field(default_factory=dict)

    def classify(self, tool_name: str) -> ActionClass:
        return self.classifications.get(tool_name, ActionClass.OUTWARD)


_NULL = {"type": "null"}


def compact_schema(node: Any) -> Any:
    """Strip pydantic's schema filler from a FastMCP tool's input schema.

    Every tool schema rides every model request, so the noise costs tokens
    on each turn (~1.3k across the Google Workspace tools). Removed:
    ``title`` labels (a title-cased copy of the key), ``default: null`` and
    the ``anyOf: [X, {type: null}]`` optional wrapper — optionality is
    already stated by absence from ``required``. Meaning-carrying keys
    (types, enums, descriptions, real defaults) are kept.

    Recurses only through schema POSITIONS: ``properties`` maps argument
    NAMES to schemas, so an argument literally called ``title`` survives.
    Returns a new structure; the input is not mutated.
    """
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" or (key == "default" and value is None):
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: compact_schema(sub) for name, sub in value.items()}
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems") and isinstance(value, list):
            out[key] = [compact_schema(sub) for sub in value]
        elif key in ("items", "additionalProperties", "not"):
            out[key] = compact_schema(value)
        else:
            out[key] = value
    variants = out.get("anyOf")
    if isinstance(variants, list) and len(variants) == 2 and _NULL in variants:
        (inner,) = [v for v in variants if v != _NULL]
        del out["anyOf"]
        # The node's own keys (description, a real default) win over the
        # variant's on collision.
        out = {**inner, **out}
    return out


class MCPTool(Tool):
    """Adapts one remote MCP tool to the in-process Tool ABC."""

    #: MCP tools always cross the network to their server's backend.
    remote = True
    #: Groupable behind load_tools on the local lane (core/tool_groups.py).
    deferrable = True

    def __init__(
        self,
        connection: MCPConnection,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        classification: ActionClass,
    ) -> None:
        self.connection = connection
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.classification = classification

    async def run(self, args: dict[str, Any]) -> str:
        return await self.connection.call_tool(self.name, args)


class MCPManager:
    """Owns the lifetime of every configured MCP server connection.

    start() and close() must run in ONE asyncio task (stdio cancel scopes) —
    never hold an MCPManager in a pytest-asyncio fixture."""

    def __init__(self, configs: list[ServerConfig]) -> None:
        self.configs = configs
        self._stack = AsyncExitStack()
        self.tools: list[MCPTool] = []

    @classmethod
    def from_config(cls, path: Path = DEFAULT_CONFIG) -> "MCPManager":
        return cls(load_server_configs(path))

    async def start(self) -> list[MCPTool]:
        """Connect every enabled server and adapt its tools. Fail fast with
        context — a half-connected tool surface is worse than a clear error."""
        for cfg in self.configs:
            if not cfg.enabled:
                continue
            conn = MCPConnection(
                cfg.name,
                StdioServerParameters(
                    command=cfg.command,
                    args=cfg.args,
                    # Server inherits our env (PATH, HOME, .env vars) plus the
                    # file jail roots, with config-specified overrides on top.
                    env={
                        **os.environ,
                        "HARNESS_FILE_ROOTS": roots_env_value(),
                        **cfg.env,
                    },
                    cwd=cfg.cwd,
                ),
            )
            await conn.start(self._stack)
            for t in await conn.list_tools():
                self.tools.append(
                    MCPTool(
                        connection=conn,
                        name=t.name,
                        description=t.description or "",
                        input_schema=compact_schema(t.inputSchema),
                        classification=cfg.classify(t.name),
                    )
                )
        return self.tools

    async def close(self) -> None:
        await self._stack.aclose()


def load_server_configs(path: Path) -> list[ServerConfig]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        data = tomllib.load(f)

    configs: list[ServerConfig] = []
    for name, raw in data.get("servers", {}).items():
        classifications: dict[str, ActionClass] = {}
        for key, action in (
            ("read_only", ActionClass.READ_ONLY),
            ("mutating_local", ActionClass.MUTATING_LOCAL),
            ("outward", ActionClass.OUTWARD),
            ("destructive", ActionClass.DESTRUCTIVE),
        ):
            for tool_name in raw.get(key, []):
                if tool_name in classifications:
                    raise ValueError(
                        f"servers.toml: tool {tool_name!r} listed under more "
                        f"than one classification for server {name!r}"
                    )
                classifications[tool_name] = action
        configs.append(
            ServerConfig(
                name=name,
                command=raw["command"],
                args=list(raw.get("args", [])),
                enabled=raw.get("enabled", True),
                env=dict(raw.get("env", {})),
                cwd=raw.get("cwd"),
                classifications=classifications,
            )
        )
    return configs
