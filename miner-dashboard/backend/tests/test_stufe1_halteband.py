# -*- coding: utf-8 -*-
"""Stufe 1: nach Max-SoC an bleiben, am Halt-SoC alles aus bis wieder Max."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from control_algorithm.fill_schedule import apply_max_soc_override_latch
from control_algorithm.load_scheduler import plan_load_actions


def _config(**over):
    cfg = {
        "enabled": True,
        "min_soc_percent": 30.0,
        "max_soc_percent": 95.0,
        "max_soc_release_percent": 90.0,
        "morning_soc_percent": 33.0,
        "storage_kwh": 41.0,
        "min_device_runtime_hours": 1.0,
        "devices": [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 6.0, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 1.7, "priority": 3},
        ],
    }
    cfg.update(over)
    return cfg


def _run(config, live, state, fill=None):
    plan = fill or {
        "ok": True,
        "over_max": False,
        "capacity_limited": False,
        "excess_charge_kw": 0.0,
        "charge_kw": 0.0,
        "desired_charge_kw": 0.0,
        "flex": {"should_start_now": True, "ip": "10.0.0.1", "power_kw": 6.0},
        "flex_stack": [
            {"ip": "10.0.0.1", "power_kw": 6.0},
        ],
    }
    with patch(
        "control_algorithm.fill_schedule.compute_fill_schedule",
        return_value=plan,
    ), patch(
        "control_algorithm.load_scheduler.miner_surplus_kwh",
        return_value=20.0,
    ):
        return plan_load_actions(config, live, state, "/tmp", 40.0, 10.0, 1.0)


class LatchTests(unittest.TestCase):
    def test_stays_on_between_max_and_hold_then_releases(self):
        state = {"max_soc_override_active": True, "fill_urgency_active": True}
        config = _config()
        self.assertTrue(
            apply_max_soc_override_latch(state, config, soc=92.0, over_max=False)
        )
        self.assertFalse(state.get("max_soc_hold_released"))

        self.assertFalse(
            apply_max_soc_override_latch(state, config, soc=90.0, over_max=False)
        )
        self.assertTrue(state.get("max_soc_hold_released"))
        self.assertFalse(state.get("max_soc_override_active"))

    def test_max_clears_hold(self):
        state = {"max_soc_hold_released": True, "max_soc_override_active": False}
        self.assertTrue(
            apply_max_soc_override_latch(state, _config(), soc=95.0, over_max=False)
        )
        self.assertFalse(state.get("max_soc_hold_released"))
        self.assertTrue(state.get("max_soc_override_active"))


class PlanTests(unittest.TestCase):
    def test_over_max_starts_highest_priority_first(self):
        live = {
            "soc_percent": 96.0,
            "pv_kw": 6.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        actions, _msgs = _run(_config(), live, {})
        resumes = [a["ip"] for a in actions if a.get("action") == "resume"]
        self.assertEqual(resumes, ["10.0.0.1"])
        self.assertFalse(any(a.get("action") == "pause" for a in actions))

    def test_between_hold_and_max_stays_on(self):
        live = {
            "soc_percent": 92.0,
            "pv_kw": 4.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
        }
        state = {"max_soc_override_active": True}
        actions, _msgs = _run(_config(), live, state)
        self.assertTrue(state.get("max_soc_override_active"))
        self.assertFalse(state.get("max_soc_hold_released"))
        self.assertFalse(any(a.get("action") == "pause" for a in actions))

    def test_at_hold_turns_all_off(self):
        live = {
            "soc_percent": 90.0,
            "pv_kw": 3.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
        }
        state = {"max_soc_override_active": True}
        actions, _msgs = _run(_config(), live, state)
        pauses = [a for a in actions if a.get("action") == "pause"]
        self.assertEqual({a["ip"] for a in pauses}, {"10.0.0.1", "10.0.0.2"})
        self.assertTrue(all(a.get("source") == "max_soc_hold" and a.get("force") for a in pauses))
        self.assertTrue(state.get("max_soc_hold_released"))
        self.assertFalse(state.get("max_soc_override_active"))

    def test_after_release_no_fill_resume(self):
        live = {
            "soc_percent": 91.0,
            "pv_kw": 5.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        state = {"max_soc_hold_released": True, "max_soc_override_active": False}
        actions, _msgs = _run(_config(), live, state)
        self.assertFalse(any(a.get("action") == "resume" for a in actions))
        self.assertTrue(state.get("max_soc_hold_released"))

    def test_max_again_turns_on(self):
        live = {
            "soc_percent": 95.0,
            "pv_kw": 6.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        state = {"max_soc_hold_released": True}
        actions, _msgs = _run(_config(), live, state)
        resumes = [a["ip"] for a in actions if a.get("action") == "resume"]
        self.assertIn("10.0.0.1", resumes)
        self.assertFalse(state.get("max_soc_hold_released"))

    def test_night_lock_does_not_block_turn_on_at_99(self):
        """Über Max-SoC schaltet die harte Grenze ein, die Nachtsperre nicht aus."""
        live = {
            "soc_percent": 99.0,
            "pv_kw": 12.0,  # 80 % = 9.6; 6.0 + 1.7 passen
            "battery_power_kw": 2.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        state = {
            "night_lock_active": True,
            "night_lock_force": True,
            "night_lock_reason": "Min-SoC-Gefahr",
        }
        actions, _msgs = _run(_config(), live, state)
        self.assertFalse(any(a.get("action") == "pause" for a in actions))
        resumes = [a["ip"] for a in actions if a.get("action") == "resume"]
        self.assertIn("10.0.0.2", resumes)


class FormBoundTests(unittest.TestCase):
    def _save(self, release, max_soc="95", min_soc="30"):
        from control_algorithm_web import save_from_form

        form = {
            "run_mode": "active",
            "city": "Beispiel",
            "has_storage": "1",
            "storage_kwh": "41",
            "has_pv": "1",
            "pv_layout": "east_west",
            "pv_kwp_east": "8",
            "pv_kwp_west": "8",
            "min_soc_percent": min_soc,
            "max_soc_percent": max_soc,
            "max_soc_release_percent": release,
            "soc_targets_json": "[]",
        }
        base = {
            "city": "Beispiel",
            "lat": 46.75,
            "lon": 14.03,
            "city_display": "Beispiel",
            "devices": [],
        }
        with patch("control_algorithm_web.load_config", return_value=dict(base)), patch(
            "control_algorithm_web.geocode_city", return_value=(46.75, 14.03, "Beispiel")
        ), patch("control_algorithm_web.save_config"), patch(
            "control_algorithm_web.load_miners", return_value=[]
        ), patch("control_algorithm_web.sync_total_kwp_in_config"):
            return save_from_form(form)

    def test_rejects_hold_at_or_above_max(self):
        _cfg, err = self._save("95")
        self.assertIn("Halt-SoC", err)
        _cfg, err = self._save("96")
        self.assertIn("Halt-SoC", err)

    def test_rejects_hold_at_or_below_min(self):
        _cfg, err = self._save("30")
        self.assertIn("Halt-SoC", err)

    def test_accepts_hold_between(self):
        cfg, err = self._save("90")
        self.assertEqual(err, "")
        self.assertEqual(cfg["max_soc_release_percent"], 90.0)

    def test_higher_max_widens_band_release_stays(self):
        """Max 98, Halt 90: Spanne wird 8 Punkte, die Unterkante bleibt 90."""
        cfg, err = self._save("90", max_soc="98")
        self.assertEqual(err, "")
        self.assertEqual(cfg["max_soc_percent"], 98.0)
        self.assertEqual(cfg["max_soc_release_percent"], 90.0)

        from control_algorithm.battery_protection import max_soc_release_percent

        release = max_soc_release_percent(cfg)
        self.assertEqual(release, 90.0)
        self.assertEqual(cfg["max_soc_percent"] - release, 8.0)

        runtime = _config(max_soc_percent=98.0, max_soc_release_percent=90.0)
        state = {"max_soc_override_active": True}
        self.assertTrue(
            apply_max_soc_override_latch(state, runtime, soc=92.0, over_max=False)
        )
        self.assertFalse(state.get("max_soc_hold_released"))
        self.assertFalse(
            apply_max_soc_override_latch(state, runtime, soc=90.0, over_max=False)
        )
        self.assertTrue(state.get("max_soc_hold_released"))

        # Unter dem neuen Max bleibt alles aus – auch bei 95, dem alten Maximum.
        live = {
            "soc_percent": 95.0,
            "pv_kw": 6.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        actions, _msgs = _run(runtime, live, {"max_soc_hold_released": True})
        self.assertFalse(any(a.get("action") == "resume" for a in actions))

        live = {
            "soc_percent": 98.0,
            "pv_kw": 6.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"},
        }
        held = {"max_soc_hold_released": True}
        actions, _msgs = _run(runtime, live, held)
        self.assertTrue(any(a.get("action") == "resume" for a in actions))
        self.assertFalse(held.get("max_soc_hold_released"))

    def test_lower_release_drains_further(self):
        """Halt 80 bei Max 95: Geräte bleiben an, bis der Speicher auf 80 fällt."""
        cfg, err = self._save("80")
        self.assertEqual(err, "")
        runtime = _config(max_soc_percent=95.0, max_soc_release_percent=80.0)
        state = {"max_soc_override_active": True}
        self.assertTrue(
            apply_max_soc_override_latch(state, runtime, soc=85.0, over_max=False)
        )
        self.assertFalse(state.get("max_soc_hold_released"))
        self.assertFalse(
            apply_max_soc_override_latch(state, runtime, soc=80.0, over_max=False)
        )
        self.assertTrue(state.get("max_soc_hold_released"))

        live = {
            "soc_percent": 80.0,
            "pv_kw": 1.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
        }
        held = {"max_soc_override_active": True}
        actions, _msgs = _run(runtime, live, held)
        pauses = [a for a in actions if a.get("action") == "pause"]
        self.assertEqual({a["ip"] for a in pauses}, {"10.0.0.1", "10.0.0.2"})
        self.assertTrue(held.get("max_soc_hold_released"))


class PvHoldKeepOnTests(unittest.TestCase):
    def _state(self, target=88.0):
        return {
            "max_soc_hold_released": True,
            "planning_target": {"soc_percent": target, "signature": "x"},
        }

    def test_stays_on_when_pv_covers_half_above_target(self):
        live = {
            "soc_percent": 90.0,
            "pv_kw": 4.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        actions, msgs = _run(_config(max_soc_under_target_percent=5), live, self._state())
        self.assertFalse(any(a.get("action") == "pause" for a in actions))
        self.assertTrue(any("bleibt an" in m for m in msgs))

    def test_shuts_off_inside_tolerance_when_pv_is_low(self):
        live = {
            "soc_percent": 86.0,
            "pv_kw": 1.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        actions, _msgs = _run(_config(max_soc_under_target_percent=5), live, self._state())
        self.assertTrue(any(a.get("action") == "pause" for a in actions))

    def test_shuts_off_below_target_minus_tolerance(self):
        live = {
            "soc_percent": 82.0,
            "pv_kw": 10.0,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"},
        }
        actions, _msgs = _run(_config(max_soc_under_target_percent=5), live, self._state())
        self.assertTrue(any(a.get("source") == "max_soc_hold" for a in actions))

    def test_tiny_miner_load_still_shuts_off(self):
        cfg = _config(max_soc_under_target_percent=5)
        cfg["devices"] = [
            {"ip": "10.0.0.9", "enabled": True, "power_kw": 0.4, "priority": 1},
        ]
        live = {
            "soc_percent": 90.0,
            "pv_kw": 8.0,
            "miner_status": {"10.0.0.9": "running"},
        }
        actions, _msgs = _run(cfg, live, self._state())
        self.assertTrue(any(a.get("action") == "pause" for a in actions))
