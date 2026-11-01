"""Server-half of the iOS-resume repro harness (plan pwa-resume-reconcile,
Step 1; extended by pwa-accurate-resume Steps 3-4) — the contract the
CLIENT's reconcile depends on.

test_gateway.py pins the happy detach reroute. These pin the boundary it
leaves open, the part recovery correctness hinges on:

- events the attached generator already consumed are GONE — detach reroutes
  only what the queue still holds, never a replay (exactly-one-sink; the lost
  head is what the client's refetch heals, and `run_done` always implies
  such a gap — see the 2026-10-17 decisions entry);
- rerouted queue events keep their order ahead of newly emitted ones;
- exactly one terminal `run_done` per detached run, however many times
  detach() fires (client /detach retry + generator finally + watchdog).

The Step 3 RESYNC contract (pwa-accurate-resume), pinned further below:

- every chat-run event carries `seq` (monotonic per run) and `run_id`,
  assigned ONCE in the emit() closure BEFORE routing — the attached SSE
  queue, the detach reroute, the broadcaster and /live show the SAME numbers
  for the same event, so a client switching sinks never renumbers. `seq`
  restarts at 1 for each NEW run on the same session, so a watermark is only
  meaningful paired with its `run_id` — never one global last-seen;
- GET /api/chat/{sid}/live?since=N → {active: true, seq: M, oldest_seq: O,
  gap: G, events: [...]} while the run is live: exactly the events with
  seq > N, in order. `since` means "I have painted everything through seq
  N"; since=0 replays the whole live run (a client COLD-OPENING the chat
  uses /messages instead — see the Step 4 contract below). `gap` is the
  SUPPORTED drop detector — true iff events the client asked for are gone
  from the front (`since + 1 < oldest_seq`; at `since == oldest_seq - 1`
  nothing is missing, and a caught-up or beyond-watermark client is never
  flagged), `oldest_seq` names that floor — the client never does seq
  arithmetic. On gap the head is GONE from live data and must NOT be
  filled by a mid-call store refetch (the store has no in-flight assistant
  message — the gotcha's lossy path): paint the suffix from oldest_seq,
  heal with the run_done refetch after the run;
- the window is RING_CAPACITY = 65,536 events, measured at 2.1x the longest
  observed web run (31,159 events @ ~1 delta/token) ≈ 105 minutes of
  streaming at the observed ~10.4 tok/s — a resume at ANY away-time during
  a run of observed length finds its watermark in-window (gap: false);
- the events come from the run's in-memory ring, written at EMIT, never via
  the drop-on-full publisher — replay is complete even when every subscriber
  buffer overflowed (pinned by the full-broadcaster test);
- {active: false} once the run is gone, and nothing else. The ring is NOT
  durable: nothing lands in SQLite, nothing survives an agentd restart, the
  replay window is the live run only. active:false OBLIGES the client to
  fall back to the persisted refetch — the run_done heal stays the post-run
  path (the ban on mid-call re-sync from the STORE stands).
- /api/events is NOT chat-only: background runs (orchestrator run_start /
  run_done / delta / thinking) arrive there with NO seq and NO run_id. Only
  chat-run events are numbered, so the client's seq dedupe must pass
  seq-less events through untouched — never treat "no seq" as "old".

The Step 4 RESUME CONTRACT (pwa-accurate-resume), pinned further below:

GET /api/sessions/{sid}/messages is the endpoint a RESUMING client calls
FIRST. One fetch returns the whole screen (fetch-then-swap atomic render):

    {messages: [...persisted transcript...],
     live: {since, seq, oldest_seq, gap, events} | null,
     compactions: [{checkpoint_id, anchor_idx, created_at, est_before, est_after}, ...]}

Step 5 of compaction-checkpoint-bookmark extended this shape: every message
row carries `idx` (its STORE idx — array position misanchors once the table
has a hole), and `compactions` (present, [] when none) lists the session's
compactions oldest-first with `anchor_idx` in the SAME store-idx space as
`idx`: the reload seam paints above the first row whose idx >= anchor_idx.
A mid-turn reload can hand the same compaction to both the anchored reload
painter and the live tail — the client dedupes by the seam's data-anchor /
data-cp (webui/compaction.js).

- `live: null` ⇒ nothing is in flight on this session: `messages` is the
  whole truth. Paint it; do NOT call /live (an immediate /live would answer
  {active: false} anyway — both endpoints share ONE liveness predicate,
  gateway/live.py, so they can never disagree in the tick between task
  completion and the run's registry reap; and the transcript is never a
  read that predates the final persist — liveness is sampled around the
  store read, pinned by the stale-read test).
- `live` present ⇒ the run is MID-ANSWER and the store is missing exactly
  its in-flight step (persisted only when the model call RETURNS — the
  2026-10-17 gotcha). THE JOIN IS SERVER-SIDE: `live` is replayed FROM
  `since` = ChatRun.persisted_seq, the watermark advanced past every event
  whose rows are already in `messages` (refreshed when a Session
  add_step/add_assistant write returns). So events ≤ since are exactly what
  `messages` contains, and `live.events` is the UNPERSISTED tail — paint
  `messages`, then replay the tail through the UNPACED replay path and
  nothing is ever painted twice. No client-side join exists for this
  (persisted rows carry no seq; replay-from-0 double-paints a tool-loop
  run's completed steps — pinned by the ToolLoopAgent tests).
- Watermark discipline (the two sinces are different numbers): after
  painting the tail, the client's own watermark for this run_id is
  `live.seq` — NEVER `since`, which is the server's persisted watermark.
  Subsequent resyncs send the client's watermark to
  GET /api/chat/{sid}/live?since=N; a gap there means events gone from the
  ring that the CLIENT lacked (paint the suffix, heal at run_done — step
  3's semantics). A gap in /messages' live means the dropped events were
  UNPERSISTED only (one in-flight model call outgrew the ring — >65,536
  events, beyond any observed run): the persisted half is WHOLE by
  construction, paint the suffix from oldest_seq, and the loss heals when
  that step persists (rows in the store; run_done's refetch is the
  canonical moment).
- Re-pull rule: `live` is the whole in-flight step — worst case one full
  model call (~2.4 MiB at the longest observed run, up to ~5 MiB at a full
  ring). A client that already holds a watermark for the live run must NOT
  re-fetch /messages to get it: it sends its watermark to
  /live?since=N and returns to /messages only when /live says active:false
  (or on a cold open, where it has no watermark and /messages is the only
  fetch needed). No pagination, no delta-compression: the watermark path
  is the avoidance mechanism.
- A session that never ran gets `live: null` (the key is PRESENT and null,
  never absent — one shape for the client to parse). Unknown session stays
  404 on this route (existence is /messages' question; /live ignores it).

The client half lives in tests/fixtures/pwa_resume/ (headless probe).
"""

