# -*- coding: utf-8 -*-
"""Tests Referenz-Anker (24 h)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from control_algorithm.calibration import maybe_reset_on_config_change
from control_algorithm.config import config_fingerprint
from control_algorithm.reference_anchor import (
    activate_reference_anchor,
    is_reference_anchor_active,
    maybe_apply_pv_reference_change,
    pv_reference_fingerprint,
)
from control_algorithm.slot_profiles import forecast_slots, perfect_day_kwh


class TestReferenceAnchor(unittest.TestCase):
    def test_activate_on_pv_change(self):
        config = {"has_pv": True, "pv_kwp": 190, "perfect_weather_sun_hours": 6}
        state = {"pv_reference_fingerprint": pv_reference_fingerprint({"has_pv": True, "pv_kwp": 100, "perfect_weather_sun_hours": 6})}
        state, activated = maybe_apply_pv_reference_change(state, config)
        self.assertTrue(activated)
        self.assertEqual(state["reference_anchor_kwh"], 1140.0)

    def test_no_activate_on_first_fingerprint(self):
        config = {"has_pv": True, "pv_kwp": 190, "perfect_weather_sun_hours": 6}
        state = {}
        state, activated = maybe_apply_pv_reference_change(state, config)
        self.assertFalse(activated)

    def test_anchor_forecast_uses_fixed_perfect(self):
        tomorrow = (datetime.now().date() + timedelta(days=1))
        hourly = {
            "time": [f"{tomorrow.isoformat()}T{h:02d}:00" for h in range(24)],
            "weather_code": [0] * 24,
            "cloud_cover": [0] * 24,
            "shortwave_radiation": [700.0] * 24,
            "sunshine_duration": [3600.0] * 24,
        }
        slots, total = forecast_slots(
            190,
            {"hourly": hourly},
            {"morning": 0.5, "midday": 2.0},
            tomorrow,
            sun_hours=6,
            perfect_day_override=1140.0,
            anchor_forecast_mode=True,
        )
        self.assertAlmostEqual(total, 1140.0, delta=5.0)

    def test_reset_keeps_slot_factors_on_pv_only_change(self):
        config = {
            "has_pv": True,
            "pv_kwp": 200,
            "perfect_weather_sun_hours": 6,
            "city": "X",
            "has_storage": True,
            "storage_kwh": 1000,
        }
        state = {
            "pv_reference_fingerprint": pv_reference_fingerprint(
                {"has_pv": True, "pv_kwp": 190, "perfect_weather_sun_hours": 6}
            ),
            "config_fingerprint": config_fingerprint(config),
            "slot_factors": {"morning": 1.5, "midday": 0.8, "afternoon": 0.8, "evening": 0.8},
        }
        state, anchor = maybe_reset_on_config_change(state, config)
        self.assertTrue(anchor)
        self.assertEqual(state["slot_factors"]["morning"], 1.5)


if __name__ == "__main__":
    unittest.main()
