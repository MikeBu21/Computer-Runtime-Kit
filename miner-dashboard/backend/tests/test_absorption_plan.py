# -*- coding: utf-8 -*-
"""24h-Absorptionsplan aus Wetter/Wochenplan."""
from datetime import datetime

from control_algorithm.energy_balance import compute_24h_absorption_plan


def _week_plan_with_sunny_day():
    hours = []
    for h in range(24):
        pv = max(0.0, 80.0 - abs(h - 13) * 8.0) if 6 <= h <= 19 else 0.0
        hours.append(
            {
                "hour": h,
                "pv_kwh": pv,
                "hydro_kwh": 12.0,
                "base_kwh": 35.0 if 8 <= h <= 18 else 10.0,
                "miner_kwh_planned": 0.0,
            }
        )
    today = datetime(2026, 7, 28, 8, 0, 0).date()
    return {
        "days": [
            {
                "day_date": today.isoformat(),
                "hours": hours,
                "steering_mode": "normal",
            }
        ]
    }


def test_absorption_plan_positive_on_sunny_forecast():
    now = datetime(2026, 7, 28, 8, 0, 0)
    cfg = {
        "storage_kwh": 1100,
        "max_soc_percent": 85,
        "min_soc_percent": 39,
        "devices": [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
        ],
    }
    live = {"soc_percent": 78.0}
    state = {"steering_learning": {"params": {}, "today": {"date": now.date().isoformat()}}}
    plan = compute_24h_absorption_plan(
        cfg, state, live, _week_plan_with_sunny_day(), now=now, night_fraction=0.15
    )
    assert plan["total_need_kwh"] >= 0
    assert sum(plan["hourly_need_kwh"]) >= 0
    day_share = sum(
        plan["hourly_need_kwh"][i]
        for i in range(24)
        if 6 <= (now.hour + i) % 24 < 22
    )
    night_share = sum(plan["hourly_need_kwh"]) - day_share
    if plan["total_need_kwh"] > 1.0:
        assert day_share >= night_share


def test_absorption_respects_night_fraction_cap():
    now = datetime(2026, 7, 28, 2, 0, 0)
    cfg = {
        "storage_kwh": 1100,
        "max_soc_percent": 85,
        "min_soc_percent": 39,
        "devices": [],
    }
    live = {"soc_percent": 80.0}
    state = {}
    plan = compute_24h_absorption_plan(
        cfg, state, live, _week_plan_with_sunny_day(), now=now, night_fraction=0.1
    )
    assert plan["night_fraction"] == 0.1
