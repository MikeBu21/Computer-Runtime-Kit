# -*- coding: utf-8 -*-
"""Tests Doppelsteuerung auto_control vs. Steuerungsalgorithmus."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.sync_api", MagicMock())

_EM = Path(__file__).resolve().parent.parent / "energy_monitor"
if str(_EM) not in sys.path:
    sys.path.insert(0, str(_EM))

import auto_control  # noqa: E402


class TestAutoControlSteering(unittest.TestCase):
    def test_algorithm_managed_ips_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "control_algorithm_config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "devices": [
                            {"ip": "10.0.0.1", "enabled": True},
                            {"ip": "10.0.0.2", "enabled": False},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(auto_control, "ALGORITHM_CONFIG_FILE", str(cfg_path)):
                ips = auto_control._algorithm_managed_ips()
            self.assertEqual(ips, {"10.0.0.1"})

    def test_consumption_skips_algorithm_managed_miner(self):
        miner = {"ip": "10.0.0.1", "auto": True, "auto_battery": False}
        with patch.object(auto_control, "_algorithm_managed_ips", return_value={"10.0.0.1"}):
            with patch.object(auto_control, "_apply_action") as mock_apply:
                auto_control.control_miners_consumption(
                    [miner], 50.0, {}, {}
                )
        mock_apply.assert_not_called()

    def test_consumption_skips_when_battery_auto_active(self):
        miner = {"ip": "10.0.0.2", "auto": True, "auto_battery": True}
        with patch.object(auto_control, "_algorithm_managed_ips", return_value=set()):
            with patch.object(auto_control, "_apply_action") as mock_apply:
                auto_control.control_miners_consumption(
                    [miner], 50.0, {}, {}
                )
        mock_apply.assert_not_called()

    def test_battery_skips_algorithm_managed_miner(self):
        miner = {"ip": "10.0.0.1", "auto_battery": True, "raise_kw": "40", "lower_kw": "60"}
        with patch.object(auto_control, "_algorithm_managed_ips", return_value={"10.0.0.1"}):
            with patch.object(auto_control, "_apply_action") as mock_apply:
                auto_control.control_miners_battery(
                    [miner], 30.0, {}, {}
                )
        mock_apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
