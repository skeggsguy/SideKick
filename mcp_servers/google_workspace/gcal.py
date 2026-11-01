"""Google Calendar read + write tools.

Local writes (calendar_event_create/update) target the user's OWN calendars
(default primary) with sendUpdates="none" and no attendees param — no outward effect,
so they are MUTATING_LOCAL. Guest management (calendar_event_attendees) and
RSVPs (calendar_event_respond) notify other people — OUTWARD, always
human-approved, like gmail_send. calendar_event_delete is DESTRUCTIVE:
Calendar has no trash API; events().delete moves the event to the calendar's
Bin (~30 days, web-UI restore only). Mirrors gmail's draft/send/trash split.
"""

from __future__ import annotations

import base64
import hashlib

from .auth import get_service


def calendar_list_events(
    time_min: str,
    time_max: str,
    max_results: int = 25,
    calendar_id: str = "primary",
    query: str = "",
) -> list[dict]:
    """List calendar events between time_min and time_max (RFC3339). Build
    the bounds with convert_time (op='event') from local dates — e.g. '00:00'
    on the day to '00:00' the next day — never a hand-written 'Z' time.
    Returned start/end are raw (the event's own offset): pass them through
    convert_time (op='to_local') before quoting any time. Recurring events
    are expanded into instances,
    ordered by start time. query free-text matches over title, description,
    location, and attendees (e.g. 'dentist') — use it to find a specific
    event without scanning the whole window."""
    svc = get_service("calendar", "v3")
    list_kwargs: dict = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": min(max_results, 100),
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if query:
        list_kwargs["q"] = query
    resp = svc.events().list(**list_kwargs).execute()
    return [_event_summary(e) for e in resp.get("items", [])]


def calendar_list_calendars() -> list[dict]:
    """List the calendars on your account. Use the returned id as calendar_id
    in the other calendar tools. Calendars whose access_role is 'reader' or
    'freeBusyReader' cannot be written to."""
    svc = get_service("calendar", "v3")
    resp = svc.calendarList().list(maxResults=100).execute()
    return [
        {
            "id": c.get("id", ""),
            "summary": c.get("summary", ""),
            "primary": bool(c.get("primary")),
            "access_role": c.get("accessRole", ""),
        }
        for c in resp.get("items", [])
    ]


def calendar_get_event(event_id: str, calendar_id: str = "primary") -> dict:
    """Get one calendar event in full by id. start/end are raw RFC3339 —
    localize them with convert_time (op='to_local') before quoting."""
    svc = get_service("calendar", "v3")
    e = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    full = _event_summary(e)
    full["description"] = e.get("description", "")
    full["attendees"] = [
        {"email": a.get("email", ""), "response": a.get("responseStatus", "")}
        for a in e.get("attendees", [])
    ]
    full["organizer"] = e.get("organizer", {}).get("email", "")
    full["is_own_event"] = bool(e.get("organizer", {}).get("self"))
    return full


def _event_id(summary: str, start: str, end: str) -> str:
    """Deterministic event id from the event's content, in Google's base32hex
    id alphabet (a-v, 0-9). A crash-retry that replays identical args produces
    the same id, so Google returns 409 instead of creating a duplicate. The one
    tradeoff: two *intentionally* identical events dedupe — acceptable and rare.
    Event ids are namespaced per calendar, so the same content on a different
    calendar_id inserts fine — no need to mix calendar_id into the hash."""
    raw = f"{summary}\x00{start}\x00{end}".encode("utf-8")
    digest = hashlib.sha1(raw).digest()
    enc = base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()
    return "h" + enc  # leading letter keeps it a valid id; ~33 chars


def _meet_request_id(event_id: str, updated: str) -> str:
    """Deterministic Meet createRequest id: stable across a crash-retry (same
    event, same 'updated' stamp) but fresh after any change — reusing a
    requestId after a remove→re-add would replay the old, deleted conference."""
    raw = f"{event_id}\x00{updated}".encode("utf-8")
    return base64.b32hexencode(hashlib.sha1(raw).digest()).decode("ascii").rstrip("=").lower()


