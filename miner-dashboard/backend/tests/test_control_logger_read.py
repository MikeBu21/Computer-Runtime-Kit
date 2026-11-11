# -*- coding: utf-8 -*-
"""Anzeige-Werte kommen aus Home Assistant, nicht aus Solarman-Registern."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bms_seite
import dashboard_battery
import flow_roles
import miner_flow_data


def _snapshot() -> dict:
    def sensor(state, unit):
        return {
            "state": state,
            "unit_of_measurement": unit,
            "attributes": {"unit_of_measurement": unit, "state_class": "measurement"},
        }

    entities = {
        "Beispiel": sensor("1225", "W"),
        "Beispiel": sensor("18", "W"),
        "Beispiel": sensor("41", "%"),
        "Beispiel": sensor("-180", "W"),
        "Beispiel": sensor("1195", "W"),
        # Dieselbe Anlage noch einmal, plus ein Sollwert. Darf nicht in die Summe.
        "sensor.inverter_2_microinverter_power": sensor("9000", "W"),
        "number.192_168_1_176_pv_power": sensor("15000", "W"),
    }
    return {"devices": {"logger": {"entities": entities}}, "unassigned_entities": {}}


class ControlLoggerReadTests(unittest.TestCase):
    def test_pv_mapping_keeps_only_the_measurement(self):
        from ha_entity_mapping import load_config, normalize_field_entry

        entry = normalize_field_entry(load_config()["fields"]["pv"])
        self.assertEqual(
            [sensor["entity_id"] for sensor in entry["sensors"]],
            ["Beispiel"],
        )

    def test_powers_come_from_mapped_ha_fields(self):
        with mock.patch("energy_monitor.ha_live_reader.load_snapshot", return_value=_snapshot()):
            with mock.patch.object(bms_seite, "read_solarman_holding") as read:
                with mock.patch.object(bms_seite, "load_settings", return_value={"logger_serial": 0}):
                    powers = bms_seite.read_control_logger_powers()
                    live = bms_seite._read_live_now()

        read.assert_not_called()
        self.assertEqual(powers["pv_kw"], 1.225)
        self.assertEqual(powers["grid_kw"], 0.018)
        self.assertEqual(powers["soc_percent"], 41.0)
        # Mapping-Vorzeichen „-“ dreht die Solarman-Angabe (−180 W) auf +0,18 kW.
        self.assertEqual(powers["battery_kw"], 0.18)
        self.assertEqual(live["source"], "Home Assistant")
        self.assertEqual(live["pv_kw"], 1.225)
        self.assertEqual(live["grid_kw"], 0.018)
        self.assertEqual(live["soc_percent"], 41.0)
        self.assertEqual(live["battery_kw"], 0.18)

    def test_missing_snapshot_does_not_read_modbus_or_scraper(self):
        with mock.patch(
            "energy_monitor.ha_live_reader.load_snapshot",
            side_effect=FileNotFoundError("weg"),
        ):
            with mock.patch.object(bms_seite, "read_solarman_holding") as read:
                with mock.patch.object(bms_seite, "_configured_house_kw", return_value=0.4):
                    with mock.patch("ha_device_control.read_ha_entity_state") as ha:
                        with mock.patch("dashboard_battery.load_battery_data") as battery:
                            with mock.patch("dashboard_control.get_grid_kw_for_control") as grid:
                                powers = bms_seite.read_control_logger_powers()
                                live = bms_seite._read_live_now()

        self.assertEqual(
            powers,
            {"soc_percent": None, "battery_kw": None, "grid_kw": None, "pv_kw": None},
        )
        self.assertIsNone(live["soc_percent"])
        self.assertIsNone(live["battery_kw"])
        self.assertIsNone(live["grid_kw"])
        self.assertIsNone(live["pv_kw"])
        self.assertEqual(live["load_kw"], 0.4)
        self.assertEqual(live["source"], "Home Assistant")
        read.assert_not_called()
        ha.assert_not_called()
        battery.assert_not_called()
        grid.assert_not_called()

    def test_extra_inverter_flow_does_not_read_modbus(self):
        settings = {
            "tracked_inverters": [
                {
                    "control": True,
                    "mac": "aa:aa:aa:aa:aa:aa",
                    "flow_field_id": "control",
                    "pv_kw": 11.8,
                    "name": "Steuer-Logger",
                },
                {
                    "control": False,
                    "mac": "bb:bb:bb:bb:bb:bb",
                    "flow_field_id": "wrbb",
                    "pv_kw": 11.8,
                    "name": "WR2",
                },
            ]
        }
        with mock.patch.object(bms_seite, "load_settings", return_value=settings):
            with mock.patch.object(bms_seite, "read_solarman_holding") as read:
                values = bms_seite.extra_inverter_flow_values()
        self.assertEqual(values, [])
        read.assert_not_called()


class ControlLoggerFlowTests(unittest.TestCase):
    def _dash(self):
        return {
            "flow_hidden_builtins": [],
            "flow_point_roles": dict(flow_roles.DEFAULT_BUILTIN_ROLES),
            "custom_flow_fields": [],
        }

    def test_flow_uses_ha_snapshot_not_file_or_modbus(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "pv_value.txt").write_text("9.0", encoding="utf-8")
            (em / "energy_value.txt").write_text("9.0", encoding="utf-8")
            (em / "wohnhaus_value.txt").write_text("9.0", encoding="utf-8")
            (em / "wasserkraft_value.txt").write_text("9.0", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"
            asked = []

            def fake_signed(field_key, ha_cfg, snapshot):
                asked.append(field_key)
                return {
                    "pv": 1.225,
                    "kelag": 0.018,
                    "battery_power": 0.18,
                    "wohnhaus": 1.195,
                    "wasserkraft": 0.0,
                    "battery_soc": 41.0,
                }.get(field_key)

            extra = [{
                "flow_field_id": "wrbb",
                "name": "WR2",
                "role": "erzeugung",
                "kw": 11.8,
            }]
            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=self._dash()):
                with mock.patch.object(miner_flow_data, "_signed_ha_field_kw", side_effect=fake_signed):
                    with mock.patch("bms_seite.extra_inverter_flow_values", return_value=extra) as extra_call:
                        with mock.patch("bms_seite.read_solarman_holding") as read:
                            with mock.patch(
                                "dashboard_sources.resolve_custom_flow_values", return_value={}
                            ):
                                flow = miner_flow_data.build_flow_response(
                                    str(base),
                                    battery_soc_loader=lambda: "0",
                                    battery_power_kw=9.0,
                                )

        self.assertIn("pv", asked)
        self.assertIn("kelag", asked)
        self.assertIn("battery_power", asked)
        self.assertIn("battery_soc", asked)
        self.assertIn("wohnhaus", asked)
        extra_call.assert_not_called()
        read.assert_not_called()
        self.assertAlmostEqual(flow["role_sums"]["erzeugung"], 1.225, places=3)
        self.assertAlmostEqual(flow["role_sums"]["netz"], 0.018, places=3)
        self.assertAlmostEqual(flow["role_sums"]["speicher"], 0.18, places=3)
        self.assertAlmostEqual(flow["role_sums"]["verbraucher"], 1.195, places=3)
        self.assertEqual(flow["pv"], "1.2")
        self.assertEqual(flow["grid"], "0")
        self.assertEqual(flow["battery_soc"], "41")
        by_point = {item["point"]: item["kw"] for item in flow["role_details"]}
        self.assertEqual(by_point["pv"], 1.225)
        self.assertEqual(by_point["kelag"], 0.018)
        self.assertEqual(by_point["battery"], 0.18)
        self.assertNotIn("custom:wrbb", by_point)

    def test_file_cache_is_used_only_when_ha_has_no_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "pv_value.txt").write_text("3.0", encoding="utf-8")
            (em / "energy_value.txt").write_text("0.4", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"

            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=self._dash()):
                with mock.patch.object(miner_flow_data, "_signed_ha_field_kw", return_value=None):
                    with mock.patch("bms_seite.extra_inverter_flow_values", return_value=[{"kw": 11.8}]) as extra:
                        with mock.patch(
                            "dashboard_sources.resolve_custom_flow_values", return_value={}
                        ):
                            flow = miner_flow_data.build_flow_response(
                                str(base),
                                battery_soc_loader=lambda: "40",
                                battery_power_kw=-0.8,
                            )

        extra.assert_not_called()
        self.assertAlmostEqual(flow["role_sums"]["erzeugung"], 3.0, places=3)
        self.assertAlmostEqual(flow["role_sums"]["netz"], 0.4, places=3)
        self.assertAlmostEqual(flow["role_sums"]["speicher"], -0.8, places=3)
        self.assertEqual(flow["pv"], "3.0")
        self.assertEqual(flow["grid"], "0.4")
        self.assertEqual(flow["battery_soc"], "40")


class BatteryDisplaySourceTests(unittest.TestCase):
    def test_loader_prefers_ha_snapshot_over_scraper_file(self):
        paths = [str(path) for path in dashboard_battery._battery_data_paths()]
        self.assertEqual(paths, [str(dashboard_battery._ha_cache_path())])
        self.assertTrue(paths[0].endswith("energy_monitor/daten.json"))
        self.assertNotIn(str(Path.home() / "daten.json"), paths)

        with mock.patch.object(
            dashboard_battery,
            "_load_ha_battery",
            return_value={"soc": 41.0, "power_kw": 0.18, "source": "home_assistant"},
        ):
            with mock.patch.object(
                dashboard_battery,
                "_load_ram_battery",
                return_value={"soc": 0, "power_kw": 0.0},
            ) as ram:
                data = dashboard_battery.load_battery_data()

        ram.assert_not_called()
        self.assertEqual(data["soc"], "41")
        self.assertEqual(data["power_kw"], 0.18)
        self.assertEqual(data["source"], "home_assistant")

    def test_cache_file_is_not_the_home_scraper(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "daten.json"
            cache.write_text('{"soc": 55, "power_kw": -0.2}', encoding="utf-8")
            with mock.patch.object(dashboard_battery, "_load_ha_battery", return_value=None):
                with mock.patch.object(dashboard_battery, "_load_ram_battery", return_value=None):
                    with mock.patch.object(dashboard_battery, "_ha_cache_path", return_value=cache):
                        data = dashboard_battery.load_battery_data()
        self.assertEqual(data["soc"], "55")
        self.assertEqual(data["power_kw"], -0.2)
        self.assertEqual(data["source"], "ha_cache")


if __name__ == "__main__":
    unittest.main()
