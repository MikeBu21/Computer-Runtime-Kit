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
        self.assertEqual(days, ["2026-10-29", "2026-10-31", "2026-11-01"])
        self.assertNotIn("2026-11-02", days)
        self.assertNotIn("2026-11-03", days)

    def test_day_mode_is_only_today(self):
        now = datetime(2026, 11, 1, 17, 50, tzinfo=TZ)
        slots = [_slot("2026-10-31", 10), _slot("2026-11-01", 9), _slot("2026-11-02", 9)]
        shown = slots_for_chart(slots, now, "day")
        self.assertEqual([slot["day"] for slot in shown], ["2026-11-01"])

    def test_future_quarter_on_3_nov_is_not_plotted(self):
        now = datetime(2026, 11, 2, 16, 50, tzinfo=TZ)
        now_ms = int(now.timestamp() * 1000)
        slots = [
            _quarter("2026-11-02", 16, 30, 34.307),
            _quarter("2026-11-02", 16, 45, 34.515),
            _quarter("2026-11-02", 17, 0, 33.137),
            _quarter("2026-11-02", 23, 45, 21.862),
            _quarter("2026-11-03", 23, 45, 24.439),
        ]
        current = slot_covering(slots, now_ms)
        self.assertIsNotNone(current)
        self.assertEqual(current["hour"], "16:45")
        self.assertEqual(current["ct_kwh"], 34.515)
        for mode in ("day", "4d"):
            shown = slots_for_chart(slots, now, mode)
            self.assertEqual(shown[-1]["day"], "2026-11-02")
            self.assertEqual(shown[-1]["hour"], "16:45")
            self.assertEqual(shown[-1]["ct_kwh"], 34.515)
            self.assertTrue(all(slot["start_ms"] <= now_ms for slot in shown))
            self.assertFalse(any(slot["day"] == "2026-11-03" for slot in shown))
            self.assertFalse(any(slot["hour"] == "23:45" for slot in shown))

    def test_live_quarter_close_matches_headline_and_open_is_previous(self):
        now = datetime(2026, 11, 2, 16, 50, tzinfo=TZ)
        slots = [
            _quarter("2026-11-02", 16, 30, 34.307),
            _quarter("2026-11-02", 16, 45, 34.515),
            _quarter("2026-11-02", 17, 0, 33.137),
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
            for mode in ("day", "4d"):
                view = price_view("eag_energieag", mode, settings)
                self.assertEqual(view["current_ct_kwh"], 34.515)
                self.assertEqual(view["current_hour"], "16:45")
                points = view["points"]
                self.assertGreaterEqual(len(points), 2)
                self.assertEqual(points[-1]["day"], "2026-11-02")
                self.assertEqual(points[-1]["hour"], "16:45")
                self.assertEqual(points[-1]["close"], view["current_ct_kwh"])
                self.assertEqual(points[-1]["close"], points[-1]["ct_kwh"])
                self.assertEqual(points[-1]["open"], 34.307)
                self.assertNotEqual(points[-1]["open"], points[-1]["close"])
                self.assertEqual(points[-1]["high"], max(points[-1]["open"], points[-1]["close"]))
                self.assertEqual(points[-1]["low"], min(points[-1]["open"], points[-1]["close"]))
                self.assertFalse(
                    points[-1]["open"] == points[-1]["high"] == points[-1]["low"] == points[-1]["close"]
                )
                self.assertTrue(all(point["day"] != "2026-11-03" for point in points))
                self.assertTrue(all(point["hour"] != "23:45" for point in points))
                self.assertNotIn("17:00", [point["hour"] for point in points])
                for index, point in enumerate(points):
                    self.assertLessEqual(point["time"], int(now.timestamp()))
                    self.assertEqual(point["close"], point["ct_kwh"])
                    if index == 0:
                        self.assertEqual(point["open"], point["close"])
                    else:
                        self.assertEqual(point["open"], points[index - 1]["ct_kwh"])
                    self.assertEqual(point["high"], max(point["open"], point["close"]))
                    self.assertEqual(point["low"], min(point["open"], point["close"]))
                    if point["open"] != point["close"]:
                        self.assertNotEqual(point["high"], point["low"])
                        self.assertFalse(point["open"] == point["high"] == point["low"] == point["close"])

    def test_day_candle_opens_on_the_previous_slot_outside_today(self):
        now = datetime(2026, 11, 2, 0, 10, tzinfo=TZ)
        slots = [
            _quarter("2026-11-01", 23, 45, 20.0),
            _quarter("2026-11-02", 0, 0, 22.5),
        ]

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        settings = {"buy_ct_kwh": 17.0, "sell_ct_kwh": 58.8, "buy_kw": 0.0, "sell_kw": 0.0}
        with patch.object(boersenhandel, "_fetch_slots", return_value=slots), patch.object(
            boersenhandel, "datetime", _Clock
        ):
            view = price_view("eag_energieag", "day", settings)
        self.assertEqual(view["current_ct_kwh"], 22.5)
        self.assertEqual(len(view["points"]), 1)
        point = view["points"][0]
        self.assertEqual(point["close"], 22.5)
        self.assertEqual(point["open"], 20.0)
        self.assertEqual(point["high"], 22.5)
        self.assertEqual(point["low"], 20.0)
        self.assertNotEqual(point["open"], point["close"])

    def test_source_ohlc_is_kept_and_close_stays_the_slot_price(self):
        now = datetime(2026, 11, 2, 15, 40, tzinfo=TZ)
        slots = [
            _quarter("2026-11-02", 15, 15, 30.0),
            _quarter("2026-11-02", 15, 30, 34.675),
        ]
        slots[1]["ohlc"] = {"open": 31.0, "high": 40.0, "low": 29.5, "close": 99.0}

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        settings = {"buy_ct_kwh": 17.0, "sell_ct_kwh": 58.8, "buy_kw": 0.0, "sell_kw": 0.0}
        with patch.object(boersenhandel, "_fetch_slots", return_value=slots), patch.object(
            boersenhandel, "datetime", _Clock
        ):
            view = price_view("eag_energieag", "day", settings)
        live = next(point for point in view["points"] if point["hour"] == "15:30")
        self.assertEqual(view["current_ct_kwh"], 34.675)
        self.assertEqual(live["close"], 34.675)
        self.assertEqual(live["open"], 31.0)
        self.assertNotEqual(live["open"], live["close"])
        self.assertNotEqual(live["open"], 30.0)
        self.assertEqual(live["high"], 40.0)
        self.assertEqual(live["low"], 29.5)

    def test_charts_mark_the_live_quarter_not_the_last_bar(self):
        text = Path(__file__).resolve().parents[1].joinpath("templates", "boersenhandel.html").read_text(encoding="utf-8")
        self.assertIn("priceLineVisible: false", text)
        self.assertIn("lastValueVisible: false", text)
        self.assertNotIn("priceLineVisible: true", text)
        self.assertIn("syncLivePriceLine", text)
        self.assertIn("data.current_ct_kwh", text)
        self.assertIn("time > nowSec", text)
        self.assertIn("addCandlestickSeries", text)
        self.assertNotIn("addHistogramSeries", text)
        self.assertIn("open: row.open", text)
        self.assertIn("high: row.high", text)
        self.assertIn("low: row.low", text)
        self.assertIn("close: row.close", text)
        self.assertIn("row.close >= row.open", text)
        self.assertNotIn("open: row.price", text)
        self.assertNotIn("value: row.price", text)
        self.assertNotIn("row.close - direction", text)
        self.assertNotIn("row.price +", text)
        self.assertNotIn("row.close +", text)


if __name__ == "__main__":
    unittest.main()
