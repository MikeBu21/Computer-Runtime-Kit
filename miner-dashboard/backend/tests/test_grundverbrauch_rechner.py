# -*- coding: utf-8 -*-
"""Grundverbrauchsrechner: Formel, ohne Miner-Verbrauch, Update-Sperren."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from grundverbrauch_rechner import (
    apply_recommendation_locks,
    build_report_from_parts,
    describe_history,
    grid_kwh_for_day,
    merge_base_archive,
    year_curve_slots,
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
                {"date": "2026-10-08", "base_kwh": 20.0, "pv_kwh": 5.0},
                {"date": "2026-10-10", "base_kwh": 30.0, "pv_kwh": 80.0},
            ],
            date(2026, 10, 15),
        )
        self.assertEqual(story["year"][0]["date"], "2026-10-01")
        self.assertEqual(story["year"][-1]["date"], "2026-10-14")
        self.assertEqual(story["year_days"], 14)
        self.assertEqual(len(story["year"]), 14)
        self.assertIsNone(story["year"][1]["base_kwh"])
        self.assertEqual(story["year"][0]["base_kwh"], 10.0)
        self.assertEqual(story["filled_days"], 3)
        self.assertEqual(story["highlights"]["hungriest"]["base_kwh"], 30.0)
        self.assertEqual(story["highlights"]["quietest"]["base_kwh"], 10.0)
        self.assertNotIn("compare", story["highlights"])
        self.assertEqual(story["sun"]["days"], 2)
        self.assertEqual(story["sun"]["covered_days"], 1)
        self.assertTrue(story["weeks"][-1]["label"].startswith("KW "))

    def test_year_curve_grows_through_yesterday_then_caps_at_365(self):
        today = date(2026, 10, 15)
        yesterday = date(2026, 10, 14)
        short = year_curve_slots(
            [
                {"date": "2026-10-10", "base_kwh": 11.0},
                {"date": "2026-10-12", "base_kwh": 13.0},
            ],
            today,
        )
        self.assertEqual(short[0]["date"], "2026-10-10")
        self.assertEqual(short[-1]["date"], "2026-10-14")
        self.assertEqual(len(short), 5)
        self.assertIsNone(short[1]["base_kwh"])
        self.assertEqual(short[-1]["base_kwh"], None)

        start = yesterday - timedelta(days=399)
        rows = [
            {"date": (start + timedelta(days=offset)).isoformat(), "base_kwh": 10.0}
            for offset in range(400)
        ]
        full = describe_history(rows, today)
        self.assertEqual(len(full["year"]), 365)
        self.assertEqual(full["year_days"], 365)
        self.assertEqual(full["year"][0]["date"], "2025-10-15")
        self.assertEqual(full["year"][-1]["date"], "2026-10-14")
        self.assertEqual(full["filled_days"], 365)
        self.assertEqual(len(full["weeks"]), 12)

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
    def test_weekly_follows_raw_inside_same_week(self):
        today = date(2026, 9, 29)
        first = apply_recommendation_locks(
            {}, 1.0, today, season_shift=False, fingerprint="a"
        )
        second = apply_recommendation_locks(
            first, 3.0, today, season_shift=True, fingerprint="a"
        )
        self.assertEqual(second["weekly_kw"], 3.0)
        self.assertEqual(second["daily_kw"], 1.0)
        self.assertTrue(second["weekly_updated"])

    def test_daily_moves_slightly_next_day_weekly_follows_raw(self):
        today = date(2026, 9, 29)
        first = apply_recommendation_locks(
            {}, 2.0, today, season_shift=False, fingerprint="a"
        )
        nxt = apply_recommendation_locks(
            first, 3.0, date(2026, 9, 30), season_shift=False, fingerprint="a"
        )
        self.assertEqual(nxt["weekly_kw"], 3.0)
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

    def test_daily_refreshes_after_ten_hours(self):
        from datetime import datetime, timedelta

        today = date(2026, 9, 29)
        first = apply_recommendation_locks(
            {}, 1.0, today, season_shift=False, fingerprint="a"
        )
        stale = dict(first)
        stale["daily_updated_at"] = (datetime.now() - timedelta(hours=11)).isoformat(timespec="seconds")
        nxt = apply_recommendation_locks(
            stale, 2.0, today, season_shift=False, fingerprint="a"
        )
        self.assertTrue(nxt["daily_updated"])
        self.assertGreater(nxt["daily_kw"], 1.0)
        self.assertLess(nxt["daily_kw"], 1.3)

    def test_purchase_setpoint_uses_night_floor(self):
        from datetime import datetime

        from grundverbrauch_rechner import hours_until_morning_sun, purchase_setpoint_kw

        out = purchase_setpoint_kw(
            soc_percent=42,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=16.8,
            tomorrow_pv_kwh=20.03,
            hours_until_sun=15,
        )
        self.assertEqual(out["kw"], 0.60)
        self.assertGreater(out["p_floor"], out["p_raw"])
        span = hours_until_morning_sun(
            datetime(2026, 11, 3, 16, 0),
            {},
            {"h07": 0.4, "h06": 0.0},
        )
        self.assertEqual(span, 15.0)

    def test_purchase_setpoint_clamps_large_floor(self):
        from grundverbrauch_rechner import purchase_setpoint_kw

        out = purchase_setpoint_kw(
            soc_percent=36,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=80,
            tomorrow_pv_kwh=0,
            hours_until_sun=1,
        )
        self.assertGreater(out["p_floor"], 2.0)
        self.assertEqual(out["kw"], 2.0)

    def test_month_open_zukauf_jumps_with_household_and_weather(self):
        from grundverbrauch_rechner import (
            month_open_zukauf_kw,
            month_shape_factor,
            weather_consumption_factor,
        )

        self.assertEqual(month_shape_factor([20.0, 20.0]), 1.0)
        self.assertAlmostEqual(month_shape_factor([20.0, 16.0]), 0.88, places=2)
        factor, sun = weather_consumption_factor([0.45])
        self.assertAlmostEqual(factor, 1.0, places=3)
        self.assertAlmostEqual(sun, 0.45, places=3)
        self.assertEqual(month_open_zukauf_kw([20.0, 16.0], [0.45]), 0.66)
        dark, _sun = weather_consumption_factor([0.0])
        self.assertAlmostEqual(dark, 1.35, places=3)
        self.assertEqual(month_open_zukauf_kw([20.0, 16.0], [0.0]), 0.89)

    def test_opening_days_ignore_today_and_mark_short_night(self):
        from datetime import date

        from grundverbrauch_rechner import month_open_for_report

        hourly = [0.0] * 24
        for hour in range(24):
            hourly[hour] = 1.0 if hour < 7 or hour >= 18 else 0.4
        days = [
            {"date": "2026-11-01", "base_hourly": hourly, "pv_hourly": None},
            {"date": "2026-11-02", "base_hourly": hourly, "pv_hourly": None},
            {"date": "2026-11-03", "base_hourly": hourly, "pv_hourly": None},
        ]
        out = month_open_for_report(
            days,
            date(2026, 11, 3),
            {"days": [{"sun_factor": 0.45}]},
            41.0,
            41.0,
            35.0,
        )
        self.assertEqual(out["days_used"], 2)
        self.assertTrue(out["short"])
        self.assertGreater(out["night_kwh"], out["headroom_kwh"])
        self.assertIsNotNone(out["kw"])

    def test_import_kw_is_the_displayed_zukauf_not_the_old_rest(self):
        import json
        import tempfile
        from pathlib import Path

        from grundverbrauch_rechner import recommended_import_kw

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "energy_monitor"
            folder.mkdir()
            (folder / "grundverbrauch_rechner.json").write_text(
                json.dumps(
                    {
                        "recommendation": {"daily_kw": 0.018, "weekly_kw": 0.018},
                        "recommended_kw": 0.83,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(recommended_import_kw(tmp), 0.83)
            (folder / "grundverbrauch_rechner.json").write_text(
                json.dumps({"recommendation": {"daily_kw": 0.018, "weekly_kw": 0.018}}),
                encoding="utf-8",
            )
            self.assertIsNone(recommended_import_kw(tmp))

    def test_forward_purchase_credits_pv_and_uses_hard_min_soc(self):
        from grundverbrauch_rechner import forward_purchase_kw, hard_min_soc_percent

        self.assertEqual(hard_min_soc_percent(32), 32.0)
        self.assertEqual(hard_min_soc_percent(40), 35.0)
        self.assertEqual(hard_min_soc_percent(25), 30.0)
        covered = forward_purchase_kw(
            soc_percent=77,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=16.7,
            base_load_scale=1.0,
            tomorrow_pv_kwh=30.0,
            day_after_pv_kwh=30.0,
            forecast_trust=0.85,
            hours_until_sun=14,
        )
        self.assertEqual(covered["kw"], 0.0)
        self.assertEqual(covered["pv_credit_kwh"], covered["load_kwh"])
        need = forward_purchase_kw(
            soc_percent=40,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=24.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=0.0,
            day_after_pv_kwh=0.0,
            forecast_trust=0.85,
            hours_until_sun=12,
        )
        self.assertGreater(need["kw"], 0.4)
        self.assertLessEqual(need["kw"], 2.0)

    def test_forward_night_plateau_stays_flat_before_sunrise(self):
        from grundverbrauch_rechner import forward_purchase_kw

        common = dict(
            soc_percent=35,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=17,
            base_load_scale=1.0,
            tomorrow_pv_kwh=30.0,
            day_after_pv_kwh=30.0,
            forecast_trust=0.85,
            daylight=False,
            max_soc_percent=95,
        )
        at_0518 = forward_purchase_kw(hours_until_sun=2.7, **common)
        at_0618 = forward_purchase_kw(hours_until_sun=1.0, **common)
        self.assertEqual(at_0518["kw"], at_0618["kw"])
        self.assertEqual(at_0618["band_factor"], 1.0)
        self.assertEqual(at_0618["kw"], 1.2)
        self.assertEqual(at_0618["pv_credit_kwh"], 0.0)
        self.assertLessEqual(at_0618["kw"], 1.2)
        self.assertNotEqual(at_0618["kw"], 2.0)
        huge = forward_purchase_kw(
            hours_until_sun=1.0,
            **{**common, "daily_base_kwh": 80.0, "tomorrow_pv_kwh": 0.0, "day_after_pv_kwh": 0.0},
        )
        self.assertEqual(huge["kw"], 1.2)

    def test_buffer_band_scales_linearly_and_fades_above_the_edge(self):
        from grundverbrauch_rechner import buffer_band_extra_kw, buffer_band_factor, forward_purchase_kw

        self.assertEqual(buffer_band_factor(35, 35), 1.0)
        self.assertAlmostEqual(buffer_band_factor(40, 35), 0.5)
        self.assertEqual(buffer_band_factor(45, 35), 0.0)
        self.assertEqual(buffer_band_factor(50, 35), 0.0)
        self.assertAlmostEqual(buffer_band_extra_kw(0.4, 40, 35), 0.2)
        common = dict(
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=10,
            base_load_scale=1.0,
            tomorrow_pv_kwh=30.0,
            day_after_pv_kwh=30.0,
            forecast_trust=0.85,
            hours_until_sun=1.0,
            daylight=False,
            max_soc_percent=95,
        )
        at_min = forward_purchase_kw(soc_percent=35, **common)
        halfway = forward_purchase_kw(soc_percent=40, **common)
        edge = forward_purchase_kw(soc_percent=45, **common)
        above = forward_purchase_kw(soc_percent=50, **common)
        self.assertEqual(at_min["kw"], 0.85)
        self.assertEqual(halfway["kw"], 0.65)
        self.assertEqual(edge["kw"], 0.40)
        self.assertEqual(above["kw"], edge["kw"])
        self.assertGreater(at_min["kw"], halfway["kw"])
        self.assertGreater(halfway["kw"], edge["kw"])
        sunny = forward_purchase_kw(soc_percent=40, hours_until_sun=10, daylight=True, **{
            key: value for key, value in common.items() if key not in {"hours_until_sun", "daylight"}
        })
        self.assertEqual(sunny["band_factor"], 0.5)
        self.assertEqual(sunny["kw"], 0.20)

    def test_forward_day_plateau_is_zero_near_max_soc(self):
        from grundverbrauch_rechner import forward_purchase_kw

        near_max = forward_purchase_kw(
            soc_percent=93,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=17,
            base_load_scale=1.0,
            tomorrow_pv_kwh=20.0,
            day_after_pv_kwh=18.0,
            forecast_trust=0.85,
            hours_until_sun=4,
            daylight=True,
            max_soc_percent=95,
        )
        self.assertEqual(near_max["kw"], 0.0)
        self.assertTrue(near_max["daylight"])
        blocked = forward_purchase_kw(
            soc_percent=50,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=30,
            base_load_scale=1.0,
            tomorrow_pv_kwh=20.0,
            day_after_pv_kwh=20.0,
            forecast_trust=0.5,
            hours_until_sun=10,
            daylight=True,
            max_soc_percent=95,
        )
        open_room = forward_purchase_kw(
            soc_percent=50,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=30,
            base_load_scale=1.0,
            tomorrow_pv_kwh=20.0,
            day_after_pv_kwh=20.0,
            forecast_trust=0.5,
            hours_until_sun=10,
            daylight=True,
            max_soc_percent=100,
        )
        self.assertEqual(blocked["kw"], 0.0)
        self.assertEqual(open_room["kw"], 0.75)

    def test_forward_doubled_48h_load_raises_setpoint_under_cap(self):
        from grundverbrauch_rechner import blend_forward_load_kwh, forward_purchase_kw

        calm = blend_forward_load_kwh(17, 17, 17)
        surge = blend_forward_load_kwh(17, 17, 34)
        self.assertAlmostEqual(calm, 17.0)
        self.assertAlmostEqual(surge, 0.70 * 34 + 0.20 * 17 + 0.10 * 17)
        out = forward_purchase_kw(
            soc_percent=35,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=17,
            weekday_kwh=17,
            recent_48h_kwh=34,
            base_load_scale=1.0,
            tomorrow_pv_kwh=30.0,
            day_after_pv_kwh=30.0,
            forecast_trust=0.85,
            hours_until_sun=1.0,
            daylight=False,
            max_soc_percent=95,
        )
        self.assertGreater(out["kw"], 1.0)
        self.assertLessEqual(out["kw"], 1.5)
        self.assertEqual(out["kw"], 1.5)
        self.assertEqual(out["cap_kw"], 1.5)
        self.assertEqual(out["band_factor"], 1.0)

    def test_recent_48h_extrapolates_to_a_day(self):
        from datetime import datetime

        from grundverbrauch_rechner import recent_48h_daily_kwh, same_weekday_base_kwh

        now = datetime(2026, 11, 12, 6, 18)
        today = now.date()
        days = []
        for day in ("2026-11-10", "2026-11-11"):
            days.append({"date": day, "base_hourly": [1.0] * 24})
        actual = [None] * 24
        for hour in range(7):
            actual[hour] = 1.0
        days.append({"date": "2026-11-12", "today_hourly_actual": actual, "base_hourly": [0.0] * 24})
        self.assertAlmostEqual(recent_48h_daily_kwh(days, today, now), 24.0)
        thursdays = [
            {"date": "2026-11-05", "base_hourly": [0.75] * 24},
            {"date": "2026-10-29", "base_hourly": [0.75] * 24},
            {"date": "2026-11-12", "base_hourly": [9.0] * 24},
        ]
        self.assertAlmostEqual(same_weekday_base_kwh(thursdays, today), 18.0)

    def test_forward_smooth_holds_two_hours_then_steps(self):
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import FORWARD_REFRESH, FORWARD_SMOOTH_ABS, apply_forward_smooth

        opened = datetime(2026, 11, 4, 10, 0, 0)
        first = apply_forward_smooth({}, 0.5, 0.0, now=opened)
        self.assertTrue(first["updated"])
        self.assertEqual(first["kw"], 0.5)
        held = apply_forward_smooth(first, 1.0, 0.0, now=opened + timedelta(seconds=10))
        self.assertFalse(held["updated"])
        self.assertEqual(held["kw"], 0.5)
        self.assertEqual(held["updated_at"], first["updated_at"])
        later = apply_forward_smooth(first, 1.0, 0.0, now=opened + FORWARD_REFRESH)
        self.assertTrue(later["updated"])
        self.assertEqual(later["kw"], 0.6)
        self.assertLessEqual(later["kw"] - first["kw"], FORWARD_SMOOTH_ABS)
        self.assertNotEqual(later["kw"], 1.0)
        emergency = apply_forward_smooth(
            first, 1.0, 0.0, now=opened + FORWARD_REFRESH, emergency=True
        )
        self.assertEqual(emergency["kw"], 0.65)
        capped = apply_forward_smooth({}, 2.0, 0.0, now=opened)
        self.assertEqual(capped["kw"], 1.5)

    def test_today_rest_cuts_purchase_that_would_stay_near_1kw(self):
        """Hoher noch erwarteter Rest heute gegen niedriges Ist.

        Ohne today_pv_kwh bleibt der Tag-Zukauf bei 1,05 kW, weil die Mischung
        nur morgen/übermorgen kennt und die hier 0 sind. Der Rest (nicht die
        schon erzeugten kWh) drückt ihn auf 0, oberhalb des Pufferbands.
        Im Band bleibt nur der lineare Aufschlag. Nachts zählt heute nicht.
        Ältere Tag-Tests lassen today_pv_kwh weg und behalten deshalb die
        reine Vorwärts-Mischung 0,6/0,4.
        """
        from grundverbrauch_rechner import forward_purchase_kw

        common = dict(
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=26.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=0.0,
            day_after_pv_kwh=0.0,
            forecast_trust=0.85,
            hours_until_sun=10,
            daylight=True,
            max_soc_percent=95,
        )
        without = forward_purchase_kw(soc_percent=46, today_pv_kwh=0.0, **common)
        # 26 kWh Last, SoC über dem Band, keine PV-Gutschrift → gerundet 1,05 kW.
        self.assertEqual(without["kw"], 1.05)
        self.assertEqual(without["pv_credit_kwh"], 0.0)
        self.assertFalse(without["pull_down"])
        # 40 kWh sind der Rest ab jetzt. Die wenigen schon erzeugten kWh
        # stecken nicht zusätzlich in today_pv_kwh.
        covered = forward_purchase_kw(soc_percent=46, today_pv_kwh=40.0, **common)
        self.assertEqual(covered["kw"], 0.0)
        self.assertEqual(covered["pv_credit_kwh"], covered["load_kwh"])
        self.assertTrue(covered["pull_down"])
        self.assertGreater(covered["pv_blend_kwh"], without["pv_blend_kwh"])
        in_band = forward_purchase_kw(soc_percent=40, today_pv_kwh=40.0, **common)
        high_band = forward_purchase_kw(soc_percent=40, today_pv_kwh=0.0, **common)
        self.assertEqual(high_band["kw"], 1.2)
        self.assertEqual(in_band["kw"], 0.55)
        self.assertGreater(in_band["band_factor"], 0.0)
        self.assertLess(in_band["kw"], 0.6)
        self.assertAlmostEqual(in_band["p_day"], in_band["extra_kw"], places=2)
        night = dict(common)
        night["daylight"] = False
        night["hours_until_sun"] = 2
        night_sun = forward_purchase_kw(soc_percent=46, today_pv_kwh=40.0, **night)
        night_dark = forward_purchase_kw(soc_percent=46, today_pv_kwh=0.0, **night)
        self.assertEqual(night_sun["kw"], night_dark["kw"])
        self.assertEqual(night_sun["pv_credit_kwh"], 0.0)
        self.assertFalse(night_sun["pull_down"])
        self.assertNotEqual(night_sun["kw"], 2.0)

    def test_remaining_today_does_not_count_produced_energy_twice(self):
        from datetime import datetime

        from grundverbrauch_rechner import (
            forecast_rest_today_kwh,
            spread_daily_kwh,
            today_pv_shortfall_kwh,
        )

        now = datetime(2026, 11, 14, 8, 30)
        hours = {"h07": 10.0, "h08": 4.0, "h09": 8.0, "h12": 6.0}
        # 07 Uhr ist vorbei. 08 Uhr nur zur Hälfte in der Zukunft. Das Ist
        # kürzt diese Zukunft nicht; der Rückstand ist Prognose minus Ist.
        self.assertAlmostEqual(
            forecast_rest_today_kwh(hours, now, day_total=43.0, actual_so_far=3.0),
            16.0,
        )
        self.assertAlmostEqual(today_pv_shortfall_kwh(hours, now, 3.0, 43.0), 9.0)
        self.assertAlmostEqual(today_pv_shortfall_kwh(hours, now, 20.0, 43.0), 0.0)
        self.assertAlmostEqual(
            forecast_rest_today_kwh(hours, now, day_total=43.0, actual_so_far=20.0),
            16.0,
        )

        def _spread_split(total: float) -> tuple:
            spread = spread_daily_kwh(total)
            elapsed = 0.5
            future = spread[8] * (1.0 - elapsed) + sum(spread[9:])
            past = sum(spread[:8]) + spread[8] * elapsed
            return future, past

        future, past = _spread_split(43.0)
        rest = forecast_rest_today_kwh(None, now, day_total=43.0, actual_so_far=3.0)
        self.assertAlmostEqual(rest, future, places=4)
        self.assertNotAlmostEqual(rest, 43.0 - 3.0, delta=0.05)
        self.assertAlmostEqual(
            today_pv_shortfall_kwh(None, now, 3.0, 43.0),
            max(0.0, past - 3.0),
            places=4,
        )
        future_empty, _past_empty = _spread_split(42.96)
        empty = forecast_rest_today_kwh({}, now, day_total=42.96, actual_so_far=2.86)
        self.assertAlmostEqual(empty, future_empty, places=4)
        self.assertNotAlmostEqual(empty, 42.96 - 2.86, delta=0.01)

    def test_noon_shortfall_raises_purchase_above_the_false_zero(self):
        """40 kWh Prognose, mittags nur 5 kWh. Der Zielwert bleibt über 0.

        40 minus das kleine Ist wären 35 kWh Gutschrift und decken die Last,
        der Zukauf fiele auf 0. Die Zukunft bleibt 20 kWh. Die fehlenden 15 kWh
        der Vergangenheit erhöhen den Rohwert sofort. Die Glättung startet bei
        Soll 0 und lässt den angezeigten Wert erst nach der Haltezeit steigen.
        """
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import (
            FORWARD_REFRESH,
            apply_forward_smooth,
            forecast_rest_today_kwh,
            forward_purchase_kw,
            today_pv_shortfall_kwh,
        )

        noon = datetime(2026, 11, 14, 12, 0, 0)
        hours = {f"h{hour:02d}": 5.0 for hour in range(8, 16)}
        future = forecast_rest_today_kwh(hours, noon, day_total=40.0, actual_so_far=5.0)
        shortfall = today_pv_shortfall_kwh(hours, noon, 5.0, 40.0)
        self.assertAlmostEqual(sum(hours.values()), 40.0)
        self.assertAlmostEqual(future, 20.0)
        self.assertAlmostEqual(shortfall, 15.0)
        self.assertNotAlmostEqual(future, 40.0 - 5.0)

        common = dict(
            soc_percent=45,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=16.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=0.0,
            day_after_pv_kwh=0.0,
            forecast_trust=0.85,
            hours_until_sun=6,
            daylight=True,
            max_soc_percent=95,
        )
        false_credit = forward_purchase_kw(
            today_pv_kwh=40.0 - 5.0,
            today_shortfall_kwh=0.0,
            **common,
        )
        self.assertEqual(false_credit["kw"], 0.0)
        self.assertEqual(false_credit["p_raw"], 0.0)
        cut_off = forward_purchase_kw(
            today_pv_kwh=future,
            today_shortfall_kwh=0.0,
            **common,
        )
        self.assertEqual(cut_off["kw"], 0.0)
        correct = forward_purchase_kw(
            today_pv_kwh=future,
            today_shortfall_kwh=shortfall,
            **common,
        )
        self.assertAlmostEqual(correct["p_raw"], 15.0 / 24.0, places=3)
        self.assertEqual(correct["kw"], 0.65)
        self.assertGreater(correct["kw"], 0.0)
        self.assertGreater(correct["p_raw"], 0.0)
        self.assertNotEqual(correct["kw"], false_credit["kw"])

        prev = {
            "kw": 0.0,
            "raw_kw": 0.0,
            "updated_at": noon.isoformat(timespec="seconds"),
        }
        held = apply_forward_smooth(
            prev,
            correct["p_raw"],
            0.0,
            now=noon + timedelta(minutes=30),
            pull_down=bool(correct["pull_down"]),
        )
        self.assertEqual(held["kw"], 0.0)
        later = apply_forward_smooth(
            prev,
            correct["p_raw"],
            0.0,
            now=noon + FORWARD_REFRESH,
            pull_down=bool(correct["pull_down"]),
        )
        self.assertGreater(later["raw_kw"], 0.0)
        self.assertAlmostEqual(later["raw_kw"], correct["p_raw"], places=3)
        self.assertGreater(later["kw"], 0.0)
        self.assertEqual(later["kw"], 0.10)
        self.assertNotEqual(later["kw"], 0.0)

    def test_clear_today_surplus_is_not_frozen_at_the_old_purchase(self):
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import FORWARD_REFRESH, apply_forward_smooth

        now = datetime(2026, 11, 14, 8, 30, 0)
        prev = {
            "kw": 1.05,
            "raw_kw": 1.05,
            "updated_at": now.isoformat(timespec="seconds"),
        }
        frozen = apply_forward_smooth(prev, 0.0, 0.0, now=now + timedelta(minutes=20))
        self.assertEqual(frozen["kw"], 1.05)
        pulled = apply_forward_smooth(
            prev, 0.0, 0.0, now=now + timedelta(minutes=20), pull_down=True
        )
        self.assertEqual(pulled["kw"], 0.0)
        band = apply_forward_smooth(
            prev, 0.30, 0.0, now=now + timedelta(minutes=20), pull_down=True
        )
        self.assertEqual(band["kw"], 0.30)
        stepped = apply_forward_smooth(prev, 0.0, 0.0, now=now + FORWARD_REFRESH)
        self.assertEqual(stepped["kw"], 0.95)
        self.assertNotEqual(stepped["kw"], 0.0)

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
