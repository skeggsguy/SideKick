"""Gateway API over ASGI: chat SSE, approvals, triggers, runs."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from harness.core.approvals import ApprovalWaiter
from harness.core.events import CronFired, EventBus
from harness.gateway.app import Gateway, create_app
from harness.gateway.broadcast import RunBroadcaster
from harness import playbooks
from harness.memory import MemoryStore
from harness.store.approvals import ApprovalStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore
from conftest import gotcha


class FakeAgent:
    """Streams a delta then finishes, mimicking the real loop's hooks.

    With a `gate`, the turn parks between the two deltas until the test sets
    it — used to detach/409 a run mid-flight."""

    def __init__(
        self,
        thinking: list[str] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.on_text_delta = lambda t: None
        self.on_tool_call = lambda n, a: None
        self.on_tool_result = lambda n, c, e: None
        self.on_thinking_delta = lambda t: None
        self.on_thinking_done = lambda: None
        self.on_tool_start = lambda n: None
        self.on_truncated = lambda: None
        # Lifecycle hooks: the real Agent calls these; the fake never does,
        # which exercises the route's post-run_turn fallback wiring instead.
        self.on_run_start = None
        self.on_model_call = None
        self.on_answer_complete = None
        self.thinking = thinking or []
        self.gate = gate
        self.current_run_id = None
        self.last_run: dict | None = None
        self.kwargs: dict | None = None

    def request_stop(self) -> bool:
        # The fake never executes tools, so a Stop is always immediate-safe.
        return True

    async def run_turn(self, session, user_input, **kwargs):
        self.kwargs = kwargs
        await session.add_user(user_input)
        for chunk in self.thinking:
            self.on_thinking_delta(chunk)
        if self.thinking:
            self.on_thinking_done()
            self.on_tool_start("web_search")  # BK-041 G3: a tool_use block began
        self.on_text_delta("hello ")
        if self.gate is not None:
            await self.gate.wait()
        self.on_text_delta("world")
        await session.add_assistant("hello world")
        return "hello world"


@pytest.fixture
async def client(db):
    memory = MemoryStore(db)  # no embedder: keyword search path
    await memory.setup()
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,  # push endpoints not exercised here
        agent_factory=lambda autonomy: FakeAgent(),
        memory=memory,
        broadcaster=RunBroadcaster(),
        tool_catalog=[
            {"name": "schedule_list", "description": "", "classification": "READ_ONLY", "remote": False},
            {"name": "calendar_event_create", "description": "", "classification": "MUTATING_LOCAL", "remote": True},
            {"name": "schedule_edit", "description": "", "classification": "MUTATING_LOCAL", "remote": False},
        ],
    )
    app = create_app(gw)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    ) as c:
        c.gw = gw
        yield c


async def test_health(client):
    # `sonnet` gates the Sonnet chat entry; the test Gateway has no key.
    resp = await client.get("/api/health")
    assert resp.json() == {"ok": True, "sonnet": False, "name": None}
    client.gw.sonnet_available = True
    client.gw.user_name = "Sam"
    resp = await client.get("/api/health")
    assert resp.json()["sonnet"] is True
    assert resp.json()["name"] == "Sam"  # the web UI greeting's HARNESS_USER_NAME


async def test_chat_sonnet_gated(client):
    """Sonnet without a host key is a 422 before any session is created;
    unknown models never reach the factory (Literal rejects at validation)."""
    resp = await client.post("/api/chat", json={"text": "hi", "model": "sonnet"})
    assert resp.status_code == 422 and "not available" in resp.json()["detail"]
    resp = await client.post("/api/chat", json={"text": "hi", "model": "haiku"})
    assert resp.status_code == 422  # pydantic Literal


async def test_chat_sonnet_selects_provider_and_pins_effort(client):
    """chat_model rides the factory (opt-in Sonnet agent); the picker effort
    is dropped server-side so the provider's pinned medium applies."""
    seen: dict = {}
    agent = FakeAgent()

    def factory(autonomy, chat_model=None):
        seen["chat_model"] = chat_model
        return agent

    client.gw.agent_factory = factory
    client.gw.sonnet_available = True
    resp = await client.post(
        "/api/chat",
        json={"text": "hi", "model": "sonnet", "effort": "xhigh"},
    )
    assert resp.status_code == 200
    assert seen["chat_model"] == "sonnet"
    assert agent.kwargs["effort"] is None  # pinned to the provider's medium


