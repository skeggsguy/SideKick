"""SlotWatcher: /slots classification during a chat turn's model waits.

Polls are driven by a queue-gated fake fetcher — the test hands over one
slot state per poll, so emitted sequences are exact with no timing races
(the interval only gates how fast a queued state is consumed)."""

from __future__ import annotations

import asyncio

from harness.gateway.model_status import SlotWatcher, _n_decoded


def slot(**overrides) -> dict:
    base = {
        "id": 0,
        "id_task": 1,
        "is_processing": True,
        "n_prompt_tokens": 1000,
        "n_prompt_tokens_processed": 0,
        "n_prompt_tokens_cache": 0,
        "next_token": [{"n_decoded": 0}],
    }
    base.update(overrides)
    return base


class ClockFetch:
    """Each fetch blocks until the test pushes the next slot state (or an
    exception to raise). An exhausted script just idles — no extra polls."""

    def __init__(self) -> None:
        self._next: asyncio.Queue = asyncio.Queue()

    async def __call__(self) -> list[dict]:
        item = await self._next.get()
        if isinstance(item, Exception):
            raise item
        return [item]

    async def poll(self, item) -> None:
        await self._next.put(item)


# > 4x the poll interval: guarantees each queued state is consumed.
STEP = 0.05
INTERVAL = 0.01


def _busy():
    return {"type": "status", "phase": "busy"}


def _reading(pct: int):
    return {"type": "status", "phase": "reading", "pct": pct}


async def test_other_clients_request_is_busy_emitted_once():
    fetch = ClockFetch()
    emitted: list[dict] = []
    w = SlotWatcher(fetch, emitted.append, interval=INTERVAL)
    task = asyncio.create_task(w.watch())

    other = slot(id_task=5, next_token=[{"n_decoded": 2}])
    await fetch.poll(other)
    await asyncio.sleep(STEP)  # first poll: baseline=5, not waiting → quiet

    w.mark_waiting()  # baseline = 5
    await fetch.poll(other)
    await asyncio.sleep(STEP)
    await fetch.poll(other)
    await asyncio.sleep(STEP)  # unchanged classification → suppressed

    task.cancel()
    assert emitted == [_busy()]


async def test_our_prefill_reads_as_pct_buckets():
    fetch = ClockFetch()
    emitted: list[dict] = []
    w = SlotWatcher(fetch, emitted.append, interval=INTERVAL)
    task = asyncio.create_task(w.watch())

    await fetch.poll(slot(id_task=1))
    await asyncio.sleep(STEP)  # baseline=1

    w.mark_waiting()
    await fetch.poll(slot(id_task=2, n_prompt_tokens_processed=400))
    await asyncio.sleep(STEP)
    await fetch.poll(slot(id_task=2, n_prompt_tokens_processed=420))
    await asyncio.sleep(STEP)  # 42% → still the 40 bucket → suppressed
    await fetch.poll(
        slot(id_task=2, n_prompt_tokens_processed=300, n_prompt_tokens_cache=300)
    )
    await asyncio.sleep(STEP)  # cache+processed = 60% → emitted

    w.mark_output()  # our first token arrived — deltas own the row
    await fetch.poll(slot(id_task=2, next_token=[{"n_decoded": 1}]))
    await asyncio.sleep(STEP)

    task.cancel()
    assert emitted == [_reading(40), _reading(60)]


async def test_mark_output_disarms_and_rearming_reannounces():
    fetch = ClockFetch()
    emitted: list[dict] = []
    w = SlotWatcher(fetch, emitted.append, interval=INTERVAL)
    task = asyncio.create_task(w.watch())

    other = slot(id_task=7, next_token=[{"n_decoded": 3}])
    await fetch.poll(other)
    await asyncio.sleep(STEP)

    w.mark_waiting()
    await fetch.poll(other)
    await asyncio.sleep(STEP)
    assert emitted == [_busy()]

    w.mark_output()  # our stream started; the watcher must go quiet
    await fetch.poll(other)
    await asyncio.sleep(STEP)
    assert emitted == [_busy()]

    # Second model call (tool loop): the same classification is re-announced,
    # because a new wait is a new row for the UI.
    w.mark_waiting()
    await fetch.poll(other)
    await asyncio.sleep(STEP)
    task.cancel()
    assert emitted == [_busy(), _busy()]


async def test_our_generation_decoding_emits_nothing():
    fetch = ClockFetch()
    emitted: list[dict] = []
    w = SlotWatcher(fetch, emitted.append, interval=INTERVAL)
    task = asyncio.create_task(w.watch())

    await fetch.poll(slot(id_task=1))
    await asyncio.sleep(STEP)  # baseline=1
    w.mark_waiting()
    # Ours (id_task > baseline) and tokens are flowing → the deltas own the row.
    await fetch.poll(slot(id_task=2, next_token=[{"n_decoded": 5}]))
    await asyncio.sleep(STEP)
    task.cancel()
    assert emitted == []


async def test_fetch_error_stops_polling_quietly(capsys):
    fetch = ClockFetch()
    emitted: list[dict] = []
    w = SlotWatcher(fetch, emitted.append, interval=INTERVAL)
    w.mark_waiting()

    task = asyncio.create_task(w.watch())
    await fetch.poll(RuntimeError("connection refused"))
    await asyncio.sleep(STEP)

    # watch() RETURNS (never raises into the turn), one stderr line, no events.
    assert task.done() and not task.cancelled() and not task.exception()
    assert emitted == []
    assert "poll failed" in capsys.readouterr().err

    # Polling really stopped: pushing another state changes nothing.
    await fetch.poll(slot(id_task=9))
    await asyncio.sleep(STEP)
    assert emitted == []


async def test_idle_slot_emits_nothing():
    fetch = ClockFetch()
    emitted: list[dict] = []
    w = SlotWatcher(fetch, emitted.append, interval=INTERVAL)
    task = asyncio.create_task(w.watch())

    w.mark_waiting()
    await fetch.poll(slot(is_processing=False))
    await asyncio.sleep(STEP)
    task.cancel()
    assert emitted == []


def test_n_decoded_defensive_parsing():
    assert _n_decoded({}) == 0
    assert _n_decoded({"next_token": []}) == 0
    assert _n_decoded({"next_token": [{"n_decoded": 3}]}) == 3
    assert _n_decoded({"next_token": [{"n_decoded": "x"}]}) == 0
    assert _n_decoded({"next_token": {"n_decoded": 2}}) == 0  # not a list
