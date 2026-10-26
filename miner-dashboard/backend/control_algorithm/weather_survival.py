# -*- coding: utf-8 -*-
"""Wetter-Klassifikation und dynamische SOC-Ziele für Mehrtages-Steuerung."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.reference_anchor import effective_perfect_day_kwh

GOOD_PV_RATIO = 0.60
NORMAL_PV_RATIO = 0.30
PRE_STORM_CHARGE_HOUR = 18
# Autonome Mindest-SoC-Checkpoints (ohne manuelle Uhrzeit-Ziele)
AUTONOMOUS_MORNING_HOUR = 6
AUTONOMOUS_EVENING_HOUR = 18
AUTONOMOUS_EVENING_BUFFER_PCT = 2.0

SOURCE_LABELS: Dict[str, str] = {
    "autonomous_min_soc": "Morgen-Minimum",
    "autonomous_evening_reserve": "Abend-Reserve",
    "pre_storm_charge": "Schlechtwetter-Vorladung",
    "user": "Manuelles Ziel",
}


def _overnight_reserve_calc(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Berechnung der autonomen Abend-Reserve (Nachtverbrauch im Speicher)."""
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    storage = float(config.get("storage_kwh") or 0)
    try:
        evening_aspirational = float(config.get("evening_soc_percent") or 90.0)
    except (TypeError, ValueError):
        evening_aspirational = 90.0
    evening_aspirational = max(min_soc, min(max_soc, evening_aspirational))
    overnight_kwh = 0.0
    avg_base_daily = None
    base_source = "Fallback 2 kW × 12 h Nacht"
    try:
        week = (state.get("energy_week_plan") or {}).get("days") or []
        bases = [
            float((d.get("daily_totals") or {}).get("base_kwh") or 0)
            for d in week[:3]
        ]
        bases = [b for b in bases if b > 0]
        if bases:
            avg_base_daily = sum(bases) / len(bases)
            if storage > 0:
                overnight_kwh = avg_base_daily * (12.0 / 24.0)
                base_source = (
                    f"Wochenplan Ø {avg_base_daily:.1f} kWh/Tag "
                    f"(12 h Nacht = {overnight_kwh:.1f} kWh)"
                )
    except Exception:
        pass
    if overnight_kwh <= 0 and storage > 0:
        overnight_kwh = 2.0 * 12.0
    overnight_pct = 0.0
    if storage > 0:
        overnight_pct = min(max_soc - min_soc, (overnight_kwh / storage) * 100.0)
    # Harte Abend-Reserve = Nachtverbrauch; aspirational (~90 %) nur Kennzahl/weich
    evening_from_night = min(
        max_soc,
        round(min_soc + overnight_pct + AUTONOMOUS_EVENING_BUFFER_PCT, 1),
    )
    evening_target = evening_from_night
    return {
        "min_soc_percent": min_soc,
        "max_soc_percent": max_soc,
        "storage_kwh": storage,
        "overnight_kwh": round(overnight_kwh, 2),
        "overnight_soc_percent": round(overnight_pct, 2),
        "evening_reserve_percent": evening_target,
        "evening_aspirational_percent": evening_aspirational,
        "buffer_soc_percent": AUTONOMOUS_EVENING_BUFFER_PCT,
        "base_consumption_daily_kwh": round(avg_base_daily, 2) if avg_base_daily else None,
        "base_consumption_source": base_source,
        "morning_hour": AUTONOMOUS_MORNING_HOUR,
        "evening_hour": AUTONOMOUS_EVENING_HOUR,
    }