async def test_chat_streams_session_deltas_done(client):
    events = []
    async with client.stream(
        "POST", "/api/chat", json={"text": "hi", "session_id": None}
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    kinds = [e["type"] for e in events]
    # A new chat now emits its own `title` event (early naming) between
    # turn_complete and the finite `done`. FakeAgent never fires the
    # on_answer_complete/on_run_start hooks, so turn_complete comes from the
    # fallback and the title task starts after run_turn — the real Agent emits
    # title earlier (mid-stream). Both orders are covered; here it's the tail.
    assert kinds == [
        "session", "delta", "delta", "turn_complete", "title", "done",
    ]
    assert events[-1]["text"] == "hello world"
    assert events[-1]["title"] == "hi"  # namer=None → fallback title

    session_id = events[0]["session_id"]
    msgs = (
        await client.get(f"/api/sessions/{session_id}/messages")
    ).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


async def _drain_chat(client, body: dict) -> list[dict]:
    events = []
    async with client.stream("POST", "/api/chat", json=body) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def test_chat_passes_effort_to_run_turn(client):
    """The web UI's effort picker rides /api/chat → start_chat_run →
    run_turn(effort=); omitted means None (the provider default)."""
    agent = FakeAgent()
    client.gw.agent_factory = lambda autonomy: agent
    events = await _drain_chat(client, {"text": "hi", "effort": "xhigh"})
    assert events[-1]["type"] == "done"
    assert agent.kwargs["effort"] == "xhigh"

    agent2 = FakeAgent()
    client.gw.agent_factory = lambda autonomy: agent2
    await _drain_chat(client, {"text": "hi"})
    assert agent2.kwargs["effort"] is None


async def test_chat_rejects_unknown_effort(client):
    resp = await client.post("/api/chat", json={"text": "hi", "effort": "high"})
    assert resp.status_code == 422


def test_chat_effort_literal_matches_provider_vocabulary():
    """ChatRequest's Literal and the provider's EFFORTS must not drift (the
    provider would raise mid-turn on a value the API let through)."""
    from typing import get_args

    from harness.gateway.routes.streaming import ChatRequest
    from harness.providers.anthropic_provider import EFFORTS

    literal = next(
        a for a in get_args(ChatRequest.model_fields["effort"].annotation)
        if a is not type(None)
    )
    assert set(get_args(literal)) == set(EFFORTS)


async def test_chat_streams_thinking_before_deltas(db):
    """Extended thinking rides the chat SSE as thinking_delta/thinking_done,
    ahead of the answer deltas."""
    memory = MemoryStore(db)
    await memory.setup()
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: FakeAgent(thinking=["let me ", "reason"]),
        memory=memory,
        broadcaster=RunBroadcaster(),
    )
    app = create_app(gw)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    ) as c:
        events = []
        async with c.stream(
            "POST", "/api/chat", json={"text": "hi", "session_id": None}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    kinds = [e["type"] for e in events]
    assert kinds == [
        "session",
        "thinking_delta",
        "thinking_delta",
        "thinking_done",
        "tool_start",
        "delta",
        "delta",
        "turn_complete",
        "title",  # new chat → early naming (tail position for FakeAgent)
        "done",
    ]
    assert [e["text"] for e in events if e["type"] == "thinking_delta"] == [
        "let me ",
        "reason",
    ]
    assert next(e for e in events if e["type"] == "tool_start")["tool"] == "web_search"


async def _park_chat_run(client, gate, session_id=None):
    """Start a chat run through start_chat_run and spin until it parks on the
    gate (first delta emitted)."""
    from harness.gateway.routes.streaming import start_chat_run

    client.gw.agent_factory = lambda autonomy: FakeAgent(gate=gate)
    run = await start_chat_run(client.gw, "hi", session_id)
    for _ in range(200):
        # Real sleep, not sleep(0): add_user's aiosqlite write needs a thread
        # roundtrip before the first delta is emitted.
        await asyncio.sleep(0.01)
        if not run.queue.empty():
            break
    assert not run.queue.empty(), "run never reached its first delta"
    return run


async def test_chat_detach_reroutes_to_broadcaster_and_persists(client):
    """A client disconnect DETACHES the run (never cancels): it completes,
    persists the assistant message, reroutes post-detach events to the
    broadcaster tagged with session_id, and ends with a terminal run_done.
    Pinned: don't put task.cancel() back in the chat stream's finally."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)
    assert run.session.id in client.gw.active_chats

    sub = client.gw.broadcaster.subscribe()
    run.detach()  # what the SSE generator's finally does on disconnect
    gate.set()
    await run.task

    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    kinds = [e["type"] for e in events]
    # The head delta that was already queued when the socket died rides out
    # first (no head-gap on the recovered view), then the second delta,
    # turn_complete + title (new chat) + done + terminal run_done.
    assert kinds == [
        "delta", "delta", "turn_complete", "title", "done", "run_done",
    ]
    assert [e["text"] for e in events[:2]] == ["hello ", "world"]
    assert all(e["session_id"] == run.session.id for e in events)

    # The turn persisted despite the disconnect — the whole point.
    msgs = (
        await client.get(f"/api/sessions/{run.session.id}/messages")
    ).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert run.session.id not in client.gw.active_chats  # registry reaped


async def test_detach_endpoint_reroutes_when_socket_death_unnoticed(client):
    """The client-initiated detach: a silently-vanished network leaves the
    server attached to a dead socket (writes keep succeeding), so the client's
    reconnect path POSTs /detach to force the reroute. Without it recovery
    streams nothing and the PWA hangs on 'Reconnecting'. Unknown session →
    202 no-op (its own teardown already detached)."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)
    sub = client.gw.broadcaster.subscribe()

    resp = await client.post(f"/api/chat/{run.session.id}/detach")
    assert resp.status_code == 202
    assert (
        await client.post("/api/chat/nonexistent/detach")
    ).status_code == 202

    gate.set()
    await run.task
    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    kinds = [e["type"] for e in events]
    assert kinds == [
        "delta", "delta", "turn_complete", "title", "done", "run_done",
    ]
    assert events[0]["text"] == "hello "  # the queued head, not a head-gap
    assert all(e["session_id"] == run.session.id for e in events)


async def test_late_detach_reroutes_whole_turn_and_stays_idempotent(client):
    """detach() fires from several places (the client's /detach retries, the
    generator's finally) and can land after the turn already finished with the
    queue never read. The reroute must handle a queue holding its terminal
    `None` (no crash on `{**None}`), leave that sentinel readable so a still
    attached generator can end rather than trapping active_chats, and do the
    reroute exactly once."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)
    sub = client.gw.broadcaster.subscribe()

    gate.set()
    await run.task  # finishes while still attached: nothing was ever read
    run.detach()
    run.detach()

    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    assert [e["type"] for e in events] == [
        "delta", "delta", "turn_complete", "title", "done",
    ]
    assert run.queue.qsize() == 1
    assert run.queue.get_nowait() is None


async def test_chat_attached_run_publishes_nothing(client):
    """While the client is attached every event goes to the per-request queue
    only — the broadcaster stays silent (no dedupe needed client-side)."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)
    sub = client.gw.broadcaster.subscribe()
    gate.set()
    await run.task

    assert sub.empty()
    events = []
    while (ev := run.queue.get_nowait()) is not None:
        events.append(ev)
    assert [e["type"] for e in events] == [
        "delta", "delta", "turn_complete", "title", "done",
    ]
    assert all("session_id" not in e for e in events)  # wire format unchanged


async def test_chat_409_while_previous_turn_still_running(client):
    """Concurrent turns on one session would corrupt its history now that
    disconnect no longer cancels — the registry guard rejects the second send."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)

    resp = await client.post(
        "/api/chat", json={"text": "again", "session_id": run.session.id}
    )
    assert resp.status_code == 409

    gate.set()
    await run.task
    # Finished run frees the session for the next turn.
    resp = await client.post(
        "/api/chat", json={"text": "again", "session_id": run.session.id}
    )
    assert resp.status_code == 200


async def test_chat_409_while_a_background_run_owns_the_session(client):
    """BK-013 (2026-10-25): a chat resumed the session a cron run was still
    writing; the two Session objects collided on messages.idx and the cron
    died. Background runs never enter active_chats, so the guard also reads
    the orchestrator's background_sessions — and rejects BEFORE resuming, so
    the store is untouched. Once the run releases it, chat proceeds."""
    sid = await client.gw.sessions.create(channel="web", title="Property run")
    client.gw.background_sessions.add(sid)

    resp = await client.post("/api/chat", json={"text": "Continué", "session_id": sid})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "A scheduled run is still working in this chat — try again when it finishes."
    )
    assert await client.gw.sessions.load_messages(sid) == []
    assert sid not in client.gw.active_chats

    client.gw.background_sessions.discard(sid)
    resp = await client.post("/api/chat", json={"text": "Continué", "session_id": sid})
    assert resp.status_code == 200


async def test_background_sessions_default_empty():
    """Tests and the CLI build a Gateway without an orchestrator: the field
    defaults to its own empty set, never a shared mutable default."""
    a = Gateway(**_minimal_gateway_kwargs())
    b = Gateway(**_minimal_gateway_kwargs())
    assert a.background_sessions == set()
    assert a.background_sessions is not b.background_sessions


def _minimal_gateway_kwargs() -> dict:
    return dict(
        sessions=None, runs=None, approval_store=None, waiter=None,
        triggers=None, bus=None, push=None, agent_factory=lambda a: None,
    )


async def test_sessions_active_flag_tracks_live_chat_runs(client):
    """/api/sessions marks sessions with an in-flight chat turn — the client's
    recovery signal after a dropped stream (also true when the run finished or
    agentd restarted: flag false → refetch the transcript)."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)

    (s,) = (await client.get("/api/sessions")).json()
    assert (s["id"], s["active"]) == (run.session.id, True)

    gate.set()
    await run.task
    (s,) = (await client.get("/api/sessions")).json()
    assert s["active"] is False


async def test_stop_endpoint_cancels_a_live_turn(client):
    """Stop mid-model-stream → request_stop() returns True, the task is
    cancelled, and the SSE stream yields a terminal `stopped` event (the runs
    row's `cancelled` status is covered in test_agent_loop)."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)

    resp = await client.post(f"/api/chat/{run.session.id}/stop")
    assert resp.status_code == 202 and resp.json() == {"stopped": True}

    with pytest.raises(asyncio.CancelledError):
        await run.task
    events = []
    while not run.queue.empty():
        ev = run.queue.get_nowait()
        if ev is not None:
            events.append(ev["type"])
    assert "stopped" in events
    assert run.session.id not in client.gw.active_chats
    assert run.session.id not in client.gw.chat_runs


async def test_stop_endpoint_defers_while_tool_executes(client):
    """request_stop() False (tool running) → the endpoint returns 202 WITHOUT
    cancelling; the loop unwinds itself after add_step (tested in
    test_agent_loop). Here we only pin the route's non-cancel branch."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)
    run.agent.request_stop = lambda: False  # simulate: a tool is executing

    resp = await client.post(f"/api/chat/{run.session.id}/stop")
    assert resp.status_code == 202
    assert not run.task.done()  # not cancelled — deferred to the loop

    gate.set()
    await run.task  # turn completes normally


async def test_stop_endpoint_404_without_live_run(client):
    resp = await client.post("/api/chat/nope/stop")
    assert resp.status_code == 404


async def test_stop_endpoint_409_after_answer_complete(client):
    """The answer is on screen; only the naming/extraction tail is left —
    Stop is a 409 ("already finishing"), not a cancelled turn."""
    gate = asyncio.Event()
    run = await _park_chat_run(client, gate)
    run.answer_complete = True

    resp = await client.post(f"/api/chat/{run.session.id}/stop")
    assert resp.status_code == 409
    assert not run.task.done()  # untouched

    gate.set()
    await run.task


async def test_chat_stream_pings_while_the_model_is_silent(client, monkeypatch):
    """The local lane queues behind another client's request for minutes with
    zero bytes on this wire; the stream must heartbeat or tailscale serve
    drops it and the PWA cries "connection lost". ASGITransport buffers the
    body until the app completes, so we assert the pings are IN the drained
    stream, not that they arrived early."""
    from harness.gateway.routes import chat_run

    monkeypatch.setattr(chat_run, "CHAT_HEARTBEAT_S", 0.05)
    gate = asyncio.Event()
    client.gw.agent_factory = lambda autonomy: FakeAgent(gate=gate)

    async def release():
        await asyncio.sleep(0.3)  # ~6 heartbeat intervals with the queue empty
        gate.set()

    releaser = asyncio.create_task(release())
    raw = ""
    try:
        async with client.stream(
            "POST", "/api/chat", json={"text": "hi"}
        ) as resp:
            async for line in resp.aiter_lines():
                raw += line + "\n"
    finally:
        await releaser

    assert ": ping" in raw
    assert '"type": "done"' in raw  # the turn itself still completed


async def test_chat_stream_anti_buffering_headers(client):
    """Without these a proxy (tailscale serve) or Safari holds the whole
    stream until it closes — same pair /api/events has always sent."""
    async with client.stream(
        "POST", "/api/chat", json={"text": "hi"}
    ) as resp:
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        async for _ in resp.aiter_lines():
            pass


async def test_stopped_approval_wait_expires_the_row(client):
    """A run cancelled while parked on an approval must resolve its row
    `expired`, or the decision can never fire and it lingers in the inbox."""
    waiter = client.gw.waiter
    request = asyncio.ensure_future(waiter.request("r1", "gmail_send", {"to": "x"}))
    pending = {}
    for _ in range(50):
        await asyncio.sleep(0)
        rows = (await client.get("/api/approvals")).json()
        if rows:
            pending = rows[0]
            break
    assert pending

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    row = await waiter.store.get(pending["id"])
    assert row.status == "expired"


async def test_events_stream_subscribes_and_cleans_up(client):
    # A GET /api/events registers a live subscriber; a disconnect must
    # unsubscribe it so the broadcaster doesn't leak buffers per closed tab.
    # (Incremental SSE delivery itself is covered in test_broadcast.py;
    # httpx's ASGITransport buffers an unbounded response, so we can't read
    # it line-by-line here.)
    hub = client.gw.broadcaster
    assert hub.subscriber_count == 0

    async def hold_open():
        async with client.stream("GET", "/api/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for _ in resp.aiter_lines():
                pass

    task = asyncio.ensure_future(hold_open())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if hub.subscriber_count:
            break
    assert hub.subscriber_count == 1

    task.cancel()  # client disconnects
    for _ in range(200):
        await asyncio.sleep(0.01)
        if hub.subscriber_count == 0:
            break
    assert hub.subscriber_count == 0  # stream's finally unsubscribed it


async def _hold_events(client, url, headers=None):
    async with client.stream("GET", url, headers=headers or {}) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass


async def _wait_subscribers(hub, want):
    for _ in range(200):
        if hub.subscriber_count == want:
            return
        await asyncio.sleep(0.01)
    assert hub.subscriber_count == want


async def test_events_page_params_lifecycle(client, capsys):
    # The diagnostic params ride the same lifecycle as a bare stream: one
    # live subscriber, unsubscribed on disconnect — and a lone page logs
    # nothing (visibility toggles reconnect constantly).
    hub = client.gw.broadcaster
    task = asyncio.ensure_future(_hold_events(client, "/api/events?page=solo1&n=1"))
    await _wait_subscribers(hub, 1)
    task.cancel()
    await _wait_subscribers(hub, 0)
    assert "[events]" not in capsys.readouterr().err


async def test_events_duplicate_page_logs_one_line_each_way(client, capsys):
    # Offline twin of the plan's two-curl check: two concurrent streams for
    # the same page → exactly one "opened" line (naming both n, the reduced
    # device class, never the raw UA) and one "closed" line when the first
    # of the pair goes; the survivor closing alone is silent.
    hub = client.gw.broadcaster
    ua = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X)"}
    first = asyncio.ensure_future(
        _hold_events(client, "/api/events?page=test1&n=1", ua)
    )
    await _wait_subscribers(hub, 1)
    second = asyncio.ensure_future(
        _hold_events(client, "/api/events?page=test1&n=2", ua)
    )
    await _wait_subscribers(hub, 2)
    first.cancel()
    await _wait_subscribers(hub, 1)
    second.cancel()
    await _wait_subscribers(hub, 0)

    lines = [ln for ln in capsys.readouterr().err.splitlines() if "[events]" in ln]
    assert len(lines) == 2, lines
    opened, closed = lines
    assert opened.startswith("[events] page test1 opened stream n=2 while 1 live (n=1")
    assert "subscribers=2" in opened
    assert opened.endswith("ua=iPhone")
    assert "Mozilla" not in opened
    assert closed.startswith("[events] page test1 closed n=1 after ")
    assert closed.endswith("; 1 left")


async def test_events_without_params_never_tracked(client, capsys):
    # Old cached clients send no params: two concurrent bare streams are
    # ordinary subscribers and log nothing.
    hub = client.gw.broadcaster
    a = asyncio.ensure_future(_hold_events(client, "/api/events"))
    b = asyncio.ensure_future(_hold_events(client, "/api/events"))
    await _wait_subscribers(hub, 2)
    a.cancel()
    b.cancel()
    await _wait_subscribers(hub, 0)
    assert "[events]" not in capsys.readouterr().err


async def test_events_malformed_params_accepted_untracked(client, capsys):
    # A malformed page/n is treated as absent — never a 422 (status asserted
    # inside _hold_events) and never echoed into the log.
    hub = client.gw.broadcaster
    bad = "/api/events?page=%3Cscript%3E%20x&n=abc"
    long_page = "/api/events?page=" + "a" * 41 + "&n=1"
    a = asyncio.ensure_future(_hold_events(client, bad))
    b = asyncio.ensure_future(_hold_events(client, bad))
    c = asyncio.ensure_future(_hold_events(client, long_page))
    d = asyncio.ensure_future(_hold_events(client, long_page))
    await _wait_subscribers(hub, 4)
    for t in (a, b, c, d):
        t.cancel()
    await _wait_subscribers(hub, 0)
    assert "[events]" not in capsys.readouterr().err


async def test_events_unavailable_without_broadcaster(db):
    from harness.gateway.app import create_app

    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: FakeAgent(),
        broadcaster=None,
    )
    transport = httpx.ASGITransport(app=create_app(gw))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    ) as c:
        assert (await c.get("/api/events")).status_code == 503


