# -*- coding: utf-8 -*-
"""PV-Prognose via open-meteo-solar-forecast (lokale Berechnung, MIT-Lizenz)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.slot_profiles import (
    TIME_SLOTS,
    default_slot_factors,
    default_hour_slot_factors,
    hour_slot_ids,
    hour_slot_label,
    hour_to_hour_slot_id,
    hour_to_slot_id,
    normalize_hour_slot_factors,
)

ENGINE_NAME = "open-meteo-solar-forecast"
FORMULA_TEXT = (
    "Open-Meteo Strahlung (GTI 15-min) → Zelltemperatur (Ross-Modell) → "
    "P = Pmax × (G/Gstc) × (1 + α×ΔT) × ηDC → kWh/h; "
    "Stunde = Roh × Performance Ratio × Stunden-Faktor (05–22 Uhr)"
)

PR_MIN = 0.50
PR_MAX = 1.25
DEFAULT_OMS_BASE = "https://api.open-meteo.com"
FORECAST_DAYS = 7
LAST_PV_HOUR_THRESHOLD_KWH = 0.1
# Physikalische Stunden-Deckelung: etwas über kWp (Wechselrichter/Kurzpeaks)
HOUR_KWH_CAP_FACTOR = 1.05


def last_pv_hour_from_hours(hours: Dict[str, float], threshold: float = LAST_PV_HOUR_THRESHOLD_KWH) -> int:
    """Letzte Stunde mit nennenswerter PV-Prognose (für Lade-/Entlade-Fenster)."""
    last = 0
    for hid, val in (hours or {}).items():
        if float(val or 0) < threshold:
            continue
        try:
            h = int(str(hid).replace("h", ""))
        except ValueError:
            continue
        last = max(last, h)
    return last


def build_pv_forecast_by_date(
    estimate: Any,
    pr: float,
    hour_factors: Dict[str, float],
    *,
    days: int = FORECAST_DAYS,
    max_hour_kwh: Optional[float] = None,
    daily_cap_kwh: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """PV-Prognose pro ISO-Datum für Tag 0 … days-1."""
    today = estimate.now().date()
    hf = normalize_hour_slot_factors(hour_factors)
    by_date: Dict[str, Dict[str, Any]] = {}
    for offset in range(days):
        d = today + timedelta(days=offset)
        iso = d.isoformat()
        hours_raw = _hours_from_wh_period(estimate.wh_period, d)
        hours_adj = _apply_hour_calibration(
            hours_raw, pr, hf, max_hour_kwh=max_hour_kwh
        )
        hours_adj = _scale_hours_to_daily_cap(hours_adj, daily_cap_kwh, max_hour_kwh)
        slots_raw = _slots_from_wh_period(estimate.wh_period, d)
        slots_adj = {
            k: round(v * pr * _slot_hour_factor_share(k, hf), 2) for k, v in slots_raw.items()
        }
        daily_raw = round(sum(hours_raw.values()), 2)
        daily_adj = round(sum(hours_adj.values()), 2)
        by_date[iso] = {
            "daily_kwh": daily_adj,
            "daily_raw_kwh": daily_raw,
            "hours": hours_adj,
            "hours_raw": hours_raw,
            "slots": slots_adj,
            "slots_raw": slots_raw,
            "last_pv_hour": last_pv_hour_from_hours(hours_adj),
            "offset": offset,
        }
    return by_date


def normalize_performance_ratio(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 1.0)
    except (TypeError, ValueError):
        v = 1.0
    return round(max(PR_MIN, min(PR_MAX, v)), 4)


def sync_total_kwp_in_config(config: Dict[str, Any]) -> None:
    """Setzt pv_kwp auf Summe der Ebenen."""
    config["pv_kwp"] = total_kwp_from_config(config)


def compass_azimuth_to_oms(compass_deg: float) -> float:
    """Kompass 0°=N, 90°=O, 180°=S, 270°=W → Open-Meteo 0°=S, -90°=O, 90°=W."""
    oms = float(compass_deg) - 180.0
    while oms > 180.0:
        oms -= 360.0
    while oms < -180.0:
        oms += 360.0
    return oms


def pv_planes_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Baut die PV-Ebenen-Liste aus der Konfiguration."""
    layout = str(config.get("pv_layout") or "east_west").strip().lower()
    decl = float(config.get("pv_declination") or 30)
    eff = float(config.get("pv_dc_efficiency") or 0.93)
    lat = float(config.get("lat") or 0)
    lon = float(config.get("lon") or 0)

    if layout == "single":
        kwp = float(config.get("pv_kwp") or 0)
        az_compass = float(config.get("pv_azimuth_south") or 180)
        return [
            {
                "label": "Süd",
                "latitude": lat,
                "longitude": lon,
                "declination": decl,
                "azimuth_compass": az_compass,
                "azimuth_oms": compass_azimuth_to_oms(az_compass),
                "dc_kwp": kwp,
                "dc_efficiency": eff,
            }
        ]

    kwp_e = float(config.get("pv_kwp_east") or 0)
    kwp_w = float(config.get("pv_kwp_west") or 0)
    az_e = float(config.get("pv_azimuth_east") or 90)
    az_w = float(config.get("pv_azimuth_west") or 270)
    return [
        {
            "label": "Ost",
            "latitude": lat,
            "longitude": lon,
            "declination": decl,
            "azimuth_compass": az_e,
            "azimuth_oms": compass_azimuth_to_oms(az_e),
            "dc_kwp": kwp_e,
            "dc_efficiency": eff,
        },
        {
            "label": "West",
            "latitude": lat,
            "longitude": lon,
            "declination": decl,
            "azimuth_compass": az_w,
            "azimuth_oms": compass_azimuth_to_oms(az_w),
            "dc_kwp": kwp_w,
            "dc_efficiency": eff,
        },
    ]


