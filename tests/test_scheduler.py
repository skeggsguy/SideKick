"""Scheduler: due/fire logic and catch-up, with explicit clocks."""

from __future__ import annotations

from datetime import datetime, timezone

from harness.core.events import CronFired, EventBus
from harness.scheduler import Scheduler
from harness.store.triggers import TriggerStore

# All test times are aware UTC; the scheduler only compares aware datetimes.
T = lambda h, m, day=15: datetime(2026, 6, day, h, m, tzinfo=timezone.utc)  # noqa: E731


async def make(db, spec="30 7 * * *", **kwargs):
    store = TriggerStore(db)
    bus = EventBus()
    scheduler = Scheduler(store, bus)
    tid = await store.add("cron", spec, "morning_briefing", **kwargs)
    # Pin creation to a known instant so tests control the baseline.
    await db.conn.execute(
        "UPDATE triggers SET created_at = ? WHERE id = ?",
        (T(6, 0).isoformat(), tid),
    )
    await db.conn.commit()
    return store, bus, scheduler, tid


async def test_not_due_before_scheduled_time(db):
    _, bus, scheduler, _ = await make(db)  # created 06:00, cron at 07:30
    assert await scheduler.tick(now=T(7, 0)) == []


async def test_fires_once_at_scheduled_time(db):
    store, bus, scheduler, tid = await make(db)
    assert await scheduler.tick(now=T(7, 31)) == [tid]

    event = await bus.get()
    assert isinstance(event, CronFired)
    assert event.playbook == "morning_briefing"
    assert event.late is False
    assert event.tools is None  # no allocation on this row


async def test_fired_event_carries_row_autonomy_and_tools(db):
    store, bus, scheduler, tid = await make(
        db, autonomy="autonomous", tools=["calendar_event_create"]
    )
    assert await scheduler.tick(now=T(7, 31)) == [tid]
    event = await bus.get()
    assert event.autonomy == "autonomous"
    assert event.tools == ["calendar_event_create"]

    # Same tick window again: already fired, must not refire.
    assert await scheduler.tick(now=T(7, 32)) == []
    # Next day it fires again.
    assert await scheduler.tick(now=T(7, 31, day=16)) == [tid]


async def test_catch_up_after_sleep_is_late_and_single(db):
    store, bus, scheduler, tid = await make(db)
    # Laptop slept from 06:00 through several scheduled firings (multi-day);
    # the next tick catches up exactly once, flagged late.
    assert await scheduler.tick(now=T(11, 45, day=17)) == [tid]
    event = await bus.get()
    assert event.late is True
    assert bus._queue.empty()  # one catch-up max, not one per missed day


async def test_one_shot_reminder_fires_then_disables(db):
    store, bus, scheduler, tid = await make(
        db, spec="0 16 * * 5", one_shot=True
    )
    # 2026-06-19 is a Friday; 16:00 passes.
    assert await scheduler.tick(now=T(16, 1, day=19)) == [tid]
    assert (await store.get(tid)).enabled is False
    assert await scheduler.tick(now=T(16, 1, day=26)) == []


async def test_bad_spec_contained_and_others_still_fire(db, capsys):
    store, bus, scheduler, _ = await make(db, spec="not a cron")
    good = await store.add("cron", "30 7 * * *", "morning_briefing")
    await db.conn.execute(
        "UPDATE triggers SET created_at = ? WHERE id = ?",
        (T(6, 0).isoformat(), good),
    )
    await db.conn.commit()

    fired = await scheduler.tick(now=T(8, 0))
    assert fired == [good]
    assert "failed" in capsys.readouterr().err


async def test_watch_triggers_ignored_by_cron_tick(db):
    store = TriggerStore(db)
    scheduler = Scheduler(store, EventBus())
    await store.add("watch", "gmail", "email_triage")
    assert await scheduler.tick(now=T(12, 0)) == []
