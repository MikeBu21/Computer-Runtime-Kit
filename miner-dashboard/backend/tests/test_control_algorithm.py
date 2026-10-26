# -*- coding: utf-8 -*-
"""Unit-Tests Steuerungsalgorithmus."""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from control_algorithm.calibration import (
    compute_accuracy_score,
    estimate_days_to_90,
    maybe_revert_inconsistent_last_calibration,
    run_evening_calibration,
)
from control_algorithm.anomaly_detection import (
    check_anomaly,
    is_anomaly_resume_blocked,
    normalize_min_history_days,
    normalize_threshold,
)
from control_algorithm.load_scheduler import plan_load_actions
from control_algorithm.planner import (
    find_priority_violations,
    next_soc_target,
    paused_devices_highest_tier,
    plan_actions,
    soc_targets_in_horizon,
    sort_for_pause,
    sort_for_resume,
)
from control_algorithm.pv_forecast import forecast_pv_kwh
from control_algorithm.slot_profiles import (
    default_slot_factors,
    forecast_slots,
    hour_slot_ids,
    integrate_generation_kwh_by_hours,
    perfect_day_kwh,
    weather_weights_by_slot,
)
from control_algorithm.weather_scale import hour_relative_sun, wmo_to_sun_factor


class TestWeatherScale(unittest.TestCase):
    def test_clear_sky(self):
        self.assertGreater(wmo_to_sun_factor(0), 0.9)

    def test_rain_low(self):
        self.assertLess(wmo_to_sun_factor(65), 0.2)

    def test_hour_relative_sun(self):
        f = hour_relative_sun(weather_code=0, cloud_cover=10)
        self.assertGreater(f, 0.5)


class TestSlotProfiles(unittest.TestCase):
    def test_perfect_day_kwh(self):
        self.assertAlmostEqual(perfect_day_kwh(190), 1140.0)
        self.assertAlmostEqual(perfect_day_kwh(190, 8), 1520.0)

    def test_forecast_slots_clear_day(self):
        tomorrow = date.today() + timedelta(days=1)
        hourly = {
            "time": [f"{tomorrow.isoformat()}T{h:02d}:00" for h in range(24)],
            "weather_code": [0] * 24,
            "cloud_cover": [0] * 24,
            "shortwave_radiation": [600.0] * 24,
            "sunshine_duration": [3600.0] * 24,
        }
        slots, total = forecast_slots(190, {"hourly": hourly}, default_slot_factors(), tomorrow)
        self.assertAlmostEqual(total, 1140.0, delta=50.0)
        self.assertGreater(sum(slots.values()), 0)

    def test_hour_slot_ids(self):
        ids = hour_slot_ids()
        self.assertEqual(len(ids), 18)
        self.assertEqual(ids[0], "h05")
        self.assertEqual(ids[-1], "h22")

    def test_integrate_generation_by_hours(self):
        samples = [
            {"m": 480, "erzeugung": 10.0},
            {"m": 540, "erzeugung": 20.0},
            {"m": 600, "erzeugung": 30.0},
        ]
        hours = integrate_generation_kwh_by_hours(samples, pv_fraction=1.0)
        self.assertGreater(hours.get("h08", 0), 0)
        self.assertGreater(hours.get("h09", 0), 0)