from __future__ import annotations

import asyncio
import time
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus
from harness.core.session import Session
from harness.core.types import Message, ToolCall, ToolResult
from harness.gateway import broadcast as broadcast_mod
from harness.gateway.app import Gateway, create_app
from harness.gateway.broadcast import RunBroadcaster
from harness.gateway.event_ring import EventRing
from harness.gateway.live import live_projection
from harness.gateway.routes.chat_run import ChatRun, start_chat_run
from harness.store.approvals import ApprovalStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore


class GatedAgent:
    """A turn that parks mid-answer: delta "head " → gate → delta "tail".

    Mirrors test_gateway.FakeAgent's hook contract — lifecycle hooks stay
    None so the route's post-run_turn fallback wiring emits turn_complete —
    but on an EXISTING session there is no title event, which keeps the
    expected event sequence minimal for these pins."""

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate
        self.on_text_delta = lambda t: None
        self.on_tool_call = lambda n, a: None
        self.on_tool_result = lambda n, c, e: None
        self.on_thinking_delta = lambda t: None
        self.on_thinking_done = lambda: None
        self.on_truncated = lambda: None
        self.on_run_start = None
        self.on_model_call = None
        self.on_answer_complete = None
        self.last_run: dict | None = None

    def request_stop(self) -> bool:
        return True

    async def run_turn(self, session, user_input, **kwargs):
        await session.add_user(user_input)
        self.on_text_delta("head ")
        await self.gate.wait()
        self.on_text_delta("tail")
        await session.add_assistant("head tail")
        return "head tail"


@pytest.fixture
async def gw(db):
    return Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: GatedAgent(asyncio.Event()),
        broadcaster=RunBroadcaster(),
    )


async def _park(gw, sid, gate) -> ChatRun:
    """Start a run on an existing session and spin until the first delta sits
    in the queue (the moment a real socket would have received it)."""
    gw.agent_factory = lambda autonomy: GatedAgent(gate)
    run = await start_chat_run(gw, "hi", sid)
    for _ in range(200):
        await asyncio.sleep(0.01)  # add_user's aiosqlite write needs a thread hop
        if not run.queue.empty():
            break
    assert not run.queue.empty(), "run never reached its first delta"
    return run


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _session(gw) -> str:
    return (await Session.create(gw.sessions, channel="web")).id


async def test_consumed_events_are_not_replayed_on_detach(gw):
    """The dead-socket boundary: events the (simulated) SSE generator already
    read died with the socket — detach reroutes NOTHING of them. Exactly one
    sink per event; the head-gap they leave is the client's to heal by
    refetch (run_done always implies such a gap)."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park(gw, sid, gate)

    consumed = _drain(run.queue)  # what the generator yielded to the dead socket
    assert [e["type"] for e in consumed] == ["delta"]
    assert consumed[0]["text"] == "head "

    sub = gw.broadcaster.subscribe()
    run.detach()  # the stream generator's finally / the client's POST /detach
    gate.set()
    await run.task

    events = _drain(sub)
    assert [e["type"] for e in events] == [
        "delta", "turn_complete", "done", "run_done",
    ]
    assert events[0]["text"] == "tail"  # NOT "head " — no replay
    assert all(e["session_id"] == sid for e in events)  # rerouted, tagged
    # run_done went to the broadcaster ONLY — the queue holds its single
    # unconsumed terminal sentinel, nothing else (no second terminal sink).
    assert _drain(run.queue) == [None]


async def test_queued_unconsumed_events_reroute_ahead_of_new_ones(gw):
    """The mirror of the consumed case: an event still QUEUED at detach was
    never written to the socket, so it reroutes — in order, before events
    emitted after the detach — so the recovered /api/events view can still
    start at the true head of the turn."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park(gw, sid, gate)  # "head " queued, never drained

    sub = gw.broadcaster.subscribe()
    run.detach()
    gate.set()
    await run.task

    events = _drain(sub)
    assert [e["type"] for e in events] == [
        "delta", "delta", "turn_complete", "done", "run_done",
    ]
    assert [e["text"] for e in events[:2]] == ["head ", "tail"]  # order kept
    assert all(e["session_id"] == sid for e in events)


