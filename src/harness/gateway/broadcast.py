"""Live fan-out of background-run output to connected web clients.

Interactive chat (/api/chat) streams over its own per-request SSE response, so
it already feels instant. Triggered/scheduled/watch runs have no such request:
without this their text is invisible until the run completes and a push fires
(minutes, if the run sat blocked on an approval). The broadcaster lets the
orchestrator publish each background run's text/tool events to every browser
subscribed to /api/events, tagged with the session id so the UI renders them
into the right conversation as they happen.

Backpressure: a slow or stuck client must never stall an agent run. publish()
is synchronous and non-blocking (put_nowait) so it's safe to call straight
from the agent's streaming callbacks; if a subscriber's buffer is full the
event is dropped for that client only — it re-syncs from stored messages the
next time it opens the session.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

# Per-subscriber buffer. A burst of token deltas is fine; a client this far
# behind is effectively gone, so we drop rather than grow without bound.
# Sized for extended thinking: thinking deltas roughly double-to-triple the
# event volume of a background run.
QUEUE_MAXSIZE = 4000


class RunBroadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()

    def subscribe(self) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict) -> None:
        """Fan an event out to every current subscriber. Never blocks and never
        raises — a full subscriber buffer simply drops the event for that one
        client (iterate a copy so unsubscribe during publish is safe)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


# --- Duplicate-subscription diagnostic --------------------------------------
# 2026-10-13: one phone held TWO /api/events subscriptions and drew streamed
# words twice. Each page now tags its stream (?page=<id>&n=<EventSource #>);
# PageStreams logs only when a page holds more than one live stream, so the
# log tells an orphan EventSource (same page, different n) from a duplicated
# request (same n) from two documents (different pages). Diagnostic only —
# it changes no delivery.


def device_class(user_agent: str | None) -> str:
    """Reduce a User-Agent to a device class — never log the full string.
    iPadOS sends a desktop Macintosh UA by default, so that class is honest
    about being ambiguous."""
    ua = user_agent or ""
    for marker, label in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Macintosh", "Mac/iPad"),
    ):
        if marker in ua:
            return label
    return "other"


@dataclass(eq=False)  # identity, not value: two streams may share page + n
class _PageStream:
    page: str
    n: int | None
    opened: float


class PageStreams:
    """Live /api/events streams per client page id."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pages: dict[str, list[_PageStream]] = {}

    def live_count(self, page: str) -> int:
        return len(self._pages.get(page, ()))

    def open(
        self, page: str | None, n: int | None, ua: str, *, subscribers: int
    ) -> tuple[_PageStream | None, str | None]:
        """Register a stream. Returns (handle, log line) — the line only when
        the page already had a live stream. page=None is never tracked."""
        if page is None:
            return None, None
        now = self._clock()
        live = self._pages.setdefault(page, [])
        line = None
        if live:
            others = ", ".join(
                f"n={s.n} opened {now - s.opened:.1f}s ago" for s in live
            )
            line = (
                f"[events] page {page} opened stream n={n} while {len(live)} "
                f"live ({others}); subscribers={subscribers}; ua={ua}"
            )
        handle = _PageStream(page, n, now)
        live.append(handle)
        return handle, line

    def close(self, handle: _PageStream | None) -> str | None:
        """Drop a stream. Returns a log line only when others remain on the
        page (a duplicate closing); idempotent."""
        if handle is None:
            return None
        live = self._pages.get(handle.page)
        if not live or handle not in live:
            return None
        live.remove(handle)
        if not live:
            del self._pages[handle.page]
            return None
        age = self._clock() - handle.opened
        return (
            f"[events] page {handle.page} closed n={handle.n} after {age:.1f}s; "
            f"{len(live)} left"
        )