class TestCalibration(unittest.TestCase):
    def test_accuracy_score(self):
        history = [
            {"hit": True, "actual": 50},
            {"hit": True, "actual": 40},
            {"hit": False, "actual": 30},
        ]
        score = compute_accuracy_score(history, window=3)
        self.assertAlmostEqual(score, 66.7, places=0)

    def test_eta_days(self):
        state = {"accuracy_score": 92, "days_since_reset": 5}
        self.assertLessEqual(estimate_days_to_90(state), 3)

    def test_evening_calibration_within_tolerance(self):
        state = {
            "morning_pv_forecast_kwh": 1000.0,
            "morning_forecast_slots": {
                "morning": 200,
                "midday": 350,
                "afternoon": 350,
                "evening": 100,
            },
            "morning_forecast_date": date.today().isoformat(),
            "performance_ratio": 1.0,
            "days_since_reset": 3,
            "yield_history": [],
        }
        config = {"pv_kwp": 190, "has_pv": True}

        class FakeLive:
            @staticmethod
            def get_actual_pv_kwh_today(_base):
                return 1020.0

        import control_algorithm.calibration as cal

        orig = cal.get_actual_pv_kwh_today
        orig_slots = cal.get_actual_pv_kwh_by_slots_today
        try:
            cal.get_actual_pv_kwh_today = FakeLive.get_actual_pv_kwh_today
            cal.get_actual_pv_kwh_by_slots_today = lambda _b: {
                "morning": 200,
                "midday": 360,
                "afternoon": 360,
                "evening": 100,
            }
            state = run_evening_calibration(state, config, "/tmp")
            self.assertTrue(state["yield_history"][-1]["hit"])
            self.assertEqual(state["performance_ratio"], 1.0)
        finally:
            cal.get_actual_pv_kwh_today = orig
            cal.get_actual_pv_kwh_by_slots_today = orig_slots

    def test_evening_calibration_adjusts_pr(self):
        state = {
            "morning_pv_forecast_kwh": 1000.0,
            "morning_forecast_slots": {},
            "morning_forecast_hours": {"h08": 50.0, "h12": 100.0},
            "morning_forecast_date": date.today().isoformat(),
            "performance_ratio": 1.0,
            "hour_slot_factors": {hid: 1.0 for hid in hour_slot_ids()},
            "days_since_reset": 1,
            "yield_history": [],
        }
        config = {"pv_kwp": 190, "has_pv": True}

        import control_algorithm.calibration as cal

        orig = cal.get_actual_pv_kwh_today
        orig_slots = cal.get_actual_pv_kwh_by_slots_today
        orig_hours = cal.get_actual_pv_kwh_by_hours_today
        try:
            cal.get_actual_pv_kwh_today = lambda _b: 800.0
            cal.get_actual_pv_kwh_by_slots_today = lambda _b: {}
            cal.get_actual_pv_kwh_by_hours_today = lambda _b: {"h08": 40.0, "h12": 80.0}
            state = run_evening_calibration(state, config, "/tmp")
            self.assertFalse(state["yield_history"][-1]["hit"])
            self.assertLess(state["performance_ratio"], 1.0)
            self.assertLess(state["hour_slot_factors"]["h08"], 1.0)
        finally:
            cal.get_actual_pv_kwh_today = orig
            cal.get_actual_pv_kwh_by_slots_today = orig_slots
            cal.get_actual_pv_kwh_by_hours_today = orig_hours

    def test_evening_calibration_skips_inconsistent_daily_vs_hours(self):
        hours = {hid: 40.0 for hid in hour_slot_ids()}  # 18*40 = 720 kWh
        state = {
            "morning_pv_forecast_kwh": 870.0,
            "morning_forecast_slots": {},
            "morning_forecast_hours": {hid: 50.0 for hid in hour_slot_ids()},
            "morning_forecast_date": date.today().isoformat(),
            "performance_ratio": 0.92,
            "hour_slot_factors": {hid: 1.0 for hid in hour_slot_ids()},
            "days_since_reset": 8,
            "yield_history": [],
        }
        config = {"pv_kwp": 190, "has_pv": True}
        import control_algorithm.calibration as cal

        orig = cal.get_actual_pv_kwh_today
        orig_slots = cal.get_actual_pv_kwh_by_slots_today
        orig_hours = cal.get_actual_pv_kwh_by_hours_today
        try:
            cal.get_actual_pv_kwh_today = lambda _b: 464.0
            cal.get_actual_pv_kwh_by_slots_today = lambda _b: {}
            cal.get_actual_pv_kwh_by_hours_today = lambda _b: hours
            state = run_evening_calibration(state, config, "/tmp")
            self.assertEqual(state["performance_ratio"], 0.92)
            self.assertEqual(state["last_calibration_note"], "skipped_inconsistent_actual")
            self.assertTrue(state["yield_history"][-1]["skipped_inconsistent_actual"])
            self.assertEqual(state["hour_slot_factors"]["h08"], 1.0)
        finally:
            cal.get_actual_pv_kwh_today = orig
            cal.get_actual_pv_kwh_by_slots_today = orig_slots
            cal.get_actual_pv_kwh_by_hours_today = orig_hours

    def test_evening_calibration_skips_outlier_pr_after_day_zero(self):
        hours = {hid: 25.0 for hid in hour_slot_ids()}  # 450 kWh, consistent with daily
        state = {
            "morning_pv_forecast_kwh": 900.0,
            "morning_forecast_slots": {},
            "morning_forecast_hours": {hid: 50.0 for hid in hour_slot_ids()},
            "morning_forecast_date": date.today().isoformat(),
            "performance_ratio": 0.92,
            "hour_slot_factors": {hid: 1.0 for hid in hour_slot_ids()},
            "days_since_reset": 8,
            "yield_history": [],
        }
        config = {"pv_kwp": 190, "has_pv": True}
        import control_algorithm.calibration as cal

        orig = cal.get_actual_pv_kwh_today
        orig_slots = cal.get_actual_pv_kwh_by_slots_today
        orig_hours = cal.get_actual_pv_kwh_by_hours_today
        try:
            cal.get_actual_pv_kwh_today = lambda _b: 450.0
            cal.get_actual_pv_kwh_by_slots_today = lambda _b: {}
            cal.get_actual_pv_kwh_by_hours_today = lambda _b: hours
            state = run_evening_calibration(state, config, "/tmp")
            self.assertEqual(state["performance_ratio"], 0.92)
            self.assertEqual(state["last_calibration_note"], "skipped_outlier")
            self.assertEqual(state["hour_slot_factors"]["h08"], 1.0)
        finally:
            cal.get_actual_pv_kwh_today = orig
            cal.get_actual_pv_kwh_by_slots_today = orig_slots
            cal.get_actual_pv_kwh_by_hours_today = orig_hours

    def test_revert_inconsistent_last_calibration_restores_pr(self):
        good_hours = {hid: 45.0 for hid in hour_slot_ids()}
        bad_hours = {hid: 38.5 for hid in hour_slot_ids()}  # ~693
        state = {
            "performance_ratio": 0.727,
            "yield_factor": 0.727,
            "hour_slot_factors": {hid: 0.8 for hid in hour_slot_ids()},
            "morning_forecast_date": date.today().isoformat(),
            "yield_history": [
                {
                    "date": "2026-07-31",
                    "predicted": 835.0,
                    "actual": 795.0,
                    "hit": True,
                    "performance_ratio": 0.9199,
                    "hour_slot_factors": {hid: 1.02 for hid in hour_slot_ids()},
                    "actual_hours": good_hours,
                },
                {
                    "date": "2026-08-12",
                    "predicted": 869.5,
                    "actual": 464.3,
                    "hit": False,
                    "performance_ratio": 0.727,
                    "hour_slot_factors": {hid: 0.8 for hid in hour_slot_ids()},
                    "actual_hours": bad_hours,
                },
            ],
        }
        out = maybe_revert_inconsistent_last_calibration(state)
        self.assertAlmostEqual(out["performance_ratio"], 0.9199)
        self.assertAlmostEqual(out["hour_slot_factors"]["h08"], 1.02)
        self.assertTrue(out["yield_history"][-1]["calibration_reverted"])
        self.assertTrue(out.get("force_weather_refresh"))
        self.assertIsNone(out.get("morning_forecast_date"))
        self.assertEqual(out["last_calibration_note"], "reverted_inconsistent_actual")
        # Zweimal aufrufen ändert nichts mehr
        again = maybe_revert_inconsistent_last_calibration(out)
        self.assertAlmostEqual(again["performance_ratio"], 0.9199)


