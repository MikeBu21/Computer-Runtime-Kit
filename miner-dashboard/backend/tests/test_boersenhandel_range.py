# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import boersenhandel
from boersenhandel import chart_window, price_view, slot_covering, slots_for_chart, tariff_grid_kw

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


def _quarter(day: str, hour: int, minute: int, ct: float) -> dict:
    start = datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=TZ)
    start_ms = int(start.timestamp() * 1000)
    return {
        "day": day,
        "hour": f"{hour:02d}:{minute:02d}",
        "start_ms": start_ms,
        "end_ms": start_ms + 15 * 60 * 1000,
        "ct_kwh": ct,
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

    def test_live_price_is_the_open_quarter_not_the_last_candle(self):
        now = datetime(2026, 11, 2, 15, 40, tzinfo=TZ)
        now_ms = int(now.timestamp() * 1000)
        slots = [
            _quarter("2026-11-02", 15, 30, 34.675),
            _quarter("2026-11-02", 23, 45, 21.862),
            _quarter("2026-11-03", 23, 45, 24.439),
        ]
        current = slot_covering(slots, now_ms)
        self.assertIsNotNone(current)
        self.assertEqual(current["ct_kwh"], 34.675)
        self.assertEqual(current["hour"], "15:30")
        self.assertEqual(slots_for_chart(slots, now, "day")[-1]["ct_kwh"], 21.862)
        self.assertEqual(slots_for_chart(slots, now, "4d")[-1]["ct_kwh"], 24.439)

    def test_live_quarter_candle_equals_headline_price(self):
        now = datetime(2026, 11, 2, 15, 40, tzinfo=TZ)
        slots = [
            _quarter("2026-11-02", 15, 15, 33.908),
            _quarter("2026-11-02", 15, 30, 34.675),
            _quarter("2026-11-02", 16, 0, 33.376),
            _quarter("2026-11-02", 23, 45, 21.862),
            _quarter("2026-11-03", 23, 45, 24.439),
        ]

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        settings = {"buy_ct_kwh": 17.0, "sell_ct_kwh": 58.8, "buy_kw": 0.0, "sell_kw": 0.0}
        with patch.object(boersenhandel, "_fetch_slots", return_value=slots), patch.object(
            boersenhandel, "datetime", _Clock
        ):
            for mode, last_ct in (("day", 21.862), ("4d", 24.439)):
                view = price_view("eag_energieag", mode, settings)
                self.assertEqual(view["current_ct_kwh"], 34.675)
                points = view["points"]
                live = next(point for point in points if point["day"] == "2026-11-02" and point["hour"] == "15:30")
                previous = next(point for point in points if point["hour"] == "15:15")
                following = next(point for point in points if point["hour"] == "16:00")
                for field in ("open", "high", "low", "close"):
                    self.assertEqual(live[field], view["current_ct_kwh"])
                    self.assertEqual(live[field], live["ct_kwh"])
                    self.assertNotEqual(live[field], previous["ct_kwh"])
                    self.assertNotEqual(live[field], following["ct_kwh"])
                    self.assertNotEqual(live[field], last_ct)
                for index, point in enumerate(points):
                    for field in ("open", "high", "low", "close"):
                        self.assertEqual(point[field], point["ct_kwh"])
                    if index:
                        neighbor = points[index - 1]["ct_kwh"]
                        if neighbor != point["ct_kwh"]:
                            self.assertNotEqual(point["open"], neighbor)
                            self.assertNotEqual(point["low"], neighbor)
                    if index + 1 < len(points):
                        neighbor = points[index + 1]["ct_kwh"]
                        if neighbor != point["ct_kwh"]:
                            self.assertNotEqual(point["close"], neighbor)
                            self.assertNotEqual(point["high"], neighbor)
                self.assertEqual(points[-1]["ct_kwh"], last_ct)
                self.assertEqual(points[-1]["open"], last_ct)
                self.assertEqual(points[-1]["close"], last_ct)

    def test_charts_mark_the_live_quarter_not_the_last_bar(self):
        text = Path(__file__).resolve().parents[1].joinpath("templates", "boersenhandel.html").read_text(encoding="utf-8")
        self.assertIn("priceLineVisible: false", text)
        self.assertIn("lastValueVisible: false", text)
        self.assertNotIn("priceLineVisible: true", text)
        self.assertIn("syncLivePriceLine", text)
        self.assertIn("data.current_ct_kwh", text)
        self.assertIn("open: row.price", text)
        self.assertIn("high: row.price", text)
        self.assertIn("low: row.price", text)
        self.assertIn("close: row.price", text)
        self.assertNotIn("previous == null ? close : previous", text)
        self.assertNotIn("row.close - direction", text)


if __name__ == "__main__":
    unittest.main()
