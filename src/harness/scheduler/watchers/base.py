"""Watcher framework: poll an external source, publish WatchHit events.

A Watcher's poll() returns payload dicts for events worth raising (the
watcher itself filters noise — e.g. Gmail triage drops newsletters before
anything reaches the bus). WatcherRunner is the daemon task: poll forever,
contain and report per-poll errors, never die silently.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from abc import ABC, abstractmethod

from ...core.events import EventBus, WatchHit


class Watcher(ABC):
    name: str
    interval_seconds: float = 120

    @abstractmethod
    async def poll(self) -> list[dict]:
        """One poll; returns a payload dict per event worth raising."""


class WatcherRunner:
    def __init__(self, watcher: Watcher, bus: EventBus) -> None:
        self.watcher = watcher
        self.bus = bus

    async def run(self) -> None:
        while True:
            try:
                for payload in await self.watcher.poll():
                    self.bus.publish(WatchHit(watcher=self.watcher.name, payload=payload))
            except Exception:
                print(
                    f"[watcher:{self.watcher.name}] poll failed:\n{traceback.format_exc()}",
                    file=sys.stderr,
                )
            await asyncio.sleep(self.watcher.interval_seconds)