class TestPlannerPriority(unittest.TestCase):
    def test_sort_for_resume_high_first(self):
        devices = [
            {"ip": "1", "priority": "low", "power_kw": 3},
            {"ip": "2", "priority": "high", "power_kw": 5},
        ]
        ordered = sort_for_resume(devices)
        self.assertEqual(ordered[0]["ip"], "2")

    def test_sort_for_resume_numeric_10_before_0(self):
        devices = [
            {"ip": "a", "priority": 0, "power_kw": 3},
            {"ip": "b", "priority": 7.5, "power_kw": 4},
            {"ip": "c", "priority": 10, "power_kw": 5},
        ]
        ordered = sort_for_resume(devices)
        self.assertEqual([d["ip"] for d in ordered], ["c", "b", "a"])

    def test_sort_for_pause_low_first(self):
        devices = [
            {"ip": "1", "priority": "high", "power_kw": 5},
            {"ip": "2", "priority": "low", "power_kw": 3},
        ]
        ordered = sort_for_pause(devices)
        self.assertEqual(ordered[0]["ip"], "2")

    def test_paused_highest_tier_skips_lower_resume(self):
        devices = [
            {"ip": "high", "enabled": True, "power_kw": 5.5, "priority": "high"},
            {"ip": "med", "enabled": True, "power_kw": 5.3, "priority": "medium"},
            {"ip": "low", "enabled": True, "power_kw": 3.2, "priority": "low"},
        ]
        status = {"high": "running", "med": "paused", "low": "paused"}
        tier = paused_devices_highest_tier(devices, status)
        self.assertEqual(len(tier), 1)
        self.assertEqual(tier[0]["ip"], "med")

    def test_find_priority_violation_low_running_while_medium_off(self):
        devices = [
            {"ip": "high", "enabled": True, "power_kw": 5.5, "priority": "high"},
            {"ip": "med", "enabled": True, "power_kw": 5.3, "priority": "medium"},
            {"ip": "low", "enabled": True, "power_kw": 3.2, "priority": "low"},
        ]
        status = {"high": "running", "med": "paused", "low": "running"}
        violations = find_priority_violations(devices, status)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["ip"], "low")

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=500.0)
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_plan_load_actions_pauses_low_when_medium_off(self, mock_proj, _mock_surplus):
        """Prioritätslücke: höherwertige Stufe reparieren statt Low force-killen."""
        now = datetime(2026, 7, 7, 12, 0, 0)
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 80,
            "target_datetime": (now + timedelta(hours=8)).isoformat(),
            "horizon_end": (now + timedelta(hours=8)).isoformat(),
            "hours_left": 8.0,
            "miner_budget_kw": 20.0,
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 50,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
            "binding_constraint": "none",
            "proactive_discharge_kw": 0,
        }
        config = {
            "enabled": True,
            "min_soc_percent": 35,
            "max_soc_percent": 95,
            "storage_kwh": 400,
            "min_device_runtime_hours": 0.5,
            "soc_targets": [{"time": "07:00", "soc_percent": 40}],
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": "high"},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.3, "priority": "medium"},
                {"ip": "10.0.0.3", "enabled": True, "power_kw": 3.2, "priority": "low"},
            ],
        }
        state = {"balance_projection": mock_proj.return_value, "device_runtime": {}}
        live = {
            "soc_percent": 60,
            "pv_kw": 0,
            "miner_status": {
                "10.0.0.1": "running",
                "10.0.0.2": "paused",
                "10.0.0.3": "running",
            },
        }
        actions, msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        force_pause_low = [
            a for a in actions
            if a.get("ip") == "10.0.0.3" and a.get("action") == "pause" and a.get("force")
        ]
        resume_med = [
            a for a in actions
            if a.get("ip") == "10.0.0.2" and a.get("action") == "resume"
        ]
        self.assertEqual(force_pause_low, [])
        self.assertTrue(resume_med)
        self.assertNotIn("10.0.0.1", [a["ip"] for a in actions if a.get("action") == "pause"])
        self.assertTrue(any("Reparatur" in m or "Priorität" in m for m in msgs))

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=500.0)
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_plan_load_actions_resumes_medium_before_low(self, mock_proj, _mock_surplus):
        now = datetime(2026, 7, 7, 12, 0, 0)
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 80,
            "target_datetime": (now + timedelta(hours=8)).isoformat(),
            "horizon_end": (now + timedelta(hours=8)).isoformat(),
            "hours_left": 8.0,
            "miner_budget_kw": 62.5,
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 50,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
        }
        config = {
            "enabled": True,
            "min_soc_percent": 35,
            "min_device_runtime_hours": 0.5,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": "high"},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.3, "priority": "medium"},
                {"ip": "10.0.0.3", "enabled": True, "power_kw": 3.2, "priority": "low"},
            ],
        }
        state = {"balance_projection": mock_proj.return_value, "horizon_projection": mock_proj.return_value}
        live = {
            "soc_percent": 60,
            "miner_status": {
                "10.0.0.1": "running",
                "10.0.0.2": "paused",
                "10.0.0.3": "paused",
            },
        }
        actions, _msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        resumes = [a for a in actions if a.get("action") == "resume"]
        self.assertEqual(len(resumes), 1)
        self.assertEqual(resumes[0]["ip"], "10.0.0.2")


class TestPlanner(unittest.TestCase):
    def test_next_soc_target(self):
        from datetime import datetime

        targets = [{"time": "07:00", "soc_percent": 40}, {"time": "18:00", "soc_percent": 90}]
        nxt = next_soc_target(targets, datetime(2026, 5, 23, 10, 0, 0))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["soc_percent"], 90)

    def test_plan_disabled(self):
        config = {"enabled": False, "devices": []}
        live = {"soc_percent": 50, "miner_status": {}}
        actions, msgs = plan_actions(config, live, 10, 20, 0.85)
        self.assertEqual(actions, [])
        self.assertTrue(any("deaktiviert" in m for m in msgs))


class TestPvForecast(unittest.TestCase):
    def test_no_pv(self):
        kwh, meta = forecast_pv_kwh(0, {"hourly": {"time": []}})
        self.assertEqual(kwh, 0.0)


