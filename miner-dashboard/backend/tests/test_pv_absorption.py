# -*- coding: utf-8 -*-
"""PV-Tags: max_soc-Bindung darf nicht Nacht-Entlade-Cohort auslösen."""
from datetime import datetime, timedelta
from unittest.mock import patch

import unittest

from control_algorithm.load_scheduler import _pv_absorption_mode, plan_load_actions


class PvAbsorptionModeTests(unittest.TestCase):
    def test_pv_absorption_when_max_soc_binding_daytime(self):
        self.assertTrue(
            _pv_absorption_mode(
                pv_kw=72.0,
                soc=48.0,
                max_soc=85.0,
                binding="max_soc",
            )
        )

    def test_no_absorption_when_over_max_soc(self):
        self.assertFalse(
            _pv_absorption_mode(
                pv_kw=72.0,
                soc=86.0,
                max_soc=85.0,
                binding="max_soc",
            )
        )

    def test_no_absorption_at_night(self):
        self.assertFalse(
            _pv_absorption_mode(
                pv_kw=0.0,
                soc=48.0,
                max_soc=85.0,
                binding="max_soc",
            )
        )


class PvDayMaxSocResumeTests(unittest.TestCase):
    @patch("control_algorithm.load_scheduler.miner_surplus_kwh", return_value=3500.0)
    @patch("control_algorithm.load_scheduler.compute_horizon_projection")
    def test_daytime_max_soc_resumes_multiple_not_discharge_cohort(
        self, mock_proj, _mock_surplus
    ):
        now = datetime(2026, 7, 28, 8, 45, 0)
        mock_proj.return_value = {
            "target_soc": 39,
            "projected_soc": 50.0,
            "reachable": True,
            "target_datetime": (now + timedelta(hours=21)).isoformat(),
            "horizon_end": (now + timedelta(days=7)).isoformat(),
            "hours_left": 168.0,
            "miner_budget_kw": 21.0,
            "binding_constraint": "max_soc",
            "proactive_discharge_kw": 0.0,
            "near_term_plan": {
                "sustained_budget_kw": 21.0,
                "near_term_budget_kw": 21.0,
            },
            "risk_factor_percent": 100,
        }
        config = {
            "enabled": True,
            "min_soc_percent": 39,
            "max_soc_percent": 85,
            "storage_kwh": 1100,
            "min_device_runtime_hours": 0.01,
            "soc_targets": [{"time": "06:00", "soc_percent": 39}],
            "devices": [
                {"ip": "a", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "b", "enabled": True, "power_kw": 5.5, "priority": 10},
                {"ip": "c", "enabled": True, "power_kw": 5.5, "priority": 6},
            ],
        }
        state = {
            "balance_projection": mock_proj.return_value,
            "horizon_projection": mock_proj.return_value,
            "export_guard_active": True,
            "discharge_plan": {
                "date": now.date().isoformat(),
                "cycles_today": 1,
                "phase": "hold",
                "ips": ["a"],
                "target_kw": 5.5,
                "hold_until_soc": 42.0,
                "target_burn_kwh": 99.0,
                "burned_kwh": 66.0,
            },
            "device_runtime": {},
        }
        live = {
            "soc_percent": 48.0,
            "pv_kw": 72.0,
            "miner_status": {"a": "paused", "b": "paused", "c": "paused"},
        }
        actions, msgs = plan_load_actions(config, live, state, "/tmp", 1000, 934, 0.92)
        resumes = [a for a in actions if a.get("action") == "resume"]
        self.assertTrue(resumes, msgs)
        self.assertEqual(resumes[0]["action"], "resume")
        # Tagsüber darf der Nacht-Entladeplan die Resumes nicht blockieren.
        self.assertGreaterEqual(len(resumes), 1)


if __name__ == "__main__":
    unittest.main()
