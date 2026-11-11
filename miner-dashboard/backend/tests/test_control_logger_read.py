# -*- coding: utf-8 -*-
"""Direktlesung des Steuer-Loggers: Register, Vorzeichen, Stromfluss ohne HA-Snapshot."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bms_seite
import flow_roles
import miner_flow_data


LOGGER_SERIAL = 2965822000


def _settings(**overrides):
    data = {
        "logger_ip": "192.168.1.176",
        "modbus_host": "10.0.0.5",
        "modbus_port": 502,
        "modbus_unit": 1,
        "logger_serial": LOGGER_SERIAL,
    }
    data.update(overrides)
    return data


class ControlLoggerReadTests(unittest.TestCase):
    def test_registers_and_signs(self):
        seen = []

        def fake_read(host, port, unit, serial, address):
            seen.append((host, port, unit, serial, address))
            return {
                bms_seite.REG_BATTERY_SOC: 64,
                bms_seite.REG_BATTERY_POWER: 2500,  # Solarman: Entladen
                bms_seite.REG_GRID_POWER: 0xFC18,  # -1000 W, Einspeisung
                0x00BA: 2000,
                0x00BB: 1500,
                0x00BC: 250,
                0x00BD: 0,
            }[address]

        with mock.patch.object(bms_seite, "load_settings", return_value=_settings()):
            with mock.patch.object(bms_seite, "read_solarman_holding", side_effect=fake_read):
                powers = bms_seite.read_control_logger_powers()

        self.assertEqual(powers["soc_percent"], 64.0)
        self.assertEqual(powers["battery_kw"], -2.5)
        self.assertEqual(powers["grid_kw"], -1.0)
        self.assertEqual(powers["pv_kw"], 3.75)
        addresses = [item[4] for item in seen]
        self.assertNotIn(0x00B2, addresses)
        self.assertEqual(
            set(addresses),
            {
                bms_seite.REG_BATTERY_SOC,
                bms_seite.REG_BATTERY_POWER,
                bms_seite.REG_GRID_POWER,
                *bms_seite.PV_POWER_REGISTERS,
            },
        )
        for host, port, unit, serial, _address in seen:
            self.assertEqual(host, "192.168.1.176")
            self.assertEqual(port, 8899)
            self.assertEqual(unit, 1)
            self.assertEqual(serial, LOGGER_SERIAL)

    def test_charge_and_import_and_modbus_host_fallback(self):
        def fake_read(host, port, unit, serial, address):
            return {
                bms_seite.REG_BATTERY_SOC: 80,
                bms_seite.REG_BATTERY_POWER: 0xFCE0,  # -800 W, Laden
                bms_seite.REG_GRID_POWER: 1800,
                0x00BA: 0,
                0x00BB: 0,
                0x00BC: 0,
                0x00BD: 0,
            }[address]

        settings = _settings(logger_ip="", modbus_host="192.168.1.50", modbus_unit=1)
        with mock.patch.object(bms_seite, "load_settings", return_value=settings):
            with mock.patch.object(bms_seite, "read_solarman_holding", side_effect=fake_read) as read:
                powers = bms_seite.read_control_logger_powers()

        self.assertEqual(powers["battery_kw"], 0.8)
        self.assertEqual(powers["grid_kw"], 1.8)
        self.assertEqual(powers["soc_percent"], 80.0)
        self.assertEqual(powers["pv_kw"], 0.0)
        self.assertEqual(read.call_args[0][0], "192.168.1.50")
        self.assertEqual(read.call_args[0][1], 8899)

    def test_three_phase_reads_measure_registers_not_setpoints(self):
        seen = []

        def fake_read(host, port, unit, serial, address):
            seen.append(address)
            return {
                bms_seite.P3_BATTERY_SOC: 41,
                bms_seite.P3_BATTERY_POWER: 0xFCE0,  # -800 W, Laden
                bms_seite.P3_GRID_LOW: 12,
                bms_seite.P3_GRID_HIGH: 0,
                0x02A0: 1800,
                0x02A1: 2200,
                0x02A2: 0,
                0x02A3: 0,
            }[address]

        settings = _settings(inverter="deye_p3")
        with mock.patch.object(bms_seite, "load_settings", return_value=settings):
            with mock.patch.object(bms_seite, "read_solarman_holding", side_effect=fake_read):
                powers = bms_seite.read_control_logger_powers()

        self.assertEqual(powers["soc_percent"], 41.0)
        self.assertEqual(powers["battery_kw"], 0.8)
        self.assertEqual(powers["grid_kw"], 0.01)
        self.assertEqual(powers["pv_kw"], 4.0)
        self.assertNotIn(0x00B8, seen)
        self.assertNotIn(0x00BE, seen)
        self.assertNotIn(0x00BA, seen)

    def test_read_error_stays_empty_and_live_does_not_ask_ha(self):
        with mock.patch.object(bms_seite, "load_settings", return_value=_settings()):
            with mock.patch.object(bms_seite, "read_solarman_holding", side_effect=OSError("weg")):
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
        self.assertEqual(live["load_kw"], 0.4)
        self.assertEqual(live["source"], "Direkt vom Solarman-Logger")
        ha.assert_not_called()
        battery.assert_not_called()
        grid.assert_not_called()

    def test_missing_logger_serial_does_not_read(self):
        with mock.patch.object(bms_seite, "load_settings", return_value=_settings(logger_serial=0)):
            with mock.patch.object(bms_seite, "read_solarman_holding") as read:
                powers = bms_seite.read_control_logger_powers()
        self.assertIsNone(powers["soc_percent"])
        self.assertIsNone(powers["battery_kw"])
        self.assertIsNone(powers["grid_kw"])
        self.assertIsNone(powers["pv_kw"])
        read.assert_not_called()

    def test_extra_inverter_flow_skips_control_logger(self):
        settings = {
            "tracked_inverters": [
                {
                    "control": True,
                    "mac": "aa:aa:aa:aa:aa:aa",
                    "flow_field_id": "control",
                    "pv_kw": 9.0,
                    "name": "Steuer-Logger",
                },
                {
                    "control": False,
                    "mac": "bb:bb:bb:bb:bb:bb",
                    "flow_field_id": "wrbb",
                    "pv_kw": 1.5,
                    "name": "WR2",
                },
            ]
        }
        with mock.patch.object(bms_seite, "load_settings", return_value=settings):
            values = bms_seite.extra_inverter_flow_values()
        self.assertEqual([item["flow_field_id"] for item in values], ["wrbb"])
        self.assertEqual(values[0]["kw"], 1.5)


class ControlLoggerFlowTests(unittest.TestCase):
    def _dash(self):
        return {
            "flow_hidden_builtins": [],
            "flow_point_roles": dict(flow_roles.DEFAULT_BUILTIN_ROLES),
            "custom_flow_fields": [],
        }

    def test_flow_takes_direct_values_not_ha_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "pv_value.txt").write_text("0.2", encoding="utf-8")
            (em / "energy_value.txt").write_text("8.0", encoding="utf-8")
            (em / "wasserkraft_value.txt").write_text("2.0", encoding="utf-8")
            (em / "wohnhaus_value.txt").write_text("9.0", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"
            asked = []

            def fake_signed(field_key, ha_cfg, snapshot):
                asked.append(field_key)
                if field_key == "wohnhaus":
                    return 1.7
                if field_key in ("pv", "kelag", "battery_power"):
                    return 99.0
                return None

            extra = [{
                "flow_field_id": "wrbb",
                "name": "WR2",
                "role": "erzeugung",
                "kw": 1.5,
            }]
            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=self._dash()):
                with mock.patch.object(miner_flow_data, "_signed_ha_field_kw", side_effect=fake_signed):
                    with mock.patch.object(
                        miner_flow_data,
                        "_control_logger_builtin_kw",
                        return_value={"pv": 4.2, "kelag": -1.2, "battery": 0.8},
                    ):
                        with mock.patch("bms_seite.extra_inverter_flow_values", return_value=extra):
                            with mock.patch(
                                "dashboard_sources.resolve_custom_flow_values", return_value={}
                            ):
                                flow = miner_flow_data.build_flow_response(
                                    str(base), battery_power_kw=-9.0
                                )

        self.assertNotIn("pv", asked)
        self.assertNotIn("kelag", asked)
        self.assertNotIn("battery_power", asked)
        self.assertIn("wohnhaus", asked)
        self.assertAlmostEqual(flow["role_sums"]["erzeugung"], 7.7, places=3)
        self.assertAlmostEqual(flow["role_sums"]["netz"], -1.2, places=3)
        self.assertAlmostEqual(flow["role_sums"]["speicher"], 0.8, places=3)
        self.assertAlmostEqual(flow["role_sums"]["verbraucher"], 1.7, places=3)
        self.assertEqual(flow["pv"], "4.2")
        self.assertEqual(flow["grid"], "-1.2")
        by_point = {item["point"]: item["kw"] for item in flow["role_details"]}
        self.assertEqual(by_point["pv"], 4.2)
        self.assertEqual(by_point["kelag"], -1.2)
        self.assertEqual(by_point["battery"], 0.8)
        self.assertEqual(by_point["custom:wrbb"], 1.5)

    def test_without_direct_values_file_fallback_ignores_ha_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "pv_value.txt").write_text("3.0", encoding="utf-8")
            (em / "energy_value.txt").write_text("0.4", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"
            asked = []

            def fake_signed(field_key, ha_cfg, snapshot):
                asked.append(field_key)
                if field_key in ("pv", "kelag", "battery_power"):
                    return 99.0
                return None

            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=self._dash()):
                with mock.patch.object(miner_flow_data, "_signed_ha_field_kw", side_effect=fake_signed):
                    with mock.patch.object(
                        miner_flow_data,
                        "_control_logger_builtin_kw",
                        return_value={"pv": None, "kelag": None, "battery": None},
                    ):
                        with mock.patch("bms_seite.extra_inverter_flow_values", return_value=[]):
                            with mock.patch(
                                "dashboard_sources.resolve_custom_flow_values", return_value={}
                            ):
                                flow = miner_flow_data.build_flow_response(
                                    str(base), battery_power_kw=-0.8
                                )

        self.assertNotIn("pv", asked)
        self.assertNotIn("kelag", asked)
        self.assertNotIn("battery_power", asked)
        self.assertAlmostEqual(flow["role_sums"]["erzeugung"], 3.0, places=3)
        self.assertAlmostEqual(flow["role_sums"]["netz"], 0.4, places=3)
        self.assertAlmostEqual(flow["role_sums"]["speicher"], -0.8, places=3)
        self.assertEqual(flow["pv"], "3.0")
        self.assertEqual(flow["grid"], "0.4")


if __name__ == "__main__":
    unittest.main()
