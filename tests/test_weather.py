"""Test weather tool with mocked Open-Meteo responses."""

import pytest
import httpx
from unittest.mock import patch

from harness.tools.weather import WeatherTool, _format


@pytest.fixture
def tool():
    """WeatherTool with a mock transport."""
    return WeatherTool()


def mock_response(json_data, status_code=200):
    """Helper to create a mock httpx.Response with JSON content."""
    import json
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(json_data).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_weather_place_resolution(tool):
    """Geocode resolution and formatting."""
    geocode_data = {
        "results": [
            {
                "name": "Paris",
                "country": "France",
                "latitude": 48.8566,
                "longitude": 2.3522,
            }
        ]
    }
    forecast_data = {
        "timezone": "Europe/Paris",
        "daily": {
            "time": ["2026-07-08"],
            "weather_code": [2],
            "temperature_2m_max": [28],
            "temperature_2m_min": [16],
            "precipitation_probability_max": [10],
            "wind_speed_10m_max": [8],
        },
    }

    class MockTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.call_count = 0

        async def handle_async_request(self, request):
            self.call_count += 1
            if "geocoding-api" in request.url.host:
                return mock_response(geocode_data)
            else:
                return mock_response(forecast_data)

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)
    result = await tool_with_transport.run({"place": "Paris"})

    assert "Paris" in result
    assert "France" in result
    assert "partly cloudy" in result
    assert transport.call_count == 2


@pytest.mark.asyncio
async def test_weather_place_not_found(tool):
    """No results from geocode."""

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return mock_response({"results": []})

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)
    result = await tool_with_transport.run({"place": "Xyzzy"})

    assert "place not found: Xyzzy" in result


@pytest.mark.asyncio
async def test_weather_query_params_no_smuggling(tool):
    """Query params never f-string interpolated (& = smuggling test)."""

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            # Verify the 'name' param is intact, never split by & or =
            assert request.url.params.get("name") == "City & Country=1"
            return mock_response({"results": []})

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)
    result = await tool_with_transport.run({"place": "City & Country=1"})

    # Should not crash or smuggle params
    assert "place not found" in result


@pytest.mark.asyncio
async def test_weather_days_clamped(tool):
    """Days clamped to 1–7."""
    geocode_data = {"results": [{"name": "NYC", "country": "US", "latitude": 40.7128, "longitude": -74.0060}]}
    forecast_data = {
        "timezone": "America/New_York",
        "daily": {
            "time": ["2026-07-08", "2026-07-09", "2026-07-10"] * 3,
            "weather_code": [0] * 9,
            "temperature_2m_max": [25] * 9,
            "temperature_2m_min": [15] * 9,
            "precipitation_probability_max": [0] * 9,
            "wind_speed_10m_max": [5] * 9,
        },
    }

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "geocoding-api" in request.url.host:
                return mock_response(geocode_data)
            else:
                return mock_response(forecast_data)

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)

    # Request 100 days, should clamp to 7
    result = await tool_with_transport.run({"place": "NYC", "days": 100})
    lines = result.split("\n")
    forecast_lines = [l for l in lines if "–" in l and "°C" in l]  # Date lines
    assert len(forecast_lines) == 7

    # Request 0 days, should clamp to 1
    result = await tool_with_transport.run({"place": "NYC", "days": 0})
    lines = result.split("\n")
    forecast_lines = [l for l in lines if "–" in l and "°C" in l]
    assert len(forecast_lines) == 1


@pytest.mark.asyncio
async def test_weather_current_when_days_1(tool):
    """Current weather only appears when days == 1."""
    geocode_data = {"results": [{"name": "NYC", "country": "US", "latitude": 40.7128, "longitude": -74.0060}]}
    forecast_data = {
        "timezone": "America/New_York",
        "current": {"temperature_2m": 22, "weather_code": 1, "wind_speed_10m": 5},
        "daily": {
            "time": ["2026-07-08", "2026-07-09"],
            "weather_code": [2, 3],
            "temperature_2m_max": [25, 26],
            "temperature_2m_min": [15, 16],
            "precipitation_probability_max": [10, 20],
            "wind_speed_10m_max": [5, 6],
        },
    }

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "geocoding-api" in request.url.host:
                return mock_response(geocode_data)
            else:
                return mock_response(forecast_data)

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)

    # days=1: should include "Now:" line
    result = await tool_with_transport.run({"place": "NYC", "days": 1})
    assert "Now:" in result
    assert "22°C" in result

    # days=2: should NOT include "Now:" line (current only when days==1)
    result = await tool_with_transport.run({"place": "NYC", "days": 2})
    assert "Now:" not in result


@pytest.mark.asyncio
async def test_weather_wmo_codes(tool):
    """WMO code mapping."""
    geocode_data = {"results": [{"name": "Test", "country": "TC", "latitude": 0, "longitude": 0}]}
    forecast_data = {
        "timezone": "UTC",
        "daily": {
            "time": ["2026-07-08"],
            "weather_code": [99],  # Thunderstorm + hail
            "temperature_2m_max": [25],
            "temperature_2m_min": [15],
            "precipitation_probability_max": [100],
            "wind_speed_10m_max": [50],
        },
    }

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "geocoding-api" in request.url.host:
                return mock_response(geocode_data)
            else:
                return mock_response(forecast_data)

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)
    result = await tool_with_transport.run({"place": "Test"})

    assert "thunderstorm + hail" in result.lower()

    # Unknown code should map to "unsettled"
    forecast_data["daily"]["weather_code"] = [9999]

    result = await tool_with_transport.run({"place": "Test"})
    assert "unsettled" in result


@pytest.mark.asyncio
async def test_weather_timeout(tool):
    """Timeout returns friendly string."""

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.TimeoutException("timeout")

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)
    result = await tool_with_transport.run({"place": "Test"})

    assert "error" in result.lower()
    assert "10s" in result or "timeout" in result.lower()


@pytest.mark.asyncio
async def test_weather_untrusted_note(tool):
    """Output carries untrusted data prefix."""
    geocode_data = {"results": [{"name": "Test", "country": "TC", "latitude": 0, "longitude": 0}]}
    forecast_data = {
        "timezone": "UTC",
        "daily": {
            "time": ["2026-07-08"],
            "weather_code": [0],
            "temperature_2m_max": [25],
            "temperature_2m_min": [15],
            "precipitation_probability_max": [0],
            "wind_speed_10m_max": [5],
        },
    }

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "geocoding-api" in request.url.host:
                return mock_response(geocode_data)
            else:
                return mock_response(forecast_data)

    transport = MockTransport()
    tool_with_transport = WeatherTool(transport=transport)
    result = await tool_with_transport.run({"place": "Test"})

    assert "[weather is external data" in result


@pytest.mark.asyncio
async def test_weather_required_place(tool):
    """Missing place returns error string."""
    result = await tool.run({})
    assert "error" in result.lower()
    assert "place" in result.lower()

    result = await tool.run({"place": ""})
    assert "error" in result.lower()
