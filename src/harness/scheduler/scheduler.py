"""Tick-loop cron scheduler with catch-up, built for a laptop that sleeps.

Every tick: for each enabled cron trigger, find its most recent scheduled
time ≤ now; if that time is after the trigger's last firing (or its creation,
for never-fired triggers), fire it. This gives catch-up for free — after the
lid was closed through 07:30, the next tick sees the missed time and fires
once, flagged late=True so the briefing can say "while you were away".
APScheduler was rejected in PLAN-v2: it duplicates our state store and these
sleep/catch-up semantics need to be custom anyway.

Cron expressions are evaluated in LOCAL time ("30 7 * * 1-5" means 07:30 on
Tom's clock), while stored timestamps stay UTC ISO — aware datetimes compare
correctly across zones.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime, timedelta, timezone

from croniter import croniter

from ..core.events import CronFired, EventBus
from ..store.triggers import Trigger, TriggerStore

TICK_SECONDS = 30
# A firing more than this far past its scheduled time is a catch-up.
LATE_THRESHOLD = timedelta(minutes=5)


def _local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class Scheduler:
    def __init__(
        self,
        triggers: TriggerStore,
        bus: EventBus,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.triggers = triggers
        self.bus = bus
        self.tick_seconds = tick_seconds

    async def run(self) -> None:
        """The daemon's scheduler task. Never dies silently: per-trigger
        errors are contained and reported, and the loop itself continues."""
        while True:
            await self.tick()
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, now: datetime | None = None) -> list[str]:
        """One pass over the triggers; returns ids fired (for tests/CLI)."""
        now = now or _local_now()
        fired: list[str] = []
        for trigger in await self.triggers.list(enabled_only=True):
            if trigger.kind != "cron":
                continue  # watch triggers are driven by watchers (P5)
            try:
                if self._due(trigger, now):
                    await self._fire(trigger, now)
                    fired.append(trigger.id)
            except Exception:
                print(
                    f"[scheduler] trigger {trigger.id} ({trigger.spec!r}) failed:\n"
                    f"{traceback.format_exc()}",
                    file=sys.stderr,
                )
        return fired

    def _due(self, trigger: Trigger, now: datetime) -> bool:
        prev_scheduled = croniter(trigger.spec, now).get_prev(datetime)
        baseline = datetime.fromisoformat(
            trigger.last_fired_at or trigger.created_at
        )
        return prev_scheduled > baseline

    async def _fire(self, trigger: Trigger, now: datetime) -> None:
        prev_scheduled = croniter(trigger.spec, now).get_prev(datetime)
        late = (now - prev_scheduled) > LATE_THRESHOLD
        # mark_fired BEFORE publishing: a crash between the two drops one
        # firing rather than double-firing an agent run.
        await self.triggers.mark_fired(trigger.id, now.isoformat())
        self.bus.publish(
            CronFired(
                trigger_id=trigger.id,
                playbook=trigger.playbook,
                channel=trigger.channel,
                autonomy=trigger.autonomy,
                late=late,
                tools=trigger.tools,
                label=trigger.label,
            )
        )