async def test_run_done_published_once_even_when_detach_fires_repeatedly(gw):
    """detach() is called from several places (the client's /detach retries,
    the generator's finally, the events.js watchdog) — the terminal run_done
    the returning client waits on must still be published exactly once, last."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park(gw, sid, gate)

    sub = gw.broadcaster.subscribe()
    run.detach()
    run.detach()  # idempotent reroute; must not double-publish anything
    gate.set()
    await run.task

    events = _drain(sub)
    dones = [e for e in events if e["type"] == "run_done"]
    assert len(dones) == 1
    assert events[-1]["type"] == "run_done"  # terminal: nothing follows it
    assert sum(e["type"] == "delta" for e in events) == 2  # no dupes either


# ---------------------------------------------------------------------------
# Step 3 (pwa-accurate-resume): seq at emit + per-run ring + GET /live
# ---------------------------------------------------------------------------


class StreamedAgent:
    """A turn that mimics the REAL Agent's hook contract: fires on_run_start
    (so events carry a run_id), streams thinking deltas ahead of the answer,
    and fires on_answer_complete (so turn_complete comes from the hook, not
    the route's fallback). With a gate it parks after the first answer delta;
    with gate=None it runs to completion (for SSE-drain tests).

    Emits, in order: thinking_delta xN, thinking_done, delta "head ",
    [gate], delta "tail", turn_complete, done — plus run_done on the
    broadcaster when detached."""

    RUN_ID = "run-7"

    def __init__(
        self,
        gate: asyncio.Event | None,
        thinking: tuple[str, ...] = ("th1", "th2"),
    ) -> None:
        self.gate = gate
        self.thinking = thinking
        self.on_text_delta = lambda t: None
        self.on_tool_call = lambda n, a: None
        self.on_tool_result = lambda n, c, e: None
        self.on_thinking_delta = lambda t: None
        self.on_thinking_done = lambda: None
        self.on_truncated = lambda: None
        # The route assigns these; run_turn only calls what's set, as Agent does.
        self.on_run_start = None
        self.on_model_call = None
        self.on_answer_complete = None
        self.last_run: dict | None = None

    def request_stop(self) -> bool:
        return True

    async def run_turn(self, session, user_input, **kwargs):
        if self.on_run_start is not None:
            self.on_run_start(self.RUN_ID)
        await session.add_user(user_input)
        for chunk in self.thinking:
            self.on_thinking_delta(chunk)
        self.on_thinking_done()
        self.on_text_delta("head ")
        if self.gate is not None:
            await self.gate.wait()
        self.on_text_delta("tail")
        if self.on_answer_complete is not None:
            self.on_answer_complete()
        await session.add_assistant("head tail")
        return "head tail"


@pytest.fixture
async def client(gw):
    """HTTP over the same Gateway the direct tests drive — /live is pinned as
    the client actually hits it (ASGITransport; JSON GETs, no SSE buffering)."""
    transport = httpx.ASGITransport(app=create_app(gw))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    ) as c:
        yield c


async def _park_streamed(gw, sid, gate) -> ChatRun:
    """Start a StreamedAgent run and wait until the whole head batch (2x
    thinking_delta, thinking_done, delta) is queued. Those four emit
    synchronously between awaits, so the queue is either empty or complete —
    no partial-batch race."""
    gw.agent_factory = lambda autonomy: StreamedAgent(gate)
    run = await start_chat_run(gw, "hi", sid)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if run.queue.qsize() >= 4:
            return run
    raise AssertionError("run never reached its gate")


async def test_seq_increases_across_detach_with_both_sinks_agreeing(gw):
    """The plan's core check: attach → detach → reroute. Events to the SSE
    queue and events rerouted to the broadcaster form ONE strictly
    increasing seq series, and the ring holds exactly one entry per
    delivered event — stamped once at emit, so no sink renumbers and no
    sink double-counts."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)

    sink_sse = _drain(run.queue)  # what the (simulated) generator yielded
    assert [e["type"] for e in sink_sse] == [
        "thinking_delta", "thinking_delta", "thinking_done", "delta",
    ]

    sub = gw.broadcaster.subscribe()
    run.detach()
    gate.set()
    await run.task

    sink_events = _drain(sub)
    assert [e["type"] for e in sink_events] == [
        "delta", "turn_complete", "done", "run_done",
    ]

    delivered = sink_sse + sink_events
    assert [e["seq"] for e in delivered] == list(range(1, 9))
    # Same NUMBERS for the same event: the ring's copy of each event agrees
    # with what a sink showed (the broadcaster copy adds session_id; the
    # queue copy IS the ring object — emit stamped before routing).
    ring = run.ring.since(0)
    assert len(ring) == 8  # one entry per delivered event, no dupes
    by_seq = {e["seq"]: e for e in delivered}
    for r in ring:
        shown = by_seq[r["seq"]]
        assert r.keys() <= shown.keys()
        assert all(r[k] == shown[k] for k in r)
    # run_id rides every event, run_done (the detached client's terminal
    # signal) included — it goes through emit like everything else now.
    assert all(e["run_id"] == StreamedAgent.RUN_ID for e in delivered)


async def test_rerouted_events_carry_the_numbers_they_were_queued_with(gw):
    """The other sink-switch: events still QUEUED at detach reroute to the
    broadcaster — with the very seq they were queued under, ahead of
    freshly emitted ones. A client that switches from the dead SSE socket
    to /api/events mid-run never sees a renumber."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)  # head batch queued, never drained

    sub = gw.broadcaster.subscribe()
    run.detach()
    gate.set()
    await run.task

    events = _drain(sub)
    assert [e["type"] for e in events] == [
        "thinking_delta", "thinking_delta", "thinking_done", "delta",  # rerouted
        "delta", "turn_complete", "done", "run_done",  # emitted after detach
    ]
    assert [e["seq"] for e in events] == list(range(1, 9))
    assert all(e["run_id"] == StreamedAgent.RUN_ID for e in events)


async def test_live_since_returns_exactly_events_after_n(gw, client):
    """The resync read: since=N gives exactly seq > N, in order — nothing
    at or below the client's watermark (that would repaint), nothing above
    missing (that would be a hole). Events are the ring's stamped dicts,
    verbatim: no session_id (the path already names the session). An
    in-window client always gets gap: false with oldest_seq naming the
    window's floor — the drop flags are the server's answer, never the
    client's arithmetic."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)  # seq 1-4, run parked mid-answer

    body = (await client.get(f"/api/chat/{sid}/live?since=2")).json()
    # BK-041 G5: + started_ms/phase — an observing page's row label + timer.
    assert set(body) == {
        "active", "seq", "oldest_seq", "gap", "events", "started_ms", "phase",
    }
    assert body["active"] is True
    assert body["phase"] == {"type": "delta"}  # slim: never the text
    assert 0 < body["started_ms"] <= time.time() * 1000
    assert body["seq"] == 4  # watermark = last seq issued
    assert body["oldest_seq"] == 1  # nothing dropped off the front
    assert body["gap"] is False
    assert [e["seq"] for e in body["events"]] == [3, 4]
    assert body["events"][0]["type"] == "thinking_done"
    assert body["events"][1] == {  # exact shape: stamped, untagged
        "type": "delta", "text": "head ", "seq": 4, "run_id": StreamedAgent.RUN_ID,
    }

    whole = (await client.get(f"/api/chat/{sid}/live")).json()  # since defaults 0
    assert [e["seq"] for e in whole["events"]] == [1, 2, 3, 4]
    assert (await client.get(f"/api/chat/{sid}/live?since=4")).json()["events"] == []
    # caught-up and beyond-the-watermark clients never see a phony gap
    assert (await client.get(f"/api/chat/{sid}/live?since=4")).json()["gap"] is False
    assert (await client.get(f"/api/chat/{sid}/live?since=99")).json()["events"] == []
    assert (await client.get(f"/api/chat/{sid}/live?since=99")).json()["gap"] is False
    assert (await client.get(f"/api/chat/{sid}/live?since=-5")).json()["seq"] == 4

    gate.set()
    await run.task


async def test_live_reports_gap_when_the_head_overflowed_the_ring(
    gw, client, monkeypatch
):
    """Step 6's out-of-window trigger, pinned over HTTP with a shrunk ring:
    `gap: true` + `oldest_seq` tell a client its watermark fell off the
    front WITHOUT any seq arithmetic, and `events` still starts at the
    window floor. The client paints that suffix and heals at run_done — a
    mid-call store refetch would be the reverted lossy path. A watermark the
    window DOES cover gets gap: false on the same overflowed ring."""
    import harness.gateway.routes.chat_run as chat_run_mod

    monkeypatch.setattr(chat_run_mod, "EventRing", lambda: EventRing(capacity=3))
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)  # seq 1-4 emitted; ring keeps 2-4

    cold = (await client.get(f"/api/chat/{sid}/live?since=0")).json()
    assert cold["gap"] is True  # asked for seq > 0; seq 1 is gone
    assert cold["oldest_seq"] == 2
    assert cold["seq"] == 4  # the watermark keeps climbing past the drop
    assert [e["seq"] for e in cold["events"]] == [2, 3, 4]  # suffix, in order

    covered = (await client.get(f"/api/chat/{sid}/live?since=1")).json()
    assert covered["gap"] is False  # everything seq > 1 is still retained
    assert [e["seq"] for e in covered["events"]] == [2, 3, 4]

    gate.set()
    await run.task


