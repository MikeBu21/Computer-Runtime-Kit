# -*- coding: utf-8 -*-
"""Morgen-Floor gefährdet → niedrige Prio gestaffelt aus, trotz Export-Guard."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from control_algorithm.decision_arbitrator import (
    lower_export_guard_for_morning,
    update_export_guard,
)
from control_algorithm.load_scheduler import (
    morning_floor_threatened,
    plan_load_actions,
    sustainable_morning_cap_kw,
)


class TestMorningFloorDetection(unittest.TestCase):
    def test_morning_checkpoint_under_floor(self):
        threatened, proj, why = morning_floor_threatened(
            {
                "projected_soc": 50.0,
                "checkpoints": [
                    {
                        "time": "07:00",
                        "soc_percent": 45.0,
                        "projected_soc": 33.0,
                        "reachable": False,
                    }
                ],
            },
            min_soc=45.0,
            morning_floor=45.0,
        )
        self.assertTrue(threatened)
        self.assertEqual(proj, 33.0)
        self.assertEqual(why, "07:00")

    def test_ok_when_morning_above_floor(self):
        threatened, _, _ = morning_floor_threatened(
            {
                "projected_soc": 55.0,
                "all_checkpoints_ok": True,
                "checkpoints": [
                    {
                        "time": "07:00",
                        "soc_percent": 45.0,
                        "projected_soc": 52.0,
                        "reachable": True,
                    }
                ],
            },
            min_soc=45.0,
            morning_floor=45.0,
        )
        self.assertFalse(threatened)

    def test_day_plus_two_fail_ignored_when_next_morning_ok(self):
        """Nächster Morgen ok → kein Shed wegen Tag+2 (Entladen für starke PV)."""
        now = datetime(2026, 8, 12, 22, 0, 0)
        threatened, proj, why = morning_floor_threatened(
            {
                "projected_soc": 40.0,
                "all_checkpoints_ok": False,
                "reachable": False,
                "checkpoints": [
                    {
                        "time": "06:00",
                        "soc_percent": 46.0,
                        "projected_soc": 63.0,
                        "reachable": True,
                        "target_datetime": "2026-08-13T06:00:00",
                    },
                    {
                        "time": "06:00",
                        "soc_percent": 46.0,
                        "projected_soc": 40.0,
                        "reachable": False,
                        "target_datetime": "2026-08-14T06:00:00",
                    },
                ],
            },
            min_soc=46.0,
            morning_floor=46.0,
            now=now,
        )
        self.assertFalse(threatened, f"proj={proj} why={why}")

    def test_next_morning_fail_still_threatened(self):
        now = datetime(2026, 8, 12, 22, 0, 0)
        threatened, proj, why = morning_floor_threatened(
            {
                "projected_soc": 33.0,
                "all_checkpoints_ok": False,
                "checkpoints": [
                    {
                        "time": "06:00",
                        "soc_percent": 46.0,
                        "projected_soc": 33.0,
                        "reachable": False,
                        "target_datetime": "2026-08-13T06:00:00",
                    },
                    {
                        "time": "06:00",
                        "soc_percent": 46.0,
                        "projected_soc": 50.0,
                        "reachable": True,
                        "target_datetime": "2026-08-14T06:00:00",
                    },
                ],
            },
            min_soc=46.0,
            morning_floor=46.0,
            now=now,
        )
        self.assertTrue(threatened)
        self.assertEqual(proj, 33.0)
        self.assertEqual(why, "06:00")


class TestExportGuardLower(unittest.TestCase):
    def test_sticky_max_then_morning_lower(self):
        state: dict = {}
        update_export_guard(
            state,
            active=True,
            floor_kw=31.6,
            reason="export",
            soc=62.0,
            checkpoint_soc=45.0,
        )
        self.assertAlmostEqual(state["export_guard_floor_kw"], 31.6)
        update_export_guard(
            state,
            active=True,
            floor_kw=11.0,
            reason="try lower sticky",
            soc=62.0,
            checkpoint_soc=45.0,
        )
        # Sticky max hält 31.6
        self.assertAlmostEqual(state["export_guard_floor_kw"], 31.6)
        lower_export_guard_for_morning(
            state, floor_kw=11.0, reason="Morgen-Floor Cap"
        )
        self.assertAlmostEqual(state["export_guard_floor_kw"], 11.0)


class TestSustainableMorningCap(unittest.TestCase):
    def test_ignores_full_fleet_preferred_uses_horizon_hours(self):
        """Surplus-Plan preferred=31.6 darf Cap nicht auf volle Flotte setzen."""
        now = datetime(2026, 8, 9, 20, 0, 0)
        tgt = now + timedelta(hours=34)
        projection = {
            "target_datetime": tgt.isoformat(),
            "binding_checkpoint": {
                "time": "06:00",
                "target_datetime": tgt.isoformat(),
                "projected_soc": 0.0,
                "reachable": False,
            },
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 46.0,
                    "projected_soc": 0.0,
                    "reachable": False,
                    "target_datetime": tgt.isoformat(),
                    "minutes_until": 34 * 60,
                }
            ],
        }
        priority_plan = {
            "preferred_continuous_kw": 31.6,
            "devices": [
                {"ip": "10.0.0.1", "role": "anchor", "power_kw": 5.5, "prefer_on": True},
                {"ip": "10.0.0.2", "role": "flex", "power_kw": 5.5, "prefer_on": True},
            ],
        }
        live = {"soc_percent": 84.0, "house_kw": 3.5}
        config = {"storage_kwh": 1100.0, "risk_factor_percent": 85.0}
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            cap = sustainable_morning_cap_kw(
                config,
                live,
                projection,
                priority_plan,
                morning_floor=46.0,
            )
        # Headroom (84-46)%*1100*0.85/34 - 3.5 ≈ 10.4 - 3.5 ≈ 6.9
        self.assertLess(cap, 15.0, cap)
        self.assertGreaterEqual(cap, 5.0, cap)

    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=5000.0)
    def test_sheds_without_near_term_sustained(self, _surplus):
        """Auch ohne near_term.sustained_budget_kw muss gestaffelt ausgehen."""
        now = datetime(2026, 8, 9, 20, 0, 0)
        horizon_end = (now + timedelta(hours=34)).isoformat()
        projection = {
            "target_soc": 46.0,
            "projected_soc": 0.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "target_datetime": horizon_end,
            "hours_left": 34.0,
            "miner_budget_kw": 31.6,
            "binding_constraint": "export_avoidance",
            "pv_yield_tomorrow_kwh": 600.0,
            "near_term_plan": {},
            "binding_checkpoint": {
                "time": "06:00",
                "target_datetime": horizon_end,
                "projected_soc": 0.0,
                "reachable": False,
            },
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 46.0,
                    "projected_soc": 0.0,
                    "reachable": False,
                    "minutes_until": 34 * 60,
                    "target_datetime": horizon_end,
                }
            ],
        }
        config = {
            "enabled": True,
            "min_soc_percent": 46.0,
            "morning_soc_percent": 33.0,
            "max_soc_percent": 86.0,
            "storage_kwh": 1100.0,
            "risk_factor_percent": 85.0,
            "min_device_runtime_hours": 0.5,
            "soc_targets": [{"time": "06:00", "soc_percent": 46.0}],
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": 5},
                {"ip": "10.0.0.4", "enabled": True, "power_kw": 3.2, "priority": 1},
                {"ip": "10.0.0.5", "enabled": True, "power_kw": 3.2, "priority": 0},
            ],
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 600.0,
            "export_guard_active": True,
            "export_guard_floor_kw": 31.6,
            "device_runtime": {},
            "fill_schedule": {"ok": True, "flex": {}, "excess_charge_kw": 0},
        }
        live = {
            "soc_percent": 84.0,
            "pv_kw": 1.8,
            "grid_kw": -0.76,
            "verbrauch_kw": 3.5,
            "house_kw": 3.5,
            "miner_status": {d["ip"]: "running" for d in config["devices"]},
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=0.76
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.05
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ), patch(
            "control_algorithm.fill_schedule.compute_fill_urgency",
            return_value=False,
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 900.0, 600.0, 1.0
            )

        self.assertTrue(state.get("morning_floor_threatened"), msgs)
        cap = float(state.get("morning_sustainable_cap_kw") or 99)
        self.assertLess(cap, 20.0, f"cap={cap} msgs={msgs}")
        pauses = [a for a in actions if a.get("action") == "pause"]
        self.assertTrue(pauses, f"erwartet Pause, got {actions}\n{msgs}")
        self.assertNotIn(
            "keine Budget-Abschaltung",
            " ".join(msgs),
        )


class TestMorningFloorShedsLowPriority(unittest.TestCase):
    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=50.0)
    def test_export_mode_sheds_lowest_when_morning_threatened(self, _surplus):
        now = datetime(2026, 8, 2, 20, 0, 0)
        horizon_end = (now + timedelta(hours=60)).isoformat()
        projection = {
            "target_soc": 45.0,
            "projected_soc": 33.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 60.0,
            "miner_budget_kw": 31.6,
            "binding_constraint": "export_avoidance",
            "pv_yield_tomorrow_kwh": 80.0,
            "near_term_plan": {"sustained_budget_kw": 11.0},
            "checkpoints": [
                {
                    "time": "07:00",
                    "soc_percent": 45.0,
                    "projected_soc": 33.0,
                    "reachable": False,
                    "minutes_until": 11 * 60,
                }
            ],
        }
        config = {
            "enabled": True,
            "min_soc_percent": 45.0,
            "morning_soc_percent": 45.0,
            "max_soc_percent": 93.0,
            "storage_kwh": 1100.0,
            "risk_factor_percent": 85.0,
            "min_device_runtime_hours": 0.5,
            "soc_targets": [{"time": "07:00", "soc_percent": 45.0}],
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": 5},
                {"ip": "10.0.0.4", "enabled": True, "power_kw": 5.5, "priority": 3},
                {"ip": "10.0.0.5", "enabled": True, "power_kw": 3.2, "priority": 1},
                {"ip": "10.0.0.6", "enabled": True, "power_kw": 3.2, "priority": 0},
                {"ip": "10.0.0.7", "enabled": True, "power_kw": 3.2, "priority": 0},
            ],
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 80.0,
            "export_guard_active": True,
            "export_guard_floor_kw": 31.6,
            "device_runtime": {},
        }
        live = {
            "soc_percent": 62.0,
            "pv_kw": 0.0,
            "grid_kw": -0.4,  # leichte Einspeisung → Export-Modus möglich
            "verbrauch_kw": 32.0,
            "miner_status": {d["ip"]: "running" for d in config["devices"]},
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=0.4
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.2
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 40.0, 80.0, 1.0
            )

        self.assertTrue(state.get("morning_floor_threatened"), msgs)
        pauses = [a for a in actions if a.get("action") == "pause"]
        self.assertTrue(pauses, f"erwartet gestaffelte Pause, got {actions}\n{msgs}")
        # Niedrigste Priorität zuerst (0)
        paused_ip = pauses[0]["ip"]
        self.assertIn(paused_ip, ("10.0.0.6", "10.0.0.7"))
        # Anti-Flatter: Morgen-Floor ohne force (Commitment/Min-Laufzeit greifen)
        self.assertFalse(pauses[0].get("force"))
        self.assertTrue(pauses[0].get("morning_floor"))
        # Hohe Prio nicht abwerfen
        self.assertNotIn("10.0.0.1", [a["ip"] for a in pauses])
        self.assertTrue(
            any("Morgen" in m for m in msgs),
            msgs,
        )
        # Guard nicht mehr bei voller Flotte sticky
        floor = float(state.get("export_guard_floor_kw") or 0)
        self.assertLess(floor, 20.0, state)


class TestProactiveDischargeNotBlockedByDay2(unittest.TestCase):
    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=5000.0)
    def test_resumes_when_next_morning_ok_despite_day2_fail(self, _surplus):
        """Starke PV morgen + SOC hoch: entladen, nicht wegen Tag+2 abschalten."""
        now = datetime(2026, 8, 12, 22, 10, 0)
        horizon_end = (now + timedelta(hours=168)).isoformat()
        projection = {
            "target_soc": 46.0,
            "projected_soc": 40.0,
            "reachable": False,
            "all_checkpoints_ok": False,
            "horizon_end": horizon_end,
            "hours_left": 168.0,
            "miner_budget_kw": 31.6,
            "proactive_discharge_kw": 31.6,
            "binding_constraint": "export_avoidance",
            "pv_yield_tomorrow_kwh": 885.0,
            "checkpoints": [
                {
                    "time": "06:00",
                    "soc_percent": 46.0,
                    "projected_soc": 63.0,
                    "reachable": True,
                    "target_datetime": "2026-08-13T06:00:00",
                },
                {
                    "time": "06:00",
                    "soc_percent": 46.0,
                    "projected_soc": 40.0,
                    "reachable": False,
                    "target_datetime": "2026-08-14T06:00:00",
                },
            ],
        }
        devices = [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": 5},
            {"ip": "10.0.0.4", "enabled": True, "power_kw": 3.2, "priority": 1},
        ]
        config = {
            "enabled": True,
            "min_soc_percent": 46.0,
            "morning_soc_percent": 46.0,
            "max_soc_percent": 86.0,
            "storage_kwh": 1100.0,
            "risk_factor_percent": 85.0,
            "min_device_runtime_hours": 0.05,
            "export_commitment_override_kw": 2.0,
            "soc_targets": [{"time": "06:00", "soc_percent": 46.0}],
            "devices": devices,
        }
        state = {
            "horizon_projection": projection,
            "balance_projection": projection,
            "pv_yield_tomorrow_kwh": 885.0,
            "export_guard_active": True,
            "export_guard_floor_kw": 11.0,
            "device_runtime": {
                "10.0.0.1": {
                    "last_pause_kind": "budget_shed",
                    "last_action": "pause",
                    "committed_off_until": (now + timedelta(hours=1)).isoformat(),
                    "last_off_at": (now - timedelta(minutes=30)).isoformat(),
                },
                "10.0.0.3": {
                    "last_pause_kind": "budget_shed",
                    "last_action": "pause",
                    "committed_off_until": (now + timedelta(hours=1)).isoformat(),
                    "last_off_at": (now - timedelta(minutes=20)).isoformat(),
                },
            },
        }
        live = {
            "soc_percent": 84.0,
            "pv_kw": 0.0,
            "grid_kw": -5.2,
            "verbrauch_kw": 5.5,
            "miner_status": {
                "10.0.0.1": "paused",
                "10.0.0.2": "running",
                "10.0.0.3": "paused",
                "10.0.0.4": "paused",
            },
        }
        with patch("control_algorithm.load_scheduler.datetime") as mock_dt, patch(
            "control_algorithm.load_scheduler.live_grid_export_kw", return_value=5.2
        ), patch(
            "control_algorithm.load_scheduler.export_threshold_kw", return_value=0.05
        ), patch(
            "control_algorithm.anchor_stretch.compute_anchor_stretch_plan",
            return_value={"ok": True, "active": False},
        ):
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = datetime.fromisoformat
            actions, msgs = plan_load_actions(
                config, live, state, "/tmp", 5000.0, 885.0, 1.0
            )

        self.assertFalse(state.get("morning_floor_threatened"), msgs)
        pauses = [a for a in actions if a.get("action") == "pause"]
        self.assertFalse(pauses, f"kein Shed erwartet, got {pauses}\n{msgs}")
        resumes = [a for a in actions if a.get("action") == "resume"]
        self.assertTrue(resumes, f"erwartet Resume zum Entladen, got {actions}\n{msgs}")
        joined = " ".join(msgs)
        self.assertTrue(
            "kein Sofort-Shed" in joined or "Export-Vermeidung:" in joined,
            msgs,
        )


if __name__ == "__main__":
    unittest.main()
