#!/usr/bin/env python3
"""Gesamtwerte Erzeugung (PV+Wasser) und Verbrauch (Aktueller Verbrauch) für Kreisdiagramme."""
from __future__ import annotations

from typing import Any, Dict, Mapping


def erzeugung_gesamt_kwh(generation: Mapping[str, Any]) -> float:
    """06 PV + 05 Wasserkraft (kWh)."""
    return round(
        float(generation.get("pv", 0) or 0) + float(generation.get("hydro", 0) or 0),
        2,
    )


def format_totals(
    generation: Mapping[str, Any],
    verbrauch_gesamt_kwh: float,
) -> Dict[str, float]:
    return {
        "erzeugung_gesamt_kwh": erzeugung_gesamt_kwh(generation),
        "verbrauch_gesamt_kwh": round(float(verbrauch_gesamt_kwh or 0), 2),
    }


def merge_verbrauch_totals(stored: Dict[str, Any], today_24h: Dict[str, Any]) -> float:
    """Abgeschlossene Perioden + laufender Tag (keine Doppelzählung im Monat/Jahr)."""
    base = float(stored.get("verbrauch_gesamt_kwh", 0) or 0)
    today = float(today_24h.get("verbrauch_gesamt_kwh", 0) or 0)
    return round(base + today, 2)
