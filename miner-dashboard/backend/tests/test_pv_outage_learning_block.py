# -*- coding: utf-8 -*-
"""Globaler Lern-Sperrschalter bei PV-Ausfall / Produktionsanomalie."""
from __future__ import annotations

import unittest
from datetime import date

from control_algorithm.steering_learning import (
    ensure_steering_learning,
    get_steering_params,
    learning_blocked_by_pv_outage,
    mark_pv_outage_learning_block,
    run_evening_steering_learning,
)


def _cfg(**kwargs):
    base = {
        "has_pv": True,
        "steering_learning_block_on_pv_outage": True,
        "max_soc_percent": 95.0,
        "min_soc_percent": 30.0,
        "devices": [],
    }
    base.update(kwargs)
    return base


class TestPvOutageLearningBlock(unittest.TestCase):
    def test_mark_pv_safety_blocks_learning(self):
        state: dict = {}
        ensure_steering_learning(state)
        mark_pv_outage_learning_block(state, reason="pv_safety Pace -80 %")
        blocked, reason = learning_blocked_by_pv_outage(_cfg(), state)
        self.assertTrue(blocked)
        self.assertIn("PV-Ausfall", reason)

    def test_severe_forecast_miss_blocks_without_safety_flag(self):
        state = {
            "morning_pv_forecast_kwh": 60.0,
            "live_pv_kwh_actual": 2.0,
        }
        ensure_steering_learning(state)
        blocked, reason = learning_blocked_by_pv_outage(_cfg(), state)
        self.assertTrue(blocked)
        self.assertTrue("2.0" in reason or "%" in reason)

    def test_normal_day_does_not_block(self):
        state = {
            "morning_pv_forecast_kwh": 50.0,
            "live_pv_kwh_actual": 48.0,
        }
        ensure_steering_learning(state)
        blocked, _ = learning_blocked_by_pv_outage(_cfg(), state)
        self.assertFalse(blocked)

    def test_config_can_disable_block(self):
        state = {
            "morning_pv_forecast_kwh": 60.0,
            "live_pv_kwh_actual": 1.0,
        }
        ensure_steering_learning(state)
        blocked, _ = learning_blocked_by_pv_outage(
            _cfg(steering_learning_block_on_pv_outage=False), state
        )
        self.assertFalse(blocked)

    def test_evening_learning_skips_and_keeps_params(self):
        state: dict = {
            "morning_pv_forecast_kwh": 55.0,
            "live_pv_kwh_actual": 1.5,
        }
        ensure_steering_learning(state)
        state["steering_learning"]["params"]["top_prio_extra_runtime_h"] = 2.5

        state = run_evening_steering_learning(state, _cfg(), "/tmp")
        after = get_steering_params(state)
        self.assertEqual(after["top_prio_extra_runtime_h"], 2.5)
        self.assertEqual(state["steering_learning"]["last_note"], "skipped_pv_outage")
        hist = state["steering_learning"]["history"]
        self.assertTrue(hist and hist[-1].get("skipped") is True)
        self.assertEqual(hist[-1].get("date"), date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
