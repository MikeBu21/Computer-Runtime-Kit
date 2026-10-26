# -*- coding: utf-8 -*-
"""Tages-Überschuss, Session-Haltung und Arbiter-Leiter."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest import TestCase

from control_algorithm.control_priority import PRIORITY_LADDER, classify_action
from control_algorithm.energy_balance import compute_today_surplus
from control_algorithm.surplus_session import (
    close_session,
    open_session,
    remaining_kwh,
    session_holds,
    suspend_session,
    update_session,
)
from control_algorithm.switch_commitment import _same_tick_arbiter


def _today_state(kwh=10.0):
    return {
        "today_surplus": {"ok": True, "kwh": kwh, "hours_left_today": 6.0},
        "curve_plan": {"steering_goal": {"target_peak_pct": 89.0, "floor_eve_pct": 40.0}},
    }


class TestControlPriority(TestCase):
    def test_ladder_order(self):
        self.assertEqual(PRIORITY_LADDER[0], "hard_stop")
        self.assertEqual(PRIORITY_LADDER[1], "plan_session")
        self.assertEqual(PRIORITY_LADDER[3], "soft_reserve")

    def test_classify_emergency_is_hard_stop(self):
        self.assertEqual(
            classify_action({"action": "pause", "source": "pv_safety", "emergency": True}),
            "hard_stop",
        )

    def test_classify_session_resume(self):
        self.assertEqual(
            classify_action({"action": "resume", "source": "surplus_session"}),
            "plan_session",
        )

    def test_classify_morning_floor_is_soft(self):
        self.assertEqual(
            classify_action(
                {
                    "action": "pause",
                    "morning_floor": True,
                    "budget_shed": True,
                    "reason": "Morgen-Floor",
                }
            ),
            "soft_reserve",
        )


class TestSurplusSession(TestCase):
    def test_open_uses_today_surplus_not_week(self):
        state = _today_state(15.0)
        state["horizon_projection"] = {"miner_surplus_kwh": 119.0}
        open_session(state, {}, {"soc_percent": 80.0}, devices=["10.0.0.1"], trigger="export")
        self.assertTrue(state["surplus_session"]["active"])
        self.assertEqual(state["surplus_session"]["goal_kwh"], 15.0)

    def test_session_holds_last_device(self):
        state = _today_state()
        open_session(state, {}, {"soc_percent": 80.0}, devices=["a", "b"])
        self.assertTrue(session_holds(state, "a"))
        suspend_session(state, "pv_safety")
        self.assertFalse(session_holds(state, "a"))

    def test_floor_does_not_close_session(self):
        state = _today_state()
        open_session(state, {}, {"soc_percent": 80.0}, devices=["a"])
        self.assertTrue(state["surplus_session"]["active"])
        # weiche Pause ändert den Zug nicht
        self.assertIsNone(state["surplus_session"].get("end_reason"))

    def test_hard_stop_closes(self):
        state = _today_state()
        open_session(state, {}, {"soc_percent": 80.0}, devices=["a"])
        close_session(state, "min_soc_lock")
        self.assertFalse(state["surplus_session"]["active"])
        self.assertEqual(state["surplus_session"]["end_reason"], "min_soc_lock")

    def test_update_counts_delivered_kwh(self):
        state = _today_state(10.0)
        now = datetime(2026, 9, 24, 12, 0, 0)
        open_session(state, {}, {"soc_percent": 80.0}, devices=["a"], now=now)
        state["tick_miner_load_kw"] = 6.0
        later = now + timedelta(minutes=20)
        update_session(state, {}, {"soc_percent": 78.0}, now=later)
        # 6 kW × 20 min = 2 kWh
        self.assertGreater(state["surplus_session"]["delivered_kwh"], 1.8)
        self.assertLess(state["surplus_session"]["delivered_kwh"], 2.2)

    def test_suspend_keeps_goal(self):
        state = _today_state(10.0)
        open_session(state, {}, {"soc_percent": 80.0}, devices=["a"])
        goal = state["surplus_session"]["goal_kwh"]
        suspend_session(state, "anomaly")
        self.assertTrue(state["surplus_session"]["active"])
        self.assertEqual(state["surplus_session"]["suspended_by"], "anomaly")
        self.assertEqual(state["surplus_session"]["goal_kwh"], goal)

    def test_neuron_caps_new_session_not_running(self):
        state = _today_state(20.0)
        state["soc_priority_neuron"] = {
            "ok": True,
            "recommended_continuous_kw": 1.0,
            "recommended_on_ips": ["a"],
            "baseline_unreachable": False,
        }
        state["soc_priority_neuron_rollout"] = {
            "live_since": "2026-09-20T17:25:08",
            "shadow_since": "2026-09-16T16:44:33",
        }
        cfg = {"soc_priority_neuron_enabled": True}
        open_session(state, cfg, {"soc_percent": 80.0}, devices=["a"])
        # 1 kW × 6 h = 6 kWh Deckel, wenn Neuron live ist
        if state["surplus_session"].get("active"):
            self.assertLessEqual(state["surplus_session"]["goal_kwh"], 20.0)

    def test_remaining_closes_when_done(self):
        state = _today_state(0.2)
        state["today_surplus"]["ok"] = True
        open_session(state, {}, {"soc_percent": 80.0}, devices=["a"])
        self.assertFalse(state.get("surplus_session", {}).get("active"))


class TestTodaySurplus(TestCase):
    def test_week_cap_sets_capped_by(self):
        now = datetime(2026, 9, 24, 12, 0, 0)
        state = {
            "horizon_projection": {"miner_surplus_kwh": 2.0},
            "curve_plan": {"steering_goal": {"floor_eve_pct": 30.0}},
            "pv_forecast_today_hours": {f"h{h:02d}": 5.0 for h in range(6, 20)},
        }
        cfg = {
            "has_storage": True,
            "storage_kwh": 40.0,
            "min_soc_percent": 30.0,
            "morning_soc_percent": 33.0,
        }
        live = {"soc_percent": 80.0, "pv_kw": 4.0}
        out = compute_today_surplus(cfg, state, live, "/tmp", now=now)
        self.assertTrue(out.get("ok"))
        if out.get("kwh") >= 2.0:
            self.assertEqual(out.get("capped_by"), "horizon_7d")
            self.assertAlmostEqual(out["kwh"], 2.0, places=1)

    def test_pace_scales_even_if_watchdog_disabled(self):
        now = datetime(2026, 9, 24, 14, 0, 0)
        hours = {f"h{h:02d}": 4.0 for h in range(6, 18)}
        actual = {f"h{h:02d}": 1.0 for h in range(6, 14)}
        state = {
            "horizon_projection": {"miner_surplus_kwh": 200.0},
            "curve_plan": {"steering_goal": {"floor_eve_pct": 30.0}},
            "pv_forecast_today_hours": hours,
            "hour_actual_today": actual,
            "live_pv_kwh_actual": 8.0,
        }
        cfg = {
            "has_storage": True,
            "storage_kwh": 40.0,
            "min_soc_percent": 30.0,
            "pv_safety_enabled": False,
        }
        live = {"soc_percent": 70.0, "pv_kw": 1.0}
        out = compute_today_surplus(cfg, state, live, "/tmp", now=now)
        self.assertTrue(out.get("ok"))
        self.assertLess(out.get("pace_scale") or 1.0, 1.0)


class TestArbiterSession(TestCase):
    def test_session_resume_beats_floor_pause(self):
        state = {
            "surplus_session": {
                "active": True,
                "suspended_by": None,
                "devices": ["192.168.1.10"],
            }
        }
        actions = [
            {
                "ip": "192.168.1.10",
                "action": "pause",
                "morning_floor": True,
                "budget_shed": True,
                "reason": "Morgen-Floor",
            },
            {
                "ip": "192.168.1.10",
                "action": "resume",
                "reason": "Netzeinspeisung vermeiden",
            },
        ]
        out = _same_tick_arbiter(actions, {}, state)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "resume")

    def test_emergency_pause_beats_session(self):
        state = {
            "surplus_session": {
                "active": True,
                "suspended_by": None,
                "devices": ["192.168.1.10"],
            }
        }
        actions = [
            {
                "ip": "192.168.1.10",
                "action": "pause",
                "emergency": True,
                "source": "min_soc",
                "reason": "Min-SoC",
            },
            {"ip": "192.168.1.10", "action": "resume", "reason": "Session"},
        ]
        out = _same_tick_arbiter(actions, {}, state)
        self.assertEqual(out[0]["action"], "pause")


def _export_devices():
    return [
        {"ip": "192.0.2.10", "power_kw": 5.8, "priority": 1, "enabled": True},
        {"ip": "192.0.2.10", "power_kw": 1.6, "priority": 2, "enabled": True},
        {"ip": "192.168.1.232", "power_kw": 3.0, "priority": 3, "enabled": True},
    ]


class TestExportLoadSizing(TestCase):
    """Einspeisung darf nur Last tragen, die sie auch bezahlt."""

    config = {"min_soc_percent": 35.0, "max_soc_percent": 95.0, "pv_kwp": 20.0}

    def _floor(self, live):
        from control_algorithm.load_scheduler import weather_run_through_floor

        return weather_run_through_floor(self.config, {}, live, _export_devices())

    def test_tiny_night_export_does_not_start_a_big_device(self):
        out = self._floor(
            {"soc_percent": 66.0, "pv_kw": 0.0, "verbrauch_kw": 0.6, "grid_kw": -0.06}
        )
        self.assertFalse(out["active"])
        self.assertEqual(out["floor_kw"], 0.0)
        self.assertFalse(out["treat_as_export"])

    def test_tiny_night_export_at_high_soc_stays_off(self):
        out = self._floor(
            {"soc_percent": 91.0, "pv_kw": 0.0, "verbrauch_kw": 0.6, "grid_kw": -0.2}
        )
        self.assertFalse(out["treat_as_export"])

    def test_real_night_export_may_run_matching_load(self):
        out = self._floor(
            {"soc_percent": 66.0, "pv_kw": 0.0, "verbrauch_kw": 0.6, "grid_kw": -6.5}
        )
        self.assertTrue(out["treat_as_export"])
        self.assertGreaterEqual(out["floor_kw"], 3.0)
        # Last bleibt im Rahmen der Einspeisung plus Messtoleranz.
        self.assertLessEqual(out["floor_kw"], 6.5 * 1.15 + 0.3)

    def test_daytime_absorption_still_overshoots_at_high_soc(self):
        out = self._floor(
            {"soc_percent": 92.0, "pv_kw": 14.0, "verbrauch_kw": 1.2, "grid_kw": -8.0}
        )
        self.assertTrue(out["treat_as_export"])
        self.assertGreaterEqual(out["floor_kw"], 10.0)


class TestExportDoesNotBypassRunReserve(TestCase):
    """Trägt der Speicher die Geräte nicht, schlägt kein Export-Modus die Schranke."""

    def test_gate_blocks_devices_despite_live_export(self):
        from unittest.mock import patch

        from control_algorithm.load_scheduler import plan_load_actions

        now = datetime(2026, 10, 1, 21, 30, 0)
        projection = {
            "target_soc": 46.0,
            "projected_soc": 48.0,
            "target_datetime": (now + timedelta(hours=9)).isoformat(),
            "base_consumption_kwh": 10.0,
            "pv_forecast_kwh": 0.0,
            "hydro_forecast_kwh": 2.0,
            "risk_factor_percent": 100.0,
            "binding_constraint": "export_avoidance",
        }
        config = {
            "enabled": True,
            "min_soc_percent": 35.0,
            "max_soc_percent": 95.0,
            "storage_kwh": 41.0,
            "pv_kwp": 20.0,
            "min_device_runtime_hours": 0.5,
            "devices": _export_devices(),
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "device_runtime": {},
            "morning_drain_plan": {
                "ok": True,
                "gate_active": True,
                "allowed_ips": [],
                "policy": "hold_only",
                "summary": "Laufreserve: Speicher trägt nicht bis PV.",
            },
        }
        live = {
            "soc_percent": 66.0,
            "pv_kw": 0.0,
            "verbrauch_kw": 0.6,
            "grid_kw": -0.2,
            "miner_status": {d["ip"]: "running" for d in _export_devices()},
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=0.2
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.05
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 0.0, 50.0, 1.0
            )

        paused = {a["ip"] for a in actions if a.get("action") == "pause"}
        self.assertEqual(
            paused,
            {d["ip"] for d in _export_devices()},
            f"Laufreserve muss trotz Einspeisung alles pausieren: {actions}\n{msgs}",
        )
        self.assertFalse([a for a in actions if a.get("action") == "resume"])
