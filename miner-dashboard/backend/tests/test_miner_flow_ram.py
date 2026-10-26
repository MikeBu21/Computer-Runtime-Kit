# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path

import miner_flow_data
import ram_cache


class MinerFlowRamTests(unittest.TestCase):
    def test_build_flow_prefers_ram_over_stale_sd(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["MINER_RAM_CACHE"] = "1"
            os.environ["MINER_RAM_ROOT"] = str(root / "ram")
            base = root / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)

            (em / "wohnhaus_value.txt").write_text("1.789", encoding="utf-8")
            (em / "pv_value.txt").write_text("0.023", encoding="utf-8")
            (em / "energy_value.txt").write_text("0.0", encoding="utf-8")

            ram_cache.write_text(base, "wohnhaus_value.txt", "0.547", scope="energy_monitor")
            ram_cache.write_text(base, "pv_value.txt", "0.0", scope="energy_monitor")
            ram_cache.write_text(base, "energy_value.txt", "0.017", scope="energy_monitor")

            flow = miner_flow_data.build_flow_response(str(base))
            self.assertEqual(flow["haus"], "0.547")
            self.assertEqual(flow["pv"], "0.0")
            self.assertEqual(flow["grid"], "0.017")

    def test_build_flow_falls_back_to_sd_when_ram_off(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["MINER_RAM_CACHE"] = "0"
            base = root / "backend"
            em = base / "energy_monitor"
            em.mkdir(parents=True)
            (em / "wohnhaus_value.txt").write_text("1.2", encoding="utf-8")
            (em / "pv_value.txt").write_text("3.4", encoding="utf-8")
            (em / "energy_value.txt").write_text("0.5", encoding="utf-8")

            flow = miner_flow_data.build_flow_response(str(base))
            self.assertEqual(flow["haus"], "1.2")
            self.assertEqual(flow["pv"], "3.4")
            self.assertEqual(flow["grid"], "0.5")


if __name__ == "__main__":
    unittest.main()
