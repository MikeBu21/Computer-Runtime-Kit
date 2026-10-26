# -*- coding: utf-8 -*-
"""Tests für Prioritäts-Laufzeitplanung."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from control_algorithm.priority_runtime import (
    allocate_surplus_by_priority,
    compute_priority_runtime_plan,
    may_resume_by_priority_plan,
    should_force_pause_flex,
)
from control_algorithm.load_scheduler import plan_load_actions


DEVICES = [
    {"ip": "hydro", "enabled": True, "power_kw": 5.5, "priority": 10},
    {"ip": "air", "enabled": True, "power_kw": 3.5, "priority": 5},
    {"ip": "heat", "enabled": True, "power_kw": 0.2, "priority": 1},
]


class TestPriorityRuntimePlan(unittest.TestCase):
    def test_greedy_gives_longest_to_highest_priority(self):
        # 50 kWh / 8 h → Anker 8 h, Flex ~1.7 h, Spill 0
        plan = compute_priority_runtime_plan(DEVICES, 50.0, 8.0)
        by_ip = plan["by_ip"]
        self.assertAlmostEqual(by_ip["hydro"]["planned_hours"], 8.0, places=1)
        self.assertGreater(by_ip["air"]["planned_hours"], 1.0)
        self.assertLess(by_ip["air"]["planned_hours"], 3.0)
        self.assertEqual(by_ip["heat"]["planned_hours"], 0.0)
        self.assertEqual(by_ip["hydro"]["role"], "anchor")
        self.assertEqual(by_ip["air"]["role"], "flex")
        self.assertEqual(by_ip["heat"]["role"], "spill")
        self.assertIn("hydro", plan["prefer_ips"])
        self.assertNotIn("heat", plan["prefer_ips"])

    def test_full_surplus_keeps_all_preferred(self):
        # genug für alle 8 h: 5.5+3.5+0.2 = 9.2 * 8 = 73.6
        plan = compute_priority_runtime_plan(DEVICES, 80.0, 8.0)
        self.assertTrue(plan["by_ip"]["hydro"]["prefer_on"])
        self.assertTrue(plan["by_ip"]["air"]["prefer_on"])
        self.assertTrue(plan["by_ip"]["heat"]["prefer_on"])

    def test_spill_only_on_export_when_no_rest(self):
        plan = compute_priority_runtime_plan(DEVICES, 44.0, 8.0)  # genau Anker
        ok, _ = may_resume_by_priority_plan(
            DEVICES[2], plan, export_mode=False, discharge_mode=False
        )
        self.assertFalse(ok)
        ok2, why = may_resume_by_priority_plan(
            DEVICES[2], plan, export_mode=True, discharge_mode=False
        )
        self.assertTrue(ok2)
        self.assertIn("Spill", why)

    def test_force_pause_flex_under_stress(self):
        plan = compute_priority_runtime_plan(DEVICES, 50.0, 8.0)
        self.assertTrue(
            should_force_pause_flex(
                DEVICES[1], plan, energy_stress=True, export_mode=False
            )
        )
        self.assertFalse(
            should_force_pause_flex(
                DEVICES[0], plan, energy_stress=True, export_mode=False
            )
        )
        self.assertFalse(
            should_force_pause_flex(
                DEVICES[1], plan, energy_stress=True, export_mode=True
            )
        )

    def test_allocate_surplus_priority_order(self):
        alloc = allocate_surplus_by_priority(DEVICES, 50.0, 8.0)
        self.assertGreater(alloc["hydro"]["allocated_kwh"], alloc["air"]["allocated_kwh"])
        self.assertGreaterEqual(alloc["air"]["allocated_kwh"], alloc["heat"]["allocated_kwh"])


class TestPlanLoadUsesPriorityRuntime(unittest.TestCase):
    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=44.0)
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_does_not_resume_spill_when_surplus_only_for_anchor(
        self, mock_proj, _mock_surplus
    ):
        now = datetime(2026, 7, 26, 12, 0, 0)
        mock_proj.return_value = {
            "target_soc": 28,
            "projected_soc": 70,
            "target_datetime": (now + timedelta(hours=8)).isoformat(),
            "horizon_end": (now + timedelta(hours=8)).isoformat(),
            "hours_left": 8.0,
            "miner_budget_kw": 5.5,
            "base_consumption_kwh": 10,
            "pv_forecast_kwh": 40,
            "hydro_forecast_kwh": 0,
            "risk_factor_percent": 100,
            "binding_constraint": "checkpoint",
        }
        config = {
            "enabled": True,
            "min_soc_percent": 28,
            "max_soc_percent": 97,
            "min_device_runtime_hours": 0.5,
            "devices": DEVICES,
        }
        state = {
            "balance_projection": mock_proj.return_value,
            "horizon_projection": mock_proj.return_value,
        }
        live = {
            "soc_percent": 70,
            "pv_kw": 2.0,
            "miner_status": {
                "hydro": "running",
                "air": "paused",
                "heat": "paused",
            },
        }
        actions, msgs = plan_load_actions(config, live, state, "/tmp", 40, 40, 1.0)
        resume_ips = [a["ip"] for a in actions if a.get("action") == "resume"]
        self.assertNotIn("heat", resume_ips)
        self.assertTrue(any("Laufzeitplan" in m for m in msgs))
        self.assertIsNotNone(state.get("priority_runtime_plan"))


if __name__ == "__main__":
    unittest.main()
