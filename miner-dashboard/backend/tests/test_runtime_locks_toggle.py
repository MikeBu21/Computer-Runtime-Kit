# -*- coding: utf-8 -*-
"""Algorithmus-Sperren aus: Mindestlaufzeit und An-Bindung, nicht die Mensch-Sperre."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from control_algorithm.load_scheduler import is_min_runtime_protected
from control_algorithm.manual_override import apply_manual_override_guards
from control_algorithm.switch_commitment import apply_switch_commitment_guards


class TestRuntimeLocksToggle(unittest.TestCase):
    def test_min_runtime_blocks_until_toggle_off(self):
        now = datetime.now()
        state = {
            "device_runtime": {
                "10.0.0.5": {"resumed_at": now.isoformat()},
            }
        }
        config = {"min_device_runtime_hours": 1.0, "algo_runtime_locks_enabled": True}
        device = {"ip": "10.0.0.5", "power_kw": 6.0, "priority": 10, "enabled": True}
        self.assertTrue(
            is_min_runtime_protected(
                "10.0.0.5", state, config, live={"soc_percent": 50}, device=device, devices=[device]
            )
        )
        config["algo_runtime_locks_enabled"] = False
        self.assertFalse(
            is_min_runtime_protected(
                "10.0.0.5", state, config, live={"soc_percent": 50}, device=device, devices=[device]
            )
        )

    def test_on_commitment_ignored_when_locks_off(self):
        now = datetime(2026, 10, 16, 14, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.5": {
                    "committed_on_until": (now + timedelta(hours=3)).isoformat(),
                    "on_count_today": 1,
                    "on_count_date": "2026-10-16",
                }
            }
        }
        action = [{"ip": "10.0.0.5", "action": "pause", "reason": "Anker-Reserve erreicht"}]
        blocked, msgs = apply_switch_commitment_guards(
            action, state, {"algo_runtime_locks_enabled": True, "hard_daily_on_limit": False}, {}, now
        )
        self.assertEqual(blocked, [])
        self.assertTrue(any("Commitment an" in m for m in msgs))

        allowed, msgs_off = apply_switch_commitment_guards(
            action, state, {"algo_runtime_locks_enabled": False, "hard_daily_on_limit": False}, {}, now
        )
        self.assertEqual(allowed[0]["action"], "pause")
        self.assertFalse(any("Commitment an" in m for m in msgs_off))

    def test_human_hold_stays_when_algo_locks_off(self):
        now = datetime(2026, 10, 16, 14, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.5": {
                    "manual_hold_until": (now + timedelta(hours=2)).isoformat(),
                    "manual_hold_reason": "fremd",
                }
            }
        }
        config = {
            "algo_runtime_locks_enabled": False,
            "manual_override_hold_hours": 2.0,
            "devices": [{"ip": "10.0.0.5", "name": "Hydro"}],
        }
        actions, msgs = apply_manual_override_guards(
            [{"ip": "10.0.0.5", "action": "pause", "reason": "Budget"}],
            state,
            config,
            now,
        )
        self.assertEqual(actions, [])
        self.assertTrue(any("Fremdsteuerung" in m for m in msgs))
