#!/usr/bin/env python3
"""Batterie-SOC und Leistung fürs Dashboard, nur aus Home Assistant.

Live-Wert ist der HA-Snapshot über ha_entity_mapping. Die Datei
energy_monitor/daten.json ist nur der Cache, den ha_live_reader daraus schreibt.
~/daten.json und andere Scraper-Dateien werden nicht gelesen.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional


def _parse_soc_value(value: Any, key_hint: str = "") -> Optional[str]:
    if isinstance(value, dict):
        for key in (
            "soc", "SoC", "SOC", "battery_soc", "battery", "batterie",
            "ladestand", "ladezustand", "percent", "percentage", "prozent",
            "value", "wert", "speicher", "akku", "capacity",
        ):
            if key in value and value[key] not in (None, ""):
                found = _parse_soc_value(value[key], key)
                if found is not None:
                    return found
        for key, nested in value.items():
            found = _parse_soc_value(nested, str(key))
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _parse_soc_value(item, key_hint)
            if found is not None:
                return found
    elif value not in (None, ""):
        text = str(value).strip().replace("%", "").replace(",", ".")
        hint = key_hint.lower()
        if any(
            w in hint
            for w in ("soc", "battery", "batterie", "lade", "percent", "prozent", "akku", "speicher")
        ) or re.match(r"^\d+(\.\d+)?$", text):
            try:
                num = float(text)
                if 0 <= num <= 100:
                    return str(int(num) if num == int(num) else round(num, 1))
            except ValueError:
                pass
    return None


def _ha_cache_path() -> Path:
    """Nur die Datei, die ha_live_reader aus dem HA-Snapshot schreibt."""
    return Path(__file__).resolve().parent / "energy_monitor" / "daten.json"


def _battery_data_paths() -> list[Path]:
    return [_ha_cache_path()]


def _load_ha_battery() -> Optional[dict]:
    """SOC und Batterieleistung aus dem Live-Snapshot, mit dem Vorzeichen aus dem Mapping."""
    try:
        from energy_monitor.ha_live_reader import load_snapshot
        from ha_entity_mapping import (
            FIELD_DEFINITIONS,
            compute_field_value,
            load_config,
            normalize_field_entry,
        )

        cfg = load_config()
        snapshot = load_snapshot()
        fields = cfg.get("fields") or {}
    except Exception:
        return None
    out: dict[str, Any] = {}
    for key, field_name in (("soc", "battery_soc"), ("power_kw", "battery_power")):
        entry = normalize_field_entry(fields.get(field_name) or {})
        if not entry.get("enabled") or not entry.get("sensors"):
            continue
        try:
            value = compute_field_value(entry, FIELD_DEFINITIONS[field_name], snapshot)
        except Exception:
            value = None
        if value is not None:
            out[key] = value
    if not out:
        return None
    out["source"] = "home_assistant"
    return out


def _load_ram_battery() -> Optional[dict]:
    try:
        from ram_cache import is_enabled, read_json

        if not is_enabled():
            return None
        base = str(Path(__file__).resolve().parent)
        data = read_json(base, "daten.json", default=None, scope="energy_monitor")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _tag_cache(data: Optional[dict]) -> Optional[dict]:
    if not isinstance(data, dict) or not data:
        return None
    tagged = dict(data)
    tagged["source"] = "ha_cache"
    return tagged


def _load_latest_battery_file() -> Optional[dict]:
    live = _load_ha_battery()
    if live:
        return live
    ram = _tag_cache(_load_ram_battery())
    if ram:
        return ram
    path = _ha_cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _tag_cache(data if isinstance(data, dict) else None)


def load_battery_soc() -> Optional[str]:
    data = _load_latest_battery_file()
    if not data:
        return None
    return _parse_soc_value(data)


def _parse_power_kw(data: dict) -> Optional[float]:
    for key in ("power_kw", "leistung_kw", "strom_kw", "kw"):
        if key not in data or data[key] in (None, ""):
            continue
        try:
            return float(str(data[key]).replace(",", "."))
        except (TypeError, ValueError):
            continue
    return None


def load_battery_power_kw() -> Optional[float]:
    data = _load_latest_battery_file()
    if not data:
        return None
    return _parse_power_kw(data)


def battery_power_display(power_kw: Optional[float]) -> dict[str, Any]:
    """
    Anzeige fürs Dashboard:
    - Laden (API +kW): grüner Pfeil zur Batterie, Wert negativ
    - Entladen (API -kW): roter Pfeil, Wert positiv
    """
    if power_kw is None:
        return {"mode": "idle", "display_kw": None, "label": ""}
    if abs(power_kw) < 0.25:
        return {"mode": "idle", "display_kw": 0.0, "label": ""}
    if power_kw < 0:
        return {
            "mode": "discharging",
            "display_kw": round(abs(power_kw), 2),
            "label": "Entladen",
        }
    return {
        "mode": "charging",
        "display_kw": round(-abs(power_kw), 2),
        "label": "Laden",
    }


def load_battery_data() -> dict[str, Any]:
    data = _load_latest_battery_file()
    soc = _parse_soc_value(data) if data else None
    power_kw = _parse_power_kw(data) if data else None
    display = battery_power_display(power_kw)
    return {
        "soc": soc,
        "power_kw": power_kw,
        "mode": display["mode"],
        "display_kw": display["display_kw"],
        "power_label": display["label"],
        "source": data.get("source") if isinstance(data, dict) else None,
    }
