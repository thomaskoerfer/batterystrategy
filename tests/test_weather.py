"""Tests for the normalized central Open-Meteo adapter."""

from __future__ import annotations

import asyncio
import unittest

from custom_components.battery_strategy.contracts import (
    ForecastRequest,
    QualityFlag,
    SlotKey,
)
from custom_components.battery_strategy.weather import (
    WEATHER_STALE_MAX_S,
    OpenMeteoWeatherProvider,
)


class _Response:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        if self._error:
            raise self._error
        return None

    async def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.error = None

    def get(self, *args, **kwargs):
        self.calls += 1
        return _Response(self.payload, self.error)


class WeatherProviderTests(unittest.TestCase):
    def test_provider_aligns_slots_marks_missing_and_caches(self):
        session = _Session(
            {
                "minutely_15": {
                    "time": [0],
                    "temperature_2m": [12.5],
                    "cloud_cover": [40],
                    "shortwave_radiation": [123],
                }
            }
        )
        provider = OpenMeteoWeatherProvider(session, 50.9, 6.1)
        request = ForecastRequest(
            0,
            "Europe/Berlin",
            (SlotKey(0, 900_000), SlotKey(900_000, 1_800_000)),
        )
        first = asyncio.run(provider.load(request))
        second = asyncio.run(provider.load(request))
        self.assertEqual(first[0].temperature_c, 12.5)
        self.assertEqual(first[0].shortwave_radiation_w_m2, 123.0)
        self.assertIn(QualityFlag.MISSING_WEATHER, first[1].quality.flags)
        self.assertEqual(first, second)
        self.assertEqual(session.calls, 1)

    def test_provider_keeps_recent_success_as_estimated_on_transient_error(self):
        session = _Session(
            {
                "minutely_15": {
                    "time": [0],
                    "temperature_2m": [12.5],
                    "cloud_cover": [40],
                    "shortwave_radiation": [123],
                }
            }
        )
        provider = OpenMeteoWeatherProvider(session, 50.9, 6.1)
        request = ForecastRequest(
            0,
            "Europe/Berlin",
            (SlotKey(0, 900_000),),
        )
        first = asyncio.run(provider.load(request))
        provider._cache_at -= 11 * 60
        session.error = RuntimeError("temporary outage")

        stale = asyncio.run(provider.load(request))

        self.assertEqual(stale[0].temperature_c, first[0].temperature_c)
        self.assertIn(QualityFlag.ESTIMATED, stale[0].quality.flags)
        self.assertIn("temporary outage", provider.last_error)
        self.assertEqual(session.calls, 2)

    def test_provider_rejects_expired_cache_on_transient_error(self):
        session = _Session(
            {
                "minutely_15": {
                    "time": [0],
                    "temperature_2m": [12.5],
                    "cloud_cover": [40],
                    "shortwave_radiation": [123],
                }
            }
        )
        provider = OpenMeteoWeatherProvider(session, 50.9, 6.1)
        request = ForecastRequest(0, "Europe/Berlin", (SlotKey(0, 900_000),))
        asyncio.run(provider.load(request))
        provider._cache_at -= WEATHER_STALE_MAX_S + 1
        session.error = RuntimeError("long outage")

        with self.assertRaisesRegex(RuntimeError, "long outage"):
            asyncio.run(provider.load(request))
