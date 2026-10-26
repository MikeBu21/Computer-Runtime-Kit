# -*- coding: utf-8 -*-
"""Tests Manuelle Fremdsteuerung / Hold-Countdown."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from control_algorithm.manual_override import (
    apply_manual_override_guards,
    detect_and_update_manual_holds,
    is_manual_hold_active,
    normalize_manual_override_hold_hours,
)


def _cfg(**kwargs):
    base = {
        "manual_override_hold_hours": 2.0,
        "devices": [{"ip": "10.0.0.1", "name": "Hydro", "enabled": True, "power_kw": 5.0}],
    }
    base.update(kwargs)
    return base


class TestNormalize(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(normalize_manual_override_hold_hours(-1), 0.0)
        self.assertEqual(normalize_manual_override_hold_hours(100), 48.0)
        self.assertEqual(normalize_manual_override_hold_hours("2.5"), 2.5)


class TestDetectFremdAus(unittest.TestCase):
    def test_fremd_aus_sets_hold_and_blocks(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {"last_action": "resume", "last_action_at": "2026-09-28T10:00:00"},
            }
        }
        live = {"10.0.0.1": "paused"}
        cfg = _cfg()
        state, msgs = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        entry = state["device_runtime"]["10.0.0.1"]
        self.assertTrue(is_manual_hold_active(entry, now))
        self.assertEqual(entry.get("manual_hold_reason"), "fremd_aus")
        self.assertTrue(any("Fremdsteuerung" in m for m in msgs))

        actions = [
            {"ip": "10.0.0.1", "action": "resume", "reason": "Budget ok"},
            {"ip": "10.0.0.1", "action": "pause", "reason": "Budget eng"},
        ]
        filtered, gmsgs = apply_manual_override_guards(actions, state, config=cfg, now=now)
        self.assertEqual(filtered, [])
        self.assertEqual(len(gmsgs), 2)


class TestDetectFremdEin(unittest.TestCase):
    def test_fremd_ein_sets_hold(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {"last_action": "pause", "last_action_at": "2026-09-28T10:00:00"},
            }
        }
        live = {"10.0.0.1": "running"}
        cfg = _cfg()
        state, _ = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        entry = state["device_runtime"]["10.0.0.1"]
        self.assertEqual(entry.get("manual_hold_reason"), "fremd_ein")
        self.assertTrue(is_manual_hold_active(entry, now))


class TestCountdownExpired(unittest.TestCase):
    def test_expired_allows_actions(self):
        now = datetime(2026, 9, 28, 14, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "resume",
                    "manual_hold_until": "2026-09-28T13:00:00",
                    "manual_hold_reason": "fremd_aus",
                }
            }
        }
        # Live wieder passend zum Soll → kein neuer Hold; abgelaufener wird geräumt
        live = {"10.0.0.1": "running"}
        cfg = _cfg()
        state, _ = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        self.assertFalse(
            is_manual_hold_active(state["device_runtime"]["10.0.0.1"], now)
        )
        actions = [{"ip": "10.0.0.1", "action": "pause", "reason": "Budget"}]
        filtered, msgs = apply_manual_override_guards(
            actions, state, config=cfg, now=now
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(msgs, [])


class TestTimerRefresh(unittest.TestCase):
    def test_same_mismatch_does_not_extend_timer(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        original_until = (now + timedelta(minutes=30)).isoformat()
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "resume",
                    "manual_hold_until": original_until,
                    "manual_hold_reason": "fremd_aus",
                    "manual_hold_since": (now - timedelta(hours=1)).isoformat(),
                }
            }
        }
        live = {"10.0.0.1": "paused"}
        cfg = _cfg(manual_override_hold_hours=2.0)
        state, msgs = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        self.assertEqual(
            state["device_runtime"]["10.0.0.1"]["manual_hold_until"], original_until
        )
        self.assertEqual(msgs, [])

    def test_direction_change_extends_timer(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        # Hold war fremd_aus, aber last_action noch resume und Live jetzt wieder an
        # → eigentlich match. Für Richtungswechsel: last_action=pause (Soll aus),
        # Hold reason war fremd_aus, Live jetzt running → fremd_ein.
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "pause",
                    "manual_hold_until": (now + timedelta(minutes=30)).isoformat(),
                    "manual_hold_reason": "fremd_aus",
                    "manual_hold_since": (now - timedelta(hours=1)).isoformat(),
                }
            }
        }
        live = {"10.0.0.1": "running"}
        cfg = _cfg(manual_override_hold_hours=2.0)
        state, msgs = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        until = state["device_runtime"]["10.0.0.1"]["manual_hold_until"]
        self.assertEqual(until, (now + timedelta(hours=2)).isoformat())
        self.assertEqual(
            state["device_runtime"]["10.0.0.1"]["manual_hold_reason"], "fremd_ein"
        )
        self.assertTrue(any("verlängert" in m for m in msgs))


class TestExpireAdoptsLive(unittest.TestCase):
    def test_expire_syncs_last_action_no_rehold(self):
        now = datetime(2026, 9, 28, 14, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "resume",
                    "manual_hold_until": "2026-09-28T13:00:00",
                    "manual_hold_reason": "fremd_aus",
                }
            }
        }
        # Gerät noch aus nach manuellem Stopp
        live = {"10.0.0.1": "paused"}
        cfg = _cfg()
        state, msgs = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        entry = state["device_runtime"]["10.0.0.1"]
        self.assertFalse(is_manual_hold_active(entry, now))
        self.assertEqual(entry.get("last_action"), "pause")
        self.assertEqual(msgs, [])
        # Zweiter Tick: kein neuer Hold, Soll = Live
        state, msgs2 = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        self.assertFalse(
            is_manual_hold_active(state["device_runtime"]["10.0.0.1"], now)
        )
        self.assertEqual(msgs2, [])


class TestFeatureOff(unittest.TestCase):
    def test_zero_hours_no_hold(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "resume",
                    "manual_hold_until": (now + timedelta(hours=1)).isoformat(),
                    "manual_hold_reason": "fremd_aus",
                }
            }
        }
        live = {"10.0.0.1": "paused"}
        cfg = _cfg(manual_override_hold_hours=0)
        state, msgs = detect_and_update_manual_holds(
            state, live, config=cfg, now=now, last_action_times={}
        )
        self.assertIsNone(state["device_runtime"]["10.0.0.1"].get("manual_hold_until"))
        self.assertEqual(msgs, [])
        actions = [{"ip": "10.0.0.1", "action": "resume", "reason": "x"}]
        filtered, _ = apply_manual_override_guards(actions, state, config=cfg, now=now)
        self.assertEqual(len(filtered), 1)


class TestHumanHoldTopsAutomaticRules(unittest.TestCase):
    def test_human_on_blocks_night_lock_and_min_soc(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "resume",
                    "manual_hold_until": (now + timedelta(hours=2)).isoformat(),
                    "manual_hold_reason": "fremd_ein",
                }
            }
        }
        cfg = _cfg()
        actions = [
            {
                "ip": "10.0.0.1",
                "action": "pause",
                "reason": "Nacht-Sperre",
                "force": True,
                "emergency": True,
                "source": "night_lock",
            },
            {
                "ip": "10.0.0.1",
                "action": "pause",
                "reason": "Mindest-SoC unterschritten",
                "force": True,
                "emergency": True,
                "source": "min_soc",
            },
        ]
        filtered, msgs = apply_manual_override_guards(
            actions, state, config=cfg, now=now
        )
        self.assertEqual(filtered, [])
        self.assertTrue(any("Vorrang" in m for m in msgs))

    def test_human_off_blocks_max_soc_resume(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "last_action": "pause",
                    "manual_hold_until": (now + timedelta(hours=2)).isoformat(),
                    "manual_hold_reason": "fremd_aus",
                }
            }
        }
        cfg = _cfg(manual_override_hold_hours=2.0)
        actions = [
            {
                "ip": "10.0.0.1",
                "action": "resume",
                "reason": "Max-SoC 100%",
                "force": True,
                "emergency": True,
                "source": "max_soc_protection",
            }
        ]
        filtered, msgs = apply_manual_override_guards(
            actions, state, config=cfg, now=now
        )
        self.assertEqual(filtered, [])
        self.assertTrue(msgs)


class TestSettleWindow(unittest.TestCase):
    def test_no_hold_shortly_after_algo_command(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {"last_action": "resume", "last_action_at": now.isoformat()},
            }
        }
        # Status noch nicht nachgezogen (Lag) → würde sonst als fremd_aus gelten
        live = {"10.0.0.1": "paused"}
        cfg = _cfg()
        last_times = {"10.0.0.1": now.timestamp() - 30.0}  # vor 30 s
        state, msgs = detect_and_update_manual_holds(
            state,
            live,
            config=cfg,
            now=now,
            last_action_times=last_times,
            settle_seconds=90.0,
        )
        self.assertFalse(
            is_manual_hold_active(state["device_runtime"]["10.0.0.1"], now)
        )
        self.assertEqual(msgs, [])

    def test_hold_after_settle(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {
            "device_runtime": {
                "10.0.0.1": {"last_action": "resume"},
            }
        }
        live = {"10.0.0.1": "paused"}
        cfg = _cfg()
        last_times = {"10.0.0.1": now.timestamp() - 120.0}
        state, msgs = detect_and_update_manual_holds(
            state,
            live,
            config=cfg,
            now=now,
            last_action_times=last_times,
            settle_seconds=90.0,
        )
        self.assertTrue(is_manual_hold_active(state["device_runtime"]["10.0.0.1"], now))
        self.assertTrue(msgs)


class TestNoLastAction(unittest.TestCase):
    def test_no_false_positive_without_last_action(self):
        now = datetime(2026, 9, 28, 12, 0, 0)
        state = {"device_runtime": {"10.0.0.1": {}}}
        live = {"10.0.0.1": "paused"}
        state, msgs = detect_and_update_manual_holds(
            state, live, config=_cfg(), now=now, last_action_times={}
        )
        self.assertFalse(
            is_manual_hold_active(state["device_runtime"]["10.0.0.1"], now)
        )
        self.assertEqual(msgs, [])


if __name__ == "__main__":
    unittest.main()
