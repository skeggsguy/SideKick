"""Local-lane model status: poll llama.cpp's /slots during a chat turn.

The main model is a local llama.cpp server with ONE slot (-np 1) shared with
other clients (opencode among them) — a chat can queue for minutes behind
another request with zero bytes on its own wire. GET /slots is metadata-only
(it does NOT touch the prompt cache; /metrics is not enabled on this server —
don't use it). While the turn is waiting for the model, this classifies slot
0 and emits coarse `status` events so the web UI can say *what* the wait is:

  busy    — someone else's request holds the model (id_task <= baseline)
  reading — our own prefill, as a % (cache + processed / n_prompt)

Heuristic: id_task only tells whose request it is relative to the last poll
before our send (the baseline) — a request that sneaks in between the
baseline poll and our send can be mislabelled. Accepted.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Awaitable, Callable

import httpx

Fetch = Callable[[], Awaitable[list[dict]]]
Emit = Callable[[dict], None]


def make_slots_fetch(local_url: str) -> Fetch:
    """The real fetcher: GET {local_url}/slots. Short timeout + trust_env=False
    (house style, cf. tools/browser_fetch.py) — an env proxy must never sit
    between us and the local server, and a wedged one must not hold the loop."""

    async def fetch() -> list[dict]:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            resp = await client.get(f"{local_url.rstrip('/')}/slots")
            resp.raise_for_status()  # e.g. 501 when slots are disabled
            data = resp.json()
        slots = data if isinstance(data, list) else [data]
        return [s for s in slots if isinstance(s, dict)]

    return fetch


def _n_decoded(slot: dict) -> int:
    """Decoded-token count of the in-flight generation, from the verified-live
    field path next_token[0].n_decoded (0 = still prefilling / unknown)."""
    try:
        return int((slot.get("next_token") or [{}])[0]["n_decoded"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


class SlotWatcher:
    """Polls the slot list every `interval` for the life of one chat turn.
    mark_waiting() (on_model_call hook) arms the classifier with the current
    baseline task id; mark_output() (first delta/tool_call) disarms it.
    `status` events go through `emit` — only when the classification changes
    (pct bucketed to 5). watch() never raises: a fetch failure prints ONE
    stderr line and stops polling for this turn."""

    def __init__(self, fetch: Fetch, emit: Emit, interval: float = 2.0) -> None:
        self._fetch = fetch
        self._emit = emit
        self._interval = interval
        self._waiting = False
        self._baseline: Any = None
        self._last_seen_task_id: Any = None
        self._last_key: tuple | None = None
        self._disabled = False

    def mark_waiting(self) -> None:
        self._waiting = True
        self._baseline = self._last_seen_task_id
        # A new model call re-announces its own progress even if it matches
        # what the previous call last emitted.
        self._last_key = None

    def mark_output(self) -> None:
        self._waiting = False

    async def watch(self) -> None:
        while not self._disabled:
            await asyncio.sleep(self._interval)
            try:
                slots = await self._fetch()
            except Exception as e:
                print(
                    f"[model_status] llama /slots poll failed — status "
                    f"polling off for this turn: {e}",
                    file=sys.stderr,
                )
                self._disabled = True
                return
            slot = slots[0] if slots else {}
            task_id = slot.get("id_task")
            if task_id is not None:
                self._last_seen_task_id = task_id
            if not self._waiting:
                continue
            status = self._classify(slot)
            if status is None:
                continue
            key = (status["phase"], status.get("pct"))
            if key != self._last_key:
                self._last_key = key
                self._emit(status)

    def _classify(self, slot: dict) -> dict | None:
        if not slot.get("is_processing"):
            return None
        task_id = slot.get("id_task") or 0
        if self._baseline is not None and task_id <= self._baseline:
            return {"type": "status", "phase": "busy"}
        if _n_decoded(slot) > 0:
            return None  # our generation already streams — deltas own the row
        n_prompt = slot.get("n_prompt_tokens") or 0
        if n_prompt <= 0:
            return None
        done = (slot.get("n_prompt_tokens_cache") or 0) + (
            slot.get("n_prompt_tokens_processed") or 0
        )
        pct = min(100, round(100 * done / n_prompt))
        return {"type": "status", "phase": "reading", "pct": round(pct / 5) * 5}
