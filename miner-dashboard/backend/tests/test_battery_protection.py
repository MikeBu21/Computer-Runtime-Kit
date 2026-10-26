# -*- coding: utf-8 -*-
"""Tests Batterie-Schutz Min/Max-SoC."""
from __future__ import annotations

import unittest

from control_algorithm.battery_protection import (
    is_max_soc_protection_active,
    is_min_soc_protection_active,
    max_soc_protection_actions,
    min_soc_protection_actions,
    refresh_min_soc_protection,
    sync_max_soc_protection_state,
)
from control_algorithm.load_scheduler import plan_load_actions


class TestBatteryProtection(unittest.TestCase):
    def _devices_config(self) -> dict:
        return {
            "enabled": True,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "min_device_runtime_hours": 1.0,
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 3.2, "priority": 3},
            ],
        }

    def test_min_soc_pauses_and_blocks_plan(self):
        config = self._devices_config()
        live = {"soc_percent": 34.0, "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"}}
        state = {}
        refresh_min_soc_protection(config, live, state)
        self.assertTrue(state["min_soc_lock_active"])
        actions, msgs = min_soc_protection_actions(config, live, state)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "pause")
        self.assertTrue(actions[0].get("force"))
        plan_actions, plan_msgs = plan_load_actions(config, live, state, "/tmp", 10.0, 8.0, 1.0)
        self.assertTrue(all(a.get("action") == "pause" for a in plan_actions))
        self.assertFalse(any(a.get("action") == "resume" for a in plan_actions))
        self.assertTrue(
            any(
                "Min" in m or "Batterie-Schutz" in m or "min_soc" in m.lower()
                for m in plan_msgs
            )
        )

    def test_min_soc_hysteresis_keeps_lock_until_recovery(self):
        config = self._devices_config()
        live = {"soc_percent": 35.5, "miner_status": {}}
        state = {"min_soc_lock_active": True}
        self.assertTrue(is_min_soc_protection_active(config, live, state))
        refresh_min_soc_protection(config, live, state)
        self.assertTrue(state["min_soc_lock_active"])
        live["soc_percent"] = 36.1
        refresh_min_soc_protection(config, live, state)
        self.assertFalse(state["min_soc_lock_active"])

    def test_max_soc_resumes_highest_first_and_blocks_plan(self):
        config = self._devices_config()
        live = {
            "soc_percent": 96.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
            "pv_kw": 20.0,
        }
        state = {}
        sync_max_soc_protection_state(state, config, live, over_max=True)
        self.assertTrue(state["max_soc_protection_active"])
        actions, msgs = max_soc_protection_actions(config, live, state)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["ip"], "10.0.0.1")
        self.assertEqual(actions[0]["action"], "resume")
        self.assertTrue(actions[0].get("force"))
        plan_actions, plan_msgs = plan_load_actions(config, live, state, "/tmp", 20.0, 10.0, 1.0)
        self.assertTrue(all(a.get("action") != "pause" for a in plan_actions))
        self.assertTrue(any("Max-SoC" in m for m in plan_msgs))

    def test_max_soc_adds_more_when_battery_still_charging(self):
        config = self._devices_config()
        live = {
            "soc_percent": 96.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
            "pv_kw": 20.0,
            "battery_power_kw": 4.0,
        }
        state = {"max_soc_protection_active": True}
        actions, _msgs = max_soc_protection_actions(config, live, state)
        self.assertEqual([a["ip"] for a in actions], ["10.0.0.2"])
        self.assertEqual(actions[0]["action"], "resume")

    def test_max_soc_does_not_add_when_pv_low_or_not_charging(self):
        config = self._devices_config()
        state = {"max_soc_protection_active": True}
        live_low_pv = {
            "soc_percent": 96.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
            "pv_kw": 6.0,  # 80% = 4.8; laufend 5.5 → kein Platz
            "battery_power_kw": 3.0,
        }
        actions, _ = max_soc_protection_actions(config, live_low_pv, state)
        self.assertEqual(actions, [])
        live_discharge = {
            "soc_percent": 96.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
            "pv_kw": 20.0,
            "battery_power_kw": -1.0,
        }
        actions, _ = max_soc_protection_actions(config, live_discharge, state)
        self.assertEqual(actions, [])

    def test_max_soc_clears_at_hold_not_half_point(self):
        config = self._devices_config()
        config["max_soc_release_percent"] = 90.0
        state = {"max_soc_override_active": True, "max_soc_protection_active": True}
        live = {"soc_percent": 94.0, "miner_status": {}}
        sync_max_soc_protection_state(state, config, live, over_max=False)
        self.assertTrue(state["max_soc_protection_active"])
        self.assertTrue(state.get("max_soc_override_active"))
        live["soc_percent"] = 90.0
        sync_max_soc_protection_state(state, config, live, over_max=False)
        self.assertFalse(state["max_soc_protection_active"])
        self.assertFalse(state.get("max_soc_override_active"))
        self.assertTrue(state.get("max_soc_hold_released"))

    def test_min_soc_wins_over_max_soc(self):
        config = self._devices_config()
        live = {"soc_percent": 30.0, "miner_status": {"10.0.0.1": "running"}, "pv_kw": 0.0}
        state = {"max_soc_protection_active": True}
        refresh_min_soc_protection(config, live, state)
        self.assertTrue(state["min_soc_lock_active"])
        plan_actions, _ = plan_load_actions(config, live, state, "/tmp", 0.0, 0.0, 1.0)
        self.assertFalse(any(a.get("action") == "resume" for a in plan_actions))


if __name__ == "__main__":
    unittest.main()
