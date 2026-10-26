# -*- coding: utf-8 -*-
"""Lehrneuron SoC-Priorität: Checkpoint-sichere Geräteauswahl."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from control_algorithm.soc_priority_neuron import (
    compute_soc_priority_plan,
    device_allowed_by_neuron,
    is_soc_priority_shadow,
    merge_neuron_into_priority_plan,
    neuron_applies,
    record_soc_priority_tick,
    run_soc_priority_evening_learning,
    run_soc_priority_neuron,
    soc_priority_rollout_status,
)
from control_algorithm.steering_learning import ensure_steering_learning, get_steering_params


def _devices():
    return [
        {"ip": "10.0.0.1", "enabled": True, "power_kw": 5.0, "priority": 10},
        {"ip": "10.0.0.2", "enabled": True, "power_kw": 3.0, "priority": 5},
        {"ip": "10.0.0.3", "enabled": True, "power_kw": 1.0, "priority": 0},
    ]


def _config(**overrides):
    cfg = {
        "storage_kwh": 100.0,
        "min_soc_percent": 35.0,
        "max_soc_percent": 95.0,
        "soc_targets": [{"time": "20:00", "soc_percent": 89}],
        "planning_horizon_days": 2,
        "devices": _devices(),
        "soc_priority_neuron_enabled": True,
        "soc_priority_neuron_shadow_days": 14,
        "soc_priority_neuron_margin_pct": 5.0,
    }
    cfg.update(overrides)
    return cfg


def _sim_ok(miner_kw: float) -> dict:
    return {
        "all_checkpoints_ok": True,
        "max_soc_ok": True,
        "checkpoints": [{"time": "20:00", "target_soc": 89, "projected_soc": 92.0}],
        "min_soc_in_horizon": 40.0,
        "binding_constraint": None,
        "miner_kw": miner_kw,
    }


def _sim_fail_at_8kw(miner_kw: float) -> dict:
    if miner_kw <= 5.0:
        return _sim_ok(miner_kw)
    return {
        "all_checkpoints_ok": False,
        "max_soc_ok": True,
        "checkpoints": [
            {"time": "20:00", "target_soc": 89, "projected_soc": 82.0, "reachable": False}
        ],
        "min_soc_in_horizon": 38.0,
        "binding_constraint": "checkpoint_20:00",
        "miner_kw": miner_kw,
    }


def test_greedy_selects_highest_priority_first():
    config = _config()
    state = {}
    ensure_steering_learning(state)
    live = {"soc_percent": 64.0, "pv_kw": 2.0, "verbrauch_kw": 4.0, "miner_status": {}}

    with patch(
        "control_algorithm.soc_priority_neuron._simulate_load",
        side_effect=lambda *args, **kwargs: _sim_fail_at_8kw(
            kwargs.get("miner_kw", args[4] if len(args) > 4 else 0)
        ),
    ):
        plan = compute_soc_priority_plan(config, state, live, "/tmp")

    assert plan["ok"] is True
    assert "10.0.0.1" in plan["recommended_on_ips"]
    assert "10.0.0.2" not in plan["recommended_on_ips"]
    assert plan["recommended_continuous_kw"] == 5.0


def test_baseline_unreachable_skips_force_pause():
    """Wenn schon 0 kW den Checkpoint verfehlt: kein would_pause / kein Shed."""
    config = _config()
    state = {}
    ensure_steering_learning(state)
    live = {
        "soc_percent": 40.0,
        "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
    }

    def _always_fail(miner_kw: float) -> dict:
        return {
            "all_checkpoints_ok": False,
            "max_soc_ok": True,
            "checkpoints": [
                {
                    "time": "06:00",
                    "target_soc": 30.0,
                    "projected_soc": 0.0,
                    "reachable": False,
                }
            ],
            "min_soc_in_horizon": 0.0,
            "miner_kw": miner_kw,
        }

    with patch(
        "control_algorithm.soc_priority_neuron._simulate_load",
        side_effect=lambda *args, **kwargs: _always_fail(
            kwargs.get("miner_kw", args[4] if len(args) > 4 else 0)
        ),
    ):
        plan = compute_soc_priority_plan(config, state, live, "/tmp")

    assert plan["ok"] is True
    assert plan["baseline_unreachable"] is True
    assert plan["would_pause_ips"] == []
    assert plan["recommended_on_ips"] == []
    state["soc_priority_neuron"] = plan
    state["soc_priority_neuron_rollout"] = {
        "shadow_since": (datetime.now() - timedelta(days=20)).isoformat(),
        "live_since": datetime.now().isoformat(),
        "auto_promoted": True,
    }
    from control_algorithm.soc_priority_neuron import should_pause_for_neuron

    assert (
        should_pause_for_neuron(
            state, config, "10.0.0.1", miner_status=live["miner_status"]
        )
        is False
    )
    ok, why = device_allowed_by_neuron(state, config, "10.0.0.1")
    assert ok is True
    assert "baseline" in why


def test_shadow_days_zero_is_live():
    config = _config(soc_priority_neuron_shadow_days=0)
    state = {
        "soc_priority_neuron_rollout": {
            "shadow_since": datetime.now().isoformat(timespec="seconds"),
            "live_since": None,
            "auto_promoted": False,
        },
        "soc_priority_neuron": {"ok": True, "recommended_on_ips": ["10.0.0.1"]},
    }
    status = soc_priority_rollout_status(config, state)
    assert status["phase"] == "live"
    assert status["shadow"] is False
    assert neuron_applies(config, state) is True


def test_merge_limits_priority_plan():
    priority_plan = {
        "preferred_continuous_kw": 9.0,
        "by_ip": {
            "10.0.0.1": {"prefer_on": True, "planned_hours": 8.0},
            "10.0.0.2": {"prefer_on": True, "planned_hours": 4.0},
        },
        "devices": [
            {"ip": "10.0.0.1", "prefer_on": True, "planned_hours": 8.0},
            {"ip": "10.0.0.2", "prefer_on": True, "planned_hours": 4.0},
        ],
        "prefer_ips": ["10.0.0.1", "10.0.0.2"],
    }
    neuron = {
        "ok": True,
        "recommended_on_ips": ["10.0.0.1"],
        "recommended_continuous_kw": 5.0,
    }
    merged = merge_neuron_into_priority_plan(priority_plan, neuron)
    assert merged["preferred_continuous_kw"] == 5.0
    assert merged["by_ip"]["10.0.0.2"]["prefer_on"] is False
    assert merged["soc_neuron_limited"] is True


def test_shadow_mode_during_rollout():
    config = _config()
    state = {}
    status = soc_priority_rollout_status(config, state)
    assert status["phase"] == "shadow"
    assert status["days_remaining"] == 14
    assert is_soc_priority_shadow(config, state) is True
    assert neuron_applies(config, state) is False
    state["soc_priority_neuron"] = {"ok": True}
    ok, _ = device_allowed_by_neuron(state, config, "10.0.0.2")
    assert ok is True


def test_auto_live_after_shadow_days():
    config = _config(soc_priority_neuron_shadow_days=14)
    state = {
        "soc_priority_neuron_rollout": {
            "shadow_since": (datetime.now() - timedelta(days=15)).isoformat(timespec="seconds"),
            "live_since": None,
            "auto_promoted": False,
        },
        "soc_priority_neuron": {
            "ok": True,
            "recommended_on_ips": ["10.0.0.1"],
            "recommended_continuous_kw": 5.0,
        },
    }
    status = soc_priority_rollout_status(config, state)
    assert status["phase"] == "live"
    assert status["auto_promoted"] is True
    assert is_soc_priority_shadow(config, state) is False
    assert neuron_applies(config, state) is True
    ok, why = device_allowed_by_neuron(state, config, "10.0.0.2")
    assert ok is False
    assert "SoC" in why


def test_evening_learning_increases_margin_on_miss():
    state = {}
    ensure_steering_learning(state)
    state["steering_learning"]["today"]["soc_prio_miss_ticks"] = 4
    state["steering_learning"]["today"]["soc_prio_ticks"] = 10
    params = dict(get_steering_params(state))
    adj = run_soc_priority_evening_learning(state, _config(), {}, params)
    assert adj
    assert params["soc_prio_margin_pct"] > 5.0


def test_run_soc_priority_neuron_stores_state():
    config = _config()
    state = {}
    live = {"soc_percent": 70.0, "miner_status": {}}
    with patch(
        "control_algorithm.soc_priority_neuron.compute_soc_priority_plan",
        return_value={
            "ok": True,
            "recommended_on_ips": ["10.0.0.1"],
            "recommended_continuous_kw": 5.0,
            "binding_checkpoint": "20:00 → 89%",
            "rollout": {"phase": "shadow", "days_remaining": 13, "shadow": True},
            "shadow": True,
        },
    ):
        run_soc_priority_neuron(state, config, live, "/tmp")
    assert state["soc_priority_neuron"]["ok"] is True
    assert state.get("soc_priority_neuron_rollout")
    record_soc_priority_tick(config, state, live)
    assert int(state["steering_learning"]["today"]["soc_prio_ticks"]) >= 1
