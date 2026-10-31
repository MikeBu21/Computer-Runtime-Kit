# -*- coding: utf-8 -*-
"""Energie-Dashboard: Börsentarif, Energiegemeinschaft, algorithmische Planung."""
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
        "soc_target_percent": SOC_TARGET_PERCENT,
    }


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
    # Nutzen nur, wenn Besitz bzw. Mitgliedschaft aktiv ist.
    if not data["boersentarif_besitze"]:
        data["boersentarif_nutzen"] = False
    if not data["gemeinschaft_bin_mitglied"]:
        data["gemeinschaft_nutzen"] = False
    return data


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    data = load_settings()
    for key in (
        "boersentarif_besitze",
        "boersentarif_nutzen",
        "gemeinschaft_bin_mitglied",
        "gemeinschaft_nutzen",
        "algorithmische_planung",
    ):
        if key in updates:
            data[key] = bool(updates[key])
    if "soc_target_percent" in updates:
        try:
            data["soc_target_percent"] = max(10.0, min(95.0, float(updates["soc_target_percent"])))
        except (TypeError, ValueError):
            pass
    if not data["boersentarif_besitze"]:
        data["boersentarif_nutzen"] = False
    if not data["gemeinschaft_bin_mitglied"]:
        data["gemeinschaft_nutzen"] = False
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def status() -> Dict[str, Any]:
    settings = load_settings()
    return {
        "settings": settings,
        "soc_target_percent": settings["soc_target_percent"],
    }
