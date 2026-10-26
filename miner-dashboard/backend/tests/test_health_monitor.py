# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from control_algorithm.health_monitor import format_heartbeat, collect_subsystem_status


class TestHealthMonitor(unittest.TestCase):
    def test_format_heartbeat_contains_all_sections(self):
        status = {
            "enabled": True,
            "dry_run": False,
            "daemon_status": "ok",
            "last_tick": None,
            "last_weather_fetch": None,
            "pv_today": 100.0,
            "pv_tomorrow": 200.0,
            "soc": 55.0,
            "pv_kw": 0.0,
            "budget_kw": 20.0,
            "binding": "proactive_discharge",
            "projected_soc": 40.0,
            "target_soc": 40.0,
            "export_guard": True,
            "export_floor_kw": 20.0,
            "night_lock": False,
            "anomaly": False,
            "consumption_samples": 10,
            "consumption_days": 60,
            "bad_streak_days": 0,
            "miners_running": 4,
            "miners_paused": 1,
            "planned_actions": 0,
            "processes": [
                {"name": "Steuerung", "ok": True},
                {"name": "EnergyMonitor", "ok": True},
                {"name": "Telegram", "ok": False},
            ],
        }
        lines = format_heartbeat(status, tick_ok=True)
        text = "\n".join(lines)
        self.assertIn("[Steuerung]", text)
        self.assertIn("[Wetteralgo]", text)
        self.assertIn("[Verbrauch]", text)
        self.assertIn("[Planer]", text)
        self.assertIn("[Last/Geräte]", text)
        self.assertIn("[Prozesse]", text)
        self.assertIn("Telegram✗", text)

    def test_collect_tolerates_missing_live(self):
        with patch(
            "control_algorithm.health_monitor.process_health", return_value=[]
        ), patch(
            "control_algorithm.config.load_config",
            return_value={"enabled": True, "dry_run": True, "consumption_history_days": 60},
        ), patch(
            "control_algorithm.config.load_state",
            return_value={"daemon_status": "ok", "last_tick": None},
        ), patch(
            "control_algorithm.consumption_history.profile_sample_count",
            return_value=3,
        ), patch(
            "control_algorithm.live_data.load_live_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            st = collect_subsystem_status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["consumption_samples"], 3)


if __name__ == "__main__":
    unittest.main()
