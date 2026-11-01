"""Manual live eval: tool groups (load_tools + call_tool) on the LOCAL lane.

Drives twelve realistic requests against the local model server and checks that
the model loads the right group and reaches the right grouped tool (Google, or
in-process schedule/maps/browser/files) through call_tool with schema-valid
arguments. No grouped tool is EXECUTED: the loop stops at the first grouped
tool call, validates its arguments against the
tool's schema, and prints PASS/FAIL plus per-step timings.

Everything the model sees is built by the REAL harness code: build_agent()
(local lane, tool groups forced on) over the real MCP tool list from
MCPManager — listing tools spawns the MCP server but makes no Google API call —
with throwaway stores on a temp DB (never harness.db). Model calls go through
the real AnthropicProvider (the local lane's /v1/messages, thinking enabled,
chat_template_kwargs reasoning_effort). Always-on READ_ONLY tools are
simulated: current_time returns a fixed Monday, convert_time runs the real
in-process tool (pure core.timezone math, zone pinned to Europe/Berlin),
recall returns no facts.

The local server is SHARED with the live chat (-np 1, one cached prefix):
running this evicts the chat's prefix. Manual only, never in loops.

Run with: uv run python scripts/tool_groups_eval.py [--effort low]
Needs HARNESS_LOCAL_URL (from .env or the shell).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from harness.composition import build_agent
from harness.core import tool_groups
from harness.core.agent import Agent
from harness.core.types import Message, ToolCall, ToolResult
from harness.mcp import MCPManager
from harness.memory import MemoryStore
from harness.store import Database
from harness.store.approvals import ApprovalStore
from harness.store.audit import AllowlistStore, AuditStore
from harness.store.client_config import ClientConfigStore
from harness.store.runs import RunStore
from harness.store.triggers import TriggerStore
from harness.tools._extract import _JS_RENDERED_NOTE
from harness.tools.notify import NotifyTool

PROMPTS: list[tuple[str, str]] = [
    ("What's on my calendar this Thursday?", "calendar_list_events"),
    ("Any emails from my bank in the last 2 days?", "gmail_search"),
    ("Add a task to renew my passport, due next Friday", "task_create"),
    ("Find the budget spreadsheet in my Drive", "drive_list"),
    ("Create a Google Doc called Trip Notes", "docs_create"),
    ("Move my dentist appointment on Wednesday to 4pm", "calendar_list_events"),
    # In-process groups (schedule / maps / browser).
    ("Remind me Friday at 9am to call the bank", "schedule_add"),
    ("How long would it take to drive from the Louvre to the Eiffel Tower right now?", "maps_route"),
    # web_fetch is simulated as a JS-rendered page → should fall back to browser_fetch.
    ("Read https://app.example-plans.com/pricing and summarise the plans", "browser_fetch"),
    ("Book a table for 2 at Example Bistro on opentable.com this Saturday at 7pm", "browser_delegate"),
    # The files group (deferred since the prompt trim, 2026-10-18).
    ("List what's in my downloads folder", "list_files"),
    ("Use Python to work out 17% compound growth on $10k over 5 years", "sandbox_run"),
]

FIXED_NOW = "Monday 28 September 2026, 5:40 PM (Europe/Berlin, CEST, UTC+02:00)"
MAX_STEPS = 8


@dataclass
class Outcome:
    prompt: str
    passed: bool = False
    result: str = ""
    trail: list[str] = field(default_factory=list)


async def _no_push(title: str, body: str, url: str) -> int:
    raise RuntimeError("tool_groups_eval never sends pushes")


def _is_grouped(agent: Agent, name: str) -> bool:
    """A tool that lives in a group (Google MCP or in-process GROUP_TOOLS):
    the eval's stopping point — it is validated, never executed."""
    tool = agent.toolset.get(name)
    return tool is not None and tool_groups.group_of(tool) is not None


async def _simulate(agent: Agent, name: str, args: dict) -> tuple[str, bool]:
    """Results for the always-on tools the eval allows to 'run'."""
    if name == "current_time":
        return FIXED_NOW, False
    if name == "convert_time":
        tool = agent.toolset.get(name)
        return await tool.run(args), False  # pure timezone math, no I/O
    if name == "recall":
        return "No matching facts.", False
    if name == "web_fetch":
        # A JavaScript-rendered page: what the real render pipeline returns.
        return "Pricing\nLoading…" + _JS_RENDERED_NOTE, False
    return f"(eval: {name} is not simulated — answer without it)", True


def _verdict(agent: Agent, call: ToolCall, expect: str, wrapped: bool) -> tuple[bool, str]:
    tool = agent.toolset.get(call.name)
    errors = tool_groups.schema_errors(tool, call.arguments)
    ok = call.name == expect and not errors and wrapped
    detail = f"{call.name} {json.dumps(call.arguments)}"
    if not wrapped:
        detail += " (called DIRECTLY, not via call_tool)"
    if errors:
        detail += f" schema errors: {errors}"
    if call.name != expect:
        detail += f" (expected {expect})"
    return ok, ("PASS: " if ok else "FAIL: ") + detail


