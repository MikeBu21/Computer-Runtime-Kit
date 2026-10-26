# -*- coding: utf-8 -*-
"""Nachtreserve-Neuron: Reserve, Phasen, Lernen, Export-Vorrang."""
from datetime import date, datetime

from control_algorithm.overnight_reserve import (
    compute_overnight_reserve_plan,
    overnight_discharge_allowed,
    overnight_resume_floor_soc,
    should_defer_overnight_resume,
)
from control_algorithm.steering_learning import (
    ensure_steering_learning,
    run_evening_steering_learning,
)


def _base_config():
    return {
        "min_soc_percent": 30.0,
        "max_soc_percent": 95.0,
        "morning_soc_percent": 33.0,
        "storage_kwh": 1100.0,
        "consumption_risk_percent": 0.0,
    }


def _base_state():
    return {
        "energy_week_plan": {
            "days": [
                {
                    "daily_totals": {"base_kwh": 48.0},
                    "hours": [],
                }
            ]
        },
        "pv_yield_tomorrow_kwh": 120.0,
    }


def test_reserve_increases_with_consumption_risk():
    cfg_low = {**_base_config(), "consumption_risk_percent": 0.0}
    cfg_high = {**_base_config(), "consumption_risk_percent": 100.0}
    state = _base_state()
    now = datetime(2026, 9, 1, 20, 0, 0)
    plan_low = compute_overnight_reserve_plan(cfg_low, state, {}, now=now)
    plan_high = compute_overnight_reserve_plan(cfg_high, state, {}, now=now)
    assert plan_high["overnight_reserve_soc"] > plan_low["overnight_reserve_soc"]
    assert plan_high["evening_entry_floor_soc"] > plan_low["evening_entry_floor_soc"]


def test_deep_night_blocks_discharge_below_floor():
    cfg = _base_config()
    state = _base_state()
    now = datetime(2026, 9, 1, 23, 0, 0)
    plan = compute_overnight_reserve_plan(cfg, state, {"soc_percent": 40.0}, now=now)
    assert plan["phase"] == "deep_night"
    allow, reason = overnight_discharge_allowed(plan, soc=40.0)
    assert allow is False
    assert "Tiefnacht" in reason


def test_predawn_allows_discharge_toward_morning_floor():
    cfg = _base_config()
    state = _base_state()
    now = datetime(2026, 9, 1, 6, 0, 0)
    plan = compute_overnight_reserve_plan(cfg, state, {"soc_percent": 50.0}, now=now)
    if plan.get("predawn_discharge_ok"):
        allow, _ = overnight_discharge_allowed(plan, soc=50.0)
        assert allow is True


def test_export_mode_overrides_overnight_block():
    cfg = _base_config()
    state = _base_state()
    now = datetime(2026, 9, 1, 23, 0, 0)
    plan = compute_overnight_reserve_plan(cfg, state, {"soc_percent": 40.0}, now=now)
    allow, reason = overnight_discharge_allowed(
        plan,
        soc=40.0,
        export_mode=True,
        live_export_kw=1.5,
        export_threshold_kw=0.05,
    )
    assert allow is True
    assert "Export" in reason


def test_deep_night_defers_flex_resume():
    cfg = _base_config()
    state = _base_state()
    now = datetime(2026, 9, 1, 1, 0, 0)
    plan = compute_overnight_reserve_plan(cfg, state, {}, now=now)
    device = {"ip": "192.168.1.50", "priority": 5}
    priority_plan = {"by_ip": {"192.168.1.50": {"role": "flex"}}}
    defer, why = should_defer_overnight_resume(plan, device, priority_plan)
    assert defer is True
    assert "Nachtreserve" in why


def test_overnight_resume_floor_evening_at_least_night_hold():
    cfg = _base_config()
    state = _base_state()
    evening = compute_overnight_reserve_plan(cfg, state, {}, now=datetime(2026, 9, 1, 19, 0))
    night = compute_overnight_reserve_plan(cfg, state, {}, now=datetime(2026, 9, 1, 2, 0))
    assert overnight_resume_floor_soc(evening) >= overnight_resume_floor_soc(night)


def test_evening_learning_increases_margin_after_stress():
    state = {
        "steering_learning": {
            "params": {
                "overnight_reserve_margin_pct": 4.0,
                "day_night_shift_bias": -0.35,
                "night_absorption_max_fraction": 0.15,
                "score": 55.0,
                "soc_preempt_margin": 4.0,
                "export_threshold_kw": 0.05,
                "headroom_extra_soc": 1.0,
                "forecast_underestimate_boost": 0.0,
                "top_prio_extra_runtime_h": 1.0,
                "top_prio_hold_below_max": 6.0,
                "switch_risk_extra_runtime_h": 0.0,
                "switch_risk_extra_off_h": 0.0,
                "switch_risk_session_bias": 0.0,
                "peak_spill_boost": 0.0,
                "anchor_conserve_bias": 0.0,
                "peak_export_sensitivity": 1.0,
                "band_max_target_pct": 90.0,
                "band_min_cushion_pct": 2.0,
                "band_weather_guard": 0.35,
                "band_consumption_guard": 0.25,
                "band_horizon_weight": 0.65,
                "soc_prio_margin_pct": 5.0,
                "soc_prio_anchor_stretch": 0.0,
                "soc_prio_flex_cut_bias": 0.0,
                "soc_prio_forecast_trust": 0.85,
            },
            "today": {
                "date": date.today().isoformat(),
                "overnight_soc_stress_ticks": 3,
                "overnight_unexpected_load_ticks": 2,
                "morning_export_kwh": 0.0,
                "night_switch_count": 0,
                "night_switch_wasted": 0,
                "day_min_soc_breach": 0,
                "miss_ticks": 0,
                "export_miss_kw_sum": 0.0,
                "on_limit_blocks": 0,
                "on_limit_miss_ticks": 0,
            },
            "history": [],
        },
        "morning_pv_forecast_kwh": 900,
        "live_pv_kwh_actual": 900,
    }
    cfg = {"min_soc_percent": 30, "max_soc_percent": 95, "storage_kwh": 1100}
    run_evening_steering_learning(state, cfg, "/tmp")
    margin = state["steering_learning"]["params"]["overnight_reserve_margin_pct"]
    assert margin > 4.0
    assert any("Nachtreserve" in a for a in state["steering_learning"]["last_adjustments"])


def test_steering_learning_defaults_include_overnight_margin():
    state = {}
    ensure_steering_learning(state)
    assert "overnight_reserve_margin_pct" in state["steering_learning"]["params"]
    assert state["steering_learning"]["today"]["overnight_soc_stress_ticks"] == 0
