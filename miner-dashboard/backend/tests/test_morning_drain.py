# -*- coding: utf-8 -*-
"""Laufreserve g: Abendstopp, Frühstart und Durchlauf aus einer Ungleichung."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from control_algorithm import morning_drain as md

BASE_KW = 0.8
BASE_DIR = "/tmp/morning-drain-tests"


@pytest.fixture(autouse=True)
def deterministic_history(monkeypatch):
    """Grundlast konstant, keine Wasserkraft – Profil-IO aus den Tests halten."""

    def _base(_base_dir, start, end):
        if end <= start:
            return 0.0
        return BASE_KW * (end - start).total_seconds() / 3600.0

    monkeypatch.setattr(md, "_base_kwh", _base)
    monkeypatch.setattr(md, "_hydro_kwh", lambda *a, **k: 0.0)
    monkeypatch.setattr(md, "_peak_base_kw", lambda *a, **k: 1.0)


def _pv_day(peak_kw: float, first: int = 6, last: int = 19) -> dict:
    """Dreieckskurve mit Scheitel in der Tagesmitte."""
    mid = (first + last) / 2.0
    span = (last - first) / 2.0
    hours = {}
    for h in range(24):
        if first <= h <= last:
            hours[f"h{h:02d}"] = round(
                max(0.0, peak_kw * (1.0 - abs(h - mid) / span)), 3
            )
        else:
            hours[f"h{h:02d}"] = 0.0
    return hours


def _state(today_peak=16.0, tomorrow_peak=16.0):
    return {
        "pv_forecast_today_hours": _pv_day(today_peak),
        "pv_forecast_tomorrow_hours": _pv_day(tomorrow_peak),
        "pv_forecast_day_after_hours": _pv_day(tomorrow_peak),
    }


def _config(storage=41.0, devices=None, min_soc=30.0, morning=33.0, max_soc=95.0):
    return {
        "storage_kwh": storage,
        "min_soc_percent": min_soc,
        "morning_soc_percent": morning,
        "max_soc_percent": max_soc,
        "min_device_runtime_hours": 1.0,
        "devices": devices
        or [
            {"ip": "anchor", "enabled": True, "power_kw": 5.8, "priority": 10},
            {"ip": "mid", "enabled": True, "power_kw": 3.0, "priority": 3},
            {"ip": "small", "enabled": True, "power_kw": 1.61, "priority": 0.5},
        ],
    }


# ------------------------------------------------------------------ Break-even


def test_break_even_is_first_hour_pv_carries_the_load():
    state = _state()
    now = datetime(2026, 6, 15, 2, 0)
    t_be = md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)
    assert t_be is not None
    # PV muss 5.8 + 0.8 = 6.6 kW liefern; Dreieck mit Peak 12 kW erreicht das
    # am Vormittag, nicht in der Dämmerung.
    assert 7 <= t_be.hour <= 11
    assert t_be.date() == now.date()


def test_break_even_none_when_device_exceeds_pv_peak():
    state = _state(today_peak=4.0, tomorrow_peak=4.0)
    now = datetime(2026, 6, 15, 2, 0)
    assert md.next_break_even(state, BASE_DIR, device_kw=20.0, now=now) is None


def test_reference_break_even_falls_back_to_the_anchor():
    """Trägt PV die volle Menge nie, gilt der Break-even des Ankers."""
    cfg = _config()
    state = _state(today_peak=12.0, tomorrow_peak=12.0)
    now = datetime(2026, 6, 15, 21, 0)
    assert md.next_break_even(state, BASE_DIR, device_kw=10.41, now=now) is None
    ref = md.reference_break_even(cfg, state, BASE_DIR, now=now, pv_scale=1.0)
    assert ref is not None
    assert ref == md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)


def test_smaller_device_reaches_break_even_earlier():
    state = _state()
    now = datetime(2026, 6, 15, 2, 0)
    big = md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)
    small = md.next_break_even(state, BASE_DIR, device_kw=1.61, now=now)
    assert small < big


def test_coverage_end_is_evening_break_even():
    state = _state()
    now = datetime(2026, 6, 15, 12, 0)
    end = md.coverage_end(state, BASE_DIR, device_kw=5.8, now=now)
    assert end is not None
    assert 13 <= end.hour <= 19


# ------------------------------------------------------------------ Monotonie


def test_g_rises_monotonically_while_devices_are_off():
    cfg = _config()
    state = _state()
    t_be = datetime(2026, 6, 16, 9, 0)
    soc = 70.0
    previous = None
    for offset in range(0, 8):
        now = t_be - timedelta(hours=8 - offset)
        idle = md._net_draw_kwh(
            state, BASE_DIR, t_be - timedelta(hours=8), now, device_kw=0.0
        )
        soc_at = soc - 100.0 * idle / cfg["storage_kwh"]
        g = md.run_reserve_kwh(
            cfg,
            state,
            BASE_DIR,
            soc_percent=soc_at,
            device_kw=5.8,
            t_be=t_be,
            now=now,
        )["g_kwh"]
        if previous is not None:
            assert g > previous, f"g fiel bei Offset {offset}"
        previous = g


def test_g_stays_flat_while_the_device_runs():
    """Läuft das Gerät, hebt es sich exakt auf – g darf nicht abstürzen."""
    cfg = _config()
    state = _state()
    t_be = datetime(2026, 6, 16, 9, 0)
    start = t_be - timedelta(hours=2)
    soc = 60.0
    values = []
    for minutes in (0, 30, 60, 90):
        now = start + timedelta(minutes=minutes)
        drawn = md._net_draw_kwh(state, BASE_DIR, start, now, device_kw=5.8)
        soc_at = soc - 100.0 * drawn / cfg["storage_kwh"]
        values.append(
            md.run_reserve_kwh(
                cfg,
                state,
                BASE_DIR,
                soc_percent=soc_at,
                device_kw=5.8,
                t_be=t_be,
                now=now,
            )["g_kwh"]
        )
    assert max(values) - min(values) < 0.6
    assert values[-1] >= values[0] - 0.1


# ------------------------------------------------------------------ Abendstopp


def test_evening_g_is_negative_for_the_anchor_on_a_small_battery():
    cfg = _config(storage=41.0)
    state = _state()
    now = datetime(2026, 6, 15, 19, 30)
    t_be = md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)
    assert t_be is not None and t_be.date() > now.date()
    reserve = md.run_reserve_kwh(
        cfg, state, BASE_DIR, soc_percent=75.0, device_kw=5.8, t_be=t_be, now=now
    )
    assert reserve["g_kwh"] < 0
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 75.0}, BASE_DIR, now=now
    )
    assert plan["ok"] is True
    assert plan["allowed_ips"] == []
    assert plan["policy"] == "evening_stop_morning_early"


def test_pv_covering_the_load_keeps_devices_on():
    cfg = _config()
    state = _state()
    now = datetime(2026, 6, 15, 12, 0)
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 80.0}, BASE_DIR, now=now
    )
    assert plan["policy"] == "keep_on"
    assert plan["reason"] == "pv_covers_load"
    assert len(plan["allowed_ips"]) == 3


# ------------------------------------------------------------------ Frühstart


def test_early_start_lands_on_the_floor_at_break_even():
    cfg = _config()
    state = _state()
    now = datetime(2026, 6, 16, 4, 0)
    t_be = md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)
    start = md._first_start(
        cfg,
        state,
        BASE_DIR,
        soc_percent=62.0,
        now=now,
        t_be=t_be,
        device_kw=5.8,
        pv_scale=1.0,
    )
    assert start is not None
    assert now < start < t_be
    # Ab start bis Break-even genau bis auf den Floor entladen
    idle = md._net_draw_kwh(state, BASE_DIR, now, start, device_kw=0.0)
    soc_at_start = 62.0 - 100.0 * idle / cfg["storage_kwh"]
    run = md._net_draw_kwh(state, BASE_DIR, start, t_be, device_kw=5.8)
    soc_at_be = soc_at_start - 100.0 * run / cfg["storage_kwh"]
    assert soc_at_be >= md.floor_soc_percent(cfg) - 0.5


def test_hard_night_blocks_the_early_start_and_holds_the_floor():
    """Nacht teurer als prognostiziert: g bleibt negativ, kein Speicherlauf."""
    cfg = _config()
    state = _state()
    now = datetime(2026, 6, 16, 4, 0)
    t_be = md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)
    reserve = md.run_reserve_kwh(
        cfg, state, BASE_DIR, soc_percent=34.0, device_kw=5.8, t_be=t_be, now=now
    )
    assert reserve["g_kwh"] < 0
    assert (
        md._first_start(
            cfg,
            state,
            BASE_DIR,
            soc_percent=34.0,
            now=now,
            t_be=t_be,
            device_kw=5.8,
            pv_scale=1.0,
        )
        is None
    )
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 34.0}, BASE_DIR, now=now
    )
    assert plan["allowed_ips"] == []
    # Gedeckelt auf den Floor: es gibt kaum noch etwas zu entladen
    assert plan["must_empty_kwh"] < 0.5


def test_no_early_start_below_min_runtime():
    cfg = _config()
    cfg["min_device_runtime_hours"] = 6.0
    state = _state()
    now = datetime(2026, 6, 16, 4, 0)
    t_be = md.next_break_even(state, BASE_DIR, device_kw=5.8, now=now)
    assert (
        md._first_start(
            cfg,
            state,
            BASE_DIR,
            soc_percent=62.0,
            now=now,
            t_be=t_be,
            device_kw=5.8,
            pv_scale=1.0,
        )
        is None
    )


# ------------------------------------------------------- Anlagenunabhängigkeit


def test_large_storage_runs_everything_through_the_night():
    cfg = _config(storage=1000.0)
    state = _state()
    now = datetime(2026, 6, 15, 21, 0)
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 75.0}, BASE_DIR, now=now
    )
    assert plan["policy"] == "keep_on"
    assert len(plan["allowed_ips"]) == 3
    assert plan["must_empty_kwh"] == 0.0


def test_medium_storage_keeps_only_part_of_the_stack():
    """Abstufung statt Alles-oder-Nichts."""
    cfg = _config(storage=200.0)
    state = _state()
    now = datetime(2026, 6, 15, 21, 0)
    t_be = md.reference_break_even(cfg, state, BASE_DIR, now=now, pv_scale=1.0)
    allowed = md.feasible_device_set(
        cfg, state, BASE_DIR, soc_percent=85.0, now=now, t_be=t_be, pv_scale=1.0
    )
    assert 0 < len(allowed["ips"]) < 3
    assert allowed["device_kw"] < 10.41


def test_tiny_storage_with_large_pv_drains_only_down_to_the_floor():
    """5 kWh Speicher an 20 kWp: Drain gekappt, Abregelung wird akzeptiert."""
    cfg = _config(storage=5.0)
    state = _state(today_peak=20.0, tomorrow_peak=20.0)
    now = datetime(2026, 6, 15, 21, 0)
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 95.0}, BASE_DIR, now=now
    )
    floor = md.floor_soc_percent(cfg)
    assert plan["target_soc_percent"] == pytest.approx(floor, abs=0.05)
    assert plan["must_empty_kwh"] == pytest.approx((95.0 - floor) / 100.0 * 5.0, abs=0.1)
    assert plan["accepted_curtail_kwh"] > 0
    # Nur ein Bruchteil des Tagesüberschusses lässt sich überhaupt puffern
    assert plan["must_empty_kwh"] < 0.2 * plan["day_surplus_kwh"]


def test_tiny_storage_early_start_still_lands_above_the_floor():
    cfg = _config(storage=5.0)
    state = _state(today_peak=20.0, tomorrow_peak=20.0)
    now = datetime(2026, 6, 16, 3, 0)
    t_be = md.reference_break_even(cfg, state, BASE_DIR, now=now, pv_scale=0.8)
    start = md._first_start(
        cfg,
        state,
        BASE_DIR,
        soc_percent=95.0,
        now=now,
        t_be=t_be,
        device_kw=5.8,
        pv_scale=0.8,
    )
    if start is None:
        return  # Fenster kürzer als die Mindestlaufzeit – ebenfalls zulässig
    run = md._net_draw_kwh(state, BASE_DIR, start, t_be, device_kw=5.8)
    idle = md._net_draw_kwh(state, BASE_DIR, now, start, device_kw=0.0)
    soc_at_be = 95.0 - 100.0 * (idle + run) / cfg["storage_kwh"]
    assert soc_at_be >= md.floor_soc_percent(cfg) - 0.5


def test_floor_wins_when_storage_is_too_small_for_the_forecast():
    """Viel PV, wenig Abnehmer, kleiner Speicher – Drain endet am Floor."""
    cfg = _config(
        storage=10.0,
        devices=[{"ip": "small", "enabled": True, "power_kw": 1.61, "priority": 10}],
    )
    state = _state(tomorrow_peak=25.0)
    now = datetime(2026, 6, 15, 21, 0)
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 90.0}, BASE_DIR, now=now
    )
    floor = md.floor_soc_percent(cfg)
    assert plan["headroom_soc_percent"] < floor
    assert plan["target_soc_percent"] == pytest.approx(floor, abs=0.05)
    assert plan["accepted_curtail_kwh"] > 0


def test_m_base_has_an_absolute_sockel_on_tiny_batteries(monkeypatch):
    monkeypatch.setattr(md, "_peak_base_kw", lambda *a, **k: 3.0)
    small = md.m_base_kwh(_config(storage=5.0), _state(), BASE_DIR, datetime.now())
    # 2 % von 5 kWh wären 0,1 kWh – der Sockel muss greifen
    assert small == pytest.approx(3.0 * md.REACTION_HOURS)
    big = md.m_base_kwh(_config(storage=1000.0), _state(), BASE_DIR, datetime.now())
    assert big == pytest.approx(20.0)


def test_m_risk_never_negative_with_strong_hydro(monkeypatch):
    monkeypatch.setattr(md, "_hydro_kwh", lambda _b, s, e: 99.0)
    cfg = _config()
    reserve = md.run_reserve_kwh(
        cfg,
        _state(),
        BASE_DIR,
        soc_percent=70.0,
        device_kw=5.8,
        t_be=datetime(2026, 6, 16, 9, 0),
        now=datetime(2026, 6, 16, 4, 0),
    )
    assert reserve["m_risk_kwh"] >= 0


def test_forecast_outage_never_switches_everything_off():
    """Leere PV-Prognose darf das Gate nicht scharf machen."""
    cfg = _config()
    state = {
        "pv_forecast_today_hours": {},
        "pv_forecast_tomorrow_hours": {},
        "pv_forecast_day_after_hours": {},
    }
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 80.0, "pv_kw": 0.0}, BASE_DIR,
        now=datetime(2026, 6, 15, 21, 0),
    )
    assert plan["reason"] == "no_break_even"
    assert plan["gate_active"] is False
    assert md.device_allowed_on_battery({"morning_drain_plan": plan}, "anchor")[0]


def test_gate_stays_off_while_live_pv_carries_the_house():
    """Teilbewölkter Vormittag: PV stützt, Speicher ist nicht die Quelle."""
    cfg = _config()
    state = _state()
    now = datetime(2026, 6, 16, 9, 0)
    cloudy = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 60.0, "pv_kw": 4.0}, BASE_DIR, now=now
    )
    assert cloudy["battery_is_source"] is False
    assert cloudy["gate_active"] is False
    dark = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 60.0, "pv_kw": 0.0}, BASE_DIR, now=now
    )
    assert dark["battery_is_source"] is True


def test_keep_on_never_activates_the_gate():
    cfg = _config(storage=1000.0)
    plan = md.compute_morning_drain_plan(
        cfg, _state(), {"soc_percent": 75.0, "pv_kw": 0.0}, BASE_DIR,
        now=datetime(2026, 6, 15, 21, 0),
    )
    assert plan["policy"] == "keep_on"
    assert plan["gate_active"] is False


def test_no_break_even_yields_no_drain():
    cfg = _config(devices=[{"ip": "x", "enabled": True, "power_kw": 50.0, "priority": 10}])
    state = _state(today_peak=3.0, tomorrow_peak=3.0)
    plan = md.compute_morning_drain_plan(
        cfg, state, {"soc_percent": 80.0}, BASE_DIR, now=datetime(2026, 12, 3, 20, 0)
    )
    assert plan["ok"] is True
    assert plan["reason"] == "no_break_even"
    assert plan["must_empty_kwh"] == 0.0


def test_deadband_scales_with_storage_and_device():
    assert md.deadband_kwh({"storage_kwh": 5.0}, 5.8) == pytest.approx(0.29)
    assert md.deadband_kwh({"storage_kwh": 1000.0}, 5.8) == pytest.approx(10.0)


# ------------------------------------------------------------------ Gate / API


def test_device_gate_blocks_only_devices_outside_the_allowed_set():
    state = {
        "morning_drain_plan": {
            "ok": True,
            "gate_active": True,
            "policy": "evening_stop_morning_early",
            "allowed_ips": ["small"],
            "g_kwh": -12.0,
        }
    }
    assert md.device_allowed_on_battery(state, "small")[0] is True
    allowed, why = md.device_allowed_on_battery(state, "anchor")
    assert allowed is False
    assert "Laufreserve" in why


def test_device_gate_passes_everything_when_gate_is_off():
    state = {
        "morning_drain_plan": {
            "ok": True,
            "gate_active": False,
            "policy": "keep_on",
            "allowed_ips": [],
            "g_kwh": 300.0,
        }
    }
    assert md.device_allowed_on_battery(state, "anchor")[0] is True


# ------------------------------------------------------------------ Neuron


def _learning_state(history_days: int = 40, **params):
    from datetime import date

    from control_algorithm.steering_learning import DEFAULT_PARAMS, _empty_today

    return {
        "steering_learning": {
            "params": {**DEFAULT_PARAMS, **params},
            "today": _empty_today(date.today().isoformat()),
            "history": [{} for _ in range(history_days)],
        }
    }


def test_undershoot_raises_the_night_buffer():
    from control_algorithm.steering_learning import get_steering_params

    state = _learning_state()
    state["steering_learning"]["today"].update({"drain_undershoot_ticks": 4})
    params = get_steering_params(state)
    before = params["drain_night_risk_frac"]
    md.run_morning_drain_evening_learning(state, {}, {}, params)
    assert params["drain_night_risk_frac"] > before
    assert params["drain_base_load_bias"] > 1.0


def test_repeated_no_start_lowers_the_night_buffer():
    from control_algorithm.steering_learning import get_steering_params

    state = _learning_state()
    state["steering_learning"]["today"].update(
        {"drain_no_start_ticks": 5, "drain_undershoot_ticks": 0}
    )
    params = get_steering_params(state)
    before = params["drain_night_risk_frac"]
    md.run_morning_drain_evening_learning(state, {}, {}, params)
    assert params["drain_night_risk_frac"] < before


def test_learned_params_stay_inside_bounds():
    from control_algorithm.steering_learning import PARAM_BOUNDS, get_steering_params

    state = _learning_state(drain_night_risk_frac=0.58)
    state["steering_learning"]["today"].update({"drain_undershoot_ticks": 50})
    params = get_steering_params(state)
    for _ in range(40):
        md.run_morning_drain_evening_learning(state, {}, {}, params)
    lo, hi = PARAM_BOUNDS["drain_night_risk_frac"]
    assert lo <= params["drain_night_risk_frac"] <= hi


def test_alpha_ramp_is_gentler_on_day_one():
    day_one = md._learning_alpha(_learning_state(history_days=0))
    mature = md._learning_alpha(_learning_state(history_days=40))
    assert day_one < mature
    assert day_one == pytest.approx(0.12)
    assert mature == pytest.approx(0.25)
