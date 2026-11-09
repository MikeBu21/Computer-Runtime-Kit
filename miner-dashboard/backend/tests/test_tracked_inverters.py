# -*- coding: utf-8 -*-
"""Wechselrichter-Liste: Scan-Markierung ohne Netzwerk."""
import unittest

from bms_seite import annotate_discovery


class TrackedInverterDiscoveryTests(unittest.TestCase):
    def test_marks_known_mac_and_keeps_new_ones(self):
        found = [
            {"ip": "192.168.1.176", "mac": "D4:27:87:0A:D7:B4", "serial": 2965822000},
            {"ip": "192.168.1.50", "mac": "aa:bb:cc:dd:ee:ff", "serial": 123},
        ]
        tracked = [{"mac": "d4:27:87:0a:d7:b4", "control": True}]
        marked = annotate_discovery(found, tracked)
        by_mac = {item["mac"]: item for item in marked}
        self.assertTrue(by_mac["d4:27:87:0a:d7:b4"]["tracked"])
        self.assertFalse(by_mac["aa:bb:cc:dd:ee:ff"]["tracked"])
        self.assertEqual(by_mac["aa:bb:cc:dd:ee:ff"]["serial"], 123)


if __name__ == "__main__":
    unittest.main()
