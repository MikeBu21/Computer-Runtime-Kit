# -*- coding: utf-8 -*-
"""Börsenpreise im Stil des go-e Controllers (aWATTar-Land wählen, Preis live).

go-e speichert das Land unter API-Key ``awc`` (Austria=0, Germany=1) und den
aktuellen Preis unter ``awcp``. Hier dieselbe Auswahl, Preis in ct/kWh.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BACKEND_DIR / "boersenhandel_settings.json"
TZ = ZoneInfo("Europe/Vienna")

# Wie go-e Controller: aWATTar-Börse je Land.
EXCHANGES: Dict[str, Dict[str, str]] = {
    "awattar_at": {
        "label": "aWATTar Österreich",
        "goe_awc": "0",
        "url": "https://api.awattar.at/v1/marketdata",
    },
    "awattar_de": {
        "label": "aWATTar Deutschland",
        "goe_awc": "1",
        "url": "https://api.awattar.de/v1/marketdata",
    },
}

_cache: Dict[str, Any] = {}
_CACHE_SEC = 60


def default_settings() -> Dict[str, Any]:
    return {
        "exchange": "awattar_at",
        "lower_ct_kwh": 5.0,
        "upper_ct_kwh": 15.0,
    }


def load_settings() -> Dict[str, Any]:
    data = default_settings()
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    if data.get("exchange") not in EXCHANGES:
        data["exchange"] = "awattar_at"
    try:
        data["lower_ct_kwh"] = float(data.get("lower_ct_kwh"))
        data["upper_ct_kwh"] = float(data.get("upper_ct_kwh"))
    except (TypeError, ValueError):
        data["lower_ct_kwh"] = 5.0
        data["upper_ct_kwh"] = 15.0
    if data["upper_ct_kwh"] < data["lower_ct_kwh"]:
        data["upper_ct_kwh"] = data["lower_ct_kwh"]
    return data


def save_settings(exchange: str, lower_ct_kwh: float, upper_ct_kwh: float) -> Dict[str, Any]:
    if exchange not in EXCHANGES:
        raise ValueError("Unbekannte Börse.")
    lower = float(lower_ct_kwh)
    upper = float(upper_ct_kwh)
    if upper < lower:
        raise ValueError("Oberer Schwellwert muss mindestens so hoch sein wie der untere.")
    payload = {
        "exchange": exchange,
        "lower_ct_kwh": round(lower, 3),
        "upper_ct_kwh": round(upper, 3),
    }
    SETTINGS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _fetch_slots(exchange: str) -> List[Dict[str, Any]]:
    now = time.time()
    cached = _cache.get(exchange)
    if cached and now - cached["fetched"] < _CACHE_SEC:
        return cached["slots"]
    spec = EXCHANGES[exchange]
    req = urllib.request.Request(spec["url"], headers={"User-Agent": "piminer-boersenhandel"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    slots: List[Dict[str, Any]] = []
    for row in body.get("data") or []:
        start_ms = int(row["start_timestamp"])
        end_ms = int(row["end_timestamp"])
        # aWATTar: Eur/MWh → ct/kWh (wie go-e awcp in Cent)
        ct = float(row["marketprice"]) / 10.0
        start = datetime.fromtimestamp(start_ms / 1000, TZ)
        slots.append(
            {
                "start": start.isoformat(),
                "end": datetime.fromtimestamp(end_ms / 1000, TZ).isoformat(),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "hour": start.strftime("%H:%M"),
                "day": start.strftime("%Y-%m-%d"),
                "ct_kwh": round(ct, 3),
            }
        )
    slots.sort(key=lambda item: item["start_ms"])
    _cache[exchange] = {"fetched": now, "slots": slots}
    return slots


def charge_action(price_ct: Optional[float], lower: float, upper: float) -> str:
    """Unterer Schwellwert startet Laden, oberer stoppt — dazwischen halten."""
    if price_ct is None:
        return "unbekannt"
    if price_ct < lower:
        return "laden"
    if price_ct > upper:
        return "stopp"
    return "halten"


def price_view(exchange: str, range_mode: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    if exchange not in EXCHANGES:
        exchange = settings["exchange"]
    slots = _fetch_slots(exchange)
    now_ms = int(time.time() * 1000)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    current = next((s for s in slots if s["start_ms"] <= now_ms < s["end_ms"]), None)
    if range_mode == "day":
        shown = [s for s in slots if s["day"] == today]
    else:
        horizon = now_ms + 24 * 3600 * 1000
        shown = [s for s in slots if s["end_ms"] > now_ms and s["start_ms"] < horizon]
        if not shown:
            shown = slots[:24]
    price = current["ct_kwh"] if current else None
    lower = float(settings["lower_ct_kwh"])
    upper = float(settings["upper_ct_kwh"])
    return {
        "exchange": exchange,
        "exchange_label": EXCHANGES[exchange]["label"],
        "goe_awc": EXCHANGES[exchange]["goe_awc"],
        "range": "day" if range_mode == "day" else "24h",
        "unit": "ct/kWh",
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "current_ct_kwh": price,
        "current_hour": current["hour"] if current else None,
        "lower_ct_kwh": lower,
        "upper_ct_kwh": upper,
        "action": charge_action(price, lower, upper),
        "points": [
            {"hour": s["hour"], "day": s["day"], "ct_kwh": s["ct_kwh"]}
            for s in shown
        ],
        "exchanges": [
            {"id": key, "label": spec["label"]} for key, spec in EXCHANGES.items()
        ],
    }


def sparkline(exchange: Optional[str] = None) -> List[float]:
    """Letzte Stunden für die kleine Kurve in der Seitenleiste."""
    settings = load_settings()
    key = exchange if exchange in EXCHANGES else settings["exchange"]
    slots = _fetch_slots(key)
    now_ms = int(time.time() * 1000)
    window_start = now_ms - 8 * 3600 * 1000
    window = [slot for slot in slots if window_start <= slot["start_ms"] <= now_ms]
    if len(window) < 3:
        window = slots[:8]
    return [float(slot["ct_kwh"]) for slot in window]
