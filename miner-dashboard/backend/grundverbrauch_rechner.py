# -*- coding: utf-8 -*-
"""Grundverbrauchsrechner: nur rechnen, nichts schalten.

Empfohlener Dauer-Netzbezug, damit der Speicher auf Sicht den Stand hält.
Grundlage ist der Grundverbrauch (Betrieb minus Miner), nicht der Verbrauch
an Tagen, an denen Miner liefen. Zusätzlich zählt nur die Leistung der Miner,
die hier als durchgehend angenommen sind.

Bewusst nicht verwendet, weil es von der Realität abweicht:
- Gesamtverbrauch inklusive Miner
- gemessener Netzbezug als Untergrenze
- Monatsmittel-PV als Lückenfüller (energy_stats_30d ist kein Tagesarchiv)
- lineare Hochrechnung der heutigen PV auf 24 Stunden
- angebrochene Tage mit zu wenigen Messstunden
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

_lock = threading.Lock()

HOURS = 24
MIN_HOURS = 18
MIN_PV_DAYS = 3
LOOKBACK_RATE = 7
LOOKBACK_CHART = 30
BASE_ARCHIVE_DAYS = 366
YEAR_DAYS = 365
WEEKLY_STEP = 0.25
WEEKLY_STEP_SEASON = 0.50
DAILY_STEP_REL = 0.08
DAILY_STEP_ABS = 0.15
# Der Tageswert bleibt stehen und wird höchstens alle 10 Stunden nachgezogen,
# zusätzlich beim Kalendertag. So folgt der Zukauf Wetter und Speicher, ohne zu zittern.
DAILY_REFRESH = timedelta(hours=10)
MODES = ("continuous", "off")
WEATHER_FORECAST_DAYS = 7
WEATHER_CACHE_SEC = 3600
# Leichte Anhebung des empfohlenen Zukaufs bei schlechter werdender Woche (Anteil × Score).
WEATHER_BUMP_FRAC = 0.12
SOC_HOLD_HORIZONS = (24, 48)
# Dauer-Zukauf: Speicherziel, drei Tage, morgige Prognose, Minsock-Boden.
PURCHASE_SOC_TARGET = 63.0
PURCHASE_SOC_DEADBAND = 3.0
PURCHASE_SOC_HOURS = 168.0
PURCHASE_HIST_HOURS = 72.0
PURCHASE_SOC_LIMIT = 0.25
PURCHASE_HIST_LIMIT = 0.15
PURCHASE_FCST_MIN = -0.30
PURCHASE_FCST_MAX = 0.50
PURCHASE_FLOOR_EFF = 0.95
PURCHASE_FLOOR_SKIP_KWH = 0.4
PURCHASE_STEP_KW = 0.05
PURCHASE_MAX_KW = 2.0
PURCHASE_PV_HOUR_KWH = 0.1
# Angezeigter Zukauf aus den ersten drei Kalendertagen des Monats.
MONTH_OPEN_DAYS = 3
MONTH_SHAPE_LIMIT = 0.12
# Heller Tag im Fenster 8–17 Uhr: shortwave/800 liegt im Mittel bei etwa 0,45, nicht bei 1.
CLEAR_DAYLIGHT_SUN = 0.45
WEATHER_SWING = 0.50
WEATHER_FACTOR_MIN = 0.85
WEATHER_FACTOR_MAX = 1.35
# Dauer-Zukauf voraus: Last 7 Tage, PV-Mischung, Speicher über 2 Tage, Glättung.
FORWARD_LOOKBACK_DAYS = 7
FORWARD_PV_TOMORROW_W = 0.6
FORWARD_PV_DAY_AFTER_W = 0.4
FORWARD_RELIEF_DAYS = 2.0
FORWARD_BUFFER_SOC = 3.0
FORWARD_SMOOTH_REL = 0.12
FORWARD_SMOOTH_ABS = 0.15
MIN_SOC_HARD_LOW = 30.0
MIN_SOC_HARD_HIGH = 35.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _state_path(base_dir: str):
    from control_algorithm.paths import energy_file

    return energy_file(base_dir, "grundverbrauch_rechner.json")


def default_targets() -> Dict[str, Any]:
    return {
        "soc_hold_enabled": False,
        "soc_hold_percent": 5.0,
        "soc_hold_horizon_hours": 24,
        "weather_adjust_enabled": True,
    }


def _empty_state() -> Dict[str, Any]:
    return {
        "assumptions": {},
        "recommendation": {},
        "base_archive": [],
        "targets": default_targets(),
        "weather_cache": {},
    }


def _load_state(path) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("assumptions", {})
    data.setdefault("recommendation", {})
    data.setdefault("base_archive", [])
    data.setdefault("targets", default_targets())
    data.setdefault("weather_cache", {})
    targets = dict(default_targets())
    raw_targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    targets.update({k: raw_targets[k] for k in targets if k in raw_targets})
    targets["soc_hold_enabled"] = bool(targets.get("soc_hold_enabled"))
    targets["weather_adjust_enabled"] = bool(targets.get("weather_adjust_enabled", True))
    try:
        targets["soc_hold_percent"] = max(1.0, min(95.0, float(targets.get("soc_hold_percent", 5.0))))
    except (TypeError, ValueError):
        targets["soc_hold_percent"] = 5.0
    try:
        horizon = int(targets.get("soc_hold_horizon_hours", 24))
    except (TypeError, ValueError):
        horizon = 24
    targets["soc_hold_horizon_hours"] = 48 if horizon >= 48 else 24
    data["targets"] = targets
    return data


def _save_state(path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _iso_week_id(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _next_week_date(d: date) -> str:
    days = (7 - d.weekday()) % 7
    if days == 0:
        days = 7
    return (d + timedelta(days=days)).isoformat()


def spread_daily_kwh(total: float) -> List[float]:
    """Tagesenergie auf eine Tageslichtkurve legen, wenn keine Stundenwerte da sind."""
    weights = []
    for hour in range(HOURS):
        if 6 <= hour <= 18:
            weights.append(max(0.0, math.sin(math.pi * (hour - 6) / 12.0)))
        else:
            weights.append(0.0)
    scale = sum(weights) or 1.0
    return [round(max(0.0, total) * w / scale, 4) for w in weights]


def hours_from_map(raw: Any, fallback_total: Optional[float] = None) -> Optional[List[float]]:
    if isinstance(raw, dict) and raw:
        out = [0.0] * HOURS
        found = False
        for hour in range(HOURS):
            key = f"h{hour:02d}"
            if key in raw:
                out[hour] = max(0.0, _f(raw.get(key)))
                found = True
        if found and sum(out) > 0:
            return out
    if fallback_total is not None and fallback_total > 0:
        return spread_daily_kwh(fallback_total)
    return None


def grid_kwh_for_day(
    load_hourly: Sequence[float],
    gen_hourly: Sequence[float],
    efficiency: float,
) -> Dict[str, float]:
    """Netz-kWh, damit der Speicher das Tagesdefizit nach Wirkungsgrad deckt.

    Überschuss, der nach dem Ausgleich übrig bleibt, senkt den Zukauf nicht
    unter null. Nur was der Speicher nicht mehr liefern kann, kommt aus dem Netz.
    """
    eff = min(1.0, max(0.5, _f(efficiency, 0.95)))
    surplus = 0.0
    deficit = 0.0
    load_sum = 0.0
    gen_sum = 0.0
    for hour in range(HOURS):
        load = _f(load_hourly[hour] if hour < len(load_hourly) else 0.0)
        gen = _f(gen_hourly[hour] if hour < len(gen_hourly) else 0.0)
        load_sum += load
        gen_sum += gen
        delta = gen - load
        if delta >= 0:
            surplus += delta
        else:
            deficit -= delta
    delivered = min(surplus, deficit / eff) * eff
    grid = max(0.0, deficit - delivered)
    cycle_loss = min(surplus, deficit / eff) * (1.0 - eff)
    return {
        "grid_kwh": round(grid, 4),
        "plain_kwh": round(max(0.0, deficit - surplus), 4),
        "cycle_loss_kwh": round(cycle_loss, 4),
        "surplus_kwh": round(surplus, 4),
        "deficit_kwh": round(deficit, 4),
        "load_kwh": round(load_sum, 4),
        "gen_kwh": round(gen_sum, 4),
    }


def _hourly_or_zero(raw: Any) -> List[float]:
    out = [0.0] * HOURS
    if not isinstance(raw, list):
        return out
    for i, value in enumerate(raw[:HOURS]):
        out[i] = max(0.0, _f(value))
    return out


def _measured_hours(hourly: Sequence[float]) -> int:
    return sum(1 for value in hourly if value > 0.02)


def _clamp_step(prev: Optional[float], raw: float, rel: float, abs_kw: float) -> float:
    if prev is None or prev < 0.05:
        return round(max(0.0, raw), 3)
    limit = max(abs_kw, abs(prev) * rel)
    delta = raw - prev
    if delta > limit:
        delta = limit
    elif delta < -limit:
        delta = -limit
    return round(max(0.0, prev + delta), 3)


def _slope_kwh_per_week(values: Sequence[float]) -> Optional[float]:
    n = len(values)
    if n < 4:
        return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = 0.0
    den = 0.0
    for i, y in enumerate(values):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    if den <= 0:
        return 0.0
    return (num / den) * 7.0


def _trend_label(per_week: Optional[float]) -> str:
    if per_week is None:
        return "zu wenig Tage"
    if per_week > 1.5:
        return "steigend"
    if per_week < -1.5:
        return "fallend"
    return "stabil"


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def apply_recommendation_locks(
    prev: Dict[str, Any],
    raw_kw: Optional[float],
    today: date,
    *,
    season_shift: bool,
    fingerprint: str,
) -> Dict[str, Any]:
    """Wochenwert höchstens einmal je ISO-Woche, Tageswert einmal je Kalendertag.

    Eine neue Miner-Annahme ist eine andere Rechnung, kein Messrauschen:
    dann werden beide Werte sofort auf den neuen Rohwert gesetzt.
    Von 0 kW darf der Wochenwert beim nächsten Termin voll auf den Rohwert,
    sonst bliebe ein relativer Schritt an der Null hängen.
    """
    week_id = _iso_week_id(today)
    daily_id = today.isoformat()
    prev_fp = str(prev.get("fingerprint") or "")
    assumption_changed = bool(prev_fp) and prev_fp != fingerprint

    def _opt(key: str) -> Optional[float]:
        raw = prev.get(key)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    weekly_prev = _opt("weekly_kw")
    daily_prev = _opt("daily_kw")

    if raw_kw is None:
        return {
            "weekly_kw": weekly_prev,
            "daily_kw": daily_prev,
            "raw_kw": None,
            "week_id": week_id,
            "daily_id": prev.get("daily_id"),
            "fingerprint": fingerprint,
            "season_shift": season_shift,
            "weekly_updated": False,
            "daily_updated": False,
        }

    raw_kw = max(0.0, raw_kw)
    if assumption_changed or weekly_prev is None:
        weekly = round(raw_kw, 3)
        weekly_updated = True
    elif prev.get("week_id") != week_id or season_shift:
        step = WEEKLY_STEP_SEASON if season_shift else WEEKLY_STEP
        weekly = _clamp_step(weekly_prev, raw_kw, step, 0.2)
        weekly_updated = True
    else:
        weekly = round(weekly_prev, 3)
        weekly_updated = False

    now = datetime.now()
    refreshed_at = _parsed_stamp(prev.get("daily_updated_at"))
    daily_due = (
        assumption_changed
        or daily_prev is None
        or prev.get("daily_id") != daily_id
        or (refreshed_at is not None and now - refreshed_at >= DAILY_REFRESH)
    )
    if not daily_due:
        daily = round(daily_prev, 3)
        daily_updated = False
        daily_updated_at = prev.get("daily_updated_at")
    elif assumption_changed or daily_prev is None:
        daily = round(raw_kw, 3)
        daily_updated = True
        daily_updated_at = now.isoformat(timespec="seconds")
    else:
        daily = _clamp_step(daily_prev, raw_kw, DAILY_STEP_REL, DAILY_STEP_ABS)
        daily_updated = True
        daily_updated_at = now.isoformat(timespec="seconds")

    return {
        "weekly_kw": weekly,
        "daily_kw": daily,
        "raw_kw": round(raw_kw, 3),
        "week_id": week_id,
        "daily_id": daily_id,
        "fingerprint": fingerprint,
        "season_shift": season_shift,
        "weekly_updated": weekly_updated,
        "daily_updated": daily_updated,
        "daily_updated_at": daily_updated_at,
        "updated_at": now.isoformat(timespec="seconds"),
    }


def _parsed_stamp(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _avg_base(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"days": 0, "base_kwh": None, "base_kw": None, "peak_kwh": None, "peak_date": None}
    base_mean = sum(r["base_kwh"] for r in rows) / len(rows)
    peak = max(rows, key=lambda row: row["base_kwh"])
    peak_date = peak.get("date")
    if hasattr(peak_date, "isoformat"):
        peak_date = peak_date.isoformat()
    return {
        "days": len(rows),
        "base_kwh": round(base_mean, 2),
        "base_kw": round(base_mean / HOURS, 3),
        "peak_kwh": round(float(peak["base_kwh"]), 2),
        "peak_date": None if peak_date is None else str(peak_date)[:10],
    }


def build_report_from_parts(
    days: List[Dict[str, Any]],
    miners: List[Dict[str, Any]],
    modes: Dict[str, str],
    *,
    efficiency: float,
    today: date,
    prev: Optional[Dict[str, Any]] = None,
    algo_enabled: bool = False,
    weather_bump_kw: float = 0.0,
    soc_hold_kw: float = 0.0,
    weather_meta: Optional[Dict[str, Any]] = None,
    targets: Optional[Dict[str, Any]] = None,
    soc_live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Rechnet den Bericht aus vorbereiteten Tagen. Schaltet nichts."""
    prev = dict(prev or {})
    eff = min(1.0, max(0.5, _f(efficiency, 0.95)))
    weather_meta = dict(weather_meta or {})
    targets = dict(targets or default_targets())
    soc_live = dict(soc_live or {})
    miner_out: List[Dict[str, Any]] = []
    normalized_modes: Dict[str, str] = {}
    continuous_kw = 0.0
    for miner in miners:
        ip = str(miner.get("ip") or "")
        if not ip:
            continue
        mode = modes.get(ip) or "off"
        if mode not in MODES:
            mode = "off"
        normalized_modes[ip] = mode
        power = max(0.0, _f(miner.get("power_kw")))
        if mode == "continuous":
            continuous_kw += power
        miner_out.append(
            {
                "ip": ip,
                "name": miner.get("name") or ip,
                "power_kw": round(power, 2),
                "mode": mode,
                "algo_enabled": bool(miner.get("algo_enabled")),
            }
        )
    fingerprint = "|".join(
        f"{m['ip']}:{m['mode']}:{m['power_kw']:.2f}" for m in miner_out
    )

    rated: List[Dict[str, Any]] = []
    series: List[Dict[str, Any]] = []
    base_days: List[Dict[str, Any]] = []
    skipped_incomplete = 0
    hydro_positive = 0

    for day in days:
        try:
            day_date = date.fromisoformat(str(day.get("date") or "")[:10])
        except ValueError:
            continue
        base_hourly = _hourly_or_zero(day.get("base_hourly"))
        base_kwh = round(sum(base_hourly), 4)
        measured = _measured_hours(base_hourly)
        is_today = day_date == today
        complete = (not is_today) and measured >= MIN_HOURS and base_kwh > 0
        if (not is_today) and base_kwh > 0 and not complete:
            skipped_incomplete += 1
        hydro_hourly = _hourly_or_zero(day.get("hydro_hourly"))
        hydro_kwh = round(sum(hydro_hourly), 4)
        if hydro_kwh > 0.05:
            hydro_positive += 1
        pv_raw = day.get("pv_hourly")
        pv_known = isinstance(pv_raw, list) and len(pv_raw) >= HOURS
        pv_hourly = _hourly_or_zero(pv_raw) if pv_known else None
        pv_kwh = round(sum(pv_hourly), 4) if pv_hourly is not None else None

        if complete:
            base_days.append(
                {
                    "date": day_date,
                    "base_kwh": base_kwh,
                    "base_hourly": base_hourly,
                    "pv_kwh": pv_kwh,
                }
            )
        row: Dict[str, Any] = {
            "date": day_date.isoformat(),
            "base_kwh": base_kwh if not is_today else None,
            "pv_kwh": pv_kwh,
            "hydro_kwh": hydro_kwh if (complete or is_today) else None,
            "gap_kwh": None,
            "used": False,
        }
        if complete and pv_hourly is not None:
            gen_hourly = [pv_hourly[h] + hydro_hourly[h] for h in range(HOURS)]
            load_hourly = [base_hourly[h] + continuous_kw for h in range(HOURS)]
            bal = grid_kwh_for_day(load_hourly, gen_hourly, eff)
            row["used"] = True
            row["gap_kwh"] = round(bal["load_kwh"] - bal["gen_kwh"], 2)
            row["base_kwh"] = base_kwh
            rated.append(
                {
                    "date": day_date,
                    "base_kwh": base_kwh,
                    "pv_kwh": pv_kwh or 0.0,
                    "hydro_kwh": hydro_kwh,
                    "grid_kwh": bal["grid_kwh"],
                    "cycle_loss_kwh": bal["cycle_loss_kwh"],
                }
            )
        if 0 <= (today - day_date).days <= LOOKBACK_CHART:
            series.append(row)

    rated.sort(key=lambda r: r["date"])
    base_days.sort(key=lambda r: r["date"])
    series.sort(key=lambda r: r["date"])
    rate_days = rated[-LOOKBACK_RATE:]
    raw_kw = None
    if rate_days:
        raw_kw = sum(r["grid_kwh"] for r in rate_days) / (len(rate_days) * HOURS)
    base_raw_kw = raw_kw
    weather_bump = max(0.0, _f(weather_bump_kw))
    soc_extra = max(0.0, _f(soc_hold_kw))
    if raw_kw is not None:
        raw_kw = max(0.0, raw_kw + weather_bump + soc_extra)

    recent_base = [r["base_kwh"] for r in base_days[-7:]]
    older_base = [r["base_kwh"] for r in base_days[-14:-7]]
    season_shift = False
    if len(recent_base) >= 4 and len(older_base) >= 4:
        recent_mean = sum(recent_base) / len(recent_base)
        older_mean = sum(older_base) / len(older_base)
        if older_mean > 1 and recent_mean > older_mean * 1.20:
            season_shift = True
    recent_pv = rated[-7:]
    older_pv = rated[-14:-7]
    if len(recent_pv) >= MIN_PV_DAYS and len(older_pv) >= MIN_PV_DAYS:
        recent_sum = sum(r["pv_kwh"] for r in recent_pv)
        older_sum = sum(r["pv_kwh"] for r in older_pv)
        if older_sum > 1 and recent_sum < older_sum * 0.70:
            season_shift = True

    trend = _slope_kwh_per_week([r["base_kwh"] for r in base_days[-28:]])
    locks = apply_recommendation_locks(
        prev,
        raw_kw,
        today,
        season_shift=season_shift,
        fingerprint=fingerprint,
    )

    profile_days = base_days[-7:]
    history_avg = [0.0] * HOURS
    if profile_days:
        for hour in range(HOURS):
            history_avg[hour] = round(
                sum(r["base_hourly"][hour] for r in profile_days) / len(profile_days),
                3,
            )
    today_hourly: List[Optional[float]] = [None] * HOURS
    today_block: Dict[str, Any] = {
        "base_kwh": None,
        "base_kw": None,
        "hours": None,
        "extrapolated_kwh": None,
        "pv_kwh": None,
    }
    for day in days:
        if str(day.get("date") or "")[:10] != today.isoformat():
            continue
        raw_today = day.get("today_hourly_actual")
        if isinstance(raw_today, list):
            for hour, value in enumerate(raw_today[:HOURS]):
                today_hourly[hour] = None if value is None else round(_f(value), 3)
        hours = _f(day.get("hours_elapsed"))
        so_far = _f(day.get("base_so_far_kwh"))
        if hours > 0.2:
            today_block = {
                "base_kwh": round(so_far, 2),
                "base_kw": round(so_far / hours, 3),
                "hours": round(hours, 2),
                "extrapolated_kwh": round(so_far / hours * HOURS, 2),
                "pv_kwh": (
                    round(_f(day.get("pv_so_far_kwh")), 2)
                    if day.get("pv_so_far_kwh") is not None
                    else None
                ),
            }
        break

    avg_loss = _mean([r["cycle_loss_kwh"] for r in rate_days])
    avg_grid = _mean([r["grid_kwh"] for r in rate_days])
    avg_pv = _mean([r["pv_kwh"] for r in rate_days])
    avg_base = _mean([r["base_kwh"] for r in rate_days])
    avg_hydro = _mean([r["hydro_kwh"] for r in rate_days])
    weekly = locks.get("weekly_kw")

    alerts: List[str] = []
    notices: List[str] = [
        "Diese Seite schaltet keine Miner und ändert keine Steuerung. Die Auswahl ist nur die Annahme für die Rechnung.",
        "Der laufende Tag geht nicht in den Zukauf. Die PV bis zum Abend linear hochzurechnen würde den Wert verfälschen.",
    ]
    if weather_meta.get("ok") and weather_bump > 0.005:
        notices.append(
            f"Wettervorhersage ({WEATHER_FORECAST_DAYS} Tage, rollierend): schlechteres Ende der Woche → "
            f"Zukauf leicht um {weather_bump:.2f} kW angehoben."
        )
    elif weather_meta.get("ok"):
        notices.append(
            f"Wettervorhersage ({WEATHER_FORECAST_DAYS} Tage, rollierend) geladen. Keine Anhebung nötig."
        )
    elif weather_meta.get("error"):
        notices.append(f"Wettervorhersage nicht verfügbar: {weather_meta.get('error')}")
    if targets.get("soc_hold_enabled") and soc_extra > 0.005:
        notices.append(
            f"Speicherziel {targets.get('soc_hold_percent')} % über "
            f"{int(targets.get('soc_hold_horizon_hours') or 24)} h → +{soc_extra:.2f} kW in der Empfehlung."
        )
    elif targets.get("soc_hold_enabled"):
        notices.append(
            f"Speicherziel {targets.get('soc_hold_percent')} % aktiv "
            f"({int(targets.get('soc_hold_horizon_hours') or 24)} h). Aktuell kein Mehrzukauf nötig."
        )
    if len(rate_days) < MIN_PV_DAYS:
        alerts.append(
            f"Erst {len(rate_days)} Tag(e) mit PV und vollständigem Grundverbrauch. "
            "Der Zukauf ist deshalb noch unsicher und wird nicht mit einem Monatsmittel aufgefüllt."
        )
    elif len(rate_days) < 7:
        alerts.append(
            f"PV liegt nur für {len(rate_days)} Tage vor. Der Zukauf nutzt genau diese Tage, ohne Monats-Schätzung."
        )
    if hydro_positive == 0:
        notices.append(
            "Wasserkraft steht in den gespeicherten Tagen auf 0 kWh und wird nicht geschätzt."
        )
    if skipped_incomplete:
        notices.append(
            f"{skipped_incomplete} unvollständige Tage (unter {MIN_HOURS} Stunden Grundverbrauch) zählen nicht mit."
        )
    if avg_loss is not None:
        notices.append(
            f"Akku-Wirkungsgrad {eff:.0%}: zwischengespeicherte Energie verursacht im Schnitt "
            f"{avg_loss:.2f} kWh Verlust pro Tag. Genug Überschuss trägt den, dann bleibt der Zukauf 0."
        )
    notices.append(
        "Einspeisung bei vollem Speicher ist nicht als Extra-Zukauf eingerechnet. "
        "Dafür gibt es keine verlässliche Tagesreihe."
    )
    if continuous_kw <= 0:
        notices.append(
            "Kein Miner ist als durchgehend markiert. Historische Miner-Laufzeit steckt nicht im Grundverbrauch."
        )
    else:
        names = [
            f"{m['name']} ({m['power_kw']:.1f} kW)"
            for m in miner_out
            if m["mode"] == "continuous"
        ]
        notices.append(
            "Durchgehend gerechnet, mit der kW-Zahl aus dem Steuerungsalgorithmus: "
            + ", ".join(names)
            + "."
        )
    for miner in miner_out:
        if miner["mode"] != "off":
            continue
        if algo_enabled and miner["algo_enabled"]:
            alerts.append(
                f"{miner['name']} ist in dieser Rechnung aus. Der Steuerungsalgorithmus kann ihn bei schönem Wetter trotzdem einschalten. "
                "Diese Seite schaltet ihn nicht ab. Die Empfehlung gilt nur unter der Annahme, dass er nicht läuft."
            )
        else:
            notices.append(f"{miner['name']} ist in dieser Rechnung aus und zählt nicht zum Zukauf.")
    if weekly == 0:
        alerts.append(
            "Rest der letzten Tage ist 0 kW: die gemessene Erzeugung deckt Grundverbrauch und angenommene Dauerläufer inklusive Akkuverlust."
        )

    return {
        "info_only": True,
        "controls_devices": False,
        "weekly_kw": weekly,
        "daily_kw": locks.get("daily_kw"),
        "raw_kw": locks.get("raw_kw"),
        "base_raw_kw": round(base_raw_kw, 3) if base_raw_kw is not None else None,
        "weather_bump_kw": round(weather_bump, 3),
        "soc_hold_kw": round(soc_extra, 3),
        "weekly_kwh_per_day": round(weekly * HOURS, 1) if weekly is not None else None,
        "month_projected_kwh": round(weekly * HOURS * 30, 0) if weekly is not None else None,
        "week_id": locks.get("week_id"),
        "next_week_change": _next_week_date(today),
        "days_used": len(rate_days),
        "uncertain": len(rate_days) < 7,
        "season_shift": season_shift,
        "trend_base_kwh_per_week": round(trend, 2) if trend is not None else None,
        "trend_label": _trend_label(trend),
        "battery_efficiency": eff,
        "continuous_kw": round(continuous_kw, 2),
        "targets": targets,
        "weather": weather_meta,
        "soc_live": soc_live,
        "window": {
            "base_kwh": round(avg_base, 2) if avg_base is not None else None,
            "pv_kwh": round(avg_pv, 2) if avg_pv is not None else None,
            "hydro_kwh": round(avg_hydro, 2) if avg_hydro is not None else None,
            "grid_kwh": round(avg_grid, 2) if avg_grid is not None else None,
            "cycle_loss_kwh": round(avg_loss, 2) if avg_loss is not None else None,
            "continuous_kwh": round(continuous_kw * HOURS, 2),
        },
        "averages": {
            "today": today_block,
            "week": _avg_base(base_days[-7:]),
            "month": _avg_base(base_days[-30:]),
        },
        "base_history": _history_rows(base_days),
        "series": [
            {
                "date": row["date"][5:],
                "iso": row["date"],
                "base_kwh": row["base_kwh"],
                "pv_kwh": row["pv_kwh"],
                "hydro_kwh": row["hydro_kwh"],
                "gap_kwh": row["gap_kwh"],
                "used": row["used"],
            }
            for row in series
        ],
        "hourly": {
            "labels": [f"{h:02d}" for h in range(HOURS)],
            "history_avg": history_avg,
            "today": today_hourly,
        },
        "miners": miner_out,
        "algo_enabled": algo_enabled,
        "alerts": alerts,
        "notices": notices,
        "formula": (
            "Zukauf = was der Speicher nach PV, Wasser und Akku-Wirkungsgrad vom Grundverbrauch "
            "plus durchgehende Miner nicht mehr decken kann, gemittelt über die letzten Tage mit PV-Messung."
        ),
        "_locks": locks,
        "_modes": normalized_modes,
    }


