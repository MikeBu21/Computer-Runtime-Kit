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
MODES = ("continuous", "off")


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


def _empty_state() -> Dict[str, Any]:
    return {"assumptions": {}, "recommendation": {}, "base_archive": []}


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

    if assumption_changed or daily_prev is None:
        daily = round(raw_kw, 3)
        daily_updated = True
    elif prev.get("daily_id") != daily_id:
        daily = _clamp_step(daily_prev, raw_kw, DAILY_STEP_REL, DAILY_STEP_ABS)
        daily_updated = True
    else:
        daily = round(daily_prev, 3)
        daily_updated = False

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
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _avg_base(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"days": 0, "base_kwh": None, "base_kw": None}
    base_mean = sum(r["base_kwh"] for r in rows) / len(rows)
    return {
        "days": len(rows),
        "base_kwh": round(base_mean, 2),
        "base_kw": round(base_mean / HOURS, 3),
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
) -> Dict[str, Any]:
    """Rechnet den Bericht aus vorbereiteten Tagen. Schaltet nichts."""
    prev = dict(prev or {})
    eff = min(1.0, max(0.5, _f(efficiency, 0.95)))
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
            "Empfohlener Zukauf ist 0 kW: die gemessene Erzeugung deckt Grundverbrauch und angenommene Dauerläufer inklusive Akkuverlust."
        )

    return {
        "info_only": True,
        "controls_devices": False,
        "weekly_kw": weekly,
        "daily_kw": locks.get("daily_kw"),
        "raw_kw": locks.get("raw_kw"),
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


def describe_history(rows: Sequence[Dict[str, Any]], today: date) -> Dict[str, Any]:
    """365 Kalendertage bis gestern. Fehlende Tage bleiben leer und füllen sich später."""
    by_date = {row["date"]: row for row in merge_base_archive([], rows)}
    end = today - timedelta(days=1)
    start = end - timedelta(days=YEAR_DAYS - 1)
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
    measured = [slot for slot in slots if slot["base_kwh"] is not None]

    def _window(end_day: date, length: int = 7) -> Tuple[Optional[float], int]:
        begin = end_day - timedelta(days=length - 1)
        values = []
        cursor = begin
        while cursor <= end_day:
            src = by_date.get(cursor.isoformat())
            if src and src.get("base_kwh") is not None:
                values.append(src["base_kwh"])
            cursor += timedelta(days=1)
        if not values:
            return None, 0
        return round(sum(values) / len(values), 2), len(values)

    compare = None
    if measured:
        last = date.fromisoformat(measured[-1]["date"])
        recent, recent_n = _window(last)
        earlier, earlier_n = _window(last - timedelta(days=28))
        delta = None
        if recent is not None and earlier is not None:
            delta = round(recent - earlier, 2)
        compare = {
            "recent_kwh": recent,
            "recent_days": recent_n,
            "earlier_kwh": earlier,
            "earlier_days": earlier_n,
            "delta_kwh": delta,
        }

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
        sun = {
            "days": len(sun_rows),
            "base_kwh": round(base_mean, 2),
            "pv_kwh": round(pv_mean, 2),
            "cover": round(pv_mean / base_mean, 2) if base_mean > 0 else None,
        }
    return {
        "year": slots,
        "filled_days": len(measured),
        "year_days": len(slots),
        "from": slots[0]["date"] if slots else None,
        "to": slots[-1]["date"] if slots else None,
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
            "compare": compare,
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
        report = build_report_from_parts(
            days,
            miners,
            modes,
            efficiency=eff,
            today=today,
            prev=state.get("recommendation") or {},
            algo_enabled=algo_on,
        )
        state["assumptions"] = report.pop("_modes")
        state["recommendation"] = report.pop("_locks")
        archive = merge_base_archive(state.get("base_archive"), report.get("base_history"))
        state["base_archive"] = archive
        report["base_history"] = archive
        report.update(describe_history(archive, today))
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


def tick(base_dir: str) -> None:
    try:
        get_report(base_dir)
    except Exception:
        pass
