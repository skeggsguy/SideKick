"""One tool call, start to finish: meta-tool handling (tool groups), policy
gating, offline pre-flight, execution, egress bookkeeping, taints, audit and
tracing. Split out of core/agent.py; the ToolDispatcher is bound to its Agent
and reads/writes the per-turn state (TurnContext, EgressLedger,
_tool_executing) through it.

The ordering inside dispatch is the safety invariant: when tool groups are
on, call_tool is UNWRAPPED FIRST — before hooks, traces, the taint check,
policy, egress bookkeeping and audit — so everything downstream keys on the
INNER tool's real name.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from ..store.runs import trace_event
from ..tools._netguard import InternalRefusalNote
from ..tools.base import Tool
from .connectivity import is_online
from .paths import is_sensitive_read
from .policy import (
    ActionClass,
    BROWSER_ACTION_TOOLS,
    DESTINATION_GATED_TOOLS,
    EGRESS_TOOLS,
)
from . import tool_groups
from .types import ToolCall, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from .agent import Agent

# After Stop, a tool that may change something gets this long to finish before
# the turn stops without it (a READ_ONLY tool is abandoned at once).
STOP_GRACE_S = 10.0

# Tool output stored in the audit log is digested to this many characters.
DIGEST_CHARS = 200

# Tool output streamed to the web UI's tool-step card. Larger than the audit
# digest (the card accordion shows detail) but still bounded so a big result
# can't flood the SSE stream to the iPad. Not persisted.
CARD_DIGEST_CHARS = 1000


class ToolDispatcher:
    def __init__(self, agent: "Agent") -> None:
        self._a = agent

    async def trace(self, run_id: str | None, kind: str, data: dict) -> None:
        # Best-effort: a failed trace write must not abort the turn (or,
        # mid-dispatch, strand a tool_use without its result).
        await trace_event(
            self._a.run_store, run_id, kind, data, source="agent"
        )

    async def dispatch_all(
        self, tool_calls: list[ToolCall], run_id: str | None
    ) -> list[ToolResult]:
        """One assistant step's calls, in order. After a Stop the rest are
        answered "not run" without dispatching: nothing more executes, and every
        tool_use still gets its result (the step must persist paired)."""
        results = []
        for tc in tool_calls:
            if self._a._stop_requested:
                await self.trace(run_id, "tool_skipped", {"tool": tc.name})
                results.append(ToolResult(
                    tool_call_id=tc.id, content="Not run: the user pressed Stop.",
                    is_error=True,
                ))
            else:
                results.append(await self.dispatch(tc, run_id))
        return results

    async def dispatch(self, tool_call: ToolCall, run_id: str | None) -> ToolResult:
        """Run one tool call. With tool groups active, the meta-tools are
        handled here: load_tools is answered in-agent, and call_tool is
        UNWRAPPED FIRST — before hooks, traces, the taint check, policy,
        egress bookkeeping and audit — so everything downstream keys on the
        INNER tool's real name (the safety invariant). The ToolResult keeps
        the wrapper's id; the persisted assistant message keeps the raw
        call_tool call verbatim (provider round-trip)."""
        if tool_call.name in tool_groups.META_TOOLS and self._a.toolset.grouped:
            if tool_call.name == tool_groups.LOAD_TOOLS:
                content, is_error = tool_groups.load_tools_result(
                    tool_call.arguments, self._a.toolset.groups()
                )
                return await self._meta_result(tool_call, content, is_error, run_id)
            inner = tool_groups.unwrap_call_tool(tool_call, self._a.toolset)
            if isinstance(inner, str):
                return await self._meta_result(tool_call, inner, True, run_id)
            return await self._dispatch_tool(inner, run_id, via_call_tool=True)
        return await self._dispatch_tool(tool_call, run_id)

    async def _meta_result(
        self, tool_call: ToolCall, content: str, is_error: bool, run_id: str | None
    ) -> ToolResult:
        """Hooks + traces for a meta-tool call the agent answers itself
        (load_tools, or a call_tool that couldn't be unwrapped). READ_ONLY
        semantics — no tool runs, so there is nothing to gate or audit."""
        self._a.on_tool_call(tool_call.name, tool_call.arguments)
        await self.trace(
            run_id, "tool_call", {"tool": tool_call.name, "args": tool_call.arguments}
        )
        await self.trace(
            run_id,
            "tool_result",
            {"tool": tool_call.name, "is_error": is_error, "chars": len(content)},
        )
        self._a.on_tool_result(tool_call.name, content, is_error)
        return ToolResult(tool_call_id=tool_call.id, content=content, is_error=is_error)

    async def _dispatch_tool(
        self, tool_call: ToolCall, run_id: str | None, via_call_tool: bool = False
    ) -> ToolResult:
        self._a.on_tool_call(tool_call.name, tool_call.arguments)
        call_trace = {"tool": tool_call.name, "args": tool_call.arguments}
        if via_call_tool:
            call_trace["via"] = tool_groups.CALL_TOOL
        await self.trace(run_id, "tool_call", call_trace)

        tool = self._a.toolset.get(tool_call.name)
        audited = tool is not None
        if tool is None:
            content, is_error, decision = self._unknown_tool(tool_call.name), True, "auto"
        else:
            await self._note_secret_read(tool_call, run_id)
            # call_tool declares its arguments as a free object, so nothing
            # upstream checked them: validate BEFORE policy, so a malformed call
            # is bounced back for self-correction without prompting anyone.
            errors = (
                tool_groups.schema_errors(tool, tool_call.arguments)
                if via_call_tool
                else []
            )
            if errors:
                content = tool_groups.invalid_args_message(tool_call.name, errors)
                is_error, decision, audited = True, "auto", False
                await self.trace(
                    run_id, "invalid_args", {"tool": tool_call.name, "errors": errors}
                )
            else:
                content, is_error, decision = await self._gate_and_run(
                    tool, tool_call, run_id
                )

        if audited:
            await self._audit(run_id, tool, tool_call, decision, content, is_error)
        await self.trace(
            run_id,
            "tool_result",
            {"tool": tool_call.name, "is_error": is_error, "chars": len(content)},
        )
        self._a.on_tool_result(tool_call.name, content, is_error)
        return ToolResult(
            tool_call_id=tool_call.id, content=content, is_error=is_error
        )

    def _unknown_tool(self, name: str) -> str:
        if self._a.toolset.in_catalog(name):
            # In the catalog but outside this run's profile. Profiles are
            # closed — there is no way to reach it, so no hint to give.
            return f"Error: tool '{name}' is not active in this run."
        return f"Error: unknown tool '{name}'"

    async def _note_secret_read(self, tool_call: ToolCall, run_id: str | None) -> None:
        """A secret-shaped read taints the turn BEFORE the tool runs, so an
        egress call in the same assistant step is already gated. A denied read
        still taints — the attempt alone signals intent."""
        if tool_call.name != "read_file" or not is_sensitive_read(
            str(tool_call.arguments.get("path", ""))
        ):
            return
        if not self._a._turn_ctx.secret_read:
            self._a._turn_ctx.secret_read = True
            await self.trace(
                run_id,
                "turn_tainted",
                {"tool": tool_call.name, "path": tool_call.arguments.get("path")},
            )

    async def _gate_and_run(
        self, tool: Tool, tool_call: ToolCall, run_id: str | None
    ) -> tuple[str, bool, str]:
        """Policy gate → offline pre-flight → execute. Returns (content,
        is_error, decision)."""
        decision, reason = await self._a.policy.check(
            tool_call.name,
            tool_call.arguments,
            tool.classification,
            context=self._a._turn_ctx,
        )
        await self.trace(
            run_id,
            "policy_decision",
            {"tool": tool_call.name, "decision": decision, "reason": reason},
        )
        if decision == "denied":
            return f"Tool call denied: {reason}", True, decision
        if tool.remote and not await is_online():
            # Refuse a doomed network call up front rather than let it unwind
            # into a half-written turn. The action is not performed and not
            # consumed — the user can retry once back online. NB: this TCP-probes 1.1.1.1, so tests that
            # dispatch a remote tool (test_invariants' egress chains) aren't network-offline (BK-006).
            await self.trace(run_id, "tool_offline", {"tool": tool_call.name})
            return (
                f"Can't run {tool_call.name}: no network connection. It was "
                "not done — ask me again when you're back online.",
                True,
                decision,
            )
        self._note_egress_call(tool_call.name, tool_call.arguments)
        content, is_error = await self._run_tool(tool, tool_call)
        await self._note_egress_result(tool_call.name, content, is_error, run_id)
        return content, is_error, decision

    async def _run_tool(self, tool: Tool, tool_call: ToolCall) -> tuple[str, bool]:
        # Only around tool.run — NOT during the approval wait upstream, where
        # a Stop should cancel immediately (nothing has executed yet).
        self._a._tool_executing = True
        try:
            return await self._run_until_stop(tool, tool_call)
        except asyncio.CancelledError:
            # A genuine cancellation of *our* task (shutdown/Ctrl-C) must
            # propagate. A CancelledError that merely bubbled out of the
            # tool's own cancel scope (e.g. an offline MCP call unwinding)
            # is reported to the model like any other tool failure.
            if asyncio.current_task().cancelling() > 0:
                raise
            return (
                f"Error running {tool_call.name}: the call was interrupted "
                "(are you offline?)",
                True,
            )
        except Exception as e:  # noqa: BLE001 - report tool failure to model
            return f"Error running {tool_call.name}: {e}", True
        finally:
            self._a._tool_executing = False

    async def _run_until_stop(self, tool: Tool, tool_call: ToolCall) -> tuple[str, bool]:
        """tool.run, unless the user presses Stop while it runs. A tool that
        never returns (a list_files blocked on a macOS privacy prompt, 2026-11-01)
        made a deferred Stop wait forever. Now a READ_ONLY tool is abandoned at
        once; any other gets STOP_GRACE_S to finish, since its side effect may be
        under way. Either way the step still persists with a result."""
        run = asyncio.ensure_future(tool.run(tool_call.arguments))
        stop = asyncio.ensure_future(self._a._stop_event.wait())
        try:
            await asyncio.wait({run, stop}, return_when=asyncio.FIRST_COMPLETED)
            if not run.done():
                read_only = tool.classification is ActionClass.READ_ONLY
                await asyncio.wait({run}, timeout=0 if read_only else STOP_GRACE_S)
            if run.done():
                return run.result(), False
            if tool.classification is ActionClass.READ_ONLY:
                return f"Stopped by the user before {tool_call.name} returned.", True
            return (
                f"Stopped by the user while {tool_call.name} was still running — "
                "it may or may not have taken effect.",
                True,
            )
        finally:
            stop.cancel()
            if not run.done():
                run.cancel()

    async def _audit(
        self,
        run_id: str | None,
        tool: Tool,
        tool_call: ToolCall,
        decision: str,
        content: str,
        is_error: bool,
    ) -> None:
        if self._a.audit is None:
            return
        try:
            await self._a.audit.log(
                run_id,
                tool_call.name,
                tool_call.arguments,
                tool.classification.value,
                decision,
                content[:DIGEST_CHARS],
                is_error,
            )
        except Exception as e:  # noqa: BLE001 - audit must not abort the turn
            print(
                f"[agent] audit.log failed for {tool_call.name}: {e}",
                file=sys.stderr,
            )

    def _note_egress_call(self, name: str, args: dict) -> None:
        """Egress bookkeeping for a call that is about to EXECUTE (denied and
        offline-refused calls never reach here): count it against the turn's
        budget, and admit a destination-gated URL's host to the session
        ledger — an unseen host only gets here auto (already seen is a
        no-op) or via an explicit approval, which is what admits it."""
        if name in EGRESS_TOOLS:
            self._a._turn_ctx.egress_calls += 1
        if name in BROWSER_ACTION_TOOLS:
            self._a._turn_ctx.action_calls += 1
        if name in DESTINATION_GATED_TOOLS:
            self._a._egress_ledger.add_url(str(args.get("url", "")))

    async def _note_egress_result(
        self, name: str, content: str, is_error: bool, run_id: str | None
    ) -> None:
        """Post-execution egress signals: an internal-target refusal taints
        the rest of the turn (same mechanism as the secret-read taint), and
        web_search results legitimize their result-line domains."""
        a = self._a
        if isinstance(content, InternalRefusalNote) and not a._turn_ctx.internal_probe:
            a._turn_ctx.internal_probe = True
            await self.trace(
                run_id,
                "turn_tainted",
                {"reason": "internal_probe", "tool": name},
            )
        if name == "web_search" and not is_error:
            a._egress_ledger.seed_search_results(content)
