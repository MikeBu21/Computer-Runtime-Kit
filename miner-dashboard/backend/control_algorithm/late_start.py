# -*- coding: utf-8 -*-
"""Spätestmögliche Miner-Aktivierung (Risikosenkung ohne Zielverletzung)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.oms_pv_forecast import LAST_PV_HOUR_THRESHOLD_KWH
from control_algorithm.planner import enabled_devices, sort_for_resume

FIRST_PV_HOUR_THRESHOLD_KWH = 0.15
LATE_START_BUFFER_HOURS = 0.35


def first_pv_hour_from_hours(
    hours: Dict[str, Any],
    threshold: float = FIRST_PV_HOUR_THRESHOLD_KWH,
) -> Optional[int]:
    """Erste Stunde mit nennenswerter PV-Prognose."""
    best: Optional[int] = None
    for hid, val in (hours or {}).items():
        if float(val or 0) < threshold:
            continue
        try:
            h = int(str(hid).replace("h", ""))
        except ValueError:
            continue
        if best is None or h < best:
            best = h
    return best


def _hours_for_date(state: Dict[str, Any], d: date) -> Dict[str, float]:
    by_date = state.get("pv_forecast_by_date") or {}
    entry = by_date.get(d.isoformat()) or {}
    hours = entry.get("hours") or {}
    if hours:
        return {str(k): float(v or 0) for k, v in hours.items()}
    today = datetime.now().date()
    offset = (d - today).days
    if offset == 0:
        hours = state.get("pv_forecast_today_hours") or {}
    elif offset == 1:
        hours = state.get("pv_forecast_tomorrow_hours") or {}
    elif offset == 2:
        hours = state.get("pv_forecast_day_after_hours") or {}
    return {str(k): float(v or 0) for k, v in (hours or {}).items()}


def next_pv_start_deadline(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[Optional[datetime], str]:
    """
    Nächster Zeitpunkt, ab dem PV nennenswert startet
    (heute Resttag oder morgen früh).
    """
    now = now or datetime.now()
    today = now.date()
    hours_today = _hours_for_date(state, today)
    first_today = first_pv_hour_from_hours(hours_today)
    if first_today is not None:
        dt = datetime.combine(today, datetime.min.time()).replace(
            hour=first_today, minute=0
        )
        if dt > now + timedelta(minutes=20):
            return dt, f"PV-Start heute ~{first_today:02d}:00 (Prognose)"

    tomorrow = today + timedelta(days=1)
    hours_tm = _hours_for_date(state, tomorrow)
    first_tm = first_pv_hour_from_hours(hours_tm)
    if first_tm is None:
        first_tm = 7
    dt = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=first_tm, minute=0
    )
    return dt, f"PV-Start morgen ~{first_tm:02d}:00 (Prognose)"


def _anchor_miner_kw(config: Dict[str, Any]) -> float:
    devices = enabled_devices(config)
    if not devices:
        return 0.0
    ranked = sort_for_resume(devices)
    return float(ranked[0].get("power_kw") or 0)


def _target_soc_for_defer(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    projection: Dict[str, Any],
) -> Tuple[float, str]:
    """Ziel-SoC, auf den vor PV-Start entladen werden soll (Headroom)."""
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)

    drain = state.get("morning_drain_plan") or {}
    if drain.get("ok") and drain.get("gate_active") and drain.get("target_soc_percent") is not None:
        try:
            tgt = float(drain["target_soc_percent"])
            tgt = max(min_soc, min(max_soc, tgt))
            return tgt, "Laufreserve-Ziel"
        except (TypeError, ValueError):
            pass

    candidates: List[Tuple[float, str]] = []

    today_rec = (
        (state.get("autopilot_soc_plan") or {}).get("today_recommendation")
        or {}
    )
    if today_rec.get("recommended_soc_percent") is None:
        try:
            from control_algorithm.config import BACKEND_DIR
            from control_algorithm.energy_balance import (
                today_end_of_production_recommendation,
            )

            today_rec = today_end_of_production_recommendation(
                config, state, live, str(BACKEND_DIR)
            )
        except Exception:
            today_rec = {}
    if today_rec.get("recommended_soc_percent") is not None:
        candidates.append(
            (
                float(today_rec["recommended_soc_percent"]),
                "Autopilot-Empfehlung (Produktionsende)",
            )
        )

    bind = projection.get("binding_checkpoint") or {}
    if bind.get("target_soc") is not None:
        try:
            candidates.append(
                (float(bind["target_soc"]), "bindender Checkpoint")
            )
        except (TypeError, ValueError):
            pass

    if projection.get("target_soc") is not None:
        try:
            candidates.append(
                (float(projection["target_soc"]), "Horizont-Ziel")
            )
        except (TypeError, ValueError):
            pass

    # Proaktives Headroom-Ziel (morgens Platz für PV)
    try:
        from control_algorithm.energy_balance import (
            _morning_soc_target,
            _proactive_headroom_soc,
        )

        morning = _morning_soc_target(config)
        storage = float(config.get("storage_kwh") or 0)
        tomorrow_pv = float(state.get("pv_yield_tomorrow_kwh") or 0)
        headroom = _proactive_headroom_soc(
            max_soc,
            tomorrow_pv,
            storage_kwh=storage,
            morning_target=morning,
        )
        candidates.append((float(headroom), "Headroom vor morgiger PV"))
        candidates.append((float(morning), "Morgen-Minimum"))
    except Exception:
        candidates.append((max(min_soc, 40.0), "Fallback-Reserve"))

    # Höchstes sinnvolles Ziel unter Max: wir wollen Platz schaffen → niedrigeres Ziel
    # Für Entladen: das relevante Headroom-Ziel (nicht Min-SoC allein).
    usable = [
        (v, lab)
        for v, lab in candidates
        if min_soc <= v <= max_soc + 0.05
    ]
    if not usable:
        return min_soc + 5.0, "Min-SoC + Puffer"
    # Niedrigstes Headroom-Ziel unter aktuellem SoC bevorzugt (mehr Entladen nötig)
    soc = live.get("soc_percent")
    if soc is not None:
        below = [(v, lab) for v, lab in usable if v < float(soc) - 1.0]
        if below:
            # Am ambitioniertesten (niedrigstes Ziel) = mehr Zeit nötig = späterer Start knapper
            # Für Spätstart brauchen wir das Ziel, das die Planung wirklich will:
            # typisch Autopilot-Empfehlung oder Headroom.
            preferred_labels = (
                "Laufreserve-Ziel",
                "Autopilot-Empfehlung (Produktionsende)",
                "Headroom vor morgiger PV",
                "bindender Checkpoint",
                "Horizont-Ziel",
            )
            for lab in preferred_labels:
                for v, l in below:
                    if l == lab:
                        return v, l
            return min(below, key=lambda x: x[0])
    return max(usable, key=lambda x: x[0])


def compute_late_start_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    *,
    projection: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    urgent_export: bool = False,
) -> Dict[str, Any]:
    """
    Spätestmöglicher Start für Entladen aus dem Speicher.

    - Bei Live-Export / starker PV-Aufnahme: kein Aufschub (urgent).
    - Sonst: nur starten, wenn nötig und Zeitpunkt = Deadline − Laufzeit − Puffer.
    """
    now = now or datetime.now()
    projection = projection or state.get("balance_projection") or {}
    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    soc = live.get("soc_percent")
    pv_kw = float(live.get("pv_kw") or 0)
    miner_kw = _anchor_miner_kw(config)

    empty = {
        "defer": False,
        "urgent": bool(urgent_export),
        "latest_start": None,
        "deadline": None,
        "target_soc_percent": None,
        "excess_kwh": 0.0,
        "hours_needed": 0.0,
        "miner_kw": miner_kw,
        "reason": None,
        "summary": None,
    }

    if urgent_export:
        empty["summary"] = "Spätstart ausgesetzt – Live-Export / dringende Einspeisung."
        empty["reason"] = "urgent_export"
        return empty

    # Tagsüber mit genug PV: Algo schaltet wie geplant (Surplus), kein Nacht-Defer
    if pv_kw >= 1.0:
        empty["summary"] = (
            "Spätstart nicht aktiv – PV läuft, Steuerung nach Budget/Export."
        )
        empty["reason"] = "daytime_pv"
        return empty

    if soc is None or storage <= 0 or miner_kw <= 0.05:
        empty["reason"] = "no_data"
        return empty

    deadline, deadline_note = next_pv_start_deadline(state, now)
    if deadline is None or deadline <= now:
        empty["reason"] = "no_deadline"
        empty["summary"] = "Kein künftiger PV-Start gefunden – kein Spätstart-Defer."
        return empty

    target_soc, target_label = _target_soc_for_defer(
        config, state, live, projection
    )
    target_soc = max(min_soc, min(max_soc, float(target_soc)))
    current = float(soc)
    excess_kwh = max(0.0, (current - target_soc) / 100.0 * storage)

    if excess_kwh < 0.4:
        # Nichts / kaum zu entladen – Geräte auslassen bis Surplus/Export
        return {
            "defer": True,
            "urgent": False,
            "latest_start": deadline.isoformat(),
            "deadline": deadline.isoformat(),
            "deadline_note": deadline_note,
            "target_soc_percent": round(target_soc, 1),
            "target_label": target_label,
            "current_soc_percent": round(current, 1),
            "excess_kwh": round(excess_kwh, 2),
            "hours_needed": 0.0,
            "miner_kw": round(miner_kw, 3),
            "reason": "no_excess",
            "summary": (
                f"Spätstart: kein Entladen nötig (SoC {current:.0f} % ≤ Ziel "
                f"{target_soc:.0f} % / {target_label}). Warten bis PV/Export."
            ),
        }

    hours_needed = excess_kwh / miner_kw
    latest_start = deadline - timedelta(
        hours=hours_needed + LATE_START_BUFFER_HOURS
    )
    # Nicht vor „jetzt“ in die Vergangenheit planen ohne Start
    hours_until_deadline = max(
        0.0, (deadline - now).total_seconds() / 3600.0
    )
    defer = now < latest_start

    # Wenn bis Deadline nicht genug Zeit: sofort starten (Ziel sonst verfehlt)
    if hours_until_deadline < hours_needed:
        defer = False
        reason = "must_start_now"
        summary = (
            f"Spätstart: sofort – bis {deadline.strftime('%H:%M')} nur noch "
            f"{hours_until_deadline:.1f} h, benötigt ~{hours_needed:.1f} h "
            f"({excess_kwh:.1f} kWh auf Ziel {target_soc:.0f} %)."
        )
    elif defer:
        reason = "wait_until_latest"
        summary = (
            f"Spätstart: warten bis ~{latest_start.strftime('%H:%M')} "
            f"(Deadline {deadline.strftime('%d.%m. %H:%M')}: {deadline_note}). "
            f"Entladen {excess_kwh:.1f} kWh → {target_soc:.0f} % ({target_label}) "
            f"mit ~{miner_kw:.1f} kW in {hours_needed:.1f} h + Puffer."
        )
    else:
        reason = "latest_window_open"
        summary = (
            f"Spätstart-Fenster offen ab jetzt – Start erlaubt, "
            f"Ziel {target_soc:.0f} % bis {deadline.strftime('%H:%M')} "
            f"({excess_kwh:.1f} kWh / {hours_needed:.1f} h)."
        )

    return {
        "defer": defer,
        "urgent": False,
        "latest_start": latest_start.isoformat(),
        "deadline": deadline.isoformat(),
        "deadline_note": deadline_note,
        "target_soc_percent": round(target_soc, 1),
        "target_label": target_label,
        "current_soc_percent": round(current, 1),
        "excess_kwh": round(excess_kwh, 2),
        "hours_needed": round(hours_needed, 2),
        "hours_until_deadline": round(hours_until_deadline, 2),
        "miner_kw": round(miner_kw, 3),
        "buffer_hours": LATE_START_BUFFER_HOURS,
        "reason": reason,
        "summary": summary,
    }
