# -*- coding: utf-8 -*-
"""Speichern / Testmodus: Geocode darf Modus nicht blockieren; Race nicht überschreiben."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class ParseRunModeTests(unittest.TestCase):
    def test_test_mode(self):
        from control_algorithm_web import parse_run_mode

        enabled, dry = parse_run_mode({"run_mode": "test"})
        self.assertTrue(enabled)
        self.assertTrue(dry)

    def test_active_mode(self):
        from control_algorithm_web import parse_run_mode

        enabled, dry = parse_run_mode({"run_mode": "active"})
        self.assertTrue(enabled)
        self.assertFalse(dry)

    def test_off_mode(self):
        from control_algorithm_web import parse_run_mode

        enabled, dry = parse_run_mode({"run_mode": "off"})
        self.assertFalse(enabled)
        self.assertTrue(dry)


class SaveFromFormGeocodeTests(unittest.TestCase):
    def test_geocode_fail_keeps_coords_and_saves_test_mode(self):
        from control_algorithm_web import save_from_form

        base = {
            "enabled": True,
            "dry_run": False,
            "city": "Beispiel",
            "lat": 46.75,
            "lon": 14.03,
            "city_display": "Beispiel",
            "has_storage": True,
            "storage_kwh": 41,
            "has_pv": True,
            "pv_layout": "east_west",
            "pv_kwp_east": 8,
            "pv_kwp_west": 8,
            "devices": [],
            "soc_targets": [],
            "min_soc_percent": 30,
            "max_soc_percent": 95,
        }
        form = MagicMock()
        form.get = lambda k, default=None: {
            "run_mode": "test",
            "city": "Beispiel",
            "has_storage": "on",
            "storage_kwh": "41",
            "has_pv": "on",
            "pv_layout": "east_west",
            "pv_kwp_east": "8",
            "pv_kwp_west": "8",
            "min_soc_percent": "30",
            "max_soc_percent": "95",
            "soc_targets_json": "[]",
        }.get(k, default)
        form.__contains__ = lambda self, k: k in (
            "has_storage",
            "has_pv",
        )

        with patch("control_algorithm_web.load_config", return_value=dict(base)), patch(
            "control_algorithm_web.geocode_city",
            side_effect=RuntimeError("Wetter-API nicht erreichbar"),
        ), patch("control_algorithm_web.save_config") as save_mock, patch(
            "control_algorithm_web.load_miners", return_value=[]
        ), patch(
            "control_algorithm_web.sync_total_kwp_in_config"
        ):
            cfg, err = save_from_form(form)

        self.assertEqual(err, "")
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["dry_run"])
        self.assertEqual(cfg["lat"], 46.75)
        self.assertTrue(save_mock.called)


class DevicePowerRaceTests(unittest.TestCase):
    def test_power_save_does_not_clobber_dry_run(self):
        from control_algorithm.device_power_measure import update_device_power_measurements

        stale = {
            "enabled": True,
            "dry_run": False,  # veralteter Tick-Snapshot
            "devices": [
                {
                    "ip": "switch.heizung",
                    "enabled": True,
                    "power_kw": 1.0,
                    "priority": 5,
                }
            ],
        }
        disk = {
            "enabled": True,
            "dry_run": True,  # User hat gerade Testmodus gespeichert
            "devices": [
                {
                    "ip": "switch.heizung",
                    "enabled": True,
                    "power_kw": 1.0,
                    "priority": 5,
                }
            ],
        }
        state: dict = {}
        live_off = {
            "wohnhaus_kw": 3.0,
            "miner_status": {"switch.heizung": "paused"},
        }
        update_device_power_measurements(stale, state, live_off, auto_apply_config=False)
        live_on = {
            "wohnhaus_kw": 5.1,
            "miner_status": {"switch.heizung": "running"},
        }
        update_device_power_measurements(stale, state, live_on, auto_apply_config=False)
        live_stable = {
            "wohnhaus_kw": 5.15,
            "miner_status": {"switch.heizung": "running"},
        }
        saved = {}

        def _save(cfg):
            saved.update(cfg)

        with patch(
            "control_algorithm.config.load_config", return_value=disk
        ), patch(
            "control_algorithm.config.save_config", side_effect=_save
        ):
            update_device_power_measurements(
                stale, state, live_stable, auto_apply_config=True
            )

        self.assertTrue(saved.get("dry_run") is True)
        self.assertTrue(stale.get("dry_run") is True)


if __name__ == "__main__":
    unittest.main()
