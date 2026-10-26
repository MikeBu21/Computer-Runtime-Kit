# -*- coding: utf-8 -*-
from datetime import datetime

from control_algorithm.device_schedule_24h import build_device_schedule_24h


def test_schedule_24h_uses_week_plan_morning_ramp():
    """Morgen-PV-Stunden → mehrere Geräte mit Zielzeiten ~06–08 Uhr."""
    config = {
        "storage_kwh": 1100,
        "min_soc_percent": 40,
        "min_device_runtime_hours": 0.5,
        "devices": [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.3", "enabled": True, "power_kw": 5.5, "priority": 6},
            {"ip": "10.0.0.4", "enabled": True, "power_kw": 3.2, "priority": 0},
        ],
    }
    now = datetime(2026, 7, 27, 18, 30, 0)
    # Heute ab 18 + morgen 0–17 = 24h
    hours_today = []
    for h in range(24):
        hours_today.append(
            {
                "hour": h,
                "pv_kwh": 10.0 if 6 <= h <= 19 else 0.0,
                "miner_kwh_planned": 5.5 if h == 18 else (0.0 if h >= 23 or h < 5 else 5.5),
                "soc_projected_pct": 55.0,
            }
        )
    hours_tomorrow = []
    for h in range(24):
        pv = 50.0 if 6 <= h <= 19 else 0.0
        miner = 18.58 if 6 <= h <= 19 else (9.0 if 5 <= h <= 22 else 0.0)
        hours_tomorrow.append(
            {
                "hour": h,
                "pv_kwh": pv,
                "miner_kwh_planned": miner,
                "soc_projected_pct": 60.0,
            }
        )
    state = {
        "energy_week_plan": {
            "days": [
                {
                    "day_date": "2026-07-27",
                    "steering_mode": "normal",
                    "hours": hours_today,
                },
                {
                    "day_date": "2026-07-28",
                    "steering_mode": "normal",
                    "hours": hours_tomorrow,
                },
            ]
        },
        "horizon_projection": {
            "target_soc": 40,
            "miner_budget_kw": 18.58,
            "projected_morning_soc": 48,
            "near_term_plan": {"sustained_budget_kw": 5.0},
        },
        "discharge_plan": {
            "phase": "hold",
            "ips": ["10.0.0.1"],
            "devices": [
                {
                    "ip": "10.0.0.1",
                    "power_kw": 5.5,
                    "target_kwh": 20.0,
                    "planned_hours": 4.0,
                }
            ],
            "target_kw": 5.0,
            "target_burn_kwh": 20.0,
            "burned_kwh": 5.0,
            "hold_until_soc": 42.0,
        },
        "device_runtime": {
            "10.0.0.1": {"resumed_at": "2026-07-27T17:00:00"},
        },
    }
    live = {
        "soc_percent": 56,
        "pv_kw": 0.2,
        "miner_status": {
            "10.0.0.1": "running",
            "10.0.0.2": "paused",
            "10.0.0.3": "paused",
            "10.0.0.4": "paused",
        },
    }
    sched = build_device_schedule_24h(config, state, live, now=now)
    assert sched["source"].startswith("energy_week_plan")
    assert sched["pv_ramp_at_display"] != "—"
    # Morgen bei 18.58 kW → 3×5.5 Geräte geplant
    d2 = next(d for d in sched["devices"] if d["ip"] == "10.0.0.2")
    assert d2["in_plan"] is True
    assert d2["on_at"] is not None
    assert "2026-07-28T06" in d2["on_at"] or "2026-07-28T05" in d2["on_at"]
    # Events enthalten Einschalten morgen
    on_ev = [e for e in sched["events"] if e["action"] == "Einschalten" and e["ip"] == "10.0.0.2"]
    assert on_ev
    assert any(s.get("hour") == 8 and len(s.get("ips") or []) >= 3 for s in sched["hour_slots"])


def test_night_slots_reduced_with_day_first_bias():
    """Mit Tag-first-Gewicht: nachts weniger Miner-Budget als in PV-Stunden."""
    config = {
        "storage_kwh": 1100,
        "min_soc_percent": 40,
        "max_device_on_per_day": 3,
        "devices": [
            {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "10.0.0.2", "enabled": True, "power_kw": 3.2, "priority": 0},
        ],
    }
    now = datetime(2026, 7, 28, 10, 0, 0)

    def _hours_for_day():
        rows = []
        for h in range(24):
            pv = 60.0 if 8 <= h <= 17 else 0.0
            rows.append(
                {
                    "hour": h,
                    "pv_kwh": pv,
                    "miner_kwh_planned": 15.0 if pv > 0 else 5.0,
                    "soc_projected_pct": 55.0,
                }
            )
        return rows

    state = {
        "energy_week_plan": {
            "days": [
                {"day_date": "2026-07-28", "steering_mode": "normal", "hours": _hours_for_day()},
                {"day_date": "2026-07-29", "steering_mode": "normal", "hours": _hours_for_day()},
            ]
        },
        "horizon_projection": {"target_soc": 40, "miner_budget_kw": 15.0},
        "steering_learning": {
            "params": {"day_night_shift_bias": -0.5, "night_absorption_max_fraction": 0.1},
            "today": {"date": now.date().isoformat()},
        },
        "absorption_plan_24h": {
            "total_need_kwh": 120.0,
            "hourly_need_kwh": [5.0 if 8 <= (10 + i) % 24 < 18 else 0.5 for i in range(24)],
        },
    }
    live = {"soc_percent": 70, "pv_kw": 5.0, "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"}}
    sched = build_device_schedule_24h(config, state, live, now=now)
    night_kw = [
        float(s.get("miner_budget_kw") or 0)
        for s in sched["hour_slots"]
        if int(s.get("hour") or 0) >= 22 or int(s.get("hour") or 0) < 6
    ]
    pv_kw = [
        float(s.get("miner_budget_kw") or 0)
        for s in sched["hour_slots"]
        if float(s.get("pv_kwh") or 0) > 0.5
    ]
    assert pv_kw, sched["hour_slots"]
    if night_kw:
        assert max(pv_kw) >= max(night_kw)
