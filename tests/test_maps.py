"""Google Maps tools — all offline, keyless, httpx.MockTransport (like
test_web_search.py). Pins the request shape (exact field mask, place-id
contract, mode-dependent fields), the formatting, and the error paths."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from harness.tools.maps import (
    UNTRUSTED_NOTE_PLACES,
    UNTRUSTED_NOTE_ROUTES,
    MapsRouteTool,
    MapsSearchTool,
    _client,
    _fmt_duration,
    _fmt_km,
    _format_places,
    _format_route,
    _transit_legs,
    _waypoint,
)


def search_tool(handler) -> MapsSearchTool:
    return MapsSearchTool("test_key", transport=httpx.MockTransport(handler))


def route_tool(handler) -> MapsRouteTool:
    return MapsRouteTool("test_key", transport=httpx.MockTransport(handler))


def capture(response_json: dict) -> tuple[dict, callable]:
    """A handler that records the last request and returns canned JSON."""
    seen: dict = {"request": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json=response_json)

    return seen, handler


# ---- maps_search formatting --------------------------------------------------

MONMOUTH = {
    "id": "ChIJd8kx3t4EdkgRxxxxxxxx",
    "displayName": {"text": "Monmouth Coffee Company"},
    "formattedAddress": "27 Monmouth St, London WC2H 9EU",
    "rating": 4.6,
    "userRatingCount": 2341,
    "businessStatus": "OPERATIONAL",
    "currentOpeningHours": {"openNow": True},
    "regularOpeningHours": {
        "weekdayDescriptions": [
            "Monday: 8:00 AM – 6:30 PM",
            "Tuesday: 8:00 AM – 6:30 PM",
            "Wednesday: 8:00 AM – 6:30 PM",
            "Thursday: 8:00 AM – 6:30 PM",
            "Friday: 8:00 AM – 6:30 PM",
            "Saturday: 9:00 AM – 6:00 PM",
            "Sunday: Closed",
        ]
    },
    "utcOffsetMinutes": 0,
}


def test_search_formats_results():
    now_utc = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # a Monday
    result = _format_places([MONMOUTH], "coffee in London", now_utc)

    assert result.startswith(UNTRUSTED_NOTE_PLACES)
    assert "1. Monmouth Coffee Company — 27 Monmouth St, London WC2H 9EU" in result
    assert "rating 4.6 (2,341)" in result
    assert "open now" in result
    assert "Monday: 8:00 AM – 6:30 PM" in result
    assert "place_id: places/ChIJd8kx3t4EdkgRxxxxxxxx" in result


def test_search_weekday_from_place_offset():
    """The hours line is picked in the PLACE's local day, not the user's or
    UTC — and a place with no utcOffsetMinutes omits the line entirely."""
    place = dict(MONMOUTH, utcOffsetMinutes=840)  # UTC+14
    now_utc = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # Mon 10:00 UTC
    result = _format_places([place], "test", now_utc)
    assert "Tuesday: 8:00 AM – 6:30 PM" in result  # already Tuesday at UTC+14
    assert "Monday:" not in result

    no_offset = {k: v for k, v in MONMOUTH.items() if k != "utcOffsetMinutes"}
    result = _format_places([no_offset], "test", now_utc)
    assert "Monday:" not in result and "Tuesday:" not in result


def test_search_omits_missing_fields():
    place = {
        "id": "test_id",
        "displayName": {"text": "No Rating Place"},
        "formattedAddress": "Test Address",
        "businessStatus": "CLOSED_PERMANENTLY",
    }
    result = _format_places([place], "test", datetime.now(timezone.utc))
    assert "No Rating Place — Test Address" in result
    assert "(permanently closed)" in result
    assert "rating" not in result
    assert "open now" not in result and "closed now" not in result
    assert "None" not in result


def test_search_closed_temporarily():
    place = {"id": "x", "displayName": {"text": "P"}, "businessStatus": "CLOSED_TEMPORARILY"}
    assert "(temporarily closed)" in _format_places([place], "t", datetime.now(timezone.utc))


def test_search_empty_results():
    result = _format_places([], "nonexistent place", datetime.now(timezone.utc))
    assert result == "No places found for 'nonexistent place'."


# ---- maps_search request shape -----------------------------------------------

async def test_search_request_shape():
    """Exact mask equality — a substring assertion here previously masked a
    doubled 'places.places.' prefix that 400s against the real API."""
    seen, handler = capture({"places": []})
    await search_tool(handler).run({"query": "test", "count": 3})

    req = seen["request"]
    assert req.method == "POST"
    assert req.url.host == "places.googleapis.com"
    assert req.headers["X-Goog-Api-Key"] == "test_key"
    assert set(req.headers["X-Goog-FieldMask"].split(",")) == {
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.currentOpeningHours",
        "places.regularOpeningHours.weekdayDescriptions",
        "places.utcOffsetMinutes",
    }
    body = json.loads(req.content)
    assert body == {"textQuery": "test", "pageSize": 3}


async def test_search_count_clamped_through_tool():
    seen, handler = capture({"places": []})
    tool = search_tool(handler)

    await tool.run({"query": "q", "count": 100})
    assert json.loads(seen["request"].content)["pageSize"] == 8
    await tool.run({"query": "q", "count": 0})
    assert json.loads(seen["request"].content)["pageSize"] == 1
    await tool.run({"query": "q"})
    assert json.loads(seen["request"].content)["pageSize"] == 4
    await tool.run({"query": "q", "count": None})  # schema is advisory
    assert json.loads(seen["request"].content)["pageSize"] == 4


async def test_search_open_now_parameter():
    seen, handler = capture({"places": []})
    await search_tool(handler).run({"query": "test", "open_now": True})
    assert json.loads(seen["request"].content)["openNow"] is True

    await search_tool(handler).run({"query": "test"})
    assert "openNow" not in json.loads(seen["request"].content)


def test_client_ignores_proxy_env():
    assert _client(None).trust_env is False


# ---- helpers ------------------------------------------------------------------

def test_fmt_duration():
    assert _fmt_duration("300s") == "5 min"
    assert _fmt_duration("3600s") == "1h"
    assert _fmt_duration("5400s") == "1h 30m"
    assert _fmt_duration("garbage") == "garbage"


def test_fmt_km():
    assert _fmt_km(21300) == "21.3 km"
    assert _fmt_km(500) == "0.5 km"


def test_waypoint_contract():
    # places/ prefix → placeId, stripped — including non-ChIJ ids (the format
    # is not a contract; the prefix is the only discriminator).
    assert _waypoint("places/EiQnonChIJ123") == {"placeId": "EiQnonChIJ123"}
    # Our own emitted label pastes in verbatim.
    assert _waypoint("place_id: places/ChIJabc") == {"placeId": "ChIJabc"}
    # A bare id WITHOUT the prefix is deliberately free text, never sniffed.
    assert _waypoint("ChIJd8kx3t4EdkgR") == {"address": "ChIJd8kx3t4EdkgR"}
    assert _waypoint("1 Infinite Loop, Cupertino") == {
        "address": "1 Infinite Loop, Cupertino"
    }


# ---- maps_route ---------------------------------------------------------------

DRIVE_ROUTES = {
    "routes": [
        {
            "duration": "2040s",
            "staticDuration": "1560s",
            "distanceMeters": 21300,
            "description": "M4",
        }
    ]
}


async def test_route_drive_traffic_contrast():
    seen, handler = capture(DRIVE_ROUTES)
    result = await route_tool(handler).run(
        {"origin": "Home", "destination": "Heathrow T5", "mode": "drive"}
    )
    assert result.startswith(UNTRUSTED_NOTE_ROUTES)
    assert "Home → Heathrow T5: 34 min in current traffic (26 min without), 21.3 km via M4." in result


async def test_route_request_shape():
    seen, handler = capture({"routes": []})
    tool = route_tool(handler)

    await tool.run({
        "origin": "places/EiQnonChIJ99",  # non-ChIJ id round-trips (contract, not format)
        "destination": "1 Road, Town",
        "mode": "drive",
        "depart_at": "2026-07-08T10:00:00Z",
    })
    req = seen["request"]
    assert req.url.host == "routes.googleapis.com"
    assert req.headers["X-Goog-Api-Key"] == "test_key"
    assert set(req.headers["X-Goog-FieldMask"].split(",")) == {
        "routes.duration",
        "routes.staticDuration",
        "routes.distanceMeters",
        "routes.description",
        "routes.localizedValues",
        "routes.legs.steps.travelMode",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.distanceMeters",
        "routes.legs.steps.transitDetails",
    }
    body = json.loads(req.content)
    assert body["origin"] == {"placeId": "EiQnonChIJ99"}
    assert body["destination"] == {"address": "1 Road, Town"}
    assert body["travelMode"] == "DRIVE"
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    assert body["departureTime"] == "2026-07-08T10:00:00Z"

    await tool.run({"origin": "A", "destination": "B", "mode": "walk"})
    body = json.loads(seen["request"].content)
    assert body["travelMode"] == "WALK"
    assert "routingPreference" not in body  # invalid outside drive
    assert "departureTime" not in body


async def test_route_local_validation_no_http():
    seen, handler = capture({"routes": []})
    tool = route_tool(handler)

    result = await tool.run({
        "origin": "A", "destination": "B",
        "mode": "drive", "arrive_at": "2026-07-08T15:00:00Z",
    })
    assert "arrive_at is only valid for transit mode" in result
    assert seen["request"] is None

    result = await tool.run({
        "origin": "A", "destination": "B", "mode": "transit",
        "depart_at": "2026-07-08T10:00:00Z", "arrive_at": "2026-07-08T15:00:00Z",
    })
    assert "mutually exclusive" in result
    assert seen["request"] is None

    result = await tool.run({"origin": "A", "destination": "B", "mode": "hoverboard"})
    assert "mode must be" in result
    assert seen["request"] is None

    result = await tool.run({"origin": "", "destination": "B"})
    assert "'origin' and 'destination' are required" in result
    assert seen["request"] is None


async def test_route_no_route_found():
    _, handler = capture({"routes": []})
    result = await route_tool(handler).run({"origin": "A", "destination": "B"})
    assert "No route found" in result


async def test_route_alternatives():
    alt = {
        "duration": "2100s",
        "staticDuration": "1680s",
        "distanceMeters": 22000,
        "description": "A406",
    }
    seen, handler = capture({"routes": [DRIVE_ROUTES["routes"][0], alt]})
    result = await route_tool(handler).run(
        {"origin": "Home", "destination": "Office", "mode": "drive", "alternatives": True}
    )
    assert json.loads(seen["request"].content)["computeAlternativeRoutes"] is True
    assert "34 min in current traffic" in result
    assert "Alternative: 35 min in current traffic (28 min without), 22.0 km via A406." in result


def test_route_walk_has_no_traffic_phrasing():
    routes = [{"duration": "1200s", "distanceMeters": 1600}]
    result = _format_route(routes, "walk", "Home", "Shop")
    assert "Home → Shop: 20 min, 1.6 km." in result
    assert "traffic" not in result


# ---- transit formatting -------------------------------------------------------

def _transit_route(with_localized: bool) -> dict:
    details = {
        "transitLine": {"nameShort": "Elizabeth line"},
        "headsign": "Westbound",
        "stopCount": 5,
        "stopDetails": {
            "departureStop": {"name": "Paddington"},
            "arrivalStop": {"name": "Heathrow T5"},
            # Routes API v2: these are RFC3339 Timestamp STRINGS, not dicts.
            "departureTime": "2026-07-08T09:04:00Z",
            "arrivalTime": "2026-07-08T09:31:00Z",
        },
    }
    if with_localized:
        details["localizedValues"] = {
            "departureTime": {"time": {"text": "09:04"}, "timeZone": "Europe/London"},
            "arrivalTime": {"time": {"text": "09:31"}, "timeZone": "Europe/London"},
        }
    return {
        "duration": "2520s",
        "legs": [
            {
                "steps": [
                    {"travelMode": "WALK", "staticDuration": "480s", "distanceMeters": 600},
                    {
                        "travelMode": "TRANSIT",
                        "staticDuration": "1620s",
                        "transitDetails": details,
                    },
                ]
            }
        ],
    }


def test_transit_leg_localized_times():
    result = _format_route([_transit_route(True)], "transit", "Home", "Heathrow T5")
    assert result.startswith(UNTRUSTED_NOTE_ROUTES)
    assert "Home → Heathrow T5: 42 min (8 min walking)" in result
    assert "walk 8 min (0.6 km)" in result
    assert (
        "Elizabeth line towards Westbound from Paddington 09:04 "
        "→ Heathrow T5 09:31 · 27 min, 5 stops" in result
    )
    assert "RFC3339" not in result  # no hint when display strings were used


def test_transit_leg_rfc3339_fallback():
    """Without localizedValues the raw Timestamp strings render (they are
    strings, not dicts — pinned so a dict-shaped parse can't come back) plus
    one convert_time hint."""
    result = _format_route([_transit_route(False)], "transit", "Home", "Heathrow T5")
    assert (
        "Elizabeth line towards Westbound from Paddington 2026-07-08T09:04:00Z "
        "→ Heathrow T5 2026-07-08T09:31:00Z" in result
    )
    assert "use convert_time" in result


def test_transit_line_name_fallback():
    route = _transit_route(True)
    details = route["legs"][0]["steps"][1]["transitDetails"]
    details["transitLine"] = {"name": "Great Western Railway"}  # no nameShort
    result = _format_route([route], "transit", "A", "B")
    assert "Great Western Railway towards Westbound from Paddington" in result


def test_transit_tiny_walk_hidden_but_counted():
    route = _transit_route(True)
    route["legs"][0]["steps"].append(
        {"travelMode": "WALK", "staticDuration": "45s", "distanceMeters": 40}
    )
    lines, walk_seconds, _ = _transit_legs(route)
    assert walk_seconds == 525  # 480 + 45 — the stub still counts toward the header
    assert sum(line.startswith("walk") for line in lines) == 1  # stub line hidden


def test_transit_consecutive_walks_merged():
    """Google splits one walk into several WALK steps — they render as one line."""
    route = _transit_route(True)
    route["legs"][0]["steps"][0:1] = [
        {"travelMode": "WALK", "staticDuration": "300s", "distanceMeters": 400},
        {"travelMode": "WALK", "staticDuration": "180s", "distanceMeters": 200},
    ]
    lines, walk_seconds, _ = _transit_legs(route)
    assert lines[0] == "walk 8 min (0.6 km)"
    assert walk_seconds == 480


def test_transit_alternatives():
    alt = _transit_route(False)  # raw times → the one hint comes from here
    alt["duration"] = "3060s"
    result = _format_route(
        [_transit_route(True), alt], "transit", "Home", "Heathrow T5"
    )
    assert "Home → Heathrow T5: 42 min (8 min walking)" in result
    assert "   Alternative: 51 min (8 min walking)" in result
    assert result.count("Elizabeth line") == 2  # both routes render their steps
    assert result.count("use convert_time") == 1
    assert result.rstrip().endswith("op='to_local')")  # hint last, once


def test_transit_step_missing_optional_fields():
    route = _transit_route(True)
    steps = route["legs"][0]["steps"]
    del steps[1]["staticDuration"]
    del steps[1]["transitDetails"]["headsign"]
    del steps[1]["transitDetails"]["stopCount"]
    result = _format_route([route], "transit", "A", "B")
    assert "Elizabeth line from Paddington 09:04 → Heathrow T5 09:31" in result
    assert "towards" not in result
    assert "·" not in result  # no dangling suffix separator
    assert "None" not in result


def test_transit_route_all_walk():
    """Google returns pure-WALK routes in transit mode for short hops."""
    route = {
        "duration": "600s",
        "legs": [
            {"steps": [{"travelMode": "WALK", "staticDuration": "600s", "distanceMeters": 800}]}
        ],
    }
    result = _format_route([route], "transit", "Home", "Shop")
    assert "Home → Shop: 10 min (10 min walking)" in result
    assert "walk 10 min (0.8 km)" in result


# ---- error paths ---------------------------------------------------------------

async def test_error_403_names_env_var_not_key():
    result = await search_tool(lambda r: httpx.Response(403, json={})).run(
        {"query": "test"}
    )
    assert "GOOGLE_MAPS_API_KEY" in result
    assert "test_key" not in result


async def test_error_429_no_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={})

    result = await search_tool(handler).run({"query": "test"})
    assert "quota hit" in result
    assert calls["n"] == 1  # deliberately no retry (unlike Brave's 1 req/s tier)


async def test_error_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out")

    result = await route_tool(handler).run({"origin": "A", "destination": "B"})
    assert "did not respond within 10s" in result


async def test_error_unreachable():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    result = await search_tool(handler).run({"query": "test"})
    assert "could not reach Google (ConnectError)" in result


async def test_error_google_error_body():
    handler = lambda r: httpx.Response(
        500, json={"error": {"message": "Routes API has not been used in project"}}
    )
    result = await search_tool(handler).run({"query": "test"})
    assert "HTTP 500" in result
    assert "Routes API has not been used in project" in result


async def test_error_non_json_error_body():
    result = await search_tool(lambda r: httpx.Response(502, text="Bad Gateway")).run(
        {"query": "test"}
    )
    assert result == "maps_search error: Google returned HTTP 502"


async def test_error_empty_inputs():
    never = lambda r: (_ for _ in ()).throw(AssertionError("no HTTP expected"))
    assert "'query' is required" in await search_tool(never).run({"query": "  "})
    assert "required" in await route_tool(never).run({"origin": "A", "destination": ""})
