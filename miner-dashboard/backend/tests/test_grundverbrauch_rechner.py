# -*- coding: utf-8 -*-
"""Grundverbrauchsrechner: Formel, ohne Miner-Verbrauch, Update-Sperren."""
from __future__ import annotations

import unittest
from datetime import date

from grundverbrauch_rechner import (
    apply_recommendation_locks,
    build_report_from_parts,
    describe_history,
    grid_kwh_for_day,
    merge_base_archive,
)


def _flat(kwh_per_hour: float) -> list:
    return [kwh_per_hour] * 24


def _day(day: str, base_kwh: float, pv_kwh: float | None) -> dict:
    base = _flat(base_kwh / 24.0)
    pv = None if pv_kwh is None else _flat(0.0)
    if pv_kwh is not None:
        # PV nur über den Tag, nicht nachts, Summe = pv_kwh
        from grundverbrauch_rechner import spread_daily_kwh

        pv = spread_daily_kwh(pv_kwh)
    return {
        "date": day,
        "base_hourly": base,
        "hydro_hourly": [0.0] * 24,
        "pv_hourly": pv,
    }


class GridFormulaTests(unittest.TestCase):
    def test_surplus_covers_deficit_after_efficiency(self):
        load = _flat(1.0)
        gen = [0.0] * 24
        gen[12] = 100.0
        bal = grid_kwh_for_day(load, gen, 0.95)
        self.assertEqual(bal["grid_kwh"], 0.0)
        self.assertGreater(bal["cycle_loss_kwh"], 0.0)

    def test_grid_includes_battery_loss(self):
        load = _flat(1.0)  # 24 kWh
        gen = [0.0] * 24
        gen[12] = 10.0
        bal = grid_kwh_for_day(load, gen, 0.95)
        self.assertAlmostEqual(bal["plain_kwh"], 14.0, places=2)
        self.assertGreater(bal["grid_kwh"], bal["plain_kwh"])
        self.assertAlmostEqual(bal["grid_kwh"], 14.45, delta=0.15)

    def test_no_generation_buys_the_load(self):
        bal = grid_kwh_for_day(_flat(2.0), [0.0] * 24, 0.95)
        self.assertAlmostEqual(bal["grid_kwh"], 48.0, places=2)


class ReportTests(unittest.TestCase):
    def test_ignores_days_without_pv_and_short_days(self):
        days = [
            _day("2026-09-20", 16.0, 80.0),
            _day("2026-09-21", 16.0, None),
            {
                "date": "2026-09-22",
                "base_hourly": [1.0] * 8 + [0.0] * 16,
                "hydro_hourly": [0.0] * 24,
                "pv_hourly": _day("2026-09-22", 16, 80)["pv_hourly"],
            },
            {
                "date": "2026-09-29",
                "base_hourly": [0.0] * 24,
                "hydro_hourly": [0.0] * 24,
                "pv_hourly": None,
                "base_so_far_kwh": 8.0,
                "hours_elapsed": 10.0,
                "pv_so_far_kwh": 40.0,
                "today_hourly_actual": [0.8] * 10 + [None] * 14,
            },
        ]
        miners = [
            {"ip": "1", "name": "Hydro", "power_kw": 5.0, "algo_enabled": True}
        ]
        report = build_report_from_parts(
            days,
            miners,
            {"1": "off"},
            efficiency=0.95,
            today=date(2026, 9, 29),
            algo_enabled=True,
        )
        self.assertEqual(report["days_used"], 1)
        self.assertEqual(report["weekly_kw"], 0.0)
        self.assertEqual(report["continuous_kw"], 0.0)
        self.assertAlmostEqual(report["averages"]["today"]["base_kwh"], 8.0)
        self.assertTrue(report["info_only"])
        self.assertFalse(report["controls_devices"])
        used = [row for row in report["series"] if row["used"]]
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["iso"], "2026-09-20")

    def test_continuous_miner_raises_purchase_off_does_not(self):
        days = [_day("2026-09-20", 20.0, 40.0)]
        miners = [{"ip": "1", "name": "Hydro", "power_kw": 2.0, "algo_enabled": True}]
        off = build_report_from_parts(
            days, miners, {"1": "off"}, efficiency=1.0, today=date(2026, 9, 21)
        )
        on = build_report_from_parts(
            days, miners, {"1": "continuous"}, efficiency=1.0, today=date(2026, 9, 21)
        )
        self.assertLess(off["raw_kw"], 0.2)
        self.assertGreater(on["raw_kw"], 1.0)
        self.assertLess(on["raw_kw"], 1.4)
        self.assertEqual(on["continuous_kw"], 2.0)

    def test_off_mode_warns_without_controlling(self):
        report = build_report_from_parts(
            [_day("2026-09-20", 20.0, 80.0)],
            [{"ip": "1", "name": "Beispiel", "power_kw": 1.6, "algo_enabled": True}],
            {"1": "off"},
            efficiency=0.95,
            today=date(2026, 9, 21),
            algo_enabled=True,
        )
        self.assertTrue(any("Beispiel" in a and "nicht ab" in a for a in report["alerts"]))
        self.assertEqual(report["continuous_kw"], 0.0)