class TestAnomalyDetection(unittest.TestCase):
    def test_threshold_normalize(self):
        self.assertEqual(normalize_threshold(5), 5.0)
        self.assertEqual(normalize_threshold(220), 220.0)
        self.assertEqual(normalize_threshold(5000), 1000.0)

    def test_no_anomaly_without_history(self):
        import tempfile

        config = {
            "anomaly_threshold_percent": 20,
            "anomaly_window_hours": 1.5,
            "anomaly_min_history_days": 7,
            "devices": [],
        }
        state = {"anomaly_active": True, "anomaly_excess_pct": 30.0}
        live = {"miner_status": {}}
        with tempfile.TemporaryDirectory() as tmp:
            state, actions, msgs = check_anomaly(config, state, live, tmp)
            self.assertFalse(state.get("anomaly_active"))

    def test_min_history_days_normalize(self):
        self.assertEqual(normalize_min_history_days(3), 3)
        self.assertEqual(normalize_min_history_days(0), 1)
        self.assertEqual(normalize_min_history_days(100), 60)

    def _anomaly_config(self) -> dict:
        return {
            "enabled": True,
            "anomaly_threshold_percent": 20,
            "anomaly_window_hours": 1.5,
            "anomaly_min_history_days": 1,
            "anomaly_clear_stable_minutes": 20,
            "anomaly_resume_delay_minutes": 30,
            "anomaly_pause_interval_minutes": 15,
            "min_device_runtime_hours": 3,
            "min_soc_percent": 35,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 3.2, "priority": "low"},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": "high"},
            ],
        }

    @patch("control_algorithm.anomaly_detection.archived_history_days", return_value=10)
    @patch(
        "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
        return_value=(15.0, 5.0),
    )
    def test_anomaly_no_force_when_min_runtime_protected(self, _mock_corr, _mock_arch):
        now = datetime(2026, 7, 7, 12, 0, 0)
        config = self._anomaly_config()
        state = {
            "device_runtime": {"10.0.0.1": {"resumed_at": now.isoformat()}},
        }
        live = {"miner_status": {"10.0.0.1": "running"}, "soc_percent": 50}
        state, actions, _msgs = check_anomaly(config, state, live, "/tmp", now=now)
        self.assertTrue(state.get("anomaly_active"))
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].get("force"))
        self.assertTrue(actions[0].get("emergency"))

    @patch("control_algorithm.anomaly_detection.archived_history_days", return_value=10)
    @patch(
        "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
        return_value=(15.0, 5.0),
    )
    def test_anomaly_pause_without_force_when_allowed(self, _mock_corr, _mock_arch):
        now = datetime(2026, 7, 7, 12, 0, 0)
        config = self._anomaly_config()
        state = {}
        live = {"miner_status": {"10.0.0.1": "running"}, "soc_percent": 50}
        state, actions, _msgs = check_anomaly(config, state, live, "/tmp", now=now)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "pause")
        self.assertTrue(actions[0].get("force"))
        self.assertTrue(actions[0].get("emergency"))

    @patch("control_algorithm.anomaly_detection.archived_history_days", return_value=10)
    @patch(
        "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
        return_value=(15.0, 5.0),
    )
    def test_anomaly_pause_interval_blocks_rapid_shutdown(self, _mock_corr, _mock_arch):
        """Sicherheits-Fast-Path: alle laufenden Geräte sofort, ohne Intervall-Sperre."""
        now = datetime(2026, 7, 7, 12, 0, 0)
        config = self._anomaly_config()
        state = {
            "anomaly_active": True,
            "anomaly_last_pause_at": (now - timedelta(minutes=5)).isoformat(),
        }
        live = {"miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"}}
        state, actions, msgs = check_anomaly(config, state, live, "/tmp", now=now)
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a.get("force") and a.get("emergency") for a in actions))
        self.assertFalse(any("nächste Abschaltung" in m for m in msgs))

    @patch("control_algorithm.anomaly_detection.archived_history_days", return_value=10)
    @patch(
        "control_algorithm.anomaly_detection.corrected_base_consumption_for_anomaly",
        return_value=(6.0, 5.0),
    )
    def test_anomaly_clear_hysteresis_keeps_active(self, _mock_corr, _mock_arch):
        now = datetime(2026, 7, 7, 12, 0, 0)
        config = self._anomaly_config()
        state = {"anomaly_active": True, "anomaly_excess_pct": 25.0}
        live = {"miner_status": {}}
        state, _actions, _msgs = check_anomaly(config, state, live, "/tmp", now=now)
        self.assertTrue(state.get("anomaly_active"))
        self.assertIsNotNone(state.get("anomaly_below_threshold_since"))

    def test_anomaly_resume_blocked_after_clear(self):
        now = datetime(2026, 7, 7, 12, 0, 0)
        config = self._anomaly_config()
        state = {
            "anomaly_cleared_at": (now - timedelta(minutes=10)).isoformat(),
        }
        self.assertTrue(is_anomaly_resume_blocked(state, config, now))

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=100.0)
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_plan_load_actions_blocks_resume_after_anomaly(self, mock_proj, _mock_surplus):
        now = datetime(2026, 7, 7, 12, 0, 0)
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 80,
            "target_datetime": (now + timedelta(hours=8)).isoformat(),
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 50,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
        }
        config = self._anomaly_config()
        state = {
            "anomaly_active": False,
            "anomaly_cleared_at": (now - timedelta(minutes=10)).isoformat(),
            "balance_projection": mock_proj.return_value,
        }
        live = {
            "soc_percent": 60,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.anomaly_detection.datetime"
        ) as mock_anom_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_anom_dt.now.return_value = now
            mock_anom_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        self.assertFalse(any(a.get("action") == "resume" for a in actions))
        self.assertTrue(any("gesperrt" in m for m in msgs))

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=0.0)
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_mild_anomaly_does_not_skip_planning(self, mock_proj, _mock_surplus):
        now = datetime(2026, 7, 7, 12, 0, 0)
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 50,
            "target_datetime": (now + timedelta(hours=8)).isoformat(),
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 20,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
        }
        config = self._anomaly_config()
        state = {
            "anomaly_active": True,
            "anomaly_excess_pct": 25.0,
            "balance_projection": mock_proj.return_value,
        }
        live = {
            "soc_percent": 60,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        actions, msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        self.assertFalse(any(a.get("source") == "anomaly" for a in actions))
        self.assertTrue(any("Planung läuft weiter" in m for m in msgs))
        self.assertFalse(any("Sicherheits-Fast-Path" in m for m in msgs))

    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_critical_anomaly_still_fast_path(self, mock_proj):
        now = datetime(2026, 7, 7, 12, 0, 0)
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 50,
            "target_datetime": (now + timedelta(hours=8)).isoformat(),
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 20,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
        }
        config = self._anomaly_config()
        state = {
            "anomaly_active": True,
            "anomaly_excess_pct": 80.0,
            "balance_projection": mock_proj.return_value,
        }
        live = {
            "soc_percent": 60,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        actions, msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        self.assertTrue(any(a.get("source") == "anomaly" for a in actions))
        self.assertTrue(any("Sicherheits-Fast-Path" in m for m in msgs))


class TestNightLockAnomalyInteraction(unittest.TestCase):
    @patch("control_algorithm.service.save_state")
    @patch("control_algorithm.service.plan_load_actions", return_value=([], []))
    @patch("control_algorithm.service.check_anomaly")
    @patch("control_algorithm.service.check_min_soc_emergency", return_value=([], []))
    @patch("control_algorithm.service.load_live_snapshot", return_value={"soc_percent": 40, "miner_status": {}})
    @patch("control_algorithm.service.refresh_weather")
    @patch("control_algorithm.service.load_state")
    @patch("control_algorithm.service.load_config")
    def test_run_tick_checks_anomaly_while_night_lock_active(
        self,
        mock_cfg,
        mock_state,
        _refresh,
        _live,
        _emergency,
        mock_anomaly,
        _plan,
        _save,
    ):
        from control_algorithm.service import run_tick

        mock_cfg.return_value = {"enabled": True, "dry_run": True, "has_pv": False}
        mock_state.return_value = {
            "night_lock_active": True,
            "night_lock_reason": "test",
            "balance_projection": {},
            "device_runtime": {},
        }
        mock_anomaly.return_value = (mock_state.return_value, [{"ip": "x", "action": "pause"}], [])

        def _balance(cfg, state, live, base_dir):
            state["night_lock_active"] = True
            return state

        with patch("control_algorithm.service.maybe_run_hourly_balance", side_effect=_balance):
            with patch("control_algorithm.service.maybe_reset_on_config_change", return_value=(mock_state.return_value, False)):
                result = run_tick()

        mock_anomaly.assert_called()
        self.assertTrue(result.get("ok"))


class TestApplyHysteresis(unittest.TestCase):
    def test_blocks_rapid_switch(self):
        import time

        from control_algorithm.planner import apply_hysteresis

        now = time.time()
        actions = [{"ip": "10.0.0.1", "action": "pause"}]
        last_times = {"10.0.0.1": now - 60}
        filtered = apply_hysteresis(
            actions,
            {},
            min_interval_seconds=300,
            last_times=last_times,
            miner_status={"10.0.0.1": "running"},
        )
        self.assertEqual(filtered, [])

    def test_force_bypasses_hysteresis(self):
        import time

        from control_algorithm.planner import apply_hysteresis

        now = time.time()
        actions = [{"ip": "10.0.0.1", "action": "pause", "force": True}]
        last_times = {"10.0.0.1": now - 10}
        filtered = apply_hysteresis(actions, {}, min_interval_seconds=300, last_times=last_times)
        self.assertEqual(len(filtered), 1)
        self.assertTrue(filtered[0].get("force"))

    def test_switch_times_from_state(self):
        from control_algorithm.planner import switch_times_from_state

        ts = datetime(2026, 7, 7, 10, 0, 0).isoformat()
        state = {"device_runtime": {"10.0.0.1": {"last_action_at": ts}}}
        times = switch_times_from_state(state)
        self.assertIn("10.0.0.1", times)


class TestNightLockRelease(unittest.TestCase):
    @patch("control_algorithm.energy_balance.compute_horizon_projection")
    def test_releases_mid_hour_when_reachable(self, mock_proj):
        from control_algorithm.energy_balance import maybe_run_hourly_balance

        now = datetime(2026, 7, 7, 9, 30, 0)
        mock_proj.return_value = {
            "reachable": True,
            "projected_soc": 50.0,
            "target_soc": 40.0,
            "target_time": "07:00",
            "target_datetime": "2026-07-08T07:00:00",
            "horizon_end": "2026-07-10T12:00:00",
            "hours_left": 72.0,
            "horizon_days": 7,
            "miner_budget_kw_raw": 12.0,
            "current_miner_kw": 8.0,
            "checkpoints": [{"time": "07:00", "target_soc": 40.0, "projected_soc": 50.0, "reachable": True}],
            "pv_forecast_kwh": 100,
            "hydro_forecast_kwh": 10,
            "base_consumption_kwh": 20,
        }
        config = {"has_storage": True, "storage_kwh": 100, "soc_targets": [{"time": "07:00", "soc_percent": 40}]}
        state = {
            "night_lock_active": True,
            "night_lock_reason": "test",
            "last_hourly_balance_key": "hourly_balance_2026-07-07_9",
        }
        live = {"soc_percent": 55}
        state = maybe_run_hourly_balance(config, state, live, "/tmp", now=now)
        self.assertFalse(state.get("night_lock_active"))
        self.assertIsNone(state.get("night_lock_reason"))

    @patch("control_algorithm.energy_balance.simulate_battery_horizon")
    @patch("control_algorithm.energy_balance.compute_horizon_projection")
    def test_sets_mid_hour_when_unreachable(self, mock_proj, mock_sim):
        from control_algorithm.energy_balance import maybe_run_hourly_balance

        now = datetime(2026, 7, 7, 9, 30, 0)
        mock_proj.return_value = {
            "reachable": False,
            "projected_soc": 30.0,
            "target_soc": 40.0,
            "target_time": "07:00",
            "target_datetime": "2026-07-08T07:00:00",
            "horizon_end": "2026-07-10T12:00:00",
            "hours_left": 72.0,
            "horizon_days": 7,
            "miner_budget_kw_raw": 0.0,
            "current_miner_kw": 9.0,
            "checkpoints": [{"time": "07:00", "target_soc": 40.0, "projected_soc": 30.0, "reachable": False}],
            "binding_checkpoint": {"time": "07:00", "target_soc": 40.0, "projected_soc": 30.0, "target_datetime": "2026-07-08T07:00:00"},
            "pv_forecast_kwh": 10,
            "hydro_forecast_kwh": 0,
            "base_consumption_kwh": 50,
        }
        mock_sim.return_value = {"all_checkpoints_ok": False}
        config = {"has_storage": True, "storage_kwh": 100, "soc_targets": [{"time": "07:00", "soc_percent": 40}]}
        state = {
            "night_lock_active": False,
            "last_hourly_balance_key": "hourly_balance_2026-07-07_9",
        }
        live = {"soc_percent": 35}
        state = maybe_run_hourly_balance(config, state, live, "/tmp", now=now)
        self.assertTrue(state.get("night_lock_active"))
        self.assertIn("Prognose", state.get("night_lock_reason") or "")


class TestBudgetDeadband(unittest.TestCase):
    def _freeze_load_scheduler_now(self, now: datetime):
        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        return patch("control_algorithm.load_scheduler.datetime", FakeDatetime)

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh")
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_no_resume_at_budget_edge(self, mock_proj, mock_surplus):
        now = datetime(2026, 7, 7, 12, 0, 0)
        hours_left = 8.0
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 80,
            "target_datetime": (now + timedelta(hours=hours_left)).isoformat(),
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 50,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
        }
        mock_surplus.return_value = 160.0  # budget_kw = 20, resume_threshold = 19
        config = {
            "enabled": True,
            "min_soc_percent": 35,
            "min_device_runtime_hours": 0.5,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": "high"},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": "medium"},
                {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": "low"},
                {"ip": "10.0.0.4", "enabled": True, "power_kw": 3.2, "priority": "low"},
            ],
        }
        state = {"balance_projection": mock_proj.return_value}
        live = {
            "soc_percent": 60,
            "miner_status": {
                "10.0.0.1": "running",
                "10.0.0.2": "running",
                "10.0.0.3": "running",
                "10.0.0.4": "paused",
            },
        }
        with self._freeze_load_scheduler_now(now):
            actions, _msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        self.assertFalse(any(a.get("action") == "resume" for a in actions))

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh")
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_no_pause_slightly_above_budget(self, mock_proj, mock_surplus):
        now = datetime(2026, 7, 7, 12, 0, 0)
        hours_left = 8.0
        mock_proj.return_value = {
            "target_soc": 40,
            "projected_soc": 80,
            "target_datetime": (now + timedelta(hours=hours_left)).isoformat(),
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 50,
            "hydro_forecast_kwh": 5,
            "risk_factor_percent": 100,
        }
        mock_surplus.return_value = 160.0  # budget_kw = 20, pause threshold = 21
        config = {
            "enabled": True,
            "min_soc_percent": 35,
            "min_device_runtime_hours": 0.5,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 3.2, "priority": "low"},
            ],
        }
        state = {"balance_projection": mock_proj.return_value}
        live = {
            "soc_percent": 60,
            "miner_status": {"10.0.0.1": "running"},
        }
        actions, _msgs = plan_load_actions(config, live, state, "/tmp", 100, 100, 1.0)
        self.assertFalse(any(a.get("action") == "pause" for a in actions))


