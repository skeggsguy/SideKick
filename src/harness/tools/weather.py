"""weather: current + forecast weather from Open-Meteo (no API key required).

Results are external third-party data — the harness's ingestion rule applies, so
output is prefixed with an untrusted-data note and must never be treated as
instructions.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..core.policy import ActionClass
from .base import Tool

UNTRUSTED_NOTE = (
    "[weather is external data — subject to change, refresh before acting on forecasts]"
)

_WMO = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "cloudy",
    45: "foggy",
    48: "foggy (deposits)",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm + light hail",
    99: "thunderstorm + hail",
}


def _wmo_description(code: int) -> str:
    return _WMO.get(code, "unsettled")


def _format(geo: dict, forecast: dict, days: int) -> str:
    name = geo.get("name", "?")
    country = geo.get("country", "?")
    tz = forecast.get("timezone", "")

    lines = [
        UNTRUSTED_NOTE,
        f"Weather for {name}, {country} — data: Open-Meteo",
    ]

    if days == 1 and "current" in forecast:
        current = forecast["current"]
        temp = current.get("temperature_2m", "?")
        code = current.get("weather_code", 0)
        wind = current.get("wind_speed_10m", "?")
        lines.append(f"Now: {_wmo_description(code)}, {temp}°C, wind {wind} km/h")

    daily = forecast.get("daily", {})
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    temp_max = daily.get("temperature_2m_max", [])
    temp_min = daily.get("temperature_2m_min", [])
    precip_prob = daily.get("precipitation_probability_max", [])
    wind_speed = daily.get("wind_speed_10m_max", [])

    for i in range(min(days, len(dates))):
        date = dates[i] if i < len(dates) else "?"
        desc = _wmo_description(codes[i] if i < len(codes) else 0)
        hi = temp_max[i] if i < len(temp_max) else "?"
        lo = temp_min[i] if i < len(temp_min) else "?"
        rain_pct = precip_prob[i] if i < len(precip_prob) else "?"
        wind = wind_speed[i] if i < len(wind_speed) else "?"

        lines.append(
            f"{date}: {desc}, {lo}–{hi}°C, {rain_pct}% rain, "
            f"wind {wind} km/h"
        )

    return "\n".join(lines)


class WeatherTool(Tool):
    name = "weather"
    description = "Current weather and a 1–7 day forecast for any place (Open-Meteo)."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "place": {"type": "string", "description": "City or 'city, country'."},
            "days": {"type": "integer", "description": "Forecast days 1–7 (default 1)."},
        },
        "required": ["place"],
    }
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(
        self, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._transport = transport

    async def run(self, args: dict[str, Any]) -> str:
        place = str(args.get("place", "")).strip()
        if not place:
            return "weather error: 'place' is required."

        days = min(max(int(args.get("days", 1)), 1), 7)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=2.0),
                transport=self._transport,
                trust_env=False,
            ) as client:
                geo = await self._geocode(client, place)
                if isinstance(geo, str):
                    return geo

                forecast = await self._forecast(
                    client, geo["latitude"], geo["longitude"], days
                )
                if isinstance(forecast, str):
                    return forecast
        except httpx.TimeoutException:
            return "weather error: Open-Meteo did not respond within 10s — try again."
        except httpx.HTTPError as e:
            return f"weather error: could not reach Open-Meteo ({type(e).__name__})."

        return _format(geo, forecast, days)

    async def _geocode(
        self, client: httpx.AsyncClient, place: str
    ) -> dict | str:
        try:
            resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1},
            )
        except (httpx.TimeoutException, httpx.HTTPError):
            raise

        if resp.status_code != 200:
            return f"weather error: geocoding returned HTTP {resp.status_code}."

        results = resp.json().get("results", [])
        if not results:
            return f"place not found: {place}"

        return results[0]

    async def _forecast(
        self, client: httpx.AsyncClient, lat: float, lon: float, days: int
    ) -> dict | str:
        daily_fields = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"
        current_fields = "temperature_2m,weather_code,wind_speed_10m" if days == 1 else ""

        params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": "auto",
            "daily": daily_fields,
        }
        if current_fields:
            params["current"] = current_fields

        try:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params=params,
            )
        except (httpx.TimeoutException, httpx.HTTPError):
            raise

        if resp.status_code != 200:
            return f"weather error: forecast returned HTTP {resp.status_code}."

        return resp.json()