class ArchiveTests(unittest.TestCase):
    def test_keeps_days_the_short_history_dropped(self):
        merged = merge_base_archive(
            [{"date": "2026-01-02", "base_kwh": 22.0}],
            [{"date": "2026-10-01", "base_kwh": 15.0}, {"date": "2026-10-02", "base_kwh": 16.0}],
        )
        self.assertEqual(merged[0]["date"], "2026-01-02")
        self.assertEqual(merged[-1]["base_kwh"], 16.0)

    def test_keeps_pv_when_a_later_row_only_updates_base(self):
        merged = merge_base_archive(
            [{"date": "2026-10-10", "base_kwh": 10.0, "pv_kwh": 80.0}],
            [{"date": "2026-10-10", "base_kwh": 12.0}],
        )
        self.assertEqual(merged[0]["base_kwh"], 12.0)
        self.assertEqual(merged[0]["pv_kwh"], 80.0)

    def test_year_chart_has_one_slot_per_day(self):
        story = describe_history(
            [
                {"date": "2026-10-01", "base_kwh": 10.0},
                {"date": "2026-10-10", "base_kwh": 30.0, "pv_kwh": 80.0},
            ],
            date(2026, 10, 15),
        )
        self.assertEqual(story["year_days"], 365)
        self.assertEqual(len(story["year"]), 365)
        self.assertEqual(story["year"][-1]["date"], "2026-10-14")
        self.assertEqual(story["filled_days"], 2)
        self.assertEqual(story["highlights"]["hungriest"]["base_kwh"], 30.0)
        self.assertEqual(story["highlights"]["quietest"]["base_kwh"], 10.0)
        self.assertEqual(story["sun"]["days"], 1)
        self.assertEqual(story["sun"]["cover"], 2.67)

    def test_report_lists_every_complete_day(self):
        days = [_day(f"2026-08-{d:02d}", 10.0 + d, None) for d in range(1, 21)]
        days.append(_day("2026-09-01", 12.0, None))
        report = build_report_from_parts(
            days, [], {}, efficiency=0.95, today=date(2026, 9, 2)
        )
        self.assertEqual(len(report["base_history"]), 21)
        self.assertEqual(report["base_history"][0]["date"], "2026-08-01")
        self.assertNotIn("pv_kwh", report["base_history"][0])


class LockTests(unittest.TestCase):
    def test_weekly_stays_inside_same_week(self):
        today = date(2026, 9, 29)
        first = apply_recommendation_locks(
            {}, 1.0, today, season_shift=False, fingerprint="a"
        )
        second = apply_recommendation_locks(
            first, 3.0, today, season_shift=False, fingerprint="a"
        )
        self.assertEqual(second["weekly_kw"], 1.0)
        self.assertEqual(second["daily_kw"], 1.0)
        self.assertFalse(second["weekly_updated"])

    def test_daily_moves_slightly_next_day_weekly_waits(self):
        today = date(2026, 9, 29)
        first = apply_recommendation_locks(
            {}, 2.0, today, season_shift=False, fingerprint="a"
        )
        nxt = apply_recommendation_locks(
            first, 3.0, date(2026, 9, 30), season_shift=False, fingerprint="a"
        )
        self.assertEqual(nxt["weekly_kw"], 2.0)
        self.assertGreater(nxt["daily_kw"], 2.0)
        self.assertLess(nxt["daily_kw"], 2.4)

    def test_assumption_change_jumps_immediately(self):
        today = date(2026, 9, 29)
        first = apply_recommendation_locks(
            {}, 0.0, today, season_shift=False, fingerprint="hourly"
        )
        jumped = apply_recommendation_locks(
            first, 2.5, today, season_shift=False, fingerprint="continuous"
        )
        self.assertEqual(jumped["weekly_kw"], 2.5)
        self.assertEqual(jumped["daily_kw"], 2.5)

    def test_zero_anchor_can_reach_new_raw_next_week(self):
        first = apply_recommendation_locks(
            {}, 0.0, date(2026, 9, 28), season_shift=False, fingerprint="a"
        )
        nxt = apply_recommendation_locks(
            first, 2.0, date(2026, 10, 5), season_shift=False, fingerprint="a"
        )
        self.assertEqual(nxt["weekly_kw"], 2.0)


if __name__ == "__main__":
    unittest.main()
