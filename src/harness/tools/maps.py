"""maps_search + maps_route: Google Maps Platform (Places API (New) + Routes API).

Registered only when GOOGLE_MAPS_API_KEY is set (composition.build_agent), so an
unconfigured harness simply has no maps tools rather than broken ones. Results
are external content — the ingestion rule applies, so both tools' output is
prefixed with an untrusted-data note and must never be treated as instructions.

No timezone conversion here — convert_time owns it. The one zone touch is
picking which weekday's hours line to show, using the place's own
utcOffsetMinutes (index selection only; the displayed strings are Google's,
untouched). The key travels in a header, never in the URL or tool args, so it
can't reach the audit trace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..core.policy import ActionClass
from ._maps_format import (  # re-exported for callers/tests
    _MIN_WALK_SECONDS,
    _PLACE_PREFIX,
    _RFC3339_HINT,
    UNTRUSTED_NOTE_PLACES,
    UNTRUSTED_NOTE_ROUTES,
    _fmt_duration,
    _fmt_km,
    _format_places,
    _format_route,
    _route_line,
    _seconds,
    _stop_time,
    _todays_hours,
    _transit_legs,
    _transit_step,
    _walk_line,
)
from .base import Tool

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"



# Full field paths, joined once; sent verbatim as X-Goog-FieldMask (the header
# is mandatory on Places New — a request without it 400s, and so does a
# malformed path, so the request-shape test asserts the exact mask).
_SEARCH_FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.currentOpeningHours",
        "places.regularOpeningHours.weekdayDescriptions",
        "places.utcOffsetMinutes",
    )
)
_ROUTES_FIELD_MASK = ",".join(
    (
        "routes.duration",
        "routes.staticDuration",
        "routes.distanceMeters",
        "routes.description",
        "routes.localizedValues",
        "routes.legs.steps.travelMode",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.distanceMeters",
        "routes.legs.steps.transitDetails",
    )
)


def _client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=2.0),
        transport=transport,
        # env proxies must not reroute keyed API traffic — same invariant as
        # web_search/web_fetch (see web_fetch's SSRF notes; pinned in tests)
        trust_env=False,
    )


async def _call(
    tool: str,
    url: str,
    body: dict,
    api_key: str,
    field_mask: str,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[dict | None, str]:
    """POST to a Maps endpoint. Returns (json, "") or (None, error string)."""
    try:
        async with _client(transport) as client:
            resp = await client.post(
                url,
                json=body,
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": field_mask,
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        return None, f"{tool} error: Google did not respond within 10s — try again."
    except httpx.HTTPError as e:
        return None, f"{tool} error: could not reach Google ({type(e).__name__})."

    if resp.status_code == 403:
        return None, (
            f"{tool} error: Google rejected the request (403) — check "
            "GOOGLE_MAPS_API_KEY in .env and that the Places API (New) and "
            "Routes API are enabled on the project."
        )
    if resp.status_code == 429:
        return None, f"{tool} error: quota hit (429) — wait a moment and retry."
    if resp.status_code != 200:
        detail = _error_detail(resp)
        suffix = f": {detail}" if detail else ""
        return None, f"{tool} error: Google returned HTTP {resp.status_code}{suffix}"
    return resp.json(), ""


def _error_detail(resp: httpx.Response) -> str:
    """Google error bodies carry a useful error.message — surface it truncated."""
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:200]
    except ValueError:
        return ""



def _waypoint(value: str) -> dict:
    """Contract, not sniffing: 'places/<id>' → {placeId}; anything else —
    including a bare ChIJ… id — is free text. Tolerates our own emitted
    'place_id: ' label so the maps_search line pastes in verbatim."""
    value = value.removeprefix("place_id:").strip()
    if value.startswith(_PLACE_PREFIX):
        return {"placeId": value[len(_PLACE_PREFIX) :]}
    return {"address": value}



class MapsSearchTool(Tool):
    name = "maps_search"
    description = (
        "Find real-world places (shops, restaurants, addresses, points of "
        "interest) via Google Maps, with rating, open/closed status, and "
        "today's opening hours. Include the locality in the query text "
        "('pharmacy in Springfield', not 'pharmacy'). Each result carries a "
        "place_id: places/… line — pass that places/… value to maps_route "
        "as origin or destination."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to find, locality included ('coffee near Borough Market').",
            },
            "count": {
                "type": "integer",
                "description": "Number of results, 1-8 (default 4).",
            },
            "open_now": {
                "type": "boolean",
                "description": "Only places open right now.",
            },
        },
        "required": ["query"],
    }
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(
        self, api_key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    async def run(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "maps_search error: 'query' is required."
        raw_count = args.get("count")
        count = min(max(int(4 if raw_count is None else raw_count), 1), 8)
        body: dict[str, Any] = {"textQuery": query, "pageSize": count}
        if isinstance(args.get("open_now"), bool):
            body["openNow"] = args["open_now"]

        data, error = await _call(
            "maps_search", PLACES_URL, body, self._api_key,
            _SEARCH_FIELD_MASK, self._transport,
        )
        if data is None:
            return error
        return _format_places(
            data.get("places", []), query, datetime.now(timezone.utc)
        )


class MapsRouteTool(Tool):
    name = "maps_route"
    description = (
        "Travel time and distance between two locations via Google Maps — "
        "driving (with current traffic), walking, cycling, or transit. "
        "origin/destination take a free-text address/place name or the "
        "places/… value from a maps_search place_id: line. depart_at takes "
        "RFC3339 from convert_time op='event' (omit to leave now); arrive_at "
        "is transit-only and cannot be combined with depart_at."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "origin": {
                "type": "string",
                "description": "Start: free-text address/place name, or a places/… id from maps_search.",
            },
            "destination": {
                "type": "string",
                "description": "End: free-text address/place name, or a places/… id from maps_search.",
            },
            "mode": {
                "type": "string",
                "enum": ["drive", "walk", "bicycle", "transit"],
                "description": "Travel mode (default drive).",
            },
            "depart_at": {
                "type": "string",
                "description": "Departure time, RFC3339 (from convert_time op='event'). Omit for now.",
            },
            "arrive_at": {
                "type": "string",
                "description": "Arrival time, RFC3339 — transit only, exclusive with depart_at.",
            },
            "alternatives": {
                "type": "boolean",
                "description": "Also list alternative routes (default false).",
            },
        },
        "required": ["origin", "destination"],
    }
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(
        self, api_key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport

    async def run(self, args: dict[str, Any]) -> str:
        origin = str(args.get("origin") or "").strip()
        destination = str(args.get("destination") or "").strip()
        if not origin or not destination:
            return "maps_route error: 'origin' and 'destination' are required."
        mode = str(args.get("mode") or "drive").lower()
        travel_mode = {
            "drive": "DRIVE", "walk": "WALK",
            "bicycle": "BICYCLE", "transit": "TRANSIT",
        }.get(mode)
        if travel_mode is None:
            return f"maps_route error: mode must be drive|walk|bicycle|transit, got {mode!r}."
        depart_at = str(args.get("depart_at") or "").strip()
        arrive_at = str(args.get("arrive_at") or "").strip()
        # Routes rejects both-together and arrivalTime outside transit —
        # validated here, in plain language, before any HTTP call.
        if depart_at and arrive_at:
            return (
                "maps_route error: depart_at and arrive_at are mutually "
                "exclusive — give one or the other (or neither)."
            )
        if arrive_at and mode != "transit":
            return "maps_route error: arrive_at is only valid for transit mode."

        body: dict[str, Any] = {
            "origin": _waypoint(origin),
            "destination": _waypoint(destination),
            "travelMode": travel_mode,
            "computeAlternativeRoutes": bool(args.get("alternatives")),
        }
        if mode == "drive":
            # Traffic-aware duration is the point of the drive mode; the field
            # is invalid for other modes, so it is omitted there.
            body["routingPreference"] = "TRAFFIC_AWARE"
        if depart_at:
            body["departureTime"] = depart_at
        if arrive_at:
            body["arrivalTime"] = arrive_at

        data, error = await _call(
            "maps_route", ROUTES_URL, body, self._api_key,
            _ROUTES_FIELD_MASK, self._transport,
        )
        if data is None:
            return error
        return _format_route(data.get("routes", []), mode, origin, destination)


def maps_tools(
    api_key: str, transport: httpx.AsyncBaseTransport | None = None
) -> list[Tool]:
    return [MapsSearchTool(api_key, transport), MapsRouteTool(api_key, transport)]
