# -*- coding: utf-8 -*-
"""Tests für Speicher-Band-Neuron."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from control_algorithm.steering_learning import ensure_steering_learning, get_steering_params
from control_algorithm.storage_band_learning import (
    band_consumption_plan_scale,
    horizon_stress,
    record_storage_band_tick,
    run_storage_band_evening_learning,
    storage_band_limits,
)


def _cfg() -> dict:
    return {
        "has_storage": True,
        "storage_kwh": 200.0,
        "min_soc_percent": 35.0,
        "morning_soc_percent": 46.0,
        "max_soc_percent": 95.0,
        "evening_soc_percent": 90.0,
        "pv_kwp": 190.0,
    }


class TestStorageBandLearning(unittest.TestCase):
    def test_limits_respect_config_band(self):
        state = {
            "pv_yield_today_kwh": 400,
            "pv_yield_tomorrow_kwh": 350,
            "pv_yield_day_after_kwh": 300,
            "consumption_estimates": {
                "base_only": {"by_day_kwh": [500, 520, 510]},
            },
        }
        ensure_steering_learning(state)
        limits = storage_band_limits(_cfg(), state, live={"soc_percent": 80})
        self.assertGreaterEqual(limits["floor_pct"], 46.0)
        self.assertLessEqual(limits["ceiling_pct"], 95.0)
        self.assertGreater(limits["ceiling_pct"], limits["floor_pct"])

    def test_horizon_overfill_when_pv_exceeds_consumption(self):
        state = {
            "pv_yield_today_kwh": 600,
            "pv_yield_tomorrow_kwh": 600,
            "pv_yield_day_after_kwh": 600,
            "consumption_estimates": {
                "base_only": {"by_day_kwh": [400, 400, 400]},
            },
        }
        stress = horizon_stress(_cfg(), state, live={"soc_percent": 88})
        self.assertGreater(stress["overfill"], 0.2)

    def test_tick_lowers_ceiling_on_high_soc_export(self):
        config = _cfg()
        state = {}
        ensure_steering_learning(state)
        with patch(
            "control_algorithm.weather_survival.live_grid_export_kw",
            return_value=3.0,
        ):
            record_storage_band_tick(
                config,
                state,
                {"soc_percent": 96.0, "export_kw": 3.0},
            )
        p = get_steering_params(state)
        self.assertLess(p["band_max_target_pct"], 90.0)
        self.assertGreater(int(state["steering_learning"]["today"]["band_over_max_ticks"]), 0)

    def test_evening_learning_tightens_on_over_max(self):
        config = _cfg()
        state = {"steering_learning": {"today": {"band_over_max_ticks": 3}}}
        ensure_steering_learning(state)
        params = get_steering_params(state)
        adj = run_storage_band_evening_learning(
            state,
            config,
            {"max_over_minutes": 30, "min_breach_minutes": 0},
            params,
        )
        self.assertTrue(adj)
        self.assertLess(params["band_max_target_pct"], 90.0)

    def test_consumption_scale_rises_on_underfill(self):
        state = {
            "pv_yield_today_kwh": 100,
            "pv_yield_tomorrow_kwh": 80,
            "pv_yield_day_after_kwh": 70,
            "consumption_estimates": {
                "base_only": {"by_day_kwh": [600, 620, 610]},
            },
        }
        ensure_steering_learning(state)
        sl = state["steering_learning"]
        sl["params"]["band_consumption_guard"] = 0.5
        scale = band_consumption_plan_scale(_cfg(), state, live={"soc_percent": 50})
        self.assertGreater(scale, 1.0)


if __name__ == "__main__":
    unittest.main()
