# -*- coding: utf-8 -*-
"""Planungsziel: bleibt bei zu wenig PV, ändert sich mit der Anlage."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from control_algorithm.fill_schedule import compute_fill_schedule
from control_algorithm.planning_target import refresh_planning_target
from control_algorithm.service import _evening_aim_payload
from control_algorithm.steering_learning import ensure_steering_learning


def _config(**overrides):
    cfg = {
        "storage_kwh": 41.0,
        "pv_kwp": 16.0,
        "min_soc_percent": 30.0,
        "max_soc_percent": 95.0,
        "devices": [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 4.6, "priority": 3},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 6.0, "priority": 1},
        ],
    }
    cfg.update(overrides)
    return cfg


def _state(band: float = 89.0):
    state = {}
    ensure_steering_learning(state)
    state["steering_learning"]["params"]["band_max_target_pct"] = band
    return state


class TestPlanningTarget(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch("control_algorithm.config.load_miners", return_value=[]),
            patch("miner_control._ssh_circuit_open", return_value=False),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self.patches])

    def test_first_sight_keeps_learned_value(self):
        state = _state(89.0)
        info = refresh_planning_target(_config(), state)
        self.assertEqual(info["soc_percent"], 89.0)
        self.assertEqual(info["reason"], "uebernahme")
        again = refresh_planning_target(_config(), state)
        self.assertEqual(again["soc_percent"], 89.0)
        self.assertEqual(again["reason"], "uebernahme")

    def test_low_forecast_does_not_replace_target(self):
        state = _state(89.0)
        refresh_planning_target(_config(), state)
        state["curve_plan"] = {
            "plan_max_soc_percent": 70.0,
            "stromprofil": {"steering_goal": {"target_peak_pct": 62.0}},
        }
        state["live_soc"] = 62.0
        aim = _evening_aim_payload(_config(), state)
        self.assertEqual(aim["soc_percent"], 89.0)

    def test_removed_device_lowers_target(self):
        state = _state(89.0)
        refresh_planning_target(_config(), state)
        smaller = _config(
            devices=[
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 4.6, "priority": 3},
            ]
        )
        info = refresh_planning_target(smaller, state)
        self.assertEqual(info["reason"], "anlage")
        self.assertLess(info["soc_percent"], 89.0)

    def test_unreachable_device_counts_as_gone(self):
        state = _state(89.0)
        refresh_planning_target(_config(), state)
        missing = [
            {
                "ip": "10.0.0.2",
                "recovery_state": "missing",
                "recovery_miss_streak": 6,
            }
        ]
        with patch("control_algorithm.config.load_miners", return_value=missing):
            info = refresh_planning_target(_config(), state)
        self.assertEqual(info["reason"], "anlage")
        self.assertLess(info["soc_percent"], 89.0)

    def test_larger_storage_raises_target(self):
        state = _state(89.0)
        refresh_planning_target(_config(), state)
        info = refresh_planning_target(_config(storage_kwh=82.0), state)
        self.assertGreater(info["soc_percent"], 89.0)


class TestFillExactTargetDoesNotStart(unittest.TestCase):
    def _run(self, charge_kw: float, soc: float = 85.0):
        now = datetime(2026, 10, 15, 12, 0, 0)
        config = {
            "storage_kwh": 41.0,
            "min_soc_percent": 30.0,
            "max_soc_percent": 95.0,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 1.5, "priority": 10},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 6.0, "priority": 1},
            ],
        }
        live = {
            "soc_percent": soc,
            "pv_kw": 3.0,
            "house_kw": 10.0,
            "battery_power_kw": charge_kw,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        with patch(
            "control_algorithm.storage_band_learning.effective_planning_max_soc",
            return_value=89.0,
        ), patch(
            "control_algorithm.weather_survival.last_pv_hour_for_date",
            return_value=16,
        ):
            return compute_fill_schedule(config, {}, live, now)

    def test_exact_fill_to_target_keeps_devices_off(self):
        # Restplatz (89-85)% * 41 kWh = 1.64 kWh über 4 h → 0.41 kW.
        plan = self._run(0.41)
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["flex"]["should_start_now"])
        self.assertLess(plan["flex"]["overflow_kwh"], 0.3)

    def test_surplus_above_target_starts_flex(self):
        plan = self._run(2.0)
        self.assertTrue(plan["flex"]["should_start_now"])
        self.assertGreater(plan["flex"]["overflow_kwh"], 0.3)