async def test_approval_endpoints(client):
    waiter = client.gw.waiter
    request = asyncio.ensure_future(waiter.request("r1", "gmail_send", {"to": "x"}))
    for _ in range(50):
        await asyncio.sleep(0)
        if (await client.get("/api/approvals")).json():
            break

    (pending,) = (await client.get("/api/approvals")).json()
    assert pending["tool"] == "gmail_send"
    # Derived source/why keys reach the API payload (r1 has no runs row →
    # unknown origin; classification is None here so why_gated is empty).
    assert pending["source_kind"] == "unknown"
    assert "source_label" in pending and "why_gated" in pending

    resp = await client.post(
        f"/api/approvals/{pending['id']}", json={"approved": True}
    )
    # applied=True: a live run was waiting on this approval and was woken.
    assert resp.json() == {"ok": True, "applied": True}
    assert await request is True

    # Deciding again conflicts.
    resp = await client.post(
        f"/api/approvals/{pending['id']}", json={"approved": False}
    )
    assert resp.status_code == 409


async def test_trigger_endpoints(client):
    tid = await client.gw.triggers.add("cron", "30 7 * * 1-5", "morning_briefing")

    triggers = (await client.get("/api/triggers")).json()
    assert triggers[0]["id"] == tid
    # List is enriched for UI transparency: resolved instructions + next fire.
    assert triggers[0]["playbook_name"] == "morning_briefing"
    assert triggers[0]["prompt"] == playbooks.resolve("morning_briefing").prompt
    assert triggers[0]["next_fire_at"]  # exact value is local-tz dependent
    # A legacy name-row's effective tools come from playbook frontmatter.
    assert "calendar_list_events" in triggers[0]["effective_tools"]

    resp = await client.post(f"/api/triggers/{tid}/fire")
    assert resp.json() == {"ok": True}
    event = await client.gw.bus.get()
    assert isinstance(event, CronFired)
    assert event.playbook == "morning_briefing"
    assert event.tools is None  # no allocation on the row


