# -*- coding: utf-8 -*-
"""Tag/Nacht-Neuron: Lernrichtung Absorptions-Timing."""
from datetime import date

from control_algorithm.steering_learning import (
    day_night_shift_bias,
    ensure_steering_learning,
    night_absorption_max_fraction,
    run_evening_steering_learning,
)


def test_day_night_params_defaults():
    state = {}
    ensure_steering_learning(state)
    assert night_absorption_max_fraction(state) <= 0.4
    assert day_night_shift_bias(state) <= 0.0


def test_evening_learning_shifts_to_day_after_morning_export_and_night_switches():
    state = {
        "steering_learning": {
            "params": {
                "day_night_shift_bias": 0.0,
                "night_absorption_max_fraction": 0.2,
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
            },
            "today": {
                "date": date.today().isoformat(),
                "morning_export_kwh": 4.5,
                "night_switch_count": 2,
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
    cfg = {"min_soc_percent": 39, "max_soc_percent": 85, "storage_kwh": 1100}
    run_evening_steering_learning(state, cfg, "/tmp")
    params = state["steering_learning"]["params"]
    assert params["night_absorption_max_fraction"] < 0.2
    assert params["day_night_shift_bias"] < 0.0
