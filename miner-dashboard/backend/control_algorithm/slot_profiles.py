# -*- coding: utf-8 -*-
"""Tageszeit-Slots, Live-/Ist-PV aus Zeitreihe und Kreisdiagramm."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.weather_scale import hourly_sun_factors

TIME_SLOTS: List[Dict[str, Any]] = [
    {"id": "morning", "start_hour": 5, "end_hour": 10, "label": "05–10"},
    {"id": "midday", "start_hour": 10, "end_hour": 14, "label": "10–14"},
    {"id": "afternoon", "start_hour": 14, "end_hour": 18, "label": "14–18"},
    {"id": "evening", "start_hour": 18, "end_hour": 22, "label": "18–22"},
]

# Stündliche Slots für feinere Kalibrierung (05:00–22:59)
HOUR_SLOT_START = 5
HOUR_SLOT_END = 23  # exklusiv → Stunden 5 … 22

PERFECT_WEATHER_FULL_LOAD_HOURS = 6
CALIBRATION_TOLERANCE = 0.10
COARSE_ERROR_THRESHOLD = 0.50
SLOT_FACTOR_MIN = 0.35
SLOT_FACTOR_MAX = 1.50


def default_slot_factors() -> Dict[str, float]:
    return {s["id"]: 1.0 for s in TIME_SLOTS}


def hour_slot_ids() -> List[str]:
    return [f"h{h:02d}" for h in range(HOUR_SLOT_START, HOUR_SLOT_END)]


def default_hour_slot_factors() -> Dict[str, float]:
    return {hid: 1.0 for hid in hour_slot_ids()}


def normalize_hour_slot_factors(raw: Optional[Dict[str, Any]]) -> Dict[str, float]:
    base = default_hour_slot_factors()
    if not raw:
        return base
    for hid in hour_slot_ids():
        try:
            base[hid] = max(SLOT_FACTOR_MIN, min(SLOT_FACTOR_MAX, float(raw.get(hid, 1.0))))
        except (TypeError, ValueError):
            pass
    return base


def hour_to_hour_slot_id(hour: int) -> Optional[str]:
    if HOUR_SLOT_START <= int(hour) < HOUR_SLOT_END:
        return f"h{int(hour):02d}"
    return None


def hour_slot_label(slot_id: str) -> str:
    if slot_id.startswith("h") and len(slot_id) == 3:
        try:
            return f"{int(slot_id[1:]):02d}:00"
        except ValueError:
            pass
    return slot_id


def normalize_slot_factors(raw: Optional[Dict[str, Any]]) -> Dict[str, float]:
    base = default_slot_factors()
    if not raw:
        return base
    for s in TIME_SLOTS:
        sid = s["id"]
        try:
            base[sid] = max(SLOT_FACTOR_MIN, min(SLOT_FACTOR_MAX, float(raw.get(sid, 1.0))))
        except (TypeError, ValueError):
            pass
    return base


def normalize_sun_hours(raw: Any) -> float:
    try:
        h = float(raw if raw is not None else PERFECT_WEATHER_FULL_LOAD_HOURS)
    except (TypeError, ValueError):
        h = PERFECT_WEATHER_FULL_LOAD_HOURS
    return max(0.5, min(16.0, h))


def perfect_day_kwh(pv_kwp: float, sun_hours: Optional[float] = None) -> float:
    """Einwandfreies Wetter: kWp × Sonnenstunden Volllast (z. B. 190 × 6 → 1140 kWh)."""
    h = normalize_sun_hours(sun_hours)
    return max(0.0, float(pv_kwp) * h)


def hour_to_slot_id(hour: int) -> Optional[str]:
    for s in TIME_SLOTS:
        if int(s["start_hour"]) <= hour < int(s["end_hour"]):
            return s["id"]
    return None


def weather_weights_by_slot(
    hourly_forecast: Dict[str, Any],
    target_date: date,
) -> Dict[str, float]:
    hourly = hourly_forecast.get("hourly") or {}
    times: List[str] = hourly.get("time") or []
    factors = hourly_sun_factors(hourly)
    weights = default_slot_factors()
    for key in weights:
        weights[key] = 0.0
    date_str = target_date.isoformat()
    for i, t in enumerate(times):
        if not str(t).startswith(date_str):
            continue
        if i >= len(factors):
            break
        try:
            hour = int(str(t)[11:13])
        except (ValueError, IndexError):
            hour = 12
        sid = hour_to_slot_id(hour)
        if sid:
            weights[sid] += float(factors[i])
    if sum(weights.values()) <= 0:
        # Gleichverteilung wenn Wetterdaten fehlen
        n = len(TIME_SLOTS)
        for s in TIME_SLOTS:
            weights[s["id"]] = 1.0 / n
    return weights


def forecast_slots(
    pv_kwp: float,
    hourly_forecast: Dict[str, Any],
    slot_factors: Dict[str, float],
    target_date: date,
    sun_hours: Optional[float] = None,
    perfect_day_override: Optional[float] = None,
    anchor_forecast_mode: bool = False,
) -> Tuple[Dict[str, float], float]:
    """
    Prognose kWh pro Tageszeit-Slot und Summe.
    Basis: kWp × Sonnenstunden Volllast, verteilt nach Wetter-Gewichtung × Slot-Faktoren.
    anchor_forecast_mode: 24h-Anker – neutrale Slot-Faktoren (1.0), feste Referenz-kWh.
    """
    if pv_kwp <= 0 and perfect_day_override is None:
        return {s["id"]: 0.0 for s in TIME_SLOTS}, 0.0

    sf = default_slot_factors() if anchor_forecast_mode else normalize_slot_factors(slot_factors)
    weights = weather_weights_by_slot(hourly_forecast, target_date)
    w_total = sum(weights.values()) or 1.0
    if perfect_day_override is not None:
        perfect = float(perfect_day_override)
    else:
        perfect = perfect_day_kwh(pv_kwp, sun_hours)
    slots: Dict[str, float] = {}
    for s in TIME_SLOTS:
        sid = s["id"]
        share = weights[sid] / w_total
        slots[sid] = round(perfect * share * sf[sid], 2)
    total = round(sum(slots.values()), 2)
    return slots, total


def _load_timeseries(base_dir: str) -> Tuple[List[Dict[str, Any]], str]:
    from control_algorithm.paths import energy_file

    path = energy_file(base_dir, "energy_timeseries_24h.json")
    if not path.is_file():
        return [], ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("samples") or []), str(data.get("date") or "")
    except Exception:
        return [], ""


def _pv_fraction(base_dir: str) -> float:
    """Anteil PV an der Erzeugung (Rest = Wasserkraft o. ä.)."""
    from control_algorithm.paths import energy_file

    path = energy_file(base_dir, "energy_stats_24h.json")
    if not path.is_file():
        return 1.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        gen = data.get("generation_kwh") or {}
        pv = float(gen.get("pv") or 0)
        hydro = float(gen.get("hydro") or 0)
        total = pv + hydro
        if total < 0.5:
            return 1.0
        return pv / total
    except Exception:
        return 1.0


def integrate_generation_kwh_by_slots(
    samples: List[Dict[str, Any]],
    pv_fraction: float = 1.0,
    only_before_minute: Optional[float] = None,
) -> Dict[str, float]:
    """Integriert Erzeugung (kW) aus der Minuten-Zeitreihe → kWh pro Slot."""
    result = {s["id"]: 0.0 for s in TIME_SLOTS}
    if len(samples) < 2:
        return result

    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if only_before_minute is not None and m0 >= only_before_minute:
            break
        if m1 <= m0:
            continue
        m_end = min(m1, only_before_minute) if only_before_minute is not None else m1
        dt_h = (m_end - m0) / 60.0
        if dt_h <= 0:
            continue
        kw0 = float(s0.get("erzeugung") or 0) * pv_fraction
        kw1 = float(s1.get("erzeugung") or 0) * pv_fraction
        avg_kw = (kw0 + kw1) / 2.0
        mid_m = (m0 + m_end) / 2.0
        hour = int(mid_m // 60)
        sid = hour_to_slot_id(hour)
        if sid:
            result[sid] += avg_kw * dt_h

    return {k: round(v, 2) for k, v in result.items()}


def get_actual_pv_kwh_by_slots_today(
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """Ist-PV kWh pro Slot aus Zeitreihe (heute)."""
    now = now or datetime.now()
    samples, ts_date = _load_timeseries(base_dir)
    if ts_date != now.date().isoformat():
        return {s["id"]: 0.0 for s in TIME_SLOTS}
    frac = _pv_fraction(base_dir)
    return integrate_generation_kwh_by_slots(samples, frac)


def integrate_generation_kwh_by_hours(
    samples: List[Dict[str, Any]],
    pv_fraction: float = 1.0,
    only_before_minute: Optional[float] = None,
) -> Dict[str, float]:
    """Integriert Erzeugung (kW) → kWh pro Stunde (05–22)."""
    result = {hid: 0.0 for hid in hour_slot_ids()}
    if len(samples) < 2:
        return result

    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if only_before_minute is not None and m0 >= only_before_minute:
            break
        if m1 <= m0:
            continue
        m_end = min(m1, only_before_minute) if only_before_minute is not None else m1
        dt_h = (m_end - m0) / 60.0
        if dt_h <= 0:
            continue
        kw0 = float(s0.get("erzeugung") or 0) * pv_fraction
        kw1 = float(s1.get("erzeugung") or 0) * pv_fraction
        avg_kw = (kw0 + kw1) / 2.0
        mid_m = (m0 + m_end) / 2.0
        hour = int(mid_m // 60)
        hid = hour_to_hour_slot_id(hour)
        if hid:
            result[hid] += avg_kw * dt_h

    return {k: round(v, 2) for k, v in result.items()}


def get_actual_pv_kwh_by_hours_today(
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """Ist-PV kWh pro Stunde (05–22) aus Zeitreihe."""
    now = now or datetime.now()
    samples, ts_date = _load_timeseries(base_dir)
    if ts_date != now.date().isoformat():
        return {hid: 0.0 for hid in hour_slot_ids()}
    frac = _pv_fraction(base_dir)
    return integrate_generation_kwh_by_hours(samples, frac)


def format_hour_slot_summary(
    predicted: Dict[str, float],
    actual: Optional[Dict[str, float]] = None,
    factors: Optional[Dict[str, float]] = None,
) -> List[str]:
    lines = []
    for hid in hour_slot_ids():
        pred = predicted.get(hid, 0)
        if actual is not None:
            act = actual.get(hid, 0)
            if factors and factors.get(hid, 1.0) != 1.0:
                lines.append(
                    f"{hour_slot_label(hid)}: Ist {act:.1f} / Prognose {pred:.1f} kWh "
                    f"(Faktor {factors.get(hid, 1.0):.2f})"
                )
            elif pred >= 0.1 or act >= 0.1:
                lines.append(f"{hour_slot_label(hid)}: Ist {act:.1f} / Prognose {pred:.1f} kWh")
        elif pred >= 0.1:
            lines.append(f"{hour_slot_label(hid)}: Prognose {pred:.1f} kWh")
    return lines


def slot_label(slot_id: str) -> str:
    for s in TIME_SLOTS:
        if s["id"] == slot_id:
            return str(s["label"])
    return slot_id


def format_slot_summary(
    predicted: Dict[str, float],
    actual: Optional[Dict[str, float]] = None,
) -> List[str]:
    lines = []
    for s in TIME_SLOTS:
        sid = s["id"]
        pred = predicted.get(sid, 0)
        if actual is not None:
            act = actual.get(sid, 0)
            lines.append(f"{s['label']}: Ist {act:.1f} / Prognose {pred:.1f} kWh")
        else:
            lines.append(f"{s['label']}: Prognose {pred:.1f} kWh")
    return lines
