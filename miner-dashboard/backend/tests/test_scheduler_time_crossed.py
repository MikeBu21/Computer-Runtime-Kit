# -*- coding: utf-8 -*-
"""Zeitsteuerung: Termine auch bei längerer Schleife treffen."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from scheduler import time_crossed


class TestSchedulerTimeCrossed(unittest.TestCase):
    def test_exact_minute(self):
        now = datetime(2026, 9, 24, 8, 30, 10)
        prev = now - timedelta(seconds=30)
        self.assertTrue(time_crossed("08:30", prev, now))

    def test_missed_minute_still_fires(self):
        """Schleife dauerte >60s – exakte Minute wurde übersprungen."""
        prev = datetime(2026, 9, 24, 8, 29, 50)
        now = datetime(2026, 9, 24, 8, 31, 5)
        self.assertTrue(time_crossed("08:30", prev, now))

    def test_not_yet(self):
        prev = datetime(2026, 9, 24, 8, 28, 0)
        now = datetime(2026, 9, 24, 8, 29, 0)
        self.assertFalse(time_crossed("08:30", prev, now))

    def test_already_passed_interval(self):
        prev = datetime(2026, 9, 24, 8, 31, 0)
        now = datetime(2026, 9, 24, 8, 32, 0)
        self.assertFalse(time_crossed("08:30", prev, now))

    def test_midnight_wrap(self):
        prev = datetime(2026, 9, 23, 23, 59, 40)
        now = datetime(2026, 9, 24, 0, 1, 10)
        self.assertTrue(time_crossed("00:00", prev, now))


if __name__ == "__main__":
    unittest.main()
