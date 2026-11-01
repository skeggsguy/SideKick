"""Base class for in-process tools.

A Tool exposes a name, a description, a JSON Schema for its input, and an
ActionClass the policy engine uses to decide whether the call needs approval.
MCP-provided tools are adapted to this same interface in a later phase, so
the registry can treat all tools uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.policy import ActionClass


class Tool(ABC):
    #: Unique tool name the model uses to call it.
    name: str
    #: Human/model-readable description of what the tool does and when to use it.
    description: str
    #: JSON Schema describing the tool's input object.
    input_schema: dict[str, Any]
    #: How the policy engine should gate this tool.
    classification: ActionClass = ActionClass.READ_ONLY
    #: True for tools that reach the network (e.g. MCP-backed). The agent runs
    #: a connectivity pre-flight before these so an offline call is refused
    #: cleanly instead of unwinding into a half-written turn.
    remote: bool = False
    #: True for tools whose definitions may be DEFERRED behind a load_tools
    #: group (core/tool_groups.py) — set by MCPTool. A marker, not an import
    #: check, so core never imports the MCP stack. In-process tools stay False
    #: (always sent).
    deferrable: bool = False

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> str:
        """Execute the tool and return a string result for the model.

        Async so MCP-backed tools plug in directly; tools doing blocking
        work (file I/O, subprocesses) must not block the event loop —
        use asyncio.to_thread or asyncio subprocesses."""
        ...
