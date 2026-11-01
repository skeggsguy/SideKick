"""The agent loop.

The heartbeat of the harness: send the conversation + tools to the model, run
any tools it calls (through the policy gate), feed results back, repeat until
it answers in plain text. Async end-to-end: model text streams to the
interface via on_text_delta, and every step is traced to the run store.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from dataclasses import asdict
from typing import Callable, Optional

from ..memory import MemoryExtractor, MemoryStore, inject_first_turn
from ..providers.base import LLMProvider, OnToolStart
from ..store.audit import AuditStore
from ..store.runs import RunStore, trace_event
from .context import Compactor
from .egress import EgressLedger
from .policy import PolicyEngine, TurnContext
from .registry import ActiveToolset, ToolRegistry
from .session import Session
from .tool_dispatch import ToolDispatcher
from .types import Usage

# Hooks let the interface (CLI now, web later) observe the loop without the
# core knowing anything about how output is rendered.
OnTextDelta = Callable[[str], None]
OnText = Callable[[str], None]
OnToolCall = Callable[[str, dict], None]
OnToolResult = Callable[[str, str, bool], None]
OnEvent = Callable[[], None]
OnRunStart = Callable[[Optional[str]], None]

# Safety backstop so a tool-call loop can't spin forever in one turn.
MAX_STEPS = 60


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        system_prompt: str,
        policy: PolicyEngine,
        run_store: RunStore | None = None,
        audit: AuditStore | None = None,
        memory: MemoryStore | None = None,
        memory_extractor: MemoryExtractor | None = None,
        compactor: Compactor | None = None,
        max_steps: int = MAX_STEPS,
        group_tools: bool = False,
        on_text_delta: Optional[OnTextDelta] = None,
        on_text: Optional[OnText] = None,
        on_tool_call: Optional[OnToolCall] = None,
        on_tool_result: Optional[OnToolResult] = None,
        on_thinking_delta: Optional[OnTextDelta] = None,
        on_thinking_done: Optional[OnEvent] = None,
        on_truncated: Optional[OnEvent] = None,
        on_run_start: Optional[OnRunStart] = None,
        on_model_call: Optional[OnEvent] = None,
        on_answer_complete: Optional[OnEvent] = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        #: What the model sees: the whole catalog by default, a playbook's
        #: profile when run_turn is given one — a closed set for that run.
        #: group_tools=True defers MCP definitions behind load_tools/call_tool
        #: (core/tool_groups.py) whenever no profile is set.
        self.toolset = ActiveToolset(registry, groups_enabled=group_tools)
        self.system_prompt = system_prompt
        self.policy = policy
        self.run_store = run_store
        self.audit = audit
        self.memory = memory
        self.memory_extractor = memory_extractor
        self.compactor = compactor
        #: Per-turn tool-call ceiling. Default MAX_STEPS; a browser subagent
        #: raises it to fit its action budget (composition._browser_child_factory).
        self.max_steps = max_steps
        self.on_text_delta = on_text_delta or (lambda _t: None)
        self.on_text = on_text or (lambda _t: None)
        self.on_tool_call = on_tool_call or (lambda _n, _a: None)
        self.on_tool_result = on_tool_result or (lambda _n, _c, _e: None)
        self.on_thinking_delta = on_thinking_delta or (lambda _t: None)
        self.on_thinking_done = on_thinking_done or (lambda: None)
        self.on_truncated = on_truncated or (lambda: None)
        # Lifecycle hooks (plain attributes defaulting to None, called only if
        # set): on_run_start right after the run row opens; on_model_call just
        # before every provider.chat; on_answer_complete once the final text is
        # persisted (BEFORE memory extraction) — live row, early title, turn_complete.
        self.on_run_start = on_run_start
        self.on_model_call = on_model_call
        self.on_answer_complete = on_answer_complete
        self.on_tool_start: Optional[OnToolStart] = None  # live hook (tool name), BK-041
        self.on_compaction = None  # live hook ("start"|"done", info); chat route wires it
        #: Set by request_stop() while a tool is executing; checked after add_step
        #: so a Stop defers (keeps the step's history + audit row). Reset each
        #: run_turn. The event wakes the dispatcher off a tool that never returns.
        self._stop_requested = False
        self._stop_event = asyncio.Event()
        #: True only around a tool.run call (NOT during an approval wait) —
        #: request_stop() reads it to decide defer-vs-cancel-immediately.
        self._tool_executing = False
        #: Text deltas of the CURRENT model call — a Stop persists the partial
        #: answer (_persist_partial_on_cancel); cleared where text is durable.
        self._partial_text: list[str] = []
        #: Stats for the most recent run: usage, cost, steps, run_id.
        self.last_run: dict | None = None
        #: Run id while a turn is executing (None between turns) — read by
        #: the deferred approver to link approvals rows to their run.
        self.current_run_id: str | None = None
        #: Per-turn effort override (off | low | medium | xhigh; None = the
        #: provider default) while a turn is executing — read by
        #: browser_delegate so a browser child inherits the parent's effort.
        self.current_effort: str | None = None
        #: Session-scoped trusted-destination ledger (one Agent = one CLI
        #: session or one daemon run); the destination gate reads it via
        #: TurnContext. Seeded only from user messages, search results, and
        #: executed fetches — never tool results/page content.
        self._egress_ledger = EgressLedger()
        #: Tool-call pipeline (policy gate, execution, egress bookkeeping,
        #: audit) — core/tool_dispatch.py.
        self._dispatcher = ToolDispatcher(self)
        #: Per-turn policy context (taints + egress budget); reset by
        #: run_turn, which is the ONLY place the real TurnContext is built —
        #: a bare TurnContext() has no ledger and silently skips the
        #: destination gate.
        self._turn_ctx = TurnContext(ledger=self._egress_ledger)

    @property
    def last_turn_context(self) -> TurnContext | None:
        """The per-turn policy context from the most recent run_turn (taints +
        egress_calls). Read by browser_delegate to report a child's action
        count and propagate an internal-probe taint to the parent turn."""
        return self._turn_ctx

    async def run_turn(
        self,
        session: Session,
        user_input: str,
        trigger: str = "user",
        channel: str = "cli",
        playbook: str | None = None,
        tool_profile: list[str] | None = None,
        effort: str | None = None,
        attachments: list[dict] | None = None,
    ) -> str:
        """Run one user turn to completion (through any tool calls). ``effort``
        overrides the provider's default for every model call in this turn."""
        self.current_effort = effort
        self._stop_requested = False
        self._stop_event.clear()
        self._partial_text = []  # never carry a previous turn's text (reused Agent)
        run_id = await self._open_run(session, trigger, channel, playbook)
        self._seed_turn(session, user_input)

        total = Usage()
        cost = 0.0
        status = "ok"
        final_text = ""
        try:
            if tool_profile is not None:
                await self._apply_profile(tool_profile, run_id)
            original_input = user_input
            user_input = await self._inject_memory(user_input, session, run_id)
            await session.add_user(user_input, attachments)

            for step in range(self.max_steps):
                resp = await self._step_chat(session, step, effort, run_id, total)
                if resp.usage is not None:
                    total.add(resp.usage)
                    cost += self.provider.cost_usd(resp.usage) or 0.0
                if resp.text:
                    self.on_text(resp.text)
                    final_text = resp.text

                # max_tokens truncation: surface it, never act on it.
                if resp.stop_reason == "max_tokens":
                    return await self._finish_truncated(
                        resp, session, step, run_id, final_text
                    )
                if not resp.tool_calls:
                    return await self._finish_answer(
                        resp, session, run_id, original_input, final_text
                    )

                # Dispatch first, then persist the assistant tool_use and its
                # results together (add_step, one commit). If dispatch is
                # cancelled or raises, neither is written — the turn aborts
                # cleanly rather than stranding a tool_use the next run can't send.
                results = await self._dispatcher.dispatch_all(resp.tool_calls, run_id)
                await session.add_step(
                    resp.text,
                    resp.tool_calls,
                    results,
                    content_blocks=resp.content_blocks,
                )
                # Durable now — the deferred-Stop raise below must not rewrite.
                self._partial_text = []
                # Stop was deferred while a tool executed — now the step is
                # persisted (side effect + audit intact), unwind the turn.
                if self._stop_requested:
                    raise asyncio.CancelledError("stopped by user")

            return await self._finish_max_steps(final_text, run_id)
        except asyncio.CancelledError:
            # Stop (or daemon shutdown) mid-turn: the runs row must not finish
            # as `ok` — CancelledError bypasses `except Exception`. Text that
            # already streamed is kept: Stop stops, it does not delete.
            status = "cancelled"
            await self._persist_partial_on_cancel(session, run_id)
            raise
        except Exception as e:
            status = "error"
            await self._trace(run_id, "error", {"error": str(e)})
            raise
        finally:
            self.current_run_id = None
            self.current_effort = None
            if self.run_store is not None and run_id is not None:
                await self.run_store.finish(run_id, status, total, cost)
            self.last_run = {
                "run_id": run_id,
                "usage": total,
                "cost_usd": cost,
            }

    async def _open_run(
        self,
        session: Session,
        trigger: str,
        channel: str,
        playbook: str | None,
    ) -> str | None:
        run_id = None
        if self.run_store is not None:
            run_id = await self.run_store.start(
                session.id,
                model=self.provider.model,
                trigger=trigger,
                channel=channel,
                playbook=playbook,
            )
        self.current_run_id = run_id
        if self.on_run_start is not None:
            self.on_run_start(run_id)
        return run_id

    def _seed_turn(self, session: Session, user_input: str) -> None:
        # Taints + egress budget reset per turn; the ledger persists for the
        # session. Prior USER messages re-seed it (the daemon builds a fresh
        # agent per run, so user-named domains must survive from history);
        # assistant text and tool results are never seeded — post-injection
        # model output must not legitimize destinations.
        self._turn_ctx = TurnContext(ledger=self._egress_ledger)
        for msg in session.messages:
            if msg.role == "user" and msg.text:
                self._egress_ledger.seed_text(msg.text)
        self._egress_ledger.seed_text(user_input)

    async def _apply_profile(self, tool_profile: list[str], run_id: str | None) -> None:
        unknown = self.toolset.set_profile(tool_profile)
        await self._trace(
            run_id,
            "tool_profile",
            {"tools": [t.name for t in self.toolset.tools()], "unknown": unknown},
        )

    async def _inject_memory(
        self, user_input: str, session: Session, run_id: str | None
    ) -> str:
        # First turn: freeze the always-on profile into the session (rides
        # the END of the system prompt — see _system_for) and prepend the
        # similar-facts block. The extractor already holds the raw input.
        if self.memory is not None and not session.messages:
            user_input, pc, bc = await inject_first_turn(self.memory, session, user_input)
            await self._trace(
                run_id, "memory_injected", {"profile_chars": pc, "block_chars": bc}
            )
        return user_input

    def _system_for(self, session: Session) -> str:
        # The frozen profile rides the END of the system prompt (identical
        # bytes every step + every later run); falsy NULL/"" sends base.
        profile = session.memory_profile
        return f"{self.system_prompt}\n\n{profile}" if profile else self.system_prompt

    async def _step_chat(
        self, session: Session, step: int, effort: str | None, run_id: str | None, total: Usage
    ):
        if self.compactor is not None:
            await self.compactor.maybe_compact(  # traces, totals usage, hooks
                session, run_id, self.run_store, total, self.on_compaction)
        tools = self.toolset.tools()
        if self.on_model_call is not None:
            self.on_model_call()
        self._partial_text = []
        resp = await self.provider.chat(
            self._system_for(session),
            session.messages,
            tools,
            on_text_delta=self._capture_delta,
            on_thinking_delta=self.on_thinking_delta,
            on_thinking_done=self.on_thinking_done,
            on_tool_start=self.on_tool_start,
            effort=effort,
        )
        # NB: no clear here — a dispatch-time cancel must keep this text.
        await self._trace(
            run_id,
            "model_response",
            {
                "step": step,
                "text_chars": len(resp.text or ""),
                "tool_calls": [tc.name for tc in resp.tool_calls],
                "tools_sent": len(tools),
                "usage": asdict(resp.usage) if resp.usage else None,
            },
        )
        return resp

    async def _finish_truncated(self, resp, session, step, run_id, final_text) -> str:
        # A partial tool_use input is garbage — dispatching it would run a real
        # action on a half-written argument dict — and a truncated assistant
        # message is not persisted with its raw blocks (they can end
        # mid-thinking). No auto-retry: cost stays visible.
        await self._trace(
            run_id,
            "truncated",
            {
                "step": step,
                "text_chars": len(resp.text or ""),
                "tool_calls": [tc.name for tc in resp.tool_calls],
            },
        )
        self.on_truncated()
        if resp.text and not resp.tool_calls:
            # Truncated prose is still real content — keep it.
            await session.add_assistant(resp.text, [])
            self._partial_text = []  # durable (truncation never raises cancel, but pin it)
        return final_text

    async def _finish_answer(
        self, resp, session, run_id, original_input: str, final_text: str
    ) -> str:
        await session.add_assistant(
            resp.text, resp.tool_calls, content_blocks=resp.content_blocks
        )
        self._partial_text = []  # durable; a cancel in the naming tail re-writes nothing
        if self.on_answer_complete is not None:
            self.on_answer_complete()
        if self.memory_extractor is not None and final_text:
            # extract() traces its own failures; it never raises.
            await self.memory_extractor.extract(run_id, original_input, final_text)
        return final_text

    async def _finish_max_steps(self, final_text: str, run_id: str | None) -> str:
        # Hit the step ceiling without a plain-text answer. Surface the notice
        # on every channel: on_text_delta reaches the CLI, chat SSE, and
        # background live streams, and folding it into final_text puts it in
        # the done event / run_done / push body.
        note = "[stopped: reached max tool-call steps for this turn]"
        await self._trace(run_id, "max_steps", {"steps": self.max_steps})
        self.on_text_delta(("\n\n" if final_text else "") + note)
        self.on_text(note)
        return f"{final_text}\n\n{note}".strip() if final_text else note

    def request_stop(self) -> bool:
        """User Stop for the turn in flight. Returns True when the caller may
        cancel the turn task immediately (a mid-model-stream cancel just
        closes the request, freeing the model slot). If a tool is EXECUTING,
        cancelling would lose both its side effect from history and its audit
        row (`_audit` runs after `_gate_and_run`), so this sets a flag the
        loop checks right after add_step and returns False; the turn then
        raises CancelledError itself, once the step is persisted."""
        if self._tool_executing:
            self._stop_requested = True
            self._stop_event.set()
            return False
        return True

    def _capture_delta(self, t: str) -> None:
        # Provider-facing hook: accumulate for the cancel path, then forward.
        # Only TEXT deltas flow here (thinking stays ephemeral; a half-
        # streamed tool_use is never persisted — nothing would pair it).
        self._partial_text.append(t)
        self.on_text_delta(t)

    async def _persist_partial_on_cancel(
        self, session: Session, run_id: str | None
    ) -> None:
        """Stop must not drop the streamed answer: persist the partial as a
        plain assistant message (text only — no dangling tool_use).
        Best-effort: the shield survives the single Stop cancel. Whitespace
        writes nothing, and so does a trailing assistant message — that means
        the normal path already persisted this text (a cancel racing inside
        add_assistant; writing again would leave two consecutive assistant
        rows and poison the chat's role alternation forever)."""
        partial = "".join(self._partial_text)
        self._partial_text = []
        last = session.messages[-1] if session.messages else None
        if not partial.strip() or (last is not None and last.role == "assistant"):
            return
        try:
            await asyncio.shield(session.add_assistant(partial, []))
        except asyncio.CancelledError:
            print("[agent] cancelled mid partial-persist", file=sys.stderr)
        except Exception:
            traceback.print_exc()
        else:
            await self._trace(run_id, "partial_persisted", {"chars": len(partial)})

    async def _trace(self, run_id: str | None, kind: str, data: dict) -> None:
        # Best-effort: a failed trace write must not abort the turn (or,
        # mid-dispatch, strand a tool_use without its result).
        await trace_event(self.run_store, run_id, kind, data, source="agent")