async def test_live_reports_inactive_once_the_run_is_gone(gw, client):
    """{active: false} is the client's cue to fall back to the persisted
    refetch — and it is all it gets: no seq, no events. The ring is not
    durable; it dies with the gw.active_chats/chat_runs entry (no SQLite,
    no restart survival). A session that never ran is inactive, NOT 404 —
    existence belongs to GET /api/sessions/{id}."""
    sid = await _session(gw)
    assert (await client.get(f"/api/chat/{sid}/live")).json() == {"active": False}
    assert (await client.get("/api/chat/no-such-sid/live")).json() == {"active": False}

    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)
    assert (await client.get(f"/api/chat/{sid}/live")).json()["active"] is True

    _drain(run.queue)  # the (simulated) generator consumes the head batch
    gate.set()
    await run.task
    for _ in range(50):  # let the _reap done-callback pop the entries
        if sid not in gw.chat_runs and sid not in gw.active_chats:
            break
        await asyncio.sleep(0.01)
    assert sid not in gw.chat_runs and sid not in gw.active_chats

    resp = await client.get(f"/api/chat/{sid}/live?since=0")
    assert resp.json() == {"active": False}


async def test_seq_numbers_restart_for_each_new_run_on_the_session(gw, client):
    """The client's dedupe slot resets on a run_id change, which is sound
    because the numbers RESTART (routes/streaming.py: "seq numbers restart at
    1 for each new run on the session, so a watermark belongs to its run_id")
    — an old run's watermark can never silently SKIP a new run's events, only
    go stale. Two runs on one session: the second ring reissues 1-4, and a
    watermark left over from run 1 reads as caught-up (empty events, no phony
    gap), never as a hole to heal."""
    sid = await _session(gw)
    gate1 = asyncio.Event()
    run1 = await _park_streamed(gw, sid, gate1)  # seq 1-4
    assert [e["seq"] for e in (
        await client.get(f"/api/chat/{sid}/live")).json()["events"]] == [1, 2, 3, 4]
    gate1.set()
    await run1.task
    await _reaped(gw, sid)

    gate2 = asyncio.Event()
    run2 = await _park_streamed(gw, sid, gate2)
    second = (await client.get(f"/api/chat/{sid}/live")).json()
    assert second["active"] is True
    assert second["seq"] == 4  # the counter restarted, it did not continue at 5
    assert [e["seq"] for e in second["events"]] == [1, 2, 3, 4]
    # A run-1 client (watermark 4) is harmlessly "caught up", not gap'd.
    stale = (await client.get(f"/api/chat/{sid}/live?since=4")).json()
    assert stale["events"] == [] and stale["gap"] is False

    gate2.set()
    await run2.task


async def test_full_broadcaster_still_replays_every_event_from_the_ring(
    gw, client, monkeypatch
):
    """The losslessness contract: publish() is drop-on-full, which is WHY
    the ring is written at emit, never via the publisher. Shrink the
    subscriber buffer to 1 so the reroute overflows it — /live must still
    replay every event the run emitted."""
    monkeypatch.setattr(broadcast_mod, "QUEUE_MAXSIZE", 1)

    sid = await _session(gw)
    gate = asyncio.Event()
    await _park_streamed(gw, sid, gate)  # head batch queued, never drained

    sub = gw.broadcaster.subscribe()  # maxsize 1 under the monkeypatch
    run = gw.chat_runs[sid]
    run.detach()  # reroutes 4 events; the full buffer keeps 1, DROPS 3
    assert sub.qsize() == 1  # the drop-on-full state is real

    body = (await client.get(f"/api/chat/{sid}/live?since=0")).json()
    assert body["active"] is True
    assert [e["seq"] for e in body["events"]] == [1, 2, 3, 4]  # ALL of them

    gate.set()
    await run.task
    # Post-run the client's cue is active:false → persisted refetch, even
    # though this client's /api/events buffer ate everything.
    assert (await client.get(f"/api/chat/{sid}/live?since=4")).json() == {
        "active": False
    }


