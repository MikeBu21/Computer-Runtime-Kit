# -*- coding: utf-8 -*-
"""Verbraucher-Summe, Statistik und umbenennbares Wohnhaus."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard_config
import dashboard_sources
import flow_roles
import miner_flow_data


def _cfg(**extra):
    data = {
        "flow_hidden_builtins": ["betrieb", "wasserkraft"],
        "flow_point_roles": dict(flow_roles.DEFAULT_BUILTIN_ROLES),
        "flow_builtin_labels": {},
        "custom_flow_fields": [],
        "pie_consumption": [
            {
                "id": "con_haus",
                "label": "Wohnhaus",
                "source": "field.wohnhaus",
                "color": "#9ccc65",
                "enabled": True,
            }
        ],
        "line_series": [
            {
                "id": "line_verbrauch",
                "label": "Aktueller Verbrauch",
                "source": "field.wohnhaus",
                "color": "#ffea00",
                "fill": False,
                "y_axis": "kw",
                "enabled": True,
            }
        ],
    }
    data.update(extra)
    return dashboard_config.normalize_config(data)


class ConsumerSumTests(unittest.TestCase):
    def test_sums_every_consumer_role_field_into_verbrauch_gesamt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "wohnhaus_value.txt").write_text("1.0", encoding="utf-8")
            (em / "pv_value.txt").write_text("3.0", encoding="utf-8")
            (em / "energy_value.txt").write_text("0.2", encoding="utf-8")
            os.environ["MINER_RAM_CACHE"] = "0"

            cfg = _cfg(
                custom_flow_fields=[
                    {
                        "id": "ups",
                        "name": "UPS NOTSTROM Haus",
                        "enabled": True,
                        "role": "verbraucher",
                        "sensors": [],
                    },
                    {
                        "id": "pool",
                        "name": "Pool",
                        "enabled": True,
                        "role": "verbraucher",
                        "sensors": [],
                    },
                    {
                        "id": "wr2",
                        "name": "WR2",
                        "enabled": True,
                        "role": "erzeugung",
                        "sensors": [],
                    },
                ]
            )

            with mock.patch.object(miner_flow_data, "_load_dash_cfg", return_value=cfg):
                with mock.patch.object(miner_flow_data, "_signed_ha_field_kw", return_value=None):
                    with mock.patch.object(
                        miner_flow_data,
                        "_control_logger_builtin_kw",
                        return_value={"pv": None, "kelag": None, "battery": None},
                    ):
                        with mock.patch(
                            "dashboard_sources.resolve_custom_flow_values",
                            return_value={
                                "ups": {"id": "ups", "name": "UPS NOTSTROM Haus", "value_kw": 0.4},
                                "pool": {"id": "pool", "name": "Pool", "value_kw": 0.6},
                                "wr2": {"id": "wr2", "name": "WR2", "value_kw": 1.25},
                            },
                        ):
                            flow = miner_flow_data.build_flow_response(str(base))

        self.assertEqual(flow["role_sums"]["verbraucher"], 2.0)
        self.assertEqual(flow["verbrauch_gesamt"], "2")
        stats = flow["consumer_statistics"]
        self.assertEqual(stats["total"]["label"], "Verbrauch gesamt")
        self.assertAlmostEqual(stats["total"]["kw"], 2.0)
        self.assertEqual(
            [item["label"] for item in stats["items"]],
            ["Wohnhaus", "UPS NOTSTROM Haus", "Pool"],
        )
        self.assertAlmostEqual(sum(item["kw"] for item in stats["items"]), stats["total"]["kw"])
        self.assertNotIn("WR2", [item["label"] for item in stats["items"]])


class ConsumerStatisticsTests(unittest.TestCase):
    def test_statistics_include_total_and_each_named_consumer(self):
        cfg = _cfg(
            flow_builtin_labels={"wohnhaus": "Gartenhaus"},
            custom_flow_fields=[
                {
                    "id": "ups",
                    "name": "UPS NOTSTROM Haus",
                    "enabled": True,
                    "role": "verbraucher",
                    "sensors": [],
                },
                {
                    "id": "pv2",
                    "name": "WR Ost",
                    "enabled": True,
                    "role": "erzeugung",
                    "sensors": [],
                },
            ],
        )
        flow = {
            "role_details": [
                {"point": "wohnhaus", "role": "verbraucher", "kw": 1.2},
                {"point": "custom:ups", "role": "verbraucher", "kw": 0.8},
                {"point": "custom:pv2", "role": "erzeugung", "kw": 3.0},
            ],
            "haus": "1.2",
        }
        stats = dashboard_config.build_consumer_statistics(cfg, flow)
        labels = [item["label"] for item in stats["items"]]
        self.assertEqual(stats["total"]["label"], "Verbrauch gesamt")
        self.assertEqual(stats["total"]["source"], "flow.verbrauch_gesamt")
        self.assertEqual(labels, ["Gartenhaus", "UPS NOTSTROM Haus"])
        self.assertNotIn("Wohnhaus", labels)
        self.assertAlmostEqual(stats["total"]["kw"], 2.0)
        self.assertAlmostEqual(sum(item["kw"] for item in stats["items"]), stats["total"]["kw"])

        enabled = [sl for sl in cfg["pie_consumption"] if sl.get("enabled")]
        sources = [sl["source"] for sl in enabled]
        self.assertIn("flow.verbrauch_gesamt", sources)
        self.assertIn("field.wohnhaus", sources)
        self.assertIn("custom.ups", sources)
        self.assertNotIn("custom.pv2", sources)
        pie = dashboard_sources.build_pie_response(
            {"con_haus": 1.2, "con_custom_ups": 0.8},
            cfg["pie_consumption"],
        )
        self.assertIn("Gartenhaus", pie["labels"])
        self.assertIn("UPS NOTSTROM Haus", pie["labels"])
        self.assertNotIn("Wohnhaus", pie["labels"])
        self.assertNotIn("Verbrauch gesamt", pie["labels"])
        self.assertAlmostEqual(sum(pie["values"]), 2.0)

        lines = {
            ser["source"]: ser["label"]
            for ser in cfg["line_series"]
            if ser.get("enabled")
        }
        self.assertEqual(lines.get("flow.verbrauch_gesamt"), "Verbrauch gesamt")
        self.assertEqual(lines.get("field.wohnhaus"), "Gartenhaus")
        self.assertEqual(lines.get("custom.ups"), "UPS NOTSTROM Haus")

        options = {
            opt["value"]: opt["label"] for opt in dashboard_config.source_options_for_flow(cfg)
        }
        self.assertEqual(options["field.wohnhaus"], "Gartenhaus")
        self.assertEqual(options["custom.ups"], "UPS NOTSTROM Haus")
        self.assertEqual(options["flow.verbrauch_gesamt"], "Verbrauch gesamt")

    def test_unrenamed_wohnhaus_keeps_default_label(self):
        cfg = _cfg()
        self.assertEqual(dashboard_config.builtin_label(cfg, "wohnhaus"), "Wohnhaus")
        haus = next(sl for sl in cfg["pie_consumption"] if sl.get("source") == "field.wohnhaus")
        self.assertEqual(haus["label"], "Wohnhaus")


if __name__ == "__main__":
    unittest.main()
