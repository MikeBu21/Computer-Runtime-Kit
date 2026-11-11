# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import flow_roles
import miner_flow_data


class FlowRolesTests(unittest.TestCase):
    def test_normalize_and_defaults(self):
        self.assertEqual(flow_roles.normalize_role("Erzeugung"), "erzeugung")
        self.assertEqual(flow_roles.normalize_role("produzent"), "erzeugung")
        self.assertEqual(flow_roles.normalize_role("unknown"), "verbraucher")
        roles = flow_roles.normalize_flow_point_roles({})
        self.assertEqual(roles["pv"], "erzeugung")
        self.assertEqual(roles["kelag"], "netz")
        self.assertEqual(roles["battery"], "speicher")
        self.assertEqual(roles["wohnhaus"], "verbraucher")
        self.assertEqual(roles["betrieb"], "verbraucher")

    def test_apply_sensor_sign_minus_subtracts(self):
        self.assertEqual(flow_roles.apply_sensor_sign(5.0, "+"), 5.0)
        self.assertEqual(flow_roles.apply_sensor_sign(5.0, "-"), -5.0)
        self.assertEqual(flow_roles.apply_sensor_sign(0.005, "-"), -0.005)
        self.assertIsNone(flow_roles.apply_sensor_sign(None, "-"))

    def test_sum_signed_sensors_plus_minus(self):
        # +Haus 2 kW, −External 0.005 kW → 1.995
        total = flow_roles.sum_signed_sensor_values(
            [
                {"entity_id": "sensor.haus", "sign": "+"},
                {"entity_id": "sensor.external", "sign": "-"},
            ],
            {"sensor.haus": 2.0, "sensor.external": 0.005},
        )
        self.assertAlmostEqual(total, 1.995, places=3)

    def test_role_sum_keeps_minus_contributions(self):
        # Punkt A (+3) und Punkt B (−0.5, Minus-Sensor) in Rolle Erzeugung
        sums = flow_roles.sum_values_by_role(
            [
                ("erzeugung", 3.0),
                ("erzeugung", flow_roles.apply_sensor_sign(0.5, "-")),
                ("verbraucher", 0.7),
                ("verbraucher", 0.3),
                ("netz", -0.4),
            ]
        )
        self.assertEqual(sums["erzeugung"], 2.5)
        self.assertEqual(sums["verbraucher"], 1.0)
        self.assertEqual(sums["netz"], -0.4)
        self.assertEqual(sums["speicher"], 0.0)


class MinerFlowRoleSumTests(unittest.TestCase):
    def test_verbrauch_sums_all_verbraucher_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "wohnhaus_value.txt").write_text("1.0", encoding="utf-8")
            (em / "betrieb_value.txt").write_text("0.5", encoding="utf-8")
            (em / "pv_value.txt").write_text("3.0", encoding="utf-8")
            (em / "wasserkraft_value.txt").write_text("2.0", encoding="utf-8")
            (em / "energy_value.txt").write_text("0.1", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"

            dash_cfg = {
                "flow_hidden_builtins": [],
                "flow_point_roles": dict(flow_roles.DEFAULT_BUILTIN_ROLES),
                "custom_flow_fields": [
                    {
                        "id": "inv2",
                        "name": "WR2",
                        "enabled": True,
                        "role": "erzeugung",
                        "sensors": [],
                    }
                ],
            }

            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=dash_cfg):
                with mock.patch.object(miner_flow_data, "_signed_ha_field_kw", return_value=None):
                    with mock.patch(
                        "dashboard_sources.resolve_custom_flow_values",
                        return_value={
                            "inv2": {
                                "id": "inv2",
                                "name": "WR2",
                                "value_kw": 1.25,
                                "display": "1.250",
                            }
                        },
                    ):
                        flow = miner_flow_data.build_flow_response(
                            str(base), battery_power_kw=-0.8
                        )

            self.assertEqual(flow["role_sums"]["erzeugung"], 6.25)  # 3 + 2 + 1.25
            self.assertEqual(flow["role_sums"]["verbraucher"], 1.5)  # 1.0 + 0.5
            self.assertEqual(flow["role_sums"]["netz"], 0.1)
            self.assertEqual(flow["role_sums"]["speicher"], -0.8)
            self.assertEqual(flow["verbrauch_gesamt"], "1.5")
            self.assertEqual(flow["erzeugung_gesamt"], "6.2")  # format_kw rounds 6.25 → 6.2
            self.assertIn("inv2", flow["custom_fields"])

    def test_minus_prefix_sensor_is_subtracted_from_role(self):
        """Wohnhaus: +Last 2 kW und −External 0.005 kW → Verbraucher 1.995."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "pv_value.txt").write_text("0", encoding="utf-8")
            (em / "energy_value.txt").write_text("0", encoding="utf-8")
            # Datei hätte den Roh-Wert ohne Vorzeichen-Korrektur — Live-HA gewinnt
            (em / "wohnhaus_value.txt").write_text("2.0", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"

            dash_cfg = {
                "flow_hidden_builtins": [],
                "flow_point_roles": dict(flow_roles.DEFAULT_BUILTIN_ROLES),
                "custom_flow_fields": [],
            }

            def fake_signed(field_key, ha_cfg, snapshot):
                if field_key == "wohnhaus":
                    # compute_field_value-Äquivalent: +2.0 + (−0.005)
                    return flow_roles.sum_signed_sensor_values(
                        [
                            {"entity_id": "sensor.load", "sign": "+"},
                            {"entity_id": "sensor.external", "sign": "-"},
                        ],
                        {"sensor.load": 2.0, "sensor.external": 0.005},
                    )
                return None

            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=dash_cfg):
                with mock.patch.object(
                    miner_flow_data, "_signed_ha_field_kw", side_effect=fake_signed
                ):
                    with mock.patch(
                        "dashboard_sources.resolve_custom_flow_values", return_value={}
                    ):
                        flow = miner_flow_data.build_flow_response(str(base))

            self.assertAlmostEqual(flow["role_sums"]["verbraucher"], 1.995, places=3)
            self.assertEqual(flow["verbrauch_gesamt"], "2")  # format_kw rundet 1.995 → 2.0 → "2"


if __name__ == "__main__":
    unittest.main()