def calendar_event_create(
    summary: str,
    start: str,
    end: str,
    location: str = "",
    description: str = "",
    with_meet: bool = False,
    calendar_id: str = "primary",
) -> dict:
    """Create an event on one of your calendars (default primary). start/end
    are RFC3339 datetimes — build them with convert_time (op='event') from the
    local date/time; do not write the UTC offset yourself. Does NOT invite
    anyone or send notifications. Set with_meet=True to attach a Google Meet
    link — do this when the event will have guests and isn't clearly in-person
    (no physical location, or the user asked for a call); leave it off for
    in-person events. The returned meet_link may be briefly empty while Google
    attaches it (near-instant; calendar_get_event shows it — don't poll).
    Retrying with identical summary/start/end is safe — it will not create a
    duplicate."""
    svc = get_service("calendar", "v3")
    body: dict = {
        "id": _event_id(summary, start, end),
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    insert_kwargs: dict = {"calendarId": calendar_id, "body": body, "sendUpdates": "none"}
    if with_meet:
        # conferenceDataVersion=1 is mandatory — without it Google silently
        # ignores conferenceData (HTTP 200, no link).
        body["conferenceData"] = {
            "createRequest": {
                "requestId": body["id"],
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        insert_kwargs["conferenceDataVersion"] = 1
    e = svc.events().insert(**insert_kwargs).execute()
    return _event_summary(e)


def calendar_event_update(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
    meet: str = "",
    calendar_id: str = "primary",
) -> dict:
    """Update fields of an existing event on one of your calendars (default
    primary) by id. Only the fields you pass are changed. start/end are
    RFC3339 datetimes — build them with convert_time (op='event'), not by
    writing the offset yourself. meet='add' attaches a Google Meet link,
    meet='remove' strips it — add one when the event has (or is about to get)
    guests and isn't clearly in-person. Does NOT notify anyone."""
    if meet not in ("", "add", "remove"):
        raise RuntimeError(f"invalid meet {meet!r} — use 'add' or 'remove', or omit it")
    svc = get_service("calendar", "v3")
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = {"dateTime": start}
    if end is not None:
        body["end"] = {"dateTime": end}
    if location is not None:
        body["location"] = location
    if description is not None:
        body["description"] = description
    patch_kwargs: dict = {
        "calendarId": calendar_id,
        "eventId": event_id,
        "body": body,
        "sendUpdates": "none",
    }
    note = ""
    # conferenceDataVersion=1 is mandatory on any patch touching conferenceData
    # — without it Google silently ignores the field (HTTP 200, no change).
    if meet == "add":
        e = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        if e.get("hangoutLink"):
            note = "event already has a Meet link"
            if not body:
                out = _event_summary(e)
                out["note"] = note
                return out
        else:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": _meet_request_id(event_id, e.get("updated", "")),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            patch_kwargs["conferenceDataVersion"] = 1
    elif meet == "remove":
        # JSON null (not key-omission) is how a patch strips the conference.
        body["conferenceData"] = None
        patch_kwargs["conferenceDataVersion"] = 1
    e = svc.events().patch(**patch_kwargs).execute()
    out = _event_summary(e)
    if note:
        out["note"] = note
    return out


def _clean_emails(emails: list[str] | None, arg_name: str) -> list[str]:
    out = []
    for raw in emails or []:
        email = raw.strip()
        if not email:
            continue
        if "@" not in email:
            raise RuntimeError(f"not an email address in {arg_name}: {raw!r}")
        out.append(email)
    return out


def calendar_event_attendees(
    event_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Add and/or remove guests (by email) on an event YOU organize (default
    primary calendar). Sends invitation emails to added guests and cancellation
    notices to removed ones — this notifies people. Fails on events organized
    by someone else (to RSVP to an invitation use calendar_event_respond).
    Existing guests' RSVP statuses are preserved; adding someone already on
    the event or removing someone not on it is skipped and reported. If the
    event is a call (no physical location) and has no Meet link yet, add one
    first with calendar_event_update(meet='add') so invitees get it."""
    adds = _clean_emails(add, "add")
    removes = _clean_emails(remove, "remove")
    if not adds and not removes:
        raise RuntimeError("nothing to do — pass at least one email in add or remove")

    svc = get_service("calendar", "v3")
    e = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    organizer = e.get("organizer", {})
    if not organizer.get("self"):
        raise RuntimeError(
            "you can only manage guests on events you organize — this event's "
            f"organizer is {organizer.get('email', 'unknown')}; to respond to "
            "an invitation use calendar_event_respond"
        )
    remove_set = {r.lower() for r in removes}
    if organizer.get("email", "").lower() in remove_set:
        raise RuntimeError("cannot remove the organizer (you) from the event")

    kept, removed = [], []
    for a in e.get("attendees", []):
        if a.get("email", "").lower() in remove_set:
            removed.append(a.get("email", ""))
        else:
            kept.append(a)
    present = {a.get("email", "").lower() for a in kept}
    added, skipped_already = [], []
    for email in adds:
        if email.lower() in present:
            skipped_already.append(email)
            continue
        kept.append({"email": email})
        present.add(email.lower())
        added.append(email)
    removed_lower = {r.lower() for r in removed}
    skipped_absent = [r for r in removes if r.lower() not in removed_lower]

    if not added and not removed:
        raise RuntimeError(
            "no change — everyone in add is already a guest and no one in "
            "remove is on the event"
        )
    updated = (
        svc.events()
        .patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"attendees": kept},
            sendUpdates="all",
        )
        .execute()
    )
    out = _event_summary(updated)
    out["added"] = added
    out["removed"] = removed
    out["skipped_already_attending"] = skipped_already
    out["skipped_not_attending"] = skipped_absent
    out["attendees"] = [a.get("email", "") for a in kept]
    return out


def calendar_event_respond(event_id: str, response: str) -> dict:
    """RSVP to a calendar invitation you received. Acts on your primary
    calendar, where invitations arrive. response is 'accepted', 'declined',
    or 'tentative'. The organizer is notified of your response. Fails on
    events you organize yourself — use calendar_event_attendees to manage
    guests there."""
    if response not in ("accepted", "declined", "tentative"):
        raise RuntimeError(
            f"invalid response {response!r} — use accepted, declined, or tentative"
        )
    svc = get_service("calendar", "v3")
    e = svc.events().get(calendarId="primary", eventId=event_id).execute()
    attendees = e.get("attendees", [])
    mine = next((a for a in attendees if a.get("self")), None)
    if mine is None:
        if e.get("organizer", {}).get("self"):
            raise RuntimeError(
                "this is your own event — there is no invitation to respond "
                "to; use calendar_event_attendees to manage guests"
            )
        raise RuntimeError("you are not on this event's guest list — nothing to RSVP to")
    mine["responseStatus"] = response
    updated = (
        svc.events()
        .patch(
            calendarId="primary",
            eventId=event_id,
            body={"attendees": attendees},
            sendUpdates="all",
        )
        .execute()
    )
    out = _event_summary(updated)
    out["response"] = response
    out["organizer"] = e.get("organizer", {}).get("email", "")
    return out


def calendar_event_delete(
    event_id: str, notify: bool = True, calendar_id: str = "primary"
) -> dict:
    """Delete an event from one of your calendars (default primary). If you
    organize it and it has guests, notify=True (default) sends them
    cancellation emails; notify=False deletes silently (right for junk/spam
    invites, where pinging the sender is unwanted). The event moves to Google
    Calendar's Bin and is recoverable there in the web UI for about 30 days
    (no API restore)."""
    svc = get_service("calendar", "v3")
    svc.events().delete(
        calendarId=calendar_id,
        eventId=event_id,
        sendUpdates="all" if notify else "none",
    ).execute()
    return {
        "event_id": event_id,
        "deleted": True,
        "note": "recoverable from Google Calendar's Bin (web UI) for ~30 days",
    }


def _event_summary(e: dict) -> dict:
    return {
        "id": e.get("id", ""),
        "summary": e.get("summary", "(no title)"),
        "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""),
        "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", ""),
        "location": e.get("location", ""),
        "status": e.get("status", ""),
        "meet_link": e.get("hangoutLink", ""),
    }
