# -*- coding: utf-8 -*-
"""Status-API darf teure Horizon-Rechnungen nicht im Request-Pfad machen."""
from __future__ import annotations

from unittest.mock import patch

from control_algorithm.service import (
    _autopilot_soc_plan_payload,
    _decision_board_payload,
)


def test_autopilot_uses_tick_cache_without_rebuild():
    cached = {
        "mode": "autonomous",
        "recommendations": [{"recommended_soc_percent": 72.0}],
        "today_recommendation": {"recommended_soc_percent": 72.0},
        "summary": "cached",
    }
    with patch(
        "control_algorithm.weather_survival.build_autopilot_soc_plan"
    ) as mocked:
        out = _autopilot_soc_plan_payload({}, {"autopilot_soc_plan": cached}, {})
        mocked.assert_not_called()
    assert out["summary"] == "cached"
    assert out["recommendations"][0]["recommended_soc_percent"] == 72.0


def test_autopilot_missing_cache_does_not_block():
    from control_algorithm import service as svc

    try:
        with patch("control_algorithm.service.threading.Thread") as thread_cls:
            with patch(
                "control_algorithm.weather_survival.build_autopilot_soc_plan"
            ) as mocked:
                out = _autopilot_soc_plan_payload({}, {}, {})
                mocked.assert_not_called()
            thread_cls.assert_called_once()
        assert out.get("pending") is True
        assert out.get("recommendations") == []
    finally:
        svc._autopilot_warm_started = False


def test_decision_board_always_rebuilds_logic_gate():
    gate = {
        "title": "Logikgattersteuerungsalgorithmus Autopilot",
        "nodes": [{"id": "fresh_gate"}],
        "edges": [],
    }
    state = {
        "decision_board": {
            "devices": [{"ip": "10.0.0.1"}],
            "signals": [],
            "score": {"plan_score": 80},
            "logic_gate": {
                "title": "Logikgattersteuerungsalgorithmus Autopilot",
                "nodes": [{"id": "stale_hub"}],
                "edges": [],
            },
        }
    }
    fresh_devices = [{"ip": "10.0.0.2", "name": "aktuell"}]
    with patch(
        "control_algorithm.decision_board._build_devices",
        return_value=fresh_devices,
    ):
        with patch("control_algorithm.logic_gate.build_logic_gate", return_value=gate) as mocked:
            board = _decision_board_payload({}, state, {})
            mocked.assert_called_once()
    assert board["logic_gate"]["nodes"][0]["id"] == "fresh_gate"
    assert board["devices"] == fresh_devices
