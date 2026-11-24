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
        # Am Boden: starke Morgen-PV deckt den Tag, der Nachtboden hält den
        # Abstand zum 35-Prozent-Boden mit der mittleren Nachtlast.
        self.assertEqual(at_0518["kw"], at_0618["kw"])
        self.assertEqual(at_0618["band_factor"], 1.0)
        self.assertEqual(at_0618["kw"], 0.70)
        self.assertGreater(at_0618["night_bridge_kw"], 0.0)
        self.assertEqual(at_0618["pv_credit_kwh"], at_0618["load_kwh"])
        self.assertFalse(at_0618["pull_down"])
        self.assertNotEqual(at_0618["kw"], 12.0)
        huge = forward_purchase_kw(
            hours_until_sun=1.0,
            **{**common, "daily_base_kwh": 80.0, "tomorrow_pv_kwh": 0.0, "day_after_pv_kwh": 0.0},
        )
        # Ohne Leistungsdeckel: volle benötigte Leistung (vorher auf 1,2 kW gekappt).
        self.assertEqual(huge["kw"], 3.35)
        self.assertIsNone(huge["cap_kw"])

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
        # Starke PV-Prognose deckt die Tagelast. Am Boden hebt der Nachtboden den
        # Zukauf. Schon etwas darüber reicht der Puffer für die kurze Restnacht.
        at_min = forward_purchase_kw(soc_percent=35, **common)
        halfway = forward_purchase_kw(soc_percent=40, **common)
        edge = forward_purchase_kw(soc_percent=45, **common)
        above = forward_purchase_kw(soc_percent=50, **common)
        self.assertGreater(at_min["kw"], 0.0)
        self.assertGreater(at_min["night_bridge_kw"], 0.0)
        self.assertEqual(halfway["kw"], 0.0)
        self.assertEqual(edge["kw"], 0.0)
        self.assertEqual(above["kw"], 0.0)
        self.assertEqual(at_min["plan_recovery_kw"], 0.0)
        sunny = forward_purchase_kw(soc_percent=40, hours_until_sun=10, daylight=True, **{
            key: value for key, value in common.items() if key not in {"hours_until_sun", "daylight"}
        })
        self.assertEqual(sunny["band_factor"], 0.5)
        self.assertEqual(sunny["kw"], 0.0)

    def test_day_plan_ramps_when_soc_slips_and_not_to_grid_max(self):
        """Unter dem SoC vom Tagesbeginn wird die Lücke bis zur Tageswende gekauft.

        10 Prozentpunkte unter dem Plan bei 40 kWh und 10 Stunden Rest sind 0,40 kW,
        nicht der Cap und nicht 12 kW. PV-Gewichte und die Glättung bleiben.
        Wieder auf dem Plan ist der Zusatz 0, und die Anzeige fährt schrittweise runter.
        """
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import (
            FORWARD_PV_DAY_AFTER_W,
            FORWARD_PV_TODAY_W,
            FORWARD_PV_TOMORROW_W,
            FORWARD_REFRESH,
            FORWARD_SMOOTH_FAST_REL,
            FORWARD_SMOOTH_REL,
            apply_forward_smooth,
            forward_purchase_kw,
            plan_recovery_kw,
            resolve_day_plan,
        )

        self.assertEqual(FORWARD_PV_TODAY_W, 1.0)
        self.assertEqual(FORWARD_PV_TOMORROW_W, 0.6)
        self.assertEqual(FORWARD_PV_DAY_AFTER_W, 0.4)
        self.assertEqual(FORWARD_SMOOTH_REL, 0.25)
        self.assertEqual(FORWARD_SMOOTH_FAST_REL, 0.70)
        self.assertEqual(int(FORWARD_REFRESH.total_seconds()), 60)
        self.assertAlmostEqual(plan_recovery_kw(40, 50, 40, 10), 0.4)
        self.assertEqual(plan_recovery_kw(50, 50, 40, 10), 0.0)
        self.assertEqual(plan_recovery_kw(55, 50, 40, 10), 0.0)

        common = dict(
            storage_kwh=40,
            min_soc_percent=35,
            daily_base_kwh=16.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=20.0,
            day_after_pv_kwh=20.0,
            forecast_trust=0.85,
            hours_until_sun=8,
            daylight=False,
            max_soc_percent=95,
        )
        on_plan = forward_purchase_kw(
            soc_percent=50, plan_soc_percent=50, hours_until_plan=10, **common
        )
        slipped = forward_purchase_kw(
            soc_percent=40, plan_soc_percent=50, hours_until_plan=10, **common
        )
        self.assertEqual(on_plan["kw"], 0.0)
        self.assertEqual(slipped["plan_recovery_kw"], 0.4)
        self.assertEqual(slipped["kw"], 0.4)
        self.assertIsNone(slipped["cap_kw"])
        self.assertNotEqual(slipped["kw"], 12.0)
        self.assertFalse(slipped["pull_down"])
        huge = forward_purchase_kw(
            soc_percent=30,
            plan_soc_percent=80,
            hours_until_plan=1,
            tomorrow_pv_kwh=0.0,
            day_after_pv_kwh=0.0,
            **{key: value for key, value in common.items() if key not in {"tomorrow_pv_kwh", "day_after_pv_kwh"}},
        )
        # Ohne Deckel: Plan-Recovery und Last fließen voll in den Zukauf.
        self.assertEqual(huge["plan_recovery_kw"], 20.0)
        self.assertEqual(huge["kw"], 20.65)
        self.assertIsNone(huge["cap_kw"])
        self.assertNotEqual(huge["kw"], 12.0)

        opened = datetime(2026, 11, 17, 12, 0, 0)
        shown = apply_forward_smooth({}, 0.0, 0.0, now=opened)
        glimpse = apply_forward_smooth(
            shown, slipped["p_raw"], 0.0, now=opened + timedelta(seconds=10)
        )
        self.assertEqual(glimpse["kw"], 0.0)
        step = apply_forward_smooth(
            shown, slipped["p_raw"], 0.0, now=opened + timedelta(minutes=1)
        )
        self.assertGreater(step["kw"], 0.0)
        self.assertLess(step["kw"], slipped["kw"])
        state = dict(step)
        for minute in range(2, 8):
            state = apply_forward_smooth(
                state,
                slipped["p_raw"],
                0.0,
                now=opened + timedelta(minutes=minute),
            )
        self.assertEqual(state["kw"], slipped["kw"])
        down = apply_forward_smooth(
            state, 0.0, 0.0, now=opened + timedelta(minutes=8)
        )
        self.assertGreater(down["kw"], 0.0)
        self.assertLess(down["kw"], state["kw"])

        noon = datetime(2026, 11, 17, 12, 0, 0)
        first, soc, hours = resolve_day_plan({}, noon.date(), noon, 62.0)
        self.assertEqual(first["soc_percent"], 62.0)
        self.assertEqual(soc, 62.0)
        self.assertAlmostEqual(hours, 12.0)
        again, soc_again, _hours_again = resolve_day_plan(
            {"day_plan": first}, noon.date(), noon.replace(hour=18), 40.0
        )
        self.assertEqual(again["soc_percent"], 62.0)
        self.assertEqual(soc_again, 62.0)

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

    def test_forward_doubled_48h_load_raises_setpoint(self):
        from grundverbrauch_rechner import blend_forward_load_kwh, forward_purchase_kw

        calm = blend_forward_load_kwh(17, 17, 17)
        surge = blend_forward_load_kwh(17, 17, 34)
        self.assertAlmostEqual(calm, 17.0)
        self.assertAlmostEqual(surge, 0.70 * 34 + 0.20 * 17 + 0.10 * 17)
        # Ohne PV-Gutschrift folgt der Soll der höheren Last ohne Leistungsdeckel.
        out = forward_purchase_kw(
            soc_percent=35,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=17,
            weekday_kwh=17,
            recent_48h_kwh=34,
            base_load_scale=1.0,
            tomorrow_pv_kwh=0.0,
            day_after_pv_kwh=0.0,
            forecast_trust=0.85,
            hours_until_sun=1.0,
            daylight=False,
            max_soc_percent=95,
        )
        self.assertGreater(out["kw"], 1.0)
        self.assertEqual(out["kw"], 1.2)
        self.assertIsNone(out["cap_kw"])
        self.assertNotEqual(out["kw"], 12.0)
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

    def test_forward_smooth_follows_a_stable_target_within_minutes(self):
        """Angezeigter Zukauf folgt dem frischen Soll in wenigen Minuten.

        Rauschen unter 0,05 kW bleibt liegen. Ein einzelner neuer Zielwert
        springt nicht sofort auf den vollen Wert. 0,65 kW bei frischem Soll
        1,5 kW, floored und unter dem Mindest-SoC, ist nach wenigen Minuten
        bei 1,5 kW und nicht erst nach stundenlangem Kriechen.
        """
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import apply_forward_smooth

        opened = datetime(2026, 11, 17, 15, 53, 0)
        first = apply_forward_smooth({}, 0.5, 0.0, now=opened)
        self.assertTrue(first["updated"])
        self.assertEqual(first["kw"], 0.5)
        glimpse = apply_forward_smooth(first, 1.0, 0.0, now=opened + timedelta(seconds=10))
        self.assertFalse(glimpse["updated"])
        self.assertEqual(glimpse["kw"], 0.5)
        self.assertEqual(glimpse["updated_at"], first["updated_at"])
        one_step = apply_forward_smooth(first, 1.0, 0.0, now=opened + timedelta(minutes=1))
        self.assertTrue(one_step["updated"])
        self.assertGreater(one_step["kw"], 0.5)
        self.assertLess(one_step["kw"], 1.0)
        self.assertGreater(one_step["kw"] - first["kw"], 0.10)
        noise = apply_forward_smooth(first, 0.53, 0.0, now=opened + timedelta(minutes=2))
        self.assertEqual(noise["kw"], 0.5)
        # Kein oberer Deckel mehr: 2,0 kW Soll bleibt 2,0 kW.
        uncapped = apply_forward_smooth({}, 2.0, 0.0, now=opened)
        self.assertEqual(uncapped["kw"], 2.0)

        behind = {
            "kw": 0.65,
            "raw_kw": 1.5,
            "smoothed_kw": 0.65,
            "floored": True,
            "updated_at": opened.isoformat(timespec="seconds"),
        }
        early = apply_forward_smooth(
            behind, 1.5, 0.0, now=opened + timedelta(seconds=10), emergency=True
        )
        self.assertGreater(early["kw"], 0.65)
        self.assertLess(early["kw"], 1.5)
        caught = apply_forward_smooth(
            behind, 1.5, 0.0, now=opened + timedelta(minutes=3), emergency=True
        )
        self.assertEqual(caught["kw"], 1.5)
        self.assertFalse(caught["floored"])
        state = dict(behind)
        reached_at = None
        for minute in range(1, 6):
            state = apply_forward_smooth(
                state,
                1.5,
                0.0,
                now=opened + timedelta(minutes=minute),
                emergency=True,
            )
            if state["kw"] == 1.5:
                reached_at = minute
                break
        self.assertIsNotNone(reached_at)
        self.assertLessEqual(reached_at, 5)
        self.assertEqual(state["kw"], 1.5)

    def test_today_rest_cuts_purchase_that_would_stay_near_1kw(self):
        """Hoher noch erwarteter Rest heute gegen niedriges Ist.

        Ohne today_pv_kwh bleibt der Tag-Zukauf bei 1,05 kW, weil die Mischung
        nur morgen/übermorgen kennt und die hier 0 sind. Der Rest (nicht die
        schon erzeugten kWh) drückt ihn auf 0, oberhalb des Pufferbands.
        Im Band bleibt ohne Tagesdefizit kein linearer Aufschlag. Nachts zählt der heutige Rest
        weiterhin nicht — wohl aber die Vorwärtsprognose morgen/übermorgen.
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
        self.assertEqual(high_band["kw"], 1.1)
        self.assertEqual(in_band["kw"], 0.0)
        self.assertGreater(in_band["band_factor"], 0.0)
        self.assertEqual(in_band["plan_recovery_kw"], 0.0)
        self.assertAlmostEqual(in_band["p_day"], in_band["extra_kw"], places=2)
        night = dict(common)
        night["daylight"] = False
        night["hours_until_sun"] = 2
        night_sun = forward_purchase_kw(soc_percent=46, today_pv_kwh=40.0, **night)
        night_dark = forward_purchase_kw(soc_percent=46, today_pv_kwh=0.0, **night)
        # Heutiger Rest zählt nachts nicht; morgen/übermorgen sind hier 0 → gleicher Soll.
        self.assertEqual(night_sun["kw"], night_dark["kw"])
        self.assertEqual(night_sun["pv_credit_kwh"], 0.0)
        self.assertFalse(night_sun["pull_down"])
        self.assertNotEqual(night_sun["kw"], 2.0)

    def test_night_forward_pv_forecast_cuts_purchase(self):
        """Nacht: starke Open-Meteo-Vorhersage senkt den Zukauf / zieht ihn auf 0."""
        from grundverbrauch_rechner import forward_purchase_kw

        weak = forward_purchase_kw(
            soc_percent=77,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=20.6,
            base_load_scale=1.0,
            tomorrow_pv_kwh=0.0,
            day_after_pv_kwh=0.0,
            forecast_trust=0.85,
            hours_until_sun=13.5,
            daylight=False,
            max_soc_percent=95,
        )
        strong = forward_purchase_kw(
            soc_percent=77,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=20.6,
            base_load_scale=1.0,
            tomorrow_pv_kwh=34.33,
            day_after_pv_kwh=20.0,
            forecast_trust=0.85,
            hours_until_sun=13.5,
            daylight=False,
            max_soc_percent=95,
        )
        self.assertGreater(weak["kw"], 0.0)
        self.assertEqual(strong["kw"], 0.0)
        self.assertEqual(strong["pv_credit_kwh"], strong["load_kwh"])
        self.assertTrue(strong["pull_down"])
        self.assertGreater(strong["pv_blend_kwh"], weak["pv_blend_kwh"])

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
        der Vergangenheit erhöhen den Rohwert sofort. Die Glättung übernimmt
        diesen Rohwert nicht in den ersten Sekunden, nach einer halben Stunde
        aber vollständig.
        """
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import (
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
            now=noon + timedelta(seconds=10),
            pull_down=bool(correct["pull_down"]),
        )
        self.assertEqual(held["kw"], 0.0)
        later = apply_forward_smooth(
            prev,
            correct["p_raw"],
            0.0,
            now=noon + timedelta(minutes=30),
            pull_down=bool(correct["pull_down"]),
        )
        self.assertGreater(later["raw_kw"], 0.0)
        self.assertAlmostEqual(later["raw_kw"], correct["p_raw"], places=3)
        self.assertEqual(later["kw"], correct["kw"])
        self.assertGreater(later["kw"], 0.10)
        self.assertNotEqual(later["kw"], 0.0)

    def test_clear_today_surplus_is_not_frozen_at_the_old_purchase(self):
        from datetime import datetime, timedelta

        from grundverbrauch_rechner import apply_forward_smooth

        now = datetime(2026, 11, 14, 8, 30, 0)
        prev = {
            "kw": 1.05,
            "raw_kw": 1.05,
            "updated_at": now.isoformat(timespec="seconds"),
        }
        fresh = apply_forward_smooth(prev, 0.0, 0.0, now=now + timedelta(seconds=10))
        self.assertEqual(fresh["kw"], 1.05)
        pulled = apply_forward_smooth(
            prev, 0.0, 0.0, now=now + timedelta(seconds=10), pull_down=True
        )
        self.assertEqual(pulled["kw"], 0.0)
        band = apply_forward_smooth(
            prev, 0.30, 0.0, now=now + timedelta(seconds=10), pull_down=True
        )
        self.assertEqual(band["kw"], 0.30)
        stepped = apply_forward_smooth(prev, 0.0, 0.0, now=now + timedelta(minutes=1))
        self.assertLess(stepped["kw"], 1.05 - 0.10)
        self.assertGreater(stepped["kw"], 0.0)
        caught = apply_forward_smooth(prev, 0.0, 0.0, now=now + timedelta(minutes=20))
        self.assertEqual(caught["kw"], 0.0)

    def test_zero_anchor_can_reach_new_raw_next_week(self):
        first = apply_recommendation_locks(
            {}, 0.0, date(2026, 9, 28), season_shift=False, fingerprint="a"
        )
        nxt = apply_recommendation_locks(
            first, 2.0, date(2026, 10, 5), season_shift=False, fingerprint="a"
        )
        self.assertEqual(nxt["weekly_kw"], 2.0)