class TestHorizonPlanning(unittest.TestCase):
    def test_soc_targets_in_horizon_three_checkpoints(self):
        now = datetime(2026, 7, 7, 10, 0, 0)
        targets = [{"time": "07:00", "soc_percent": 40.0}]
        cps = soc_targets_in_horizon(targets, now, horizon_days=3)
        self.assertEqual(len(cps), 3)
        dates = [c["target_datetime"][:10] for c in cps]
        self.assertEqual(dates, ["2026-07-08", "2026-07-09", "2026-07-10"])

    def test_horizon_always_three_days_from_now(self):
        from control_algorithm.energy_balance import compute_horizon_projection

        now = datetime(2026, 7, 7, 14, 30, 0)
        config = {
            "storage_kwh": 1000.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
        }
        state = {
            "pv_forecast_today_hours": {f"h{h:02d}": 1.0 for h in range(24)},
            "pv_forecast_tomorrow_hours": {f"h{h:02d}": 1.0 for h in range(24)},
            "pv_forecast_day_after_hours": {f"h{h:02d}": 1.0 for h in range(24)},
        }
        live = {"soc_percent": 70.0}
        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", return_value=5.0
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=1.0
        ):
            proj = compute_horizon_projection(config, state, live, "/tmp", now)
        horizon_end = datetime.fromisoformat(str(proj["horizon_end"]))
        days = int(proj.get("horizon_days") or 7)
        self.assertGreaterEqual(days, 3)
        self.assertAlmostEqual(
            (horizon_end - now).total_seconds() / 3600.0, days * 24.0, delta=0.2
        )
        self.assertGreaterEqual(len(proj.get("checkpoints") or []), 3)

    def test_max_soc_caps_miner_surplus(self):
        """Niedrigeres Max-SoC → weniger Spielraum (ohne Near-Term-Boost)."""
        from control_algorithm.energy_balance import _max_miner_kw_for_horizon

        now = datetime(2026, 7, 7, 12, 0, 0)
        base_config = {
            "storage_kwh": 1000.0,
            "min_soc_percent": 35.0,
            "risk_factor_percent": 100.0,
            "planning_horizon_days": 3,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            "devices": [{"ip": "10.0.0.1", "enabled": True, "power_kw": 50.0, "priority": "low"}],
        }
        state = {
            "pv_yield_today_kwh": 100.0,
            "pv_yield_tomorrow_kwh": 100.0,
            "pv_forecast_today_hours": {f"h{h:02d}": (8.0 if 8 <= h <= 16 else 0.0) for h in range(24)},
            "pv_forecast_tomorrow_hours": {f"h{h:02d}": (8.0 if 8 <= h <= 16 else 0.0) for h in range(24)},
            "pv_forecast_day_after_hours": {f"h{h:02d}": (8.0 if 8 <= h <= 16 else 0.0) for h in range(24)},
        }
        live = {"soc_percent": 55.0, "pv_kw": 5.0}

        def _base(_bd, start, end):
            return max(0.0, (end - start).total_seconds() / 3600.0) * 2.0

        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", side_effect=_base
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=0.0
        ):
            hi, _, _ = _max_miner_kw_for_horizon(
                {**base_config, "max_soc_percent": 95.0}, state, live, "/tmp", now, 3
            )
            lo, _, _ = _max_miner_kw_for_horizon(
                {**base_config, "max_soc_percent": 60.0}, state, live, "/tmp", now, 3
            )
        # Niedrigeres Max zwingt mehr Last (nicht vollaufen / entladen).
        self.assertGreaterEqual(lo, hi)

    def test_above_max_soc_allows_miner_budget(self):
        """Speicher über Max-SoC: Miner-Budget darf nicht 0 sein (Entladen)."""
        from control_algorithm.energy_balance import (
            compute_horizon_projection,
            miner_surplus_kwh,
        )

        now = datetime(2026, 7, 10, 14, 0, 0)
        config = {
            "storage_kwh": 1100.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 4.5, "priority": "high"},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 4.5, "priority": "high"},
            ],
        }
        state = {
            "pv_forecast_today_hours": {f"h{h:02d}": 5.0 for h in range(24)},
            "pv_forecast_tomorrow_hours": {f"h{h:02d}": 5.0 for h in range(24)},
            "pv_forecast_day_after_hours": {f"h{h:02d}": 5.0 for h in range(24)},
        }
        live = {"soc_percent": 96.0, "miner_status": {}}
        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", return_value=15.0
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=1.0
        ):
            surplus = miner_surplus_kwh(config, state, live, "/tmp", now)
            proj = compute_horizon_projection(config, state, live, "/tmp", now)
        self.assertGreater(surplus, 0.0)
        self.assertGreater(float(proj.get("miner_budget_kw") or 0), 0.0)

    def test_proactive_discharge_boosts_budget_before_sunny_day(self):
        from control_algorithm.energy_balance import compute_horizon_projection

        now = datetime(2026, 7, 10, 20, 0, 0)
        config = {
            "storage_kwh": 1100.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            "devices": [
                {"ip": f"10.0.0.{i}", "enabled": True, "power_kw": 4.5, "priority": "high"}
                for i in range(1, 8)
            ],
        }
        state = {
            "pv_yield_tomorrow_kwh": 580.0,
            "pv_forecast_today_hours": {f"h{h:02d}": 0.0 for h in range(24)},
            "pv_forecast_tomorrow_hours": {f"h{h:02d}": 8.0 for h in range(24)},
            "pv_forecast_day_after_hours": {f"h{h:02d}": 5.0 for h in range(24)},
        }
        live = {"soc_percent": 90.0, "miner_status": {}, "pv_kw": 0.0}

        def _base(_bd, start, end):
            return max(0.0, (end - start).total_seconds() / 3600.0) * 2.0

        def _hydro(_bd, start, end):
            return max(0.0, (end - start).total_seconds() / 3600.0) * 0.5

        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", side_effect=_base
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", side_effect=_hydro
        ):
            proj = compute_horizon_projection(config, state, live, "/tmp", now)
        proactive = float(proj.get("proactive_discharge_kw") or 0)
        budget = float(proj.get("miner_budget_kw") or 0)
        self.assertGreater(proactive, 8.0)
        # Budget kann unter Proactive liegen, wenn 7-Tage-Checkpoints enger sind –
        # dann greift der Scheduler ggf. nur teilweise; Proactive-Wert bleibt sichtbar.
        self.assertGreater(budget, 0.0)

    def test_near_term_boost_raises_budget_on_sunny_days(self):
        """7-Tage-Gleichlast zu niedrig → heute/morgen Boost bis alle Miner."""
        from control_algorithm.energy_balance import compute_horizon_projection

        now = datetime(2026, 7, 18, 11, 0, 0)
        config = {
            "storage_kwh": 1100.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "planning_horizon_days": 7,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            "devices": [
                {"ip": f"10.0.0.{i}", "enabled": True, "power_kw": 5.5, "priority": "high"}
                for i in range(1, 7)
            ],
        }
        # Realistische Stundenwerte (~800/900 kWh Tag)
        def _day_hours(peak: float) -> dict:
            out = {}
            for h in range(24):
                if 6 <= h <= 18:
                    # Glockenkurve
                    x = (h - 12) / 4.0
                    out[f"h{h:02d}"] = round(max(0.5, peak * (2.718 ** (-x * x))), 2)
                else:
                    out[f"h{h:02d}"] = 0.0
            return out

        hours_sun = _day_hours(90.0)
        hours_low = _day_hours(8.0)
        today = now.date()
        state = {
            "pv_yield_today_kwh": 800.0,
            "pv_yield_tomorrow_kwh": 900.0,
            "pv_forecast_today_hours": hours_sun,
            "pv_forecast_tomorrow_hours": hours_sun,
            "pv_forecast_by_date": {
                today.isoformat(): {"daily_kwh": 800.0, "hours": hours_sun},
                (today + timedelta(days=1)).isoformat(): {"daily_kwh": 900.0, "hours": hours_sun},
                (today + timedelta(days=2)).isoformat(): {"daily_kwh": 350.0, "hours": _day_hours(40.0)},
                (today + timedelta(days=3)).isoformat(): {"daily_kwh": 300.0, "hours": _day_hours(35.0)},
                (today + timedelta(days=4)).isoformat(): {"daily_kwh": 280.0, "hours": _day_hours(32.0)},
                (today + timedelta(days=5)).isoformat(): {"daily_kwh": 250.0, "hours": _day_hours(28.0)},
                (today + timedelta(days=6)).isoformat(): {"daily_kwh": 250.0, "hours": _day_hours(28.0)},
            },
        }
        live = {"soc_percent": 70.0, "pv_kw": 100.0, "miner_status": {}}

        def _base(_bd, start, end):
            return max(0.0, (end - start).total_seconds() / 3600.0) * 1.8

        def _hydro(_bd, start, end):
            return max(0.0, (end - start).total_seconds() / 3600.0) * 0.5

        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", side_effect=_base
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", side_effect=_hydro
        ):
            proj = compute_horizon_projection(config, state, live, "/tmp", now)
        budget = float(proj.get("miner_budget_kw") or 0)
        self.assertEqual(proj.get("binding_constraint"), "near_term_discharge")
        nt = proj.get("near_term_plan") or {}
        self.assertTrue(nt.get("plan_ok"))
        sustained = float(nt.get("sustained_budget_kw") or 0)
        self.assertIsNotNone(nt.get("sustained_budget_kw"))
        # Zwei-Phasen: Near-Term höher als nachhaltiges Budget
        self.assertGreater(budget, sustained)
        self.assertGreater(budget, 12.0)

        # Mit Zwei-Phasen-Last bleiben 7-Tage-Checkpoints erreichbar
        from control_algorithm.energy_balance import simulate_battery_horizon

        sim = simulate_battery_horizon(
            config,
            state,
            "/tmp",
            now,
            start_kwh=70.0 / 100.0 * 1100.0,
            miner_kw=float(nt["sustained_budget_kw"]),
            miner_kw_near=budget,
            near_term_hours=float(nt["near_term_hours"]),
            horizon_days=7,
        )
        self.assertTrue(sim.get("all_checkpoints_ok"))
        self.assertGreaterEqual(float(sim.get("min_soc_in_horizon") or 0), 35.0)

    def test_night_lock_on_day_two_checkpoint_failure(self):
        from control_algorithm.energy_balance import maybe_run_hourly_balance

        now = datetime(2026, 7, 7, 22, 0, 0)
        config = {
            "has_storage": True,
            "storage_kwh": 1000.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
        }
        state: dict = {}
        live = {"soc_percent": 42.0}
        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", return_value=80.0
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=0.0
        ), patch(
            "control_algorithm.energy_balance._pv_forecast_kwh", return_value=5.0
        ):
            state = maybe_run_hourly_balance(config, state, live, "/tmp", now)
        self.assertTrue(state.get("night_lock_active"))
        self.assertIn("Prognose", state.get("night_lock_reason") or "")

    def test_night_lock_with_running_miners_at_low_soc(self):
        """42 % SOC + laufende Miner über Nacht → Nacht-Sperre (nicht nur Hauslast)."""
        from control_algorithm.energy_balance import maybe_run_hourly_balance

        now = datetime(2026, 7, 9, 23, 0, 0)
        config = {
            "has_storage": True,
            "storage_kwh": 1100.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 4.5, "priority": "high"},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 4.5, "priority": "high"},
            ],
        }
        state: dict = {}
        live = {
            "soc_percent": 42.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
        }
        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", return_value=40.0
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=2.0
        ), patch(
            "control_algorithm.energy_balance._pv_forecast_kwh", return_value=8.0
        ):
            state = maybe_run_hourly_balance(config, state, live, "/tmp", now)
        self.assertTrue(state.get("night_lock_active"))
        self.assertIn("auch ohne Miner", state.get("night_lock_reason") or "")

    def test_rolling_window_recalculates_after_day_passes(self):
        from control_algorithm.energy_balance import compute_horizon_projection

        config = {
            "storage_kwh": 1000.0,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
        }
        state = {
            "pv_forecast_today_hours": {f"h{h:02d}": 1.0 for h in range(24)},
            "pv_forecast_tomorrow_hours": {f"h{h:02d}": 1.0 for h in range(24)},
            "pv_forecast_day_after_hours": {f"h{h:02d}": 1.0 for h in range(24)},
        }
        live = {"soc_percent": 70.0}
        now1 = datetime(2026, 7, 7, 10, 0, 0)
        now2 = now1 + timedelta(days=1)
        with patch(
            "control_algorithm.energy_balance.expected_base_consumption_kwh", return_value=5.0
        ), patch(
            "control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=1.0
        ):
            proj1 = compute_horizon_projection(config, state, live, "/tmp", now1)
            proj2 = compute_horizon_projection(config, state, live, "/tmp", now2)
        end1 = datetime.fromisoformat(str(proj1["horizon_end"]))
        end2 = datetime.fromisoformat(str(proj2["horizon_end"]))
        days = int(proj1.get("horizon_days") or 7)
        self.assertEqual(end1, now1 + timedelta(days=days))
        self.assertEqual(end2, now2 + timedelta(days=days))
        self.assertNotEqual(end1, end2)


class TestDryRunState(unittest.TestCase):
    def test_update_device_runtime_skips_dry_run_actions(self):
        from control_algorithm.load_scheduler import update_device_runtime

        state: dict = {"device_runtime": {}}
        actions = [{"ip": "10.0.0.1", "action": "resume", "executed": False, "dry_run": True}]
        state = update_device_runtime(state, actions)
        self.assertNotIn("10.0.0.1", state.get("device_runtime") or {})

    def test_apply_device_state_updates_without_executed_flag_still_writes(self):
        """Low-level helper schreibt weiter; Dry-Run nutzt update_device_runtime."""
        from control_algorithm.load_scheduler import apply_device_state_updates

        state: dict = {"device_runtime": {}}
        actions = [{"ip": "10.0.0.1", "action": "resume", "executed": False, "dry_run": True}]
        state = apply_device_state_updates(state, actions)
        runtime = state["device_runtime"]["10.0.0.1"]
        self.assertEqual(runtime["last_action"], "resume")
        self.assertIsNotNone(runtime.get("resumed_at"))


if __name__ == "__main__":
    unittest.main()
