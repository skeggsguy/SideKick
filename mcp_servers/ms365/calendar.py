"""Read-only Outlook calendar tools (Microsoft Graph)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import graph


def outlook_calendar_list_events(
    start: str = "", end: str = "", max_results: int = 20
) -> list[dict]:
    """List calendar events between two ISO datetimes (defaults: now to
    7 days ahead). Times are returned in the calendar's own timezone."""
    now = datetime.now(timezone.utc)
    start_dt = start or now.isoformat()
    end_dt = end or (now + timedelta(days=7)).isoformat()
    data = graph.get(
        "/me/calendarView",
        params={
            "startDateTime": start_dt,
            "endDateTime": end_dt,
            "$top": min(max_results, 50),
            "$orderby": "start/dateTime",
            "$select": "id,subject,start,end,location,isAllDay,organizer",
        },
    )
    return [
        {
            "id": e["id"],
            "subject": e.get("subject", ""),
            "start": (e.get("start") or {}).get("dateTime", ""),
            "end": (e.get("end") or {}).get("dateTime", ""),
            "all_day": e.get("isAllDay", False),
            "location": (e.get("location") or {}).get("displayName", ""),
            "organizer": (e.get("organizer") or {})
            .get("emailAddress", {})
            .get("address", ""),
        }
        for e in data.get("value", [])
    ]