class SafetyCorrectionTests(unittest.TestCase):
    """Sicherheitszuschlag: Takt, Teiler, kumulative Korrektur."""

    def test_normalize_interval_and_divisor(self):
        from grundverbrauch_rechner import (
            SAFETY_DIVISOR_DEFAULT,
            SAFETY_INTERVAL_DEFAULT_MINUTES,
            normalize_safety_divisor,
            normalize_safety_interval_minutes,
            safety_interval_minutes_from_input,
        )

        self.assertEqual(normalize_safety_interval_minutes(1), 1)
        self.assertEqual(normalize_safety_interval_minutes(7), 7)
        self.assertEqual(normalize_safety_interval_minutes(180), 180)
        self.assertEqual(
            normalize_safety_interval_minutes(0), SAFETY_INTERVAL_DEFAULT_MINUTES
        )
        self.assertEqual(safety_interval_minutes_from_input(2, "hours"), 120)
        self.assertEqual(safety_interval_minutes_from_input(2, "Stunden"), 120)
        self.assertEqual(safety_interval_minutes_from_input(90, "minutes"), 90)
        self.assertEqual(normalize_safety_divisor(24), 24.0)
        self.assertEqual(normalize_safety_divisor(8), 8.0)
        self.assertEqual(normalize_safety_divisor(0), SAFETY_DIVISOR_DEFAULT)
        self.assertEqual(normalize_safety_divisor(999), SAFETY_DIVISOR_DEFAULT)

    def test_saved_safety_interval_survives_reload(self):
        """Seitenwechsel lädt den Bericht neu und darf den Takt nicht auf 60 setzen."""
        import json
        import tempfile
        from pathlib import Path

        from grundverbrauch_rechner import _load_state, _save_state, _state_path

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "energy_monitor"
            folder.mkdir()
            path = _state_path(tmp)
            _save_state(
                path,
                {
                    "assumptions": {},
                    "recommendation": {"weekly_kw": 0.4},
                    "targets": {
                        "soc_hold_enabled": False,
                        "soc_hold_percent": 5.0,
                        "soc_hold_horizon_hours": 24,
                        "weather_adjust_enabled": True,
                        "safety_enabled": True,
                        "safety_interval_minutes": 9,
                        "safety_divisor": 12,
                    },
                },
            )
            loaded = _load_state(path)
            self.assertEqual(loaded["targets"]["safety_interval_minutes"], 9)
            self.assertEqual(loaded["targets"]["safety_divisor"], 12.0)
            # Wie get_report beim erneuten Öffnen: Stand laden und wieder schreiben.
            _save_state(path, loaded)
            again = _load_state(path)
            self.assertEqual(again["targets"]["safety_interval_minutes"], 9)
            self.assertEqual(again["targets"]["safety_divisor"], 12.0)
            # Ein Schreiber ohne die neuen Felder darf den gespeicherten Takt nicht löschen.
            stale = {
                "targets": {
                    "soc_hold_enabled": False,
                    "soc_hold_percent": 5.0,
                    "soc_hold_horizon_hours": 24,
                    "weather_adjust_enabled": True,
                }
            }
            _save_state(path, stale)
            kept = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["targets"]["safety_interval_minutes"], 9)
            self.assertEqual(kept["targets"]["safety_divisor"], 12.0)

    def test_safety_base_uses_pv_trust_and_band_relief(self):
        from grundverbrauch_rechner import FORWARD_BUFFER_BAND_PCT, HOURS, forward_purchase_kw

        out = forward_purchase_kw(
            soc_percent=80,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=24.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=8.9,
            day_after_pv_kwh=30.0,
            forecast_trust=0.85,
            hours_until_sun=6,
            daylight=False,
        )
        blend = 0.6 * 8.9 + 0.4 * 30.0
        headroom = (80 - 35) / 100.0 * 41
        usable = headroom - (FORWARD_BUFFER_BAND_PCT / 100.0 * 41)
        expected = round((blend * 0.85 + usable) / HOURS, 3)
        self.assertAlmostEqual(out["safety_base_kw"], expected, places=3)
        self.assertNotEqual(out["kw"], out["safety_base_kw"])
        with_house = forward_purchase_kw(
            soc_percent=80,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=24.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=8.9,
            day_after_pv_kwh=30.0,
            forecast_trust=0.85,
            hours_until_sun=6,
            daylight=False,
            house_kwh=blend * 0.85 + usable + 5.0,
        )
        self.assertEqual(with_house["safety_base_kw"], 0.0)
        self.assertEqual(with_house["kw"], out["kw"])

    def test_night_bridge_raises_purchase_before_floor_is_hit(self):
        from datetime import date

        from grundverbrauch_rechner import (
            NIGHT_SPAN_HOURS,
            forward_purchase_kw,
            night_bridge_kw,
            recent_night_base_kwh,
        )

        hourly = [0.2] * 7 + [0.6] * 11 + [0.9] * 6
        night = sum(hourly[h] for h in list(range(0, 7)) + list(range(18, 24)))
        days = [
            {"date": f"2026-11-{day:02d}", "base_hourly": hourly}
            for day in range(10, 17)
        ]
        self.assertAlmostEqual(recent_night_base_kwh(days, date(2026, 11, 17)), night, places=4)
        # SoC 40 %: noch über dem Boden, aber 5 Prozent = 2,05 kWh reichen für
        # die schwere Nacht nicht. Der Zukauf steigt schon vorher.
        common = dict(
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=18.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=40.0,
            day_after_pv_kwh=40.0,
            forecast_trust=0.85,
            hours_until_sun=10.0,
            daylight=False,
            night_kwh=night,
        )
        thin = forward_purchase_kw(soc_percent=40, **common)
        headroom = (40 - 35) / 100.0 * 41
        e_load = night * (10.0 / NIGHT_SPAN_HOURS)
        expected = (e_load - headroom) / 10.0
        self.assertAlmostEqual(thin["night_bridge_kw"], expected, places=3)
        self.assertGreater(thin["kw"], 0.0)
        self.assertAlmostEqual(thin["kw"], round(expected / 0.05) * 0.05, places=2)
        self.assertTrue(thin["short"])
        self.assertFalse(thin["pull_down"])
        # Ohne Nachtboden würde die starke PV den Zukauf auf 0 ziehen.
        self.assertEqual(thin["pv_credit_kwh"], thin["load_kwh"])
        self.assertGreater(thin["kw"], thin["p_day"])
        # Sicherheitsfeld greift früher (plus 5 Prozent Reserve) und liegt höher.
        safety_headroom = max(0.0, (40 - 35 - 5) / 100.0 * 41)
        safety_expected = (e_load - safety_headroom) / 10.0
        self.assertAlmostEqual(thin["safety_base_kw"], -safety_expected, places=3)
        self.assertLess(thin["safety_base_kw"], 0.0)
        self.assertGreater(-thin["safety_base_kw"], thin["night_bridge_kw"])
        self.assertGreater(-thin["safety_base_kw"], thin["kw"])
        fat = forward_purchase_kw(soc_percent=80, **common)
        self.assertEqual(fat["night_bridge_kw"], 0.0)
        self.assertEqual(fat["kw"], 0.0)
        self.assertGreater(fat["safety_base_kw"], 0.0)
        self.assertEqual(
            night_bridge_kw(
                load_kwh=18.0,
                night_kwh=night,
                hours_until_sun=10.0,
                headroom_kwh=headroom,
                daylight=True,
            ),
            0.0,
        )

    def test_safety_below_floor_buys_the_shortfall_despite_pv_surplus(self):
        from grundverbrauch_rechner import HOURS, forward_purchase_kw, safety_feed_kw

        tomorrow, after = 50.0, 25.775
        blend = 0.6 * tomorrow + 0.4 * after
        self.assertAlmostEqual(blend, 40.31, places=2)
        house = 16.44
        common = dict(
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=18.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=tomorrow,
            day_after_pv_kwh=after,
            forecast_trust=0.85,
            hours_until_sun=10,
            daylight=True,
            house_kwh=house,
        )
        shortfall_kwh = (35 - 28) / 100.0 * 41
        bezug_kw = round(shortfall_kwh / HOURS, 3)
        self.assertGreater(bezug_kw, 0.0)
        below = forward_purchase_kw(soc_percent=28, **common)
        # Gespeichert negativ, die Seite dreht das Vorzeichen zum positiven Bezug.
        self.assertAlmostEqual(below["safety_base_kw"], -bezug_kw, places=3)
        self.assertGreater(-below["safety_base_kw"], 0.0)
        surplus = round(max(0.0, blend * 0.85 - house) / HOURS, 3)
        self.assertGreater(surplus, 0.0)
        self.assertNotEqual(below["safety_base_kw"], surplus)
        self.assertEqual(
            safety_feed_kw(blend, 0.85, 0.0, house, shortfall_kwh),
            -bezug_kw,
        )
        on_floor = forward_purchase_kw(soc_percent=35, **common)
        self.assertAlmostEqual(on_floor["safety_base_kw"], surplus, places=3)
        self.assertGreater(on_floor["safety_base_kw"], 0.0)
        above = forward_purchase_kw(soc_percent=80, **common)
        headroom = (80 - 35) / 100.0 * 41
        usable = headroom - 0.10 * 41
        self.assertAlmostEqual(
            above["safety_base_kw"],
            round((blend * 0.85 + usable - house) / HOURS, 3),
            places=3,
        )
        self.assertGreater(above["safety_base_kw"], 0.0)
        self.assertNotEqual(below["kw"], -bezug_kw)

    def test_safety_house_is_rest_of_today_plus_all_of_tomorrow(self):
        from datetime import datetime

        from grundverbrauch_rechner import HOURS, safety_feed_kw, safety_horizon_house_kwh

        now = datetime(2026, 11, 18, 20, 35, 0)
        today = [0.0] * 24
        today[20] = 1.2
        today[21] = 1.0
        today[22] = 0.8
        today[23] = 0.6
        tomorrow = [0.5] * 24
        house = safety_horizon_house_kwh(today, tomorrow, now)
        rest_tonight = 1.2 * (25.0 / 60.0) + 1.0 + 0.8 + 0.6
        self.assertAlmostEqual(house, round(rest_tonight + 12.0, 4), places=4)
        credited = 28.619
        self.assertEqual(
            safety_feed_kw(0.0, 0.85, credited, house),
            round(max(0.0, credited - house) / HOURS, 3),
        )
        self.assertGreater(safety_feed_kw(0.0, 0.85, credited, house), 0.0)

    def test_rest_display_uses_the_same_formula_as_the_purchase(self):
        from grundverbrauch_rechner import HOURS, forward_purchase_kw

        tomorrow, after = 8.94, 30.97
        out = forward_purchase_kw(
            soc_percent=79,
            storage_kwh=41,
            min_soc_percent=35,
            daily_base_kwh=18.11,
            base_load_scale=1.0,
            tomorrow_pv_kwh=tomorrow,
            day_after_pv_kwh=after,
            forecast_trust=0.85,
            hours_until_sun=10,
            daylight=False,
        )
        blend = 0.6 * tomorrow + 0.4 * after
        headroom = (79 - 35) / 100.0 * 41
        usable = headroom - 0.10 * 41
        self.assertEqual(out["new_risk_kw"], out["kw"])
        self.assertEqual(out["new_risk_kw"], 0.0)
        self.assertAlmostEqual(out["safety_base_kw"], round((blend * 0.85 + usable) / HOURS, 3), places=3)
        self.assertGreater(out["safety_base_kw"], 0.0)

    def test_mehrbedarf_counts_only_in_daylight_and_only_on_safety_fields(self):
        from datetime import datetime

        from grundverbrauch_rechner import (
            apply_mehrbedarf_to_safety_fields,
            mehrbedarf_day_share_kw,
            mehrbedarf_window_hours,
        )

        now = datetime(2026, 11, 18, 20, 54, 0)
        end = datetime(2026, 11, 20, 0, 0, 0)
        hours = (end - now).total_seconds() / 3600.0
        self.assertAlmostEqual(mehrbedarf_window_hours(now), hours, places=6)
        self.assertAlmostEqual(hours, 27.1, places=6)
        self.assertEqual(mehrbedarf_day_share_kw(None, now), 0.0)
        self.assertEqual(mehrbedarf_day_share_kw(0, now), 0.0)
        share = 10.0 / hours
        self.assertAlmostEqual(mehrbedarf_day_share_kw(10, now), share, places=6)
        self.assertAlmostEqual(mehrbedarf_day_share_kw(100, now), 100.0 / hours, places=6)
        base, deviation, shown = apply_mehrbedarf_to_safety_fields(1.5, 0.25, 10, now=now)
        self.assertAlmostEqual(base, round(1.5 - share, 3), places=3)
        self.assertAlmostEqual(deviation, round(0.25 + share, 3), places=3)
        self.assertAlmostEqual(shown, round(share, 3), places=3)

    def test_deficit_over_divisor_and_cumulative(self):
        from datetime import datetime

        from grundverbrauch_rechner import apply_safety_correction, safety_step_kw

        self.assertAlmostEqual(safety_step_kw(1.0, 24), 1.0 / 24.0, places=6)
        self.assertAlmostEqual(safety_step_kw(1.0, 8), 0.125, places=6)

        now = datetime(2026, 11, 18, 14, 5, 0)
        start = datetime(2026, 11, 18, 13, 0, 0)
        end = datetime(2026, 11, 18, 14, 0, 0)
        # Historie 10 kW, Ist 11 kW → Defizit 1 kW → 1/24 Zuschlag.
        state, report = apply_safety_correction(
            {},
            enabled=True,
            interval_minutes=60,
            divisor=24,
            now=now,
            actual_kw=11.0,
            history_kw=10.0,
            window_start=start,
            window_end=end,
            recommended_kw=0.5,
        )
        self.assertTrue(report["applied"])
        self.assertAlmostEqual(report["deficit_kw"], 1.0, places=4)
        self.assertAlmostEqual(report["step_kw"], 1.0 / 24.0, places=6)
        self.assertAlmostEqual(report["surcharge_kw"], 1.0 / 24.0, places=4)
        self.assertAlmostEqual(report["safety_recommended_kw"], 0.5 + 1.0 / 24.0, places=3)

        # Zweites Fenster mit erneutem 10%-Defizit summiert denselben Zuschlag.
        state2, report2 = apply_safety_correction(
            state,
            enabled=True,
            interval_minutes=60,
            divisor=24,
            now=datetime(2026, 11, 18, 15, 5, 0),
            actual_kw=11.0,
            history_kw=10.0,
            window_start=datetime(2026, 11, 18, 14, 0, 0),
            window_end=datetime(2026, 11, 18, 15, 0, 0),
            recommended_kw=0.5,
        )
        self.assertTrue(report2["applied"])
        self.assertAlmostEqual(report2["surcharge_kw"], 2.0 / 24.0, places=4)
        self.assertAlmostEqual(
            report2["safety_recommended_kw"], 0.5 + 2.0 / 24.0, places=3
        )

        # Unterverbrauch senkt den Zuschlag wieder (10% weniger → −1/24).
        state3, report3 = apply_safety_correction(
            state2,
            enabled=True,
            interval_minutes=60,
            divisor=24,
            now=datetime(2026, 11, 18, 16, 5, 0),
            actual_kw=9.0,
            history_kw=10.0,
            window_start=datetime(2026, 11, 18, 15, 0, 0),
            window_end=datetime(2026, 11, 18, 16, 0, 0),
            recommended_kw=0.5,
        )
        self.assertAlmostEqual(report3["surcharge_kw"], 1.0 / 24.0, places=4)
        self.assertLess(report3["safety_recommended_kw"], 0.5 + 2.0 / 24.0)

    def test_same_window_is_not_applied_twice(self):
        from datetime import datetime

        from grundverbrauch_rechner import apply_safety_correction

        now = datetime(2026, 11, 18, 14, 5, 0)
        start = datetime(2026, 11, 18, 13, 0, 0)
        end = datetime(2026, 11, 18, 14, 0, 0)
        state, first = apply_safety_correction(
            {},
            enabled=True,
            interval_minutes=60,
            divisor=24,
            now=now,
            actual_kw=12.0,
            history_kw=10.0,
            window_start=start,
            window_end=end,
            recommended_kw=0.5,
        )
        _, second = apply_safety_correction(
            state,
            enabled=True,
            interval_minutes=60,
            divisor=24,
            now=now,
            actual_kw=12.0,
            history_kw=10.0,
            window_start=start,
            window_end=end,
            recommended_kw=0.5,
        )
        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertAlmostEqual(first["surcharge_kw"], second["surcharge_kw"], places=6)

    def test_safety_can_fall_below_normal_and_has_no_upper_cap(self):
        from datetime import datetime

        from grundverbrauch_rechner import apply_safety_correction

        now = datetime(2026, 11, 18, 14, 5, 0)
        # Starker Unterverbrauch: Zuschlag negativ → unter dem normalen Soll.
        _, low = apply_safety_correction(
            {"cumulative_kw": 0.0},
            enabled=True,
            interval_minutes=60,
            divisor=8,
            now=now,
            actual_kw=2.0,
            history_kw=10.0,
            window_start=datetime(2026, 11, 18, 13, 0, 0),
            window_end=datetime(2026, 11, 18, 14, 0, 0),
            recommended_kw=0.5,
        )
        self.assertLess(low["safety_recommended_kw"], 0.5)
        self.assertAlmostEqual(low["step_kw"], -1.0, places=6)

        # Starker Mehrverbrauch: kein Deckel bei 1,2/1,5.
        _, high = apply_safety_correction(
            {"cumulative_kw": 0.0},
            enabled=True,
            interval_minutes=60,
            divisor=1,
            now=now,
            actual_kw=40.0,
            history_kw=10.0,
            window_start=datetime(2026, 11, 18, 13, 0, 0),
            window_end=datetime(2026, 11, 18, 14, 0, 0),
            recommended_kw=0.5,
        )
        self.assertGreater(high["safety_recommended_kw"], 1.5)
        self.assertAlmostEqual(high["surcharge_kw"], 30.0, places=4)

    def test_last_closed_window_and_hourly_mean(self):
        from datetime import datetime

        from grundverbrauch_rechner import (
            last_closed_safety_window,
            mean_kw_from_hourly,
        )

        now = datetime(2026, 11, 18, 14, 30, 0)
        start, end = last_closed_safety_window(now, 60)
        self.assertEqual(start, datetime(2026, 11, 18, 13, 0, 0))
        self.assertEqual(end, datetime(2026, 11, 18, 14, 0, 0))

        start3, end3 = last_closed_safety_window(now, 180)
        self.assertEqual(start3, datetime(2026, 11, 18, 9, 0, 0))
        self.assertEqual(end3, datetime(2026, 11, 18, 12, 0, 0))

        hourly = [1.0] * 24
        hourly[13] = 11.0
        self.assertAlmostEqual(
            mean_kw_from_hourly(hourly, start, end), 11.0, places=4
        )


