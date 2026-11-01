"""The orchestrator: turns events into agent runs.

Consumes the EventBus forever. Each run gets a fresh Agent from the injected
factory, wired for the event's autonomy level — a cron briefing runs
supervised (deferred approvals), a sub-run would be restricted. A semaphore
caps concurrent runs to bound RAM and API spend.

Spawned run tasks must never die silently (the daemon's primary failure
mode): every task gets a done-callback that prints the traceback, and the
run row already records status=error via the agent loop.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from .. import playbooks
from ..store.sessions import SessionStore
from .agent import Agent
from .approvals import ApprovalWaiter
from .events import ApprovalDecision, CronFired, EventBus, UserMessage, WatchHit
from .policy import Autonomy
from .session import Session
from .tool_dispatch import CARD_DIGEST_CHARS

MAX_CONCURRENT_RUNS = 2

# Which playbook handles hits from each watcher.
WATCH_PLAYBOOKS = {"gmail": "email_triage"}

AgentFactory = Callable[[Autonomy], Agent]
# (title, final_text, channel, session_id) — the gateway hooks notifications
# here; session_id lets a notification deep-link to its conversation.
# On success it fires for WATCHER runs only: scheduled (cron) runs are
# deliberately silent on completion — a job that needs to alert the user calls
# the notify tool. A cron or watcher run that RAISES fires it once with a
# "<title> — run failed" title.
OnRunComplete = Callable[[str, str, str, str | None], Awaitable[None]]
# A live UI event for a background run (carries its own session_id). The
# gateway hooks this to the RunBroadcaster so /api/events streams it; unlike
# interactive chat, a triggered run has no per-request SSE of its own.
OnRunEvent = Callable[[dict], None]
# Builds a per-run local-lane slot watcher around an emit (agentd passes a
# gateway.model_status.SlotWatcher factory; None off the local lane / in tests).
# Duck-typed: mark_waiting(), mark_output(), async watch().
SlotWatcherFactory = Callable[[OnRunEvent], Any]


class Orchestrator:
    def __init__(
        self,
        bus: EventBus,
        sessions: SessionStore,
        agent_factory: AgentFactory,
        waiter: ApprovalWaiter,
        on_run_complete: Optional[OnRunComplete] = None,
        on_run_event: Optional[OnRunEvent] = None,
        slot_watcher: Optional[SlotWatcherFactory] = None,
    ) -> None:
        self.bus = bus
        self.sessions = sessions
        self.agent_factory = agent_factory
        self.waiter = waiter
        self.on_run_complete = on_run_complete
        self.on_run_event = on_run_event
        self.slot_watcher = slot_watcher
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._tasks: set[asyncio.Task] = set()
        #: Session ids a cron/watcher run is writing RIGHT NOW — the single
        #: source of background ownership. agentd shares this exact set with
        #: the Gateway, whose chat route 409s a send to any of them: a second
        #: writer keeps its own Session._next_idx and the two collide on
        #: messages.idx (the 2026-10-25 background run died that way). Chat
        #: runs never register here; they have gw.active_chats.
        self.background_sessions: set[str] = set()

    async def run(self) -> None:
        """The daemon's event loop task."""
        while True:
            await self.handle(await self.bus.get())

    async def handle(self, event) -> None:
        if isinstance(event, ApprovalDecision):
            # Resolved inline: a waiting run resumes immediately, and this
            # must never queue behind the run semaphore.
            await self.waiter.resolve(event.approval_id, event.approved)
        elif isinstance(event, CronFired):
            self._spawn(self._scheduled_run(event), f"cron:{event.trigger_id}")
        elif isinstance(event, UserMessage):
            self._spawn(self._chat_run(event), "user message")
        elif isinstance(event, WatchHit):
            self._spawn(self._watch_run(event), f"watch:{event.watcher}")

    async def _scheduled_run(self, event: CronFired) -> None:
        async with self._semaphore:
            job = playbooks.resolve_job(event.playbook, event.tools, event.autonomy)
            if job.downgraded:
                print(
                    f"[orchestrator] trigger {event.trigger_id}: {job.downgraded}",
                    file=sys.stderr,
                )
            pb = job.playbook
            title = event.label or pb.name or pb.prompt[:60]
            now = datetime.now(timezone.utc).astimezone()
            header = (
                f"[Scheduled run · {now.strftime('%A %Y-%m-%d %H:%M %Z')}"
                + (" · LATE catch-up: this fired after the machine was asleep"
                   if event.late else "")
                + "]\n\n"
            )
            session = await Session.create(
                self.sessions, channel=event.channel, title=title
            )
            agent = self.agent_factory(Autonomy(job.autonomy))
            end_live = self._wire_live(
                agent, session.id, title, f"cron:{event.trigger_id}"
            )
            # No await since Session.create: nothing can have seen the id yet.
            self.background_sessions.add(session.id)
            text = None
            try:
                text = await agent.run_turn(
                    session,
                    header + pb.prompt,
                    trigger=f"cron:{event.trigger_id}",
                    channel=event.channel,
                    playbook=pb.name,
                    tool_profile=job.tools,
                )
            except Exception as e:
                await self._notify_failure(title, e, event.channel, session.id)
                raise
            finally:
                self.background_sessions.discard(session.id)
                end_live(text)
            # No completion push for scheduled runs — jobs alert via notify.

    async def _watch_run(self, event: WatchHit) -> None:
        async with self._semaphore:
            playbook_name = WATCH_PLAYBOOKS.get(event.watcher)
            pb = playbooks.resolve(
                playbook_name or f"A watcher ({event.watcher}) raised an event; "
                "assess it and summarize briefly for a push notification."
            )
            # Payload content (email headers/snippets) is untrusted data —
            # delimited and labelled, appended to the user turn, never the
            # system prompt.
            payload_lines = "\n".join(f"{k}: {v}" for k, v in event.payload.items())
            title = _watch_title(event)
            session = await Session.create(self.sessions, channel="web", title=title)
            agent = self.agent_factory(Autonomy.SUPERVISED)
            end_live = self._wire_live(agent, session.id, title, f"watch:{event.watcher}")
            self.background_sessions.add(session.id)
            text = None
            try:
                text = await agent.run_turn(
                    session,
                    f"{pb.prompt}\n\n<watch_event watcher=\"{event.watcher}\">\n"
                    f"{payload_lines}\n</watch_event>",
                    trigger=f"watch:{event.watcher}",
                    channel="web",
                    playbook=pb.name,
                    tool_profile=pb.tools,
                )
            except Exception as e:
                await self._notify_failure(title, e, "web", session.id)
                raise
            finally:
                self.background_sessions.discard(session.id)
                end_live(text)
            await self._notify(title, text, "web", session.id)

    async def _chat_run(self, event: UserMessage) -> None:
        async with self._semaphore:
            if event.session_id is not None:
                session = await Session.resume(self.sessions, event.session_id)
            else:
                session = None
            if session is None:
                session = await Session.create(self.sessions, channel=event.channel)
            agent = self.agent_factory(Autonomy.INTERACTIVE)
            await agent.run_turn(
                session, event.text, trigger="user", channel=event.channel
            )

    def _wire_live(
        self, agent: Agent, session_id: str, title: str, trigger: str
    ) -> Callable[[str | None], None]:
        """Forward this run's streaming callbacks to the live event hub, tagged
        with its session so the UI renders them into the right conversation.
        Returns the run's end hook (call it in `finally` with the final text):
        it publishes run_done and stops the slot watcher. No-op when no hub is
        wired (e.g. the CLI, or tests).

        run_start is deferred to the first live event — in practice the first
        model call, which follows the user turn's persist (BK-041 G2a: an
        auto-opened chat fetches /messages on run_start; published earlier,
        the blue prompt is missing until the run ends)."""
        if self.on_run_event is None:
            return lambda _t: None
        hub = self.on_run_event
        started = False
        watch_task: asyncio.Task | None = None

        def emit(ev: dict) -> None:
            nonlocal started
            if not started:
                started = True
                hub({"type": "run_start", "session_id": session_id,
                     "title": title, "trigger": trigger})
            hub({**ev, "session_id": session_id})

        watcher = self.slot_watcher(emit) if self.slot_watcher else None

        def live(ev: dict) -> None:
            if watcher is not None:
                watcher.mark_output()  # our bytes arrived: deltas own the row
            emit(ev)

        def on_model_call() -> None:
            nonlocal watch_task
            emit({"type": "model_wait"})
            if watcher is None:
                return
            watcher.mark_waiting()
            if watch_task is None:
                watch_task = asyncio.create_task(watcher.watch())
                watch_task.add_done_callback(_surfaced(f"slot watcher ({trigger})"))

        def end(text: str | None) -> None:
            if watch_task is not None and not watch_task.done():
                watch_task.cancel()
            emit({"type": "run_done", "text": text or ""})

        agent.on_model_call = on_model_call
        agent.on_text_delta = lambda t: live({"type": "delta", "text": t})
        agent.on_tool_call = lambda n, a: live(
            {"type": "tool_call", "tool": n, "args": a}
        )
        agent.on_tool_start = lambda n: live({"type": "tool_start", "tool": n})
        agent.on_tool_result = lambda n, c, e: emit(
            {"type": "tool_result", "tool": n, "is_error": e,
             "digest": c[:CARD_DIGEST_CHARS], "chars": len(c)}
        )
        agent.on_thinking_delta = lambda t: live({"type": "thinking_delta", "text": t})
        agent.on_thinking_done = lambda: emit({"type": "thinking_done"})
        agent.on_truncated = lambda: emit({"type": "truncated"})
        return end

    async def _notify(
        self, title: str, text: str, channel: str, session_id: str | None
    ) -> None:
        if self.on_run_complete is None:
            return
        try:
            await self.on_run_complete(title, text, channel, session_id)
        except Exception:
            print(
                f"[orchestrator] completion notification failed for {title!r}:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )

    async def _notify_failure(
        self, title: str, e: Exception, channel: str, session_id: str
    ) -> None:
        """A background run that RAISED pushes once, deep-linked to its
        session — the 2026-10-25 background run died silently. Callers re-raise,
        so _spawn's traceback and the runs row's status=error are unchanged.
        Only Exception: a CancelledError (Stop, shutdown) stays quiet, and
        scheduled runs stay silent on success."""
        body = f"{type(e).__name__}: {e}"[:200]
        await self._notify(f"{title} — run failed", body, channel, session_id)

    def _spawn(self, coro, context: str) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                print(
                    f"[orchestrator] run failed ({context}):",
                    file=sys.stderr,
                )
                traceback.print_exception(t.exception(), file=sys.stderr)

        task.add_done_callback(_done)
        return task

    def cancel_inflight(self) -> None:
        """Cancel every in-flight run (daemon shutdown)."""
        for task in list(self._tasks):
            task.cancel()

    async def drain(self) -> None:
        """Wait for in-flight runs (shutdown and tests)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def _surfaced(label: str) -> Callable[[asyncio.Task], None]:
    """Done-callback for a run's auxiliary task: never let it die silently."""

    def cb(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            print(f"[orchestrator] {label} task died:", file=sys.stderr)
            traceback.print_exception(t.exception(), file=sys.stderr)

    return cb


def _watch_title(event: WatchHit) -> str:
    label = event.payload.get("label", "").title()
    subject = event.payload.get("subject", "")
    if label and subject:
        return f"{label} email: {subject[:60]}"
    return f"Watcher event: {event.watcher}"
