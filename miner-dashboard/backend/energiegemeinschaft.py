# -*- coding: utf-8 -*-
"""Energiegemeinschaft — Struktur mit Platzhalterdaten.

Echte Gemeinschafts-Schnittstellen sind noch offen. Die Auswahl, der
Preisvergleich und der Einspeise-/Verbrauchsstatus sind deshalb klar als
Beispiel markiert und können später gegen Live-Daten getauscht werden.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BACKEND_DIR / "energiegemeinschaft_settings.json"

# Platzhalter-Gemeinschaften. Preise relativ zur Börse, bis eine Quelle festliegt.
COMMUNITIES: Dict[str, Dict[str, Any]] = {
    "eeg_regional": {
        "label": "Regionale EEG (Beispiel)",
        "region": "Platzhalter",
        "discount": 0.82,
    },
    "beg_lokal": {
        "label": "Lokale Bürgerenergiegemeinschaft (Beispiel)",
        "region": "Platzhalter",
        "discount": 0.9,
    },
    "geag": {
        "label": "Gemeinschaftliche Erzeugungsanlage (Beispiel)",
        "region": "Platzhalter",
        "discount": 0.75,
    },
}


def load_settings() -> Dict[str, Any]:
    community = "eeg_regional"
    enabled = False
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if raw.get("community") in COMMUNITIES:
                    community = raw["community"]
                enabled = bool(raw.get("enabled"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"community": community, "enabled": enabled}


def save_settings(community: str, enabled: bool | None = None) -> Dict[str, Any]:
    if community not in COMMUNITIES:
        raise ValueError("Unbekannte Energiegemeinschaft.")
    current = load_settings()
    raw: Dict[str, Any] = {}
    if SETTINGS_FILE.is_file():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            raw = {}
    raw["community"] = community
    raw["enabled"] = current["enabled"] if enabled is None else bool(enabled)
    SETTINGS_FILE.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return {"community": raw["community"], "enabled": raw["enabled"]}


def comparison(exchange_points: List[Dict[str, Any]], community_id: str) -> Dict[str, Any]:
    spec = COMMUNITIES.get(community_id) or COMMUNITIES["eeg_regional"]
    factor = float(spec["discount"])
    points = []
    cheaper = 0
    for row in exchange_points:
        market = float(row["ct_kwh"])
        community = round(market * factor, 3)
        points.append(
            {
                "hour": row.get("hour"),
                "day": row.get("day"),
                "market_ct_kwh": round(market, 3),
                "community_ct_kwh": community,
                "community_cheaper": community < market,
            }
        )
        if community < market:
            cheaper += 1
    return {
        "community": community_id if community_id in COMMUNITIES else "eeg_regional",
        "label": spec["label"],
        "placeholder": True,
        "note": "Gemeinschaftspreise sind Beispielwerte (fester Abstand zur Börse), keine Live-Abrechnung.",
        "status": "unbekannt",
        "status_label": "Zähler folgt — Einspeisung oder Verbrauch noch nicht gemessen",
        "cheaper_hours": cheaper,
        "points": points,
        "communities": [
            {"id": key, "label": item["label"], "region": item["region"]}
            for key, item in COMMUNITIES.items()
        ],
    }
