# -*- coding: utf-8 -*-
"""Tests: Unterverbrauch-Anomalie + Verbrauchs-Musterschätzer."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from control_algorithm.anomaly_detection import check_anomaly
from control_algorithm.consumption_pattern_estimate import (
    apply_intraday_consumption_override,
    consumption_pattern_scale,
    estimate_today_consumption_from_patterns,
    get_pattern_params,
)


class TestAnomalyLow(unittest.TestCase):
    def test_low_anomaly_resumes_device(self):
        config = {
            "anomaly_threshold_percent": 20.0,
            "anomaly_low_enabled": True,
            "anomaly_low_threshold_percent": 20.0,
            "anomaly_window_hours": 1.5,
            "anomaly_min_history_days": 7,
            "anomaly_pause_interval_minutes": 15.0,
            "anomaly_low_resume_interval_minutes": 15.0,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 3.0, "priority": 8},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 3.0, "priority": 5},
            ],
        }
        state = {
            "anomaly_active": False,
            "anomaly_low_active": False,
        }
        live = {
            "miner_status": {
                "10.0.0.1": "paused",
                "10.0.0.2": "paused",
            }
        }
        now = datetime(2026, 8, 9, 12, 0, 0)
        with patch(
            "control_algorithm.anomaly_detection.archived_history_days",
            return_value=14,
        ), patch(
            "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
            return_value=(4.0, 10.0),  # 60 % unter Erwartung
        ):
            state, actions, messages = check_anomaly(
                config, state, live, "/tmp", now=now
            )
        self.assertTrue(state.get("anomaly_low_active"))
        self.assertGreaterEqual(float(state.get("anomaly_deficit_pct") or 0), 50)
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a["action"] == "resume" for a in actions))
        self.assertTrue(all(a.get("force") and a.get("emergency") for a in actions))
        self.assertEqual(actions[0]["ip"], "10.0.0.1")  # höhere Prio zuerst
        self.assertIn("unter historischem Schnitt", actions[0]["reason"])

    def test_high_anomaly_wins_over_low(self):
        config = {
            "anomaly_threshold_percent": 20.0,
            "anomaly_low_enabled": True,
            "anomaly_low_threshold_percent": 20.0,
            "anomaly_window_hours": 1.5,
            "anomaly_min_history_days": 7,
            "anomaly_pause_interval_minutes": 15.0,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 3.0, "priority": 5},
            ],
        }
        state = {"anomaly_low_active": True, "anomaly_deficit_pct": 30}
        live = {"miner_status": {"10.0.0.1": "running"}}
        now = datetime(2026, 8, 9, 12, 0, 0)
        with patch(
            "control_algorithm.anomaly_detection.archived_history_days",
            return_value=14,
        ), patch(
            "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
            return_value=(15.0, 10.0),  # +50 %
        ):
            state, actions, messages = check_anomaly(
                config, state, live, "/tmp", now=now
            )
        self.assertTrue(state.get("anomaly_active"))
        self.assertFalse(state.get("anomaly_low_active"))
        self.assertEqual(actions[0]["action"], "pause")

    def test_low_disabled_no_resume(self):
        config = {
            "anomaly_threshold_percent": 20.0,
            "anomaly_low_enabled": False,
            "anomaly_low_threshold_percent": 20.0,
            "anomaly_window_hours": 1.5,
            "anomaly_min_history_days": 7,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 3.0, "priority": 5},
            ],
        }
        state = {}
        live = {"miner_status": {"10.0.0.1": "paused"}}
        now = datetime(2026, 8, 9, 12, 0, 0)
        with patch(
            "control_algorithm.anomaly_detection.archived_history_days",
            return_value=14,
        ), patch(
            "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
            return_value=(4.0, 10.0),
        ):
            state, actions, messages = check_anomaly(
                config, state, live, "/tmp", now=now
            )
        self.assertFalse(state.get("anomaly_low_active"))
        self.assertEqual(actions, [])

    def test_morning_floor_blocks_low_anomaly_resume(self):
        config = {
            "anomaly_threshold_percent": 20.0,
            "anomaly_low_enabled": True,
            "anomaly_low_threshold_percent": 20.0,
            "anomaly_window_hours": 1.5,
            "anomaly_min_history_days": 7,
            "anomaly_low_resume_interval_minutes": 15.0,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 3.0, "priority": 8},
            ],
        }
        state = {
            "anomaly_active": False,
            "anomaly_low_active": False,
            "morning_floor_threatened": True,
        }
        live = {"miner_status": {"10.0.0.1": "paused"}}
        now = datetime(2026, 8, 9, 21, 0, 0)
        with patch(
            "control_algorithm.anomaly_detection.archived_history_days",
            return_value=14,
        ), patch(
            "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
            return_value=(4.0, 10.0),
        ):
            state, actions, messages = check_anomaly(
                config, state, live, "/tmp", now=now
            )
        self.assertTrue(state.get("anomaly_low_active"))
        self.assertEqual(actions, [])
        self.assertTrue(any("Morgen-Floor" in m for m in messages))


class TestConsumptionPattern(unittest.TestCase):
    def test_params_from_config(self):
        p = get_pattern_params(
            {
                "consumption_pattern_engage_pct": 18,
                "consumption_pattern_release_pct": 12,
                "consumption_pattern_trust": 0.8,
            }
        )
        self.assertEqual(p["engage_pct"], 18.0)
        self.assertEqual(p["release_pct"], 12.0)
        self.assertAlmostEqual(p["pattern_trust"], 0.8)

    def test_under_pace_engages_scale_below_one(self):
        state = {
            "consumption_pattern_estimate": {
                "estimated_kwh": 80.0,
                "forecast_kwh": 100.0,
            }
        }
        config = {
            "consumption_pattern_enabled": True,
            "consumption_pattern_engage_pct": 15.0,
            "consumption_pattern_release_pct": 10.0,
            "consumption_pattern_negative_cap_pct": 25.0,
            "consumption_pattern_positive_cap_pct": 15.0,
            "consumption_pattern_min_hour": 8,
            "consumption_pattern_min_expected_so_far_kwh": 8.0,
            "consumption_pattern_trust": 0.7,
        }
        now = datetime(2026, 8, 9, 14, 0, 0)
        with patch(
            "control_algorithm.consumption_pattern_estimate.expected_base_consumption_kwh",
            return_value=20.0,
        ), patch(
            "control_algorithm.consumption_pattern_estimate.actual_base_consumption_kwh",
            return_value=14.0,  # −30 %
        ):
            apply_intraday_consumption_override(state, config, "/tmp", now=now)
        ov = state["consumption_pattern_override"]
        self.assertTrue(ov["active"])
        self.assertLess(ov["effective_scale"], 1.0)
        self.assertGreaterEqual(ov["effective_scale"], 0.75)
        self.assertLess(consumption_pattern_scale(state, config), 1.0)

    def test_estimate_underconsumption(self):
        now = datetime(2026, 8, 9, 14, 0, 0)
        with patch(
            "control_algorithm.consumption_pattern_estimate.expected_base_full_day_kwh",
            return_value=100.0,
        ), patch(
            "control_algorithm.consumption_pattern_estimate.expected_base_consumption_kwh",
            return_value=40.0,
        ), patch(
            "control_algorithm.consumption_pattern_estimate.actual_base_consumption_kwh",
            return_value=28.0,
        ):
            est = estimate_today_consumption_from_patterns(
                "/tmp", now=now, forecast_base_kwh=100.0
            )
        self.assertIsNotNone(est["estimated_kwh"])
        self.assertLess(est["estimated_kwh"], 100.0)
        self.assertLess(est["pace_ratio"], 0.95)
        self.assertLess(est["delta_vs_forecast_pct"], -5)


if __name__ == "__main__":
    unittest.main()
