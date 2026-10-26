# -*- coding: utf-8 -*-
"""Tests: Survival lastabhängig + Min-SoC Belohnen/Bestrafen."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from control_algorithm.energy_balance import _max_miner_kw_for_horizon
from control_algorithm.steering_learning import (
    record_tick_observation,
    run_evening_steering_learning,
)


def test_survival_keeps_full_budget_when_checkpoints_ok():
    """Schlechtwetter-Label, aber Sim mit voller Last OK → kein 12%-Deckel."""
    config = {
        "has_storage": True,
        "storage_kwh": 1000.0,
        "max_soc_percent": 97.0,
        "min_soc_percent": 28.0,
        "soc_targets": [{"time": "17:00", "soc_percent": 94.0}],
        "planning_horizon_days": 7,
        "devices": [
            {"ip": "1.1.1.1", "enabled": True, "power_kw": 3.0, "priority": 10},
        ],
    }
    state = {
        "pv_yield_today_kwh": 200.0,  # relativ zu riesiger Anlage „bad“, absolut ok
        "pv_yield_tomorrow_kwh": 200.0,
        "pv_forecast_by_date": {},
    }
    live = {"soc_percent": 80.0, "pv_kw": 5.0, "miner_status": {}}
    now = datetime(2026, 7, 26, 12, 0)

    ok_sim = {
        "all_checkpoints_ok": True,
        "max_soc_ok": True,
        "binding_constraint": "none",
        "checkpoints": [],
        "max_soc_in_horizon": 90.0,
        "min_soc_in_horizon": 50.0,
    }

    with patch(
        "control_algorithm.energy_balance.weather_context",
        return_value={"days": [{"steering_mode": "survival_mode"}]},
    ), patch(
        "control_algorithm.energy_balance.simulate_battery_horizon",
        return_value=ok_sim,
    ), patch(
        "control_algorithm.energy_balance.enabled_devices",
        return_value=config["devices"],
    ), patch(
        "control_algorithm.energy_balance._proactive_discharge_kw",
        return_value=None,
    ), patch(
        "control_algorithm.energy_balance._export_avoidance_kw",
        return_value=None,
    ), patch(
        "control_algorithm.energy_balance._near_term_discharge_boost",
        return_value=(None, None, {}),
    ):
        kw, binding, _ = _max_miner_kw_for_horizon(
            config, state, live, "/tmp", now, horizon_days=7
        )

    assert kw >= 2.5  # nahezu volle 3 kW, nicht 0.36
    assert binding in ("survival_surplus_ok", "none")


def test_survival_does_not_force_12pct_when_horizon_already_scarce():
    """Bei knapper Bilanz kommt das Budget aus der Horizont-Suche – Survival
    soll nicht zusätzlich pauschal auf 12 % der Gerätelast drücken wenn unnötig."""
    config = {
        "has_storage": True,
        "storage_kwh": 40.0,
        "max_soc_percent": 95.0,
        "min_soc_percent": 28.0,
        "soc_targets": [{"time": "17:00", "soc_percent": 94.0}],
        "planning_horizon_days": 7,
        "devices": [
            {"ip": "1.1.1.1", "enabled": True, "power_kw": 9.0, "priority": 10},
        ],
    }
    state = {"pv_yield_today_kwh": 10.0, "pv_yield_tomorrow_kwh": 5.0}
    live = {"soc_percent": 40.0, "pv_kw": 0.5, "miner_status": {}}
    now = datetime(2026, 7, 26, 12, 0)

    def _sim(**kwargs):
        miner = float(kwargs.get("miner_kw") or 0)
        ok = miner <= 1.5
        return {
            "all_checkpoints_ok": ok,
            "max_soc_ok": True,
            "binding_constraint": "none" if ok else "checkpoint_fail",
            "checkpoints": [],
            "max_soc_in_horizon": 50.0,
            "min_soc_in_horizon": 30.0,
        }

    with patch(
        "control_algorithm.energy_balance.weather_context",
        return_value={"days": [{"steering_mode": "survival_mode"}]},
    ), patch(
        "control_algorithm.energy_balance.simulate_battery_horizon",
        side_effect=lambda *a, **k: _sim(**k),
    ), patch(
        "control_algorithm.energy_balance.enabled_devices",
        return_value=config["devices"],
    ), patch(
        "control_algorithm.energy_balance._proactive_discharge_kw",
        return_value=None,
    ), patch(
        "control_algorithm.energy_balance._export_avoidance_kw",
        return_value=None,
    ), patch(
        "control_algorithm.energy_balance._near_term_discharge_boost",
        return_value=(None, None, {}),
    ):
        kw, binding, _ = _max_miner_kw_for_horizon(
            config, state, live, "/tmp", now, horizon_days=7
        )

    # Horizont erlaubt ~1.5 kW – nicht pauschal 9*0.12=1.08 erzwingen
    assert 1.0 <= kw <= 1.8
    assert binding in ("survival_surplus_ok", "survival_mode", "none")


def test_min_soc_avoidable_penalizes_neuron():
    state = {
        "steering_learning": {
            "params": {
                "soc_preempt_margin": 5.0,
                "export_threshold_kw": 0.05,
                "headroom_extra_soc": 2.0,
                "forecast_underestimate_boost": 0.2,
                "top_prio_extra_runtime_h": 4.0,
                "top_prio_hold_below_max": 8.0,
                "score": 55.0,
            },
            "today": {
                "date": datetime.now().date().isoformat(),
                "miss_ticks": 0,
                "export_miss_kw_sum": 0.0,
                "high_soc_ticks": 0,
                "good_absorb_ticks": 0,
                "min_soc_breach_ticks": 10,
                "min_soc_breach_while_load_ticks": 8,
                "near_min_soc_ticks": 0,
                "band_ok_ticks": 0,
            },
            "history": [],
        },
        "morning_pv_forecast_kwh": 60.0,
        "live_pv_kwh_actual": 50.0,
    }
    config = {"max_soc_percent": 97.0, "min_soc_percent": 28.0}
    with patch(
        "control_algorithm.steering_learning.analyze_timeseries_export_misses",
        return_value={
            "miss_minutes": 0,
            "miss_export_kwh": 0.0,
            "high_soc_minutes": 0,
            "peak_export_kw": 0.0,
        },
    ), patch(
        "control_algorithm.steering_learning.analyze_timeseries_soc_band",
        return_value={
            "min_breach_minutes": 30,
            "max_over_minutes": 0,
            "band_ok_minutes": 0,
        },
    ):
        run_evening_steering_learning(state, config, "/tmp")
    params = state["steering_learning"]["params"]
    assert params["score"] < 55.0
    assert any("Min-SoC" in a and "vermeidbar" in a for a in state["steering_learning"]["last_adjustments"])


def test_min_soc_unavoidable_keeps_score():
    state = {
        "steering_learning": {
            "params": {
                "soc_preempt_margin": 5.0,
                "export_threshold_kw": 0.05,
                "headroom_extra_soc": 0.0,
                "forecast_underestimate_boost": 0.0,
                "top_prio_extra_runtime_h": 0.0,
                "top_prio_hold_below_max": 5.0,
                "score": 55.0,
            },
            "today": {
                "date": datetime.now().date().isoformat(),
                "miss_ticks": 0,
                "export_miss_kw_sum": 0.0,
                "high_soc_ticks": 0,
                "good_absorb_ticks": 0,
                "min_soc_breach_ticks": 20,
                "min_soc_breach_while_load_ticks": 0,  # Geräte aus
                "near_min_soc_ticks": 0,
                "band_ok_ticks": 0,
            },
            "history": [],
        },
        "morning_pv_forecast_kwh": 40.0,
        "live_pv_kwh_actual": 5.0,  # Schnee / kaum PV
    }
    config = {"max_soc_percent": 97.0, "min_soc_percent": 28.0}
    with patch(
        "control_algorithm.steering_learning.analyze_timeseries_export_misses",
        return_value={
            "miss_minutes": 0,
            "miss_export_kwh": 0.0,
            "high_soc_minutes": 0,
            "peak_export_kw": 0.0,
        },
    ), patch(
        "control_algorithm.steering_learning.analyze_timeseries_soc_band",
        return_value={
            "min_breach_minutes": 40,
            "max_over_minutes": 0,
            "band_ok_minutes": 0,
        },
    ):
        run_evening_steering_learning(state, config, "/tmp")
    assert state["steering_learning"]["params"]["score"] == 55.0
    adjs = state["steering_learning"]["last_adjustments"] or []
    note = state["steering_learning"].get("last_note")
    # Globaler PV-Ausfall-Lernsperrer oder Legacy-Pfad „unvermeidbar“
    assert note == "skipped_pv_outage" or any(
        "unvermeidbar" in a or "PV-Ausfall" in a for a in adjs
    )

def test_record_tick_counts_min_breach_with_load():
    state = {"steering_learning": None}
    config = {
        "max_soc_percent": 97.0,
        "min_soc_percent": 28.0,
        "devices": [{"ip": "1.1.1.1", "enabled": True, "power_kw": 3.0, "priority": 10}],
    }
    live = {
        "soc_percent": 25.0,
        "miner_status": {"1.1.1.1": "running"},
        "netz_kw": 0.0,
    }
    with patch(
        "control_algorithm.weather_survival.live_grid_export_kw", return_value=0.0
    ), patch(
        "control_algorithm.planner.miner_load_kw", return_value=3.0
    ), patch(
        "control_algorithm.planner.enabled_devices",
        return_value=config["devices"],
    ):
        record_tick_observation(config, state, live)
    today = state["steering_learning"]["today"]
    assert today["min_soc_breach_ticks"] >= 1
    assert today["min_soc_breach_while_load_ticks"] >= 1
