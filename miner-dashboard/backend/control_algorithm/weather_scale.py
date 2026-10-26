# -*- coding: utf-8 -*-
"""
Fester Wetter-Maßstab: Wettercode / Bewölkung → relative Sonnenstunden (0–1).
Wird NICHT durch die Abend-Kalibrierung verändert (nur yield_factor).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# WMO weather code → relative sun factor (0 = keine Sonne, 1 = klarer Sommertag)
WMO_SUN_FACTOR: Dict[int, float] = {
    0: 1.0,
    1: 0.95,
    2: 0.85,
    3: 0.7,
    45: 0.35,
    48: 0.3,
    51: 0.25,
    53: 0.2,
    55: 0.15,
    61: 0.1,
    63: 0.08,
    65: 0.05,
    71: 0.12,
    73: 0.1,
    75: 0.08,
    80: 0.15,
    81: 0.12,
    82: 0.08,
    95: 0.1,
    96: 0.08,
    99: 0.05,
}


def wmo_to_sun_factor(code: Optional[int]) -> float:
    if code is None:
        return 0.5
    if code in WMO_SUN_FACTOR:
        return WMO_SUN_FACTOR[code]
    if code in (56, 57, 66, 67, 77, 85, 86):
        return 0.12
    if 1 <= code <= 3:
        return 0.9 - (code - 1) * 0.1
    return 0.4


def cloud_cover_to_sun_factor(cloud_percent: Optional[float]) -> float:
    if cloud_percent is None:
        return 0.5
    try:
        c = max(0.0, min(100.0, float(cloud_percent)))
    except (TypeError, ValueError):
        return 0.5
    return max(0.05, 1.0 - (c / 100.0) * 0.92)


def hour_relative_sun(
    weather_code: Optional[int] = None,
    cloud_cover: Optional[float] = None,
    shortwave_radiation: Optional[float] = None,
    sunshine_duration: Optional[float] = None,
) -> float:
    """
    Kombiniert feste Skala: primär shortwave_radiation wenn vorhanden,
    sonst WMO + Bewölkung.
    """
    if shortwave_radiation is not None:
        try:
            sw = float(shortwave_radiation)
            # ~800 W/m² ≈ volle Sonne am Mittag (Referenz)
            return max(0.0, min(1.0, sw / 800.0))
        except (TypeError, ValueError):
            pass
    if sunshine_duration is not None:
        try:
            # sunshine_duration in Sekunden pro Stunde (Open-Meteo)
            sec = float(sunshine_duration)
            return max(0.0, min(1.0, sec / 3600.0))
        except (TypeError, ValueError):
            pass
    wmo = wmo_to_sun_factor(weather_code)
    cloud = cloud_cover_to_sun_factor(cloud_cover)
    return max(0.05, min(1.0, wmo * 0.55 + cloud * 0.45))


def hourly_sun_factors(hourly: Dict[str, List[Any]]) -> List[float]:
    """Liste relative Sonnenfaktoren pro Stunde aus Open-Meteo hourly block."""
    n = 0
    for key in ("time", "weather_code", "cloud_cover", "shortwave_radiation", "sunshine_duration"):
        if key in hourly and hourly[key]:
            n = max(n, len(hourly[key]))
    factors: List[float] = []
    for i in range(n):
        wc = _at(hourly.get("weather_code"), i)
        cc = _at(hourly.get("cloud_cover"), i)
        sw = _at(hourly.get("shortwave_radiation"), i)
        sd = _at(hourly.get("sunshine_duration"), i)
        factors.append(
            hour_relative_sun(
                weather_code=int(wc) if wc is not None else None,
                cloud_cover=cc,
                shortwave_radiation=sw,
                sunshine_duration=sd,
            )
        )
    return factors


def _at(arr: Optional[List[Any]], idx: int) -> Any:
    if not arr or idx >= len(arr):
        return None
    return arr[idx]