def build_autopilot_soc_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
    horizon_days: int = 7,
    base_dir: Optional[str] = None,
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Empfohlene SoC-Zeitpunkte für Dashboard: Autopilot + Prognose je Checkpoint.
    Kernkennzahl: heute bei Produktionsende (7-Tage Min/Max).
    """
    now = now or datetime.now()
    user_targets = config.get("soc_targets") or []
    reserve = _overnight_reserve_calc(config, state)
    merged = merged_soc_targets_in_horizon(config, state, now, horizon_days)
    projection = state.get("balance_projection") or state.get("horizon_projection") or {}
    proj_by_dt: Dict[str, Dict[str, Any]] = {}
    for cp in projection.get("checkpoints") or []:
        dt = str(cp.get("target_datetime") or "")
        if dt:
            proj_by_dt[dt] = cp

    today_rec: Dict[str, Any] = {}
    try:
        from control_algorithm.config import BACKEND_DIR
        from control_algorithm.energy_balance import today_end_of_production_recommendation

        today_rec = today_end_of_production_recommendation(
            config,
            state,
            live or {"soc_percent": state.get("live_soc"), "pv_kw": state.get("live_pv_kw")},
            base_dir or str(BACKEND_DIR),
            now,
        )
    except Exception as e:
        today_rec = {"error": str(e), "recommended_soc_percent": reserve.get("evening_reserve_percent")}

    if user_targets:
        mode = "manual"
        summary = (
            "Manuelle SoC-Uhrzeiten aktiv – Autopilot ergänzt dynamische "
            "Schlechtwetter-Vorladung und Horizont-Schutz."
        )
    else:
        mode = "autonomous"
        rec_pct = today_rec.get("recommended_soc_percent")
        rec_time = today_rec.get("at_time") or f"{reserve['evening_hour']:02d}:00"
        summary = (
            f"Heute empfohlen bei Produktionsende (~{rec_time}): "
            f"{rec_pct if rec_pct is not None else '—'} % "
            f"(7-Tage-Plan ≥ {reserve['min_soc_percent']:.0f} %, ≤ {reserve['max_soc_percent']:.0f} %)."
        )

    recommendations: List[Dict[str, Any]] = []
    for cp in merged[:24]:
        dt_raw = str(cp.get("target_datetime") or "")
        reason = cp.get("dynamic_reason")
        if reason in SOURCE_LABELS:
            source = reason
            source_label = SOURCE_LABELS[reason]
        elif user_targets:
            source = "user"
            source_label = SOURCE_LABELS["user"]
        else:
            source = "merged"
            source_label = "Planungs-Checkpoint"

        recommended = float(cp.get("soc_percent") or 0)
        proj = proj_by_dt.get(dt_raw) or {}
        projected = proj.get("projected_soc")
        reachable = proj.get("reachable")
        if projected is None and dt_raw:
            reachable = None

        if source == "autonomous_evening_reserve":
            expl = (
                "Horizontrechnung: SoC bei PV-Ende, damit Folgetage ≥ Min-SoC bleiben "
                f"(Deckel Max-SoC). Nacht-Fallback ~{reserve['evening_reserve_percent']:.0f} %."
            )
        elif source == "autonomous_min_soc":
            expl = "Untergrenze Min-SoC – Speicher darf in der Nacht nicht darunter fallen."
        elif source == "pre_storm_charge":
            expl = "Vollladen vor erwarteter PV-Schwäche (Survival-Modus)."
        elif source == "user":
            expl = "Von Ihnen im Dashboard hinterlegtes SoC-Ziel."
        else:
            expl = "Zusammengeführt aus Planung und Horizont-Simulation."

        try:
            cp_dt = datetime.fromisoformat(dt_raw)
            date_str = cp_dt.date().isoformat()
            time_str = cp.get("time") or cp_dt.strftime("%H:%M")
        except Exception:
            date_str = dt_raw[:10] if len(dt_raw) >= 10 else ""
            time_str = str(cp.get("time") or "—")

        recommendations.append(
            {
                "date": date_str,
                "time": time_str,
                "target_datetime": dt_raw,
                "recommended_soc_percent": round(recommended, 1),
                "projected_soc_percent": (
                    round(float(projected), 1) if projected is not None else None
                ),
                "reachable": reachable,
                "source": source,
                "source_label": source_label,
                "explanation": expl,
                "minutes_until": cp.get("minutes_until"),
            }
        )

    binding = projection.get("binding_checkpoint")
    calc_note = (
        "Heute-Empfehlung = kleinster SoC am PV-Ende, bei dem die 7-Tage-Simulation "
        f"nie unter {reserve['min_soc_percent']:.0f} % fällt und {reserve['max_soc_percent']:.0f} % "
        "nicht als Ziel überschreitet. Schwache Folgetage → höhere Empfehlung "
        "(bis Max-SoC); starke Folgetage → niedriger, oft nahe Nachtreserve."
    )

    return {
        "mode": mode,
        "summary": summary,
        "today_recommendation": today_rec,
        "calculation": {**reserve, "horizon_days": horizon_days, "note": calc_note},
        "recommendations": recommendations,
        "binding_checkpoint": binding,
        "min_soc_in_horizon": projection.get("min_soc_in_horizon"),
        "projected_soc_at_binding": projection.get("projected_soc"),
        "miner_budget_kw": projection.get("miner_budget_kw"),
        "binding_constraint": projection.get("binding_constraint"),
        "updated_at": now.isoformat(),
    }


def daily_pv_kwh_for_date(state: Dict[str, Any], d: date, today: Optional[date] = None) -> float:
    """Tages-PV kWh aus pv_forecast_by_date oder Legacy-Keys."""
    today = today or datetime.now().date()
    offset = (d - today).days
    if offset == 0:
        try:
            from control_algorithm.pv_pattern_estimate import effective_today_pv_kwh

            return float(effective_today_pv_kwh(state))
        except Exception:
            return float(state.get("pv_yield_today_kwh") or 0)
    by_date = state.get("pv_forecast_by_date") or {}
    entry = by_date.get(d.isoformat())
    if entry:
        return float(entry.get("daily_kwh") or 0)
    if offset == 1:
        return float(state.get("pv_yield_tomorrow_kwh") or 0)
    if offset == 2:
        return float(state.get("pv_yield_day_after_kwh") or 0)
    return 0.0


def last_pv_hour_for_date(state: Dict[str, Any], d: date) -> int:
    by_date = state.get("pv_forecast_by_date") or {}
    entry = by_date.get(d.isoformat()) or {}
    if entry.get("last_pv_hour") is not None:
        return int(entry["last_pv_hour"])
    hours = entry.get("hours") or {}
    if not hours:
        offset = (d - datetime.now().date()).days
        if offset == 0:
            hours = state.get("pv_forecast_today_hours") or {}
        elif offset == 1:
            hours = state.get("pv_forecast_tomorrow_hours") or {}
        elif offset == 2:
            hours = state.get("pv_forecast_day_after_hours") or {}
    last = 0
    for hid, val in (hours or {}).items():
        if float(val or 0) < 0.1:
            continue
        try:
            last = max(last, int(str(hid).replace("h", "")))
        except ValueError:
            continue
    return last or 20


def classify_weather_tier(
    daily_pv_kwh: float,
    reference_kwh: float,
) -> str:
    if reference_kwh <= 0:
        return "normal"
    ratio = daily_pv_kwh / reference_kwh
    if ratio >= GOOD_PV_RATIO:
        return "good"
    if ratio >= NORMAL_PV_RATIO:
        return "normal"
    return "bad"


def classify_weather_window(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    days: int = 7,
    start_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Klassifikation für Tag 0 … days-1 ab start_date (default heute)."""
    start = start_date or datetime.now().date()
    ref = effective_perfect_day_kwh(state, config)
    result: List[Dict[str, Any]] = []
    for offset in range(days):
        d = start + timedelta(days=offset)
        pv = daily_pv_kwh_for_date(state, d, start)
        tier = classify_weather_tier(pv, ref)
        result.append(
            {
                "date": d.isoformat(),
                "offset": offset,
                "pv_kwh": round(pv, 2),
                "reference_kwh": round(ref, 2),
                "weather_tier": tier,
                "last_pv_hour": last_pv_hour_for_date(state, d),
            }
        )
    return result


