# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from control_algorithm.decision_board import build_decision_board


def _base_cfg():
    return {
        "min_soc_percent": 28,
        "max_soc_percent": 97,
        "storage_kwh": 41,
        "risk_factor_percent": 100,
        "devices": [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
            {"ip": "192.168.1.232", "enabled": True, "power_kw": 3.5, "priority": 2},
        ],
    }


def test_layers_intervened_on_override_and_anomaly():
    cfg = _base_cfg()
    state = {
        "pv_forecast_override": {
            "active": True,
            "pace_deviation_pct": -17.0,
            "effective_kwh": 53.0,
            "reason": "pace",
        },
        "anomaly_active": True,
        "anomaly_excess_pct": 32.0,
        "balance_projection": {
            "target_soc": 50,
            "projected_soc": 40,
            "miner_budget_kw": 1.2,
            "binding_constraint": "none",
            "risk_factor_percent": 100,
        },
        "planned_actions": [],
        "late_start_plan": {"defer": False},
    }
    live = {"soc_percent": 55, "pv_kw": 0.2, "grid_kw": 0.1, "miner_status": {}}
    board = build_decision_board(cfg, state, live, now=datetime(2026, 7, 27, 22, 0))
    keys = {L["key"]: L for L in board["layers"]}
    assert keys["prod_pattern"]["status"] == "intervened"
    assert keys["prod_pattern"]["value"] == -17.0
    assert keys["cons_anomaly"]["status"] == "intervened"
    assert board["intervened_count"] >= 2


def test_device_planned_on_from_late_start():
    cfg = _base_cfg()
    latest = "2026-07-28T04:15:00"
    state = {
        "pv_forecast_override": {"active": False},
        "anomaly_active": False,
        "balance_projection": {
            "target_soc": 50,
            "projected_soc": 88,
            "miner_budget_kw": 5.5,
            "binding_constraint": "proactive_discharge",
        },
        "planned_actions": [],
        "late_start_plan": {
            "defer": True,
            "urgent": False,
            "latest_start": latest,
            "deadline": "2026-07-28T06:00:00",
            "excess_kwh": 12.0,
            "hours_needed": 2.2,
            "miner_kw": 5.5,
            "summary": "warten bis 04:15",
        },
        "priority_runtime_plan": {
            "devices": [
                {"ip": "192.0.2.10", "planned_hours": 2.2},
            ]
        },
        "device_runtime": {},
    }
    live = {
        "soc_percent": 90,
        "pv_kw": 0.0,
        "grid_kw": 0.2,
        "miner_status": {"192.0.2.10": "paused", "192.168.1.232": "paused"},
    }
    board = build_decision_board(cfg, state, live, now=datetime(2026, 7, 27, 22, 0))
    by_ip = {d["ip"]: d for d in board["devices"]}
    # Nur Anker (höchste Prio) bekommt Spätstart – nicht alle parallel
    assert by_ip["192.0.2.10"]["planned_on"] == latest
    assert by_ip["192.0.2.10"]["planned_on_display"]
    assert by_ip["192.0.2.10"]["wait_note"] == "spätstart"
    assert by_ip["192.0.2.10"]["planned_off"] is not None
    assert by_ip["192.168.1.232"]["planned_on"] is None
    assert "höhere Prio" in (by_ip["192.168.1.232"]["wait_note"] or "")


def test_export_signal_mehr_verbrauch():
    cfg = _base_cfg()
    state = {
        "pv_forecast_override": {"active": False},
        "anomaly_active": False,
        "balance_projection": {
            "target_soc": 60,
            "projected_soc": 70,
            "miner_budget_kw": 5.5,
            "binding_constraint": "export_avoidance",
        },
        "planned_actions": [
            {"ip": "192.0.2.10", "action": "resume", "reason": "export"},
        ],
        "late_start_plan": {"defer": False},
    }
    live = {
        "soc_percent": 80,
        "pv_kw": 4.0,
        "grid_kw": -1.5,
        "miner_status": {"192.0.2.10": "paused"},
    }
    board = build_decision_board(cfg, state, live, now=datetime(2026, 7, 27, 12, 0))
    sigs = {s["id"]: s for s in board["signals"]}
    assert sigs["algo2"]["direction"] == "mehr_verbrauch"
    assert sigs["algo2"]["active"] is True
    assert sigs["algo2"]["numbers"]["export_kw"] == 1.5
    assert sigs["algo3"]["numbers"]["resume_count"] == 1
    hydro = next(d for d in board["devices"] if d["ip"] == "192.0.2.10")
    assert hydro["planned_on"] is not None
    assert hydro["planned_action"] == "Einschalten"


def test_score_present():
    cfg = _base_cfg()
    state = {
        "balance_projection": {"target_soc": 50, "projected_soc": 60, "miner_budget_kw": 1},
        "pv_forecast_override": {"active": False, "pace_deviation_pct": 5},
        "planned_actions": [],
        "late_start_plan": {},
    }
    board = build_decision_board(cfg, state, {"soc_percent": 55, "miner_status": {}})
    assert board["score"]["plan_score"] is not None
    assert board["score"]["soc_gap_pct"] == 10.0
