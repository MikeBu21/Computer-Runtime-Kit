# -*- coding: utf-8 -*-
"""Tests: Geräte-Leistung aus Energiedaten (ohne Geräte-API)."""
from __future__ import annotations

import unittest
from unittest.mock import patch


class DevicePowerMeasureTests(unittest.TestCase):
    def test_single_device_on_learns_from_house_delta(self):
        from control_algorithm.device_power_measure import update_device_power_measurements

        config = {
            "devices": [
                {
                    "ip": "switch.heizung",
                    "enabled": True,
                    "power_kw": 1.0,
                    "priority": 5,
                }
            ]
        }
        state: dict = {}
        live1 = {
            "wohnhaus_kw": 3.0,
            "miner_status": {"switch.heizung": "paused"},
        }
        update_device_power_measurements(config, state, live1, auto_apply_config=False)

        live2 = {
            "wohnhaus_kw": 5.1,
            "miner_status": {"switch.heizung": "running"},
        }
        update_device_power_measurements(config, state, live2, auto_apply_config=False)

        live3 = {
            "wohnhaus_kw": 5.15,
            "miner_status": {"switch.heizung": "running"},
        }
        with patch("control_algorithm.config.save_config") as save_mock:
            update_device_power_measurements(
                config, state, live3, auto_apply_config=True
            )

        measured = (
            (state.get("device_power_measure") or {}).get("by_ip") or {}
        ).get("switch.heizung") or {}
        self.assertIsNotNone(measured.get("measured_kw"))
        self.assertGreater(float(measured["measured_kw"]), 1.5)
        self.assertLess(float(measured["measured_kw"]), 3.0)
        self.assertEqual(measured.get("source"), "energy_delta")
        self.assertAlmostEqual(
            float(config["devices"][0]["power_kw"]),
            float(measured["measured_kw"]),
            delta=0.15,
        )
        self.assertTrue(save_mock.called)

    def test_multi_switch_discards_measurement(self):
        from control_algorithm.device_power_measure import update_device_power_measurements

        config = {
            "devices": [
                {"ip": "a", "enabled": True, "power_kw": 3.0},
                {"ip": "b", "enabled": True, "power_kw": 3.0},
            ]
        }
        state: dict = {}
        update_device_power_measurements(
            config,
            state,
            {"wohnhaus_kw": 4.0, "miner_status": {"a": "paused", "b": "paused"}},
            auto_apply_config=False,
        )
        update_device_power_measurements(
            config,
            state,
            {"wohnhaus_kw": 10.0, "miner_status": {"a": "running", "b": "running"}},
            auto_apply_config=False,
        )
        update_device_power_measurements(
            config,
            state,
            {"wohnhaus_kw": 10.0, "miner_status": {"a": "running", "b": "running"}},
            auto_apply_config=False,
        )
        by_ip = (state.get("device_power_measure") or {}).get("by_ip") or {}
        self.assertFalse(by_ip)


if __name__ == "__main__":
    unittest.main()
