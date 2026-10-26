# -*- coding: utf-8 -*-
"""Tests Verbrauchshistorie und Energiebilanz."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from control_algorithm.consumption_history import (
    ARCHIVE_SCHEDULE_HOUR,
    ARCHIVE_SCHEDULE_MINUTE,
    actual_base_consumption_kwh,
    archive_day_from_timeseries,
    build_consumption_forecast_display,
    build_day_record,
    corrected_base_consumption_for_anomaly,
    algo_attributed_miner_kwh,
    empty_history,
    expected_base_consumption_kwh,
    integrate_base_miner_kwh_by_hour,
    integrate_base_consumption_window,
    integrate_kw_by_hour,
    integrate_miner_consumption_window,
    load_history,
    maybe_archive_daily_consumption,
    save_history,
    trim_fifo,
    update_weekday_profiles,
)
from control_algorithm.energy_balance import (
    compute_balance_projection,
    miner_surplus_kwh,
    normalize_risk_factor,
)
from control_algorithm.load_scheduler import (
    apply_device_runtime_guards,
    check_min_soc_emergency,
    pick_pause_candidate,
    plan_load_actions,
)


class TestConsumptionHistory(unittest.TestCase):
    def test_integrate_subtracts_miner_for_base(self):
        samples = [
            {"m": 480, "betrieb": 50.0, "miner_kw": 10.0, "hydro_kw": 5.0},
            {"m": 540, "betrieb": 50.0, "miner_kw": 10.0, "hydro_kw": 5.0},
        ]
        record = build_day_record(date.today().isoformat(), samples)
        self.assertGreater(record["hourly_base_kwh"][8], 0)
        self.assertLess(record["hourly_base_kwh"][8], record["hourly_verbrauch_kwh"][8])
        self.assertAlmostEqual(sum(record["hourly_base_pct"]), 1.0, places=4)
        self.assertAlmostEqual(sum(record["hourly_verbrauch_pct"]), 1.0, places=4)
        self.assertEqual(record["total_base_kwh"], sum(record["hourly_base_kwh"]))

    def test_segment_base_plus_miner_equals_total(self):
        samples = [
            {"m": 0, "betrieb": 40.0, "miner_kw": 10.0},
            {"m": 60, "betrieb": 40.0, "miner_kw": 10.0},
        ]
        total, miner, base = integrate_base_miner_kwh_by_hour(samples)
        self.assertAlmostEqual(base[0], 30.0, places=2)
        self.assertAlmostEqual(miner[0], 10.0, places=2)
        self.assertAlmostEqual(base[0] + miner[0], total[0], places=2)

    def test_fifo_trim(self):
        hist = empty_history()
        for i in range(65):
            hist["days"].append({"date": f"2026-01-{i+1:02d}", "weekday": 0})
        trim_fifo(hist, 60)
        self.assertEqual(len(hist["days"]), 60)

    def test_weekday_profile_fallback(self):
        hist = empty_history()
        recent = (date.today() - timedelta(days=3)).isoformat()
        hist["days"] = [
            {
                "date": recent,
                "weekday": date.fromisoformat(recent).weekday(),
                "hourly_base_kwh": [1.0] * 24,
                "hourly_hydro_kwh": [0.5] * 24,
                "hourly_verbrauch_pct": [1 / 24] * 24,
                "hourly_base_pct": [1 / 24] * 24,
            }
        ]
        update_weekday_profiles(hist)
        wd = str(date.fromisoformat(recent).weekday())
        self.assertEqual(hist["weekday_profiles"][wd]["sample_count"], 1)
        self.assertAlmostEqual(
            sum(hist["weekday_profiles"][wd]["hourly_base_pct_avg"]), 1.0, places=4
        )

    def test_weekday_profile_uses_only_last_two_months(self):
        """Ältere Montags-Tage außerhalb ~2 Monate zählen nicht mehr mit."""
        from control_algorithm.consumption_history import WEEKDAY_PROFILE_LOOKBACK_DAYS

        hist = empty_history()
        ref = date(2026, 8, 6)
        # Montag innerhalb Fenster: hoher Verbrauch
        recent_mon = ref - timedelta(days=7)  # 2026-07-30 is Thursday... need Monday
        # Find recent Monday within lookback
        recent_mon = ref
        while recent_mon.weekday() != 0:
            recent_mon -= timedelta(days=1)
        old_mon = recent_mon - timedelta(days=WEEKDAY_PROFILE_LOOKBACK_DAYS + 7)
        while old_mon.weekday() != 0:
            old_mon -= timedelta(days=1)

        def day(d: date, base: float) -> dict:
            return {
                "date": d.isoformat(),
                "weekday": d.weekday(),
                "hourly_base_kwh": [base] * 24,
                "hourly_hydro_kwh": [0.0] * 24,
                "hourly_verbrauch_pct": [1 / 24] * 24,
                "hourly_base_pct": [1 / 24] * 24,
                "total_base_kwh": base * 24,
            }

        hist["days"] = [day(old_mon, 10.0), day(recent_mon, 2.0)]
        update_weekday_profiles(hist, reference=ref)
        prof = hist["weekday_profiles"]["0"]
        self.assertEqual(prof["sample_count"], 1)
        self.assertAlmostEqual(prof["hourly_base_kwh_avg"][0], 2.0, places=3)
        self.assertEqual(prof.get("lookback_days"), WEEKDAY_PROFILE_LOOKBACK_DAYS)


    def test_weekday_profile_respects_custom_lookback(self):
        """Kurzer Lookback ignoriert ältere gleiche Wochentage im Archiv."""
        hist = empty_history()
        ref = date(2026, 8, 23)  # Sonntag
        def day(d: date, base: float) -> dict:
            return {
                "date": d.isoformat(),
                "weekday": d.weekday(),
                "hourly_base_kwh": [base] * 24,
                "hourly_hydro_kwh": [0.0] * 24,
                "hourly_verbrauch_pct": [1 / 24] * 24,
                "hourly_base_pct": [1 / 24] * 24,
                "total_base_kwh": base * 24,
            }
        # 4 Sonntage: 23.08, 16.08, 09.08, 02.08
        sundays = [ref - timedelta(days=7 * i) for i in range(4)]
        hist["days"] = [day(d, 10.0 if i else 2.0) for i, d in enumerate(sundays)]
        update_weekday_profiles(hist, reference=ref, lookback_days=14)
        prof = hist["weekday_profiles"]["6"]
        self.assertEqual(prof["sample_count"], 3)  # heute + 2 Vorwochen
        self.assertEqual(prof.get("lookback_days"), 14)

    def test_consumption_forecast_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            cf = build_consumption_forecast_display(tmp)
            self.assertEqual(len(cf.get("hours") or []), 24)
            self.assertIn("total_expected_kwh", cf)

    def test_archive_skips_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            samples = [
                {"m": 0, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
                {"m": 60, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
            ]
            d = date.today().isoformat()
            self.assertTrue(archive_day_from_timeseries(tmp, d, samples, max_days=60))
            self.assertFalse(archive_day_from_timeseries(tmp, d, samples, max_days=60))

    def test_maybe_archive_daily_before_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            samples = [
                {"m": 0, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
                {"m": 60, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
            ]
            now = datetime.now().replace(
                hour=ARCHIVE_SCHEDULE_HOUR,
                minute=ARCHIVE_SCHEDULE_MINUTE - 1,
                second=0,
                microsecond=0,
            )
            self.assertFalse(
                maybe_archive_daily_consumption(
                    tmp, now, samples=samples, day_str=now.date().isoformat()
                )
            )

    def test_maybe_archive_daily_at_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            samples = [
                {"m": 0, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
                {"m": 60, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
            ]
            now = datetime.now().replace(
                hour=ARCHIVE_SCHEDULE_HOUR,
                minute=ARCHIVE_SCHEDULE_MINUTE,
                second=0,
                microsecond=0,
            )
            self.assertTrue(
                maybe_archive_daily_consumption(
                    tmp, now, samples=samples, day_str=now.date().isoformat()
                )
            )
            self.assertEqual(len(load_history(tmp).get("days") or []), 1)

    def test_maybe_archive_stale_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = (date.today() - timedelta(days=1)).isoformat()
            samples = [
                {"m": 0, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
                {"m": 60, "betrieb": 40.0, "miner_kw": 5.0, "hydro_kw": 2.0},
            ]
            now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
            self.assertTrue(
                maybe_archive_daily_consumption(tmp, now, samples=samples, day_str=stale)
            )

    def test_integrate_miner_consumption_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            em_dir = Path(tmp) / "energy_monitor"
            em_dir.mkdir(parents=True)
            today = date.today().isoformat()
            ts_path = em_dir / "energy_timeseries_24h.json"
            ts_path.write_text(
                json.dumps(
                    {
                        "date": today,
                        "samples": [
                            {"m": 0, "betrieb": 40.0, "miner_kw": 0.0},
                            {"m": 60, "betrieb": 40.0, "miner_kw": 0.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            miner_kwh = integrate_miner_consumption_window(tmp, 1.0)
            self.assertAlmostEqual(miner_kwh, 0.0, places=2)

    def test_corrected_base_consumption_for_anomaly_adjusts_missing_miner_kw(self):
        with tempfile.TemporaryDirectory() as tmp:
            em_dir = Path(tmp) / "energy_monitor"
            em_dir.mkdir(parents=True)
            today = date.today().isoformat()
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [1.0] * 24,
                    "hourly_hydro_kwh": [0.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                    "hourly_base_pct": [1 / 24] * 24,
                }
            ] * 8
            update_weekday_profiles(hist)
            save_history(tmp, hist)
            ts_path = em_dir / "energy_timeseries_24h.json"
            ts_path.write_text(
                json.dumps(
                    {
                        "date": today,
                        "samples": [
                            {"m": 0, "betrieb": 50.0, "miner_kw": 10.0},
                            {"m": 60, "betrieb": 50.0, "miner_kw": 10.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "devices": [
                    {"ip": "10.0.0.1", "enabled": True, "power_kw": 10.0, "priority": "low"},
                ],
            }
            live = {"miner_status": {"10.0.0.1": "running"}}
            now = datetime.now().replace(hour=1, minute=0, second=0, microsecond=0)
            resumed = (now - timedelta(hours=1)).isoformat()
            state = {"device_runtime": {"10.0.0.1": {"resumed_at": resumed}}}
            from control_algorithm.consumption_history import integrate_base_consumption_window

            raw_actual, _expected = integrate_base_consumption_window(tmp, 1.0, now)
            actual, expected = corrected_base_consumption_for_anomaly(
                config, live, tmp, 1.0, now, state
            )
            self.assertLess(actual, raw_actual)
            self.assertGreater(raw_actual - actual, 0.2)

    def test_algo_attributed_miner_kwh_respects_partial_runtime(self):
        config = {
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 6.0, "priority": "low"},
            ],
        }
        now = datetime(2026, 8, 29, 14, 0, 0)
        resumed = (now - timedelta(minutes=20)).isoformat()
        state = {"device_runtime": {"10.0.0.1": {"resumed_at": resumed}}}
        live = {"miner_status": {"10.0.0.1": "running"}}
        with tempfile.TemporaryDirectory() as tmp:
            kwh = algo_attributed_miner_kwh(config, live, tmp, 1.0, now, state)
            self.assertAlmostEqual(kwh, 2.0, delta=0.05)

    def test_algo_attributed_counts_paused_device_runtime_in_window(self):
        """Pausiertes Gerät: frühere ON-Zeit im Fenster muss trotzdem abgezogen werden."""
        config = {
            "devices": [
                {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            ],
        }
        now = datetime(2026, 9, 20, 13, 0, 0)
        paused_at = now - timedelta(hours=1)  # lief 2 h, dann 1 h Pause im 3-h-Fenster
        resumed_at = now - timedelta(hours=3)
        state = {
            "device_runtime": {
                "10.0.0.1": {
                    "resumed_at": None,
                    "paused_at": paused_at.isoformat(),
                    "last_action": "pause",
                }
            }
        }
        live = {"miner_status": {"10.0.0.1": "paused"}}
        store = {
            "sessions": [
                {
                    "ip": "10.0.0.1",
                    "paused_at": paused_at.isoformat(),
                    "resumed_at": None,
                }
            ],
            "open_by_ip": {"10.0.0.1": 0},
            "events": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "control_algorithm.steering_history.load_steering_history",
                return_value=store,
            ):
                # Runtime hatte vor der Pause einen Start – für Fall ohne Session-Lücke
                state["device_runtime"]["10.0.0.1"]["resumed_at"] = resumed_at.isoformat()
                # Nach Pause: resumed_at wird typischerweise geleert; Session trägt Pause
                state["device_runtime"]["10.0.0.1"]["resumed_at"] = None
                kwh = algo_attributed_miner_kwh(config, live, tmp, 3.0, now, state)
        # Fenster 3 h, Pause letzte 1 h → 2 h × 5.5 kW = 11 kWh
        self.assertAlmostEqual(kwh, 11.0, delta=0.15)

    def test_check_anomaly_ignores_algo_miner_spike_without_miner_kw(self):
        from control_algorithm.anomaly_detection import check_anomaly

        with tempfile.TemporaryDirectory() as tmp:
            em_dir = Path(tmp) / "energy_monitor"
            em_dir.mkdir(parents=True)
            today = date.today().isoformat()
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [1.0] * 24,
                    "hourly_hydro_kwh": [0.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                    "hourly_base_pct": [1 / 24] * 24,
                }
            ] * 8
            update_weekday_profiles(hist)
            save_history(tmp, hist)
            ts_path = em_dir / "energy_timeseries_24h.json"
            ts_path.write_text(
                json.dumps(
                    {
                        "date": today,
                        "samples": [
                            {"m": 0, "betrieb": 1.0, "miner_kw": 0.0},
                            {"m": 60, "betrieb": 6.5, "miner_kw": 0.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "anomaly_threshold_percent": 20,
                "anomaly_window_hours": 1.0,
                "anomaly_min_history_days": 7,
                "anomaly_low_enabled": False,
                "devices": [
                    {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
                ],
            }
            now = datetime.now().replace(hour=1, minute=0, second=0, microsecond=0)
            resumed = (now - timedelta(minutes=55)).isoformat()
            state = {
                "device_runtime": {"192.0.2.10": {"resumed_at": resumed}},
            }
            live = {"miner_status": {"192.0.2.10": "running"}}
            state, actions, _msgs = check_anomaly(config, state, live, tmp, now=now)
            self.assertFalse(state.get("anomaly_active"))
            self.assertEqual(actions, [])

    def test_check_anomaly_ignores_spike_when_algo_device_already_paused(self):
        """+5.5 kW durch Algo, Gerät danach pausiert → keine Hoch-Anomalie."""
        from control_algorithm.anomaly_detection import check_anomaly

        with tempfile.TemporaryDirectory() as tmp:
            em_dir = Path(tmp) / "energy_monitor"
            em_dir.mkdir(parents=True)
            today = date.today().isoformat()
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [1.0] * 24,
                    "hourly_hydro_kwh": [0.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                    "hourly_base_pct": [1 / 24] * 24,
                }
            ] * 8
            update_weekday_profiles(hist)
            save_history(tmp, hist)
            ts_path = em_dir / "energy_timeseries_24h.json"
            ts_path.write_text(
                json.dumps(
                    {
                        "date": today,
                        "samples": [
                            {"m": 0, "betrieb": 1.0, "miner_kw": 0.0},
                            {"m": 60, "betrieb": 6.5, "miner_kw": 0.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "anomaly_threshold_percent": 20,
                "anomaly_window_hours": 1.0,
                "anomaly_min_history_days": 7,
                "anomaly_low_enabled": False,
                "devices": [
                    {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
                ],
            }
            now = datetime.now().replace(hour=1, minute=0, second=0, microsecond=0)
            paused_at = now - timedelta(minutes=5)
            resumed = (now - timedelta(minutes=55)).isoformat()
            state = {
                "device_runtime": {
                    "192.0.2.10": {
                        "paused_at": paused_at.isoformat(),
                        "resumed_at": None,
                        "last_action": "pause",
                    }
                },
            }
            live = {"miner_status": {"192.0.2.10": "paused"}}
            store = {
                "sessions": [
                    {
                        "ip": "192.0.2.10",
                        "paused_at": paused_at.isoformat(),
                        "resumed_at": None,
                    }
                ],
                "open_by_ip": {"192.0.2.10": 0},
                "events": [],
            }
            with patch(
                "control_algorithm.steering_history.load_steering_history",
                return_value=store,
            ):
                # resumed vor Pause für Attribution der ON-Phase
                state["device_runtime"]["192.0.2.10"]["resumed_at"] = resumed
                # Session-basierte Rechnung: Fenster minus Pause
                state["device_runtime"]["192.0.2.10"]["resumed_at"] = None
                state, actions, _msgs = check_anomaly(
                    config, state, live, tmp, now=now
                )
            self.assertFalse(state.get("anomaly_active"))
            self.assertEqual(actions, [])
    def test_anomaly_window_after_midnight(self):
        with tempfile.TemporaryDirectory() as tmp:
            em_dir = Path(tmp) / "energy_monitor"
            em_dir.mkdir(parents=True)
            today = date.today().isoformat()
            ts_path = em_dir / "energy_timeseries_24h.json"
            ts_path.write_text(
                json.dumps(
                    {
                        "date": today,
                        "samples": [
                            {"m": 0, "betrieb": 40.0, "miner_kw": 0.0},
                            {"m": 30, "betrieb": 40.0, "miner_kw": 0.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [2.0] * 24,
                    "hourly_hydro_kwh": [0.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                    "hourly_base_pct": [1 / 24] * 24,
                }
            ]
            update_weekday_profiles(hist)
            save_history(tmp, hist)

            now = datetime.now().replace(hour=0, minute=30, second=0, microsecond=0)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            actual, expected = integrate_base_consumption_window(tmp, 3.0, now)
            self.assertAlmostEqual(actual, actual_base_consumption_kwh(tmp, midnight, now), places=2)
            self.assertAlmostEqual(
                expected, expected_base_consumption_kwh(tmp, midnight, now), places=2
            )
            full_window_expected = expected_base_consumption_kwh(
                tmp, now - timedelta(hours=3), now
            )
            self.assertLess(expected, full_window_expected)


class TestEnergyBalance(unittest.TestCase):
    def test_risk_factor_clamp(self):
        self.assertEqual(normalize_risk_factor(150), 100.0)
        self.assertEqual(normalize_risk_factor(80), 80.0)

    def test_projected_soc_capped_at_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [1.0] * 24,
                    "hourly_hydro_kwh": [0.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                    "hourly_base_pct": [1 / 24] * 24,
                }
            ]
            update_weekday_profiles(hist)
            save_history(tmp, hist)

            config = {
                "storage_kwh": 1100.0,
                "min_soc_percent": 35.0,
                "max_soc_percent": 95.0,
                "risk_factor_percent": 100.0,
                "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            }
            state = {
                "pv_forecast_today_hours": {f"h{h:02d}": 200.0 for h in range(24)},
                "pv_forecast_tomorrow_hours": {f"h{h:02d}": 200.0 for h in range(24)},
                "pv_forecast_day_after_hours": {f"h{h:02d}": 200.0 for h in range(24)},
            }
            live = {"soc_percent": 80.0}
            now = datetime.now().replace(hour=1, minute=0, second=0, microsecond=0)
            proj = compute_balance_projection(config, state, live, tmp, now)
            max_kwh = 1100.0 * 0.95
            self.assertLessEqual(proj["projected_soc"], 95.0)
            self.assertLessEqual(proj.get("projected_kwh_raw", 0), max_kwh + 1.0)
            self.assertGreater(proj.get("projected_kwh_raw", 0), 850.0)

    def test_miner_surplus_uses_capped_projection(self):
        config = {
            "storage_kwh": 1000.0,
            "min_soc_percent": 35.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
        }
        state = {
            "balance_projection": {
                "target_soc": 40.0,
                "target_datetime": "2099-01-02T07:00:00",
                "base_consumption_kwh": 10.0,
                "pv_forecast_kwh": 5000.0,
                "hydro_forecast_kwh": 500.0,
            },
        }
        live = {"soc_percent": 90.0}
        surplus = miner_surplus_kwh(config, state, live, "/tmp")
        self.assertLessEqual(surplus, 600.0)

    def test_surplus_uses_battery_above_target(self):
        config = {
            "storage_kwh": 1000.0,
            "min_soc_percent": 35.0,
            "risk_factor_percent": 100.0,
            "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
        }
        state = {
            "pv_forecast_today_hours": {f"h{h:02d}": 5.0 for h in range(5, 23)},
            "pv_forecast_tomorrow_hours": {f"h{h:02d}": 50.0 for h in range(5, 23)},
            "balance_projection": {
                "target_soc": 40.0,
                "target_datetime": "2099-01-02T07:00:00",
                "base_consumption_kwh": 50.0,
                "pv_forecast_kwh": 100.0,
                "hydro_forecast_kwh": 20.0,
            },
        }
        live = {"soc_percent": 80.0}
        from control_algorithm.energy_balance import miner_surplus_kwh

        surplus = miner_surplus_kwh(config, state, live, "/tmp")
        # 800 + 120 - 50 - 400 = 470
        self.assertGreater(surplus, 400.0)

    def test_projection_unreachable_triggers_low_soc(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = empty_history()
            hist["days"] = [
                {
                    "date": (date.today() - timedelta(days=1)).isoformat(),
                    "weekday": date.today().weekday(),
                    "hourly_base_kwh": [100.0] * 24,
                    "hourly_hydro_kwh": [1.0] * 24,
                    "hourly_verbrauch_pct": [1 / 24] * 24,
                }
            ]
            update_weekday_profiles(hist)
            save_history(tmp, hist)

            config = {
                "storage_kwh": 100.0,
                "min_soc_percent": 35.0,
                "risk_factor_percent": 100.0,
                "soc_targets": [{"time": "07:00", "soc_percent": 40.0}],
            }
            state = {
                "pv_forecast_today_hours": {f"h{h:02d}": 0.0 for h in range(5, 23)},
                "pv_forecast_tomorrow_hours": {f"h{h:02d}": 0.0 for h in range(5, 23)},
            }
            live = {"soc_percent": 36.0}
            now = datetime.now().replace(hour=20, minute=0, second=0, microsecond=0)
            proj = compute_balance_projection(config, state, live, tmp, now)
            self.assertFalse(proj.get("reachable"))


class TestLoadScheduler(unittest.TestCase):
    def test_pause_picks_lowest_priority_running(self):
        devices = [
            {"ip": "high", "enabled": True, "power_kw": 5.5, "priority": "high"},
            {"ip": "med", "enabled": True, "power_kw": 5.3, "priority": "medium"},
            {"ip": "low", "enabled": True, "power_kw": 3.2, "priority": "low"},
        ]
        status = {"high": "running", "med": "running", "low": "running"}
        config = {"min_device_runtime_hours": 3.0}
        d = pick_pause_candidate(devices, status, {}, config)
        self.assertEqual(d["ip"], "low")

    def test_pause_skips_min_runtime_protected(self):
        from datetime import datetime

        devices = [
            {"ip": "low", "enabled": True, "power_kw": 3.2, "priority": "low"},
            {"ip": "med", "enabled": True, "power_kw": 5.3, "priority": "medium"},
        ]
        status = {"low": "running", "med": "running"}
        state = {
            "device_runtime": {
                "low": {"resumed_at": datetime.now().isoformat(), "last_action": "resume"}
            }
        }
        config = {"min_device_runtime_hours": 3.0}
        d = pick_pause_candidate(devices, status, state, config)
        self.assertEqual(d["ip"], "med")

    def test_min_soc_emergency_pauses_all(self):
        config = {
            "min_soc_percent": 40.0,
            "devices": [
                {"ip": "1", "enabled": True, "power_kw": 5, "priority": "high"},
                {"ip": "2", "enabled": True, "power_kw": 3, "priority": "low"},
            ],
        }
        live = {"soc_percent": 35.0, "miner_status": {"1": "running", "2": "running"}}
        actions, msgs = check_min_soc_emergency(config, live)
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a["action"] == "pause" for a in actions))

    def test_min_runtime_blocks_pause(self):
        config = {"min_device_runtime_hours": 3.0}
        state = {
            "device_runtime": {
                "1": {
                    "resumed_at": datetime.now().isoformat(),
                    "last_action": "resume",
                }
            }
        }
        actions = [{"ip": "1", "action": "pause", "force": False}]
        filtered = apply_device_runtime_guards(actions, state, config)
        self.assertEqual(len(filtered), 0)

    def test_min_runtime_allows_force_pause(self):
        config = {"min_device_runtime_hours": 3.0}
        state = {
            "device_runtime": {
                "1": {"resumed_at": datetime.now().isoformat(), "last_action": "resume"}
            }
        }
        actions = [{"ip": "1", "action": "pause", "force": True}]
        filtered = apply_device_runtime_guards(actions, state, config)
        self.assertEqual(len(filtered), 1)

    def test_min_runtime_allows_budget_shed_pause(self):
        config = {"min_device_runtime_hours": 3.0, "devices": []}
        state = {
            "device_runtime": {
                "1": {"resumed_at": datetime.now().isoformat(), "last_action": "resume"}
            }
        }
        actions = [
            {
                "ip": "1",
                "action": "pause",
                "force": False,
                "budget_shed": True,
                "morning_floor": True,
            }
        ]
        filtered = apply_device_runtime_guards(actions, state, config)
        self.assertEqual(len(filtered), 1)


if __name__ == "__main__":
    unittest.main()
