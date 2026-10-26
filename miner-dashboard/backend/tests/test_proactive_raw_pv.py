# -*- coding: utf-8 -*-
"""Vorentladen trotz PR-Delle bei starker Roh-PV."""
from __future__ import annotations

import unittest
from datetime import datetime

from control_algorithm.energy_balance import (
    _effective_tomorrow_pv_for_discharge,
    _proactive_discharge_kw,
)


class TestEffectiveTomorrowPvForDischarge(unittest.TestCase):
    def test_raw_strong_overrides_depressed_pr(self):
        state = {
            "pv_yield_tomorrow_kwh": 624.0,  # PR 0.73 → unter 0.55*1140
            "pv_forecast_detail": {"tomorrow_raw_kwh": 1016.0},
        }
        eff = _effective_tomorrow_pv_for_discharge(state, perfect_day_kwh=1140.0)
        self.assertAlmostEqual(eff, 1016.0 * 0.85, places=1)

    def test_weak_raw_keeps_adj(self):
        state = {
            "pv_yield_tomorrow_kwh": 300.0,
            "pv_forecast_detail": {"tomorrow_raw_kwh": 320.0},
        }
        eff = _effective_tomorrow_pv_for_discharge(state, perfect_day_kwh=1140.0)
        self.assertAlmostEqual(eff, 300.0, places=1)


class TestProactiveWithDepressedPr(unittest.TestCase):
    def test_proactive_on_when_raw_full_sun_but_pr_low(self):
        """Regression: PR 0.73 → 625 kWh killte Vorentladen trotz Roh ~1016."""
        config = {
            "min_soc_percent": 46.0,
            "morning_soc_percent": 46.0,
            "max_soc_percent": 86.0,
            "storage_kwh": 1100.0,
            "has_storage": True,
            "soc_targets": [{"time": "06:00", "soc_percent": 46.0}],
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": 6},
                {"ip": "10.0.0.4", "enabled": True, "power_kw": 5.5, "priority": 5},
                {"ip": "10.0.0.5", "enabled": True, "power_kw": 3.2, "priority": 1.5},
                {"ip": "10.0.0.6", "enabled": True, "power_kw": 3.2, "priority": 1},
                {"ip": "10.0.0.7", "enabled": True, "power_kw": 3.2, "priority": 0.5},
            ],
        }
        state = {
            "pv_yield_tomorrow_kwh": 624.53,
            "pv_yield_today_kwh": 0.0,
            "pv_forecast_detail": {
                "tomorrow_raw_kwh": 1016.34,
                "today_raw_kwh": 1012.0,
                "performance_ratio": 0.727,
            },
            "reference_anchor_kwh": 1140.0,
            "steering_learning": {
                "params": {
                    "headroom_extra_soc": 2.5,
                    "forecast_underestimate_boost": 0.0,
                    "top_prio_hold_below_max": 8.0,
                }
            },
        }
        live = {"soc_percent": 74.0, "pv_kw": 0.0, "verbrauch_kw": 5.5}
        now = datetime(2026, 8, 13, 4, 50, 0)
        kw = _proactive_discharge_kw(config, state, live, now)
        self.assertIsNotNone(kw)
        self.assertGreaterEqual(float(kw), 20.0)


if __name__ == "__main__":
    unittest.main()
