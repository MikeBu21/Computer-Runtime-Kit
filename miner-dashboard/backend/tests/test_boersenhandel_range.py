# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from boersenhandel import chart_window, slots_for_chart, tariff_grid_kw

TZ = ZoneInfo("Europe/Vienna")


def _slot(day: str, hour: int) -> dict:
    start = datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=TZ)
    start_ms = int(start.timestamp() * 1000)
    return {
        "day": day,
        "hour": f"{hour:02d}:00",
        "start_ms": start_ms,
        "end_ms": start_ms + 3600 * 1000,
        "ct_kwh": 10.0,
    }


class TariffAmountTests(unittest.TestCase):
    def test_buy_and_sell_use_the_entered_amount_at_once(self):
        self.assertEqual(tariff_grid_kw(10, 19.9, 36.5, 3.5, 4, 8.0, 13.18), 3.5)
        self.assertEqual(tariff_grid_kw(10, 19.9, 36.5, 10, 4, 6.5, 13.18), 6.5)
        self.assertEqual(tariff_grid_kw(40, 19.9, 36.5, 3.5, 4, 6.5, 13.18), -4.0)
        self.assertIsNone(tariff_grid_kw(25, 19.9, 36.5, 3.5, 4, 8.0, 13.18))

class ChartWindowTests(unittest.TestCase):
    def test_four_days_back_from_midnight_plus_next_day(self):
        now = datetime(2026, 11, 1, 17, 50, tzinfo=TZ)
        start, end = chart_window(now)
        self.assertEqual(start.strftime("%Y-%m-%d %H:%M"), "2026-10-29 00:00")
        self.assertEqual(end.strftime("%Y-%m-%d %H:%M"), "2026-11-03 00:00")

    def test_history_before_today_stays_visible(self):
        now = datetime(2026, 11, 1, 17, 50, tzinfo=TZ)
        slots = [
            _slot("2026-10-28", 12),
            _slot("2026-10-29", 0),
            _slot("2026-10-31", 15),
            _slot("2026-11-01", 8),
            _slot("2026-11-02", 20),
            _slot("2026-11-03", 1),
        ]
        shown = slots_for_chart(slots, now, "4d")
        days = [slot["day"] for slot in shown]
        self.assertEqual(days, ["2026-10-29", "2026-10-31", "2026-11-01", "2026-11-02"])

    def test_day_mode_is_only_today(self):
        now = datetime(2026, 11, 1, 17, 50, tzinfo=TZ)
        slots = [_slot("2026-10-31", 10), _slot("2026-11-01", 9), _slot("2026-11-02", 9)]
        shown = slots_for_chart(slots, now, "day")
        self.assertEqual([slot["day"] for slot in shown], ["2026-11-01"])


if __name__ == "__main__":
    unittest.main()
