# -*- coding: utf-8 -*-
"""PV-Ertrags-Schätzung: Tageszeit-Slots + Wetter."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from control_algorithm.slot_profiles import (
    default_slot_factors,
    forecast_slots,
    normalize_slot_factors,
    normalize_sun_hours,
)


def forecast_pv_kwh(
    pv_kwp: float,
    hourly_forecast: Dict[str, Any],
    yield_factor: float = 0.85,
    target_date: Optional[date] = None,
    now: Optional[datetime] = None,
    slot_factors: Optional[Dict[str, float]] = None,
    sun_hours: Optional[float] = None,
    perfect_day_override: Optional[float] = None,
    anchor_forecast_mode: bool = False,
) -> Tuple[float, Dict[str, Any]]:
    """
    Geschätzter PV-Ertrag in kWh für einen Tag (Summe aller Slots).
    yield_factor skaliert alle Slot-Faktoren gleichmäßig (Abwärtskompatibilität).
    """
    tz_now = now or datetime.now()
    if target_date is None:
        target_date = tz_now.date()

    sf = normalize_slot_factors(slot_factors or default_slot_factors())
    if yield_factor and abs(yield_factor - 1.0) > 0.001:
        sf = {k: v * float(yield_factor) for k, v in sf.items()}

    slots, total = forecast_slots(
        pv_kwp,
        hourly_forecast,
        sf,
        target_date,
        sun_hours,
        perfect_day_override=perfect_day_override,
        anchor_forecast_mode=anchor_forecast_mode,
    )
    return total, {
        "slots": slots,
        "yield_factor": yield_factor,
        "date": target_date.isoformat(),
    }


def forecast_today_and_tomorrow(
    pv_kwp: float,
    forecast_json: Dict[str, Any],
    slot_factors: Dict[str, float],
    now: Optional[datetime] = None,
    sun_hours: Optional[float] = None,
    perfect_day_override: Optional[float] = None,
    anchor_forecast_mode: bool = False,
) -> Tuple[float, float, Dict[str, float], Dict[str, float]]:
    tz_now = now or datetime.now()
    today = tz_now.date()
    tomorrow = today + timedelta(days=1)
    sf = normalize_slot_factors(slot_factors)
    sh = normalize_sun_hours(sun_hours)
    kw = dict(
        perfect_day_override=perfect_day_override,
        anchor_forecast_mode=anchor_forecast_mode,
    )
    today_slots, today_kwh = forecast_slots(pv_kwp, forecast_json, sf, today, sh, **kw)
    tomorrow_slots, tomorrow_kwh = forecast_slots(pv_kwp, forecast_json, sf, tomorrow, sh, **kw)
    return today_kwh, tomorrow_kwh, today_slots, tomorrow_slots
