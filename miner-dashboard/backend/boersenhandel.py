# -*- coding: utf-8 -*-
"""Börsenpreise im Stil des go-e Controllers (aWATTar-Land wählen, Preis live).

go-e speichert das Land unter API-Key ``awc`` (Austria=0, Germany=1) und den
aktuellen Preis unter ``awcp``. Hier dieselbe Auswahl, Preis in ct/kWh.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
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
    # EAG Österreich: Anbieter aus der go-e-App, die den Day-Ahead Österreich durchreichen.
    # Aufschlag nur, wo er öffentlich ausgewiesen ist. Sonst die reine Börse.
    "eag_smartenergy": {
        "label": "EAG · smartENERGY smartCONTROL (+1,44 ct)",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
        "markup_ct": "1.44",
    },
    "eag_steiermark": {
        "label": "EAG · Energie Steiermark (EPEX AT)",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
    },
    "eag_verbund": {
        "label": "EAG · VERBUND V-Strom Spot",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
    },
    "eag_oekostrom": {
        "label": "EAG · oekostrom Spot+",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
    },
    "eag_salzburg": {
        "label": "EAG · Salzburg AG FlexSpot",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
    },
    "eag_vkw": {
        "label": "EAG · VKW Strom Dynamisch (+1,2 ct)",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
        "markup_ct": "1.2",
    },
    "eag_energieag": {
        "label": "EAG · Energie AG",
        "group": "EAG Österreich",
        "kind": "energy-charts",
        "bzn": "AT",
    },
}

MONTH_LABELS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
_YEAR_CACHE_SEC = 6 * 3600

_cache: Dict[str, Any] = {}
_CACHE_SEC = 60
_YEAR_DISK = Path("/dev/shm/minersteuerung/boersen_year_cache.json")
_net_lock = threading.Lock()
_last_energy_charts = 0.0


def _markup(spec: Dict[str, str]) -> float:
    try:
        return float(spec.get("markup_ct") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ordered_thresholds(buy: float, stop: float, sell: float) -> tuple:
    buy_v = round(float(buy), 3)
    stop_v = round(max(float(stop), buy_v), 3)
    sell_v = round(max(float(sell), stop_v), 3)
    return buy_v, stop_v, sell_v


def default_settings() -> Dict[str, Any]:
    return {
        "exchange": "awattar_at",
        "buy_ct_kwh": 5.0,
        "stop_ct_kwh": 15.0,
        "sell_ct_kwh": 25.0,
        "lower_ct_kwh": 5.0,
        "upper_ct_kwh": 15.0,
        "apply_bms": False,
    }


def load_settings() -> Dict[str, Any]:
    data = default_settings()
    raw: Dict[str, Any] = {}
    if SETTINGS_FILE.is_file():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
                data.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    if data.get("exchange") not in EXCHANGES:
        data["exchange"] = "awattar_at"
    try:
        buy = float(data["buy_ct_kwh"] if "buy_ct_kwh" in raw else data.get("lower_ct_kwh"))
        stop = float(data["stop_ct_kwh"] if "stop_ct_kwh" in raw else data.get("upper_ct_kwh"))
        sell = float(data.get("sell_ct_kwh", 25.0))
    except (TypeError, ValueError):
        buy, stop, sell = 5.0, 15.0, 25.0
    buy, stop, sell = _ordered_thresholds(buy, stop, sell)
    data["buy_ct_kwh"] = buy
    data["stop_ct_kwh"] = stop
    data["sell_ct_kwh"] = sell
    data["lower_ct_kwh"] = buy
    data["upper_ct_kwh"] = stop
    data["apply_bms"] = bool(data.get("apply_bms"))
    return data


def save_settings(
    exchange: str,
    buy_ct_kwh: float,
    stop_ct_kwh: float,
    sell_ct_kwh: float,
    apply_bms: bool = False,
) -> Dict[str, Any]:
    if exchange not in EXCHANGES:
        raise ValueError("Unbekannte Börse.")
    buy, stop, sell = _ordered_thresholds(buy_ct_kwh, stop_ct_kwh, sell_ct_kwh)
    payload = {
        "exchange": exchange,
        "buy_ct_kwh": buy,
        "stop_ct_kwh": stop,
        "sell_ct_kwh": sell,
        "lower_ct_kwh": buy,
        "upper_ct_kwh": stop,
        "apply_bms": bool(apply_bms),
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
    global _last_energy_charts
    energy = "energy-charts.info" in url
    delay = 2.0
    last_error = "Börse nicht erreichbar."
    for attempt in range(4):
        if energy:
            with _net_lock:
                wait = 1.5 - (time.time() - _last_energy_charts)
                if wait > 0:
                    time.sleep(wait)
                _last_energy_charts = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "piminer-boersenhandel", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not isinstance(body, dict):
                raise RuntimeError("Unerwartete Börsenantwort.")
            return body
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code == 429 and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"Börse antwortet mit {last_error}.") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc) or last_error
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error)


def _fetch_slots(exchange: str) -> List[Dict[str, Any]]:
    now = time.time()
    spec = EXCHANGES[exchange]
    base_key = f"slots:{spec['kind']}:{spec.get('url') or spec['bzn']}"
    cached = _cache.get(base_key)
    if not (cached and now - cached["fetched"] < _CACHE_SEC):
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
        _cache[base_key] = {"fetched": now, "slots": slots}
        cached = _cache[base_key]
    extra = _markup(spec)
    if not extra:
        return cached["slots"]
    shifted = []
    for slot in cached["slots"]:
        copy = dict(slot)
        copy["ct_kwh"] = round(float(slot["ct_kwh"]) + extra, 3)
        shifted.append(copy)
    return shifted


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


def charge_action(price_ct: Optional[float], buy: float, stop: float, sell: float) -> str:
    """Unter der Kauflinie kaufen, über der Stopplinie nicht mehr, über der Verkaufslinie verkaufen."""
    if price_ct is None:
        return "unbekannt"
    if price_ct >= sell:
        return "verkaufen"
    if price_ct > stop:
        return "nicht_kaufen"
    if price_ct <= buy:
        return "kaufen"
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
    buy = float(settings["buy_ct_kwh"])
    stop = float(settings["stop_ct_kwh"])
    sell = float(settings["sell_ct_kwh"])
    return {
        "exchange": exchange,
        "exchange_label": EXCHANGES[exchange]["label"],
        "goe_awc": EXCHANGES[exchange].get("goe_awc", ""),
        "range": "day" if range_mode == "day" else "24h",
        "unit": "ct/kWh",
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "current_ct_kwh": price,
        "current_hour": current["hour"] if current else None,
        "buy_ct_kwh": buy,
        "stop_ct_kwh": stop,
        "sell_ct_kwh": sell,
        "lower_ct_kwh": buy,
        "upper_ct_kwh": stop,
        "action": charge_action(price, buy, stop, sell),
        "points": [
            {"hour": s["hour"], "day": s["day"], "ct_kwh": s["ct_kwh"]}
            for s in shown
        ],
        "exchanges": [
            {"id": key, "label": spec["label"], "group": spec.get("group") or "Börsen"}
            for key, spec in EXCHANGES.items()
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


def _read_year_disk() -> Dict[str, Any]:
    try:
        raw = json.loads(_YEAR_DISK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_year_disk(store: Dict[str, Any]) -> None:
    try:
        _YEAR_DISK.parent.mkdir(parents=True, exist_ok=True)
        _YEAR_DISK.write_text(json.dumps(store), encoding="utf-8")
    except OSError:
        pass


def _year_base(bzn: str) -> Dict[str, Any]:
    """Ein Abruf für 2023 bis heute, danach sechs Stunden aus dem Speicher."""
    cache_key = f"year:{bzn}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached["fetched"] < _YEAR_CACHE_SEC:
        return cached["payload"]
    disk = _read_year_disk().get(cache_key)
    if isinstance(disk, dict) and now - float(disk.get("fetched") or 0) < _YEAR_CACHE_SEC:
        _cache[cache_key] = disk
        return disk["payload"]

    try:
        monthly = _monthly_averages(bzn, datetime(2023, 1, 1, tzinfo=TZ), datetime.now(TZ))
    except RuntimeError:
        if isinstance(disk, dict) and isinstance(disk.get("payload"), dict):
            _cache[cache_key] = disk
            return disk["payload"]
        if cached:
            return cached["payload"]
        raise

    by_month: Dict[int, List[float]] = {month: [] for month in range(1, 13)}
    for label, avg in monthly.items():
        year_text, month_text = label.split("-")
        if int(year_text) in (2023, 2024, 2025):
            by_month[int(month_text)].append(avg)
    typical = [
        round(sum(by_month[month]) / len(by_month[month]), 2) if by_month[month] else None
        for month in range(1, 13)
    ]
    months_back: List[datetime] = []
    cursor = datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(12):
        months_back.append(cursor)
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    recent = []
    for moment in reversed(months_back):
        key = moment.strftime("%Y-%m")
        if key in monthly:
            recent.append(
                {
                    "label": f"{MONTH_LABELS[moment.month - 1]} {moment.year % 100:02d}",
                    "ct_kwh": monthly[key],
                }
            )
    payload = {"typical_ct": typical, "recent": recent}
    packed = {"fetched": now, "payload": payload}
    _cache[cache_key] = packed
    store = _read_year_disk()
    store[cache_key] = packed
    _write_year_disk(store)
    return payload


def year_view(exchange: str) -> Dict[str, Any]:
    """Monatsmittel: typisches Jahr (2023–2025). Ein Netzabruf pro Börsenzone."""
    if exchange not in EXCHANGES:
        exchange = "awattar_at"
    spec = EXCHANGES[exchange]
    base = _year_base(spec["bzn"])
    extra = _markup(spec)

    def shift(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(float(value) + extra, 2)

    typical = [shift(value) for value in base.get("typical_ct") or []]
    summer_vals = [typical[month - 1] for month in (5, 6, 7, 8) if month - 1 < len(typical) and typical[month - 1] is not None]
    winter_vals = [typical[month - 1] for month in (11, 12, 1, 2) if month - 1 < len(typical) and typical[month - 1] is not None]
    summer = round(sum(summer_vals) / len(summer_vals), 2) if summer_vals else None
    winter = round(sum(winter_vals) / len(winter_vals), 2) if winter_vals else None
    recent = [
        {"label": row["label"], "ct_kwh": shift(row.get("ct_kwh"))}
        for row in base.get("recent") or []
    ]
    note = "Monatsmittel Day-Ahead in ct/kWh."
    if extra:
        note += f" Inklusive {extra:.2f} ct/kWh Anbieteraufschlag."
    if summer is not None and winter is not None:
        note += f" Typisches Jahr 2023–2025: Sommer (Mai–Aug) {summer:.1f} ct, Winter (Nov–Feb) {winter:.1f} ct."
    return {
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
