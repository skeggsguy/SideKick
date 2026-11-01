"""Timezone resolution and conversion — the single source of truth for "what
time is it where the user is", used by both the read path (rendering calendar
events in the morning briefing) and the write path (building RFC3339 values for
calendar/task creation).

Deliberately pure: no DB, no async, no I/O beyond reading the HARNESS_TZ env and
the system clock. The caller resolves the user's reported zone (from the daemon's
client_config store) and passes the name in; everything here is testable offline
with a fixed zone.

DST correctness: conversions go through ZoneInfo, so "9am on a date after a DST
change" gets that date's offset, not today's. This is the bug the old hand-rolled
offset arithmetic in current_time could not fix (it subtracted today's offset for
a future event).
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone, tzinfo
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A resolved zone is either a ZoneInfo (DST-aware, the normal path) or a
# fixed-offset timezone (the last-resort system fallback).
Tz = tzinfo


def resolve_zone(reported: str | None) -> Tz:
    """Resolve the user's local zone, in priority order:

    1. `reported` — the IANA name the client (iPad) sent, when valid.
    2. HARNESS_TZ env — a manual pin/override.
    3. The system clock's current zone (fixed-offset fallback; loses future-DST
       awareness, so the reported zone is strongly preferred).
    """
    for cand in (reported, os.environ.get("HARNESS_TZ")):
        if cand:
            try:
                return ZoneInfo(cand)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    # Fallback: whatever offset the host is at now. A fixed-offset timezone, so
    # future DST transitions are not modelled — acceptable only as last resort.
    return datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc


def zone_name(zone: Tz) -> str:
    """A human label for a resolved zone — the IANA key when we have it
    (ZoneInfo), else the abbreviation/offset name."""
    key = getattr(zone, "key", None)
    if key:
        return key
    return datetime.now(zone).tzname() or str(zone)


def now_local(zone: Tz) -> datetime:
    return datetime.now(timezone.utc).astimezone(zone)


def _parse_datetime(value: str, zone: Tz) -> datetime:
    """RFC3339/ISO (calendar, Drive) or an RFC 2822 email Date header
    ('Tue, 20 Jun 2026 14:00:00 -0700', as gmail_search/gmail_read return)."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"not an RFC3339 or email Date value: {value!r}"
            ) from None
        # RFC 2822's '-0000' means UTC with no local zone known.
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        # Google always sends an offset; a naive value would be ambiguous, so
        # treat it as already being in the target zone (wall-clock).
        dt = dt.replace(tzinfo=zone)
    return dt


def to_local(value: str, zone: Tz) -> dict:
    """Render one timestamp (as returned raw by Google) in the user's local
    zone. `value` is an RFC3339 datetime ('2026-06-20T14:00:00-07:00' or a 'Z'
    form), an email Date header, or a date-only all-day marker ('2026-06-20').

    Returns a dict the model can quote directly without doing any arithmetic:
      - all-day:  {"all_day": True, "date", "label"}
      - timed:    {"all_day": False, "iso", "label", "weekday", "time", "zone"}
    """
    try:
        d = date.fromisoformat(value)
    except ValueError:
        d = None
    if d is not None:
        # All-day event: a date label, no time and no conversion.
        return {
            "all_day": True,
            "date": d.isoformat(),
            "label": d.strftime("%a %d %b %Y") + " (all day)",
        }

    dt = _parse_datetime(value, zone)
    local = dt.astimezone(zone)
    return {
        "all_day": False,
        "iso": local.isoformat(),
        "label": local.strftime("%a %d %b %Y, %-I:%M %p %Z"),
        "weekday": local.strftime("%A"),
        "time": local.strftime("%-I:%M %p"),
        "zone": zone_name(zone),
    }


def to_rfc3339(date: str, clock: str, zone: Tz) -> str:
    """Build an RFC3339 datetime from a local wall-clock for a calendar write.
    `date` is 'YYYY-MM-DD', `clock` is 'HH:MM' (24h). The offset is computed for
    THAT date via ZoneInfo, so it is DST-correct for future events.
    """
    d = datetime.fromisoformat(date).date()
    parts = clock.split(":")
    t = time(int(parts[0]), int(parts[1]))
    aware = datetime.combine(d, t).replace(tzinfo=zone)
    return aware.isoformat()


def to_utc_midnight(date: str) -> str:
    """Google Tasks store a due *date* as a UTC-midnight timestamp (the time is
    never shown). 'YYYY-MM-DD' -> 'YYYY-MM-DDT00:00:00Z'."""
    d = datetime.fromisoformat(date).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