async def test_trigger_crud_routes(client):
    # Create (the web UI's Schedules form).
    resp = await client.post(
        "/api/triggers",
        json={
            "cron": "0 9 * * *",
            "prompt": "Do the weekly review.",
            "label": "Weekly review",
            "autonomy": "autonomous",
            "tools": ["schedule_list"],
        },
    )
    assert resp.status_code == 200
    tid = resp.json()["id"]
    (t,) = (await client.get("/api/triggers")).json()
    assert (t["label"], t["autonomy"], t["tools"]) == (
        "Weekly review", "autonomous", ["schedule_list"],
    )
    assert t["effective_tools"] == ["schedule_list"]

    # Fire-route parity: the rebuilt CronFired carries the row's allocation.
    await client.post(f"/api/triggers/{tid}/fire")
    event = await client.gw.bus.get()
    assert event.autonomy == "autonomous"
    assert event.tools == ["schedule_list"]

    # Patch validates the MERGED row: clearing tools while autonomous → 422.
    resp = await client.patch(f"/api/triggers/{tid}", json={"tools": []})
    assert resp.status_code == 422
    resp = await client.patch(
        f"/api/triggers/{tid}", json={"autonomy": "supervised", "tools": []}
    )
    assert resp.status_code == 200
    (t,) = (await client.get("/api/triggers")).json()
    assert (t["autonomy"], t["tools"]) == ("supervised", None)

    # Validation failures: bad cron, unknown tool, autonomous without tools,
    # and interactive is not a schedulable autonomy.
    for bad in (
        {"cron": "nope", "prompt": "x"},
        {"cron": "0 9 * * *", "prompt": "x", "tools": ["no_such_tool"]},
        {"cron": "0 9 * * *", "prompt": "x", "autonomy": "autonomous"},
        {"cron": "0 9 * * *", "prompt": "x", "autonomy": "interactive"},
    ):
        assert (await client.post("/api/triggers", json=bad)).status_code == 422

    # Delete.
    assert (await client.delete(f"/api/triggers/{tid}")).json() == {"ok": True}
    assert (await client.delete(f"/api/triggers/{tid}")).status_code == 404
    assert (await client.get("/api/triggers")).json() == []


