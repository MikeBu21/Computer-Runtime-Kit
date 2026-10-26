# -*- coding: utf-8 -*-
"""Tests für SoC-Kalibrierung / Lernen."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from control_algorithm.soc_calibration import (
    DEFAULT_BASE_LOAD_SCALE,
    DEFAULT_BATTERY_EFFICIENCY,
    apply_net_to_battery_kwh,
    normalize_base_load_scale,
    normalize_battery_efficiency,
    run_soc_calibration,
    store_morning_soc_forecast,
)


def _cap(kwh, storage, max_soc=None):
    cap = storage
    if storage > 0 and max_soc is not None:
        cap = min(cap, max_soc / 100.0 * storage)
    return max(0.0, min(cap, kwh))


def test_normalize_clamps():
    assert normalize_base_load_scale(0.1) == 0.5
    assert normalize_base_load_scale(9) == 1.5
    assert normalize_battery_efficiency(0.1) == 0.75
    assert normalize_battery_efficiency(1.5) == 1.0


def test_apply_net_charge_and_discharge():
    charged = apply_net_to_battery_kwh(100, 10, 400, 95, 0.9, _cap)
    assert charged == 109.0  # 100 + 10*0.9
    discharged = apply_net_to_battery_kwh(100, -10, 400, 95, 0.9, _cap)
    assert abs(discharged - (100 - 10 / 0.9)) < 1e-6


def test_store_morning_soc_once_per_day():
    state = {}
    store_morning_soc_forecast(state, {"h08": 50.0, "h09": 55.0}, 48.0, date(2026, 7, 27))
    assert state["morning_soc_forecast_date"] == "2026-07-27"
    assert state["morning_soc_start_pct"] == 48.0
    store_morning_soc_forecast(state, {"h08": 99.0}, 99.0, date(2026, 7, 27))
    assert state["morning_soc_forecast_hours"]["h08"] == 50.0  # not overwritten


def test_run_soc_calibration_raises_base_scale_on_high_bias():
    state = {
        "base_load_scale": 1.0,
        "battery_efficiency": 0.95,
        "soc_history": [],
        "soc_days_since_reset": 2,
        "morning_soc_forecast_date": date.today().isoformat(),
        "morning_soc_forecast_hours": {
            f"h{h:02d}": 70.0 for h in range(8, 18)
        },
    }
    # Actual much lower → forecast too high → raise base_load_scale
    actual = [None] * 24
    for h in range(8, 18):
        actual[h] = 50.0
    cfg = {"storage_kwh": 400, "max_soc_percent": 95}
    with patch(
        "control_algorithm.soc_calibration.get_hourly_actual_soc_today",
        return_value=actual,
    ):
        out = run_soc_calibration(state, cfg, "/tmp")
    assert out["base_load_scale"] > DEFAULT_BASE_LOAD_SCALE
    assert out["last_soc_calibration_note"] == "ok"
    assert out["soc_history"]


def test_run_soc_calibration_hit_keeps_params():
    state = {
        "base_load_scale": 1.05,
        "battery_efficiency": 0.93,
        "soc_history": [],
        "soc_days_since_reset": 5,
        "morning_soc_forecast_date": date.today().isoformat(),
        "morning_soc_forecast_hours": {
            f"h{h:02d}": 60.0 + h * 0.1 for h in range(8, 16)
        },
    }
    actual = [None] * 24
    for h in range(8, 16):
        actual[h] = 60.0 + h * 0.1 + 0.5  # within tolerance
    cfg = {"storage_kwh": 400}
    with patch(
        "control_algorithm.soc_calibration.get_hourly_actual_soc_today",
        return_value=actual,
    ):
        out = run_soc_calibration(state, cfg, "/tmp")
    assert abs(out["base_load_scale"] - 1.05) < 0.01
    assert abs(out["battery_efficiency"] - 0.93) < 0.01
