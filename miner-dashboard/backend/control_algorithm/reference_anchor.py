# -*- coding: utf-8 -*-
"""24h-Anker für Vollsonne-Referenz (kWp × Sonnenstunden) ohne Yield/Slot-Reset."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from control_algorithm.slot_profiles import normalize_sun_hours, perfect_day_kwh

REFERENCE_ANCHOR_HOURS = 24


def pv_reference_fingerprint(config: Dict[str, Any]) -> str:
    """Nur PV-Peak und Sonnenstunden – Änderung löst Referenz-Anker aus."""
    parts = [
        str(config.get("has_pv")),
        str(config.get("pv_kwp")),
        str(config.get("perfect_weather_sun_hours")),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def activate_reference_anchor(state: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Neuer Anhaltspunkt: bei 100 % Schönwetter = kWp × Sonnenstunden (24 h gültig)."""
    pv_kwp = float(config.get("pv_kwp") or 0)
    sun_h = normalize_sun_hours(config.get("perfect_weather_sun_hours"))
    anchor_kwh = round(perfect_day_kwh(pv_kwp, sun_h), 2)
    now = datetime.now()
    state["reference_anchor_kwh"] = anchor_kwh
    state["reference_anchor_pv_kwp"] = pv_kwp
    state["reference_anchor_sun_hours"] = sun_h
    state["reference_anchor_started_at"] = now.isoformat()
    state["reference_anchor_until"] = (now + timedelta(hours=REFERENCE_ANCHOR_HOURS)).isoformat()
    state["pv_reference_fingerprint"] = pv_reference_fingerprint(config)
    return state


def is_reference_anchor_active(state: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    if state.get("reference_anchor_kwh") is None:
        return False
    started = state.get("reference_anchor_started_at")
    if not started:
        return False
    now = now or datetime.now()
    try:
        until = state.get("reference_anchor_until")
        if until:
            end = datetime.fromisoformat(str(until))
        else:
            start = datetime.fromisoformat(str(started))
            end = start + timedelta(hours=REFERENCE_ANCHOR_HOURS)
        return now < end
    except Exception:
        return False


def reference_anchor_hours_remaining(state: Dict[str, Any], now: Optional[datetime] = None) -> float:
    if not is_reference_anchor_active(state, now):
        return 0.0
    now = now or datetime.now()
    try:
        until = datetime.fromisoformat(str(state.get("reference_anchor_until")))
        return max(0.0, (until - now).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def effective_perfect_day_kwh(state: Dict[str, Any], config: Dict[str, Any]) -> float:
    if is_reference_anchor_active(state):
        return float(state.get("reference_anchor_kwh") or 0)
    pv_kwp = float(config.get("pv_kwp") or 0)
    return perfect_day_kwh(pv_kwp, config.get("perfect_weather_sun_hours"))


def anchor_status_message(state: Dict[str, Any], config: Dict[str, Any]) -> Optional[str]:
    if not is_reference_anchor_active(state):
        return None
    kwh = float(state.get("reference_anchor_kwh") or 0)
    kwp = float(state.get("reference_anchor_pv_kwp") or config.get("pv_kwp") or 0)
    sh = normalize_sun_hours(
        state.get("reference_anchor_sun_hours") or config.get("perfect_weather_sun_hours")
    )
    h_left = reference_anchor_hours_remaining(state)
    return (
        f"Referenz-Anker aktiv ({h_left:.1f} h verbleibend): Bei 100 % Schönwetter "
        f"Prognose-Basis = {kwh:.0f} kWh ({kwp:.0f} kWp × {sh:g} h). "
        f"Slot-Faktoren werden in dieser Zeit nicht angepasst."
    )


def maybe_apply_pv_reference_change(state: Dict[str, Any], config: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """
    Bei Änderung von kWp oder Sonnenstunden: Anker setzen (ohne Yield/Slot-Reset).
    Returns (state, anchor_just_activated).
    """
    pv_fp = pv_reference_fingerprint(config)
    stored = state.get("pv_reference_fingerprint")
    activated = False
    if stored is None:
        state["pv_reference_fingerprint"] = pv_fp
    elif stored != pv_fp:
        state = activate_reference_anchor(state, config)
        state["morning_pv_forecast_kwh"] = None
        state["morning_forecast_slots"] = None
        state["morning_forecast_date"] = None
        activated = True
    return state, activated
