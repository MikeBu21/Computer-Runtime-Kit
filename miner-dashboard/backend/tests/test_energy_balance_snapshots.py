# -*- coding: utf-8 -*-
"""Tests für Multi-Day-Energy-Snapshots und Hydro-Tageswert."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta

from control_algorithm.consumption_history import (
    empty_history,
    expected_base_full_day_kwh,
    expected_hydro_full_day_kwh,
    save_history,
    update_weekday_profiles,
    weekday_base_consumption_detail,
)
from control_algorithm.energy_balance import (
    build_consumption_estimate_summary,
    build_multi_day_energy_snapshots,
    build_weekly_energy_plan,
    cap_planned_miner_to_day_surplus,
    shape_hourly_miner_budgets,
)
from control_algorithm.oms_pv_forecast import _parse_estimate


class _FakeEstimate:
    def __init__(self, wh_period, now_dt):
        self._wh_period = wh_period
        self._now = now_dt
        self.watts = {}

    def now(self):
        return self._now

    @property
    def wh_period(self):
        return self._wh_period


class TestOmsDayAfter(unittest.TestCase):
    def test_parse_estimate_includes_day_after(self):
        today = date(2026, 6, 15)
        tz_now = datetime(2026, 6, 15, 10, 0, 0)
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)
        wh = {
            datetime(today.year, today.month, today.day, 12, 0): 100_000,
            datetime(tomorrow.year, tomorrow.month, tomorrow.day, 12, 0): 200_000,
            datetime(day_after.year, day_after.month, day_after.day, 12, 0): 300_000,
        }
        est = _FakeEstimate(wh, tz_now)
        parsed = _parse_estimate(est, 1.0, {})
        today_adj, tomorrow_adj, day_after_adj = parsed[0], parsed[1], parsed[2]
        extra = parsed[-1] if isinstance(parsed[-1], dict) else {}
        by_date = parsed[-2] if len(parsed) >= 14 and isinstance(parsed[-2], dict) else extra
        self.assertTrue(
            extra.get("day_after_hours_raw")
            or any("2026-06-17" in str(k) for k in by_date)
        )
        self.assertAlmostEqual(today_adj, 100.0)
        self.assertAlmostEqual(tomorrow_adj, 200.0)
        self.assertAlmostEqual(day_after_adj, 300.0)
        self.assertIn("day_after_hours_raw", extra)


class TestHydroFullDay(unittest.TestCase):
    def test_expected_hydro_full_day_sums_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [1.0] * 24,
                    "hourly_hydro_kwh": [2.5] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                }
            ]
            update_weekday_profiles(hist)
            save_history(tmp, hist)
            total = expected_hydro_full_day_kwh(tmp, date.today())
            self.assertAlmostEqual(total, 60.0, places=1)


class TestMultiDaySnapshots(unittest.TestCase):
    def _seed_history(self, tmp: str) -> None:
        hist = empty_history()
        hist["days"] = [
            {
                "date": (date.today() - timedelta(days=1)).isoformat(),
                "weekday": date.today().weekday(),
                "hourly_base_kwh": [5.0] * 24,
                "hourly_hydro_kwh": [1.0] * 24,
                "hourly_verbrauch_pct": [1 / 24] * 24,
            }
        ]
        update_weekday_profiles(hist)
        save_history(tmp, hist)

    def test_three_snapshots_with_daily_pv(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_history(tmp)
            now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
            config = {
                "storage_kwh": 1000.0,
                "min_soc_percent": 35.0,
                "risk_factor_percent": 100.0,
                "soc_targets": [{"time": "18:30", "soc_percent": 50.0}],
                "devices": [
                    {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.0, "priority": "low"},
                ],
            }
            state = {
                "pv_yield_today_kwh": 984.0,
                "pv_yield_tomorrow_kwh": 900.0,
                "pv_yield_day_after_kwh": 850.0,
                "pv_forecast_today_hours": {f"h{h:02d}": 40.0 for h in range(5, 23)},
                "pv_forecast_tomorrow_hours": {f"h{h:02d}": 35.0 for h in range(5, 23)},
                "pv_forecast_day_after_hours": {f"h{h:02d}": 30.0 for h in range(5, 23)},
            }
            live = {"soc_percent": 70.0, "miner_status": {}}
            snaps = build_multi_day_energy_snapshots(config, state, live, tmp, now)
            self.assertGreaterEqual(len(snaps), 3)
            snaps = snaps[:3]
            self.assertAlmostEqual(snaps[0]["production_daily"]["pv_kwh"], 984.0)
            self.assertAlmostEqual(snaps[1]["production_daily"]["pv_kwh"], 900.0)
            self.assertAlmostEqual(snaps[2]["production_daily"]["pv_kwh"], 850.0)
            self.assertFalse(snaps[0]["is_forecast"])
            self.assertTrue(snaps[1]["is_forecast"])
            self.assertTrue(snaps[2]["is_forecast"])
            self.assertIn("consumption_daily", snaps[0])
            self.assertIn("day_balance", snaps[0])
            self.assertIn("control_plan", snaps[0])
            self.assertGreater(snaps[0]["consumption_daily"]["miner_all_day_kwh"], 0)
            self.assertTrue(snaps[1]["control_plan"])
            self.assertTrue(snaps[2]["control_plan"])

    def test_consumption_estimate_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_history(tmp)
            now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
            config = {
                "storage_kwh": 1000.0,
                "min_soc_percent": 35.0,
                "risk_factor_percent": 100.0,
                "devices": [
                    {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.0, "priority": "low"},
                ],
            }
            state = {
                "pv_yield_today_kwh": 500.0,
                "pv_yield_tomorrow_kwh": 500.0,
                "pv_yield_day_after_kwh": 500.0,
                "pv_forecast_today_hours": {f"h{h:02d}": 20.0 for h in range(5, 23)},
                "pv_forecast_tomorrow_hours": {f"h{h:02d}": 20.0 for h in range(5, 23)},
                "pv_forecast_day_after_hours": {f"h{h:02d}": 20.0 for h in range(5, 23)},
            }
            live = {"soc_percent": 70.0, "miner_status": {}}
            snaps = build_multi_day_energy_snapshots(config, state, live, tmp, now)
            # Ohne Wochenplan: kein fiktiver 24h-Volllast-Miner
            summary = build_consumption_estimate_summary(snaps, tmp, now)
            self.assertIn("total_with_miners", summary)
            self.assertIn("base_only", summary)
            self.assertIn("pattern", summary)
            self.assertIn("grundverbrauch", summary)
            self.assertIn("today_kwh", summary["grundverbrauch"])
            total_today = summary["total_with_miners"]["today_kwh"]
            base_today = summary["base_only"]["today_kwh"]
            self.assertIsNotNone(total_today)
            self.assertIsNotNone(base_today)
            self.assertAlmostEqual(total_today, base_today, delta=0.05)
            self.assertIsNotNone(summary["total_with_miners"]["tomorrow_kwh"])
            self.assertIsNotNone(summary["base_only"]["day_after_kwh"])
            self.assertEqual(summary["grundverbrauch"]["today_kwh"], base_today)

    def test_weak_pv_day_consumption_not_inflated_by_fleet_kw(self):
        """Regression: schlechter PV-Tag darf nicht ~120 kWh Verbrauch zeigen."""
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_history(tmp)
            now = datetime.now().replace(hour=16, minute=0, second=0, microsecond=0)
            config = {
                "storage_kwh": 55.0,
                "min_soc_percent": 28.0,
                "max_soc_percent": 95.0,
                "risk_factor_percent": 100.0,
                "devices": [
                    {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.8, "priority": "high"},
                    {"ip": "10.0.0.2", "enabled": True, "power_kw": 1.2, "priority": "medium"},
                    {"ip": "10.0.0.3", "enabled": True, "power_kw": 3.0, "priority": "low"},
                ],
            }
            # ~36 kWh PV, Grundlast-Profil aus Seed (~120 kWh/Tag bei 5 kW×24) —
            # hier gezielt schwaches PV-Profil setzen
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [0.6] * 24,  # ~14.4 kWh Grundlast
                    "hourly_hydro_kwh": [0.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                }
            ]
            update_weekday_profiles(hist)
            save_history(tmp, hist)

            pv_hours = {f"h{h:02d}": 0.0 for h in range(24)}
            # Schwacher Tag: Summe ~36 kWh, mittags peakt
            for h, v in [
                (8, 1.5), (9, 3.5), (10, 4.5), (11, 6.0), (12, 6.0),
                (13, 4.5), (14, 3.5), (15, 2.5), (16, 2.0), (17, 1.5), (18, 0.5),
            ]:
                pv_hours[f"h{h:02d}"] = v
            state = {
                "pv_yield_today_kwh": 36.0,
                "pv_yield_tomorrow_kwh": 62.0,
                "pv_yield_day_after_kwh": 59.0,
                "pv_forecast_today_hours": pv_hours,
                "pv_forecast_tomorrow_hours": {f"h{h:02d}": 3.0 for h in range(7, 19)},
                "pv_forecast_day_after_hours": {f"h{h:02d}": 3.0 for h in range(7, 19)},
                "horizon_projection": {
                    "miner_budget_kw": 10.0,  # volle Flotte – früher → 100+ kWh Lüge
                    "miner_surplus_kwh": 1680.0,
                    "binding_constraint": "export_avoidance",
                },
            }
            live = {"soc_percent": 95.0, "pv_kw": 1.4, "miner_status": {}}
            snaps = build_multi_day_energy_snapshots(config, state, live, tmp, now)
            week = build_weekly_energy_plan(config, state, live, tmp, now)
            summary = build_consumption_estimate_summary(
                snaps, tmp, now, week_plan=week, config=config
            )
            base = float(summary["base_only"]["today_kwh"])
            total = float(summary["total_with_miners"]["today_kwh"])
            miner = total - base
            day0 = (week.get("days") or [{}])[0]
            pv = float((day0.get("daily_totals") or {}).get("pv_kwh") or 36.0)
            # Invariante: Verbrauch mit Minern ≤ Grundlast + PV-Überschuss
            self.assertLessEqual(miner, max(0.0, pv - base) + 0.6)
            self.assertLess(total, 55.0)  # nie wieder ~120 kWh an einem 36-kWh-Tag
            self.assertLessEqual(total, base + pv + 0.6)

    def test_shape_hourly_never_exceeds_hourly_net(self):
        pv = {f"h{h:02d}": 0.0 for h in range(24)}
        for h in range(8, 16):
            pv[f"h{h:02d}"] = 2.0
        base = [0.5] * 24
        hydro = [0.0] * 24
        shaped = shape_hourly_miner_budgets(
            budget_kw=10.0,
            pv_hours=pv,
            hourly_base=base,
            hourly_hydro=hydro,
            total_device_kw=10.0,
            steering_mode="normal",
        )
        for h in range(24):
            net = max(0.0, float(pv[f"h{h:02d}"]) + hydro[h] - base[h])
            self.assertLessEqual(shaped[h], net + 1e-6)
        # Keine Phantom-Schulter (5–7 / 16–22 ohne Überschuss)
        self.assertEqual(shaped[5], 0.0)
        self.assertEqual(shaped[21], 0.0)
        self.assertLessEqual(sum(shaped), sum(
            max(0.0, float(pv[f"h{h:02d}"]) - base[h]) for h in range(24)
        ) + 0.01)

    def test_cap_planned_miner_to_day_surplus(self):
        self.assertAlmostEqual(
            cap_planned_miner_to_day_surplus(106.0, pv_kwh=36.0, base_kwh=14.0),
            22.0,
            places=1,
        )
        self.assertAlmostEqual(
            cap_planned_miner_to_day_surplus(5.0, pv_kwh=36.0, base_kwh=14.0),
            5.0,
            places=1,
        )

    def test_soc_chain_stays_within_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_history(tmp)
            now = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
            config = {
                "storage_kwh": 500.0,
                "min_soc_percent": 35.0,
                "risk_factor_percent": 100.0,
                "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            }
            state = {
                "pv_yield_today_kwh": 500.0,
                "pv_yield_tomorrow_kwh": 500.0,
                "pv_yield_day_after_kwh": 500.0,
                "pv_forecast_today_hours": {f"h{h:02d}": 20.0 for h in range(5, 23)},
                "pv_forecast_tomorrow_hours": {f"h{h:02d}": 20.0 for h in range(5, 23)},
                "pv_forecast_day_after_hours": {f"h{h:02d}": 20.0 for h in range(5, 23)},
            }
            live = {"soc_percent": 80.0, "miner_status": {}}
            snaps = build_multi_day_energy_snapshots(config, state, live, tmp, now)
            for snap in snaps[1:]:
                soc = snap["storage"]["soc_percent"]
                self.assertIsNotNone(soc)
                self.assertLessEqual(soc, 100.0)
                self.assertGreaterEqual(soc, 0.0)


class TestWeekdayBaseDetail(unittest.TestCase):
    def test_detail_lists_matching_weekdays(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = empty_history()
            wd = date.today().weekday()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=7)).isoformat(),
                    "weekday": wd,
                    "hourly_base_kwh": [10.0] * 24,
                    "hourly_hydro_kwh": [1.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                },
                {
                    "date": (date.today() - timedelta(days=14)).isoformat(),
                    "weekday": wd,
                    "hourly_base_kwh": [20.0] * 24,
                    "hourly_hydro_kwh": [1.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                },
            ]
            update_weekday_profiles(hist)
            save_history(tmp, hist)
            detail = weekday_base_consumption_detail(tmp, date.today())
            self.assertEqual(detail["sample_count"], 2)
            self.assertGreater(detail["total_kwh"], 0)
            self.assertIn("Durchschnitt", detail["source_note"])


if __name__ == "__main__":
    unittest.main()
