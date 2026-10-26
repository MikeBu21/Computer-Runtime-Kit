# -*- coding: utf-8 -*-
"""Tests Stromprofil / Soll-Kurve."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from control_algorithm.curve_plan import (
    build_stromprofil,
    build_steering_soc_goal_24h,
    compute_target_soc_curve,
)


def _week_hours():
    rows = []
    for h in range(24):
        rows.append(
            {
                "hour": h,
                "pv_kwh": 12.0 if 8 <= h <= 16 else 0.0,
                "base_kwh": 2.0,
                "miner_kwh_planned": 4.0 if 10 <= h <= 16 else 0.0,
                "soc_projected_pct": 40.0 + h,
            }
        )
    return rows


class TestBuildStromprofil(unittest.TestCase):
    def _state(self):
        return {
            "pv_forecast_today_hours": {
                f"h{h:02d}": (20.0 if 8 <= h <= 16 else 0.0) for h in range(24)
            },
            "energy_week_plan": {"days": [{"hours": _week_hours()}]},
            "live_soc": 55.0,
            "live_pv_kw": 8.0,
        }

    def _live(self):
        return {
            "soc_percent": 55.0,
            "pv_kw": 8.0,
            "wohnhaus_kw": 1.5,
            "verbrauch_kw": 5.0,
        }

    def _act_maps(self):
        pv = {f"h{h:02d}": (6.0 if h <= 12 else 0.0) for h in range(24)}
        cons = {"total_kwh": [(3.0 if h <= 12 else 0.0) for h in range(24)]}
        soc = [(50.0 if h <= 12 else None) for h in range(24)]
        return pv, cons, soc

    def test_always_24_hours_calc_and_ist_only_until_now(self):
        now = datetime(2026, 8, 17, 12, 30, 0)
        pv, cons, soc = self._act_maps()
        targets = {h: 60.0 + (h - 12) for h in range(12, 20)}
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([2.0] * 24, "test"),
        ), patch(
            "control_algorithm.slot_profiles.get_actual_pv_kwh_by_hours_today",
            return_value=pv,
        ), patch(
            "control_algorithm.consumption_history.get_hourly_consumption_breakdown_today",
            return_value=cons,
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=soc,
        ):
            out = build_stromprofil({}, self._state(), self._live(), now, targets)

        self.assertTrue(out["ok"])
        hours = out["hours"]
        self.assertEqual(len(hours), 24)
        self.assertEqual([r["hour"] for r in hours], list(range(24)))
        self.assertEqual(out["now_hour"], 12)

        for row in hours:
            self.assertIsInstance(row["pv_calc_kw"], float)
            self.assertIsInstance(row["cons_calc_kw"], float)
            self.assertGreaterEqual(row["cons_calc_kw"], 2.0)

        self.assertAlmostEqual(hours[12]["pv_calc_kw"], 12.0)
        self.assertAlmostEqual(hours[12]["cons_calc_kw"], 6.0)  # aktuelle Stunde: Plan
        self.assertAlmostEqual(hours[8]["cons_calc_kw"], 3.0)  # Vergangenheit: Ist-Last
        # Vergangenheit: SoC-Plan = Ist (keine fiktive Miner-Leer-Rechnung)
        self.assertIsNotNone(hours[12]["soc_calc_pct"])
        self.assertIsNotNone(hours[8]["soc_calc_pct"])
        self.assertAlmostEqual(hours[12]["soc_calc_pct"], hours[12]["soc_act_pct"], delta=0.2)
        self.assertAlmostEqual(hours[8]["soc_calc_pct"], hours[8]["soc_act_pct"], delta=0.2)

        for row in hours[:13]:
            self.assertIsNotNone(row["pv_act_kw"])
            self.assertIsNotNone(row["cons_act_kw"])
            self.assertIsNotNone(row["soc_act_pct"])
        for row in hours[13:]:
            self.assertIsNone(row["pv_act_kw"])
            self.assertIsNone(row["cons_act_kw"])
            self.assertIsNone(row["soc_act_pct"])

    def test_exception_still_returns_24_hours(self):
        now = datetime(2026, 8, 17, 9, 0, 0)
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            side_effect=RuntimeError("boom"),
        ):
            out = build_stromprofil({}, {}, {}, now)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["hours"]), 24)
        self.assertEqual(out["now_hour"], 9)
        self.assertTrue(out.get("reason"))
        for row in out["hours"]:
            self.assertIsNone(row["pv_act_kw"])
            self.assertIsNone(row["cons_act_kw"])
            self.assertIsNone(row["soc_act_pct"])

    def test_overnight_soc_falls_even_if_week_plan_flat_at_max(self):
        """Regression: Bat-Plan darf nachts nicht auf Max kleben wenn Last > 0."""
        now = datetime(2026, 9, 20, 17, 0, 0)
        flat_week = []
        for h in range(24):
            flat_week.append(
                {
                    "hour": h,
                    "pv_kwh": 8.0 if 8 <= h <= 16 else 0.0,
                    "base_kwh": 1.2,
                    "miner_kwh_planned": 0.0,
                    "soc_projected_pct": 89.0,
                }
            )
        state = {
            "energy_week_plan": {"days": [{"hours": flat_week}]},
            "live_soc": 91.0,
        }
        live = {"soc_percent": 91.0, "pv_kw": 0.2, "verbrauch_kw": 1.5}
        soc_act = [65.0, 63.0, 61.0, 59.0, 55.0, 42.0, 29.0] + [None] * 17
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([1.2] * 24, "test"),
        ), patch(
            "control_algorithm.slot_profiles.get_actual_pv_kwh_by_hours_today",
            return_value={},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_consumption_breakdown_today",
            return_value={"total_kwh": [1.0] * 24},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=soc_act,
        ):
            out = build_stromprofil(
                {"storage_kwh": 41.0, "max_soc_percent": 89.0},
                state,
                live,
                now,
            )
        hours = out["hours"]
        night = [hours[h]["soc_calc_pct"] for h in range(0, 7)]
        self.assertTrue(all(v is not None for v in night))
        self.assertGreater(night[0], night[6])
        self.assertLess(night[0], 85.0)
        # Vergangenheit folgt Ist — kein Sprung von 0 auf Live
        for h in range(0, 7):
            self.assertAlmostEqual(hours[h]["soc_calc_pct"], soc_act[h], delta=0.2)
        self.assertAlmostEqual(hours[17]["soc_calc_pct"], 91.0, delta=0.2)

    def test_past_soc_not_zeroed_by_planned_miners(self):
        """Geplante Miner dürfen die Vergangenheit nicht auf 0 % rechnen."""
        now = datetime(2026, 9, 22, 15, 0, 0)
        week = []
        for h in range(24):
            week.append(
                {
                    "hour": h,
                    "pv_kwh": 0.0,  # Plan-PV fehlt → früher: Leer-Rechnung
                    "base_kwh": 1.0,
                    "miner_kwh_planned": 4.0 if h < 15 else 1.0,
                    "soc_projected_pct": 89.0,
                }
            )
        soc_act = [67.0, 65.0, 63.0, 61.0, 60.0, 58.0, 56.0, 53.0, 47.0, 50.0, 56.0, 66.0, 61.0, 59.0, 79.0] + [None] * 9
        cons_act = [0.7] * 15 + [0.0] * 9
        pv_act = {f"h{h:02d}": (8.0 if 8 <= h <= 14 else 0.0) for h in range(24)}
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([1.0] * 24, "test"),
        ), patch(
            "control_algorithm.slot_profiles.get_actual_pv_kwh_by_hours_today",
            return_value=pv_act,
        ), patch(
            "control_algorithm.consumption_history.get_hourly_consumption_breakdown_today",
            return_value={"total_kwh": cons_act},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=soc_act,
        ):
            out = build_stromprofil(
                {"storage_kwh": 41.0, "max_soc_percent": 89.0},
                {"energy_week_plan": {"days": [{"hours": week}]}, "live_soc": 90.0},
                {"soc_percent": 90.0, "pv_kw": 6.0},
                now,
            )
        hours = out["hours"]
        past_calc = [hours[h]["soc_calc_pct"] for h in range(15)]
        self.assertTrue(all(v is not None and v >= 40.0 for v in past_calc))
        self.assertGreater(min(past_calc), 0.0)
        # Kein Teleport: letzte Vergangenheit nahe Live
        self.assertAlmostEqual(hours[14]["soc_calc_pct"], 79.0, delta=0.2)
        self.assertAlmostEqual(hours[15]["soc_calc_pct"], 90.0, delta=0.2)


class TestSteeringSocGoal(unittest.TestCase):
    def _cfg(self):
        return {
            "storage_kwh": 41.0,
            "min_soc_percent": 30.0,
            "max_soc_percent": 89.0,
            "morning_soc_percent": 45.0,
            "soc_peak_hour": 18,
        }

    def test_good_weather_peak_near_plan_max_evening_above_min(self):
        now = datetime(2026, 9, 23, 16, 0, 0)
        state = {
            "pv_yield_today_kwh": 80.0,
            "pv_yield_tomorrow_kwh": 70.0,
            "pv_forecast_today_hours": {
                f"h{h:02d}": (9.0 if 8 <= h <= 16 else 0.0) for h in range(24)
            },
            "overnight_reserve_plan": {
                "night_hold_floor_soc": 54.0,
                "evening_entry_floor_soc": 58.0,
                "morning_floor_soc": 33.0,
            },
            "live_soc": 88.0,
        }
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([1.0] * 24, "test"),
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=[65.0] + [None] * 23,
        ), patch(
            "control_algorithm.energy_balance._plant_perfect_day_kwh",
            return_value=90.0,
        ):
            g = build_steering_soc_goal_24h(
                self._cfg(), state, {"soc_percent": 88.0}, now
            )
        self.assertTrue(g["ok"])
        self.assertGreaterEqual(g["target_peak_pct"], 80.0)
        self.assertGreaterEqual(min(g["hours"][19:]), 30.0)
        self.assertGreaterEqual(min(g["hours"]), 30.0)

    def test_weak_pv_peak_below_plan_max(self):
        now = datetime(2026, 9, 23, 12, 0, 0)
        state = {
            "pv_yield_today_kwh": 20.0,
            "pv_yield_tomorrow_kwh": 15.0,
            "pv_forecast_today_hours": {
                f"h{h:02d}": (1.5 if 9 <= h <= 15 else 0.0) for h in range(24)
            },
            "overnight_reserve_plan": {
                "night_hold_floor_soc": 40.0,
                "evening_entry_floor_soc": 50.0,
                "morning_floor_soc": 33.0,
            },
            "live_soc": 55.0,
        }
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([1.2] * 24, "test"),
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=[50.0] + [None] * 23,
        ), patch(
            "control_algorithm.energy_balance._plant_perfect_day_kwh",
            return_value=90.0,
        ):
            g = build_steering_soc_goal_24h(
                self._cfg(), state, {"soc_percent": 55.0}, now
            )
        # Schwache PV senkt das Ziel nicht. reachable darf darunter liegen.
        self.assertAlmostEqual(g["target_peak_pct"], g["plan_max_pct"], delta=0.2)
        self.assertLess(g["reachable_pct"], g["plan_max_pct"])
        self.assertGreaterEqual(min(g["hours"]), 30.0)

    def test_evening_gentle_slope_no_cliff(self):
        """Nach Peak kein abrupter Absturz — Grundlast-Winkel bis Morgen-Floor."""
        now = datetime(2026, 9, 23, 17, 0, 0)
        state = {
            "pv_yield_today_kwh": 80.0,
            "pv_yield_tomorrow_kwh": 70.0,
            "pv_forecast_today_hours": {
                f"h{h:02d}": (9.0 if 8 <= h <= 16 else 0.0) for h in range(24)
            },
            "overnight_reserve_plan": {
                "night_hold_floor_soc": 54.0,
                "evening_entry_floor_soc": 58.0,
                "morning_floor_soc": 45.0,
            },
            "live_soc": 90.0,
        }
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([1.0] * 24, "test"),
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=[65.0] + [None] * 23,
        ), patch(
            "control_algorithm.energy_balance._plant_perfect_day_kwh",
            return_value=90.0,
        ):
            g = build_steering_soc_goal_24h(
                self._cfg(), state, {"soc_percent": 90.0}, now
            )
        eve = g["hours"][17:]
        for i in range(1, len(eve)):
            self.assertLessEqual(eve[i - 1] - eve[i], 4.0)  # kein Cliff
        self.assertGreaterEqual(min(eve), 45.0)
        self.assertGreater(eve[0], eve[-1])  # insgesamt fallend, aber sanft
        # Nicht flach bei Max kleben
        self.assertGreater(eve[0] - eve[-1], 6.0)


    def test_stromprofil_includes_soc_goal(self):
        now = datetime(2026, 9, 23, 14, 0, 0)
        state = {
            "energy_week_plan": {"days": [{"hours": _week_hours()}]},
            "pv_forecast_today_hours": {
                f"h{h:02d}": (8.0 if 8 <= h <= 16 else 0.0) for h in range(24)
            },
            "pv_yield_today_kwh": 60.0,
            "pv_yield_tomorrow_kwh": 55.0,
            "overnight_reserve_plan": {
                "night_hold_floor_soc": 40.0,
                "evening_entry_floor_soc": 45.0,
                "morning_floor_soc": 33.0,
            },
            "live_soc": 70.0,
        }
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([1.0] * 24, "test"),
        ), patch(
            "control_algorithm.slot_profiles.get_actual_pv_kwh_by_hours_today",
            return_value={},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_consumption_breakdown_today",
            return_value={"total_kwh": [1.0] * 24},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=[60.0] + [None] * 23,
        ), patch(
            "control_algorithm.energy_balance._plant_perfect_day_kwh",
            return_value=80.0,
        ):
            out = build_stromprofil(self._cfg(), state, {"soc_percent": 70.0}, now)
        self.assertTrue(out["ok"])
        goals = [h.get("soc_goal_pct") for h in out["hours"]]
        self.assertTrue(all(v is not None for v in goals))
        self.assertGreaterEqual(min(goals), 30.0)
        self.assertIn("steering_goal", out)


class TestComputeTargetSocCurveStromprofil(unittest.TestCase):
    def test_curve_plan_embeds_24h_stromprofil(self):
        now = datetime(2026, 8, 17, 14, 0, 0)
        config = {
            "storage_kwh": 41.0,
            "min_soc_percent": 46.0,
            "max_soc_percent": 86.0,
            "morning_soc_percent": 50.0,
            "soc_peak_hour": 19,
        }
        state = {
            "pv_forecast_today_hours": {f"h{h:02d}": 5.0 for h in range(24)},
            "energy_week_plan": {"days": [{"hours": _week_hours()}]},
            "fill_schedule": {"charge_kw": 2.0},
        }
        live = {"soc_percent": 64.0, "pv_kw": 10.0, "battery_power_kw": 2.0}
        with patch(
            "control_algorithm.curve_plan._hourly_base_profile",
            return_value=([2.0] * 24, "test"),
        ), patch(
            "control_algorithm.slot_profiles.get_actual_pv_kwh_by_hours_today",
            return_value={},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_consumption_breakdown_today",
            return_value={"total_kwh": [0.0] * 24, "base_kwh": [0.0] * 24},
        ), patch(
            "control_algorithm.consumption_history.get_hourly_actual_soc_today",
            return_value=[None] * 24,
        ):
            out = compute_target_soc_curve(config, state, live, now=now)
        self.assertTrue(out["ok"])
        sp = out["stromprofil"]
        self.assertEqual(len(sp["hours"]), 24)
        self.assertEqual(sp["now_hour"], 14)


class TestTargetPeakHour(unittest.TestCase):
    def test_follows_production_end_not_fixed_18(self):
        from datetime import datetime

        from control_algorithm.curve_plan import _target_peak_hour

        now = datetime(2026, 12, 15, 10, 0)
        winter = {"pv_forecast_by_date": {"2026-12-15": {"last_pv_hour": 16}}}
        summer_now = datetime(2026, 6, 15, 10, 0)
        summer = {"pv_forecast_by_date": {"2026-06-15": {"last_pv_hour": 21}}}
        self.assertEqual(_target_peak_hour({}, winter, now), 16)
        self.assertEqual(_target_peak_hour({}, summer, summer_now), 21)


if __name__ == "__main__":
    unittest.main()
