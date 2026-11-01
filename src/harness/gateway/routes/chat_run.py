"""The interactive chat turn: request model, event plumbing, and the SSE
response that carries it.

Chat streams over its own POST response: the per-request agent's hooks push
JSON events into a queue that the response generator drains. A client
disconnect (PWA suspension kills the socket) DETACHES the run instead of
cancelling it: the turn finishes and persists, its remaining events reroute
to the broadcaster tagged with session_id, and a terminal run_done is
published so a returning client can refetch the transcript. Each event goes
to exactly ONE sink (queue while attached, broadcaster after detach) — no
client dedupe.

Every event leaving `emit()` is stamped with `seq` (monotonic per run) and
`run_id` ONCE, here, before it reaches any sink, and copied to the run's
in-memory ring (gateway/event_ring.py) — so the attached queue, the detach
reroute, the broadcaster and GET /api/chat/{sid}/live all carry identical
numbers for the same event. The per-connection `session` handshake is not a
run event: it carries no seq and never enters the ring.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from dataclasses import dataclass, field
from typing import Callable, Literal

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...core.agent import Agent
from ...core.policy import Autonomy
from ...core.session import Session
from ...core.tool_dispatch import CARD_DIGEST_CHARS
from ..event_ring import EventRing
from ..live import LivePhase
from ..model_status import SlotWatcher, make_slots_fetch
from ..state import Gateway
from ._sse import compaction_event, sse

# Chat SSE heartbeat interval. The local lane queues behind another client's
# request on the shared model slot with ZERO bytes on this wire for minutes
# (observed ~3.5 min, 2026-10-01) — well past what tailscale serve tolerates
# idle, which the PWA then read as a lost connection. SSE comment lines are
# the cheapest way to keep bytes moving. Tests monkeypatch this constant.
CHAT_HEARTBEAT_S = 15.0


class ChatRequest(BaseModel):
    text: str
    session_id: str | None = None
    # The web UI's effort picker (anthropic_provider.EFFORTS); None = the
    # provider default (HARNESS_EFFORT). Anything else is a 422.
    effort: Literal["off", "low", "medium", "xhigh"] | None = None
    # The opt-in Sonnet chat option — the only non-local model anywhere.
    # Pinned to medium effort server-side; 422 without ANTHROPIC_API_KEY.
    model: Literal["sonnet"] | None = None
    # Media ids from POST /api/media (gateway/media.py). The route resolves
    # each to {id, mime, path, name, size} BEFORE the run starts (unknown id
    # -> 404); the stored attachment never carries the url the upload
    # returned.
    attachment_ids: list[str] = []


def _fallback_title(text: str, max_chars: int = 40) -> str:
    """A deterministic chat title from the first message when the namer is
    unavailable — first non-empty line, whitespace-collapsed and bounded, so a
    chat is never stuck on 'New chat' and Runs never show a bare 'user'."""
    snippet = " ".join((text or "").split())[:max_chars].strip()
    return snippet or "New chat"


@dataclass
class ChatRun:
    """A started chat turn, decoupled from the HTTP response that spawned it.

    `task` is None until the coroutine is created (the watcher/title wiring
    closes over the ChatRun before `asyncio.create_task` runs), then filled in.
    `answer_complete` flips the moment on_answer_complete fires so the Stop
    endpoint can answer 409 "already finishing" during the naming tail rather
    than cancelling a turn whose user-visible work is done.

    `ring` holds every event this run emitted (stamped, oldest dropped past
    capacity). It is the replay source for GET /api/chat/{sid}/live and lives
    and dies with the gw.chat_runs/active_chats entries — never durable.

    `persisted_seq` is the run's PERSISTED watermark — the highest `seq` the
    store now covers, refreshed whenever a transcript write returns (see the
    wrap in start_chat_run). /messages replays live FROM here, so its events
    are exactly the UNPERSISTED tail: never a step already in the transcript.
    """

    session: Session
    queue: asyncio.Queue[dict | None]
    detach: Callable[[], None]
    agent: Agent
    ring: EventRing
    #: Start time + last row-setting event, for a mid-run join (BK-041 G4).
    phase: LivePhase = field(default_factory=LivePhase)
    task: asyncio.Task | None = None
    answer_complete: bool = False
    persisted_seq: int = 0


def _surfaced(label: str) -> Callable[[asyncio.Task], None]:
    """Done-callback for the auxiliary chat tasks (watcher, title). Belt-and-
    braces per the no-silent-task-death rule: these catch their own failures,
    so anything reaching here is a bug worth printing."""

    def cb(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            print(f"[chat] {label} task died:", file=sys.stderr)
            traceback.print_exception(t.exception())

    return cb


async def start_chat_run(
    gw: Gateway,
    text: str,
    session_id: str | None,
    effort: str | None = None,
    model: str | None = None,
    attachments: list[dict] | None = None,
) -> ChatRun:
    """Spawn a chat turn whose events feed the returned queue while the client
    is attached, and reroute to the broadcaster (tagged session_id) after
    detach(). Module-level so disconnect behavior is testable without holding
    an SSE response open (httpx ASGITransport buffers streams).

    model="sonnet" builds the turn's agent on the opt-in cloud chat provider
    (utility/compaction stay local); it is rejected before any session is
    created when the host has no key."""
    if model == "sonnet":
        if not gw.sonnet_available:
            raise HTTPException(
                422, "Sonnet is not available on this host (no API key)"
            )
        effort = None  # Sonnet is pinned to medium (the provider default)
    # One writer per session: a live chat turn OR a background run owns it.
    if (busy := gw.busy_reason(session_id)) is not None:
        raise HTTPException(409, busy)

    session = None
    if session_id:
        session = await Session.resume(gw.sessions, session_id)
    # Auto-name ONLY a genuinely new chat (no id supplied). A non-empty but
    # stale/unknown id that fails to resume must NOT be treated as new —
    # otherwise a typo silently forks a fresh, auto-named session.
    created = not session_id
    if session is None:
        session = await Session.create(gw.sessions, channel="web")

    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    ring = EventRing()
    attached = True
    # The run's id, captured at on_run_start and KEPT (Agent clears its own
    # once run_turn returns — a `title` event can legitimately arrive after
    # `stopped`). Events emitted before on_run_start carry run_id: None.
    run_id: str | None = None

    phase = LivePhase()

    def emit(ev: dict) -> None:
        # Stamp once HERE, before routing to any sink: the queue payload, the
        # detach reroute, the broadcaster payload and the ring copy all carry
        # this event's seq/run_id. The ring is written here rather than via
        # the publisher because publish() is drop-on-full — a publisher-fed
        # ring would go silently lossy exactly when backpressure hits.
        ev = ring.stamp(ev, run_id)
        phase.observe(ev)
        if attached:
            queue.put_nowait(ev)
        elif gw.broadcaster is not None:
            gw.broadcaster.publish({**ev, "session_id": session.id})

    def detach() -> None:
        nonlocal attached
        if not attached:
            return
        attached = False
        # Events already sitting in the queue were written toward a socket that
        # is gone; leaving them there strands the head of the turn (the
        # recovered view starts mid-answer and stays that way until the final
        # refetch). Reroute them in order — but leave a never-consumed terminal
        # `None` on the queue, so a generator that is somehow still attached can
        # end instead of waiting forever (a stuck task holds active_chats and
        # 409s every later message on this session).
        reroute: list[dict] = []
        tail: list[None] = []
        while not queue.empty():
            ev = queue.get_nowait()
            (tail if ev is None else reroute).append(ev)
        for _ in tail:
            queue.put_nowait(None)
        if gw.broadcaster is not None:
            for ev in reroute:
                gw.broadcaster.publish({**ev, "session_id": session.id})

    # Only the Sonnet option passes chat_model; every other run (and every
    # background caller of agent_factory) is the local lane.
    agent = (
        gw.agent_factory(Autonomy.INTERACTIVE, chat_model=model)
        if model
        else gw.agent_factory(Autonomy.INTERACTIVE)
    )
    chat_run = ChatRun(
        session=session, queue=queue, detach=detach, agent=agent, ring=ring,
        phase=phase,
    )

    # Persisted watermark for /messages (Step 4 amendment): the run's
    # transcript rows ALL land via session.add_step/add_assistant, and the
    # agent persists AFTER each call's events fire — so watermarking at the
    # Session boundary makes persisted_seq exactly "what the store covers",
    # and /messages replays live FROM there: the unpersisted tail, never a
    # completed step replayed. At the boundary rather than as an
    # agent.on_step_persisted hook because agent.py sits at the 420-cap AND
    # the boundary cannot miss a call site (truncation and cancel partials
    # flow through it too). add_user stays unwrapped — a user row has no run
    # event to overlap with. Tests: ToolLoopAgent in tests/test_chat_recovery.
    def _watermark_on(persist):
        async def wrapper(*args, **kwargs):
            out = await persist(*args, **kwargs)
            chat_run.persisted_seq = ring.seq
            return out

        return wrapper

    session.add_step = _watermark_on(session.add_step)
    session.add_assistant = _watermark_on(session.add_assistant)

    # Live model status: poll llama.cpp's /slots while the turn waits on the
    # shared model slot. Sonnet turns skip it — their tokens never touch the
    # local server; model_wait fires on both.
    watcher = (
        SlotWatcher(make_slots_fetch(gw.local_url), emit)
        if gw.local_url and model != "sonnet"
        else None
    )
    watcher_task: asyncio.Task | None = None
    title_task: asyncio.Task | None = None

    def _live(ev: dict) -> None:
        # First bytes of OUR request — the status row defers to deltas now.
        if watcher is not None:
            watcher.mark_output()
        emit(ev)

    agent.on_text_delta = lambda t: _live({"type": "delta", "text": t})
    agent.on_tool_call = lambda n, a: _live({"type": "tool_call", "tool": n, "args": a})
    agent.on_thinking_delta = lambda t: _live({"type": "thinking_delta", "text": t})
    # BK-041 G3: a tool_use block began — "Preparing <tool>…" while its args stream.
    agent.on_tool_start = lambda n: _live({"type": "tool_start", "tool": n})
    agent.on_tool_result = lambda n, c, e: emit(
        {"type": "tool_result", "tool": n, "is_error": e,
         "digest": c[:CARD_DIGEST_CHARS], "chars": len(c)}
    )
    agent.on_thinking_done = lambda: emit({"type": "thinking_done"})
    agent.on_truncated = lambda: emit({"type": "truncated"})
    agent.on_compaction = lambda phase, info=None: emit(compaction_event(phase, info))

    def start_title(run_id: str | None) -> None:
        """Kick off auto-naming as its own task, once. Normally fired from
        on_run_start so the title lands WHILE the answer streams (the old
        after-run_turn naming surfaced it seconds late behind memory
        extraction); agents that never fire the hook (test fakes) start it
        after run_turn instead. Awaiting it is shielded in run(), and the task
        is deliberately never cancelled — a titled chat beats a stopped one
        (a title event can legitimately arrive after `stopped`)."""
        nonlocal title_task
        if not created or title_task is not None:
            return

        async def _name() -> str:
            title = None
            # On-device Apple FM; it traces its own failures but must never
            # take the chat stream with it.
            if gw.namer is not None:
                try:
                    title = await gw.namer.name(session.id, text, run_id=run_id)
                except Exception:
                    traceback.print_exc()
            if not title:
                title = _fallback_title(text)
                await gw.sessions.set_title_if_unset(session.id, title)
            emit({"type": "title", "title": title})
            return title

        title_task = asyncio.create_task(_name())
        title_task.add_done_callback(_surfaced("chat title"))

    def on_run_start(new_run_id: str | None) -> None:
        nonlocal run_id, watcher_task
        # From here on every event this closure emits carries this run_id —
        # the number a resyncing client keys its last-seen `seq` against.
        run_id = new_run_id
        start_title(new_run_id)
        if watcher is not None and watcher_task is None:
            watcher_task = asyncio.create_task(watcher.watch())
            watcher_task.add_done_callback(_surfaced("slot watcher"))

    def on_model_call() -> None:
        # Each model call re-arms the wait: the queue can form again between
        # steps (tool loop), not just at turn start.
        emit({"type": "model_wait"})
        if watcher is not None:
            watcher.mark_waiting()

    def on_answer_complete() -> None:
        # The turn's user-visible work is done — drop the "working" indicator
        # BEFORE run_turn's memory-extraction tail, not after.
        chat_run.answer_complete = True
        emit({"type": "turn_complete"})

    agent.on_run_start = on_run_start
    agent.on_model_call = on_model_call
    agent.on_answer_complete = on_answer_complete

    async def run() -> None:
        try:
            text_out = await agent.run_turn(
                session, text, trigger="user", channel="web", effort=effort,
                attachments=attachments,
            )
            if not chat_run.answer_complete:
                # Hook-capable agents fire turn_complete inside run_turn;
                # fakes (and any future agent without hooks) get it here.
                emit({"type": "turn_complete"})
            if created and title_task is None:
                start_title((agent.last_run or {}).get("run_id"))
            # The title normally arrived long before this; the shield covers
            # the naming tail (a disconnect mid-name still persists it).
            title = await asyncio.shield(title_task) if title_task else None
            emit({"type": "done", "text": text_out, "title": title})
        except asyncio.CancelledError:
            # Stop button, or daemon shutdown. Say so on the wire so the
            # client drops the spinner without rendering an error; the turn's
            # runs row was already marked `cancelled` in Agent.run_turn.
            emit({"type": "stopped"})
            raise
        except Exception as e:  # surfaced to the client, not swallowed
            emit({"type": "error", "error": str(e)})
        finally:
            if watcher_task is not None and not watcher_task.done():
                watcher_task.cancel()
            queue.put_nowait(None)
            # A detached client has no queue to read `done`/`error` from —
            # run_done on the shared stream is its terminal signal (the UI
            # refetches the persisted transcript on it). Routed through emit
            # (the `not attached` guard makes it the broadcaster branch) so it
            # carries this run's seq/run_id like every other event. Note it
            # lands in the ring with no reader: emit returns, run() returns and
            # the task is done before any other coroutine runs, so /live's
            # task-done gate answers {active: false} — the terminal event is
            # consumed on /api/events, and the heal is the persisted refetch.
            if not attached and gw.broadcaster is not None:
                emit({"type": "run_done"})

    task = asyncio.create_task(run())
    chat_run.task = task
    gw.active_chats[session.id] = task
    gw.chat_runs[session.id] = chat_run

    def _reap(t: asyncio.Task) -> None:
        if gw.active_chats.get(session.id) is t:
            del gw.active_chats[session.id]
        if gw.chat_runs.get(session.id) is chat_run:
            del gw.chat_runs[session.id]
        # run() catches into the `error` event; this is belt-and-braces per
        # the no-silent-task-death rule (e.g. a bug in run() itself).
        if not t.cancelled() and t.exception() is not None:
            traceback.print_exception(t.exception())

    task.add_done_callback(_reap)
    return chat_run


def chat_sse_response(run: ChatRun) -> StreamingResponse:
    """Drain the run's queue as SSE; detach (never cancel) on disconnect."""

    async def stream():
        yield sse({"type": "session", "session_id": run.session.id})
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        run.queue.get(), timeout=CHAT_HEARTBEAT_S
                    )
                except asyncio.TimeoutError:
                    # Heartbeat: a local-lane queue behind another client's
                    # request idles for minutes with no bytes of its own —
                    # without pings tailscale serve drops the stream and the
                    # PWA cries "connection lost" mid-turn.
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield sse(event)
        finally:
            # Client disconnected mid-run (or the run ended): DETACH, never
            # cancel — the turn finishes and persists, remaining events go
            # to the broadcaster. The active_chats 409 guard is what keeps
            # concurrent same-session turns out, not cancellation.
            run.detach()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # Same anti-buffering pair as /api/events — without these a proxy
        # (or Safari) can hold the whole stream until it closes.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