async def test_sse_stream_carries_the_same_numbers(gw, client):
    """End-to-end over the real SSE wire: every run event on /api/chat
    carries seq+run_id; the per-connection `session` handshake is the one
    exception — it is not a run event, carries no number, and never enters
    the ring."""
    sid = await _session(gw)
    gw.agent_factory = lambda autonomy: StreamedAgent(gate=None)

    events = []
    async with client.stream(
        "POST", "/api/chat", json={"text": "hi", "session_id": sid}
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    assert [e["type"] for e in events] == [
        "session", "thinking_delta", "thinking_delta", "thinking_done",
        "delta", "delta", "turn_complete", "done",
    ]
    assert "seq" not in events[0] and "run_id" not in events[0]
    assert [e["seq"] for e in events[1:]] == list(range(1, 8))
    assert all(e["run_id"] == StreamedAgent.RUN_ID for e in events[1:])


def test_ring_overflow_drops_the_head_but_never_the_numbering():
    """Overflow behaviour the client depends on: seq keeps climbing past the
    dropped head, and replay() flags the loss itself — gap: true only for a
    watermark the window no longer covers, oldest_seq naming the floor.
    (The production window is RING_CAPACITY = 65,536 events ≈ 105 min of
    streaming; capacity 4 here just reaches overflow quickly.)"""
    ring = EventRing(capacity=4)
    assert ring.replay(0) == {  # empty: honest about having nothing, no gap
        "seq": 0, "oldest_seq": None, "gap": False, "events": []
    }
    for i in range(10):
        ring.stamp({"type": "thinking_delta", "i": i}, "r1")
    assert ring.seq == 10
    assert ring.oldest_seq == 7
    batch = ring.replay(0)  # watermark 0: asked for 1-6 too — they're gone
    assert batch["gap"] is True and batch["oldest_seq"] == 7
    assert [e["seq"] for e in batch["events"]] == [7, 8, 9, 10]
    assert batch["events"][0]["i"] == 6
    in_window = ring.replay(6)  # watermark inside the window: no gap
    assert in_window["gap"] is False
    assert [e["seq"] for e in in_window["events"]] == [7, 8, 9, 10]
    assert ring.replay(-5) == ring.replay(0)  # since is clamped at 0
    assert ring.since(9) == [
        {"type": "thinking_delta", "i": 9, "seq": 10, "run_id": "r1"}
    ]
    assert ring.since(10) == []


async def test_events_before_run_start_carry_run_id_none(gw):
    """The forgiving half: agents without the hook wiring (test fakes) still
    get numbered events — seq is unconditional, run_id is None until
    on_run_start opens the run."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park(gw, sid, gate)  # GatedAgent never fires on_run_start
    events = _drain(run.queue)
    assert [e["seq"] for e in events] == [1]
    assert all(e["run_id"] is None for e in events)
    gate.set()
    await run.task


# ---------------------------------------------------------------------------
# Step 4 (pwa-accurate-resume): GET /api/sessions/{sid}/messages carries the
# live answer. The contract (module docstring, and the route docstring in
# routes/resources.py — this file IS the client's contract):
#
#   {messages: [...persisted...],
#    live: {since, seq, oldest_seq, gap, events} | null}
#
# A resuming client's FIRST fetch is this one: messages + the UNPERSISTED
# tail in ONE response preserves the fetch-then-swap atomic render (a cold
# open of a mid-answer chat shows the live reply with no second request and
# no race). live null obliges "messages is the whole truth"; live present
# obliges "paint messages, then replay live.events unpaced" — the tail
# starts at the SERVER watermark (live.since = ChatRun.persisted_seq), so
# the replayed tail and the persisted transcript never share content: no
# client-side join, no double-paint of completed tool-loop steps. A client
# WITH a watermark does not re-fetch this payload (MiB-scale) — it sends
# its own watermark to /live?since=N instead.
# ---------------------------------------------------------------------------


async def _reaped(gw, sid) -> None:
    """Spin until the finished run's _reap callback has popped its registry
    entries (the done-callback fires on the loop after `await run.task`)."""
    for _ in range(50):
        if sid not in gw.chat_runs and sid not in gw.active_chats:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run was never reaped")


async def test_messages_mid_answer_carries_the_live_ring(gw, client):
    """The plan's first check: a mid-answer fetch returns the persisted
    transcript AND live — including the thinking events — so a cold open
    paints the whole screen from ONE response. The persisted half shows the
    TIMING the store actually has: the in-flight assistant step is absent
    until the model call returns (the 2026-10-17 gotcha — exactly why the
    ring, not a store refetch, is the live source). live's shape is
    ring.replay(persisted_seq) plus `since`, untagged like /live's — this
    fake persists nothing before its gate, so the tail is the whole run."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)  # seq 1-4, parked before "tail"

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    # Step 5 (compaction-checkpoint-bookmark) extended the envelope: the key
    # set is EXACTLY these three, present even mid-answer (compactions reads
    # after the transcript loop, so a fresh row anchors past these rows and
    # the client parks its seam above the tail — never an absent key).
    # BK-041 G4: + bg_live (null: this is a chat run, not a cron/watch one).
    assert set(body) == {"messages", "live", "compactions", "bg_live"}
    assert body["bg_live"] is None
    assert [m["role"] for m in body["messages"]] == ["user"]  # no in-flight step

    live = body["live"]
    assert live is not None
    # BK-041 G4: + the run's start and its last row-setting event, so a
    # mid-run open shows the real label and elapsed time (not 0:00).
    assert set(live) == {
        "since", "seq", "oldest_seq", "gap", "events", "started_ms", "phase",
    }
    assert live["phase"] == {"type": "delta"}
    assert 0 < live["started_ms"] <= time.time() * 1000
    assert live["since"] == 0  # nothing persisted mid-call: tail = whole run
    assert live["seq"] == 4  # the run's watermark
    assert live["oldest_seq"] == 1 and live["gap"] is False  # whole ring
    assert [e["type"] for e in live["events"]] == [
        "thinking_delta", "thinking_delta", "thinking_done", "delta",
    ]  # the answer still being written, thinking included, since 0
    assert [e["seq"] for e in live["events"]] == [1, 2, 3, 4]
    assert live["events"][1] == {  # exact stamped shape, no session_id
        "type": "thinking_delta", "text": "th2", "seq": 2,
        "run_id": StreamedAgent.RUN_ID,
    }

    gate.set()
    await run.task


async def test_messages_never_claims_whole_truth_from_a_stale_read(
    gw, client, monkeypatch
):
    """The null obligation under the completion race. `live: null` promises
    "messages is the whole truth", so the transcript must never be a read
    that STARTED before the run's final persist and was returned after it.
    Here the store read predates the persist by construction (the run is
    held at its gate while the read happens; it then completes before the
    read returns). A route that samples liveness only after loading would
    answer live:null plus an answer-less transcript — the 2026-10-17 loss
    shape inside the new contract."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)

    orig = gw.sessions.load_messages_indexed
    read = asyncio.Event()
    release = asyncio.Event()

    async def stale_then_release(s):
        out = await orig(s)  # the read: the store lacks the in-flight answer
        read.set()
        await release.wait()
        return out

    # The route's store read is the INDEXED loader since Step 5 (wire `idx`
    # = store idx): patch that one — the race under test is the same read.
    monkeypatch.setattr(gw.sessions, "load_messages_indexed", stale_then_release)
    req = asyncio.create_task(client.get(f"/api/sessions/{sid}/messages"))
    await asyncio.wait_for(read.wait(), 2)
    gate.set()
    await run.task  # final persist done — AFTER the store read above
    release.set()
    body = (await asyncio.wait_for(req, 2)).json()

    assert body["live"] is None  # the run is gone by the time we answer
    assert any(m["role"] == "assistant" and m["text"] == "head tail"
               for m in body["messages"]), (
        "live:null promised the whole truth, but messages predates the persist"
    )


async def test_messages_post_run_live_null_with_persisted_answer(gw, client):
    """The plan's second check: once the run is gone, live is null AND the
    assistant message is in messages — the two halves of "the store has the
    whole answer now", the cue to fall back to plain persisted rendering."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)
    _drain(run.queue)
    gate.set()
    await run.task
    await _reaped(gw, sid)

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    assert body["live"] is None
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["text"] == "head tail"  # the answer the ring was streaming


async def test_messages_live_is_null_for_a_session_with_no_run(gw, client):
    """The plan's third check, stated as shape: no run means live PRESENT
    and null — never an absent key (one shape for the client to parse, and
    `null` is the obligation "messages is the whole truth"). Unknown session
    stays 404 here (existence is this route's question; /live ignores it).
    Step 5's compactions key obeys the same rule: present, [] — never
    absent."""
    sid = await _session(gw)
    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    assert "live" in body and body["live"] is None
    assert "compactions" in body and body["compactions"] == []
    assert body["messages"] == []
    assert (
        await client.get("/api/sessions/no-such-sid/messages")
    ).status_code == 404


