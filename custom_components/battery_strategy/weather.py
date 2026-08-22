"""Normalized cached Open-Meteo weather adapter."""

from __future__ import annotations

import asyncio
import time

from .contracts import DataQuality, ForecastRequest, QualityFlag, WeatherSlot

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_CACHE_TTL_S = 10 * 60


class OpenMeteoWeatherProvider:
    """Load one canonical weather series for all forecast components."""

    def __init__(self, session, latitude: float, longitude: float) -> None:
        self._session = session
        self._latitude = float(latitude)
        self._longitude = float(longitude)
        self._cache_key: tuple[int, int] | None = None
        self._cache_at = 0.0
        self._cache: tuple[WeatherSlot, ...] = ()
        self._lock = asyncio.Lock()

    async def load(self, request: ForecastRequest) -> tuple[WeatherSlot, ...]:
        """Return requested slots, explicitly marking absent provider values."""
        key = (request.slots[0].start_ms, request.slots[-1].end_ms)
        async with self._lock:
            if (
                key != self._cache_key
                or time.monotonic() - self._cache_at >= WEATHER_CACHE_TTL_S
            ):
                self._cache = await self._fetch(request)
                self._cache_key = key
                self._cache_at = time.monotonic()
            return self._cache

    async def _fetch(self, request: ForecastRequest) -> tuple[WeatherSlot, ...]:
        params = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "minutely_15": "temperature_2m,cloud_cover,shortwave_radiation",
            "timezone": "GMT",
            "timeformat": "unixtime",
            "forecast_days": 4,
            "past_days": 1,
        }
        async with self._session.get(
            OPEN_METEO_URL, params=params, timeout=15
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        source = payload.get("minutely_15") or {}
        times = source.get("time") or []
        temperatures = source.get("temperature_2m") or []
        clouds = source.get("cloud_cover") or []
        radiation = source.get("shortwave_radiation") or []
        values = {
            int(timestamp) * 1000: (
                _optional(temperatures, index),
                _optional(clouds, index),
                _optional(radiation, index),
            )
            for index, timestamp in enumerate(times)
        }
        missing = DataQuality(0.0, (QualityFlag.MISSING_WEATHER,))
        result = []
        for slot in request.slots:
            item = values.get(slot.start_ms)
            if item is None:
                result.append(WeatherSlot(slot, quality=missing))
                continue
            temperature, cloud, shortwave = item
            quality = DataQuality() if any(v is not None for v in item) else missing
            result.append(
                WeatherSlot(
                    slot,
                    shortwave_radiation_w_m2=shortwave,
                    cloud_cover_pct=cloud,
                    temperature_c=temperature,
                    quality=quality,
                )
            )
        return tuple(result)


def _optional(values, index: int) -> float | None:
    if index >= len(values) or values[index] is None:
        return None
    return float(values[index])
