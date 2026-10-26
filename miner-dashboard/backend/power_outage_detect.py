#!/usr/bin/env python3
"""
Stromausfall-Erkennung für Telegram-Alarm.

Strategie (schnell + zuverlässig, Pi bleibt online):
1. Keine frischen Enline-Daten → wahrscheinlich Ausfall oder Totalausfall der Messung.
2. Frische Daten, aber Erzeugung + Netz + Verbrauch ~0 → Anlage ohne Leistung.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# auto_control pollt Enline alle 6 s → 90 s ohne Update = Ausfall
STALE_DATA_SECONDS = 90
ZERO_POWER_KW = 0.5
ZERO_SAMPLES_REQUIRED = 2

_consecutive_zero_samples = 0


@dataclass
class OutageCheckResult:
    is_outage: bool
    reason: str
    data_age_seconds: Optional[float] = None


def _parse_iso_age_seconds(iso_text: str) -> Optional[float]:
    try:
        text = str(iso_text).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - dt).total_seconds())
    except Exception:
        return None


def _file_age_seconds(path: Path) -> Optional[float]:
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
        return max(0.0, datetime.now().timestamp() - mtime)
    except Exception:
        return None


def _read_enline_snapshot(em_dir: Path) -> Tuple[Optional[float], Dict[str, float]]:
    """Alter der Daten (Sekunden) und kW-Werte."""
    path = em_dir / "enline_values.json"
    if not path.is_file():
        return _file_age_seconds(em_dir / "energy_value.txt"), {}

    age: Optional[float] = None
    values: Dict[str, float] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("updated_at"):
            age = _parse_iso_age_seconds(data["updated_at"])
        raw = data.get("values_kw") or {}
        for key, val in raw.items():
            try:
                values[str(key)] = float(val)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass

    if age is None:
        age = _file_age_seconds(path)
    txt_age = _file_age_seconds(em_dir / "energy_value.txt")
    if txt_age is not None:
        age = min(age, txt_age) if age is not None else txt_age
    return age, values


def _kw(values: Dict[str, float], *keys: str) -> float:
    for key in keys:
        if key in values:
            return float(values[key])
    return 0.0


def check_power_outage(base_dir: str) -> OutageCheckResult:
    global _consecutive_zero_samples

    em_dir = Path(base_dir) / "energy_monitor"
    age, values = _read_enline_snapshot(em_dir)

    if age is None:
        return OutageCheckResult(
            is_outage=True,
            reason="Keine Energiedaten vom Monitor (Messung offline).",
            data_age_seconds=None,
        )

    if age > STALE_DATA_SECONDS:
        return OutageCheckResult(
            is_outage=True,
            reason=(
                f"Keine aktuellen Enline-Messwerte seit {int(age)} s "
                f"(Grenze {STALE_DATA_SECONDS} s)."
            ),
            data_age_seconds=age,
        )

    pv = _kw(values, "pv", "erzeugung_photovoltaik_gesamt")
    hydro = _kw(values, "wasserkraft", "erzeugung_wasserkraft")
    gesamt = _kw(values, "erzeugung_gesamt")
    erzeugung = gesamt if gesamt > 0 else (pv + hydro)
    netz = _kw(values, "bezug", "bezug_evu_impuls")
    if netz == 0:
        grid_txt = em_dir / "energy_value.txt"
        if grid_txt.is_file():
            try:
                netz = float(grid_txt.read_text(encoding="utf-8").strip().replace(",", "."))
            except ValueError:
                pass
    verbrauch = _kw(values, "betrieb_verbrauch", "betrieb") + _kw(values, "wohnhaus")

    if (
        erzeugung <= ZERO_POWER_KW
        and abs(netz) <= ZERO_POWER_KW
        and verbrauch <= ZERO_POWER_KW
    ):
        _consecutive_zero_samples += 1
    else:
        _consecutive_zero_samples = 0

    if _consecutive_zero_samples >= ZERO_SAMPLES_REQUIRED:
        return OutageCheckResult(
            is_outage=True,
            reason=(
                "Anlage ohne Leistung: Erzeugung, Netz und Verbrauch nahe 0 kW "
                f"({_consecutive_zero_samples} Messungen)."
            ),
            data_age_seconds=age,
        )

    return OutageCheckResult(
        is_outage=False,
        reason="OK",
        data_age_seconds=age,
    )