async def test_playbooks_and_cron_preview_routes(client):
    pbs = (await client.get("/api/playbooks")).json()
    briefing = next(p for p in pbs if p["name"] == "morning_briefing")
    assert "morning briefing" in briefing["prompt"]
    assert "calendar_list_events" in briefing["tools"]

    resp = await client.get("/api/cron/next", params={"spec": "0 9 * * *"})
    assert resp.status_code == 200 and resp.json()["next_fire_at"]
    resp = await client.get("/api/cron/next", params={"spec": "bogus"})
    assert resp.status_code == 422


async def test_trigger_list_enrichment(client):
    # Inline prompt: not a playbook name, so it IS the instructions.
    await client.gw.triggers.add("cron", "0 9 * * *", "Remind me: submit the report")
    # Watch triggers have no cron spec to project a next fire from.
    await client.gw.triggers.add("watch", "inbox_watch", "email_triage")

    triggers = (await client.get("/api/triggers")).json()
    by_kind = {t["kind"]: t for t in triggers}

    inline = by_kind["cron"]
    assert inline["playbook_name"] is None
    assert inline["prompt"] == "Remind me: submit the report"
    assert inline["next_fire_at"]

    watch = by_kind["watch"]
    assert watch["playbook_name"] == "email_triage"
    assert watch["prompt"] == playbooks.resolve("email_triage").prompt
    assert watch["next_fire_at"] is None