async def test_messages_carries_compactions_anchored_to_store_idx(gw, client):
    """Step 5 (compaction-checkpoint-bookmark): /messages gains
    `compactions: [{checkpoint_id, anchor_idx, created_at, est_before,
    est_after}]` — the session's run_events kind='compaction' rows joined
    to runs, oldest first. Pins: est_* are the trace's est_tokens_* under
    the wire names; created_at is the event ts WITH a UTC offset (the
    checkpoint row's `[:19]` stamp is not on this wire); anchor-less
    pre-Step-3 rows and other sessions' (and session-less background) runs
    never appear."""
    sid = await _session(gw)
    other = await _session(gw)
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        await gw.sessions.append_message(sid, i, Message(role=role, text=f"m{i}"))

    run = await gw.runs.start(sid, "test-model")
    await gw.runs.add_event(run, "compaction", {
        "checkpoint_id": "cp-aaa", "anchor_idx": 1,
        "est_tokens_before": 90000, "est_tokens_after": 24000,
    })
    await gw.runs.add_event(run, "compaction", {  # pre-Step-3 shape: dropped
        "messages_summarized": 12, "est_tokens_before": 5, "est_tokens_after": 2,
    })
    await gw.runs.add_event(run, "compaction", {
        "checkpoint_id": "", "anchor_idx": 3,  # failed checkpoint write
        "est_tokens_before": 80000, "est_tokens_after": 21000,
    })
    orun = await gw.runs.start(other, "test-model")
    await gw.runs.add_event(orun, "compaction", {
        "checkpoint_id": "cp-other", "anchor_idx": 0,
        "est_tokens_before": 1, "est_tokens_after": 1,
    })
    brun = await gw.runs.start(None, "test-model")  # background run
    await gw.runs.add_event(brun, "compaction", {
        "checkpoint_id": "cp-bg", "anchor_idx": 77,
        "est_tokens_before": 1, "est_tokens_after": 1,
    })

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    assert [m["idx"] for m in body["messages"]] == [0, 1, 2, 3]
    cps = body["compactions"]
    assert [c["anchor_idx"] for c in cps] == [1, 3]  # event order, anchor-less gone
    assert cps[0] == {
        "checkpoint_id": "cp-aaa", "anchor_idx": 1,
        "created_at": cps[0]["created_at"],  # timestamp asserted just below
        "est_before": 90000, "est_after": 24000,
    }
    assert cps[0]["created_at"].endswith("+00:00")  # UTC-ISO: JS Date parses it right
    assert cps[1]["checkpoint_id"] == ""  # failed write: anchor_idx still dedupes
    assert "cp-other" not in json.dumps(cps) and "cp-bg" not in json.dumps(cps)


async def test_messages_live_and_live_endpoint_never_disagree(
    gw, client, monkeypatch
):
    """The constraint that gives the shared predicate its teeth: over one
    overflowed ring, /messages' live IS /live?since=<live.since> minus
    `active` — same gap, same oldest_seq, same events (this fake persists
    nothing mid-call, so its server watermark is 0) — and after the run BOTH
    say gone. Restating the liveness predicate per-route would let one answer
    "live" and the other "gone" in the tick between task completion and
    _reap."""
    import harness.gateway.routes.chat_run as chat_run_mod

    monkeypatch.setattr(chat_run_mod, "EventRing", lambda: EventRing(capacity=3))
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_streamed(gw, sid, gate)  # seq 1-4 emitted; ring keeps 2-4

    live_field = (await client.get(f"/api/sessions/{sid}/messages")).json()["live"]
    assert live_field["since"] == 0  # fake persists nothing before its gate
    tail = {k: v for k, v in live_field.items() if k != "since"}
    body = (await client.get(f"/api/chat/{sid}/live?since={live_field['since']}")).json()
    assert body["active"] is True
    assert {k: v for k, v in body.items() if k != "active"} == tail
    assert live_field["gap"] is True and live_field["oldest_seq"] == 2
    # the helper is the single source, both routes just project it
    assert live_projection(gw, sid, since=live_field["since"]) == tail
    # a client WITH a watermark takes the cheap path instead of the tail
    cheap = (await client.get(f"/api/chat/{sid}/live?since=3")).json()
    assert [e["seq"] for e in cheap["events"]] == [4]  # bytes /messages can't match

    gate.set()
    await run.task
    await _reaped(gw, sid)
    assert (await client.get(f"/api/sessions/{sid}/messages")).json()["live"] is None
    assert (await client.get(f"/api/chat/{sid}/live")).json() == {"active": False}


async def test_done_to_reap_tick_already_says_gone(gw, client):
    """The window the shared predicate exists FOR: the run's task has
    completed, but its _reap done-callback has not run yet, so the registry
    still holds the entry. Both endpoints must already answer "gone" here —
    a registry-membership test would say "live" and hand a client a ring whose
    last step the store already holds (a double paint on resume).

    Kept SEPARATE from the equivalence test above because that one awaits
    _reaped() first, so it cannot observe this tick: after the reap both sides
    agree for the trivial reason. Here the entry is deliberately still
    present when both routes answer."""
    sid = await _session(gw)
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    ring = EventRing()
    ring.stamp({"type": "delta", "text": "head "}, "run-tick")
    gw.chat_runs[sid] = SimpleNamespace(task=task, ring=ring)
    gw.active_chats[sid] = task
    try:
        assert sid in gw.chat_runs and sid in gw.active_chats  # the tick
        assert live_projection(gw, sid) is None
        body = (await client.get(f"/api/sessions/{sid}/messages")).json()
        assert body["live"] is None
        assert (await client.get(f"/api/chat/{sid}/live")).json() == {
            "active": False
        }
    finally:
        gw.chat_runs.pop(sid, None)
        gw.active_chats.pop(sid, None)


