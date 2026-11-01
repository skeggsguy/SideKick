"""RunBroadcaster: live fan-out of background-run output to web clients."""

from __future__ import annotations

import asyncio

from harness.gateway.broadcast import QUEUE_MAXSIZE, RunBroadcaster


async def test_publish_fans_out_to_all_subscribers():
    hub = RunBroadcaster()
    a, b = hub.subscribe(), hub.subscribe()
    assert hub.subscriber_count == 2

    hub.publish({"type": "delta", "text": "hi"})
    assert a.get_nowait() == {"type": "delta", "text": "hi"}
    assert b.get_nowait() == {"type": "delta", "text": "hi"}


async def test_unsubscribe_stops_delivery():
    hub = RunBroadcaster()
    q = hub.subscribe()
    hub.unsubscribe(q)
    assert hub.subscriber_count == 0
    hub.publish({"type": "delta", "text": "x"})
    assert q.empty()


async def test_full_buffer_drops_for_that_client_only():
    hub = RunBroadcaster()
    slow = hub.subscribe()
    # Fill the slow client's buffer to the brim (it never drains).
    for i in range(QUEUE_MAXSIZE):
        hub.publish({"type": "delta", "text": str(i)})
    assert slow.qsize() == QUEUE_MAXSIZE

    # A healthy client that joins now must still receive the next event even
    # though the slow client is full — backpressure is isolated per client.
    fast = hub.subscribe()
    hub.publish({"type": "delta", "text": "overflow"})

    assert slow.qsize() == QUEUE_MAXSIZE  # capped, dropped its overflow
    assert fast.get_nowait() == {"type": "delta", "text": "overflow"}


async def test_publish_with_no_subscribers_is_a_noop():
    hub = RunBroadcaster()
    hub.publish({"type": "delta", "text": "nobody listening"})  # must not raise
    assert hub.subscriber_count == 0


async def test_subscribe_during_publish_is_safe():
    # publish iterates a snapshot, so a queue added/removed mid-fan-out can't
    # corrupt the loop.
    hub = RunBroadcaster()
    q = hub.subscribe()
    await asyncio.sleep(0)  # ensure we're on the loop
    hub.publish({"type": "run_done", "session_id": "s1", "text": ""})
    assert q.get_nowait()["type"] == "run_done"


# --- PageStreams: the duplicate-subscription diagnostic ----------------------
# One stderr line only when a page holds >1 live /api/events stream; the
# single-client path (constant visibility toggles) must stay silent.


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _tracker():
    from harness.gateway.broadcast import PageStreams

    clock = _Clock()
    return PageStreams(clock=clock), clock


def test_page_streams_first_open_is_silent():
    tr, _ = _tracker()
    handle, line = tr.open("abc12345", 1, "iPhone", subscribers=1)
    assert handle is not None
    assert line is None


def test_page_streams_second_open_names_both_ns():
    tr, clock = _tracker()
    tr.open("abc12345", 1, "iPhone", subscribers=1)
    clock.t += 3.5
    _, line = tr.open("abc12345", 2, "iPhone", subscribers=2)
    assert line is not None
    assert line.startswith("[events] page abc12345 opened stream n=2 while 1 live")
    assert "n=1 opened 3.5s ago" in line
    assert "subscribers=2" in line
    assert line.endswith("ua=iPhone")


def test_page_streams_same_n_twice_still_flagged():
    # One EventSource whose request got duplicated (Safari/proxy) reuses n —
    # the diagnostic must still see it; it's one of the three mechanisms.
    tr, _ = _tracker()
    tr.open("abc12345", 4, "iPad", subscribers=1)
    _, line = tr.open("abc12345", 4, "iPad", subscribers=2)
    assert line is not None
    assert "opened stream n=4 while 1 live (n=4 opened" in line


def test_page_streams_close_removes_and_logs_only_duplicates():
    tr, clock = _tracker()
    h1, _ = tr.open("abc12345", 1, "iPhone", subscribers=1)
    h2, _ = tr.open("abc12345", 2, "iPhone", subscribers=2)
    clock.t += 2.0
    # Closing one of a concurrent pair → a matching line.
    assert tr.close(h1) == "[events] page abc12345 closed n=1 after 2.0s; 1 left"
    # The survivor is now alone: closing it is the ordinary path, silent.
    assert tr.close(h2) is None
    assert tr.live_count("abc12345") == 0
    # And a fresh open after everything closed is a first open again.
    _, line = tr.open("abc12345", 3, "iPhone", subscribers=1)
    assert line is None


def test_page_streams_close_is_idempotent_and_none_safe():
    tr, _ = _tracker()
    h, _ = tr.open("abc12345", 1, "iPhone", subscribers=1)
    assert tr.close(h) is None
    assert tr.close(h) is None  # double close never raises
    assert tr.close(None) is None


def test_page_streams_different_pages_are_independent():
    tr, _ = _tracker()
    tr.open("page-a", 1, "iPhone", subscribers=1)
    _, line = tr.open("page-b", 1, "iPhone", subscribers=2)
    assert line is None


def test_page_streams_untracked_page_none():
    # An old cached client sends no page: never tracked, never logged.
    tr, _ = _tracker()
    h1, l1 = tr.open(None, None, "other", subscribers=1)
    h2, l2 = tr.open(None, None, "other", subscribers=2)
    assert (h1, l1, h2, l2) == (None, None, None, None)
    assert tr.close(h1) is None


def test_page_streams_invariant_over_random_sequences():
    # Generated open/close sequences: live_count always equals the model's,
    # an open logs iff the page already had a live stream, a close logs iff
    # at least one stream remains, and nothing lingers after all close.
    import random

    from harness.gateway.broadcast import PageStreams

    rng = random.Random(1234)
    for _ in range(50):
        tr = PageStreams(clock=lambda: 0.0)
        model: dict[str, list] = {}
        for _ in range(60):
            page = rng.choice(["p1", "p2", "p3"])
            live = model.setdefault(page, [])
            if live and rng.random() < 0.5:
                h = live.pop(rng.randrange(len(live)))
                line = tr.close(h)
                assert (line is not None) == bool(live)
            else:
                h, line = tr.open(page, rng.randint(1, 3), "other", subscribers=0)
                assert (line is not None) == bool(live)
                live.append(h)
            assert tr.live_count(page) == len(live)
        for live in model.values():
            for h in live:
                tr.close(h)
        assert all(tr.live_count(p) == 0 for p in ("p1", "p2", "p3"))
        assert tr._pages == {}


def test_device_class_reduces_user_agent():
    from harness.gateway.broadcast import device_class

    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) AppleWebKit/605.1.15"
    ipad = "Mozilla/5.0 (iPad; CPU OS 27_0 like Mac OS X) AppleWebKit/605.1.15"
    android = "Mozilla/5.0 (Linux; Android 16; SM-S938B) AppleWebKit/537.36 Chrome/140"
    mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
    assert device_class(iphone) == "iPhone"
    assert device_class(ipad) == "iPad"
    assert device_class(android) == "Android"
    # iPadOS Safari/PWAs send a desktop Macintosh UA by default — the server
    # can't tell them apart, so the class says so rather than guessing.
    assert device_class(mac) == "Mac/iPad"
    assert device_class("curl/8.7.1") == "other"
    assert device_class(None) == "other"
