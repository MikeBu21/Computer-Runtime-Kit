# -*- coding: utf-8 -*-
"""Wetter-Fetch 2h + Mitternachtssperre + laufende PV-Referenzprognose."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from control_algorithm.oms_pv_forecast import ForecastResult
from control_algorithm.service import (
    WEATHER_INTERVAL_SECONDS,
    in_weather_midnight_quiet,
    refresh_weather,
)


class TestWeatherRefreshAnchor(unittest.TestCase):
    def test_weather_interval_is_two_hours(self):
        self.assertEqual(WEATHER_INTERVAL_SECONDS, 2 * 3600)

    def test_midnight_quiet_window(self):
        self.assertTrue(in_weather_midnight_quiet(datetime(2026, 9, 24, 23, 50)))
        self.assertTrue(in_weather_midnight_quiet(datetime(2026, 9, 25, 0, 30)))
        self.assertTrue(in_weather_midnight_quiet(datetime(2026, 9, 25, 1, 14)))
        self.assertFalse(in_weather_midnight_quiet(datetime(2026, 9, 25, 1, 15)))
        self.assertFalse(in_weather_midnight_quiet(datetime(2026, 9, 24, 12, 0)))
        self.assertFalse(in_weather_midnight_quiet(datetime(2026, 9, 24, 23, 44)))

    @patch("control_algorithm.service.fetch_pv_forecast")
    @patch("control_algorithm.service.datetime")
    def test_refresh_skips_in_midnight_quiet(self, mock_dt, mock_fetch):
        mock_dt.now.return_value = datetime(2026, 9, 25, 0, 10, 0)
        mock_dt.fromisoformat = datetime.fromisoformat
        state = {"last_weather_fetch": None}
        config = {"lat": 46.7, "lon": 14.0, "has_pv": True, "pv_kwp": 16}
        out = refresh_weather(config, state, force=True)
        mock_fetch.assert_not_called()
        self.assertIn("Mitternacht", str(out.get("weather_refresh_note") or ""))

    @patch("control_algorithm.service.fetch_pv_forecast")
    @patch("control_algorithm.service.datetime")
    def test_refresh_updates_reference_every_good_fetch(self, mock_dt, mock_fetch):
        mock_dt.now.return_value = datetime(2026, 9, 24, 12, 0, 0)
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_fetch.return_value = ForecastResult(
            today_kwh=38.0,
            tomorrow_kwh=60.0,
            today_slots={"morning": 10},
            today_hours={"h10": 5.0, "h12": 8.0},
            fallback_active=False,
            error=None,
            cache={"ok": True},
        )
        state = {
            "morning_forecast_date": "2026-09-24",
            "morning_pv_forecast_kwh": 64.4,
            "morning_forecast_hours": {"h10": 20.0},
            "last_weather_fetch": "2026-09-24T09:00:00",
            "pv_forecast_detail": {"hours_today": [{"kwh_adj": 1}]},
            "pv_forecast_today_hours": {"h10": 1.0},
        }
        config = {"lat": 46.7, "lon": 14.0, "has_pv": True, "pv_kwp": 16}
        out = refresh_weather(config, state, force=True)
        self.assertEqual(out["morning_pv_forecast_kwh"], 38.0)
        self.assertEqual(out["morning_forecast_hours"]["h10"], 5.0)
        self.assertEqual(out["pv_yield_today_kwh"], 38.0)

    @patch("control_algorithm.service.fetch_pv_forecast")
    @patch("control_algorithm.service.datetime")
    def test_fallback_does_not_overwrite_reference(self, mock_dt, mock_fetch):
        mock_dt.now.return_value = datetime(2026, 9, 24, 12, 0, 0)
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_fetch.return_value = ForecastResult(
            today_kwh=64.0,
            tomorrow_kwh=45.0,
            today_slots={},
            today_hours={"h10": 64.0},
            fallback_active=True,
            error="cache",
            cache={},
        )
        state = {
            "morning_forecast_date": "2026-09-24",
            "morning_pv_forecast_kwh": 38.0,
            "morning_forecast_hours": {"h10": 5.0},
            "last_weather_fetch": "2026-09-24T09:00:00",
            "pv_forecast_detail": {"hours_today": [{"kwh_adj": 1}]},
            "pv_forecast_today_hours": {"h10": 1.0},
        }
        config = {"lat": 46.7, "lon": 14.0, "has_pv": True, "pv_kwp": 16}
        out = refresh_weather(config, state, force=True)
        self.assertEqual(out["morning_pv_forecast_kwh"], 38.0)
        self.assertTrue(str(out["daemon_status"]).startswith("fallback:"))


if __name__ == "__main__":
    unittest.main()
