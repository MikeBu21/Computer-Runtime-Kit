# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from control_algorithm.maintenance import (
    check_calibration,
    check_heartbeat,
    is_calibration_due,
    should_run_nightly_recovery,
)
from control_algorithm.service import maybe_run_calibration


class TestCalibrationDue(unittest.TestCase):
    def test_not_before_window(self):
        now = datetime(2026, 9, 1, 22, 59, 0)
        self.assertFalse(is_calibration_due(now, 23, 0, None))

    def test_due_after_window_without_key(self):
        now = datetime(2026, 9, 1, 23, 11, 0)
        self.assertTrue(is_calibration_due(now, 23, 0, None))

    def test_not_due_if_already_ran(self):
        now = datetime(2026, 9, 1, 23, 11, 0)
        self.assertFalse(is_calibration_due(now, 23, 0, "cal_2026-09-01"))


class TestMaybeRunCalibrationWindow(unittest.TestCase):
    """Abend-Lernen muss auch bei 10‑Min-Tick nach 23:00 greifen (nicht nur :00)."""

    def test_runs_at_23_11_not_only_exact_minute(self):
        config = {"calibration_hour": 23, "calibration_minute": 0}
        state: dict = {"last_calibration_run_key": None}
        now = datetime(2026, 9, 1, 23, 11, 0)

        with patch(
            "control_algorithm.service.run_evening_calibration",
            side_effect=lambda s, c, b: {**s, "last_calibration_note": "ok"},
        ) as cal, patch(
            "control_algorithm.steering_learning.learning_blocked_by_pv_outage",
            return_value=(False, ""),
        ), patch(
            "control_algorithm.steering_learning.run_evening_steering_learning",
            side_effect=lambda s, c, b: s,
        ), patch(
            "control_algorithm.pv_pattern_estimate.run_evening_forecast_pace_learning",
            side_effect=lambda s: s,
        ), patch(
            "control_algorithm.soc_calibration.run_soc_calibration",
            side_effect=lambda s, c, b: s,
        ):
            out = maybe_run_calibration(config, state, now=now)

        cal.assert_called_once()
        self.assertEqual(out.get("last_calibration_run_key"), "cal_2026-09-01")

    def test_skips_before_window(self):
        config = {"calibration_hour": 23, "calibration_minute": 0}
        state = {"last_calibration_run_key": None}
        now = datetime(2026, 9, 1, 22, 50, 0)
        with patch(
            "control_algorithm.service.run_evening_calibration"
        ) as cal:
            out = maybe_run_calibration(config, state, now=now)
        cal.assert_not_called()
        self.assertIsNone(out.get("last_calibration_run_key"))

    def test_skips_if_already_ran_today(self):
        config = {"calibration_hour": 23, "calibration_minute": 0}
        state = {"last_calibration_run_key": "cal_2026-09-01"}
        now = datetime(2026, 9, 1, 23, 40, 0)
        with patch(
            "control_algorithm.service.run_evening_calibration"
        ) as cal:
            out = maybe_run_calibration(config, state, now=now)
        cal.assert_not_called()
        self.assertEqual(out.get("last_calibration_run_key"), "cal_2026-09-01")


class TestMaintenanceChecks(unittest.TestCase):
    def test_heartbeat_stale(self):
        now = datetime(2026, 9, 1, 20, 0, 0)
        hb = {"ts": "2026-09-01T19:50:00", "last_tick": "2026-09-01T19:50:00"}
        result = check_heartbeat(hb, now=now, max_age_sec=180, max_tick_age_sec=900)
        self.assertFalse(result["ok"])
        self.assertIn("heartbeat_stale_600s", result["issues"])

    def test_calibration_missed_after_verify_time(self):
        now = datetime(2026, 9, 1, 23, 40, 0)
        config = {"calibration_hour": 23, "calibration_minute": 0}
        state = {"last_calibration_run_key": "cal_2026-08-31"}
        result = check_calibration(config, state, now=now)
        self.assertTrue(result["missed_today"])
        self.assertFalse(result["ok"])

    def test_heartbeat_stale_ignored_while_process_running_and_tick_fresh(self):
        now = datetime(2026, 9, 1, 20, 0, 0)
        hb = {"ts": "2026-09-01T19:56:00", "last_tick": "2026-09-01T19:55:00"}
        result = check_heartbeat(
            hb, now=now, max_age_sec=180, max_tick_age_sec=900, process_running=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["issues"], [])

    def test_heartbeat_stale_flags_when_process_dead(self):
        now = datetime(2026, 9, 1, 20, 0, 0)
        hb = {"ts": "2026-09-01T19:56:00", "last_tick": "2026-09-01T19:55:00"}
        result = check_heartbeat(
            hb, now=now, max_age_sec=180, max_tick_age_sec=900, process_running=False
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any(i.startswith("heartbeat_stale_") for i in result["issues"]))

    def test_last_tick_stale_restarts_even_if_process_running(self):
        now = datetime(2026, 9, 1, 20, 0, 0)
        hb = {"ts": "2026-09-01T19:59:00", "last_tick": "2026-09-01T19:40:00"}
        result = check_heartbeat(
            hb, now=now, max_age_sec=180, max_tick_age_sec=900, process_running=True
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any(i.startswith("last_tick_stale_") for i in result["issues"]))

    def test_nightly_recovery_window(self):
        report = {"ok": False, "issues": ["heartbeat_missing"]}
        inside = datetime(2026, 9, 2, 0, 7, 0)
        outside = datetime(2026, 9, 2, 1, 0, 0)
        self.assertTrue(should_run_nightly_recovery(report, now=inside))
        self.assertFalse(should_run_nightly_recovery(report, now=outside))


if __name__ == "__main__":
    unittest.main()
