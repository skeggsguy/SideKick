"""Timezone resolution, conversion, and the convert_time / current_time tools."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from harness.core.timezone import (
    resolve_zone,
    to_local,
    to_rfc3339,
    to_utc_midnight,
    zone_name,
)
from harness.store.client_config import ClientConfigStore
from harness.tools.timezone import ConvertTimeTool, CurrentTimeTool

LOCAL = ZoneInfo("Asia/Singapore")  # +08:00 year-round, no DST
NY = ZoneInfo("America/New_York")


# --- resolve_zone -----------------------------------------------------------

def test_resolve_zone_prefers_reported():
    assert zone_name(resolve_zone("Asia/Singapore")) == "Asia/Singapore"


def test_resolve_zone_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("HARNESS_TZ", "America/New_York")
    # An invalid reported value is skipped, env is used.
    assert zone_name(resolve_zone("Not/AZone")) == "America/New_York"
    # None reported also uses env.
    assert zone_name(resolve_zone(None)) == "America/New_York"


def test_resolve_zone_system_fallback(monkeypatch):
    monkeypatch.delenv("HARNESS_TZ", raising=False)
    # No reported, no env: still a usable tzinfo, never an error.
    assert resolve_zone(None) is not None


# --- to_local (read path) ---------------------------------------------------

def test_to_local_converts_offset_to_user_zone():
    # 2pm in LA (PDT, -07:00) on 20 Jun is 5am next day in Singapore (+08:00).
    out = to_local("2026-06-20T14:00:00-07:00", LOCAL)
    assert out["all_day"] is False
    assert out["iso"] == "2026-06-21T05:00:00+08:00"
    assert out["weekday"] == "Sunday"
    assert "5:00 AM" in out["time"]


def test_to_local_handles_zulu():
    out = to_local("2026-06-20T00:00:00Z", LOCAL)
    # Midnight UTC = 8am in Singapore.
    assert out["iso"] == "2026-06-20T08:00:00+08:00"


def test_to_local_all_day_is_not_shifted():
    out = to_local("2026-06-20", LOCAL)
    assert out["all_day"] is True
    assert out["date"] == "2026-06-20"
    assert "all day" in out["label"]


def test_to_local_handles_email_date_headers():
    """gmail_search/gmail_read return the raw RFC 2822 Date header — both a
    weekday starting with 'T' (once mistaken for an RFC3339 'T') and one
    without must parse, not come back unparseable."""
    tue = to_local("Tue, 16 Jun 2026 14:00:00 -0700", LOCAL)
    assert tue["iso"] == "2026-06-17T05:00:00+08:00"
    mon = to_local("Mon, 15 Jun 2026 09:30:00 +1000", LOCAL)
    assert mon["iso"] == "2026-06-15T07:30:00+08:00"
    # '-0000' = UTC with no local zone known, per RFC 2822.
    utc = to_local("Mon, 15 Jun 2026 00:00:00 -0000", LOCAL)
    assert utc["iso"] == "2026-06-15T08:00:00+08:00"


def test_to_local_rejects_garbage_with_value_error():
    # ConvertTimeTool renders ValueError as "(unparseable: ...)" per value.
    with pytest.raises(ValueError):
        to_local("next tuesday", LOCAL)


# --- to_rfc3339 (write path, DST-correct) -----------------------------------

def test_to_rfc3339_winter_offset():
    assert to_rfc3339("2026-01-15", "09:00", NY) == "2026-01-15T09:00:00-05:00"


def test_to_rfc3339_summer_offset():
    # Same wall-clock, post-DST date: offset is -04:00, not -05:00. This is the
    # bug fixed-offset arithmetic could not get right for future events.
    assert to_rfc3339("2026-07-15", "09:00", NY) == "2026-07-15T09:00:00-04:00"


def test_to_utc_midnight():
    assert to_utc_midnight("2026-06-19") == "2026-06-19T00:00:00Z"


# --- ConvertTimeTool --------------------------------------------------------

async def test_convert_tool_to_local(monkeypatch):
    monkeypatch.setenv("HARNESS_TZ", "Asia/Singapore")
    tool = ConvertTimeTool(client_config=None)
    out = await tool.run({"op": "to_local", "times": ["2026-06-20T14:00:00-07:00"]})
    assert "Sun 21 Jun 2026" in out
    assert "5:00 AM" in out


async def test_convert_tool_event(monkeypatch):
    monkeypatch.setenv("HARNESS_TZ", "America/New_York")
    tool = ConvertTimeTool(client_config=None)
    out = await tool.run({"op": "event", "date": "2026-07-15", "clock": "09:00"})
    assert "2026-07-15T09:00:00-04:00" in out


async def test_convert_tool_event_zone_override(monkeypatch):
    monkeypatch.setenv("HARNESS_TZ", "America/New_York")
    tool = ConvertTimeTool(client_config=None)
    out = await tool.run(
        {"op": "event", "date": "2026-07-15", "clock": "09:00", "zone": "Asia/Tokyo"}
    )
    assert "+09:00" in out


async def test_convert_tool_task_due(monkeypatch):
    monkeypatch.setenv("HARNESS_TZ", "America/New_York")
    tool = ConvertTimeTool(client_config=None)
    out = await tool.run({"op": "task_due", "date": "2026-06-19"})
    assert "2026-06-19T00:00:00Z" in out


async def test_convert_tool_errors():
    tool = ConvertTimeTool(client_config=None)
    assert "Error" in await tool.run({"op": "to_local", "times": []})
    assert "Error" in await tool.run({"op": "event", "date": "2026-07-15"})
    assert "Error" in await tool.run({"op": "bogus"})


# --- store + tool integration -----------------------------------------------

async def test_client_config_round_trip(db):
    store = ClientConfigStore(db)
    assert await store.get_timezone() is None
    await store.set_timezone("Asia/Singapore")
    assert await store.get_timezone() == "Asia/Singapore"
    # Last write wins.
    await store.set_timezone("America/New_York")
    assert await store.get_timezone() == "America/New_York"


async def test_tools_resolve_zone_from_store(db, monkeypatch):
    monkeypatch.delenv("HARNESS_TZ", raising=False)
    store = ClientConfigStore(db)
    await store.set_timezone("Asia/Singapore")

    current = CurrentTimeTool(client_config=store)
    assert "Asia/Singapore" in await current.run({})

    convert = ConvertTimeTool(client_config=store)
    out = await convert.run({"op": "event", "date": "2026-06-20", "clock": "09:00"})
    # Singapore is +08:00 year-round.
    assert "+08:00" in out
