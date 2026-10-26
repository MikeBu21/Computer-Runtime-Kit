# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from control_algorithm.late_start import (
    compute_late_start_plan,
    first_pv_hour_from_hours,
    next_pv_start_deadline,
)


def test_first_pv_hour():
    hours = {"h05": 0.0, "h06": 0.05, "h07": 0.4, "h08": 2.0}
    assert first_pv_hour_from_hours(hours) == 7


def test_late_start_defers_when_plenty_of_time():
    cfg = {
        "min_soc_percent": 28,
        "max_soc_percent": 97,
        "storage_kwh": 41,
        "devices": [
            {"ip": "1", "enabled": True, "power_kw": 5.5, "priority": 10},
        ],
    }
    now = datetime(2026, 7, 27, 22, 0, 0)
    state = {
        "pv_forecast_tomorrow_hours": {
            "h05": 0.0,
            "h06": 0.1,
            "h07": 1.5,
            "h08": 3.0,
        },
        "pv_yield_tomorrow_kwh": 60,
    }
    live = {"soc_percent": 90.0, "pv_kw": 0.0, "grid_kw": 0.1}
    plan = compute_late_start_plan(
        cfg,
        state,
        live,
        projection={"target_soc": 50.0},
        now=now,
        urgent_export=False,
    )
    assert plan["excess_kwh"] > 1
    assert plan["latest_start"] is not None
    # 90→50 % = 16.4 kWh / 5.5 kW ≈ 3 h → Start ~04:00, also defer um 22:00
    assert plan["defer"] is True
    assert plan["reason"] in ("wait_until_latest", "no_excess")


def test_late_start_not_deferred_on_export():
    cfg = {
        "min_soc_percent": 28,
        "max_soc_percent": 97,
        "storage_kwh": 41,
        "devices": [{"ip": "1", "enabled": True, "power_kw": 5.5, "priority": 10}],
    }
    plan = compute_late_start_plan(
        cfg,
        {"pv_forecast_tomorrow_hours": {"h07": 2.0}},
        {"soc_percent": 90.0, "pv_kw": 0.0},
        now=datetime(2026, 7, 27, 22, 0, 0),
        urgent_export=True,
    )
    assert plan["defer"] is False
    assert plan["urgent"] is True


def test_late_start_uses_run_reserve_target_instead_of_morning_floor():
    cfg = {
        "min_soc_percent": 30,
        "max_soc_percent": 95,
        "morning_soc_percent": 33,
        "storage_kwh": 41,
        "devices": [
            {"ip": "1", "enabled": True, "power_kw": 5.8, "priority": 10},
        ],
    }
    now = datetime(2026, 10, 1, 20, 0, 0)
    state = {
        "pv_forecast_tomorrow_hours": {"h07": 1.5, "h08": 3.0},
        "pv_yield_tomorrow_kwh": 53,
        "morning_drain_plan": {
            "ok": True,
            "gate_active": True,
            "policy": "hold_only",
            "target_soc_percent": 66.0,
            "must_empty_kwh": 0.0,
            "allowed_ips": [],
        },
    }
    live = {"soc_percent": 66.0, "pv_kw": 0.0, "grid_kw": 0.1}
    plan = compute_late_start_plan(
        cfg, state, live, projection={"target_soc": 30.0}, now=now
    )
    assert plan["target_soc_percent"] >= 60.0
    assert plan["excess_kwh"] < 1.0
    assert plan["defer"] is True


def test_next_deadline_tomorrow():
    now = datetime(2026, 7, 27, 21, 0, 0)
    state = {"pv_forecast_tomorrow_hours": {"h06": 0.0, "h07": 1.2}}
    dt, note = next_pv_start_deadline(state, now)
    assert dt is not None
    assert dt.hour == 7
    assert "morgen" in note