def test_both_endpoints_share_the_liveness_predicate():
    """Structural pin for the tick between task completion and _reap: the
    predicate `run is None or run.task is None or run.task.done()` exists
    EXACTLY ONCE in the gateway package (gateway/live.py, in _live_run).
    /live and /messages each call THEIR projection (client-watermark
    live_projection, server-watermark persisted_tail_projection), and both
    projections take liveness from _live_run — neither route restates it, so
    no future edit can make one endpoint say "live" while the other says
    "gone". (stop_chat has its own task.done() guard on purpose — it needs
    the ChatRun handle for answer_complete/agent, not a replay projection.)"""
    gateway = Path(__file__).resolve().parents[1] / "src/harness/gateway"
    predicate = "run is None or run.task is None or run.task.done()"
    everywhere = {
        p: p.read_text().count(predicate)
        for p in sorted(gateway.rglob("*.py"))
    }
    hits = {p: c for p, c in everywhere.items() if c}
    assert hits == {gateway / "live.py": 1}, (
        f"liveness predicate restated outside gateway/live.py: {hits}"
    )
    for route, projection in (
        ("routes/streaming.py", "live_projection("),
        ("routes/resources.py", "persisted_tail_projection("),
    ):
        assert projection in (gateway / route).read_text(), (
            f"{route} stopped calling the gateway/live.py projection {projection!r}"
        )


# ---------------------------------------------------------------------------
# Step 4 amendment (cleric hand-back): the multi-step (tool-loop) case. The
# plan's "ring since 0" re-rendered persisted steps — a tool-loop run's
# messages already contain step 1 while replay-from-0 streams it again, and
# the client has no key joining a replayed event to a persisted row (rows
# carry no seq). So /messages replays from ChatRun.persisted_seq, the
# watermark chat_run.py advances whenever a Session add_step/add_assistant
# write returns (agent.py persists AFTER each call's events fire, so the
# watermark covers exactly the durable ones). Pinned below: the no-double-
# paint absence, the tail-only gap semantics, and the persist-window.
# ---------------------------------------------------------------------------


class ToolLoopAgent:
    """A TOOL-LOOP turn mirroring the real Agent.run_turn persist order
    (core/agent.py) so the watermark contract is pinned against a faithful
    event/write sequence:

      on_run_start → add_user
      call 1: on_model_call, delta "plan ", on_tool_call/on_tool_result
              around the (simulated) dispatch, THEN session.add_step
              (text + tool_use + results, one commit)
      call 2: on_model_call, thinking_delta, delta "head ", [gate],
              delta "tail", THEN session.add_assistant — then
              on_answer_complete (turn_complete lands AFTER the persist,
              _finish_answer's order), then an optional tail_gate standing
              in for the naming/extraction tail (task still alive).

    Every on_* here is wired by chat_run.py, and add_step/add_assistant are
    the Session methods start_chat_run wraps to advance persisted_seq — the
    fake never touches ChatRun itself, so the wrap is what the pins observe.
    """

    RUN_ID = "run-loop"

    def __init__(
        self, gate: asyncio.Event, tail_gate: asyncio.Event | None = None
    ) -> None:
        self.gate = gate
        self.tail_gate = tail_gate
        for h in (
            "on_text_delta", "on_tool_call", "on_tool_result",
            "on_thinking_delta", "on_thinking_done", "on_truncated",
        ):
            setattr(self, h, lambda *_a: None)
        # The route assigns these; run_turn only calls what's set, as Agent does.
        self.on_run_start = None
        self.on_model_call = None
        self.on_answer_complete = None
        self.last_run: dict | None = None

    def request_stop(self) -> bool:
        return True

    async def run_turn(self, session, user_input, **kwargs):
        if self.on_run_start is not None:
            self.on_run_start(self.RUN_ID)
        await session.add_user(user_input)
        if self.on_model_call is not None:  # model_wait (seq 1)
            self.on_model_call()
        self.on_text_delta("plan ")  # seq 2
        self.on_tool_call("sandbox_run", {"cmd": "ls"})  # seq 3
        self.on_tool_result("sandbox_run", "ok", False)  # seq 4
        await session.add_step(  # dispatch-then-persist, like agent.py
            "plan ",
            [ToolCall(id="tc1", name="sandbox_run", arguments={"cmd": "ls"})],
            [ToolResult(tool_call_id="tc1", content="ok", is_error=False)],
        )
        if self.on_model_call is not None:  # seq 5
            self.on_model_call()
        self.on_thinking_delta("th")  # seq 6
        self.on_text_delta("head ")  # seq 7
        await self.gate.wait()
        self.on_text_delta("tail")  # seq 8
        await session.add_assistant("head tail")  # the final persist
        if self.on_answer_complete is not None:  # turn_complete AFTER it
            self.on_answer_complete()
        if self.tail_gate is not None:  # naming/extraction tail
            await self.tail_gate.wait()
        return "head tail"


async def _park_loop(
    gw, sid, gate, tail_gate: asyncio.Event | None = None
) -> ChatRun:
    """Start a ToolLoopAgent run and wait until step 1 persisted and call 2
    is parked at its gate (seq 1-7 queued; they emit synchronously between
    awaits, so the queue is empty or complete — no partial-batch race)."""
    gw.agent_factory = lambda autonomy: ToolLoopAgent(gate, tail_gate)
    run = await start_chat_run(gw, "hi", sid)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if run.queue.qsize() >= 7:
            return run
    raise AssertionError("run never reached its gate")


async def test_messages_live_tail_skips_the_persisted_steps(gw, client):
    """THE multi-step check (cleric hand-back): with step 1 persisted, the
    transcript holds "plan " + the tool card, and live.events must contain
    NEITHER — replay-from-0 painted both twice, and no client-side dedupe
    can catch it (persisted rows carry no seq). The server watermark IS the
    join: step 1's events (seq 1-4) appear only in messages, the in-flight
    step (5-7) only in the tail."""
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_loop(gw, sid, gate)

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool"]
    assert body["messages"][1]["text"] == "plan "
    assert body["messages"][1]["tool_calls"][0]["id"] == "tc1"
    assert body["messages"][2]["tool_results"][0]["tool_call_id"] == "tc1"

    live = body["live"]
    assert live["since"] == run.persisted_seq == 4
    assert [e["seq"] for e in live["events"]] == [5, 6, 7]
    assert [e["type"] for e in live["events"]] == [
        "model_wait", "thinking_delta", "delta",
    ]
    # The provable no-double-paint: nothing persisted rides the tail.
    assert not any(e.get("text") == "plan " for e in live["events"])
    assert not any(
        e["type"] in ("tool_call", "tool_result") for e in live["events"]
    )
    assert live["oldest_seq"] == 1 and live["gap"] is False
    assert all(e["run_id"] == ToolLoopAgent.RUN_ID for e in live["events"])

    # Same numbers, same since: /live at the server watermark answers this
    # tail exactly — while /live?since=0 keeps its CLIENT semantics (the
    # whole window, persisted steps included: that endpoint answers for a
    # viewer without the transcript, e.g. /api/events catch-up, and its
    # `since` stays the client's own number — never the server's).
    tail = (
        await client.get(f"/api/chat/{sid}/live?since={live['since']}")
    ).json()
    assert {k: v for k, v in tail.items() if k != "active"} == {
        k: v for k, v in live.items() if k != "since"
    }
    whole = (await client.get(f"/api/chat/{sid}/live?since=0")).json()
    assert [e["seq"] for e in whole["events"]] == [1, 2, 3, 4, 5, 6, 7]

    gate.set()
    await run.task


