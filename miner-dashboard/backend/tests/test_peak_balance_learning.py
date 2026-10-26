# -*- coding: utf-8 -*-
"""Lehrneuron Prio-Balance: Spitzen-Export vs. Anker-Laufzeit."""
from datetime import date, datetime
from unittest.mock import patch

from control_algorithm.peak_balance_learning import (
    allocate_ips_with_conserve,
    anchor_conserve_bias,
    peak_export_risk,
    peak_spill_boost,
    record_peak_balance_tick,
    run_peak_balance_evening_learning,
    should_conserve_for_anchor,
)
from control_algorithm.steering_learning import ensure_steering_learning, get_steering_params


def _devices():
    return [
        {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.5, "priority": 10},
        {"ip": "10.0.0.2", "enabled": True, "power_kw": 5.5, "priority": 10},
        {"ip": "10.0.0.3", "enabled": True, "power_kw": 3.2, "priority": 0},
    ]


def _config():
    return {
        "pv_kwp": 50,
        "max_soc_percent": 95,
        "min_soc_percent": 40,
        "devices": _devices(),
    }


def test_peak_export_miss_increases_spill_boost():
    state = {}
    ensure_steering_learning(state)
    config = _config()
    live = {
        "soc_percent": 93,
        "pv_kw": 25,
        "verbrauch_kw": 5,
        "grid_kw": -2.5,
        "miner_status": {
            "10.0.0.1": "running",
            "10.0.0.2": "paused",
            "10.0.0.3": "paused",
        },
    }
    with patch("control_algorithm.peak_balance_learning._is_peak_hour", return_value=True):
        with patch(
            "control_algorithm.weather_survival.live_grid_export_kw",
            return_value=2.5,
        ):
            record_peak_balance_tick(config, state, live)
    assert int(state["steering_learning"]["today"]["peak_export_miss_ticks"]) >= 1
    assert peak_spill_boost(state) > 0.0


def test_overparallel_without_export_increases_anchor_bias():
    state = {
        "steering_learning": {
            "params": {
                "peak_spill_boost": 0.0,
                "anchor_conserve_bias": 0.0,
                "peak_export_sensitivity": 1.0,
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
                "day_night_shift_bias": -0.35,
                "night_absorption_max_fraction": 0.15,
            },
            "today": {
                "date": date.today().isoformat(),
                "peak_pv_ticks": 12,
                "peak_export_miss_ticks": 0,
                "peak_export_miss_kw_sum": 0.0,
                "peak_overparallel_ticks": 6,
                "peak_good_absorb_ticks": 5,
            },
            "history": [],
        }
    }
    config = _config()
    adj = run_peak_balance_evening_learning(state, config, {"miss_export_kwh": 0.0})
    params = get_steering_params(state)
    assert params["anchor_conserve_bias"] > 0.0
    assert params["top_prio_extra_runtime_h"] > 1.0
    assert any("Lehrneuron" in a for a in adj)


def test_allocate_respects_export_over_conserve_bias():
    devices = _devices()
    from control_algorithm.planner import sort_for_resume

    sorted_devs = sort_for_resume(devices)
    # Mit hohem Bias aber Export: alle Geräte dürfen rein (Budget für 3 Geräte)
    chosen_export, _ = allocate_ips_with_conserve(
        15.0,
        sorted_devs,
        anchor_conserve=0.8,
        live_export_kw=1.0,
        export_thr=0.05,
    )
    chosen_conserve, _ = allocate_ips_with_conserve(
        15.0,
        sorted_devs,
        anchor_conserve=0.8,
        live_export_kw=0.0,
        export_thr=0.05,
    )
    assert len(chosen_export) >= len(chosen_conserve)
    assert "10.0.0.3" in chosen_export
    assert "10.0.0.3" not in chosen_conserve or len(chosen_conserve) < len(chosen_export)


def test_should_conserve_false_during_export():
    state = {}
    ensure_steering_learning(state)
    sl = state["steering_learning"]
    sl["params"]["anchor_conserve_bias"] = 0.8
    config = _config()
    live = {"soc_percent": 90, "pv_kw": 20, "export_kw": 1.5, "grid_kw": -1.5}
    assert should_conserve_for_anchor(config, state, live, export_mode=True) is False
    assert should_conserve_for_anchor(config, state, live, export_mode=False) is False


def test_peak_export_risk_at_high_soc_and_pv():
    state = {}
    ensure_steering_learning(state)
    config = _config()
    live = {
        "soc_percent": 92,
        "pv_kw": 30,
        "verbrauch_kw": 8,
        "grid_kw": -0.8,
    }
    with patch("control_algorithm.peak_balance_learning._is_peak_hour", return_value=True):
        with patch(
            "control_algorithm.weather_survival.live_grid_export_kw",
            return_value=0.8,
        ):
            assert peak_export_risk(config, state, live) is True


def test_evening_learning_export_miss_boosts_spill():
    state = {
        "steering_learning": {
            "params": {
                "peak_spill_boost": 0.0,
                "anchor_conserve_bias": 0.3,
                "peak_export_sensitivity": 1.0,
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
                "day_night_shift_bias": -0.35,
                "night_absorption_max_fraction": 0.15,
            },
            "today": {
                "date": date.today().isoformat(),
                "peak_pv_ticks": 20,
                "peak_export_miss_ticks": 5,
                "peak_export_miss_kw_sum": 8.0,
                "peak_overparallel_ticks": 0,
                "peak_good_absorb_ticks": 2,
            },
            "history": [],
        }
    }
    config = _config()
    run_peak_balance_evening_learning(
        state, config, {"miss_export_kwh": 10.0, "peak_export_kw": 3.0}
    )
    assert peak_spill_boost(state) > 0.0
    assert anchor_conserve_bias(state) < 0.3
