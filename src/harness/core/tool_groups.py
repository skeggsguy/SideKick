"""Tool groups: deferred loading of tool definitions, in-conversation.

On the local model lane every tool definition rides the prompt prefix, and the
~40 Google Workspace tools are the bulk of it. With grouping on, the model is
sent the always-on tools plus two small meta-tool DEFINITIONS:

- ``load_tools(group)`` — returns the group's full definitions as a tool
  RESULT, so they enter the conversation, never the (cached) prefix.
- ``call_tool(name, arguments)`` — the only way to reach a deferred tool. The
  llama.cpp fork's tool-call grammar is a choice over DECLARED tool names
  only, so a loaded-but-undeclared tool can't be called by its own name
  (spike 2026-09-28: 0/6 native vs 6/6 via call_tool).

The agent unwraps call_tool BEFORE the policy gate, audit and egress
bookkeeping (core/agent.py) — everything downstream sees the INNER tool name.
Neither meta-tool is a registry Tool: they never reach the catalog, GET
/api/tools, or the allowlist surfaces.

Two ways into a group, both configured below: an in-process tool is grouped
only when NAMED in GROUP_TOOLS (listing it is the opt-in); a ``deferrable``
tool (MCPTool) is grouped by name prefix. Anything else — including a
deferrable tool matching no prefix — stays always-on, never silently hidden.
Keep always-on the tools the system prompt tells the model to use unprompted
(current_time, convert_time, recall) and the everyday ones (web_search/web_fetch,
weather, notify — tools-less reminders run grouped and must alert by
themselves). The file/sandbox tools are occasional-use and live in the
"files" group, which registers first so it heads the catalog.

Open trap (eval can't catch it): a chat continuing a cron-started (closed-profile) session sees native
calls in history, then emits grouped names natively → snapped to the nearest DECLARED name; read the trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..tools.base import Tool
from .types import ToolCall

if TYPE_CHECKING:
    from .registry import ActiveToolset

LOAD_TOOLS = "load_tools"
CALL_TOOL = "call_tool"
META_TOOLS = frozenset({LOAD_TOOLS, CALL_TOOL})

# Name prefix → group. The ONE place a new MCP tool family is grouped; a
# deferrable tool matching none stays always-on (docs/adding-a-tool.md).
GROUP_PREFIXES: tuple[tuple[str, str], ...] = (
    ("calendar_", "calendar"),
    ("gmail_", "gmail"),
    ("drive_", "drive"),
    ("sheets_", "sheets_docs"),
    ("docs_", "sheets_docs"),
    ("task_", "tasks"),
    ("tasks_", "tasks"),
    ("tasklist_", "tasks"),
    ("tasklists_", "tasks"),
    ("outlook_", "outlook"),
)

# In-process tools deferred into a group, by exact name — the occasional-use
# ones. Registration order decides where each group appears in load_tools.
GROUP_TOOLS: dict[str, str] = {
    "read_file": "files",
    "list_files": "files",
    "search_files": "files",
    "edit_file": "files",
    "copy_file": "files",
    "sandbox_run": "files",
    "read_document": "files",
    "schedule_add": "schedule",
    "schedule_list": "schedule",
    "schedule_edit": "schedule",
    "schedule_disable": "schedule",
    "schedule_delete": "schedule",
    "maps_search": "maps",
    "maps_route": "maps",
    "run_stats": "admin",
    "calibration_report": "admin",
    "browser_fetch": "browser",
    "browser_delegate": "browser",
    "remember": "memory_admin",
    "forget": "memory_admin",
    "rss_fetch": "feeds",
    "reddit_comments": "feeds",
    "hn_comments": "feeds",
}


# One-line purpose per group, shown in load_tools' catalog. The model only
# sees names until it loads a group, so this is what routes a request to the
# right group ("remind me at 9am" → schedule, not a calendar event or a
# date-only Task: live probe 2026-09-28 dithered ~700 thinking tokens without).
GROUP_DESCRIPTIONS: dict[str, str] = {
    "files": "files on this machine — read, list, search, edit, copy; run code in the offline sandbox; PDF/DOCX/XLSX text",
    "schedule": "reminders (\"remind me at…\" → schedule_add one_shot) and recurring jobs",
    "calendar": "read/create/move events, invite people, RSVP",
    "tasks": "Google Tasks to-dos (date-only due dates, no times)",
    "gmail": "search/read mail, drafts, send, attachments",
    "drive": "find/read/upload/move Drive files",
    "sheets_docs": "read/write Google Sheets, create/edit Docs",
    "maps": "find places, travel times and routes",
    "browser": "pages web_fetch can't read (browser_fetch); actions on a site like booking or forms (browser_delegate)",
    "memory_admin": "save or delete long-term facts (recall is always available)",
    "admin": "the agent's own usage/cost stats and approval calibration",
    "outlook": "Outlook mail and calendar (read-only)",
    "feeds": "RSS/Atom feeds from Reddit, Hacker News, blogs, YouTube, etc.",
}


@dataclass(frozen=True)
class ToolDef:
    """A definition-only tool the provider sends (name/description/schema) —
    duck-typed to what the provider reads off a Tool. Never registered, never
    run: the agent handles the meta-tools itself."""

    name: str
    description: str
    input_schema: dict[str, Any]


CALL_TOOL_DEF = ToolDef(
    name=CALL_TOOL,
    description=(
        "Call a tool from a loaded group by name, with arguments matching its "
        "loaded definition."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "required": ["name", "arguments"],
    },
)


def group_of(tool: Tool) -> str | None:
    """The group a tool is deferred into, or None when it is always sent."""
    if tool.name in GROUP_TOOLS:
        return GROUP_TOOLS[tool.name]
    if not getattr(tool, "deferrable", False):
        return None
    for prefix, group in GROUP_PREFIXES:
        if tool.name.startswith(prefix):
            return group
    return None


def partition(tools: list[Tool]) -> tuple[list[Tool], dict[str, list[Tool]]]:
    """(always-on tools, {group: tools}) — both in registration order, groups
    ordered by first appearance, so the sent prefix is byte-stable."""
    always: list[Tool] = []
    groups: dict[str, list[Tool]] = {}
    for tool in tools:
        group = group_of(tool)
        if group is None:
            always.append(tool)
        else:
            groups.setdefault(group, []).append(tool)
    return always, groups


def _catalog_line(group: str, members: list[Tool]) -> str:
    names = ", ".join(t.name for t in members)
    purpose = GROUP_DESCRIPTIONS.get(group)
    return f"- {group}: {purpose} — {names}" if purpose else f"- {group}: {names}"


def load_tools_def(groups: dict[str, list[Tool]]) -> ToolDef:
    catalog = "\n".join(_catalog_line(g, members) for g, members in groups.items())
    return ToolDef(
        name=LOAD_TOOLS,
        description=(
            "Load a group of tools so you can use them. The tools below exist but "
            "their definitions are not loaded yet; load the group first, then call "
            "them via call_tool. Loading is cheap — reload if a group's definitions "
            f"are far back in the chat.\nGroups:\n{catalog}"
        ),
        input_schema={
            "type": "object",
            "properties": {"group": {"type": "string", "enum": list(groups)}},
            "required": ["group"],
        },
    )


def load_tools_result(
    args: dict[str, Any], groups: dict[str, list[Tool]]
) -> tuple[str, bool]:
    """(content, is_error) for a load_tools call: the group's definitions, one
    JSON object per line inside a <tools> tag."""
    group = args.get("group")
    if not isinstance(group, str) or group not in groups:
        return (
            f"Error: unknown tool group {group!r}. Valid groups: "
            f"{', '.join(groups)}",
            True,
        )
    defs = "\n".join(
        json.dumps(
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        )
        for t in groups[group]
    )
    return (
        f"Loaded the {group} tools. Call them via call_tool.\n"
        f'<tools group="{group}">\n{defs}\n</tools>',
        False,
    )


def unwrap_call_tool(tool_call: ToolCall, toolset: "ActiveToolset") -> ToolCall | str:
    """The INNER call (same id) a call_tool wraps, or an error string.

    Guards: the inner name must be a string naming an active registry tool
    (never a meta-tool — no call_tool→call_tool/load_tools recursion), and
    arguments must be an object.

    Top-level ``null`` arguments are DROPPED (= "not given"): compact_schema
    removed the ``anyOf [X, null]`` wrapper from optional MCP args, and local
    models often send ``"query": null``. Absent and None are the same to a
    FastMCP tool (its default is None), and a real default ("primary") is
    what a model sending null for it means anyway. Dropped here, so
    validation, policy, audit and the tool all see the same arguments."""
    name = tool_call.arguments.get("name")
    arguments = tool_call.arguments.get("arguments")
    if not isinstance(name, str) or not name:
        return "Error: call_tool needs 'name' — the tool to call, as a string."
    if name in META_TOOLS:
        return f"Error: call_tool cannot call {name}; call {name} directly."
    if toolset.get(name) is None:
        return (
            f"Error: unknown tool '{name}'. Load its group with load_tools "
            "and use a name from the loaded definitions."
        )
    if not isinstance(arguments, dict):
        return (
            f"Error: call_tool needs 'arguments' — an object of {name}'s "
            "arguments (use {} when it takes none)."
        )
    return ToolCall(
        id=tool_call.id,
        name=name,
        arguments={k: v for k, v in arguments.items() if v is not None},
    )


def schema_errors(tool: Tool, arguments: dict[str, Any]) -> list[str]:
    """Why ``arguments`` don't match ``tool``'s input schema ([] = valid).

    call_tool declares its arguments as a free object, so nothing upstream
    (the grammar or the API) checks them against the real tool's schema."""
    try:
        Draft202012Validator.check_schema(tool.input_schema)
    except SchemaError as e:
        # A broken schema is the server's bug, not the model's — say so rather
        # than blame the arguments, and don't run the tool on unchecked input.
        return [f"{tool.name}'s own input schema is invalid: {e.message}"]
    errors = sorted(
        Draft202012Validator(tool.input_schema).iter_errors(arguments),
        key=lambda e: [str(p) for p in e.path],
    )
    return [
        (f"{'/'.join(str(p) for p in e.path)}: " if e.path else "") + e.message
        for e in errors
    ]


def invalid_args_message(name: str, errors: list[str]) -> str:
    listed = "\n".join(f"- {e}" for e in errors)
    return (
        f"Error: arguments for {name} don't match its definition (the tool was "
        f"not run):\n{listed}\nFix the arguments and call it again."
    )