def _history_rows(base_days: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in base_days:
        item: Dict[str, Any] = {
            "date": row["date"].isoformat() if hasattr(row["date"], "isoformat") else str(row["date"])[:10],
            "base_kwh": round(_f(row.get("base_kwh")), 2),
        }
        if row.get("pv_kwh") is not None:
            item["pv_kwh"] = round(_f(row.get("pv_kwh")), 2)
        rows.append(item)
    return rows


def merge_base_archive(
    previous: Sequence[Dict[str, Any]],
    fresh: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Behält Tages-Grundverbrauch auch dann, wenn die Verbrauchshistorie ältere Tage löscht."""
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in list(previous or []) + list(fresh or []):
        day = str(row.get("date") or "")[:10]
        if not day:
            continue
        slot = dict(by_date.get(day) or {"date": day})
        if row.get("base_kwh") is not None:
            slot["base_kwh"] = round(_f(row.get("base_kwh")), 2)
        if row.get("pv_kwh") is not None:
            slot["pv_kwh"] = round(_f(row.get("pv_kwh")), 2)
        by_date[day] = slot
    out: List[Dict[str, Any]] = []
    for day in sorted(by_date)[-BASE_ARCHIVE_DAYS:]:
        slot = by_date[day]
        if slot.get("base_kwh") is None:
            continue
        item = {"date": day, "base_kwh": slot["base_kwh"]}
        if slot.get("pv_kwh") is not None:
            item["pv_kwh"] = slot["pv_kwh"]
        out.append(item)
    return out


def _calendar_slots(
    by_date: Dict[str, Dict[str, Any]],
    start: date,
    end: date,
) -> List[Dict[str, Any]]:
    slots: List[Dict[str, Any]] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        src = by_date.get(iso)
        slots.append(
            {
                "date": iso,
                "base_kwh": None if src is None else src["base_kwh"],
                "pv_kwh": None if src is None else src.get("pv_kwh"),
            }
        )
        day += timedelta(days=1)
    return slots


def year_curve_slots(rows: Sequence[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    """Fenster der Jahreskurve: erster gemessener Tag bis gestern, höchstens 365 Tage.

    Jeder Kalendertag in diesem Fenster ist ein Punkt, kurze Lücken dazwischen
    bleiben leer. Tage vor der ersten Messung fehlen, damit die Kurve die
    Breite füllt und jeden Tag um einen Punkt wächst. Ab 365 Kalender- oder
    Messtagen bleiben die letzten 365 Tage stehen.
    """
    by_date = {row["date"]: row for row in merge_base_archive([], rows)}
    end = today - timedelta(days=1)
    measured = [
        date.fromisoformat(day)
        for day, row in by_date.items()
        if row.get("base_kwh") is not None and day <= end.isoformat()
    ]
    if not measured:
        return []
    first = min(measured)
    span = (end - first).days + 1
    if span >= YEAR_DAYS or len(measured) >= YEAR_DAYS:
        start = end - timedelta(days=YEAR_DAYS - 1)
        if start < first:
            start = first
    else:
        start = first
    return _calendar_slots(by_date, start, end)


def describe_history(rows: Sequence[Dict[str, Any]], today: date) -> Dict[str, Any]:
    """Kennzahlen über 365 Tage bis gestern. Die Jahreskurve nutzt das gewachsene Fenster."""
    by_date = {row["date"]: row for row in merge_base_archive([], rows)}
    end = today - timedelta(days=1)
    start = end - timedelta(days=YEAR_DAYS - 1)
    slots = _calendar_slots(by_date, start, end)
    measured = [slot for slot in slots if slot["base_kwh"] is not None]
    hungriest = max(measured, key=lambda slot: slot["base_kwh"]) if measured else None
    quietest = min(measured, key=lambda slot: slot["base_kwh"]) if measured else None
    weeks: Dict[str, List[float]] = {}
    for slot in measured:
        stamp = date.fromisoformat(slot["date"])
        iso = stamp.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        weeks.setdefault(key, []).append(slot["base_kwh"])
    week_rows = [
        {
            "week": key,
            "label": "KW " + key.split("-W")[-1],
            "base_kwh": round(sum(values) / len(values), 2),
            "days": len(values),
        }
        for key, values in sorted(weeks.items())
    ][-12:]
    sun_rows = [slot for slot in measured if slot.get("pv_kwh")]
    sun = None
    if sun_rows:
        base_mean = sum(slot["base_kwh"] for slot in sun_rows) / len(sun_rows)
        pv_mean = sum(slot["pv_kwh"] for slot in sun_rows) / len(sun_rows)
        covered = sum(1 for slot in sun_rows if slot["pv_kwh"] >= slot["base_kwh"])
        sun = {
            "days": len(sun_rows),
            "covered_days": covered,
            "base_kwh": round(base_mean, 2),
            "pv_kwh": round(pv_mean, 2),
            "cover": round(pv_mean / base_mean, 2) if base_mean > 0 else None,
        }
    year = year_curve_slots(rows, today)
    return {
        "year": year,
        "filled_days": len(measured),
        "year_days": len(year),
        "from": year[0]["date"] if year else None,
        "to": year[-1]["date"] if year else None,
        "weeks": week_rows,
        "highlights": {
            "hungriest": (
                {"date": hungriest["date"], "base_kwh": hungriest["base_kwh"]}
                if hungriest
                else None
            ),
            "quietest": (
                {"date": quietest["date"], "base_kwh": quietest["base_kwh"]}
                if quietest
                else None
            ),
        },
        "sun": sun,
    }


def _yield_maps(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in state.get("yield_history") or []:
        day = str(row.get("date") or "")[:10]
        if not day or row.get("excluded"):
            continue
        if row.get("actual") is None:
            continue
        out[day] = row
    return out


def _efficiency(state: Dict[str, Any]) -> float:
    try:
        from control_algorithm.soc_calibration import normalize_battery_efficiency

        return normalize_battery_efficiency(state.get("battery_efficiency"))
    except Exception:
        return min(1.0, max(0.5, _f(state.get("battery_efficiency"), 0.95) or 0.95))


def _miner_inputs(base_dir: str) -> Tuple[List[Dict[str, Any]], bool]:
    from control_algorithm.config import load_config, load_miners, merge_devices_from_miners

    cfg = load_config()
    miners = []
    for dev in merge_devices_from_miners(cfg, load_miners()):
        if dev.get("type") != "miner":
            continue
        miners.append(
            {
                "ip": dev.get("ip"),
                "name": dev.get("note") or dev.get("ip"),
                "power_kw": dev.get("power_kw") or 0,
                "algo_enabled": bool(dev.get("enabled")),
            }
        )
    return miners, bool(cfg.get("enabled"))


def _today_actual(base_dir: str, today: date) -> Dict[str, Any]:
    from control_algorithm.consumption_history import (
        integrate_base_miner_kwh_by_hour,
        integrate_kw_by_hour,
    )
    from energy_timeseries_24h import load_timeseries_state

    empty = {
        "date": today.isoformat(),
        "base_hourly": [0.0] * HOURS,
        "hydro_hourly": [0.0] * HOURS,
        "pv_hourly": None,
        "base_so_far_kwh": 0.0,
        "pv_so_far_kwh": None,
        "hours_elapsed": 0.0,
        "today_hourly_actual": [None] * HOURS,
    }
    try:
        ts = load_timeseries_state(base_dir)
    except Exception:
        return empty
    if str(ts.get("date") or "")[:10] != today.isoformat():
        return empty
    samples = list(ts.get("samples") or [])
    if len(samples) < 2:
        return empty
    _total, _miner, base = integrate_base_miner_kwh_by_hour(samples)
    generation = integrate_kw_by_hour(samples, "erzeugung")
    last_m = max(_f(s.get("m")) for s in samples)
    hour_now = int(last_m // 60)
    actual: List[Optional[float]] = [None] * HOURS
    for hour in range(min(HOURS, hour_now + 1)):
        actual[hour] = round(base[hour], 4)
    return {
        "date": today.isoformat(),
        "base_hourly": [0.0] * HOURS,
        "hydro_hourly": [0.0] * HOURS,
        "pv_hourly": None,
        "base_so_far_kwh": round(sum(base[: hour_now + 1]), 4),
        "pv_so_far_kwh": round(sum(generation[: hour_now + 1]), 4),
        "hours_elapsed": round(max(last_m, 1.0) / 60.0, 3),
        "today_hourly_actual": actual,
    }


def load_days(base_dir: str, today: Optional[date] = None) -> Tuple[List[Dict[str, Any]], float]:
    from control_algorithm.config import load_state
    from control_algorithm.consumption_history import load_history

    today = today or date.today()
    state = load_state()
    yields = _yield_maps(state)
    history = load_history(base_dir)
    days: List[Dict[str, Any]] = []
    for row in history.get("days") or []:
        day = str(row.get("date") or "")[:10]
        if not day or day >= today.isoformat():
            continue
        pv_hourly = None
        if day in yields:
            pv_hourly = hours_from_map(
                yields[day].get("actual_hours"),
                _f(yields[day].get("actual")),
            )
        days.append(
            {
                "date": day,
                "base_hourly": _hourly_or_zero(row.get("hourly_base_kwh")),
                "hydro_hourly": _hourly_or_zero(row.get("hourly_hydro_kwh")),
                "pv_hourly": pv_hourly,
            }
        )
    days.append(_today_actual(base_dir, today))
    return days, _efficiency(state)


def _algo_lat_lon() -> Tuple[Optional[float], Optional[float]]:
    try:
        from control_algorithm.config import load_config

        cfg = load_config()
        lat = cfg.get("lat")
        lon = cfg.get("lon")
        if lat is None or lon is None:
            return None, None
        return float(lat), float(lon)
    except Exception:
        return None, None


def _daily_sun_outlook(hourly: Dict[str, Any], today: date) -> List[Dict[str, Any]]:
    from control_algorithm.weather_scale import hour_relative_sun

    times = list(hourly.get("time") or [])
    codes = list(hourly.get("weather_code") or [])
    clouds = list(hourly.get("cloud_cover") or [])
    shortwave = list(hourly.get("shortwave_radiation") or [])
    sunshine = list(hourly.get("sunshine_duration") or [])
    buckets: Dict[str, List[float]] = {}
    for i, stamp in enumerate(times):
        try:
            day = str(stamp)[:10]
            hour = int(str(stamp)[11:13])
        except Exception:
            continue
        try:
            day_date = date.fromisoformat(day)
        except ValueError:
            continue
        offset = (day_date - today).days
        if offset < 0 or offset >= WEATHER_FORECAST_DAYS:
            continue
        # Tageslichtfenster: sonst verzerren Nachstunden den Mittelwert.
        if hour < 8 or hour > 17:
            continue
        wc = codes[i] if i < len(codes) else None
        cc = clouds[i] if i < len(clouds) else None
        sw = shortwave[i] if i < len(shortwave) else None
        sd = sunshine[i] if i < len(sunshine) else None
        try:
            wc_i = int(wc) if wc is not None else None
        except (TypeError, ValueError):
            wc_i = None
        factor = hour_relative_sun(
            weather_code=wc_i,
            cloud_cover=cc,
            shortwave_radiation=sw,
            sunshine_duration=sd,
        )
        buckets.setdefault(day, []).append(factor)
    rows: List[Dict[str, Any]] = []
    for offset in range(WEATHER_FORECAST_DAYS):
        day = (today + timedelta(days=offset)).isoformat()
        vals = buckets.get(day) or []
        mean = round(sum(vals) / len(vals), 3) if vals else None
        rows.append({"date": day, "offset": offset, "sun_factor": mean, "samples": len(vals)})
    return rows


def weather_worsening_score(days: Sequence[Dict[str, Any]]) -> float:
    """0 = gleich/besser, 1 = stark schlechter werdend über die Woche."""
    early = [d["sun_factor"] for d in days[:3] if d.get("sun_factor") is not None]
    late = [d["sun_factor"] for d in days[4:7] if d.get("sun_factor") is not None]
    if len(early) < 2 or len(late) < 2:
        return 0.0
    early_m = sum(early) / len(early)
    late_m = sum(late) / len(late)
    if early_m <= 0.05:
        return 0.0
    drop = (early_m - late_m) / early_m
    return round(max(0.0, min(1.0, drop)), 3)


def weather_bump_from_score(base_kw: Optional[float], score: float, enabled: bool) -> float:
    if not enabled or base_kw is None or score <= 0.02:
        return 0.0
    bump = float(base_kw) * WEATHER_BUMP_FRAC * float(score)
    return round(max(0.0, bump), 3)


def fetch_week_weather(
    state: Dict[str, Any],
    today: date,
    *,
    force: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Holt/cached Open-Meteo für 7 rollierende Tage. Gibt (meta, neuer_cache) zurück."""
    cache = dict(state.get("weather_cache") or {})
    now = time.time()
    cached_at = _f(cache.get("fetched_at"), 0.0)
    if (
        not force
        and cache.get("ok")
        and cache.get("anchor_date") == today.isoformat()
        and now - cached_at < WEATHER_CACHE_SEC
        and isinstance(cache.get("days"), list)
    ):
        return {
            "ok": True,
            "source": "open-meteo",
            "forecast_days": WEATHER_FORECAST_DAYS,
            "cached": True,
            "days": cache.get("days"),
            "worsening_score": _f(cache.get("worsening_score")),
            "fetched_at": cache.get("fetched_at_iso"),
        }, cache

    lat, lon = _algo_lat_lon()
    if lat is None or lon is None:
        meta = {"ok": False, "error": "Keine Koordinaten im Steuerungsalgorithmus.", "forecast_days": WEATHER_FORECAST_DAYS}
        return meta, cache
    try:
        from control_algorithm.weather import fetch_forecast

        payload = fetch_forecast(lat, lon, forecast_days=WEATHER_FORECAST_DAYS)
        days = _daily_sun_outlook(payload.get("hourly") or {}, today)
        score = weather_worsening_score(days)
        cache = {
            "ok": True,
            "anchor_date": today.isoformat(),
            "fetched_at": now,
            "fetched_at_iso": datetime.now().isoformat(timespec="seconds"),
            "days": days,
            "worsening_score": score,
            "lat": lat,
            "lon": lon,
        }
        meta = {
            "ok": True,
            "source": "open-meteo",
            "forecast_days": WEATHER_FORECAST_DAYS,
            "cached": False,
            "days": days,
            "worsening_score": score,
            "fetched_at": cache["fetched_at_iso"],
            "lat": lat,
            "lon": lon,
        }
        return meta, cache
    except Exception as exc:
        meta = {
            "ok": False,
            "error": str(exc) or "Wetterabruf fehlgeschlagen",
            "forecast_days": WEATHER_FORECAST_DAYS,
        }
        return meta, cache


def _storage_kwh() -> Optional[float]:
    try:
        from control_algorithm.config import load_config

        val = load_config().get("storage_kwh")
        if val is None:
            return None
        return max(1.0, float(val))
    except Exception:
        return None


def _live_soc_percent() -> Optional[float]:
    try:
        from dashboard_battery import load_battery_data

        raw = load_battery_data().get("soc")
        if raw is None:
            return None
        return max(0.0, min(100.0, float(str(raw).replace("%", "").replace(",", "."))))
    except Exception:
        return None


def soc_hold_extra_kw(
    targets: Dict[str, Any],
    *,
    efficiency: float,
    current_soc: Optional[float],
    storage_kwh: Optional[float],
) -> Tuple[float, Dict[str, Any]]:
    """Dauer-kW, um den Ziel-SoC im gewählten Fenster zu erreichen/halten."""
    info: Dict[str, Any] = {
        "enabled": bool(targets.get("soc_hold_enabled")),
        "target_percent": targets.get("soc_hold_percent"),
        "horizon_hours": int(targets.get("soc_hold_horizon_hours") or 24),
        "current_soc": current_soc,
        "storage_kwh": storage_kwh,
        "gap_percent": None,
        "needed_kwh": None,
        "extra_kw": 0.0,
    }
    if not info["enabled"]:
        return 0.0, info
    if current_soc is None or storage_kwh is None:
        info["note"] = "SoC oder Speicherkapazität fehlt."
        return 0.0, info
    target = float(targets.get("soc_hold_percent") or 5.0)
    horizon = 48 if int(targets.get("soc_hold_horizon_hours") or 24) >= 48 else 24
    gap = max(0.0, target - float(current_soc))
    info["gap_percent"] = round(gap, 2)
    if gap <= 0.05:
        info["note"] = "Ziel bereits erreicht oder überschritten."
        return 0.0, info
    eff = min(1.0, max(0.5, float(efficiency)))
    needed = (gap / 100.0) * float(storage_kwh) / eff
    extra = needed / float(horizon)
    info["needed_kwh"] = round(needed, 3)
    info["extra_kw"] = round(extra, 3)
    info["horizon_hours"] = horizon
    return round(extra, 3), info


def _clamp_kw(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_step(value: float, step: float) -> float:
    return math.floor((value / step) + 0.5) * step


def _first_sun_hour(hours: Any) -> Optional[int]:
    best: Optional[int] = None
    if not isinstance(hours, dict):
        return None
    for key, val in hours.items():
        try:
            hour = int(str(key).replace("h", ""))
            kwh = float(val or 0)
        except (TypeError, ValueError):
            continue
        if kwh < PURCHASE_PV_HOUR_KWH:
            continue
        if best is None or hour < best:
            best = hour
    return best


def hours_until_morning_sun(now: datetime, today_hours: Any, tomorrow_hours: Any) -> float:
    """Stunden bis zur ersten Stunde mit mindestens 0,1 kWh Sonne. Mindestens 1."""
    today_first = _first_sun_hour(today_hours)
    if today_first is not None and now.hour < today_first:
        target = now.replace(hour=today_first, minute=0, second=0, microsecond=0)
    else:
        morning = _first_sun_hour(tomorrow_hours)
        if morning is None:
            morning = 7
        target = (now + timedelta(days=1)).replace(
            hour=morning, minute=0, second=0, microsecond=0
        )
    return max(1.0, (target - now).total_seconds() / 3600.0)


def purchase_setpoint_kw(
    *,
    soc_percent: Optional[float],
    storage_kwh: Optional[float],
    min_soc_percent: float,
    daily_base_kwh: Optional[float],
    tomorrow_pv_kwh: Optional[float],
    hours_until_sun: float,
    soc_mean_3d: Optional[float] = None,
    target_soc: float = PURCHASE_SOC_TARGET,
) -> Dict[str, Any]:
    """Empfohlener Dauer-Zukauf in kW.

    P_soc gleicht den Speicher langsam auf das Ziel aus (168 h, Totzone,
    begrenzt). P_hist macht dasselbe mit dem Mittel der letzten drei Tage.
    P_fcst setzt die Tageslast gegen die PV von morgen. Der Minsock-Boden
    hebt das Ergebnis, wenn die Nacht sonst unter die Grenze fällt.
    Angezeigt wird das gerundete, auf 0…2 kW geklemmte Ergebnis.
    """
    empty = {
        "kw": None,
        "p_soc": 0.0,
        "p_hist": 0.0,
        "p_fcst": 0.0,
        "p_raw": None,
        "p_floor": 0.0,
        "hours_until_sun": None,
        "daily_base_kwh": None,
    }
    if soc_percent is None or storage_kwh is None or storage_kwh <= 0:
        return empty
    storage = float(storage_kwh)
    horizon = max(1.0, float(hours_until_sun))
    gap = float(target_soc) - float(soc_percent)
    if abs(gap) <= PURCHASE_SOC_DEADBAND:
        p_soc = 0.0
    else:
        p_soc = _clamp_kw(gap / 100.0 * storage / PURCHASE_SOC_HOURS, -PURCHASE_SOC_LIMIT, PURCHASE_SOC_LIMIT)
    if soc_mean_3d is None:
        p_hist = 0.0
    else:
        hist_gap = float(target_soc) - float(soc_mean_3d)
        p_hist = _clamp_kw(
            hist_gap / 100.0 * storage / PURCHASE_HIST_HOURS,
            -PURCHASE_HIST_LIMIT,
            PURCHASE_HIST_LIMIT,
        )
    daily = None if daily_base_kwh is None else max(0.0, float(daily_base_kwh))
    if daily is None or tomorrow_pv_kwh is None:
        p_fcst = 0.0
    else:
        p_fcst = _clamp_kw(
            (daily - float(tomorrow_pv_kwh)) / HOURS,
            PURCHASE_FCST_MIN,
            PURCHASE_FCST_MAX,
        )
    p_raw = p_soc + p_hist + p_fcst
    p_floor = 0.0
    if daily is not None:
        e_load = (daily / HOURS) * horizon
        e_above = (float(soc_percent) - float(min_soc_percent)) / 100.0 * storage
        buffer_kwh = PURCHASE_SOC_DEADBAND / 100.0 * storage
        e_floor = max(0.0, e_load + buffer_kwh - e_above)
        if e_floor >= PURCHASE_FLOOR_SKIP_KWH:
            p_floor = e_floor / (PURCHASE_FLOOR_EFF * horizon)
    held = _clamp_kw(max(p_raw, p_floor), 0.0, PURCHASE_MAX_KW)
    shown = round(_round_step(held, PURCHASE_STEP_KW), 2)
    return {
        "kw": shown,
        "p_soc": round(p_soc, 3),
        "p_hist": round(p_hist, 3),
        "p_fcst": round(p_fcst, 3),
        "p_raw": round(p_raw, 3),
        "p_floor": round(p_floor, 3),
        "hours_until_sun": round(horizon, 2),
        "daily_base_kwh": round(daily, 2) if daily is not None else None,
    }


def _recent_daily_base_kwh(algo_state: Dict[str, Any], days: Sequence[Dict[str, Any]], today: date) -> Optional[float]:
    bases: List[float] = []
    for row in ((algo_state.get("energy_week_plan") or {}).get("days") or [])[:3]:
        try:
            value = float((row.get("daily_totals") or {}).get("base_kwh") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            bases.append(value)
    if bases:
        return sum(bases) / len(bases)
    totals: List[float] = []
    for day in days:
        try:
            day_date = date.fromisoformat(str(day.get("date") or "")[:10])
        except ValueError:
            continue
        if day_date >= today:
            continue
        hourly = _hourly_or_zero(day.get("base_hourly"))
        total = sum(hourly)
        if _measured_hours(hourly) >= MIN_HOURS and total > 0:
            totals.append(total)
    last = totals[-3:]
    if not last:
        return None
    return sum(last) / len(last)


def _complete_base_totals(
    algo_state: Dict[str, Any],
    days: Sequence[Dict[str, Any]],
    today: date,
    lookback: int,
) -> List[float]:
    """Grundverbrauch der letzten vollständigen Tage, neueste zuerst aus dem Wochenplan."""
    bases: List[float] = []
    for row in (algo_state.get("energy_week_plan") or {}).get("days") or []:
        try:
            value = float((row.get("daily_totals") or {}).get("base_kwh") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            bases.append(value)
        if len(bases) >= lookback:
            break
    if len(bases) >= max(3, lookback // 2):
        return bases[:lookback]
    totals: List[float] = []
    for day in days:
        try:
            day_date = date.fromisoformat(str(day.get("date") or "")[:10])
        except ValueError:
            continue
        if day_date >= today:
            continue
        hourly = _hourly_or_zero(day.get("base_hourly"))
        total = sum(hourly)
        if _measured_hours(hourly) >= MIN_HOURS and total > 0:
            totals.append(total)
    return totals[-lookback:]


def hard_min_soc_percent(raw: Any) -> float:
    """Mindest-SoC aus der Steuerung, Korridor 30–35 %. Keine Planungsbänder."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = MIN_SOC_HARD_HIGH
    return _clamp_kw(value, MIN_SOC_HARD_LOW, MIN_SOC_HARD_HIGH)


def forward_purchase_kw(
    *,
    soc_percent: Optional[float],
    storage_kwh: Optional[float],
    min_soc_percent: float,
    daily_base_kwh: Optional[float],
    base_load_scale: float,
    tomorrow_pv_kwh: Optional[float],
    day_after_pv_kwh: Optional[float],
    forecast_trust: float,
    hours_until_sun: float,
) -> Dict[str, Any]:
    """Ruhiger Dauer-Zukauf: Lastbasis, PV-Gutschrift, Speicher, Nachtboden.

    Last = 7-Tage-Mittel × base_load_scale.
    PV-Gutschrift = Mischung morgen/übermorgen × Trust, gedeckelt bei der Last.
    Speicher-Entlastung = Headroom über Min-SoC+Puffer, gestreckt auf 2 Tage.
    Roh-kW = Restdefizit / 24. Boden greift nur, wenn sonst Min-SoC reißt.
    """
    empty: Dict[str, Any] = {
        "kw": None,
        "p_raw": None,
        "p_floor": 0.0,
        "p_held": None,
        "load_kwh": None,
        "pv_credit_kwh": None,
        "relief_kwh": None,
        "deficit_kwh": None,
        "headroom_kwh": None,
        "min_soc_percent": None,
        "hours_until_sun": None,
        "short": False,
    }
    if daily_base_kwh is None or daily_base_kwh <= 0:
        return empty
    if storage_kwh is None or storage_kwh <= 0 or soc_percent is None:
        return empty
    min_soc = hard_min_soc_percent(min_soc_percent)
    try:
        scale = float(base_load_scale)
    except (TypeError, ValueError):
        scale = 1.0
    scale = max(0.5, min(2.0, scale))
    load_kwh = max(0.0, float(daily_base_kwh) * scale)
    trust = _clamp_kw(_f(forecast_trust, 0.85), 0.3, 1.0)
    pv_tom = max(0.0, _f(tomorrow_pv_kwh))
    pv_after = max(0.0, _f(day_after_pv_kwh))
    if tomorrow_pv_kwh is None and day_after_pv_kwh is None:
        pv_blend = 0.0
    elif day_after_pv_kwh is None:
        pv_blend = pv_tom
    elif tomorrow_pv_kwh is None:
        pv_blend = pv_after
    else:
        pv_blend = FORWARD_PV_TOMORROW_W * pv_tom + FORWARD_PV_DAY_AFTER_W * pv_after
    pv_credit = min(load_kwh, pv_blend * trust)
    storage = float(storage_kwh)
    headroom = max(0.0, (float(soc_percent) - min_soc) / 100.0 * storage)
    buffer_kwh = FORWARD_BUFFER_SOC / 100.0 * storage
    usable = max(0.0, headroom - buffer_kwh)
    relief = usable / FORWARD_RELIEF_DAYS
    deficit = max(0.0, load_kwh - pv_credit - relief)
    p_raw = deficit / HOURS
    horizon = max(1.0, float(hours_until_sun))
    e_load = (load_kwh / HOURS) * horizon
    e_above = max(0.0, (float(soc_percent) - min_soc) / 100.0 * storage)
    e_floor = max(0.0, e_load + buffer_kwh - e_above)
    p_floor = 0.0
    if e_floor >= PURCHASE_FLOOR_SKIP_KWH:
        p_floor = e_floor / (PURCHASE_FLOOR_EFF * horizon)
    held = _clamp_kw(max(p_raw, p_floor), 0.0, PURCHASE_MAX_KW)
    shown = round(_round_step(held, PURCHASE_STEP_KW), 2)
    return {
        "kw": shown,
        "p_raw": round(p_raw, 3),
        "p_floor": round(p_floor, 3),
        "p_held": round(held, 3),
        "load_kwh": round(load_kwh, 2),
        "pv_credit_kwh": round(pv_credit, 2),
        "relief_kwh": round(relief, 2),
        "deficit_kwh": round(deficit, 2),
        "headroom_kwh": round(headroom, 2),
        "min_soc_percent": round(min_soc, 1),
        "hours_until_sun": round(horizon, 2),
        "short": bool(p_floor > p_raw + 0.02),
        "base_load_scale": round(scale, 4),
        "forecast_trust": round(trust, 3),
        "pv_blend_kwh": round(pv_blend, 2),
    }


def apply_forward_smooth(
    prev: Optional[Dict[str, Any]],
    raw_kw: Optional[float],
    floor_kw: float,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Aktualisiert den Dauerzukauf bei jedem Auslesen, mit Schritt-Glättung gegen Schwanken."""
    now = now or datetime.now()
    prev = dict(prev or {})
    try:
        prev_kw = float(prev["kw"]) if prev.get("kw") is not None else None
    except (TypeError, ValueError):
        prev_kw = None
    if raw_kw is None:
        return {
            "kw": prev_kw,
            "raw_kw": None,
            "floor_kw": round(max(0.0, float(floor_kw)), 3),
            "updated": False,
            "updated_at": prev.get("updated_at"),
            "floored": False,
        }
    target = max(0.0, float(raw_kw))
    floor = max(0.0, float(floor_kw))
    aimed = max(target, floor)
    floored = floor > target + 0.005
    smoothed = _clamp_step(prev_kw, aimed, FORWARD_SMOOTH_REL, FORWARD_SMOOTH_ABS)
    held = _clamp_kw(smoothed, 0.0, PURCHASE_MAX_KW)
    shown = round(_round_step(held, PURCHASE_STEP_KW), 2)
    changed = prev_kw is None or abs(shown - float(prev_kw)) >= 0.005
    return {
        "kw": shown,
        "raw_kw": round(target, 3),
        "floor_kw": round(floor, 3),
        "smoothed_kw": round(smoothed, 3),
        "updated": changed,
        "updated_at": now.isoformat(timespec="seconds"),
        "floored": floored,
    }


def _forward_for_report(
    days: Sequence[Dict[str, Any]],
    today: date,
    current_soc: Optional[float],
    storage_kwh: Optional[float],
    prev_forward: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Baut den angezeigten Dauer-Zukauf aus Steuerungsdaten und Glättung."""
    bases: List[float] = []
    try:
        from control_algorithm.config import load_config, load_state
        from control_algorithm.soc_calibration import normalize_base_load_scale

        algo = load_state()
        cfg = load_config()
        min_soc = hard_min_soc_percent(cfg.get("min_soc_percent"))
        if storage_kwh is None:
            raw_storage = cfg.get("storage_kwh")
            storage_kwh = None if raw_storage is None else float(raw_storage)
        bases = _complete_base_totals(algo, days, today, FORWARD_LOOKBACK_DAYS)
        daily = sum(bases) / len(bases) if bases else None
        scale = normalize_base_load_scale(algo.get("base_load_scale"))
        trust = _f(algo.get("forecast_trust"), 0.85)
        raw_tom = algo.get("pv_yield_tomorrow_kwh")
        raw_after = algo.get("pv_yield_day_after_kwh")
        tomorrow_pv = None if raw_tom is None else float(raw_tom)
        day_after_pv = None if raw_after is None else float(raw_after)
        horizon = hours_until_morning_sun(
            datetime.now(),
            algo.get("pv_forecast_today_hours"),
            algo.get("pv_forecast_tomorrow_hours"),
        )
    except Exception:
        return {"kw": None, "short": False, "parts": {}, "lock": {}, "days_used": 0}
    parts = forward_purchase_kw(
        soc_percent=current_soc,
        storage_kwh=storage_kwh,
        min_soc_percent=min_soc,
        daily_base_kwh=daily,
        base_load_scale=scale,
        tomorrow_pv_kwh=tomorrow_pv,
        day_after_pv_kwh=day_after_pv,
        forecast_trust=trust,
        hours_until_sun=horizon,
    )
    lock = apply_forward_smooth(
        prev_forward,
        parts.get("p_raw") if parts.get("p_raw") is not None else parts.get("kw"),
        _f(parts.get("p_floor")),
    )
    if lock.get("kw") is not None:
        parts = dict(parts)
        parts["kw"] = lock["kw"]
        parts["smoothed_kw"] = lock.get("smoothed_kw")
        parts["floored"] = lock.get("floored")
        parts["days_used"] = len(bases)
    return {
        "kw": lock.get("kw"),
        "short": bool(parts.get("short") or lock.get("floored")),
        "parts": parts,
        "lock": lock,
        "days_used": len(bases),
    }


def _purchase_for_report(
    days: Sequence[Dict[str, Any]],
    today: date,
    current_soc: Optional[float],
    storage_kwh: Optional[float],
) -> Dict[str, Any]:
    """Liest Prognose und Tageslast einmal und rechnet den angezeigten Zukauf."""
    try:
        from control_algorithm.config import load_config, load_state

        algo = load_state()
        cfg = load_config()
        min_soc = hard_min_soc_percent(cfg.get("min_soc_percent"))
        if storage_kwh is None:
            raw_storage = cfg.get("storage_kwh")
            storage_kwh = None if raw_storage is None else float(raw_storage)
        raw_pv = algo.get("pv_yield_tomorrow_kwh")
        tomorrow_pv = None if raw_pv is None else float(raw_pv)
        horizon = hours_until_morning_sun(
            datetime.now(),
            algo.get("pv_forecast_today_hours"),
            algo.get("pv_forecast_tomorrow_hours"),
        )
        daily = _recent_daily_base_kwh(algo, days, today)
    except Exception:
        return {"kw": None}
    return purchase_setpoint_kw(
        soc_percent=current_soc,
        storage_kwh=storage_kwh,
        min_soc_percent=min_soc,
        daily_base_kwh=daily,
        tomorrow_pv_kwh=tomorrow_pv,
        hours_until_sun=horizon,
    )


def month_shape_factor(daily_kwh: Sequence[float]) -> float:
    """Leichter Sprung: der letzte Monatstag zieht proportional am Mittel.

    Halb der Abstand zum Mittel, halb der Schritt vom Vortag. Begrenzt auf
    ±12 %, damit der Zukauf mit dem Haushalt mitgeht, ohne zu springen.
    Nächsten Monat gilt dieselbe Regel auf dessen ersten Tagen.
    """
    if len(daily_kwh) < 2:
        return 1.0
    mean = sum(float(v) for v in daily_kwh) / len(daily_kwh)
    if mean <= 0.05:
        return 1.0
    latest = float(daily_kwh[-1])
    prev = float(daily_kwh[-2])
    jump = 0.5 * ((latest / mean) - 1.0) + 0.5 * ((latest - prev) / mean)
    return 1.0 + _clamp_kw(jump, -MONTH_SHAPE_LIMIT, MONTH_SHAPE_LIMIT)


def weather_consumption_factor(sun_factors: Sequence[Any]) -> Tuple[float, Optional[float]]:
    """Hebt oder senkt den Zukauf nach dem Tageslicht der nächsten Woche.

    1,0 bei einem hellen Tag (Mittel etwa 0,45). Dunkler hebt, heller senkt,
    höchstens +35 % und −15 %.
    """
    vals: List[float] = []
    for raw in sun_factors:
        if raw is None:
            continue
        try:
            vals.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not vals or CLEAR_DAYLIGHT_SUN <= 0:
        return 1.0, None
    sun = sum(vals) / len(vals)
    tilt = WEATHER_SWING * (CLEAR_DAYLIGHT_SUN - sun) / CLEAR_DAYLIGHT_SUN
    return _clamp_kw(1.0 + tilt, WEATHER_FACTOR_MIN, WEATHER_FACTOR_MAX), sun


def month_open_zukauf_kw(daily_kwh: Sequence[float], sun_factors: Sequence[Any]) -> Optional[float]:
    """kW aus dem Monatsanfang, leicht skaliert, mal Wetterfaktor der nächsten Woche."""
    usable = [max(0.0, float(v)) for v in daily_kwh if v is not None and float(v) > 0]
    if not usable:
        return None
    shape = month_shape_factor(usable)
    factor, _sun = weather_consumption_factor(sun_factors)
    shaped = (sum(usable) / len(usable)) * shape
    return round(max(0.0, shaped / HOURS * factor), 2)


def _opening_month_rows(days: Sequence[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    """Vollständige Tage unter den ersten drei Kalendertagen des Monats."""
    start = today.replace(day=1)
    end = start + timedelta(days=MONTH_OPEN_DAYS)
    picked: List[Dict[str, Any]] = []
    for day in days:
        try:
            day_date = date.fromisoformat(str(day.get("date") or "")[:10])
        except ValueError:
            continue
        if day_date < start or day_date >= end or day_date >= today:
            continue
        hourly = _hourly_or_zero(day.get("base_hourly"))
        total = sum(hourly)
        if _measured_hours(hourly) < MIN_HOURS or total <= 0:
            continue
        pv_raw = day.get("pv_hourly")
        pv = _hourly_or_zero(pv_raw) if isinstance(pv_raw, list) and len(pv_raw) >= HOURS else None
        night = 0.0
        for hour in range(HOURS):
            if pv is not None:
                is_night = pv[hour] < PURCHASE_PV_HOUR_KWH
            else:
                is_night = hour < 7 or hour >= 18
            if is_night:
                night += hourly[hour]
        picked.append({"date": day_date, "base_kwh": total, "night_kwh": night})
    picked.sort(key=lambda row: row["date"])
    return picked


def month_open_for_report(
    days: Sequence[Dict[str, Any]],
    today: date,
    weather_meta: Optional[Dict[str, Any]],
    current_soc: Optional[float],
    storage_kwh: Optional[float],
    min_soc_percent: Optional[float],
) -> Dict[str, Any]:
    """Nur die Anzeige. Der Dauer-Zukauf ins BMS liest diese Zahl nicht."""
    empty: Dict[str, Any] = {
        "kw": None,
        "short": False,
        "shape": None,
        "weather_factor": None,
        "sun_mean": None,
        "daily_kwh": None,
        "night_kwh": None,
        "days_used": 0,
        "headroom_kwh": None,
    }
    rows = _opening_month_rows(days, today)
    if not rows:
        return empty
    bases = [float(row["base_kwh"]) for row in rows]
    shape = month_shape_factor(bases)
    suns = [
        row.get("sun_factor")
        for row in ((weather_meta or {}).get("days") or [])
        if isinstance(row, dict)
    ]
    factor, sun = weather_consumption_factor(suns)
    mean = sum(bases) / len(bases)
    night_mean = sum(float(row["night_kwh"]) for row in rows) / len(rows)
    night_shaped = night_mean * shape
    headroom = None
    short = False
    if current_soc is not None and storage_kwh is not None and storage_kwh > 0 and min_soc_percent is not None:
        headroom = (float(current_soc) - float(min_soc_percent)) / 100.0 * float(storage_kwh)
        short = night_shaped > max(0.0, headroom)
    return {
        "kw": month_open_zukauf_kw(bases, suns),
        "short": short,
        "shape": round(shape, 3),
        "weather_factor": round(factor, 3),
        "sun_mean": None if sun is None else round(sun, 3),
        "daily_kwh": round(mean, 2),
        "night_kwh": round(night_shaped, 2),
        "days_used": len(rows),
        "headroom_kwh": None if headroom is None else round(headroom, 2),
    }


def get_report(base_dir: str) -> Dict[str, Any]:
    path = _state_path(base_dir)
    today = date.today()
    with _lock:
        state = _load_state(path)
        modes = {
            str(k): v
            for k, v in (state.get("assumptions") or {}).items()
            if v in MODES
        }
        miners, algo_on = _miner_inputs(base_dir)
        days, eff = load_days(base_dir, today)
        targets = dict(state.get("targets") or default_targets())
        weather_meta, weather_cache = fetch_week_weather(state, today)
        state["weather_cache"] = weather_cache
        # Vorläufige Historien-Rechnung ohne Bumps, nur für Base-raw.
        prelim = build_report_from_parts(
            days,
            miners,
            modes,
            efficiency=eff,
            today=today,
            prev=state.get("recommendation") or {},
            algo_enabled=algo_on,
            targets=targets,
        )
        base_raw = prelim.get("base_raw_kw")
        if base_raw is None:
            base_raw = prelim.get("raw_kw")
        score = _f(weather_meta.get("worsening_score"))
        weather_bump = weather_bump_from_score(
            base_raw,
            score,
            bool(targets.get("weather_adjust_enabled", True)),
        )
        current_soc = _live_soc_percent()
        storage = _storage_kwh()
        soc_extra, soc_live = soc_hold_extra_kw(
            targets,
            efficiency=eff,
            current_soc=current_soc,
            storage_kwh=storage,
        )
        report = build_report_from_parts(
            days,
            miners,
            modes,
            efficiency=eff,
            today=today,
            prev=state.get("recommendation") or {},
            algo_enabled=algo_on,
            weather_bump_kw=weather_bump,
            soc_hold_kw=soc_extra,
            weather_meta=weather_meta,
            targets=targets,
            soc_live=soc_live,
        )
        state["assumptions"] = report.pop("_modes")
        state["recommendation"] = report.pop("_locks")
        archive = merge_base_archive(state.get("base_archive"), report.get("base_history"))
        state["base_archive"] = archive
        report["base_history"] = archive
        report.update(describe_history(archive, today))
        # Angezeigter Dauer-Zukauf: bei jedem Auslesen aktuell, BMS-Schreiben unverändert.
        # weekly/daily bleibt der Rest der letzten Tage und geht nicht ins Register.
        prev_forward = (
            state.get("forward_purchase")
            if isinstance(state.get("forward_purchase"), dict)
            else {}
        )
        forward = _forward_for_report(days, today, current_soc, storage, prev_forward)
        lock = forward.get("lock") or {}
        state["forward_purchase"] = {
            "kw": lock.get("kw"),
            "updated_at": lock.get("updated_at"),
            "raw_kw": lock.get("raw_kw"),
            "floor_kw": lock.get("floor_kw"),
        }
        report["recommended_kw"] = lock.get("kw")
        state["recommended_kw"] = lock.get("kw")
        report["recommended_short"] = bool(forward.get("short"))
        parts = dict(forward.get("parts") or {})
        parts["days_used"] = forward.get("days_used")
        parts["updated_at"] = lock.get("updated_at")
        report["recommended_parts"] = parts
        if lock.get("kw") is not None:
            report["formula"] = (
                "Empfohlener Zukauf = (7-Tage-Grundverbrauch × base_load_scale "
                "− PV-Gutschrift aus morgen/übermorgen × forecast_trust, gedeckelt bei der Last "
                "− Speicher-Headroom über Mindest-SoC plus Puffer, gestreckt auf 2 Tage) / 24. "
                "Nachtboden nur wenn sonst das Mindest-SoC (30–35 %, dynamisch aus der Steuerung) "
                "vor der nächsten PV reißt. Glättung max. 12 % oder 0,15 kW. "
                "Die Anzeige wird bei jedem Auslesen aktualisiert. "
                "Nur bei eingeschaltetem Schalter Planung, keine Netzspitzen geht dieser Wert ins BMS."
            )
        _save_state(path, state)
        return report


def save_assumptions(base_dir: str, modes: Dict[str, Any]) -> Dict[str, Any]:
    miners, _algo = _miner_inputs(base_dir)
    allowed = {str(m.get("ip")) for m in miners}
    clean: Dict[str, str] = {}
    for ip, mode in (modes or {}).items():
        ip_s = str(ip)
        if ip_s not in allowed:
            continue
        if mode not in MODES:
            raise ValueError("Unbekannter Betrieb")
        clean[ip_s] = str(mode)
    path = _state_path(base_dir)
    with _lock:
        state = _load_state(path)
        merged = {
            str(k): v
            for k, v in (state.get("assumptions") or {}).items()
            if v in MODES and str(k) in allowed
        }
        merged.update(clean)
        state["assumptions"] = merged
        _save_state(path, state)
    return get_report(base_dir)


def save_targets(base_dir: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    path = _state_path(base_dir)
    with _lock:
        state = _load_state(path)
        targets = dict(state.get("targets") or default_targets())
        if "soc_hold_enabled" in updates:
            targets["soc_hold_enabled"] = bool(updates["soc_hold_enabled"])
        if "weather_adjust_enabled" in updates:
            targets["weather_adjust_enabled"] = bool(updates["weather_adjust_enabled"])
        if "soc_hold_percent" in updates:
            try:
                targets["soc_hold_percent"] = max(1.0, min(95.0, float(updates["soc_hold_percent"])))
            except (TypeError, ValueError):
                raise ValueError("Ungültiges Speicherziel")
        if "soc_hold_horizon_hours" in updates:
            try:
                horizon = int(updates["soc_hold_horizon_hours"])
            except (TypeError, ValueError):
                raise ValueError("Ungültiges Zeitfenster")
            if horizon not in SOC_HOLD_HORIZONS:
                raise ValueError("Zeitfenster muss 24 oder 48 Stunden sein")
            targets["soc_hold_horizon_hours"] = horizon
        state["targets"] = targets
        _save_state(path, state)
    return get_report(base_dir)


def recommended_import_kw(base_dir: str) -> Optional[float]:
    """Gespeicherter angezeigter Zukauf in kW, ohne neue Rechnung.

    Das ist der Wert der Karte Empfohlener Zukauf. Der Rest der letzten Tage
    (weekly/daily, früher 0,02 kW) wird hier nicht gelesen.
    """
    path = _state_path(base_dir)
    with _lock:
        raw = _load_state(path).get("recommended_kw")
    try:
        if raw is None:
            return None
        return round(max(0.0, float(raw)), 3)
    except (TypeError, ValueError):
        return None


def tick(base_dir: str) -> None:
    try:
        get_report(base_dir)
    except Exception:
        pass
