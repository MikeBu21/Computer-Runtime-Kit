# -*- coding: utf-8 -*-
"""Energie-Dashboard: Börsentarif, Energiegemeinschaft, algorithmische Planung, Autopilot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BACKEND_DIR / "energie_steuerung.json"

# Ziel für die algorithmische Planung: Akkustand über 24 h bei etwa 65 %.
SOC_TARGET_PERCENT = 65.0


def default_settings() -> Dict[str, Any]:
    return {
        "boersentarif_besitze": False,
        "boersentarif_nutzen": False,
        "gemeinschaft_bin_mitglied": False,
        "gemeinschaft_nutzen": False,
        "algorithmische_planung": False,
        "autopilot": False,
        "soc_target_percent": SOC_TARGET_PERCENT,
    }


def active_pillar_count(data: Dict[str, Any]) -> int:
    """Wie viele der drei Punkte darunter aktiv sind (Nutzen bzw. Planung)."""
    count = 0
    if data.get("boersentarif_nutzen"):
        count += 1
    if data.get("gemeinschaft_nutzen"):
        count += 1
    if data.get("algorithmische_planung"):
        count += 1
    return count


def site_has_pv() -> bool:
    """True, wenn am Standort eine PV-Anlage konfiguriert ist."""
    try:
        from control_algorithm.config import load_config

        config = load_config()
    except Exception:
        return False
    if config.get("has_pv"):
        return True
    try:
        return float(config.get("pv_kwp") or 0) > 0
    except (TypeError, ValueError):
        return False


def _normalize(data: Dict[str, Any], has_pv: bool | None = None) -> Dict[str, Any]:
    if has_pv is None:
        has_pv = site_has_pv()
    # Mit PV bleibt die Energiegemeinschaft aus. Dafür gelten Planung und Autopilot.
    if has_pv:
        data["gemeinschaft_bin_mitglied"] = False
        data["gemeinschaft_nutzen"] = False
    if not data["boersentarif_besitze"]:
        data["boersentarif_nutzen"] = False
    if not data["gemeinschaft_bin_mitglied"]:
        data["gemeinschaft_nutzen"] = False
    # Autopilot ist nur Anzeige; Speichern erlaubt nur bei mind. zwei aktiven Punkten.
    if active_pillar_count(data) < 2:
        data["autopilot"] = False
    return data


def load_settings() -> Dict[str, Any]:
    data = default_settings()
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            for key in data:
                if key == "soc_target_percent":
                    continue
                if key in raw:
                    data[key] = bool(raw[key])
            try:
                target = float(raw.get("soc_target_percent", SOC_TARGET_PERCENT))
                data["soc_target_percent"] = max(10.0, min(95.0, target))
            except (TypeError, ValueError):
                pass
    return _normalize(data)


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    data = load_settings()
    for key in (
        "boersentarif_besitze",
        "boersentarif_nutzen",
        "gemeinschaft_bin_mitglied",
        "gemeinschaft_nutzen",
        "algorithmische_planung",
        "autopilot",
    ):
        if key in updates:
            data[key] = bool(updates[key])
    if "soc_target_percent" in updates:
        try:
            data["soc_target_percent"] = max(10.0, min(95.0, float(updates["soc_target_percent"])))
        except (TypeError, ValueError):
            pass
    data = _normalize(data)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        from bms_seite import apply_community_hold, apply_exchange_hold, apply_planning_hold

        held = apply_community_hold()
        if not held.get("community_hold"):
            held = apply_exchange_hold(held)
        if not held.get("community_hold") and not held.get("exchange_hold"):
            apply_planning_hold(held)
    except Exception:
        pass
    return data


def boersenhandel_aktiv(data: Dict[str, Any] | None = None) -> bool:
    """Oben auf Börsenhandel nur, wenn Besitz und Nutzen beide an sind."""
    settings = load_settings() if data is None else data
    return bool(settings.get("boersentarif_besitze") and settings.get("boersentarif_nutzen"))


def status() -> Dict[str, Any]:
    settings = load_settings()
    return {
        "settings": settings,
        "soc_target_percent": settings["soc_target_percent"],
        "active_pillars": active_pillar_count(settings),
        "autopilot_ready": active_pillar_count(settings) >= 2,
        "has_pv": site_has_pv(),
    }