def total_kwp_from_config(config: Dict[str, Any]) -> float:
    planes = pv_planes_from_config(config)
    return round(sum(float(p["dc_kwp"]) for p in planes), 2)


def _hours_from_wh_period(
    wh_period: Dict[datetime, float],
    target_date: date,
) -> Dict[str, float]:
    result = {hid: 0.0 for hid in hour_slot_ids()}
    for ts, watts in wh_period.items():
        if ts.date() != target_date:
            continue
        hid = hour_to_hour_slot_id(ts.hour)
        if hid:
            result[hid] += float(watts) / 1000.0
    return {k: round(v, 2) for k, v in result.items()}


def daily_forecast_cap_kwh(config: Dict[str, Any]) -> float:
    """Obergrenze kalibrierter Tagesprognose ≈ 110 % Vollsonne-Referenz."""
    from control_algorithm.slot_profiles import perfect_day_kwh

    kwp = float(config.get("pv_kwp") or 0)
    if kwp <= 0:
        return 0.0
    return round(perfect_day_kwh(kwp, config.get("perfect_weather_sun_hours")) * 1.10, 2)


def _scale_hours_to_daily_cap(
    hours: Dict[str, float],
    daily_cap: Optional[float],
    hour_cap: Optional[float],
) -> Dict[str, float]:
    out = {
        k: min(float(v or 0), float(hour_cap)) if hour_cap else float(v or 0)
        for k, v in (hours or {}).items()
    }
    total = sum(out.values())
    if daily_cap and daily_cap > 0 and total > daily_cap and total > 0:
        scale = float(daily_cap) / total
        out = {k: round(v * scale, 2) for k, v in out.items()}
    else:
        out = {k: round(v, 2) for k, v in out.items()}
    return out


def _apply_hour_calibration(
    raw_hours: Dict[str, float],
    pr: float,
    hour_factors: Dict[str, float],
    *,
    max_hour_kwh: Optional[float] = None,
) -> Dict[str, float]:
    """Roh-Stunden × PR × Stundenfaktor, optional physikalisch gedeckelt (kWp)."""
    hf = normalize_hour_slot_factors(hour_factors)
    out: Dict[str, float] = {}
    for hid in hour_slot_ids():
        v = float(raw_hours.get(hid, 0)) * pr * hf.get(hid, 1.0)
        if max_hour_kwh is not None and max_hour_kwh > 0:
            v = min(v, float(max_hour_kwh))
        out[hid] = round(v, 2)
    return out


