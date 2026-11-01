"""Per-run in-memory event ring — the replay source for live resume.

pwa-accurate-resume Step 3: every event a chat run emits is numbered ONCE
(`seq`, monotonic per run) and copied here BEFORE routing to any sink, so a
client whose SSE socket died can resync by number: GET
/api/chat/{sid}/live?since=N replays exactly the events with seq > N.

Two properties the resume contract depends on, both deliberate:

- WRITTEN AT EMIT, NEVER VIA THE PUBLISHER. RunBroadcaster.publish() is sync
  and drop-on-full (see broadcast.py); a ring fed by the publisher would be
  silently lossy exactly when backpressure hits — the moment replay matters
  most. The run's emit() closure stamps and records before choosing a sink,
  so the attached queue, the detach reroute and the broadcaster all carry
  the identical number for the same event, ring or no ring.
- NOT DURABLE. The ring lives and dies with the run's gw.active_chats /
  gw.chat_runs entry: nothing lands in SQLite, nothing survives an agentd
  restart, and the replay window is the live run only. Once the run is gone
  /live answers {active: false} and the client falls back to the persisted
  refetch (the run_done heal stays the post-run path).

A ring that overflows drops its OLDEST events; seq numbers keep climbing, so
the watermark stays honest, and `replay()` says so explicitly: `oldest_seq`
is the oldest event still replayable and `gap` is true iff events the client
asked for were dropped off the front (`since + 1 < oldest_seq`) — the client
never infers overflow from seq arithmetic. A gap'd head is GONE from live
data and NOT recoverable while the run is live — the persisted store has no
in-flight assistant message (decisions 2026-10-17), so a mid-call refetch
is exactly the lossy path this quest exists to kill. The client paints the
surviving suffix; the run_done refetch heals the transcript once the run
ends.
"""

from __future__ import annotations

from collections import deque

#: Ring capacity per live run, sized in the unit its failure is measured in.
#: NOT broadcast.QUEUE_MAXSIZE's reasoning (shared number, different failure:
#: a lagging subscriber re-syncs from the store at run end, but a ring head
#: dropped mid-run is unrecoverable until run_done) — this window IS the
#: away-time a resume can cover, so it is sized to cover the quest's normal
#: path. Measured on the live harness.db (2026-10-18, read-only): the longest
#: web chat run emitted 31,159 output tokens over 3001 s (~10.4 tok/s; the
#: provider fires one delta per upstream chunk and llama.cpp chunks are
#: ~token-sized, so ~1 event per token); five more ran 13k-27k tokens.
#: 65,536 = 2.1x the longest observed run, i.e. ~105 minutes of continuous
#: streaming at the observed rate before a watermark can fall off the front.
#: Cost, measured with unique delta strings: ~270 B/event, ~17 MiB per FULLY
#: USED ring — on a 64 GB single-user host with one live chat per session (a
#: handful at most), noise. Overflow is honest, not silent: /live reports
#: `gap` + `oldest_seq` (see replay()).
RING_CAPACITY = 65_536


class EventRing:
    """Monotonic seq stamp + bounded event buffer for ONE chat run.

    Not thread-safe by design: every call happens on the daemon's event
    loop, from the agent's synchronous stream hooks — the same context as
    the asyncio queue it feeds.
    """

    __slots__ = ("_events", "_seq")

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        self._events: deque[dict] = deque(maxlen=capacity)
        self._seq = 0

    def stamp(self, ev: dict, run_id: str | None) -> dict:
        """Number `ev`, record it, and return the stamped COPY.

        Called exactly once per event — in the run's emit() closure, before
        any sink sees it. Every later copy (the broadcaster's, tagged with
        session_id) inherits this number; run_id is the run's once
        on_run_start has fired (None for events emitted before it)."""
        self._seq += 1
        stamped = {**ev, "seq": self._seq, "run_id": run_id}
        self._events.append(stamped)
        return stamped

    @property
    def seq(self) -> int:
        """Highest seq issued (0 before the first event) — the run's
        watermark, reported by /live beside `events`. The drop-detector a
        client reads is `replay()`'s gap (`len(events)` alone cannot tell,
        since `since` already trims the tail)."""
        return self._seq

    @property
    def oldest_seq(self) -> int | None:
        """Seq of the OLDEST event still retained (None before the first).
        The retained window is exactly oldest_seq..seq, dense — nothing
        inside it is ever skipped."""
        return self._events[0]["seq"] if self._events else None

    def replay(self, since: int) -> dict:
        """The resync batch for a client's watermark, with honest flags:
        {seq, oldest_seq, gap, events}.

        `gap` is the SUPPORTED way to learn the head is gone: true iff the
        client asked (seq > since) for events the ring has already dropped
        off the front (since + 1 < oldest_seq) — the client never does seq
        arithmetic to find out. A gap'd head is not recoverable while the
        run is live (the store has no in-flight message), so the suffix must
        never be painted as if it were the whole run; run_done's refetch
        heals it. A caught-up or in-window client always gets gap: false."""
        n = max(since, 0)
        oldest = self._events[0]["seq"] if self._events else None
        return {
            "seq": self._seq,
            "oldest_seq": oldest,
            "gap": oldest is not None and n + 1 < oldest,
            "events": [e for e in self._events if e["seq"] > n],
        }

    def since(self, n: int) -> list[dict]:
        """Recorded events with seq > n, oldest first — replay()'s batch
        without the flags (direct callers own the overflow question)."""
        return [e for e in self._events if e["seq"] > n]
