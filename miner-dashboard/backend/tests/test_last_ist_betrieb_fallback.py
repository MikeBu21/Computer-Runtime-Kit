# -*- coding: utf-8 -*-
from __future__ import annotations

from miner_flow_data import _total_verbrauch_kw
from control_algorithm.consumption_history import (
    _sample_betrieb_kw,
    integrate_base_miner_kwh_by_hour,
)


def test_total_verbrauch_without_hydro():
    # Ohne Wasserkraft darf die Bilanz nicht None werden
    assert _total_verbrauch_kw(5.0, None, 0.1, -0.5) == 5.0 + 0.0 + 0.1 - (-0.5)
    assert _total_verbrauch_kw(5.0, None, 0.1, None) == 5.1
    assert _total_verbrauch_kw(None, 1.0, 0.1) is None


def test_sample_betrieb_falls_back_to_line_verbrauch():
    assert _sample_betrieb_kw({"betrieb": 0, "line_verbrauch": 6.2}) == 6.2
    assert _sample_betrieb_kw({"betrieb": 4.5, "line_verbrauch": 6.2}) == 4.5


def test_integrate_uses_line_verbrauch_when_betrieb_zero():
    samples = [
        {"m": 0, "betrieb": 0, "line_verbrauch": 2.0, "miner_kw": 0},
        {"m": 60, "betrieb": 0, "line_verbrauch": 2.0, "miner_kw": 0},
    ]
    total, miner, base = integrate_base_miner_kwh_by_hour(samples)
    assert total[0] == 2.0
    assert base[0] == 2.0