async def test_memory_endpoints(client):
    memory = client.gw.memory
    fid, _ = await memory.add_fact("User prefers tea over coffee", category="preference")
    await memory.add_fact("Working on the harness P6 scale-out", category="project")

    facts = (await client.get("/api/memory/facts")).json()
    assert len(facts) == 2

    facts = (await client.get("/api/memory/facts", params={"q": "tea"})).json()
    assert any("tea" in f["content"] for f in facts)

    assert (await client.post(f"/api/memory/facts/{fid}/forget")).json() == {
        "ok": True
    }
    assert (await client.post(f"/api/memory/facts/{fid}/forget")).status_code == 404
    facts = (await client.get("/api/memory/facts")).json()
    assert len(facts) == 1
    both = (
        await client.get("/api/memory/facts", params={"include_inactive": "true"})
    ).json()
    assert len(both) == 2


async def test_memory_fact_create_and_patch(client):
    # POST: hand-typed facts get confidence 1.0 and report created.
    resp = await client.post(
        "/api/memory/facts",
        json={"content": "Tom's boss is Bill", "category": "contact"},
    )
    assert resp.json()["created"] is True
    fid = resp.json()["id"]
    facts = (await client.get("/api/memory/facts")).json()
    assert facts[0]["confidence"] == 1.0
    assert facts[0]["last_recalled_at"] is None  # new field serialized

    # Duplicate POST dedupes.
    resp = await client.post(
        "/api/memory/facts",
        json={"content": "Tom's boss is Bill", "category": "contact"},
    )
    assert resp.json() == {"ok": True, "id": fid, "created": False}

    # Validation.
    resp = await client.post(
        "/api/memory/facts", json={"content": "x", "category": "nonsense"}
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/memory/facts", json={"content": "  ", "category": "contact"}
    )
    assert resp.status_code == 422

    # PATCH: partial update reflected in GET.
    resp = await client.patch(
        f"/api/memory/facts/{fid}", json={"content": "Tom's boss is Phil"}
    )
    assert resp.json() == {"ok": True}
    facts = (await client.get("/api/memory/facts")).json()
    assert facts[0]["content"] == "Tom's boss is Phil"
    assert facts[0]["category"] == "contact"

    # PATCH validation: empty patch, blank content, bad category, unknown id.
    assert (await client.patch(f"/api/memory/facts/{fid}", json={})).status_code == 422
    resp = await client.patch(f"/api/memory/facts/{fid}", json={"content": " "})
    assert resp.status_code == 422
    resp = await client.patch(f"/api/memory/facts/{fid}", json={"category": "bad"})
    assert resp.status_code == 422
    resp = await client.patch("/api/memory/facts/9999", json={"category": "contact"})
    assert resp.status_code == 404


