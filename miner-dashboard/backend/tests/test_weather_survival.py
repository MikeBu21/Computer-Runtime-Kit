# -*- coding: utf-8 -*-
"""Tests Wetter-Überleben, 7-Tage-PV und Wochenplan."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

from control_algorithm.energy_balance import (
    DEFAULT_HORIZON_DAYS,
    _export_avoidance_kw,
    _pv_hours_for_date,
    build_weekly_energy_plan,
    compute_horizon_projection,
    planning_horizon_days,
)
from control_algorithm.oms_pv_forecast import last_pv_hour_from_hours
from control_algorithm.weather_survival import (
    classify_weather_tier,
    classify_weather_window,
    detect_bad_streak,
    dynamic_soc_targets_for_horizon,
    merged_soc_targets_in_horizon,
    weather_context,
)


def _state_with_pv_by_date(today: date, daily_values: list) -> dict:
    by_date = {}
    for i, kwh in enumerate(daily_values):
        d = today + timedelta(days=i)
        hours = {f"h{h:02d}": (kwh / 18.0 if 5 <= h <= 22 else 0.0) for h in range(24)}
        by_date[d.isoformat()] = {
            "daily_kwh": kwh,
            "hours": hours,
            "last_pv_hour": last_pv_hour_from_hours(hours),
            "offset": i,
        }
    return {
        "pv_forecast_by_date": by_date,
        "pv_yield_today_kwh": daily_values[0] if daily_values else 0,
        "pv_yield_tomorrow_kwh": daily_values[1] if len(daily_values) > 1 else 0,
        "performance_ratio": 1.0,
    }


class TestWeatherSurvival:
    def test_default_horizon_is_7(self):
        assert DEFAULT_HORIZON_DAYS == 7

    def test_classify_weather_tiers(self):
        assert classify_weather_tier(600, 1000) == "good"
        assert classify_weather_tier(400, 1000) == "normal"
        assert classify_weather_tier(100, 1000) == "bad"

    def test_detect_bad_streak(self):
        window = [
            {"offset": 0, "weather_tier": "good"},
            {"offset": 1, "weather_tier": "bad"},
            {"offset": 2, "weather_tier": "bad"},
            {"offset": 3, "weather_tier": "normal"},
        ]
        start, length = detect_bad_streak(window, from_offset=1)
        assert start == 1
        assert length == 2

    def test_pv_hours_for_date_day5(self):
        today = date(2026, 7, 13)
        state = _state_with_pv_by_date(today, [100, 200, 300, 400, 500, 600, 700])
        d5 = today + timedelta(days=5)
        hours = _pv_hours_for_date(state, d5, today)
        assert sum(hours.values()) > 0
        assert hours != (state.get("pv_forecast_tomorrow_hours") or {})

    def test_dynamic_pre_storm_checkpoint(self):
        today = date(2026, 7, 13)
        cfg = {
            "has_storage": True,
            "storage_kwh": 1000,
            "max_soc_percent": 95,
            "soc_targets": [{"time": "07:00", "soc_percent": 40}],
            "pv_kwp": 190,
            "perfect_weather_sun_hours": 6,
        }
        state = _state_with_pv_by_date(today, [800, 100, 100, 100, 100, 100, 100])
        now = datetime.combine(today, datetime.min.time()).replace(hour=10)
        extras = dynamic_soc_targets_for_horizon(cfg, state, now, horizon_days=7)
        assert any(e.get("dynamic_reason") == "pre_storm_charge" for e in extras)

    def test_merged_soc_targets_includes_dynamic(self):
        today = date(2026, 7, 13)
        cfg = {
            "soc_targets": [{"time": "07:00", "soc_percent": 40}],
            "max_soc_percent": 95,
            "pv_kwp": 190,
            "perfect_weather_sun_hours": 6,
        }
        state = _state_with_pv_by_date(today, [800, 80, 80, 80, 80, 80, 80])
        now = datetime.combine(today, datetime.min.time()).replace(hour=8)
        merged = merged_soc_targets_in_horizon(cfg, state, now, 7)
        assert len(merged) >= 2

    def test_merged_empty_user_targets_uses_recovery_horizon(self):
        today = date(2026, 7, 13)
        cfg = {
            "soc_targets": [],
            "min_soc_percent": 35,
            "max_soc_percent": 95,
            "pv_kwp": 190,
            "perfect_weather_sun_hours": 6,
        }
        # PV ab 08:00 spürbar → Erholungshorizont, keine Fix-07:00
        hours = {f"h{h:02d}": (12.0 if h >= 8 else 0.0) for h in range(24)}
        state = {
            "pv_forecast_by_date": {
                (today + timedelta(days=i)).isoformat(): {
                    "daily_kwh": 600,
                    "hours": hours,
                }
                for i in range(3)
            }
        }
        now = datetime.combine(today, datetime.min.time()).replace(hour=10)
        merged = merged_soc_targets_in_horizon(cfg, state, now, 3)
        assert merged
        recoveries = [c for c in merged if c.get("dynamic_reason") == "recovery_horizon"]
        assert recoveries
        assert all(float(c["soc_percent"]) == 35.0 for c in recoveries)
        assert all("07:00" not in str(c.get("target_datetime")) for c in recoveries)

    @patch("control_algorithm.energy_balance.export_avoidance_recommended", return_value=True)
    def test_export_avoidance_kw(self, _mock):
        cfg = {
            "has_storage": True,
            "devices": [{"ip": "1", "enabled": True, "power_kw": 10}],
        }
        live = {"pv_kw": 5.0, "soc_percent": 93}
        kw = _export_avoidance_kw(cfg, {}, live, datetime.now())
        assert kw is not None and kw > 0

    def test_risk_factor_scales_miner_surplus(self):
        today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        base_cfg = {
            "enabled": True,
            "has_storage": True,
            "storage_kwh": 500,
            "min_soc_percent": 35,
            "max_soc_percent": 95,
            "soc_targets": [{"time": "07:00", "soc_percent": 40}],
            "devices": [{"ip": "10.0.0.1", "enabled": True, "power_kw": 5, "priority": "medium"}],
            "pv_kwp": 190,
            "perfect_weather_sun_hours": 6,
        }
        state = _state_with_pv_by_date(today.date(), [300] * 7)
        live = {"soc_percent": 60, "miner_status": {}}

        cfg100 = {**base_cfg, "risk_factor_percent": 100}
        cfg80 = {**base_cfg, "risk_factor_percent": 80}
        with patch(
            "control_algorithm.energy_balance._max_miner_kw_for_horizon",
            return_value=(10.0, "none", {}),
        ):
            # Bei gemocktem Budget: Surplus gleich (Risiko steckt in echter Simulation)
            proj100 = compute_horizon_projection(cfg100, state, live, "/tmp", today, horizon_days=7)
            proj80 = compute_horizon_projection(cfg80, state, live, "/tmp", today, horizon_days=7)
        assert float(proj100.get("risk_factor_percent") or 0) == 100.0
        assert float(proj80.get("risk_factor_percent") or 0) == 80.0

        # Echte Simulation: 80 % PV → niedrigeres oder gleiches Miner-Budget
        with patch("control_algorithm.energy_balance.expected_base_consumption_kwh", return_value=0.5):
            with patch("control_algorithm.energy_balance.expected_hydro_production_kwh", return_value=0.0):
                from control_algorithm.energy_balance import _max_miner_kw_for_horizon

                kw100, _, _ = _max_miner_kw_for_horizon(cfg100, state, live, "/tmp", today, 3)
                kw80, _, _ = _max_miner_kw_for_horizon(cfg80, state, live, "/tmp", today, 3)
        assert kw80 <= kw100 + 0.05

    def test_build_weekly_energy_plan_7_days(self):
        today = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
        cfg = {
            "has_storage": True,
            "storage_kwh": 400,
            "max_soc_percent": 95,
            "min_soc_percent": 35,
            "soc_targets": [{"time": "07:00", "soc_percent": 40}],
            "pv_kwp": 190,
            "perfect_weather_sun_hours": 6,
        }
        state = _state_with_pv_by_date(today.date(), [250, 200, 180, 160, 140, 120, 100])
        state["horizon_projection"] = {
            "miner_budget_kw": 2.0,
            "risk_factor_percent": 100,
            "weather_context": weather_context(cfg, state, days=7),
        }
        live = {"soc_percent": 55, "pv_kw": 0}
        plan = build_weekly_energy_plan(cfg, state, live, "/home/piMiner/miner-dashboard/backend", today)
        assert len(plan.get("days") or []) == planning_horizon_days(cfg)
        today_hours = plan["days"][0]["hours"]
        assert len(today_hours) == 24
        assert any(h.get("soc_projected_pct") is not None for h in today_hours)
        current = next(h for h in today_hours if h.get("is_current"))
        assert current.get("soc_actual_pct") == 55.0
        # Zukünftige Stunden ohne Ist; vergangene können aus 24h-Timeseries kommen
        future = [h for h in today_hours if not h.get("is_past") and not h.get("is_current")]
        assert all(h.get("soc_actual_pct") is None for h in future)
