# -*- coding: utf-8 -*-
"""Tests für PV-Muster-Schätzung (ähnliche Tage / Tagespace)."""
from datetime import datetime

from control_algorithm.pv_pattern_estimate import (
    estimate_today_pv_from_patterns,
    pv_estimate_trace_payload,
    record_pv_estimate_trace,
)
from control_algorithm.slot_profiles import hour_slot_ids


def _hours(scale: float) -> dict:
    """Einfache Glockenkurve um 13 Uhr."""
    out = {}
    for hid in hour_slot_ids():
        h = int(hid.lstrip("h"))
        dist = abs(h - 13)
        out[hid] = round(max(0.0, (6 - dist) * scale), 2)
    return out


def test_pace_detects_underperformance():
    fc = _hours(20)  # ~ sum ~ 420* something
    # Scale forecast total
    fc_total = sum(fc.values())
    # Actual only ~80% through hour 14
    act = {h: v * 0.8 for h, v in fc.items()}
    history = [
        {
            "date": "2026-07-10",
            "predicted": fc_total,
            "actual": fc_total * 0.82,
            "predicted_hours": fc,
            "actual_hours": {h: v * 0.82 for h, v in fc.items()},
        },
        {
            "date": "2026-07-11",
            "predicted": fc_total * 0.95,
            "actual": fc_total * 0.78,
            "predicted_hours": fc,
            "actual_hours": {h: v * 0.78 for h, v in fc.items()},
        },
    ]
    est = estimate_today_pv_from_patterns(
        forecast_total=fc_total,
        forecast_hours=fc,
        actual_hours=act,
        actual_total=sum(v for h, v in act.items() if int(h.lstrip("h")) < 14),
        yield_history=history,
        now=datetime(2026, 7, 19, 14, 30),
    )
    assert est["estimated_kwh"] is not None
    assert est["estimated_kwh"] < fc_total
    assert est["pace_ratio"] is not None
    assert est["pace_ratio"] < 0.95
    assert est["delta_vs_oms_pct"] < -5


def test_estimate_trace_keeps_24h_and_refreshes_recent_point():
    state = {
        "pv_yield_today_kwh": 100.0,
        "pv_pattern_estimate": {"estimated_kwh": 90.0},
    }
    t0 = datetime(2026, 10, 15, 10, 0, 0)
    record_pv_estimate_trace(state, now=t0)
    state["pv_yield_today_kwh"] = 110.4
    record_pv_estimate_trace(state, now=t0.replace(minute=2))
    assert len(state["pv_estimate_trace"]) == 1
    assert state["pv_estimate_trace"][0]["t"] == "2026-10-15T10:00:00"
    assert state["pv_estimate_trace"][0]["oms"] == 110.4
    assert state["pv_estimate_trace"][0]["pattern"] == 90.0

    record_pv_estimate_trace(state, now=t0.replace(minute=6))
    assert len(state["pv_estimate_trace"]) == 2

    state["pv_estimate_trace"].insert(
        0, {"t": "2026-10-14T09:00:00", "oms": 1, "pattern": 1}
    )
    record_pv_estimate_trace(state, now=datetime(2026, 10, 15, 10, 20, 0))
    assert all(s["t"] >= "2026-10-14T10:20:00" for s in state["pv_estimate_trace"])


def test_estimate_trace_payload_ends_at_now():
    state = {
        "pv_yield_today_kwh": 80.0,
        "pv_pattern_estimate": {"estimated_kwh": 70.0},
        "pv_estimate_trace": [
            {"t": "2026-10-15T08:00:00", "oms": 75.0, "pattern": 72.0}
        ],
    }
    series = pv_estimate_trace_payload(
        state, now=datetime(2026, 10, 15, 10, 0, 0)
    )
    assert len(series) == 2
    assert series[-1]["t"] == "2026-10-15T10:00:00"
    assert series[-1]["oms"] == 80.0
    assert series[-1]["pattern"] == 70.0


def test_empty_without_data():
    est = estimate_today_pv_from_patterns(
        forecast_total=50,
        forecast_hours={},
        actual_hours={},
        actual_total=0,
        yield_history=[],
        now=datetime(2026, 7, 19, 8, 0),
    )
    assert est["estimated_kwh"] is None