def detect_bad_streak(
    window: List[Dict[str, Any]],
    *,
    from_offset: int = 1,
) -> Tuple[int, int]:
    """
    Längste aufeinanderfolgende bad-Tage ab from_offset.
    Returns (start_offset, length) or (0, 0).
    """
    best_start = 0
    best_len = 0
    cur_start = 0
    cur_len = 0
    for day in window:
        off = int(day.get("offset") or 0)
        if off < from_offset:
            continue
        if day.get("weather_tier") == "bad":
            if cur_len == 0:
                cur_start = off
            cur_len += 1
            if cur_len > best_len:
                best_len = cur_len
                best_start = cur_start
        else:
            cur_len = 0
    return best_start, best_len


def steering_mode_for_day(
    day: Dict[str, Any],
    *,
    bad_streak_start: int,
    bad_streak_len: int,
    offset: int,
) -> str:
    tier = day.get("weather_tier") or "normal"
    if bad_streak_len >= 2 and bad_streak_start <= offset < bad_streak_start + bad_streak_len:
        return "survival_mode"
    if bad_streak_len >= 1 and offset == bad_streak_start - 1 and tier in ("good", "normal"):
        return "pre_storm_charge"
    if tier == "good":
        return "normal"
    if tier == "bad":
        return "survival_mode"
    return "normal"


