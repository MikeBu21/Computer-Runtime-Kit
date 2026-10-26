# -*- coding: utf-8 -*-
"""Tests für PV-Sicherheits-Watchdog (Layer 3)."""
from datetime import datetime, timedelta

from control_algorithm.pv_safety_watchdog import check_pv_safety_watchdog
from control_algorithm.slot_profiles import hour_slot_ids


def _hours(scale: float) -> dict:
    out = {}
    for hid in hour_slot_ids():
        h = int(hid.lstrip("h"))
        dist = abs(h - 13)
        out[hid] = round(max(0.0, (6 - dist) * scale), 2)
    return out


def _base_config(**over):
    cfg = {
        "has_pv": True,
        "pv_safety_enabled": True,
        "pv_safety_threshold_percent": 20.0,
        "pv_safety_clear_percent": 12.0,
        "pv_safety_recheck_minutes": 60.0,
        "pv_safety_min_hour": 9,
        "pv_safety_confirm_hours": 2,
        "pv_safety_min_forecast_so_far_kwh": 5.0,
        "devices": [
            {"ip": "192.0.2.10", "enabled": True, "power_kw": 5.5, "priority": 10},
        ],
    }
    cfg.update(over)
    return cfg


def test_triggers_on_triple_confirm_and_pauses():
    fc = _hours(2.0)
    act = {h: v * 0.55 for h, v in fc.items()}  # −45 %
    now = datetime(2026, 7, 26, 11, 30)
    until = now.hour
    actual = sum(v for h, v in act.items() if int(h.lstrip("h")) < until)
    state = {
        "morning_forecast_hours": fc,
        "pv_forecast_today_hours": fc,
        "hour_actual_today": act,
        "live_pv_kwh_actual": actual,
        "pv_forecast_override": {"active": True},
        "pv_safety_last_soc": 50.0,
    }
    live = {
        "pv_kw": 0.5,
        "soc_percent": 48.0,
        "miner_status": {"192.0.2.10": "running"},
    }
    state, actions, msgs = check_pv_safety_watchdog(
        _base_config(), state, live, now=now
    )
    assert state["pv_safety_active"] is True
    assert any(a["action"] == "pause" and a.get("force") for a in actions)
    assert state["pv_safety_next_recheck_at"]
    assert any("Sicherheitsabschaltung" in m for m in msgs)


def test_no_trigger_without_stress():
    fc = _hours(2.0)
    act = {h: v * 0.75 for h, v in fc.items()}  # −25 % day
    now = datetime(2026, 7, 26, 11, 30)
    until = now.hour
    actual = sum(v for h, v in act.items() if int(h.lstrip("h")) < until)
    state = {
        "morning_forecast_hours": fc,
        "hour_actual_today": act,
        "live_pv_kwh_actual": actual,
        "pv_forecast_override": {"active": False},
        "pv_safety_last_soc": 48.0,
    }
    # Live-PV ~ erwarteter Stunde, SOC nicht fallend → kein Stress
    hid = f"h{until:02d}"
    live = {
        "pv_kw": float(fc[hid]) * 0.9,
        "soc_percent": 49.0,
        "miner_status": {"192.0.2.10": "running"},
    }
    state, actions, _ = check_pv_safety_watchdog(_base_config(), state, live, now=now)
    assert state.get("pv_safety_active") is not True
    assert actions == []


def test_recheck_clears_when_recovered():
    fc = _hours(2.0)
    now = datetime(2026, 7, 26, 12, 0)
    # Fast aufgeholt
    act = {h: v * 0.95 for h, v in fc.items()}
    until = now.hour
    actual = sum(v for h, v in act.items() if int(h.lstrip("h")) < until)
    state = {
        "pv_safety_active": True,
        "pv_safety_next_recheck_at": (now - timedelta(minutes=1)).isoformat(),
        "pv_safety_deviation_pct": -30.0,
        "morning_forecast_hours": fc,
        "hour_actual_today": act,
        "live_pv_kwh_actual": actual,
        "pv_forecast_override": {"active": False},
    }
    live = {
        "pv_kw": float(fc[f"h{until:02d}"]),
        "soc_percent": 55.0,
        "miner_status": {"192.0.2.10": "paused"},
    }
    state, actions, msgs = check_pv_safety_watchdog(
        _base_config(), state, live, now=now
    )
    assert state["pv_safety_active"] is False
    assert any("beendet" in m for m in msgs)


def test_recheck_keeps_lock_when_still_bad():
    fc = _hours(2.0)
    now = datetime(2026, 7, 26, 12, 0)
    act = {h: v * 0.50 for h, v in fc.items()}
    until = now.hour
    actual = sum(v for h, v in act.items() if int(h.lstrip("h")) < until)
    state = {
        "pv_safety_active": True,
        "pv_safety_next_recheck_at": (now - timedelta(minutes=1)).isoformat(),
        "morning_forecast_hours": fc,
        "pv_forecast_today_hours": fc,
        "hour_actual_today": act,
        "live_pv_kwh_actual": actual,
        "pv_forecast_override": {"active": True},
        "pv_safety_last_soc": 50.0,
    }
    live = {
        "pv_kw": 0.3,
        "soc_percent": 45.0,
        "miner_status": {"192.0.2.10": "paused"},
    }
    state, _, msgs = check_pv_safety_watchdog(_base_config(), state, live, now=now)
    assert state["pv_safety_active"] is True
    assert any("weiterhin" in m or "Recheck" in m for m in msgs)


