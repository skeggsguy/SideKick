"""The ONE liveness predicate + the two replay projections built on it.

pwa-accurate-resume Step 4: two endpoints answer "what is live on this
session" — GET /api/chat/{sid}/live (resync with a CLIENT watermark) and the
`live` field of GET /api/sessions/{sid}/messages (cold open: one fetch = the
whole screen, joined at the SERVER's persisted watermark). They MUST never
disagree about liveness: a client that consults both in the same instant
cannot get "live" from one and "gone" from the other. The tick where they
could is real — a run's task COMPLETES first, then its _reap done-callback
pops the gw.chat_runs entry; in between, a predicate keyed on registry
membership says "live" while one keyed on the task being done says "gone".
So the predicate lives HERE, once, in _live_run (run missing, its task
handle unset, or the task done means NOT live) — pinned structurally in
tests/test_chat_recovery.py. Task-done being authoritative is also what
makes /messages' contract sound: by the time the run's task is done,
run_turn has returned, so the assistant step IS persisted (or the turn was
cancelled/errored and its partial is) — "not live" always pairs with "the
persisted store already has the whole answer", never a moment early.

THE TWO SINCES ARE DIFFERENT NUMBERS — do not conflate them:

- live_projection(since=N) — N is the CLIENT's watermark ("I have painted
  through seq N"); since=0 is the whole live window. A gap here means events
  the client asked for are gone from the ring front, and some of them may be
  in NEITHER the store nor the client — paint the suffix from oldest_seq,
  heal at run_done (step 3's semantics, unchanged).
- persisted_tail_projection() — the SERVER's watermark, run.persisted_seq:
  every event at or below it is covered by the persisted transcript, so the
  batch is exactly the UNPERSISTED tail and carries its `since` for the
  client to join on. A gap here can only eat UNPERSISTED events (the
  in-flight step outgrew the ring — >65,536 events in ONE model call, beyond
  any observed run): the persisted half stays whole, the suffix paints from
  oldest_seq, and the loss heals the moment that step persists (its rows are
  in the store; run_done's refetch is the canonical moment).

Both batches are ring.replay(...) verbatim otherwise — {seq, oldest_seq,
gap, events} (gateway/event_ring.py owns `gap`'s arithmetic: the SUPPORTED
overflow detector). Nothing here is durable: the ring is the live run's
memory and dies with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import Gateway
    from .routes.chat_run import ChatRun


# The events that set the activity row's label (webui/labels.js labelFor).
PHASE_TYPES = frozenset({
    "model_wait", "status", "thinking_delta", "thinking_done",
    "compaction_start", "delta", "tool_start", "tool_call", "tool_result",
})


def _slim(ev: dict) -> dict:
    """A phase event minus its payload: the label needs the type, the tool
    (call_tool: the wrapped name) and a status's phase/pct — never text,
    args or a result digest (untrusted, and MiB-scale on a join)."""
    out = {k: ev[k] for k in ("type", "tool", "phase", "pct") if k in ev}
    name = (ev.get("args") or {}).get("name") if ev.get("tool") == "call_tool" else None
    if isinstance(name, str):
        out["args"] = {"name": name}
    return out


@dataclass
class LivePhase:
    """When a live run started and the last event that set its row
    (BK-041 G4/G5): a page joining mid-run shows the current label with the
    real elapsed time, not "Waiting for model…" from 0:00."""

    started_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    event: dict | None = None

    def observe(self, ev: dict) -> None:
        if ev.get("type") in PHASE_TYPES:
            self.event = _slim(ev)

    def view(self) -> dict[str, Any]:
        return {"started_ms": self.started_ms, "phase": self.event}


class BgLive:
    """LivePhase per background (cron/watch) session, fed by the
    orchestrator's run events on their way to the broadcaster — a bg run has
    no ChatRun or ring, so /messages' `bg_live` reads this instead."""

    def __init__(self) -> None:
        self._runs: dict[str, LivePhase] = {}

    def observe(self, ev: dict) -> None:
        sid, t = ev.get("session_id"), ev.get("type")
        if t == "run_start" and sid:
            self._runs[sid] = LivePhase()
        elif t == "run_done":
            self._runs.pop(sid, None)
        elif sid in self._runs:
            self._runs[sid].observe(ev)

    def view(self, session_id: str) -> dict[str, Any] | None:
        run = self._runs.get(session_id)
        return run.view() if run else None


def _live_run(gw: "Gateway", session_id: str) -> "ChatRun | None":
    """The session's live chat run, or None. THE liveness predicate — both
    projections below, and through them both endpoints, take liveness from
    this guard alone."""
    run = gw.chat_runs.get(session_id)
    if run is None or run.task is None or run.task.done():
        return None
    return run


def live_projection(
    gw: "Gateway", session_id: str, since: int = 0
) -> dict[str, Any] | None:
    """Replay batch for a CLIENT watermark (`since`), or None when no run is
    live — what GET /api/chat/{sid}/live answers {active, ...} from."""
    run = _live_run(gw, session_id)
    if run is None:
        return None
    return {**run.ring.replay(since), **run.phase.view()}


def persisted_tail_projection(
    gw: "Gateway", session_id: str
) -> dict[str, Any] | None:
    """Replay batch from the SERVER's persisted watermark, or None when no
    run is live — what GET /api/sessions/{sid}/messages answers `live` from.
    Shape: {since, seq, oldest_seq, gap, events}; `events` is exactly what
    the persisted `messages` do NOT contain, and `since` tells the client
    where the join happened (its own painted watermark becomes `seq` after
    the tail, never `since` — see gateway/live.py's two-sinces note)."""
    run = _live_run(gw, session_id)
    if run is None:
        return None
    return {
        "since": run.persisted_seq,
        **run.ring.replay(run.persisted_seq),
        **run.phase.view(),
    }
