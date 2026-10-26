# -*- coding: utf-8 -*-
"""Harte SoC-Grenzen stehen über der Nachtsperre."""
from __future__ import annotations

import unittest

from control_algorithm.battery_protection import max_soc_protection_actions
from control_algorithm.hard_soc_guard import seal_hard_soc_actions, verify_hard_soc_rules
from control_algorithm.load_scheduler import night_lock_pause_actions


def _cfg():
    return {
        "min_soc_percent": 35.0,
        "max_soc_percent": 95.0,
        "devices": [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 1.5, "priority": 3},
        ],
    }


class TestHardSocLimits(unittest.TestCase):
    def test_over_max_starts_despite_night_lock(self):
        live = {
            "soc_percent": 99.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        state = {"night_lock_active": True, "night_lock_force": True}
        actions, _msgs = max_soc_protection_actions(_cfg(), live, state)
        ips = {a["ip"] for a in actions}
        # Erster Start bei Max-SoC: nur höchste Priorität
        self.assertEqual(ips, {"10.0.0.1"})
        self.assertTrue(all(a["action"] == "resume" and a["emergency"] for a in actions))

    def test_night_lock_does_not_pause_over_max(self):
        live = {
            "soc_percent": 100.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
        }
        state = {
            "night_lock_active": True,
            "night_lock_force": True,
            "night_lock_reason": "7-Tage-Prognose",
        }
        actions, msgs = night_lock_pause_actions(_cfg(), live, state)
        self.assertEqual(actions, [])
        self.assertTrue(any("ausgesetzt" in m for m in msgs))

    def test_startup_probe_accepts_current_rules(self):
        self.assertEqual(verify_hard_soc_rules(), [])

    def test_seal_restores_max_after_a_later_pause(self):
        cfg = _cfg()
        live = {
            "soc_percent": 96.0,
            "pv_kw": 20.0,
            "battery_power_kw": 3.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "running"},
        }
        stolen = [
            {
                "ip": "10.0.0.2",
                "action": "pause",
                "source": "night_lock",
                "reason": "Nachtsperre",
            }
        ]
        actions, notes = seal_hard_soc_actions(stolen, cfg, live)
        self.assertFalse(any(a.get("action") == "pause" for a in actions))
        self.assertIn("10.0.0.1", {a.get("ip") for a in actions if a.get("action") == "resume"})
        self.assertTrue(notes)

    def test_seal_does_not_force_all_when_battery_not_charging(self):
        cfg = _cfg()
        live = {
            "soc_percent": 96.0,
            "pv_kw": 20.0,
            "battery_power_kw": -2.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        actions, _notes = seal_hard_soc_actions([], cfg, live)
        self.assertFalse(any(a.get("action") == "resume" for a in actions))
        self.assertFalse(any(a.get("action") == "pause" for a in actions))

    def test_under_min_still_pauses(self):
        from control_algorithm.battery_protection import min_soc_protection_actions

        live = {
            "soc_percent": 34.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        actions, _msgs = min_soc_protection_actions(_cfg(), live, {})
        self.assertEqual([a["ip"] for a in actions], ["10.0.0.1"])
        self.assertEqual(actions[0]["action"], "pause")
        self.assertTrue(actions[0]["emergency"])