class PvEstimateModeTests(unittest.TestCase):
    """Umschalter PV-Schätzung vs. Musterschätzung für alle Vorwärtspfade."""

    def test_default_is_pv_and_aliases_normalize(self):
        from grundverbrauch_rechner import (
            PV_ESTIMATE_MODE_PATTERN,
            PV_ESTIMATE_MODE_PV,
            default_targets,
            normalize_pv_estimate_mode,
        )

        self.assertEqual(default_targets()["pv_estimate_mode"], PV_ESTIMATE_MODE_PV)
        self.assertEqual(normalize_pv_estimate_mode(None), PV_ESTIMATE_MODE_PV)
        self.assertEqual(normalize_pv_estimate_mode("pv"), PV_ESTIMATE_MODE_PV)
        self.assertEqual(normalize_pv_estimate_mode("pattern"), PV_ESTIMATE_MODE_PATTERN)
        self.assertEqual(normalize_pv_estimate_mode("Muster"), PV_ESTIMATE_MODE_PATTERN)
        self.assertEqual(
            normalize_pv_estimate_mode("Musterschätzung"), PV_ESTIMATE_MODE_PATTERN
        )

    def test_resolve_pattern_scales_all_forward_days(self):
        from datetime import datetime

        from grundverbrauch_rechner import resolve_forward_pv_inputs

        now = datetime(2026, 11, 20, 10, 0, 0)
        algo = {
            "pv_yield_today_kwh": 20.0,
            "pv_yield_tomorrow_kwh": 40.0,
            "pv_yield_day_after_kwh": 30.0,
            "pv_forecast_today_hours": {str(h): 20.0 / 24.0 for h in range(24)},
            "forecast_trust": 0.85,
            "live_pv_kwh_actual": 5.0,
            "pv_pattern_estimate": {
                "estimated_kwh": 10.0,
                "oms_forecast_kwh": 20.0,
            },
        }
        pv = resolve_forward_pv_inputs(algo, "pv", now=now)
        self.assertEqual(pv["mode"], "pv")
        self.assertEqual(pv["source"], "pv")
        self.assertEqual(pv["today_kwh"], 20.0)
        self.assertEqual(pv["tomorrow_kwh"], 40.0)
        self.assertEqual(pv["day_after_kwh"], 30.0)

        muster = resolve_forward_pv_inputs(algo, "pattern", now=now)
        self.assertEqual(muster["mode"], "pattern")
        self.assertEqual(muster["source"], "pattern")
        self.assertEqual(muster["today_kwh"], 10.0)
        self.assertAlmostEqual(muster["ratio"], 0.5, places=4)
        self.assertAlmostEqual(muster["tomorrow_kwh"], 20.0, places=3)
        self.assertAlmostEqual(muster["day_after_kwh"], 15.0, places=3)
        self.assertIsNotNone(muster["today_rest_kwh"])
        self.assertLess(muster["today_rest_kwh"], pv["today_rest_kwh"])

    def test_pattern_feeds_purchase_safety_night_and_rest(self):
        """Musterschätzung senkt PV-Gutschrift → Zukauf, Sicherheit, Nachtboden, Rest."""
        from datetime import datetime

        from grundverbrauch_rechner import forward_purchase_kw, resolve_forward_pv_inputs

        algo = {
            "pv_yield_today_kwh": 40.0,
            "pv_yield_tomorrow_kwh": 50.0,
            "pv_yield_day_after_kwh": 40.0,
            "pv_forecast_today_hours": {str(h): 40.0 / 24.0 for h in range(24)},
            "forecast_trust": 0.85,
            # Ist ≈ vergangene Prognose → kein Tages-Rückstand, der den Vergleich verzerrt.
            "live_pv_kwh_actual": 20.0,
            "pv_pattern_estimate": {"estimated_kwh": 8.0, "oms_forecast_kwh": 40.0},
        }
        now = datetime(2026, 11, 20, 12, 0, 0)
        pv_in = resolve_forward_pv_inputs(algo, "pv", now=now)
        muster_in = resolve_forward_pv_inputs(algo, "pattern", now=now)
        self.assertAlmostEqual(pv_in["today_shortfall_kwh"], 0.0, places=2)
        # Am Tag: kein Nachtboden, damit der Zukauf die PV-Quelle zeigt.
        day_common = dict(
            soc_percent=50.0,
            storage_kwh=41.0,
            min_soc_percent=35.0,
            daily_base_kwh=24.0,
            base_load_scale=1.0,
            forecast_trust=0.85,
            hours_until_sun=6.0,
            daylight=True,
            night_kwh=12.0,
            house_kwh=30.0,
            weekday_kwh=24.0,
            recent_48h_kwh=24.0,
        )
        pv_day = forward_purchase_kw(
            tomorrow_pv_kwh=pv_in["tomorrow_kwh"],
            day_after_pv_kwh=pv_in["day_after_kwh"],
            today_pv_kwh=pv_in["today_rest_kwh"],
            today_shortfall_kwh=pv_in["today_shortfall_kwh"],
            **day_common,
        )
        muster_day = forward_purchase_kw(
            tomorrow_pv_kwh=muster_in["tomorrow_kwh"],
            day_after_pv_kwh=muster_in["day_after_kwh"],
            today_pv_kwh=muster_in["today_rest_kwh"],
            today_shortfall_kwh=muster_in["today_shortfall_kwh"],
            **day_common,
        )
        self.assertGreater(muster_day["kw"], pv_day["kw"])
        self.assertGreater(muster_day["p_day"], pv_day["p_day"])
        self.assertEqual(muster_day["new_risk_kw"], muster_day["kw"])
        self.assertEqual(pv_day["new_risk_kw"], pv_day["kw"])
        self.assertLess(muster_day["pv_credit_kwh"], pv_day["pv_credit_kwh"])
        self.assertLessEqual(muster_day["safety_base_kw"], pv_day["safety_base_kw"])

        night_common = dict(day_common)
        night_common.update(daylight=False, hours_until_sun=10.0, soc_percent=38.0)
        pv_night = forward_purchase_kw(
            tomorrow_pv_kwh=pv_in["tomorrow_kwh"],
            day_after_pv_kwh=pv_in["day_after_kwh"],
            today_pv_kwh=pv_in["today_rest_kwh"],
            today_shortfall_kwh=0.0,
            **night_common,
        )
        muster_night = forward_purchase_kw(
            tomorrow_pv_kwh=muster_in["tomorrow_kwh"],
            day_after_pv_kwh=muster_in["day_after_kwh"],
            today_pv_kwh=muster_in["today_rest_kwh"],
            today_shortfall_kwh=0.0,
            **night_common,
        )
        self.assertGreater(pv_night["night_bridge_kw"], 0.0)
        self.assertGreater(muster_night["night_bridge_kw"], 0.0)
        self.assertEqual(pv_night["night_bridge_kw"], muster_night["night_bridge_kw"])
        # Nachtboden selbst ist PV-unabhängig; p_day folgt weiter der Quelle.
        # Der Rest liegt über dem Zukauf, weil der Sollwert 45 % über dem SoC 38 % liegt.
        self.assertGreater(muster_night["p_day"], pv_night["p_day"])
        from grundverbrauch_rechner import HOURS, REST_SOC_SETPOINT_DEFAULT

        gap_kw = (REST_SOC_SETPOINT_DEFAULT - 38.0) / 100.0 * 41.0 / HOURS
        self.assertAlmostEqual(
            pv_night["new_risk_kw"], round(pv_night["kw"] + gap_kw, 3), places=3
        )
        self.assertAlmostEqual(
            muster_night["new_risk_kw"], round(muster_night["kw"] + gap_kw, 3), places=3
        )
        self.assertGreater(pv_night["new_risk_kw"], pv_night["kw"])

    def test_save_targets_persists_and_toggles_both_ways(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from grundverbrauch_rechner import (
            PV_ESTIMATE_MODE_PATTERN,
            PV_ESTIMATE_MODE_PV,
            _load_state,
            _state_path,
            default_targets,
            save_targets,
        )

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "energy_monitor"
            folder.mkdir()
            fake_report = {
                "targets": default_targets(),
                "recommended_kw": 0.0,
                "recommended_parts": {},
            }

            def _fake_report(base_dir):
                state = _load_state(_state_path(base_dir))
                fake_report["targets"] = dict(state.get("targets") or default_targets())
                return dict(fake_report)

            with patch(
                "grundverbrauch_rechner.get_report", side_effect=_fake_report
            ):
                first = save_targets(tmp, {"pv_estimate_mode": "pattern"})
                self.assertEqual(
                    first["targets"]["pv_estimate_mode"], PV_ESTIMATE_MODE_PATTERN
                )
                loaded = _load_state(_state_path(tmp))
                self.assertEqual(
                    loaded["targets"]["pv_estimate_mode"], PV_ESTIMATE_MODE_PATTERN
                )

                back = save_targets(tmp, {"pv_estimate_mode": "pv"})
                self.assertEqual(
                    back["targets"]["pv_estimate_mode"], PV_ESTIMATE_MODE_PV
                )
                again = _load_state(_state_path(tmp))
                self.assertEqual(
                    again["targets"]["pv_estimate_mode"], PV_ESTIMATE_MODE_PV
                )

            # Ohne explizites Feld bleibt der Default PV.
            bare = _load_state(_state_path(tmp))
            bare["targets"].pop("pv_estimate_mode", None)
            from grundverbrauch_rechner import _save_state

            _save_state(_state_path(tmp), bare)
            restored = _load_state(_state_path(tmp))
            self.assertEqual(
                restored["targets"]["pv_estimate_mode"], PV_ESTIMATE_MODE_PV
            )

    def test_below_floor_daylight_zero_purchase_despite_soc(self):
        """Unter 35 % am Tag: hohe PV-Gutschrift → p_day 0; Boden nur in Sicherheit."""
        from grundverbrauch_rechner import forward_purchase_kw

        out = forward_purchase_kw(
            soc_percent=29.0,
            storage_kwh=41.0,
            min_soc_percent=35.0,
            daily_base_kwh=17.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=46.0,
            day_after_pv_kwh=16.0,
            today_pv_kwh=0.0,
            today_shortfall_kwh=0.0,
            forecast_trust=0.85,
            hours_until_sun=2.0,
            daylight=True,
            night_kwh=8.0,
            house_kwh=20.0,
            weekday_kwh=17.0,
            recent_48h_kwh=17.0,
        )
        self.assertEqual(out["kw"], 0.0)
        self.assertEqual(out["p_day"], 0.0)
        self.assertEqual(out["night_bridge_kw"], 0.0)
        self.assertLess(out["safety_base_kw"], 0.0)
        from grundverbrauch_rechner import HOURS

        wall = (45.0 - 29.0) / 100.0 * 41.0 / HOURS
        self.assertAlmostEqual(out["new_risk_kw"], round(wall, 3), places=3)
        self.assertGreater(out["new_risk_kw"], out["kw"])

    def test_rest_slider_payload_reaches_save_targets(self):
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        import app as dashboard

        captured = {}

        def fake_save(base_dir, updates):
            captured.update(updates)
            return {"targets": dict(updates)}

        client = dashboard.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        with patch("grundverbrauch_rechner.save_targets", fake_save):
            response = client.post(
                "/api/grundverbrauch/ziele",
                json={
                    "rest_soc_setpoint_percent": 62,
                    "rest_bv_risk_tomorrow_percent": 15,
                    "rest_bv_risk_day_after_percent": 30,
                    "rest_total_risk_percent": 140,
                    "purchase_min_soc_percent": 42,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["rest_soc_setpoint_percent"], 62)
        self.assertEqual(captured["rest_bv_risk_tomorrow_percent"], 15)
        self.assertEqual(captured["rest_bv_risk_day_after_percent"], 30)
        self.assertEqual(captured["rest_total_risk_percent"], 140)
        self.assertEqual(captured["purchase_min_soc_percent"], 42)

    def test_purchase_min_soc_is_separate_from_the_algorithm(self):
        from grundverbrauch_rechner import (
            forward_purchase_kw,
            hard_min_soc_percent,
            purchase_min_soc_for_calculation,
        )

        self.assertEqual(hard_min_soc_percent(40), 35.0)
        self.assertEqual(purchase_min_soc_for_calculation(None, 32), 32.0)
        self.assertEqual(purchase_min_soc_for_calculation(40, 32), 40.0)
        self.assertEqual(purchase_min_soc_for_calculation(25, 40), 25.0)
        common = dict(
            soc_percent=38.0,
            storage_kwh=41.0,
            daily_base_kwh=18.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=10.0,
            day_after_pv_kwh=10.0,
            forecast_trust=0.85,
            hours_until_sun=10.0,
            daylight=False,
            night_kwh=12.0,
            weekday_kwh=18.0,
            recent_48h_kwh=18.0,
        )
        low = forward_purchase_kw(min_soc_percent=35.0, **common)
        high = forward_purchase_kw(min_soc_percent=40.0, **common)
        self.assertEqual(low["min_soc_percent"], 35.0)
        self.assertEqual(high["min_soc_percent"], 40.0)
        self.assertGreater(low["headroom_kwh"], high["headroom_kwh"])
        self.assertEqual(high["headroom_kwh"], 0.0)

    def test_rest_sliders_scale_and_do_not_change_purchase(self):
        from grundverbrauch_rechner import HOURS, forward_purchase_kw, rest_of_recent_days_kw

        base_args = dict(
            soc_percent=60.0,
            storage_kwh=40.0,
            min_soc_percent=35.0,
            daily_base_kwh=24.0,
            base_load_scale=1.0,
            tomorrow_pv_kwh=4.0,
            day_after_pv_kwh=4.0,
            forecast_trust=0.85,
            hours_until_sun=6.0,
            daylight=True,
            weekday_kwh=24.0,
            recent_48h_kwh=24.0,
        )
        plain = forward_purchase_kw(**base_args)
        tuned = forward_purchase_kw(
            **base_args,
            soc_setpoint_percent=70.0,
            bv_risk_tomorrow_percent=50.0,
            bv_risk_day_after_percent=25.0,
            total_risk_percent=200.0,
        )
        self.assertEqual(tuned["kw"], plain["kw"])
        self.assertGreater(tuned["new_risk_kw"], plain["new_risk_kw"])
        scale = 2.0
        load = plain["load_kwh"]
        risk_kw = load * (0.6 * (0.50 * scale) + 0.4 * (0.25 * scale)) / HOURS
        set_kw = (70.0 - 60.0) / 100.0 * 40.0 * scale / HOURS
        self.assertAlmostEqual(
            tuned["new_risk_kw"],
            round(plain["kw"] + risk_kw + set_kw, 3),
            places=3,
        )
        muted = rest_of_recent_days_kw(
            forward_kw=plain["kw"],
            soc_percent=60.0,
            storage_kwh=40.0,
            min_soc_percent=35.0,
            load_kwh=load,
            soc_setpoint_percent=70.0,
            bv_risk_tomorrow_percent=50.0,
            bv_risk_day_after_percent=25.0,
            total_risk_percent=0.0,
        )
        self.assertEqual(muted, plain["kw"])

    def test_socmin_wall_holds_the_band_edge_exactly(self):
        from grundverbrauch_rechner import HOURS, rest_of_recent_days_kw

        storage = 40.0
        # SoC im Linearband 35–45. Zukauf 0 würde hineinrutschen.
        wall = (45.0 - 40.0) / 100.0 * storage / HOURS
        held = rest_of_recent_days_kw(
            forward_kw=0.0,
            soc_percent=40.0,
            storage_kwh=storage,
            min_soc_percent=35.0,
            load_kwh=24.0,
            soc_setpoint_percent=40.0,
            bv_risk_tomorrow_percent=0.0,
            bv_risk_day_after_percent=0.0,
            total_risk_percent=0.0,
        )
        self.assertAlmostEqual(held, round(wall, 3), places=3)
        # Schon über der Grenze: keine Wand, der Zukauf bleibt.
        above = rest_of_recent_days_kw(
            forward_kw=0.2,
            soc_percent=50.0,
            storage_kwh=storage,
            min_soc_percent=35.0,
            load_kwh=24.0,
            soc_setpoint_percent=45.0,
            total_risk_percent=100.0,
        )
        self.assertEqual(above, 0.2)
        # Unter Socmin hält die Wand an 45 %, nicht an 35 %.
        below = rest_of_recent_days_kw(
            forward_kw=0.0,
            soc_percent=29.0,
            storage_kwh=storage,
            min_soc_percent=35.0,
            load_kwh=10.0,
            soc_setpoint_percent=30.0,
            total_risk_percent=0.0,
        )
        self.assertAlmostEqual(below, round((45.0 - 29.0) / 100.0 * storage / HOURS, 3), places=3)
        self.assertGreater(below, (35.0 - 29.0) / 100.0 * storage / HOURS)


if __name__ == "__main__":
    unittest.main()
