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
# Kerzenchart: vier Tage zurück ab Mitternacht, plus den schon veröffentlichten Folgetag.
CHART_HISTORY_DAYS = 4
_YEAR_DISK = Path("/dev/shm/minersteuerung/boersen_year_cache.json")
_net_lock = threading.Lock()
_last_energy_charts = 0.0


def _markup(spec: Dict[str, str]) -> float:
    try:
        return float(spec.get("markup_ct") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ordered_thresholds(buy: float, sell: float) -> tuple:
    buy_v = round(float(buy), 3)
    sell_v = round(max(float(sell), buy_v), 3)
    return buy_v, sell_v


def clamp_power_kw(raw: Any, cap_kw: float) -> float:
    """Leistung zwischen 0 und der aktuellen Obergrenze, in kW."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    cap = max(0.0, float(cap_kw))
    return round(min(cap, max(0.0, value)), 2)


def tariff_grid_kw(
    price_ct: Optional[float],
    buy_ct: float,
    sell_ct: float,
    buy_kw: float,
    sell_kw: float,
    import_cap_kw: float,
    sell_cap_kw: float,
) -> Optional[float]:
    """Voller Buy- oder Sell-Betrag, sobald die Preislinie gerissen wird.

    Unter Buy der eingegebene Kauf in einem Schritt, nie über dem aktuellen
    Max. Netzbezug. Über Sell der eingegebene Verkauf in einem Schritt.
    Dazwischen None, dann wird nichts erzwungen.
    """
    if price_ct is None:
        return None
    price = float(price_ct)
    if price < float(buy_ct):
        return clamp_power_kw(buy_kw, import_cap_kw)
    if price > float(sell_ct):
        return -clamp_power_kw(sell_kw, sell_cap_kw)
    return None


def default_settings() -> Dict[str, Any]:
    return {
        "exchange": "awattar_at",
        "buy_ct_kwh": 5.0,
        "stop_ct_kwh": 15.0,
        "sell_ct_kwh": 25.0,
        "lower_ct_kwh": 5.0,
        "upper_ct_kwh": 15.0,
        "buy_kw": 0.0,
        "sell_kw": 0.0,
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
        sell = float(data.get("sell_ct_kwh", 25.0))
    except (TypeError, ValueError):
        buy, sell = 5.0, 25.0
    buy, sell = _ordered_thresholds(buy, sell)
    data["buy_ct_kwh"] = buy
    data["sell_ct_kwh"] = sell
    data["lower_ct_kwh"] = buy
    data["stop_ct_kwh"] = buy
    data["upper_ct_kwh"] = buy
    data["buy_kw"] = clamp_power_kw(data.get("buy_kw"), 1000.0)
    data["sell_kw"] = clamp_power_kw(data.get("sell_kw"), 1000.0)
    data["apply_bms"] = bool(data.get("apply_bms"))
    return data


def save_settings(
    exchange: str,
    buy_ct_kwh: float,
    sell_ct_kwh: float,
    apply_bms: bool = False,
    buy_kw: Any = None,
    sell_kw: Any = None,
    import_cap_kw: Optional[float] = None,
    sell_cap_kw: Optional[float] = None,
) -> Dict[str, Any]:
    del import_cap_kw, sell_cap_kw
    if exchange not in EXCHANGES:
        raise ValueError("Unbekannte Börse.")
    current = load_settings()
    buy, sell = _ordered_thresholds(buy_ct_kwh, sell_ct_kwh)
    payload = {
        "exchange": exchange,
        "buy_ct_kwh": buy,
        "stop_ct_kwh": buy,
        "sell_ct_kwh": sell,
        "lower_ct_kwh": buy,
        "upper_ct_kwh": buy,
        "buy_kw": clamp_power_kw(current.get("buy_kw") if buy_kw is None else buy_kw, 1000.0),
        "sell_kw": clamp_power_kw(current.get("sell_kw") if sell_kw is None else sell_kw, 1000.0),
        "apply_bms": bool(apply_bms),
    }
    SETTINGS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _slot(
    start_ms: int,
    end_ms: int,
    ct: float,
    ohlc: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    start = datetime.fromtimestamp(start_ms / 1000, TZ)
    slot: Dict[str, Any] = {
        "start": start.isoformat(),
        "end": datetime.fromtimestamp(end_ms / 1000, TZ).isoformat(),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "hour": start.strftime("%H:%M"),
        "day": start.strftime("%Y-%m-%d"),
        "ct_kwh": round(ct, 3),
    }
    if ohlc:
        slot["ohlc"] = {key: round(float(ohlc[key]), 3) for key in ("open", "high", "low", "close")}
    return slot


def _ohlc_ct_from_eur_mwh(values: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Echte Open/High/Low/Close, falls die Börse sie liefert. Einheit wie der Slot: ct/kWh."""
    if any(values.get(key) is None for key in ("open", "high", "low", "close")):
        return None
    try:
        return {key: float(values[key]) / 10.0 for key in ("open", "high", "low", "close")}
    except (TypeError, ValueError):
        return None


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


def chart_window(now: datetime) -> tuple:
    """Vier Kalendertage ab Mitternacht, Ende am übernächsten Mitternacht (Folgetag inklusive)."""
    start = (now - timedelta(days=CHART_HISTORY_DAYS - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def slot_covering(slots: List[Dict[str, Any]], now_ms: int) -> Optional[Dict[str, Any]]:
    """Das Viertel, in dem `now` liegt. Spätere Kerzen im Chart sind nicht der Live-Preis."""
    return next((slot for slot in slots if slot["start_ms"] <= now_ms < slot["end_ms"]), None)


def slots_for_chart(slots: List[Dict[str, Any]], now: datetime, range_mode: str) -> List[Dict[str, Any]]:
    """Heute allein, oder die Vier-Tage-Spanne. Vergangene Stunden bleiben sichtbar."""
    if range_mode == "day":
        today = now.strftime("%Y-%m-%d")
        return [slot for slot in slots if slot["day"] == today]
    start, end = chart_window(now)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    shown = [slot for slot in slots if slot["end_ms"] > start_ms and slot["start_ms"] < end_ms]
    return shown or list(slots)


def candle_for_slot(slot: Dict[str, Any], previous_close: Optional[float]) -> Dict[str, float]:
    """Kerze eines Viertelstunden-Slots.

    Close ist immer der Preis dieses Slots. Hat die Quelle Open/High/Low, bleiben
    die erhalten. Sonst ist Open der vorige Slot und High/Low nur das Maximum
    und Minimum dieser beiden Notierungen — kein erfundener Docht.
    """
    close = round(float(slot["ct_kwh"]), 3)
    explicit = slot.get("ohlc")
    if isinstance(explicit, dict) and all(key in explicit for key in ("open", "high", "low")):
        try:
            open_ = round(float(explicit["open"]), 3)
            high = round(float(explicit["high"]), 3)
            low = round(float(explicit["low"]), 3)
        except (TypeError, ValueError):
            open_ = high = low = None
        else:
            return {
                "open": open_,
                "high": max(high, open_, close),
                "low": min(low, open_, close),
                "close": close,
            }
    open_ = close if previous_close is None else round(float(previous_close), 3)
    return {
        "open": open_,
        "high": max(open_, close),
        "low": min(open_, close),
        "close": close,
    }


def chart_points(slots: List[Dict[str, Any]], shown: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Kerzen der sichtbaren Slots. Open darf vom Slot direkt vor dem Fenster kommen."""
    shown_ids = {slot["start_ms"] for slot in shown}
    previous: Optional[float] = None
    points: List[Dict[str, Any]] = []
    for slot in sorted(slots, key=lambda item: item["start_ms"]):
        candle = candle_for_slot(slot, previous)
        if slot["start_ms"] in shown_ids:
            points.append(
                {
                    "hour": slot["hour"],
                    "day": slot["day"],
                    "ct_kwh": slot["ct_kwh"],
                    "time": int(slot["start_ms"] / 1000),
                    **candle,
                }
            )
        previous = float(slot["ct_kwh"])
    return points


def _fetch_slots(exchange: str) -> List[Dict[str, Any]]:
    now = time.time()
    moment = datetime.now(TZ)
    start_dt, end_dt = chart_window(moment)
    spec = EXCHANGES[exchange]
    base_key = f"slots:{spec['kind']}:{spec.get('url') or spec['bzn']}:{start_dt.date()}:{end_dt.date()}"
    cached = _cache.get(base_key)
    if not (cached and now - cached["fetched"] < _CACHE_SEC):
        slots: List[Dict[str, Any]] = []
        if spec["kind"] == "awattar":
            query = urllib.parse.urlencode(
                {
                    "start": int(start_dt.timestamp() * 1000),
                    "end": int(end_dt.timestamp() * 1000),
                }
            )
            body = _http_json(f"{spec['url']}?{query}")
            for row in body.get("data") or []:
                start_ms = int(row["start_timestamp"])
                end_ms = int(row["end_timestamp"])
                price_ct = float(row["marketprice"]) / 10.0
                slots.append(_slot(start_ms, end_ms, price_ct, _ohlc_ct_from_eur_mwh(row)))
        else:
            query = urllib.parse.urlencode(
                {
                    "bzn": spec["bzn"],
                    "start": start_dt.date().isoformat(),
                    "end": end_dt.date().isoformat(),
                }
            )
            body = _http_json(f"https://api.energy-charts.info/price?{query}")
            times = body.get("unix_seconds") or []
            prices = body.get("price") or []
            opens = body.get("open")
            highs = body.get("high")
            lows = body.get("low")
            closes = body.get("close")

            def _at(series: Any, index: int) -> Any:
                if not isinstance(series, list) or index >= len(series):
                    return None
                return series[index]

            for index, (ts, price) in enumerate(zip(times, prices)):
                if price is None:
                    continue
                start_ms = int(ts) * 1000
                if index + 1 < len(times):
                    end_ms = int(times[index + 1]) * 1000
                else:
                    end_ms = start_ms + 15 * 60 * 1000
                ohlc = _ohlc_ct_from_eur_mwh(
                    {
                        "open": _at(opens, index),
                        "high": _at(highs, index),
                        "low": _at(lows, index),
                        "close": _at(closes, index),
                    }
                )
                slots.append(_slot(start_ms, end_ms, float(price) / 10.0, ohlc))
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
        if isinstance(slot.get("ohlc"), dict):
            copy["ohlc"] = {
                key: round(float(value) + extra, 3)
                for key, value in slot["ohlc"].items()
            }
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


def charge_action(price_ct: Optional[float], buy: float, sell: float) -> str:
    """Unter der Buy-Linie kaufen, über der Sell-Linie verkaufen. Dazwischen kein Eingriff."""
    if price_ct is None:
        return "unbekannt"
    if float(price_ct) < float(buy):
        return "kaufen"
    if float(price_ct) > float(sell):
        return "verkaufen"
    return "warten"


def price_view(exchange: str, range_mode: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    if exchange not in EXCHANGES:
        exchange = settings["exchange"]
    slots = _fetch_slots(exchange)
    moment = datetime.now(TZ)
    now_ms = int(moment.timestamp() * 1000)
    today = moment.strftime("%Y-%m-%d")
    current = slot_covering(slots, now_ms)
    shown = slots_for_chart(slots, moment, "day" if range_mode == "day" else "4d")
    price = current["ct_kwh"] if current else None
    buy = float(settings["buy_ct_kwh"])
    sell = float(settings["sell_ct_kwh"])
    return {
        "exchange": exchange,
        "exchange_label": EXCHANGES[exchange]["label"],
        "goe_awc": EXCHANGES[exchange].get("goe_awc", ""),
        "range": "day" if range_mode == "day" else "4d",
        "today": today,
        "unit": "ct/kWh",
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "current_ct_kwh": price,
        "current_hour": current["hour"] if current else None,
        "buy_ct_kwh": buy,
        "sell_ct_kwh": sell,
        "buy_kw": settings.get("buy_kw"),
        "sell_kw": settings.get("sell_kw"),
        "lower_ct_kwh": buy,
        "upper_ct_kwh": sell,
        "action": charge_action(price, buy, sell),
        "points": chart_points(slots, shown),
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
    if cached and now - cached["fetched"] < _YEAR_CACHE_SEC and _year_payload_ready(cached.get("payload")):
        return cached["payload"]
    disk = _read_year_disk().get(cache_key)
    if isinstance(disk, dict) and now - float(disk.get("fetched") or 0) < _YEAR_CACHE_SEC and _year_payload_ready(disk.get("payload")):
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
    payload = {
        "typical_ct": typical,
        "recent": recent,
        "monthly": {label: round(float(avg), 2) for label, avg in monthly.items()},
    }
    packed = {"fetched": now, "payload": payload}
    _cache[cache_key] = packed
    store = _read_year_disk()
    store[cache_key] = packed
    _write_year_disk(store)
    return payload


def _year_payload_ready(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("monthly"), dict) and bool(payload["monthly"])


def _years_in(monthly: Dict[str, Any]) -> List[int]:
    years = set()
    for label in monthly:
        text = str(label)
        if len(text) >= 4 and text[:4].isdigit():
            years.add(int(text[:4]))
    return sorted(years)


def year_view(exchange: str, year: Optional[int] = None) -> Dict[str, Any]:
    """Monatsmittel eines Kalenderjahres. Die Rohdaten bleiben im Zwischenspeicher."""
    if exchange not in EXCHANGES:
        exchange = "awattar_at"
    spec = EXCHANGES[exchange]
    base = _year_base(spec["bzn"])
    extra = _markup(spec)
    monthly = base.get("monthly") if isinstance(base.get("monthly"), dict) else {}
    years = _years_in(monthly)
    if not years:
        years = [datetime.now(TZ).year]
    if year not in years:
        year = years[-1]

    def shift(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(float(value) + extra, 2)

    values = [shift(monthly.get(f"{year}-{month:02d}")) for month in range(1, 13)]
    summer_vals = [values[month - 1] for month in (5, 6, 7, 8) if values[month - 1] is not None]
    winter_vals = [values[month - 1] for month in (11, 12, 1, 2) if values[month - 1] is not None]
    summer = round(sum(summer_vals) / len(summer_vals), 2) if summer_vals else None
    winter = round(sum(winter_vals) / len(winter_vals), 2) if winter_vals else None
    note = f"{year}: Monatsmittel Day-Ahead in ct/kWh."
    if extra:
        note += f" Inklusive {extra:.2f} ct/kWh Anbieteraufschlag."
    if summer is not None and winter is not None:
        note += f" Sommer (Mai–Aug) {summer:.1f} ct, Winter (Nov–Feb) {winter:.1f} ct."
    elif summer is None or winter is None:
        note += " Dieses Jahr ist noch nicht vollständig."
    return {
        "exchange": exchange,
        "exchange_label": spec["label"],
        "unit": "ct/kWh",
        "year": year,
        "years": years,
        "months": MONTH_LABELS,
        "typical_ct": values,
        "summer_ct": summer,
        "winter_ct": winter,
        "note": note,
    }