def test_pace_uses_current_oms_not_stale_morning():
    """Morgen-OMS 64er-Profil, aktuelle OMS niedrig – Ist passt zu aktuell → keine Sperre."""
    morning = _hours(3.0)  # hoch (≈ Morgen-Freeze)
    current = _hours(1.2)  # aktuelle Wetter-OMS
    now = datetime(2026, 9, 24, 11, 30)
    until = now.hour
    # Ist ≈ aktuelle OMS-Pace (leicht darunter, aber > −20 %)
    act = {h: v * 0.92 for h, v in current.items()}
    actual = sum(v for h, v in act.items() if int(h.lstrip("h")) < until)
    state = {
        "morning_forecast_hours": morning,
        "pv_forecast_today_hours": current,
        "hour_actual_today": act,
        "live_pv_kwh_actual": actual,
        "pv_forecast_override": {"active": False},
        "pv_safety_last_soc": 70.0,
    }
    live = {
        "pv_kw": float(current[f"h{until:02d}"]) * 0.95,
        "soc_percent": 72.0,
        "miner_status": {"192.0.2.10": "running"},
    }
    state, actions, _ = check_pv_safety_watchdog(_base_config(), state, live, now=now)
    assert state.get("pv_safety_active") is not True
    assert actions == []
    sig = state.get("pv_safety_signals") or {}
    # Gegen aktuelle OMS darf die Abweichung nicht −20 % unterschreiten
    assert sig.get("day_deviation_pct") is None or sig["day_deviation_pct"] > -20


def _max_soc_state(now, fc):
    return {
        "pv_safety_active": True,
        "pv_safety_next_recheck_at": (now + timedelta(minutes=30)).isoformat(),
        "pv_safety_deviation_pct": -30.0,
        "morning_forecast_hours": fc,
        "pv_forecast_today_hours": fc,
        "max_soc_override_active": True,
        "fill_schedule": {"max_soc_percent": 95.0, "over_max": True},
        "live_soc": 96.0,
    }


def test_max_soc_suspends_pv_safety_lock_without_clearing():
    """Batterie-Schutz hat Vorrang, aber die Sperre wird nur ruhend gestellt.

    Früher wurde der Latch gelöscht – sobald die Last den SoC wieder unter die
    Schwelle drückte, durfte sofort neu getriggert werden (Flip-Flop).
    """
    fc = _hours(2.0)
    now = datetime(2026, 9, 24, 13, 0)
    live = {
        "pv_kw": 8.0,
        "soc_percent": 96.0,
        "miner_status": {"192.0.2.10": "running"},
    }
    state, actions, msgs = check_pv_safety_watchdog(
        _base_config(), _max_soc_state(now, fc), live, now=now
    )
    assert state["pv_safety_active"] is True
    assert state["pv_safety_suspended"] is True
    assert state["pv_safety_suspended_reason"] == "max_soc_priority"
    assert actions == []
    assert any("Max-SoC" in m for m in msgs)


def test_same_session_resumes_after_max_soc_priority_ends():
    """Nach dem Vorrang läuft dieselbe Sperre weiter – kein Neu-Trigger."""
    fc = _hours(2.0)
    now = datetime(2026, 9, 24, 13, 0)
    live_full = {"pv_kw": 8.0, "soc_percent": 96.0, "miner_status": {}}
    state, _, _ = check_pv_safety_watchdog(
        _base_config(), _max_soc_state(now, fc), live_full, now=now
    )
    triggered_at = state.get("pv_safety_triggered_at")

    # Last hat den Speicher unter die Schwelle gezogen
    state["max_soc_override_active"] = False
    state["fill_schedule"] = {"max_soc_percent": 95.0}
    state["live_soc"] = 80.0
    later = now + timedelta(minutes=10)
    state, _, msgs = check_pv_safety_watchdog(
        _base_config(),
        state,
        {"pv_kw": 1.0, "soc_percent": 80.0, "miner_status": {}},
        now=later,
    )
    assert state["pv_safety_active"] is True
    assert state["pv_safety_suspended"] is False
    # Gleiche Session: Trigger-Zeitpunkt unverändert
    assert state.get("pv_safety_triggered_at") == triggered_at
    assert any("wieder aktiv" in m for m in msgs)


def test_no_retrigger_during_cooldown_after_clear():
    """Nach einem Clear sperrt der Cooldown einen sofortigen Neu-Trigger."""
    fc = _hours(2.0)
    now = datetime(2026, 9, 24, 13, 0)
    state = {
        "pv_safety_cooldown_until": (now + timedelta(minutes=20)).isoformat(),
        "pv_forecast_today_hours": fc,
        "hour_actual_today": {hid: 0.0 for hid in hour_slot_ids()},
        "live_soc": 50.0,
    }
    live = {"pv_kw": 0.0, "soc_percent": 50.0, "miner_status": {}}
    state, actions, _ = check_pv_safety_watchdog(_base_config(), state, live, now=now)
    assert not state.get("pv_safety_active")
    assert state["pv_safety_last_skip_reason"] == "cooldown"
    assert actions == []
