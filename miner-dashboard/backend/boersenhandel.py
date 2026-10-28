# -*- coding: utf-8 -*-
"""Börsenpreise im Stil des go-e Controllers (aWATTar-Land wählen, Preis live).

go-e speichert das Land unter API-Key ``awc`` (Austria=0, Germany=1) und den
aktuellen Preis unter ``awcp``. Hier dieselbe Auswahl, Preis in ct/kWh.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BACKEND_DIR / "boersenhandel_settings.json"
TZ = ZoneInfo("Europe/Vienna")

# Börsen wie in der go-e ChaCha-App: aWATTar-Länder plus die europäischen
# Day-Ahead-Märkte, die man dort als Preiszone auswählt.
EXCHANGES: Dict[str, Dict[str, str]] = {
    "awattar_at": {
        "label": "aWATTar Österreich",
        "kind": "awattar",
        "bzn": "AT",
        "url": "https://api.awattar.at/v1/marketdata",
    },
    "awattar_de": {
        "label": "aWATTar Deutschland",
        "kind": "awattar",
        "bzn": "DE-LU",
        "url": "https://api.awattar.de/v1/marketdata",
    },
    "epex_at": {"label": "EPEX / EXAA Österreich", "kind": "energy-charts", "bzn": "AT"},
    "epex_de": {"label": "EPEX Deutschland-Luxemburg", "kind": "energy-charts", "bzn": "DE-LU"},
    "epex_fr": {"label": "EPEX Frankreich", "kind": "energy-charts", "bzn": "FR"},
    "epex_nl": {"label": "EPEX Niederlande", "kind": "energy-charts", "bzn": "NL"},
    "epex_be": {"label": "EPEX Belgien", "kind": "energy-charts", "bzn": "BE"},
    "epex_ch": {"label": "EPEX Schweiz", "kind": "energy-charts", "bzn": "CH"},
    "ote_cz": {"label": "OTE Tschechien", "kind": "energy-charts", "bzn": "CZ"},
    "epex_pl": {"label": "TGE Polen", "kind": "energy-charts", "bzn": "PL"},
    "hupx_hu": {"label": "HUPX Ungarn", "kind": "energy-charts", "bzn": "HU"},
    "nordpool_dk1": {"label": "Nord Pool Dänemark West", "kind": "energy-charts", "bzn": "DK1"},
    "nordpool_dk2": {"label": "Nord Pool Dänemark Ost", "kind": "energy-charts", "bzn": "DK2"},
    "nordpool_no2": {"label": "Nord Pool Norwegen Süd", "kind": "energy-charts", "bzn": "NO2"},
    "nordpool_se3": {"label": "Nord Pool Schweden Mitte", "kind": "energy-charts", "bzn": "SE3"},
    "omie_es": {"label": "OMIE Spanien", "kind": "energy-charts", "bzn": "ES"},
    "gme_it": {"label": "GME Italien Nord", "kind": "energy-charts", "bzn": "IT-North"},
}

MONTH_LABELS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
_YEAR_CACHE_SEC = 6 * 3600

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


def _slot(start_ms: int, end_ms: int, ct: float) -> Dict[str, Any]:
    start = datetime.fromtimestamp(start_ms / 1000, TZ)
    return {
        "start": start.isoformat(),
        "end": datetime.fromtimestamp(end_ms / 1000, TZ).isoformat(),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "hour": start.strftime("%H:%M"),
        "day": start.strftime("%Y-%m-%d"),
        "ct_kwh": round(ct, 3),
    }


def _http_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "piminer-boersenhandel"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError("Unerwartete Börsenantwort.")
    return body


def _fetch_slots(exchange: str) -> List[Dict[str, Any]]:
    now = time.time()
    cached = _cache.get(exchange)
    if cached and now - cached["fetched"] < _CACHE_SEC:
        return cached["slots"]
    spec = EXCHANGES[exchange]
    slots: List[Dict[str, Any]] = []
    if spec["kind"] == "awattar":
        body = _http_json(spec["url"])
        for row in body.get("data") or []:
            start_ms = int(row["start_timestamp"])
            end_ms = int(row["end_timestamp"])
            slots.append(_slot(start_ms, end_ms, float(row["marketprice"]) / 10.0))
    else:
        moment = datetime.now(TZ)
        start = (moment - timedelta(hours=12)).isoformat()
        end = (moment + timedelta(hours=36)).isoformat()
        query = urllib.parse.urlencode({"bzn": spec["bzn"], "start": start, "end": end})
        body = _http_json(f"https://api.energy-charts.info/price?{query}")
        times = body.get("unix_seconds") or []
        prices = body.get("price") or []
        for index, (ts, price) in enumerate(zip(times, prices)):
            if price is None:
                continue
            start_ms = int(ts) * 1000
            if index + 1 < len(times):
                end_ms = int(times[index + 1]) * 1000
            else:
                end_ms = start_ms + 15 * 60 * 1000
            slots.append(_slot(start_ms, end_ms, float(price) / 10.0))
    slots.sort(key=lambda item: item["start_ms"])
    _cache[exchange] = {"fetched": now, "slots": slots}
    return slots


def _monthly_averages(bzn: str, start: datetime, end: datetime) -> Dict[str, float]:
    query = urllib.parse.urlencode({"bzn": bzn, "start": start.isoformat(), "end": end.isoformat()})
    body = _http_json(f"https://api.energy-charts.info/price?{query}")
    buckets: Dict[str, List[float]] = {}
    for ts, price in zip(body.get("unix_seconds") or [], body.get("price") or []):
        if price is None:
            continue
        label = datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m")
        buckets.setdefault(label, []).append(float(price) / 10.0)
    return {
        label: round(sum(values) / len(values), 2)
        for label, values in buckets.items()
        if values
    }


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
        "goe_awc": EXCHANGES[exchange].get("goe_awc", ""),
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


def year_view(exchange: str) -> Dict[str, Any]:
    """Monatsmittel: typisches Jahr (2023–2025) plus die letzten 12 Monate."""
    if exchange not in EXCHANGES:
        exchange = "awattar_at"
    spec = EXCHANGES[exchange]
    bzn = spec["bzn"]
    cache_key = f"year:{bzn}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["fetched"] < _YEAR_CACHE_SEC:
        payload = dict(cached["payload"])
        payload["exchange"] = exchange
        payload["exchange_label"] = spec["label"]
        return payload

    by_month: Dict[int, List[float]] = {month: [] for month in range(1, 13)}
    for year in (2023, 2024, 2025):
        start = datetime(year, 1, 1, tzinfo=TZ)
        end = datetime(year + 1, 1, 1, tzinfo=TZ)
        for label, avg in _monthly_averages(bzn, start, end).items():
            month = int(label.split("-")[1])
            by_month[month].append(avg)
    typical = [
        round(sum(by_month[month]) / len(by_month[month]), 2) if by_month[month] else None
        for month in range(1, 13)
    ]
    summer_vals = [typical[month - 1] for month in (5, 6, 7, 8) if typical[month - 1] is not None]
    winter_vals = [typical[month - 1] for month in (11, 12, 1, 2) if typical[month - 1] is not None]
    summer = round(sum(summer_vals) / len(summer_vals), 2) if summer_vals else None
    winter = round(sum(winter_vals) / len(winter_vals), 2) if winter_vals else None

    now = datetime.now(TZ)
    recent_raw = _monthly_averages(bzn, now - timedelta(days=370), now)
    months_back: List[datetime] = []
    cursor = datetime(now.year, now.month, 1, tzinfo=TZ)
    for _ in range(12):
        months_back.append(cursor)
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1, tzinfo=TZ)
    recent = []
    for moment in reversed(months_back):
        key = moment.strftime("%Y-%m")
        if key in recent_raw:
            recent.append(
                {
                    "label": f"{MONTH_LABELS[moment.month - 1]} {moment.year % 100:02d}",
                    "ct_kwh": recent_raw[key],
                }
            )

    note = "Monatsmittel Day-Ahead in ct/kWh, gleiche Börsenbasis wie aWATTar."
    if summer is not None and winter is not None:
        note += f" Typisches Jahr 2023–2025: Sommer (Mai–Aug) {summer:.1f} ct, Winter (Nov–Feb) {winter:.1f} ct."
    payload = {
        "exchange": exchange,
        "exchange_label": spec["label"],
        "unit": "ct/kWh",
        "months": MONTH_LABELS,
        "typical_ct": typical,
        "recent": recent,
        "summer_ct": summer,
        "winter_ct": winter,
        "note": note,
    }
    _cache[cache_key] = {"fetched": time.time(), "payload": payload}
    return payload