async def _handle(
    agent: Agent, tc: ToolCall, expect: str
) -> tuple[ToolResult | None, str | None, bool]:
    """(tool result, final verdict, passed). A verdict ends the prompt."""
    if tc.name == tool_groups.LOAD_TOOLS:
        content, err = tool_groups.load_tools_result(tc.arguments, agent.toolset.groups())
        return ToolResult(tc.id, content, err), None, False
    call, wrapped = tc, False
    if tc.name == tool_groups.CALL_TOOL:
        inner = tool_groups.unwrap_call_tool(tc, agent.toolset)
        if isinstance(inner, str):
            return ToolResult(tc.id, inner, True), None, False
        call, wrapped = inner, True
    if _is_grouped(agent, call.name):
        ok, verdict = _verdict(agent, call, expect, wrapped)
        return None, verdict, ok
    content, err = await _simulate(agent, call.name, call.arguments)
    return ToolResult(tc.id, content, err), None, False


def _describe(resp) -> str:
    if resp.tool_calls:
        return ", ".join(
            f"{tc.name}({json.dumps(tc.arguments)[:140]})" for tc in resp.tool_calls
        )
    return f"TEXT: {(resp.text or '')[:150]!r}"


async def run_prompt(agent: Agent, prompt: str, expect: str, effort: str) -> Outcome:
    out = Outcome(prompt)
    messages = [Message(role="user", text=prompt)]
    for _ in range(MAX_STEPS):
        t0 = time.monotonic()
        resp = await agent.provider.chat(
            agent.system_prompt, messages, agent.toolset.tools(), effort=effort
        )
        u = resp.usage
        tokens = f" [in={u.input_tokens} cache_read={u.cache_read_input_tokens}]" if u else ""
        out.trail.append(f"{time.monotonic() - t0:.1f}s{tokens} {_describe(resp)}")
        if resp.stop_reason == "max_tokens":
            out.result = "FAIL: truncated at max_tokens"
            return out
        if not resp.tool_calls:
            out.result = "FAIL: ended without a grouped tool call"
            return out
        messages.append(
            Message(
                role="assistant",
                text=resp.text,
                tool_calls=resp.tool_calls,
                content_blocks=resp.content_blocks,
            )
        )
        results: list[ToolResult] = []
        for tc in resp.tool_calls:
            result, verdict, passed = await _handle(agent, tc, expect)
            if verdict is not None:
                out.passed, out.result = passed, verdict
                return out
            results.append(result)
        messages.append(Message(role="tool", tool_results=results))
    out.result = f"FAIL: no grouped tool call within {MAX_STEPS} steps"
    return out


def _report_prefix(agent: Agent) -> None:
    sent = agent.toolset.tools()
    full = agent.toolset.catalog()

    def chars(tools) -> int:
        return len(
            json.dumps(
                [
                    {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                    for t in tools
                ]
            )
        )

    groups = {g: len(ts) for g, ts in agent.toolset.groups().items()}
    print(
        f"tools sent: {len(sent)} ({chars(sent)} chars) vs full catalog "
        f"{len(full)} ({chars(full)} chars); groups={groups}",
        flush=True,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--effort", default="low", choices=["off", "low", "medium", "xhigh"])
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("HARNESS_LOCAL_URL"):
        print("HARNESS_LOCAL_URL is not set — this eval targets the local lane.", file=sys.stderr)
        return 2
    os.environ["HARNESS_TOOL_GROUPS"] = "on"
    os.environ["HARNESS_TZ"] = "Europe/Berlin"
    # The utility (Haiku) provider is built but never called here.
    os.environ.setdefault("ANTHROPIC_API_KEY", "eval-dummy")

    with tempfile.TemporaryDirectory(prefix="tool_groups_eval-") as tmp:
        db = await Database.open(Path(tmp) / "eval.db")
        # MCP connections must open and close in this same task.
        manager = MCPManager.from_config()
        try:
            mcp_tools = await manager.start()
            agent, model = build_agent(
                run_store=RunStore(db),
                audit=AuditStore(db),
                allowlist=AllowlistStore(db),
                triggers=TriggerStore(db),
                memory=MemoryStore(db),
                client_config=ClientConfigStore(db),
                approvals=ApprovalStore(db),
                extra_tools=[*mcp_tools, NotifyTool(_no_push)],
            )
            print(f"model={model} effort={args.effort} grouped={agent.toolset.grouped}")
            _report_prefix(agent)
            outcomes = []
            for prompt, expect in PROMPTS:
                out = await run_prompt(agent, prompt, expect, args.effort)
                outcomes.append(out)
                print(f"\n## {out.prompt}\n  {out.result}", flush=True)
                for line in out.trail:
                    print(f"   · {line}", flush=True)
        finally:
            await manager.close()
            await db.close()

    passed = sum(o.passed for o in outcomes)
    print(f"\n{passed}/{len(outcomes)} passed")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
