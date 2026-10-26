# -*- coding: utf-8 -*-
"""fill_urgency bei Über-Max / Kapazitätsgrenze auch ohne pausierte Flex-Lasten."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from control_algorithm.fill_schedule import compute_fill_urgency
from control_algorithm.load_scheduler import plan_load_actions
from control_algorithm.switch_commitment import state_has_fill_max_soc_urgency


class TestFillUrgencyOverMax(unittest.TestCase):
    def test_capacity_limited_without_flex_is_urgent(self):
        """Bug: früher False wenn alles schon läuft → Morgen-Floor schaltete ab."""
        plan = {
            "ok": True,
            "capacity_limited": True,
            "over_max": True,
            "excess_charge_kw": 45.9,
            "charge_kw": 45.9,
            "flex": None,
            "flex_stack": [],
        }
        self.assertTrue(compute_fill_urgency(plan, {}))
        self.assertTrue(
            state_has_fill_max_soc_urgency({"fill_schedule": plan}, {})
        )

    def test_no_urgency_without_signals(self):
        plan = {
            "ok": True,
            "capacity_limited": False,
            "over_max": False,
            "excess_charge_kw": 0.0,
            "flex": {"should_start_now": False},
            "flex_stack": [],
        }
        self.assertFalse(compute_fill_urgency(plan, {}))


class TestNoMorningShedWhenOverMaxProducing(unittest.TestCase):
    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=5000.0)
    def test_over_max_full_pv_skips_morning_floor_shed(self, _surplus):
        """SOC über Max + volle PV + capacity_limited → keine Morgen-Floor-Pause."""
        now = datetime(2026, 8, 12, 16, 24, 0)
        horizon_end = (now + timedelta(hours=60)).isoformat()
        projection = {
            "target_soc": 46.0,
            "projected_soc": 41.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 60.0,
            "miner_budget_kw": 31.6,
            "binding_constraint": "discharge_above_max",
            "pv_yield_tomorrow_kwh": 800.0,
            "near_term_plan": {"sustained_budget_kw": 11.0},
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 46.0,
                    "projected_soc": 41.0,
                    "reachable": False,
                    "minutes_until": 14 * 60,
                }
            ],
        }
        devices = [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": 6},
            {"ip": "10.0.0.4", "enabled": True, "power_kw": 5.5, "priority": 5},
            {"ip": "10.0.0.5", "enabled": True, "power_kw": 3.2, "priority": 1.5},
            {"ip": "10.0.0.6", "enabled": True, "power_kw": 3.2, "priority": 1},
            {"ip": "10.0.0.7", "enabled": True, "power_kw": 3.2, "priority": 0.5},
        ]
        config = {
            "enabled": True,
            "min_soc_percent": 46.0,
            "morning_soc_percent": 46.0,
            "max_soc_percent": 86.0,
            "storage_kwh": 1100.0,
            "risk_factor_percent": 85.0,
            "min_device_runtime_hours": 0.5,
            "soc_targets": [{"time": "06:00", "soc_percent": 46.0}],
            "devices": devices,
        }
        fill = {
            "ok": True,
            "soc_percent": 94.0,
            "max_soc_percent": 86.0,
            "capacity_limited": True,
            "over_max": True,
            "excess_charge_kw": 45.9,
            "charge_kw": 45.9,
            "desired_charge_kw": 0.0,
            "pv_kw": 76.0,
            "flex": None,
            "flex_stack": [],
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 800.0,
            "export_guard_active": True,
            "export_guard_floor_kw": 11.0,
            "device_runtime": {},
            "fill_schedule": fill,
        }
        live = {
            "soc_percent": 94.0,
            "pv_kw": 76.0,
            "grid_kw": -1.8,
            "verbrauch_kw": 35.0,
            "miner_status": {d["ip"]: "running" for d in devices},
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=1.8
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.05
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 5000.0, 800.0, 1.0
            )

        pauses = [a for a in actions if a.get("action") == "pause"]
        morning_pauses = [
            a for a in pauses if a.get("morning_floor") or a.get("budget_shed")
        ]
        self.assertFalse(
            morning_pauses,
            f"keine Morgen-Floor-Pause erwartet, got {pauses}\n{msgs}",
        )
        joined = " ".join(msgs)
        self.assertTrue(
            state.get("fill_urgency_active")
            or state.get("max_soc_protection_active")
            or "Max-SoC" in joined,
            msgs,
        )
        self.assertTrue(
            "keine Absorb-Kürzung" in joined
            or "Morgen-Cap ausgesetzt" in joined
            or "Max-SoC" in joined
            or "Füll" in joined,
            msgs,
        )

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=5000.0)
    def test_over_max_resumes_previous_morning_floor_shed(self, _surplus):
        """Nach falschem Floor-Shed: bei Über-Max wieder einschalten."""
        now = datetime(2026, 8, 12, 16, 30, 0)
        horizon_end = (now + timedelta(hours=60)).isoformat()
        projection = {
            "target_soc": 46.0,
            "projected_soc": 41.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 60.0,
            "miner_budget_kw": 31.6,
            "binding_constraint": "discharge_above_max",
            "pv_yield_tomorrow_kwh": 800.0,
            "near_term_plan": {"sustained_budget_kw": 11.0},
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 46.0,
                    "projected_soc": 41.0,
                    "reachable": False,
                    "minutes_until": 14 * 60,
                }
            ],
        }
        devices = [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 5},
            {"ip": "10.0.0.3", "enabled": True, "power_kw": 3.2, "priority": 1.5},
            {"ip": "10.0.0.4", "enabled": True, "power_kw": 3.2, "priority": 0.5},
        ]
        config = {
            "enabled": True,
            "min_soc_percent": 46.0,
            "morning_soc_percent": 46.0,
            "max_soc_percent": 86.0,
            "storage_kwh": 1100.0,
            "risk_factor_percent": 85.0,
            "min_device_runtime_hours": 0.05,
            "soc_targets": [{"time": "06:00", "soc_percent": 46.0}],
            "devices": devices,
        }
        fill = {
            "ok": True,
            "soc_percent": 94.0,
            "max_soc_percent": 86.0,
            "capacity_limited": False,
            "over_max": True,
            "excess_charge_kw": 45.9,
            "charge_kw": 45.9,
            "desired_charge_kw": 0.0,
            "pv_kw": 76.0,
            "flex": {
                "ip": "10.0.0.3",
                "power_kw": 3.2,
                "should_start_now": True,
                "overflow_kwh": 100.0,
            },
            "flex_stack": [
                {"ip": "10.0.0.3", "power_kw": 3.2, "should_start_now": True},
                {"ip": "10.0.0.4", "power_kw": 3.2, "should_start_now": True},
            ],
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 800.0,
            "export_guard_active": True,
            "export_guard_floor_kw": 31.6,
            "morning_floor_threatened": True,
            "fill_schedule": fill,
            "device_runtime": {
                "10.0.0.3": {
                    "last_pause_kind": "morning_floor",
                    "last_action": "pause",
                    "committed_off_until": (now + timedelta(hours=2)).isoformat(),
                    "last_off_at": (now - timedelta(minutes=10)).isoformat(),
                },
                "10.0.0.4": {
                    "last_pause_kind": "morning_floor",
                    "last_action": "pause",
                    "committed_off_until": (now + timedelta(hours=2)).isoformat(),
                    "last_off_at": (now - timedelta(minutes=10)).isoformat(),
                },
            },
        }
        live = {
            "soc_percent": 94.0,
            "pv_kw": 76.0,
            "grid_kw": -1.8,
            "verbrauch_kw": 25.0,
            "battery_power_kw": 45.9,
            "miner_status": {
                "10.0.0.1": "running",
                "10.0.0.2": "running",
                "10.0.0.3": "paused",
                "10.0.0.4": "paused",
            },
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=1.8
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.05
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 5000.0, 800.0, 1.0
            )

        resumes = [a for a in actions if a.get("action") == "resume"]
        self.assertTrue(resumes, f"erwartet Resume nach Floor-Shed, got {actions}\n{msgs}")
        resumed_ips = {a["ip"] for a in resumes}
        self.assertTrue(
            resumed_ips & {"10.0.0.3", "10.0.0.4"},
            f"erwartet 10.0.0.3/4, got {resumes}\n{msgs}",
        )
        joined = " ".join(msgs)
        self.assertTrue(
            "vorheriger Floor-Shed" in joined
            or "Max-SoC-Füll" in joined
            or "keine Absorb-Kürzung" in joined
            or "Max-SoC-Override" in joined
            or "Max-SoC" in joined,
            msgs,
        )


class TestMaxSocOverrideBeatsMorningFloor(unittest.TestCase):
    """Sobald SOC ≥ Max: niedrige Prio ein; unter Max−0,5: Algo wieder Kontrolle."""

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=776.0)
    def test_over_max_resumes_low_prio_despite_morning_floor_cap(self, _surplus):
        now = datetime(2026, 8, 27, 15, 39, 0)
        horizon_end = (now + timedelta(hours=168)).isoformat()
        projection = {
            "target_soc": 33.0,
            "projected_soc": 0.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 168.0,
            "miner_budget_kw": 9.0,
            "binding_constraint": "discharge_above_max",
            "pv_yield_tomorrow_kwh": 80.5,
            "near_term_plan": {"sustained_budget_kw": 0.666},
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 33.0,
                    "projected_soc": 0.0,
                    "reachable": False,
                    "target_datetime": "2026-08-28T06:00:00",
                    "minutes_until": 14 * 60,
                    "dynamic_reason": "autonomous_min_soc",
                }
            ],
        }
        devices = [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "192.168.1.232", "enabled": True, "power_kw": 3.5, "priority": 2},
        ]
        config = {
            "enabled": True,
            "min_soc_percent": 30.0,
            "morning_soc_percent": 33.0,
            "max_soc_percent": 95.0,
            "storage_kwh": 41.0,
            "risk_factor_percent": 100.0,
            "min_device_runtime_hours": 1.0,
            "min_device_off_hours": 1.0,
            "devices": devices,
        }
        fill = {
            "ok": True,
            "soc_percent": 98.0,
            "max_soc_percent": 95.0,
            "capacity_limited": False,
            "over_max": True,
            "excess_charge_kw": 0.89,
            "charge_kw": 0.89,
            "desired_charge_kw": 0.0,
            "pv_kw": 6.15,
            "hours_to_max": 0.0,
            "flex": {
                "ip": "192.168.1.232",
                "power_kw": 3.5,
                "should_start_now": True,
                "overflow_kwh": 2.98,
            },
            "flex_stack": [
                {
                    "ip": "192.168.1.232",
                    "power_kw": 3.5,
                    "should_start_now": True,
                    "reason": "Flex-Stack gegen Max-SoC",
                }
            ],
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 80.5,
            "export_guard_active": False,
            "fill_schedule": fill,
            "device_runtime": {},
        }
        live = {
            "soc_percent": 98.0,
            "pv_kw": 15.0,  # 80 % = 12 kW; 5.5 + 3.5 passen
            "grid_kw": 0.0,
            "verbrauch_kw": 5.22,
            "battery_power_kw": 2.0,
            "miner_status": {
                "192.0.2.10": "running",
                "192.168.1.232": "paused",
            },
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=0.0
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.03
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False, "flex_ips": ["192.168.1.232"]},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 84.0, 80.5, 1.0
            )

        self.assertTrue(state.get("fill_urgency_active"), msgs)
        self.assertTrue(state.get("max_soc_override_active"), msgs)
        resumes = [a for a in actions if a.get("action") == "resume"]
        self.assertTrue(
            any(a.get("ip") == "192.168.1.232" for a in resumes),
            f"erwartet Luftgekühlt-Resume trotz Morgen-Floor, got {actions}\n{msgs}",
        )
        # Kein Cap-Kürzungs-Resume-Block
        joined = " ".join(msgs)
        self.assertNotIn("unterdrückt – Morgen-Floor", joined, msgs)

    def test_latch_holds_until_release_soc(self):
        from control_algorithm.fill_schedule import (
            apply_max_soc_override_latch,
            sync_fill_urgency_from_plan,
        )

        state: dict = {
            "max_soc_override_active": True,
            "fill_urgency_active": True,
            "fill_schedule": {"over_max": False, "capacity_limited": False},
        }
        config = {"max_soc_percent": 95.0, "min_soc_percent": 30.0, "max_soc_release_percent": 90.0}
        active = apply_max_soc_override_latch(
            state, config, soc=94.4, over_max=False
        )
        self.assertTrue(active)
        self.assertTrue(state.get("max_soc_override_active"))
        self.assertFalse(state.get("max_soc_hold_released"))
        active = apply_max_soc_override_latch(
            state, config, soc=90.0, over_max=False
        )
        self.assertFalse(active)
        self.assertFalse(state.get("max_soc_override_active"))
        self.assertTrue(state.get("max_soc_override_just_cleared"))
        self.assertTrue(state.get("max_soc_hold_released"))
        self.assertFalse(state.get("fill_urgency_active"))
        # Sync darf Urgency nicht wieder ankleben ohne Plan-Signal
        self.assertFalse(sync_fill_urgency_from_plan(state, config))

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=50.0)
    def test_under_max_with_low_anomaly_still_sheds_anchor(self, _surplus):
        """Unter Max + anomaly_low darf Morgen-Floor Hydro abschalten (kein Early-Return)."""
        now = datetime(2026, 8, 27, 18, 0, 0)
        horizon_end = (now + timedelta(hours=48)).isoformat()
        projection = {
            "target_soc": 33.0,
            "projected_soc": 10.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 48.0,
            "miner_budget_kw": 0.8,
            "binding_constraint": "min_soc",
            "pv_yield_tomorrow_kwh": 70.0,
            "near_term_plan": {"sustained_budget_kw": 0.8},
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 33.0,
                    "projected_soc": 10.0,
                    "reachable": False,
                    "target_datetime": "2026-08-28T06:00:00",
                    "minutes_until": 12 * 60,
                }
            ],
        }
        devices = [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "192.168.1.232", "enabled": True, "power_kw": 3.5, "priority": 2},
        ]
        config = {
            "enabled": True,
            "min_soc_percent": 30.0,
            "morning_soc_percent": 33.0,
            "max_soc_percent": 95.0,
            "storage_kwh": 41.0,
            "risk_factor_percent": 100.0,
            "min_device_runtime_hours": 0.05,
            "min_device_off_hours": 0.05,
            "devices": devices,
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 70.0,
            "anomaly_low_active": True,
            "anomaly_deficit_pct": 76.0,
            "morning_floor_threatened": True,
            "fill_urgency_active": True,  # sticky vom Über-Max – muss gelöscht werden
            "max_soc_override_active": False,
            "device_runtime": {
                "192.0.2.10": {
                    "last_action": "resume",
                    "resumed_at": (now - timedelta(hours=8)).isoformat(),
                }
            },
        }
        live = {
            "soc_percent": 77.0,
            "pv_kw": 0.9,
            "grid_kw": 0.0,
            "verbrauch_kw": 1.0,
            "battery_power_kw": -4.0,
            "miner_status": {
                "192.0.2.10": "running",
                "192.168.1.232": "paused",
            },
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=0.0
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.03
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 84.0, 70.0, 1.0
            )

        self.assertFalse(state.get("fill_urgency_active"), msgs)
        pauses = [a for a in actions if a.get("action") == "pause"]
        self.assertTrue(
            any(a.get("ip") == "192.0.2.10" for a in pauses),
            f"erwartet Hydro-Pause unter Max bei Morgen-Floor, got {actions}\n{msgs}",
        )

    def test_arbiter_fill_urgency_beats_morning_floor_pause(self):
        from control_algorithm.switch_commitment import _same_tick_arbiter

        actions = [
            {
                "ip": "192.168.1.232",
                "action": "pause",
                "morning_floor": True,
                "budget_shed": True,
                "reason": "Morgen-Floor",
                "load_overhang_kw": 5.0,
            },
            {
                "ip": "192.168.1.232",
                "action": "resume",
                "fill_urgency": True,
                "urgent": True,
                "reason": "Flex gegen Max-SoC",
            },
        ]
        out = _same_tick_arbiter(
            actions,
            {},
            state={
                "max_soc_override_active": True,
                "fill_schedule": {"over_max": True},
            },
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "resume")
        self.assertTrue(out[0].get("fill_urgency"))


    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=776.0)
    def test_over_max_resumes_despite_low_anomaly_and_morning_floor(self, _surplus):
        """Regression: anomaly_low + Morgen-Floor Early-Return blockierte Über-Max."""
        now = datetime(2026, 8, 27, 15, 39, 0)
        horizon_end = (now + timedelta(hours=168)).isoformat()
        projection = {
            "target_soc": 33.0,
            "projected_soc": 0.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 168.0,
            "miner_budget_kw": 9.0,
            "binding_constraint": "discharge_above_max",
            "pv_yield_tomorrow_kwh": 80.5,
            "near_term_plan": {"sustained_budget_kw": 0.666},
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 33.0,
                    "projected_soc": 0.0,
                    "reachable": False,
                    "target_datetime": "2026-08-28T06:00:00",
                    "minutes_until": 14 * 60,
                }
            ],
        }
        devices = [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "192.168.1.232", "enabled": True, "power_kw": 3.5, "priority": 2},
        ]
        config = {
            "enabled": True,
            "min_soc_percent": 30.0,
            "morning_soc_percent": 33.0,
            "max_soc_percent": 95.0,
            "storage_kwh": 41.0,
            "risk_factor_percent": 100.0,
            "min_device_runtime_hours": 0.05,
            "min_device_off_hours": 0.05,
            "devices": devices,
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 80.5,
            "anomaly_low_active": True,
            "anomaly_deficit_pct": 85.0,
            "morning_floor_threatened": True,
            "device_runtime": {},
        }
        live = {
            "soc_percent": 98.0,
            "pv_kw": 15.0,  # 80 % = 12 kW; 5.5 + 3.5 passen
            "grid_kw": 0.0,
            "verbrauch_kw": 5.22,
            "battery_power_kw": 0.89,
            "miner_status": {
                "192.0.2.10": "running",
                "192.168.1.232": "paused",
            },
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=0.0
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.03
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False, "flex_ips": ["192.168.1.232"]},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 84.0, 80.5, 1.0
            )

        self.assertTrue(state.get("max_soc_override_active"), msgs)
        self.assertTrue(
            any(
                a.get("action") == "resume" and a.get("ip") == "192.168.1.232"
                for a in actions
            ),
            f"erwartet Resume trotz anomaly_low+Morgen-Floor, got {actions}\n{msgs}",
        )


class TestFillAfterPvEnd(unittest.TestCase):
    def test_plan_band_does_not_start_miners_after_pv_end(self):
        """91 % nachts bei Band 89 % / hartem Max 95 % → keine Füll-Resume."""
        from control_algorithm.fill_schedule import (
            compute_fill_schedule,
            fill_schedule_resume_actions,
        )

        now = datetime(2026, 10, 1, 18, 39, 0)
        devices = [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.8, "priority": 10},
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 1.6, "priority": 3},
        ]
        config = {
            "storage_kwh": 41.0,
            "max_soc_percent": 95.0,
            "min_soc_percent": 30.0,
            "morning_soc_percent": 33.0,
            "devices": devices,
        }
        state = {
            "steering_learning": {"params": {"band_max_target_pct": 89.0}},
            "morning_drain_plan": {
                "ok": True,
                "gate_active": True,
                "allowed_ips": [],
                "g_kwh": -150.0,
            },
        }
        live = {
            "soc_percent": 91.0,
            "pv_kw": 0.0,
            "battery_power_kw": -0.4,
            "miner_status": {},
        }
        with patch(
            "control_algorithm.fill_schedule._pv_end_datetime",
            return_value=(now, 0.0, "PV-Ende erreicht"),
        ):
            plan = compute_fill_schedule(config, state, live, now=now)
            self.assertTrue(plan.get("pv_ended"))
            self.assertFalse(plan.get("over_max"))
            self.assertFalse(plan.get("fill_now"))
            self.assertEqual(plan.get("desired_charge_kw"), 0.0)
            fa, _, _ = fill_schedule_resume_actions(
                config, state, live, {}, 0.0
            )
        self.assertEqual(fa, [])


if __name__ == "__main__":
    unittest.main()
