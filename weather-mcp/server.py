#!/usr/bin/env python3
"""
MCP Server for Weather data using Open-Meteo API (no API key required).
"""

import json
import os
from typing import Optional
from enum import Enum

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather_mcp")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


# ── Shared helpers ────────────────────────────────────────────────────────────

async def _geocode(city: str) -> dict:
    """Return the first geocoding result for a city name."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            GEOCODING_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=15.0,
        )
        r.raise_for_status()
    results = r.json().get("results")
    if not results:
        raise ValueError(f"Location not found: '{city}'. Try a more specific city name.")
    return results[0]


def _handle_error(e: Exception) -> str:
    if isinstance(e, ValueError):
        return f"Error: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        return f"Error: API request failed (HTTP {e.response.status_code})."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Please try again."
    return f"Error: {type(e).__name__}: {e}"


def _wind_direction(deg: float) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(deg / 45) % 8]


# ── Input models ─────────────────────────────────────────────────────────────

class GeoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    city: str = Field(..., description="City name (e.g. 'London', 'New York')", min_length=2, max_length=100)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")


class CurrentWeatherInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    city: str = Field(..., description="City name (e.g. 'Tokyo', 'Paris')", min_length=2, max_length=100)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")


class ForecastInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    city: str = Field(..., description="City name (e.g. 'Berlin', 'Sydney')", min_length=2, max_length=100)
    days: int = Field(default=7, description="Number of forecast days (1–16)", ge=1, le=16)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")


class HistoricalInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    city: str = Field(..., description="City name", min_length=2, max_length=100)
    start_date: str = Field(..., description="Start date in YYYY-MM-DD format (e.g. '2024-01-01')")
    end_date: str = Field(..., description="End date in YYYY-MM-DD format (e.g. '2024-01-31')")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="weather_geocode",
    annotations={
        "title": "Geocode a City",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def weather_geocode(params: GeoInput) -> str:
    """Convert a city name to latitude, longitude, country, and timezone.

    Use this tool first when coordinates are needed for other weather tools,
    or when the user asks where a city is located.

    Args:
        params (GeoInput):
            - city (str): City name to look up
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Location details including lat/lon, country, population, timezone.

    Examples:
        - "Where is Reykjavik?" -> weather_geocode(city="Reykjavik")
        - "Get coordinates for Mumbai" -> weather_geocode(city="Mumbai")
    """
    try:
        loc = await _geocode(params.city)
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(loc, indent=2)
        return (
            f"## {loc['name']}, {loc.get('country', 'N/A')}\n"
            f"- **Latitude**: {loc['latitude']}\n"
            f"- **Longitude**: {loc['longitude']}\n"
            f"- **Timezone**: {loc.get('timezone', 'N/A')}\n"
            f"- **Population**: {loc.get('population', 'N/A'):,}\n"
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="weather_get_current",
    annotations={
        "title": "Get Current Weather",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def weather_get_current(params: CurrentWeatherInput) -> str:
    """Get current weather conditions for a city.

    Returns temperature, apparent temperature, humidity, wind speed/direction,
    precipitation, cloud cover, and weather condition code.

    Args:
        params (CurrentWeatherInput):
            - city (str): City name (e.g. 'London', 'New York')
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Current weather conditions. JSON schema:
        {
            "city": str,
            "country": str,
            "latitude": float,
            "longitude": float,
            "temperature_c": float,
            "feels_like_c": float,
            "humidity_pct": int,
            "wind_speed_kmh": float,
            "wind_direction": str,
            "precipitation_mm": float,
            "cloud_cover_pct": int,
            "weather_code": int
        }

    Error responses:
        "Error: Location not found: '<city>'. Try a more specific city name."
    """
    try:
        loc = await _geocode(params.city)
        async with httpx.AsyncClient() as client:
            r = await client.get(
                WEATHER_URL,
                params={
                    "latitude": loc["latitude"],
                    "longitude": loc["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation,cloud_cover,weather_code",
                    "timezone": loc.get("timezone", "auto"),
                },
                timeout=15.0,
            )
            r.raise_for_status()
        data = r.json()["current"]

        result = {
            "city": loc["name"],
            "country": loc.get("country", ""),
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "temperature_c": data["temperature_2m"],
            "feels_like_c": data["apparent_temperature"],
            "humidity_pct": data["relative_humidity_2m"],
            "wind_speed_kmh": data["wind_speed_10m"],
            "wind_direction": _wind_direction(data["wind_direction_10m"]),
            "precipitation_mm": data["precipitation"],
            "cloud_cover_pct": data["cloud_cover"],
            "weather_code": data["weather_code"],
        }

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(result, indent=2)

        return (
            f"## Current Weather: {result['city']}, {result['country']}\n\n"
            f"- **Temperature**: {result['temperature_c']}°C (feels like {result['feels_like_c']}°C)\n"
            f"- **Humidity**: {result['humidity_pct']}%\n"
            f"- **Wind**: {result['wind_speed_kmh']} km/h {result['wind_direction']}\n"
            f"- **Precipitation**: {result['precipitation_mm']} mm\n"
            f"- **Cloud Cover**: {result['cloud_cover_pct']}%\n"
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="weather_get_forecast",
    annotations={
        "title": "Get Weather Forecast",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def weather_get_forecast(params: ForecastInput) -> str:
    """Get a daily weather forecast for a city (up to 16 days).

    Returns max/min temperature, precipitation sum, wind speed, and dominant
    wind direction for each day.

    Args:
        params (ForecastInput):
            - city (str): City name
            - days (int): Number of days to forecast (1–16, default 7)
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Daily forecast data. JSON schema:
        {
            "city": str,
            "country": str,
            "days": [
                {
                    "date": str,           # YYYY-MM-DD
                    "temp_max_c": float,
                    "temp_min_c": float,
                    "precipitation_mm": float,
                    "wind_max_kmh": float,
                    "wind_direction": str
                }
            ]
        }

    Examples:
        - "What's the weather like in Rome this week?" -> weather_get_forecast(city="Rome", days=7)
        - "Will it rain in Seattle tomorrow?" -> weather_get_forecast(city="Seattle", days=1)
    """
    try:
        loc = await _geocode(params.city)
        async with httpx.AsyncClient() as client:
            r = await client.get(
                WEATHER_URL,
                params={
                    "latitude": loc["latitude"],
                    "longitude": loc["longitude"],
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,wind_direction_10m_dominant",
                    "forecast_days": params.days,
                    "timezone": loc.get("timezone", "auto"),
                },
                timeout=15.0,
            )
            r.raise_for_status()
        daily = r.json()["daily"]

        days_data = [
            {
                "date": daily["time"][i],
                "temp_max_c": daily["temperature_2m_max"][i],
                "temp_min_c": daily["temperature_2m_min"][i],
                "precipitation_mm": daily["precipitation_sum"][i],
                "wind_max_kmh": daily["wind_speed_10m_max"][i],
                "wind_direction": _wind_direction(daily["wind_direction_10m_dominant"][i]),
            }
            for i in range(len(daily["time"]))
        ]

        result = {"city": loc["name"], "country": loc.get("country", ""), "days": days_data}

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(result, indent=2)

        lines = [f"## {params.days}-Day Forecast: {loc['name']}, {loc.get('country', '')}\n"]
        for d in days_data:
            lines.append(
                f"**{d['date']}** — {d['temp_min_c']}°C – {d['temp_max_c']}°C | "
                f"Rain: {d['precipitation_mm']}mm | Wind: {d['wind_max_kmh']} km/h {d['wind_direction']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="weather_get_historical",
    annotations={
        "title": "Get Historical Weather",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def weather_get_historical(params: HistoricalInput) -> str:
    """Get historical daily weather data for a city and date range.

    Returns daily max/min temperature, precipitation, and max wind speed
    for each day in the requested range. Useful for climate analysis,
    trip planning, or answering questions about past weather events.

    Args:
        params (HistoricalInput):
            - city (str): City name
            - start_date (str): Start date YYYY-MM-DD
            - end_date (str): End date YYYY-MM-DD
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Historical daily weather. JSON schema:
        {
            "city": str,
            "country": str,
            "start_date": str,
            "end_date": str,
            "days": [
                {
                    "date": str,
                    "temp_max_c": float,
                    "temp_min_c": float,
                    "precipitation_mm": float,
                    "wind_max_kmh": float
                }
            ]
        }

    Examples:
        - "What was the weather in Paris in January 2024?"
          -> weather_get_historical(city="Paris", start_date="2024-01-01", end_date="2024-01-31")
    """
    try:
        loc = await _geocode(params.city)
        async with httpx.AsyncClient() as client:
            r = await client.get(
                ARCHIVE_URL,
                params={
                    "latitude": loc["latitude"],
                    "longitude": loc["longitude"],
                    "start_date": params.start_date,
                    "end_date": params.end_date,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
                    "timezone": loc.get("timezone", "auto"),
                },
                timeout=20.0,
            )
            r.raise_for_status()
        daily = r.json()["daily"]

        days_data = [
            {
                "date": daily["time"][i],
                "temp_max_c": daily["temperature_2m_max"][i],
                "temp_min_c": daily["temperature_2m_min"][i],
                "precipitation_mm": daily["precipitation_sum"][i],
                "wind_max_kmh": daily["wind_speed_10m_max"][i],
            }
            for i in range(len(daily["time"]))
        ]

        result = {
            "city": loc["name"],
            "country": loc.get("country", ""),
            "start_date": params.start_date,
            "end_date": params.end_date,
            "days": days_data,
        }

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(result, indent=2)

        lines = [f"## Historical Weather: {loc['name']}, {loc.get('country', '')} ({params.start_date} → {params.end_date})\n"]
        for d in days_data:
            lines.append(
                f"**{d['date']}** — {d['temp_min_c']}°C – {d['temp_max_c']}°C | "
                f"Rain: {d['precipitation_mm']}mm | Wind max: {d['wind_max_kmh']} km/h"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http", port=port)