def hour_kwh_physical_cap(config: Dict[str, Any]) -> float:
    """Max. sinnvolle kWh pro Stunde ≈ kWp × Cap-Faktor."""
    kwp = float(config.get("pv_kwp") or 0)
    if kwp <= 0:
        return 0.0
    return round(kwp * HOUR_KWH_CAP_FACTOR, 3)


def _slots_from_wh_period(
    wh_period: Dict[datetime, float],
    target_date: date,
) -> Dict[str, float]:
    slots = default_slot_factors()
    for k in slots:
        slots[k] = 0.0
    for ts, watts in wh_period.items():
        if ts.date() != target_date:
            continue
        sid = hour_to_slot_id(ts.hour)
        if sid:
            slots[sid] += float(watts) / 1000.0
    return {k: round(v, 2) for k, v in slots.items()}


def _hourly_today_summary(
    wh_period: Dict[datetime, float],
    watts: Dict[datetime, int],
    target_date: date,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    hours: List[Dict[str, Any]] = []
    for ts, w_avg in wh_period.items():
        if ts.date() != target_date:
            continue
        hours.append(
            {
                "time": ts.strftime("%H:%M"),
                "kwh": round(float(w_avg) / 1000.0, 2),
                "peak_w": int(watts.get(ts, 0)),
            }
        )
    hours.sort(key=lambda x: x["kwh"], reverse=True)
    return hours[:top_n]


@dataclass
class ForecastResult:
    today_kwh: float = 0.0
    tomorrow_kwh: float = 0.0
    day_after_kwh: float = 0.0
    today_slots: Dict[str, float] = field(default_factory=default_slot_factors)
    tomorrow_slots: Dict[str, float] = field(default_factory=default_slot_factors)
    day_after_slots: Dict[str, float] = field(default_factory=default_slot_factors)
    today_hours: Dict[str, float] = field(default_factory=default_hour_slot_factors)
    tomorrow_hours: Dict[str, float] = field(default_factory=default_hour_slot_factors)
    day_after_hours: Dict[str, float] = field(default_factory=default_hour_slot_factors)
    today_hours_raw: Dict[str, float] = field(default_factory=default_hour_slot_factors)
    tomorrow_hours_raw: Dict[str, float] = field(default_factory=default_hour_slot_factors)
    day_after_hours_raw: Dict[str, float] = field(default_factory=default_hour_slot_factors)
    pv_forecast_by_date: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    today_raw_kwh: float = 0.0
    tomorrow_raw_kwh: float = 0.0
    day_after_raw_kwh: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    cache: Optional[Dict[str, Any]] = None
    fallback_active: bool = False
    error: Optional[str] = None


async def _fetch_oms_estimate(config: Dict[str, Any]) -> Any:
    from open_meteo_solar_forecast import OpenMeteoSolarForecast

    planes = pv_planes_from_config(config)
    if not planes or sum(p["dc_kwp"] for p in planes) <= 0:
        raise ValueError("PV kWp nicht konfiguriert")

    base_url = (config.get("open_meteo_base_url") or "").strip() or None
    kwargs: Dict[str, Any] = {
        "latitude": [p["latitude"] for p in planes],
        "longitude": [p["longitude"] for p in planes],
        "declination": [p["declination"] for p in planes],
        "azimuth": [p["azimuth_oms"] for p in planes],
        "dc_kwp": [p["dc_kwp"] for p in planes],
        "efficiency_factor": [p["dc_efficiency"] for p in planes],
        "past_days": 2,
        "forecast_days": FORECAST_DAYS,
    }
    if base_url:
        kwargs["base_url"] = base_url.rstrip("/")

    async with OpenMeteoSolarForecast(**kwargs) as forecast:
        return await forecast.estimate()


def _parse_estimate(
    estimate: Any,
    pr: float,
    hour_factors: Dict[str, float],
    *,
    max_hour_kwh: Optional[float] = None,
    daily_cap_kwh: Optional[float] = None,
) -> Tuple[
    float, float, float,
    Dict, Dict, Dict,
    Dict, Dict, Dict,
    Dict, Dict, Dict,
    Dict[str, Dict[str, Any]],
    Dict[str, Any],
]:
    today = estimate.now().date()
    by_date = build_pv_forecast_by_date(
        estimate,
        pr,
        hour_factors,
        days=FORECAST_DAYS,
        max_hour_kwh=max_hour_kwh,
        daily_cap_kwh=daily_cap_kwh,
    )

    def _day(offset: int) -> Dict[str, Any]:
        iso = (today + timedelta(days=offset)).isoformat()
        return by_date.get(iso) or {}

    d0, d1, d2 = _day(0), _day(1), _day(2)
    today_hours_raw = dict(d0.get("hours_raw") or {})
    tomorrow_hours_raw = dict(d1.get("hours_raw") or {})
    day_after_hours_raw = dict(d2.get("hours_raw") or {})
    today_hours_adj = dict(d0.get("hours") or {})
    tomorrow_hours_adj = dict(d1.get("hours") or {})
    day_after_hours_adj = dict(d2.get("hours") or {})
    today_raw = float(d0.get("daily_raw_kwh") or 0)
    tomorrow_raw = float(d1.get("daily_raw_kwh") or 0)
    day_after_raw = float(d2.get("daily_raw_kwh") or 0)
    today_adj = float(d0.get("daily_kwh") or 0)
    tomorrow_adj = float(d1.get("daily_kwh") or 0)
    day_after_adj = float(d2.get("daily_kwh") or 0)
    today_slots_adj = dict(d0.get("slots") or default_slot_factors())
    tomorrow_slots_adj = dict(d1.get("slots") or default_slot_factors())
    day_after_slots_adj = dict(d2.get("slots") or default_slot_factors())

    try:
        peak_time = estimate.power_highest_peak_time_today
        peak_w = int(estimate.watts.get(peak_time, 0))
        peak_time_str = peak_time.strftime("%H:%M")
    except Exception:
        peak_time_str = "—"
        peak_w = 0

    extra = {
        "peak_time_today": peak_time_str,
        "peak_power_w": peak_w,
        "hourly_top_today": _hourly_today_summary(estimate.wh_period, estimate.watts, today),
        "today_slots_raw": dict(d0.get("slots_raw") or {}),
        "tomorrow_slots_raw": dict(d1.get("slots_raw") or {}),
        "day_after_slots_raw": dict(d2.get("slots_raw") or {}),
        "today_hours_raw": today_hours_raw,
        "tomorrow_hours_raw": tomorrow_hours_raw,
        "day_after_hours_raw": day_after_hours_raw,
    }
    return (
        today_adj,
        tomorrow_adj,
        day_after_adj,
        today_slots_adj,
        tomorrow_slots_adj,
        day_after_slots_adj,
        today_hours_adj,
        tomorrow_hours_adj,
        day_after_hours_adj,
        today_hours_raw,
        tomorrow_hours_raw,
        day_after_hours_raw,
        by_date,
        extra,
    )


def _slot_hour_factor_share(slot_id: str, hour_factors: Dict[str, float]) -> float:
    """Mittlerer Stunden-Faktor innerhalb eines 4h-Slots (Abwärtskompatibilität)."""
    for s in TIME_SLOTS:
        if s["id"] == slot_id:
            ids = [
                hour_to_hour_slot_id(h)
                for h in range(int(s["start_hour"]), int(s["end_hour"]))
            ]
            vals = [hour_factors.get(hid, 1.0) for hid in ids if hid]
            return sum(vals) / len(vals) if vals else 1.0
    return 1.0


def _build_detail(
    config: Dict[str, Any],
    pr: float,
    hour_factors: Dict[str, float],
    today_raw: float,
    tomorrow_raw: float,
    today_adj: float,
    tomorrow_adj: float,
    today_slots: Dict[str, float],
    today_hours: Dict[str, float],
    today_hours_raw: Dict[str, float],
    extra: Dict[str, Any],
    *,
    fallback_active: bool,
    error: Optional[str],
    fetched_at: str,
    weather_source: str,
) -> Dict[str, Any]:
    planes = pv_planes_from_config(config)
    plane_rows = []
    total_kwp = sum(p["dc_kwp"] for p in planes) or 1.0
    for p in planes:
        share = round(100.0 * float(p["dc_kwp"]) / total_kwp, 1)
        plane_rows.append(
            {
                "label": p["label"],
                "kwp": p["dc_kwp"],
                "declination": p["declination"],
                "azimuth_compass": p["azimuth_compass"],
                "azimuth_oms": p["azimuth_oms"],
                "share_percent": share,
                "dc_efficiency": p["dc_efficiency"],
            }
        )

    slot_labels = {s["id"]: s["label"] for s in TIME_SLOTS}
    slots_display = [
        {
            "slot": slot_labels.get(sid, sid),
            "slot_id": sid,
            "kwh_raw": extra.get("today_slots_raw", {}).get(sid, 0),
            "kwh_adj": today_slots.get(sid, 0),
        }
        for sid in [s["id"] for s in TIME_SLOTS]
    ]

    hf = normalize_hour_slot_factors(hour_factors)
    hours_display = []
    for hid in hour_slot_ids():
        raw_h = float(today_hours_raw.get(hid, 0))
        adj_h = float(today_hours.get(hid, 0))
        hours_display.append(
            {
                "hour": hour_slot_label(hid),
                "hour_id": hid,
                "kwh_raw": round(raw_h, 2),
                "kwh_adj": round(adj_h, 2),
                "factor": round(hf.get(hid, 1.0), 3),
            }
        )

    return {
        "engine": ENGINE_NAME,
        "formula": FORMULA_TEXT,
        "performance_ratio": pr,
        "hour_slot_factors": hf,
        "pv_layout": config.get("pv_layout", "east_west"),
        "location": {
            "lat": config.get("lat"),
            "lon": config.get("lon"),
            "city": config.get("city_display") or config.get("city"),
        },
        "planes": plane_rows,
        "today_raw_kwh": round(today_raw, 2),
        "today_adj_kwh": today_adj,
        "tomorrow_raw_kwh": round(tomorrow_raw, 2),
        "tomorrow_adj_kwh": tomorrow_adj,
        "slots_today": slots_display,
        "hours_today": hours_display,
        "hourly_today": extra.get("hourly_top_today", []),
        "peak_time_today": extra.get("peak_time_today"),
        "peak_power_w": extra.get("peak_power_w"),
        "weather_source": weather_source,
        "fetched_at": fetched_at,
        "fallback_active": fallback_active,
        "error": error,
        "dc_efficiency": float(config.get("pv_dc_efficiency") or 0.93),
    }


def _cache_fetched_date(cache: Dict[str, Any]) -> Optional[date]:
    raw = cache.get("fetched_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except Exception:
        return None


def _day_bundle_from_by_date(
    by_date: Dict[str, Any],
    day: date,
    *,
    pr: float,
    hf: Dict[str, float],
    hour_cap: Optional[float],
    day_cap: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Ein Kalendertag aus pv_forecast_by_date (ISO-Key) → Stunden/Slots/Summen."""
    entry = by_date.get(day.isoformat())
    if not isinstance(entry, dict):
        return None
    hours_raw = dict(entry.get("hours_raw") or {})
    if not hours_raw and entry.get("hours"):
        # ältere Caches: nur kalibrierte Stunden
        hours_raw = {k: float(v or 0) for k, v in dict(entry.get("hours") or {}).items()}
    if not hours_raw and entry.get("daily_kwh") is None and entry.get("daily_raw_kwh") is None:
        return None
    hours = _scale_hours_to_daily_cap(
        _apply_hour_calibration(hours_raw, pr, hf, max_hour_kwh=hour_cap),
        day_cap,
        hour_cap,
    )
    slots = dict(entry.get("slots") or {})
    if not slots and entry.get("slots_raw"):
        slots = {
            k: round(float(v) * pr * _slot_hour_factor_share(k, hf), 2)
            for k, v in dict(entry.get("slots_raw") or {}).items()
        }
    raw_total = entry.get("daily_raw_kwh")
    if raw_total is None:
        raw_total = round(sum(float(v or 0) for v in hours_raw.values()), 2)
    adj_total = entry.get("daily_kwh")
    if adj_total is None:
        adj_total = round(sum(hours.values()), 2)
    return {
        "hours_raw": hours_raw,
        "hours": hours,
        "slots": slots or default_slot_factors(),
        "raw": round(float(raw_total or 0), 2),
        "adj": round(float(adj_total or 0), 2),
    }


def _apply_cache(
    cache: Dict[str, Any], pr: float, hour_factors: Dict[str, float], config: Dict[str, Any]
) -> ForecastResult:
    """
    Cache-Fallback: Tage immer über ISO-Datum aus ``pv_forecast_by_date`` auflösen.

    Verhindert den Mitternachts-Bug: gestern „heute 64“ darf nach 00:00 nicht
    als neues „heute“ landen – stattdessen gestern „morgen“ (= heutiges ISO).
    """
    hf = normalize_hour_slot_factors(hour_factors)
    hour_cap = hour_kwh_physical_cap(config) or None
    day_cap = daily_forecast_cap_kwh(config) or None
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    by_date = dict(cache.get("pv_forecast_by_date") or {})

    today_b = _day_bundle_from_by_date(
        by_date, today, pr=pr, hf=hf, hour_cap=hour_cap, day_cap=day_cap
    )
    tomorrow_b = _day_bundle_from_by_date(
        by_date, tomorrow, pr=pr, hf=hf, hour_cap=hour_cap, day_cap=day_cap
    )
    day_after_b = _day_bundle_from_by_date(
        by_date, day_after, pr=pr, hf=hf, hour_cap=hour_cap, day_cap=day_cap
    )

    # Relative Cache-Felder:
    # - gleicher Kalendertag (oder Legacy ohne fetched_at) → today/tomorrow wie gespeichert
    # - Cache von gestern ohne by_date → um eine Nacht schieben (today←tomorrow, …)
    fetched_day = _cache_fetched_date(cache)
    if fetched_day is None or fetched_day == today:
        rel_shift = 0
    elif fetched_day == today - timedelta(days=1):
        rel_shift = 1
    else:
        rel_shift = -1  # zu alt / Zukunft → relative Felder nicht nutzen

    def _rel_raw(name: str) -> Dict[str, float]:
        return dict(cache.get(name) or {})

    def _rel(offset: int) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float], float, float]:
        src = offset + rel_shift
        if rel_shift < 0 or src < 0 or src > 2:
            return {}, {}, default_slot_factors(), 0.0, 0.0
        if src == 0:
            raw_key, slots_key = "today_hours_raw", "today_slots_raw"
        elif src == 1:
            raw_key, slots_key = "tomorrow_hours_raw", "tomorrow_slots_raw"
        else:
            raw_key, slots_key = "day_after_hours_raw", "day_after_slots_raw"
        hours_raw = _rel_raw(raw_key)
        hours = _scale_hours_to_daily_cap(
            _apply_hour_calibration(hours_raw, pr, hf, max_hour_kwh=hour_cap),
            day_cap,
            hour_cap,
        )
        slots = {
            k: round(float(v) * pr * _slot_hour_factor_share(k, hf), 2)
            for k, v in (_rel_raw(slots_key)).items()
        }
        return (
            hours_raw,
            hours,
            slots or default_slot_factors(),
            round(sum(hours_raw.values()), 2),
            round(sum(hours.values()), 2),
        )

    if today_b is None:
        today_hours_raw, today_hours, today_slots, today_raw, today_adj = _rel(0)
    else:
        today_hours_raw = today_b["hours_raw"]
        today_hours = today_b["hours"]
        today_slots = today_b["slots"]
        today_raw = today_b["raw"]
        today_adj = today_b["adj"]

    if tomorrow_b is None:
        tomorrow_hours_raw, tomorrow_hours, tomorrow_slots, tomorrow_raw, tomorrow_adj = _rel(1)
    else:
        tomorrow_hours_raw = tomorrow_b["hours_raw"]
        tomorrow_hours = tomorrow_b["hours"]
        tomorrow_slots = tomorrow_b["slots"]
        tomorrow_raw = tomorrow_b["raw"]
        tomorrow_adj = tomorrow_b["adj"]

    if day_after_b is None:
        day_after_hours_raw, day_after_hours, day_after_slots, day_after_raw, day_after_adj = _rel(2)
    else:
        day_after_hours_raw = day_after_b["hours_raw"]
        day_after_hours = day_after_b["hours"]
        day_after_slots = day_after_b["slots"]
        day_after_raw = day_after_b["raw"]
        day_after_adj = day_after_b["adj"]

    prev_detail = cache.get("detail") or {}
    extra = {
        "today_slots_raw": cache.get("today_slots_raw") or {},
        "tomorrow_slots_raw": cache.get("tomorrow_slots_raw") or {},
        "today_hours_raw": today_hours_raw,
        "tomorrow_hours_raw": tomorrow_hours_raw,
        "day_after_hours_raw": day_after_hours_raw,
        "peak_time_today": prev_detail.get("peak_time_today"),
        "peak_power_w": prev_detail.get("peak_power_w"),
        "hourly_top_today": prev_detail.get("hourly_today") or [],
    }
    base_url = (config.get("open_meteo_base_url") or "").strip() or DEFAULT_OMS_BASE
    detail = _build_detail(
        config,
        pr,
        hf,
        today_raw,
        tomorrow_raw,
        today_adj,
        tomorrow_adj,
        today_slots or default_slot_factors(),
        today_hours,
        today_hours_raw,
        extra,
        fallback_active=True,
        error=str(cache.get("error") or "Cache-Fallback aktiv"),
        fetched_at=str(cache.get("fetched_at") or datetime.now().isoformat()),
        weather_source=base_url,
    )
    pv_by_date = dict(by_date)
    if not pv_by_date and (today_hours_raw or tomorrow_hours_raw) and rel_shift == 0:
        pv_by_date = {
            today.isoformat(): {
                "daily_kwh": today_adj,
                "daily_raw_kwh": today_raw,
                "hours": today_hours,
                "hours_raw": today_hours_raw,
                "slots": today_slots,
                "last_pv_hour": last_pv_hour_from_hours(today_hours),
                "offset": 0,
            },
            tomorrow.isoformat(): {
                "daily_kwh": tomorrow_adj,
                "daily_raw_kwh": tomorrow_raw,
                "hours": tomorrow_hours,
                "hours_raw": tomorrow_hours_raw,
                "slots": tomorrow_slots,
                "last_pv_hour": last_pv_hour_from_hours(tomorrow_hours),
                "offset": 1,
            },
            day_after.isoformat(): {
                "daily_kwh": day_after_adj,
                "daily_raw_kwh": day_after_raw,
                "hours": day_after_hours,
                "hours_raw": day_after_hours_raw,
                "slots": day_after_slots,
                "last_pv_hour": last_pv_hour_from_hours(day_after_hours),
                "offset": 2,
            },
        }
    return ForecastResult(
        today_kwh=today_adj,
        tomorrow_kwh=tomorrow_adj,
        day_after_kwh=day_after_adj,
        today_slots=today_slots or default_slot_factors(),
        tomorrow_slots=tomorrow_slots or default_slot_factors(),
        day_after_slots=day_after_slots or default_slot_factors(),
        today_hours=today_hours,
        tomorrow_hours=tomorrow_hours,
        day_after_hours=day_after_hours,
        today_hours_raw=today_hours_raw,
        tomorrow_hours_raw=tomorrow_hours_raw,
        day_after_hours_raw=day_after_hours_raw,
        pv_forecast_by_date=pv_by_date,
        today_raw_kwh=today_raw,
        tomorrow_raw_kwh=tomorrow_raw,
        day_after_raw_kwh=day_after_raw,
        detail=detail,
        fallback_active=True,
        error=str(cache.get("error") or "Cache-Fallback aktiv"),
    )


def fetch_pv_forecast(config: Dict[str, Any], state: Dict[str, Any]) -> ForecastResult:
    """Synchroner Einstieg: holt OMS-Prognose oder nutzt Cache-Fallback."""
    pr = normalize_performance_ratio(state.get("performance_ratio"))
    hour_factors = normalize_hour_slot_factors(state.get("hour_slot_factors"))
    if not config.get("has_pv"):
        return ForecastResult(detail={"engine": ENGINE_NAME, "error": "PV deaktiviert"})

    lat, lon = config.get("lat"), config.get("lon")
    if lat is None or lon is None:
        return ForecastResult(detail={"engine": ENGINE_NAME, "error": "Keine Koordinaten"})

    total_kwp = total_kwp_from_config(config)
    if total_kwp <= 0:
        return ForecastResult(detail={"engine": ENGINE_NAME, "error": "kWp = 0"})

    base_url = (config.get("open_meteo_base_url") or "").strip() or DEFAULT_OMS_BASE
    fetched_at = datetime.now().isoformat()

    try:
        estimate = asyncio.run(_fetch_oms_estimate(config))
        (
            today_adj,
            tomorrow_adj,
            day_after_adj,
            today_slots,
            tomorrow_slots,
            day_after_slots,
            today_hours,
            tomorrow_hours,
            day_after_hours,
            today_hours_raw,
            tomorrow_hours_raw,
            day_after_hours_raw,
            pv_by_date,
            extra,
        ) = _parse_estimate(
            estimate,
            pr,
            hour_factors,
            max_hour_kwh=hour_kwh_physical_cap(config) or None,
            daily_cap_kwh=daily_forecast_cap_kwh(config) or None,
        )
        today_raw = round(sum(today_hours_raw.values()), 2)
        tomorrow_raw = round(sum(tomorrow_hours_raw.values()), 2)
        day_after_raw = round(sum(day_after_hours_raw.values()), 2)
        detail = _build_detail(
            config,
            pr,
            hour_factors,
            today_raw,
            tomorrow_raw,
            today_adj,
            tomorrow_adj,
            today_slots,
            today_hours,
            today_hours_raw,
            extra,
            fallback_active=False,
            error=None,
            fetched_at=fetched_at,
            weather_source=base_url,
        )
        cache_payload = {
            "today_raw_kwh": today_raw,
            "tomorrow_raw_kwh": tomorrow_raw,
            "day_after_raw_kwh": day_after_raw,
            "today_slots_raw": extra.get("today_slots_raw", {}),
            "tomorrow_slots_raw": extra.get("tomorrow_slots_raw", {}),
            "day_after_slots_raw": extra.get("day_after_slots_raw", {}),
            "today_hours_raw": today_hours_raw,
            "tomorrow_hours_raw": tomorrow_hours_raw,
            "day_after_hours_raw": day_after_hours_raw,
            "pv_forecast_by_date": pv_by_date,
            "detail": detail,
            "fetched_at": fetched_at,
        }
        return ForecastResult(
            today_kwh=today_adj,
            tomorrow_kwh=tomorrow_adj,
            day_after_kwh=day_after_adj,
            today_slots=today_slots,
            tomorrow_slots=tomorrow_slots,
            day_after_slots=day_after_slots,
            today_hours=today_hours,
            tomorrow_hours=tomorrow_hours,
            day_after_hours=day_after_hours,
            today_hours_raw=today_hours_raw,
            tomorrow_hours_raw=tomorrow_hours_raw,
            day_after_hours_raw=day_after_hours_raw,
            pv_forecast_by_date=pv_by_date,
            today_raw_kwh=today_raw,
            tomorrow_raw_kwh=tomorrow_raw,
            day_after_raw_kwh=day_after_raw,
            detail=detail,
            cache=cache_payload,
            fallback_active=False,
            error=None,
        )
    except Exception as e:
        cache = state.get("pv_forecast_cache") or {}
        if cache.get("today_raw_kwh") or cache.get("today_hours_raw"):
            result = _apply_cache(cache, pr, hour_factors, config)
            result.error = str(e)
            if result.detail:
                result.detail["error"] = str(e)
            return result
        empty_detail = _build_detail(
            config,
            pr,
            hour_factors,
            0,
            0,
            0,
            0,
            default_slot_factors(),
            default_hour_slot_factors(),
            default_hour_slot_factors(),
            {},
            fallback_active=False,
            error=str(e),
            fetched_at=fetched_at,
            weather_source=base_url,
        )
        return ForecastResult(detail=empty_detail, error=str(e))


