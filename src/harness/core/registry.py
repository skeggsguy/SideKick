"""Tool registry: a single place that holds every tool the agent can call.

The registry is the full catalog. What the model actually sees each turn is
an ActiveToolset — either everything (default) or a playbook's profile, a
small deterministic subset that keeps the schema list short and byte-stable
for prompt caching. A profile is CLOSED: it cannot grow mid-run, so the
active set (and the cached prefix) is fixed for the whole run.

Groups mode (core/tool_groups.py, local lane by default): with no profile,
the model is sent the always-on tools plus the load_tools/call_tool meta-tool
definitions instead of every deferrable (MCP) definition; lookups still
resolve ANY registry tool so deferred tools stay callable via call_tool. A
profile turns grouping off for that run — its tools are sent in full.
"""

from __future__ import annotations

from ..tools.base import Tool
from . import tool_groups
from .tool_groups import ToolDef


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name!r}")
        if tool.name in tool_groups.META_TOOLS:
            # Agent._dispatch intercepts these names when grouping is on — a
            # registry tool so named would be silently shadowed, never run.
            raise ValueError(f"Reserved tool name: {tool.name!r} (tool-groups meta-tool)")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())


class ActiveToolset:
    """The ordered set of tools one run exposes to the model.

    With no profile every registered tool is active (registration order).
    With a profile, only the named tools are active, in profile order —
    deterministic, so identical runs produce an identical (cacheable) prefix.
    """

    def __init__(self, registry: ToolRegistry, groups_enabled: bool = False) -> None:
        self.registry = registry
        self._names: list[str] | None = None  # None ⇒ the whole catalog
        #: Tool-groups mode requested (composition: local lane default).
        self.groups_enabled = groups_enabled

    def groups(self) -> dict[str, list[Tool]]:
        """{group: tools} over the whole catalog (registration order)."""
        return tool_groups.partition(self.registry.all())[1]

    @property
    def grouped(self) -> bool:
        """Grouping is ACTIVE for this run — load_tools/call_tool are being
        offered: requested, no profile set, and something to defer. A closed
        profile always wins, so call_tool can never widen one."""
        return self.groups_enabled and self._names is None and bool(self.groups())

    def set_profile(self, names: list[str]) -> list[str]:
        """Restrict the active set to ``names``; returns any unknown names so
        the caller can trace them rather than fail the run.

        A profile is a closed set: nothing can join it mid-run. That makes an
        autonomous job's allocation a real boundary (its MUTATING_LOCAL tools
        can auto-run) and keeps the schema list byte-stable for the whole
        run."""
        known: list[str] = []
        unknown: list[str] = []
        for name in names:
            if name in known:
                continue
            (known if self.registry.get(name) else unknown).append(name)
        self._names = known
        return unknown

    def get(self, name: str) -> Tool | None:
        """Look up an *active* tool; inactive catalog tools return None."""
        if self._names is not None and name not in self._names:
            return None
        return self.registry.get(name)

    def in_catalog(self, name: str) -> bool:
        return self.registry.get(name) is not None

    def tools(self) -> list[Tool | ToolDef]:
        """What is sent to the model this step. Grouped: the always-on tools,
        then the load_tools + call_tool DEFINITIONS (never registry tools)."""
        if self._names is not None:
            return [t for t in (self.registry.get(n) for n in self._names) if t]
        if not self.grouped:
            return self.registry.all()
        always, groups = tool_groups.partition(self.registry.all())
        return [*always, tool_groups.load_tools_def(groups), tool_groups.CALL_TOOL_DEF]

    def catalog(self) -> list[Tool]:
        return self.registry.all()