async def test_static_ui_served(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Sidekick" in resp.text
    # CSP is set on the document so script-src 'self' governs the page.
    csp = resp.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    # img-src 'self' (no external, no data:) blocks a prompt-injected markdown
    # image from exfiltrating via a render-time GET. Keep in lockstep with the
    # renderMd FORBID_TAGS strip in webui/dom.js.
    assert "img-src 'self';" in csp
    assert "data:" not in csp
    assert (await client.get("/sw.js")).status_code == 200


async def test_client_timezone_persists(client, db):
    from harness.store.client_config import ClientConfigStore

    store = ClientConfigStore(db)
    client.gw.client_config = store

    resp = await client.post("/api/client/timezone", json={"tz": "Asia/Singapore"})
    assert resp.json() == {"ok": True, "tz": "Asia/Singapore"}
    assert await store.get_timezone() == "Asia/Singapore"


async def test_client_timezone_rejects_garbage(client):
    assert (
        await client.post("/api/client/timezone", json={"tz": "Not/AZone"})
    ).status_code == 422
    assert (
        await client.post("/api/client/timezone", json={"tz": ""})
    ).status_code == 422


async def test_client_refs_round_trip(client, db):
    """POST /api/client/refs stores identity-ref values; GET reads them back for
    the edit UI (single-user authed surface). An empty string clears a key."""
    from harness.store.client_config import ClientConfigStore

    client.gw.client_config = ClientConfigStore(db)

    # unset → empty strings
    assert (await client.get("/api/client/refs")).json() == {"email": "", "phone": ""}

    resp = await client.post(
        "/api/client/refs",
        json={"email": "test@example.invalid", "phone": "+15550000000"},
    )
    assert resp.json() == {"ok": True}
    assert (await client.get("/api/client/refs")).json() == {
        "email": "test@example.invalid",
        "phone": "+15550000000",
    }

    # A field omitted is left untouched; an empty string clears the key.
    await client.post("/api/client/refs", json={"email": ""})
    got = (await client.get("/api/client/refs")).json()
    assert got == {"email": "", "phone": "+15550000000"}


async def test_no_buffering_middleware_on_the_app(db):
    """CSP must live on STATIC responses (NoCacheStatic.get_response), NOT a
    BaseHTTPMiddleware — the latter buffers bodies and breaks the /api/chat and
    /api/events SSE streams.

    If middleware is ever legitimately added, it must be pure-ASGI (not
    BaseHTTPMiddleware) and this test must be updated consciously.

    Pinned in tests/test_gateway.py."""
    memory = MemoryStore(db)
    await memory.setup()
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: FakeAgent(),
        memory=memory,
        broadcaster=RunBroadcaster(),
        tool_catalog=[],
    )
    app = create_app(gw)
    assert app.user_middleware == [], gotcha(
        "csp-static-not-middleware",
        "BaseHTTPMiddleware buffers bodies and breaks SSE; CSP belongs on static responses"
    )