async def test_messages_tail_gap_eats_only_unpersisted_events(
    gw, client, monkeypatch
):
    """What `gap` MEANS at the server watermark: persisted history can no
    longer be lost from live (its events sit at or below `since` and the
    store holds their rows), so a gap here proves the IN-FLIGHT STEP itself
    outgrew the ring. Shrunk ring: step 1's four events overflow away, yet
    messages stay WHOLE and since stays 4 — the loss is bounded to one
    model call (unlike step 3's since=0 gap), the suffix paints from
    oldest_seq, and completion heals it."""
    import harness.gateway.routes.chat_run as chat_run_mod

    monkeypatch.setattr(chat_run_mod, "EventRing", lambda: EventRing(capacity=2))
    sid = await _session(gw)
    gate = asyncio.Event()
    run = await _park_loop(gw, sid, gate)  # 7 emitted; ring keeps 6-7

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    live = body["live"]
    assert live["since"] == 4  # the overflow cannot move the persisted join
    assert live["gap"] is True and live["oldest_seq"] == 6
    assert [e["seq"] for e in live["events"]] == [6, 7]  # suffix from oldest
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool"]
    assert body["messages"][1]["text"] == "plan "  # gap never eats the transcript

    gate.set()
    await run.task
    await _reaped(gw, sid)
    done = (await client.get(f"/api/sessions/{sid}/messages")).json()
    assert done["live"] is None  # healed: the store now holds everything
    assert done["messages"][-1]["text"] == "head tail"


async def test_messages_join_is_never_ahead_of_its_transcript(
    gw, client, monkeypatch
):
    """`live.since` names the seq boundary of the rows in `messages`, so both
    must be sampled at ONE instant. A step that PERSISTS while the store read
    is in flight advances the watermark past a row that read never saw: the
    client would paint the older transcript and replay only events AFTER that
    step — silently skipping its text and tool cards until run_done (the
    head-loss shape, in the completed-step direction).

    Held open by construction: the read snapshots step 1, the run then
    persists step 2 before the read returns, and the run stays LIVE (naming
    tail, task alive) so this is not the null-flip case the stale-read test
    covers."""
    sid = await _session(gw)
    gate = asyncio.Event()
    tail_gate = asyncio.Event()
    run = await _park_loop(gw, sid, gate, tail_gate)  # since=4, call 2 parked

    orig = gw.sessions.load_messages_indexed  # the route's read seam since Step 5
    read = asyncio.Event()
    release = asyncio.Event()
    first = True

    async def stale_read(s):
        nonlocal first
        out = await orig(s)  # the snapshot: step 2 is not persisted yet
        if first:
            first = False
            read.set()
            await release.wait()
        return out

    monkeypatch.setattr(gw.sessions, "load_messages_indexed", stale_read)
    req = asyncio.create_task(client.get(f"/api/sessions/{sid}/messages"))
    await asyncio.wait_for(read.wait(), 2)
    gate.set()  # step 2 completes and persists WHILE the read is held
    for _ in range(200):
        await asyncio.sleep(0.01)
        if run.persisted_seq >= 8:
            break
    assert run.persisted_seq == 8  # "head tail" is durable now
    release.set()
    body = (await asyncio.wait_for(req, 2)).json()

    live = body["live"]
    assert live is not None  # the naming tail keeps the run live
    assert live["since"] == 8  # the join covers step 2...
    assert body["messages"][-1]["text"] == "head tail", (
        "live.since is ahead of the transcript: the tail skips a persisted step"
    )
    tail_gate.set()
    await run.task


class CancelPersistAgent(StreamedAgent):
    """A turn that streams text, parks, and on cancel persists the partial
    before re-raising — agent.py's `_persist_partial_on_cancel` shape (text
    only, shielded). Stop must not lose the streamed answer, and the persist
    must go through the same wrapped Session write the watermark listens to."""

    async def run_turn(self, session, user_input, **kwargs):
        if self.on_run_start is not None:
            self.on_run_start(self.RUN_ID)
        await session.add_user(user_input)
        self.on_text_delta("head ")
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            await asyncio.shield(session.add_assistant("head "))
            raise


async def test_stop_persists_the_partial_and_messages_then_says_gone(
    gw, client
):
    """The OTHER "not live" shape: a cancelled run. The streamed partial IS
    persisted (agent.py's `_persist_partial_on_cancel`, shielded) before the
    task dies, so `/messages` must answer live:null beside it — never null
    with the text the user already saw missing (the Stop branch of the
    contract's "or the turn was cancelled/errored and its partial is")."""
    sid = await _session(gw)
    gate = asyncio.Event()
    gw.agent_factory = lambda autonomy: CancelPersistAgent(gate)
    run = await start_chat_run(gw, "hi", sid)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not run.queue.empty():
            break
    run.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run.task
    await _reaped(gw, sid)

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    assert body["live"] is None
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][-1]["text"] == "head "


async def test_messages_tail_is_signals_only_during_the_persisting_tail(
    gw, client
):
    """The ANSWER-end window the watermark exists for: once add_assistant
    persists the final step, turn_complete fires (AFTER the persist —
    _finish_answer's order) and the naming/extraction tail runs with the
    task still alive. A cold open in that window must find the answer in
    messages and NOTHING paintable left in the tail — else the just-
    persisted answer double-paints while the user waits on the spinner."""
    sid = await _session(gw)
    gate = asyncio.Event()
    tail_gate = asyncio.Event()
    run = await _park_loop(gw, sid, gate, tail_gate)
    _drain(run.queue)
    gate.set()
    for _ in range(200):  # wait for add_assistant's watermark (delta = seq 8)
        await asyncio.sleep(0.01)
        if run.persisted_seq >= 8:
            break

    body = (await client.get(f"/api/sessions/{sid}/messages")).json()
    live = body["live"]
    assert live is not None  # the task is alive (naming/extraction tail)
    assert live["since"] == run.persisted_seq == 8
    assert [e["type"] for e in live["events"]] == ["turn_complete"]
    assert not any("text" in e for e in live["events"])  # nothing paintable
    assert body["messages"][-1]["text"] == "head tail"  # the answer IS stored

    tail_gate.set()
    await run.task
