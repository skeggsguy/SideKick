"""Timezone-aware time tools — the agent's single capability for reading and
writing times correctly.

`current_time` tells the agent what time it is and, crucially, *which zone it is
in* (resolved from the client's last-reported IANA zone). `convert_time` does
the actual conversions in BOTH directions so the model never does offset
arithmetic itself:

  - reading  (op="to_local"): raw Google RFC3339 strings -> local labels, for
    the morning briefing and any calendar read.
  - writing  (op="event"):    a local wall-clock -> the RFC3339 value to hand to
    calendar_event_create / _update (DST-correct via ZoneInfo).
  - writing  (op="task_due"): a local date -> the UTC-midnight value Google
    Tasks expects.

Both tools resolve the zone the same way (client-reported -> HARNESS_TZ ->
system), so reads and writes always agree. The zone lives in the daemon DB
(client_config); these tools run in-process and read it directly, so the Google
MCP server needs no timezone knowledge at all.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..core.policy import ActionClass
from ..core.timezone import (
    Tz,
    now_local,
    resolve_zone,
    to_local,
    to_rfc3339,
    to_utc_midnight,
    zone_name,
)
from ..store.client_config import ClientConfigStore
from .base import Tool


async def _resolve(client_config: ClientConfigStore | None) -> Tz:
    reported = await client_config.get_timezone() if client_config else None
    return resolve_zone(reported)


class CurrentTimeTool(Tool):
    name = "current_time"
    description = (
        "Get the current local date, time, weekday and the user's timezone. "
        "Call before any relative-date or cron work; for converting times use "
        "convert_time."
    )
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    classification = ActionClass.READ_ONLY

    def __init__(self, client_config: ClientConfigStore | None = None) -> None:
        self.client_config = client_config

    async def run(self, args: dict[str, Any]) -> str:
        zone = await _resolve(self.client_config)
        now = now_local(zone)
        offset = now.strftime("%z")
        day_labels = [
            (now + timedelta(days=d)).strftime("%A %Y-%m-%d") for d in range(1, 6)
        ]
        return (
            f"Current local time: {now.strftime('%A %Y-%m-%d %H:%M:%S')} "
            f"{zone_name(zone)} (UTC{offset})\n"
            f"Upcoming days (local): {', '.join(day_labels)}\n"
            f"Building calendar/task times? Use convert_time (op='event' or "
            f"'task_due'). Reading event times? Use convert_time (op='to_local')."
        )


class ConvertTimeTool(Tool):
    name = "convert_time"
    description = (
        "Convert times so you never reason about offsets. Ops:\n"
        "- to_local: `times` = raw timestamps exactly as tools return them "
        "(calendar RFC3339, all-day dates, Drive modifiedTime, Gmail Date "
        "headers) → each in the user's local zone. Use on every event before "
        "stating a time or writing a briefing. Not for Google Tasks `due` "
        "(date-only; read as-is).\n"
        "- event: local `date` ('YYYY-MM-DD') + `clock` ('HH:MM', 24h), "
        "optional IANA `zone` for another city → the RFC3339 string for "
        "calendar_event_create/_update.\n"
        "- task_due: `date` → the UTC-midnight string Google Tasks expects."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["to_local", "event", "task_due"]},
            "times": {"type": "array", "items": {"type": "string"}},
            "date": {"type": "string"},
            "clock": {"type": "string"},
            "zone": {"type": "string"},
        },
        "required": ["op"],
    }
    classification = ActionClass.READ_ONLY

    def __init__(self, client_config: ClientConfigStore | None = None) -> None:
        self.client_config = client_config

    async def run(self, args: dict[str, Any]) -> str:
        op = args.get("op")
        zone = await _resolve(self.client_config)

        if op == "to_local":
            times = args.get("times") or []
            if not times:
                return "Error: op='to_local' needs a non-empty `times` list."
            lines = []
            for value in times:
                try:
                    rendered = to_local(value, zone)
                except ValueError as exc:
                    lines.append(f"{value} -> (unparseable: {exc})")
                    continue
                lines.append(f"{value} -> {rendered['label']}")
            return "\n".join(lines)

        if op == "event":
            date, clock = args.get("date"), args.get("clock")
            if not date or not clock:
                return "Error: op='event' needs `date` and `clock`."
            ev_zone = resolve_zone(args["zone"]) if args.get("zone") else zone
            try:
                value = to_rfc3339(date, clock, ev_zone)
            except ValueError as exc:
                return f"Error: could not build a datetime from {date!r} {clock!r}: {exc}"
            return (
                f"{date} {clock} ({zone_name(ev_zone)}) -> {value}\n"
                f"Pass this as start/end to calendar_event_create."
            )

        if op == "task_due":
            date = args.get("date")
            if not date:
                return "Error: op='task_due' needs `date`."
            try:
                value = to_utc_midnight(date)
            except ValueError as exc:
                return f"Error: could not parse date {date!r}: {exc}"
            return f"{date} -> {value} (use as the task due date)."

        return f"Error: unknown op {op!r}. Use 'to_local', 'event', or 'task_due'."


def timezone_tools(client_config: ClientConfigStore | None = None) -> list[Tool]:
    return [CurrentTimeTool(client_config), ConvertTimeTool(client_config)]
