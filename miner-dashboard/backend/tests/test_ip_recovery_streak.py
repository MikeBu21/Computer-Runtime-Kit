"""Tests für Miss-Streak vor Geräte-Nachfrage."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestRecoveryMissStreak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.miners_path = self.root / "miners.json"
        self.notify_path = self.root / "ip_recovery_notifications.json"
        self.notify_path.write_text("[]\n", encoding="utf-8")

        import ip_recovery as ir

        self.ir = ir
        self._patches = [
            mock.patch.object(ir, "MINERS_PATH", self.miners_path),
            mock.patch.object(ir, "NOTIFY_PATH", self.notify_path),
            mock.patch.object(ir, "LOCK_PATH", self.root / ".ip_recovery.lock"),
            mock.patch.object(ir, "_notify_path", lambda: self.notify_path),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _write_miner(self, **extra):
        entry = {
            "ip": "192.168.1.50",
            "mac": "aa:bb:cc:dd:ee:ff",
            "note": "TestMiner",
            "recovery_state": "ok",
            "recovery_miss_streak": 0,
        }
        entry.update(extra)
        self.miners_path.write_text(
            json.dumps([entry], indent=2) + "\n", encoding="utf-8"
        )
        return entry

    def test_asks_only_after_20_misses(self):
        self._write_miner()
        miner = self.ir.load_miners()[0]

        for i in range(1, self.ir.MISSING_CONFIRM_STREAK):
            result = self.ir.record_recovery_miss(miner, daily=False)
            self.assertEqual(result.action, "searching", f"fail at {i}")
            live = self.ir.load_miners()[0]
            self.assertEqual(live["recovery_state"], "searching")
            self.assertEqual(live["recovery_miss_streak"], i)
            self.assertEqual(self.ir.pop_notifications(), [])

        result = self.ir.record_recovery_miss(miner, daily=False)
        self.assertEqual(result.action, "missing")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_state"], "missing")
        notes = self.ir.pop_notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "device_missing")

    def test_confirmed_user_not_asked_again(self):
        self._write_miner(
            recovery_state="awaiting_user",
            user_confirmed_exists=True,
            recovery_miss_streak=0,
        )
        miner = self.ir.load_miners()[0]
        result = self.ir.record_recovery_miss(miner, daily=True)
        self.assertEqual(result.action, "missing")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_state"], "awaiting_user")
        self.assertEqual(self.ir.pop_notifications(), [])
        self.assertEqual(self.ir.list_pending_recovery(), [])

    def test_reachable_clears_streak(self):
        self._write_miner(recovery_state="searching", recovery_miss_streak=7)
        self.ir.mark_miner_reachable("192.168.1.50")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_state"], "ok")
        self.assertEqual(live["recovery_miss_streak"], 0)

    def test_miss_then_find_resets_to_zero(self):
        """Nach Misses und erneutem Fund muss Suche wieder bei 0 starten."""
        self._write_miner()
        miner = self.ir.load_miners()[0]
        for _ in range(3):
            self.ir.record_recovery_miss(miner, daily=False)
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_miss_streak"], 3)
        self.assertEqual(live["recovery_state"], "searching")

        self.ir.mark_miner_reachable("192.168.1.50")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_miss_streak"], 0)
        self.assertEqual(live["recovery_state"], "ok")

        # Nächster Miss zählt wieder bei 1, nicht 4
        result = self.ir.record_recovery_miss(self.ir.load_miners()[0], daily=False)
        self.assertEqual(result.action, "searching")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_miss_streak"], 1)

    def test_try_recover_same_ip_clears_streak(self):
        self._write_miner(
            recovery_state="searching",
            recovery_miss_streak=5,
            mac="aa:bb:cc:dd:ee:ff",
        )
        miner = self.ir.load_miners()[0]
        with mock.patch.object(self.ir, "resolve_ip_by_mac", return_value="192.168.1.50"):
            with mock.patch.object(self.ir, "_should_immediate_scan", return_value=True):
                result = self.ir.try_recover_miner(miner, force=True)
        self.assertEqual(result.action, "none")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_miss_streak"], 0)
        self.assertEqual(live["recovery_state"], "ok")

    def test_try_recover_probe_find_clears_before_miss(self):
        """ARP kurz ohne MAC, aber Probe findet Gerät → kein Streak-Hochzählen."""
        self._write_miner(
            recovery_state="searching",
            recovery_miss_streak=4,
            mac="aa:bb:cc:dd:ee:ff",
        )
        miner = self.ir.load_miners()[0]
        with mock.patch.object(self.ir, "resolve_ip_by_mac", return_value=None):
            with mock.patch.object(
                self.ir,
                "discover_mac_for_ip",
                return_value=("aa:bb:cc:dd:ee:ff", "arp"),
            ):
                with mock.patch.object(self.ir, "store_mac_on_miner"):
                    with mock.patch.object(
                        self.ir, "_should_immediate_scan", return_value=True
                    ):
                        result = self.ir.try_recover_miner(miner, force=True)
        self.assertEqual(result.action, "none")
        live = self.ir.load_miners()[0]
        self.assertEqual(live["recovery_miss_streak"], 0)
        self.assertEqual(live["recovery_state"], "ok")


if __name__ == "__main__":
    unittest.main()