def dynamic_soc_targets_for_horizon(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
    horizon_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Zusätzliche SOC-Checkpoints für Schlechtwetter-Vorbereitung.
    Ergänzt Benutzer-Checkpoints (z. B. 07:00 @ 40%).
    """
    now = now or datetime.now()
    max_soc = float(config.get("max_soc_percent") or 95.0)
    window = classify_weather_window(config, state, days=horizon_days, start_date=now.date())
    bad_start, bad_len = detect_bad_streak(window)

    extras: List[Dict[str, Any]] = []
    horizon_end = now + timedelta(days=horizon_days)

    if bad_len >= 1 and bad_start >= 1:
        pre_day = window[bad_start - 1] if bad_start - 1 < len(window) else None
        if pre_day:
            charge_hour = max(PRE_STORM_CHARGE_HOUR, int(pre_day.get("last_pv_hour") or PRE_STORM_CHARGE_HOUR))
            charge_dt = datetime.combine(
                date.fromisoformat(str(pre_day["date"])),
                datetime.min.time(),
            ).replace(hour=min(22, charge_hour), minute=0)
            if charge_dt <= now:
                charge_dt += timedelta(days=1)
            if charge_dt <= horizon_end:
                extras.append(
                    {
                        "time": charge_dt.strftime("%H:%M"),
                        "soc_percent": max_soc,
                        "target_datetime": charge_dt.isoformat(),
                        "minutes_until": int((charge_dt - now).total_seconds() / 60),
                        "dynamic_reason": "pre_storm_charge",
                    }
                )

    return extras


def autonomous_min_soc_targets_for_horizon(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
    horizon_days: int = 7,
    base_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Wenn keine manuellen SoC-Uhrzeiten gesetzt sind: tägliche Morgen-Mindest-Checkpoints.

    Abend-Ziel kommt bewusst NICHT mehr als Reserve-Checkpoint (das hat gegen die
    Füllkurve/Max-SoC um PV-Ende gekämpft). Abend-Soll steuert die Füllkurve
    (typisch Max-SoC ~86 % um ~20:00).
    """
    now = now or datetime.now()
    user = config.get("soc_targets") or []
    if user:
        return []

    reserve = _overnight_reserve_calc(config, state)
    min_soc = float(reserve["min_soc_percent"])
    max_soc = float(reserve["max_soc_percent"])
    try:
        morning_target = float(config.get("morning_soc_percent") or min_soc)
    except (TypeError, ValueError):
        morning_target = min_soc
    morning_target = max(min_soc, min(max_soc, morning_target))
    horizon_end = now + timedelta(days=horizon_days)
    extras: List[Dict[str, Any]] = []

    # Schönwetter: Morgen-Checkpoint auf morning_soc (~33 %), sonst harter Floor
    try:
        from control_algorithm.reference_anchor import effective_perfect_day_kwh

        ref = float(effective_perfect_day_kwh(state, config) or 0)
    except Exception:
        ref = float(config.get("pv_kwp") or 0) * float(
            config.get("perfect_weather_sun_hours") or 6.0
        )
    today_pv = float(state.get("pv_yield_today_kwh") or state.get("morning_pv_forecast_kwh") or 0)
    tomorrow_pv = float(state.get("pv_yield_tomorrow_kwh") or 0)

    day = now.date()
    end_day = horizon_end.date()
    while day <= end_day:
        morning_dt = datetime.combine(day, datetime.min.time()).replace(
            hour=AUTONOMOUS_MORNING_HOUR, minute=0
        )
        if now < morning_dt <= horizon_end:
            # Für morgen früh: tomorrow_pv; für heute (vor 06:00): today_pv
            if day == now.date():
                day_pv = today_pv
            elif day == now.date() + timedelta(days=1):
                day_pv = tomorrow_pv
            else:
                day_pv = 0.0  # ohne Detailprognose: harter Floor
            good_morning = ref > 0 and day_pv >= ref * GOOD_PV_RATIO
            morning_soc = morning_target if good_morning else min_soc
            extras.append(
                {
                    "time": f"{AUTONOMOUS_MORNING_HOUR:02d}:00",
                    "soc_percent": morning_soc,
                    "target_datetime": morning_dt.isoformat(),
                    "minutes_until": int((morning_dt - now).total_seconds() / 60),
                    "dynamic_reason": "autonomous_min_soc",
                }
            )
        day += timedelta(days=1)
    return extras


def merged_soc_targets_in_horizon(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
    horizon_days: int = 7,
) -> List[Dict[str, Any]]:
    """Benutzer- + dynamische + ggf. autonome SOC-Ziele; nie unter Mindest-SoC."""
    from control_algorithm.planner import soc_targets_in_horizon

    now = now or datetime.now()
    min_soc = float(config.get("min_soc_percent") or 35.0)
    user = soc_targets_in_horizon(config.get("soc_targets") or [], now, horizon_days)
    dynamic = dynamic_soc_targets_for_horizon(config, state, now, horizon_days)
    autonomous = autonomous_min_soc_targets_for_horizon(config, state, now, horizon_days)
    by_dt: Dict[str, Dict[str, Any]] = {}
    for cp in user + dynamic + autonomous:
        dt = str(cp.get("target_datetime") or "")
        if not dt:
            continue
        cp = dict(cp)
        try:
            pct = float(cp.get("soc_percent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        # Planungs-Floor: Ziel nie unter Mindest-SoC (z. B. 07:00 @ 25 % bei Min 28 %)
        if pct < min_soc:
            cp["soc_percent"] = min_soc
            cp["clamped_from"] = pct
        existing = by_dt.get(dt)
        if existing is None or float(cp.get("soc_percent") or 0) > float(
            existing.get("soc_percent") or 0
        ):
            by_dt[dt] = cp
    return sorted(by_dt.values(), key=lambda x: x["target_datetime"])


def weather_context(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    days: int = 7,
) -> Dict[str, Any]:
    """Kompakte Wetter-/Steuerungsübersicht für API und Scheduler."""
    window = classify_weather_window(config, state, days=days)
    bad_start, bad_len = detect_bad_streak(window)
    modes = [
        {**day, "steering_mode": steering_mode_for_day(day, bad_streak_start=bad_start, bad_streak_len=bad_len, offset=int(day.get("offset") or 0))}
        for day in window
    ]
    return {
        "days": modes,
        "bad_streak_start_offset": bad_start,
        "bad_streak_days": bad_len,
        "reference_kwh": round(effective_perfect_day_kwh(state, config), 2),
    }


def should_block_proactive_discharge(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> bool:
    """Kein Entladen vor sonnigem morgen, wenn danach Schlechtwetter folgt und SOC noch niedrig."""
    ctx = weather_context(config, state, days=7)
    bad_len = int(ctx.get("bad_streak_days") or 0)
    if bad_len < 2:
        return False
    max_soc = float(config.get("max_soc_percent") or 95.0)
    soc = live.get("soc_percent")
    if soc is None:
        return False
    return float(soc) < max_soc - 8


def live_grid_export_kw(live: Dict[str, Any]) -> float:
    """
    Aktuelle Netzeinspeisung in kW.

    Konvention im Dashboard: grid_kw < 0 = Einspeisung (Lieferung ans Netz).
    """
    try:
        grid = float(live.get("grid_kw") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, -grid)


def export_avoidance_recommended(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> bool:
    """
    Miner einschalten, um Einspeisung zu vermeiden / Max-SoC zu schützen.

    Priorität 1: Jede Live-Einspeisung über der Lern-Schwelle (Menge egal, kein PV-Gate)
                 solange Min-SoC sicher – Orientierung an eingestelltem Max-SoC.
    Priorität 2: SoC nahe/über Max-SoC (Oberkante) → Last, bevor/obwohl eingespeist wird.
    Priorität 3: Gelerntes Preempt / Wetter-Heuristik (mit PV).
    """
    from control_algorithm.steering_learning import (
        export_threshold_kw,
        soc_preempt_level,
    )

    pv_kw = float(live.get("pv_kw") or 0)
    soc = live.get("soc_percent")
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    export_kw = live_grid_export_kw(live)
    thr = export_threshold_kw(state)
    verbrauch_kw = float(live.get("verbrauch_kw") or 0)

    if soc is None:
        return False
    soc_f = float(soc)
    if soc_f < min_soc + 5.0:
        return False

    # Live-Einspeisung: Schwelle reicht – unabhängig von PV und Export-Höhe
    if export_kw >= thr:
        return True

    # Oberkante (eingestelltes Max-SoC): Steuerung muss greifen, bevor Versagen messbar ist
    if soc_f >= max_soc - 0.5:
        return True
    if soc_f >= max_soc - 3.0 and (pv_kw >= 0.2 or export_kw >= thr * 0.5):
        return True

    if not config.get("has_storage"):
        return False

    # Gelernt: vor dem Volllaufen einschalten (Puffer gegen Export-Spitzen)
    preempt = soc_preempt_level(config, state)
    surplus = max(0.0, pv_kw - verbrauch_kw)
    if soc_f >= preempt and pv_kw >= 0.5 and surplus >= 0.4:
        return True

    if pv_kw < 0.5:
        return False
    if soc_f < max_soc - 5:
        return False
    window = classify_weather_window(config, state, days=4)
    tomorrow = next((d for d in window if int(d.get("offset") or 0) == 1), None)
    if not tomorrow:
        return False
    # Auch Schönwetter: bei hohem SoC preemptiv Last, sonst droht Export
    return tomorrow.get("weather_tier") in ("bad", "normal", "good")
