# -*- coding: utf-8 -*-
"""Dashboard-Schalter: Nachtsperre aus, übrige Regeln bleiben."""
from __future__ import annotations

import unittest

from control_algorithm.energy_balance import apply_night_lock_switch, night_lock_enabled
from control_algorithm.load_scheduler import night_lock_pause_actions, plan_load_actions


def _cfg(**extra):
    cfg = {
        "min_soc_percent": 35.0,
        "max_soc_percent": 95.0,
        "max_soc_release_percent": 90.0,
        "has_pv": True,
        "has_storage": True,
        "storage_kwh": 41.0,
        "devices": [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 6.0, "priority": 10},
        ],
    }
    cfg.update(extra)
    return cfg


def _locked_state():
    return {
        "night_lock_active": True,
        "night_lock_force": True,
        "night_lock_reason": "7-Tage-Prognose Tief unter Min",
    }


class NightLockSwitchTests(unittest.TestCase):
    def test_missing_flag_keeps_lock_on(self):
        self.assertTrue(night_lock_enabled({}))

    def test_switch_off_clears_forced_flag(self):
        state = apply_night_lock_switch(
            _cfg(night_lock_enabled=False), _locked_state()
        )
        self.assertFalse(state["night_lock_active"])
        self.assertFalse(state["night_lock_force"])
        self.assertIsNone(state["night_lock_reason"])

    def test_switch_on_keeps_forced_flag(self):
        state = apply_night_lock_switch(_cfg(night_lock_enabled=True), _locked_state())
        self.assertTrue(state["night_lock_active"])

    def test_switch_off_emits_no_pause(self):
        live = {
            "soc_percent": 70.0,
            "pv_kw": 8.0,
            "miner_status": {"192.0.2.10": "running"},
        }
        actions, _msgs = night_lock_pause_actions(
            _cfg(night_lock_enabled=False), live, _locked_state()
        )
        self.assertEqual(actions, [])

    def test_switch_on_still_pauses(self):
        live = {
            "soc_percent": 70.0,
            "pv_kw": 8.0,
            "miner_status": {"192.0.2.10": "running"},
        }
        actions, _msgs = night_lock_pause_actions(
            _cfg(night_lock_enabled=True), live, _locked_state()
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["ip"], "192.0.2.10")
        self.assertEqual(actions[0]["source"], "night_lock")

    def test_planner_skips_night_lock_return_when_off(self):
        live = {
            "soc_percent": 70.0,
            "pv_kw": 8.0,
            "miner_status": {"192.0.2.10": "running"},
        }
        actions, messages = plan_load_actions(
            _cfg(night_lock_enabled=False),
            live,
            _locked_state(),
            "/tmp",
            40.0,
            10.0,
            1.0,
        )
        self.assertFalse(any(a.get("source") == "night_lock" for a in actions))
        self.assertFalse(any("Nacht-Sperre aktiv" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
