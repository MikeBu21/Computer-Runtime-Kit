# -*- coding: utf-8 -*-
"""Tests open-meteo-solar-forecast Wrapper."""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from control_algorithm.oms_pv_forecast import (
    ForecastResult,
    compass_azimuth_to_oms,
    fetch_pv_forecast,
    normalize_performance_ratio,
    pv_planes_from_config,
    total_kwp_from_config,
)


class TestCompassAzimuth(unittest.TestCase):
    def test_east_west(self):
        self.assertAlmostEqual(compass_azimuth_to_oms(90), -90)
        self.assertAlmostEqual(compass_azimuth_to_oms(270), 90)
        self.assertAlmostEqual(compass_azimuth_to_oms(180), 0)


class TestPvPlanes(unittest.TestCase):
    def test_east_west_planes(self):
        cfg = {
            "pv_layout": "east_west",
            "lat": 46.75,
            "lon": 14.03,
            "pv_kwp_east": 95,
            "pv_kwp_west": 95,
            "pv_declination": 30,
            "pv_dc_efficiency": 0.93,
        }
        planes = pv_planes_from_config(cfg)
        self.assertEqual(len(planes), 2)
        self.assertEqual(total_kwp_from_config(cfg), 190.0)
        self.assertAlmostEqual(planes[0]["azimuth_oms"], -90)
        self.assertAlmostEqual(planes[1]["azimuth_oms"], 90)


class TestFetchPvForecast(unittest.TestCase):
    def _mock_estimate(self):
        tz = timezone(timedelta(hours=2))
        today = date.today()
        tomorrow = today + timedelta(days=1)
        hour_morning = datetime(today.year, today.month, today.day, 8, 0, tzinfo=tz)
        hour_midday = datetime(today.year, today.month, today.day, 12, 0, tzinfo=tz)
        day_after = today + timedelta(days=2)
        hour_day_after = datetime(day_after.year, day_after.month, day_after.day, 11, 0, tzinfo=tz)

        estimate = MagicMock()
        estimate.now.return_value = datetime.now(tz=tz)
        estimate.energy_production_today = 500_000
        estimate.energy_production_tomorrow = 600_000
        estimate.wh_period = {
            hour_morning: 50_000,
            hour_midday: 100_000,
            hour_day_after: 75_000,
        }
        estimate.watts = {hour_midday: 80_000}
        estimate.power_highest_peak_time_today = hour_midday
        return estimate

    @patch("control_algorithm.oms_pv_forecast.asyncio.run")
    def test_fetch_success(self, mock_run):
        mock_run.return_value = self._mock_estimate()
        config = {
            "has_pv": True,
            "lat": 46.75,
            "lon": 14.03,
            "pv_layout": "east_west",
            "pv_kwp_east": 95,
            "pv_kwp_west": 95,
            "pv_declination": 30,
            "pv_dc_efficiency": 0.93,
            "open_meteo_base_url": "",
        }
        state = {"performance_ratio": 0.9}
        result = fetch_pv_forecast(config, state)
        self.assertFalse(result.fallback_active)
        self.assertAlmostEqual(result.today_raw_kwh, 150.0)
        self.assertAlmostEqual(result.today_kwh, 135.0)
        self.assertAlmostEqual(result.today_hours.get("h08", 0), 45.0)
        self.assertAlmostEqual(result.today_hours.get("h12", 0), 90.0)
        self.assertAlmostEqual(result.day_after_kwh, 67.5)
        self.assertAlmostEqual(result.day_after_hours.get("h11", 0), 67.5)
        self.assertIsNotNone(result.cache)
        self.assertEqual(result.detail.get("engine"), "open-meteo-solar-forecast")

    @patch("control_algorithm.oms_pv_forecast.asyncio.run")
    def test_fetch_fallback(self, mock_run):
        mock_run.side_effect = RuntimeError("API down")
        config = {
            "has_pv": True,
            "lat": 46.75,
            "lon": 14.03,
            "pv_layout": "east_west",
            "pv_kwp_east": 95,
            "pv_kwp_west": 95,
            "pv_declination": 30,
        }
        state = {
            "performance_ratio": 1.0,
            "pv_forecast_cache": {
                "today_raw_kwh": 400.0,
                "tomorrow_raw_kwh": 500.0,
                "today_slots_raw": {"morning": 100, "midday": 200, "afternoon": 50, "evening": 50},
                "today_hours_raw": {"h08": 100.0, "h12": 300.0},
                "tomorrow_slots_raw": {},
                "tomorrow_hours_raw": {},
                "detail": {"engine": "open-meteo-solar-forecast"},
            },
        }
        result = fetch_pv_forecast(config, state)
        self.assertTrue(result.fallback_active)
        self.assertAlmostEqual(result.today_kwh, 400.0)

    @patch("control_algorithm.oms_pv_forecast.asyncio.run")
    def test_cache_fallback_uses_by_date_not_yesterdays_today(self, mock_run):
        """Nach Mitternacht: ISO-Datum aus by_date, nicht gestern „heute“-Felder."""
        mock_run.side_effect = RuntimeError("API down")
        today = date.today()
        yesterday = today - timedelta(days=1)
        config = {
            "has_pv": True,
            "lat": 46.75,
            "lon": 14.03,
            "pv_layout": "east_west",
            "pv_kwp_east": 8,
            "pv_kwp_west": 8,
            "pv_declination": 30,
        }
        state = {
            "performance_ratio": 1.0,
            "pv_forecast_cache": {
                # Relative Felder = gestern „heute“ (falsch nach Mitternacht)
                "today_hours_raw": {"h10": 64.0},
                "tomorrow_hours_raw": {"h10": 38.0},
                "day_after_hours_raw": {"h10": 50.0},
                "today_slots_raw": {},
                "tomorrow_slots_raw": {},
                "day_after_slots_raw": {},
                "fetched_at": datetime.combine(yesterday, datetime.min.time()).isoformat(),
                "pv_forecast_by_date": {
                    yesterday.isoformat(): {
                        "daily_kwh": 64.0,
                        "daily_raw_kwh": 64.0,
                        "hours_raw": {"h10": 64.0},
                        "hours": {"h10": 64.0},
                        "slots": {},
                    },
                    today.isoformat(): {
                        "daily_kwh": 38.0,
                        "daily_raw_kwh": 38.0,
                        "hours_raw": {"h10": 38.0},
                        "hours": {"h10": 38.0},
                        "slots": {},
                    },
                    (today + timedelta(days=1)).isoformat(): {
                        "daily_kwh": 50.0,
                        "daily_raw_kwh": 50.0,
                        "hours_raw": {"h10": 50.0},
                        "hours": {"h10": 50.0},
                        "slots": {},
                    },
                },
                "detail": {},
            },
        }
        result = fetch_pv_forecast(config, state)
        self.assertTrue(result.fallback_active)
        self.assertAlmostEqual(result.today_kwh, 38.0, places=1)
        self.assertAlmostEqual(result.tomorrow_kwh, 50.0, places=1)


class TestPerformanceRatio(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(normalize_performance_ratio(5), 1.25)
        self.assertEqual(normalize_performance_ratio(0.01), 0.50)


if __name__ == "__main__":
    unittest.main()
