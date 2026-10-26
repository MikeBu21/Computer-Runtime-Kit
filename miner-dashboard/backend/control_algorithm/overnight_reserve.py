# -*- coding: utf-8 -*-
"""
Nachtreserve-Neuron: dynamische Mindest-Speicherreserve für die Nacht.

Ziel: nicht mit zu wenig Reserve in die Tiefnacht gehen; unerwartete Lasten
abfedern; Entladen bevorzugt abends oder in der Morgendämmerung vor PV-Start.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

DEEP_NIGHT_START = 22
DEEP_NIGHT_END = 5
EVENING_START = 18
EVENING_END = 22
PREDawn_START = 5
DISCHARGE_FLOOR_BUFFER_PCT = 3.0
UNEXPECTED_CONSUMPTION_KW_MARGIN = 0.8


def overnight_reserve_margin_pct(state: Dict[str, Any]) -> float:
    try:
        from control_algorithm.steering_learning import get_steering_params

        return float(get_steering_params(state).get("overnight_reserve_margin_pct") or 4.0)
    except Exception:
        return 4.0


def _morning_floor(config: Dict[str, Any]) -> float:
    min_soc = float(config.get("min_soc_percent") or 35.0)
    try:
        morning = float(
            config.get("morning_soc_percent")
            if config.get("morning_soc_percent") is not None
            else min_soc
        )
    except (TypeError, ValueError):
        morning = min_soc
    return max(min_soc, morning)


def _overnight_reserve_soc(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[float, float, str]:
    """Reserve in %-Pkt über min_soc und kWh-Äquivalent."""
    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    margin = overnight_reserve_margin_pct(state)

    overnight_kwh = 0.0
    source = "Fallback"
    try:
        from control_algorithm.weather_survival import _overnight_reserve_calc

        calc = _overnight_reserve_calc(config, state) or {}
        overnight_kwh = float(calc.get("overnight_kwh") or 0)
        source = str(calc.get("base_consumption_source") or source)
    except Exception:
        pass
    if overnight_kwh <= 0:
        overnight_kwh = 2.0 * 12.0
        source = "Fallback 2 kW × 12 h"

    try:
        from control_algorithm.energy_balance import consumption_risk_scale

        risk_scale = float(consumption_risk_scale(config))
    except Exception:
        risk_scale = 1.0

    reserve_kwh = overnight_kwh * risk_scale
    if storage > 0:
        reserve_soc = min(max_soc - min_soc, (reserve_kwh / storage) * 100.0)
    else:
        reserve_soc = float(
            (_overnight_reserve_calc(config, state) or {}).get("overnight_soc_percent") or 8.0
        )
    reserve_soc = min(reserve_soc + margin, max_soc - min_soc - 2.0)
    return round(reserve_soc, 2), round(reserve_kwh, 2), source


def _phase_for_hour(hour: int, predawn_end_hour: int) -> str:
    if EVENING_START <= hour < EVENING_END:
        return "evening"
    if hour >= DEEP_NIGHT_START or hour < DEEP_NIGHT_END:
        return "deep_night"
    if PREDawn_START <= hour < predawn_end_hour:
        return "predawn"
    return "day"


def _predawn_end_hour(state: Dict[str, Any], now: datetime) -> int:
    try:
        from control_algorithm.late_start import first_pv_hour_from_hours, _hours_for_date

        tomorrow = now.date() + timedelta(days=1)
        first_tm = first_pv_hour_from_hours(_hours_for_date(state, tomorrow))
        if first_tm is not None:
            return max(PREDawn_START + 1, first_tm)
        first_today = first_pv_hour_from_hours(_hours_for_date(state, now.date()))
        if first_today is not None and first_today > now.hour:
            return max(PREDawn_START + 1, first_today)
    except Exception:
        pass
    return 7


def _tomorrow_pv_ok(config: Dict[str, Any], state: Dict[str, Any]) -> bool:
    try:
        from control_algorithm.anchor_stretch import _tomorrow_pv_ok

        ok, _, _ = _tomorrow_pv_ok(config, state)
        return bool(ok)
    except Exception:
        tomorrow = float(state.get("pv_yield_tomorrow_kwh") or 0)
        return tomorrow >= 35.0


def compute_overnight_reserve_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Berechnet Phasen, SoC-Floors und Entlade-Caps für das Nachtreserve-Neuron."""
    now = now or datetime.now()
    live = live or {}
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    morning_floor = _morning_floor(config)
    reserve_soc, reserve_kwh, reserve_source = _overnight_reserve_soc(config, state)
    margin = overnight_reserve_margin_pct(state)
    predawn_end = _predawn_end_hour(state, now)
    phase = _phase_for_hour(now.hour, predawn_end)

    evening_entry_floor_soc = round(
        min(max_soc - 5.0, morning_floor + reserve_soc + 2.0),
        1,
    )
    night_hold_floor_soc = round(
        min(max_soc - 5.0, morning_floor + reserve_soc * 0.85 + 1.0),
        1,
    )
    night_hold_floor_soc = max(night_hold_floor_soc, morning_floor + 2.0)

    tomorrow_ok = _tomorrow_pv_ok(config, state)
    predawn_discharge_ok = bool(
        phase == "predawn"
        and tomorrow_ok
        and now.hour >= PREDawn_START
    )

    soc = live.get("soc_percent")
    try:
        soc_f = float(soc) if soc is not None else None
    except (TypeError, ValueError):
        soc_f = None

    storage = float(config.get("storage_kwh") or 0)
    max_night_discharge_kw = 0.0
    if soc_f is not None and storage > 0 and phase in ("deep_night", "evening", "predawn"):
        floor = (
            morning_floor
            if predawn_discharge_ok
            else night_hold_floor_soc
            if phase == "deep_night"
            else evening_entry_floor_soc
        )
        headroom_kwh = max(0.0, (soc_f - floor - DISCHARGE_FLOOR_BUFFER_PCT) / 100.0 * storage)
        hours_left = max(1.0, _hours_until_phase_end(now, phase, predawn_end))
        max_night_discharge_kw = round(headroom_kwh / hours_left, 2)

    try:
        from control_algorithm.energy_balance import consumption_risk_scale

        risk_scale = float(consumption_risk_scale(config))
    except Exception:
        risk_scale = 1.0

    return {
        "ok": True,
        "phase": phase,
        "evening_entry_floor_soc": evening_entry_floor_soc,
        "night_hold_floor_soc": night_hold_floor_soc,
        "morning_floor_soc": morning_floor,
        "predawn_discharge_ok": predawn_discharge_ok,
        "predawn_end_hour": predawn_end,
        "max_night_discharge_kw": max_night_discharge_kw,
        "overnight_reserve_soc": reserve_soc,
        "overnight_reserve_kwh": reserve_kwh,
        "overnight_reserve_margin_pct": margin,
        "consumption_risk_scale": round(risk_scale, 3),
        "reserve_source": reserve_source,
        "tomorrow_pv_ok": tomorrow_ok,
        "live_soc": round(soc_f, 1) if soc_f is not None else None,
        "generated_at": now.isoformat(timespec="seconds"),
    }


def _hours_until_phase_end(now: datetime, phase: str, predawn_end: int) -> float:
    if phase == "deep_night":
        if now.hour >= DEEP_NIGHT_START:
            end = datetime.combine(
                now.date() + timedelta(days=1),
                datetime.min.time(),
            ).replace(hour=PREDawn_START)
        else:
            end = now.replace(hour=PREDawn_START, minute=0, second=0, microsecond=0)
        return max(1.0, (end - now).total_seconds() / 3600.0)
    if phase == "evening":
        end = now.replace(hour=EVENING_END, minute=0, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return max(1.0, (end - now).total_seconds() / 3600.0)
    if phase == "predawn":
        end = now.replace(hour=predawn_end, minute=0, second=0, microsecond=0)
        if end <= now:
            end += timedelta(hours=1)
        return max(1.0, (end - now).total_seconds() / 3600.0)
    return 8.0


def overnight_discharge_allowed(
    plan: Dict[str, Any],
    *,
    soc: Optional[float],
    export_mode: bool = False,
    live_export_kw: float = 0.0,
    export_threshold_kw: float = 0.05,
) -> Tuple[bool, str]:
    """
    Darf proaktives/nächtliches Entladen aktiv sein?
    Export-Mode und Live-Einspeisung haben Vorrang.
    """
    if export_mode or live_export_kw >= export_threshold_kw:
        return True, "Export/Einspeisung hat Vorrang"
    if not plan.get("ok"):
        return True, ""
    if soc is None:
        return True, ""

    phase = str(plan.get("phase") or "day")
    soc_f = float(soc)
    buffer = DISCHARGE_FLOOR_BUFFER_PCT

    if phase == "day":
        return True, ""

    if phase == "predawn" and plan.get("predawn_discharge_ok"):
        morning = float(plan.get("morning_floor_soc") or 0)
        if soc_f <= morning + buffer:
            return False, (
                f"Morgendämmerung: SOC {soc_f:.0f} % ≤ Morgen-Ziel "
                f"{morning:.0f} %+{buffer:.0f} – Entladen gestoppt"
            )
        return True, "Morgendämmerung: Entladen Richtung Morgen-Checkpoint erlaubt"

    if phase == "evening":
        floor = float(plan.get("evening_entry_floor_soc") or 0)
        if soc_f <= floor + buffer:
            return False, (
                f"Abend-Reserve: SOC {soc_f:.0f} % ≤ Eintritt {floor:.0f} % "
                f"+{buffer:.0f} – Nacht-Entladen gesperrt"
            )
        return True, ""

    if phase == "deep_night":
        floor = float(plan.get("night_hold_floor_soc") or 0)
        if soc_f <= floor + buffer:
            return False, (
                f"Tiefnacht-Reserve: SOC {soc_f:.0f} % ≤ Halte-Floor "
                f"{floor:.0f} %+{buffer:.0f} – Entladen gesperrt"
            )
        return False, (
            f"Tiefnacht: Reserve {floor:.0f} % – Entladen zurückgestellt "
            f"(Lasten tagsüber/Morgendämmerung)"
        )

    return True, ""


def overnight_resume_floor_soc(plan: Dict[str, Any]) -> float:
    """Dynamischer Mindest-SOC für Nacht-Einschaltungen."""
    phase = str(plan.get("phase") or "day")
    if phase == "deep_night":
        return float(plan.get("night_hold_floor_soc") or plan.get("morning_floor_soc") or 0)
    if phase == "evening":
        return float(plan.get("evening_entry_floor_soc") or plan.get("morning_floor_soc") or 0)
    return float(plan.get("morning_floor_soc") or 0)


def should_defer_overnight_resume(
    plan: Dict[str, Any],
    device: Dict[str, Any],
    priority_plan: Dict[str, Any],
    *,
    discharge_mode: bool = False,
) -> Tuple[bool, str]:
    """Flex/Spill in Tiefnacht zurückstellen; in Morgendämmerung lockern."""
    phase = str(plan.get("phase") or "day")
    if phase == "day":
        return False, ""
    ip = str(device.get("ip") or "")
    row = (priority_plan.get("by_ip") or {}).get(ip) or {}
    if not row:
        role = "flex"
        for d in priority_plan.get("devices") or []:
            if str(d.get("ip") or "") == ip:
                role = str(d.get("role") or "flex")
                break
    else:
        role = str(row.get("role") or "flex")
    if role == "anchor":
        return False, ""

    if phase == "predawn" and plan.get("predawn_discharge_ok"):
        if discharge_mode and role in ("flex", "spill"):
            return False, ""
        return False, ""

    if phase == "deep_night":
        return True, (
            f"Nachtreserve: {role} nachts zurückgestellt "
            f"(Floor {float(plan.get('night_hold_floor_soc') or 0):.0f} % halten)."
        )

    if phase == "evening" and role == "spill":
        return True, (
            "Nachtreserve: Spill abends zurückgestellt – Reserve für unerwartete Nacht-Last."
        )

    return False, ""


def record_overnight_soc_stress(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> None:
    """Tick: SOC unter Nacht-Floor oder unerwarteter Verbrauch → Lernzähler."""
    plan = plan or state.get("overnight_reserve_plan") or {}
    if not plan.get("ok"):
        return
    soc = live.get("soc_percent")
    if soc is None:
        return
    try:
        soc_f = float(soc)
    except (TypeError, ValueError):
        return

    from control_algorithm.steering_learning import ensure_steering_learning

    sl = ensure_steering_learning(state)
    today = sl["today"]
    phase = str(plan.get("phase") or "day")

    floor = float(
        plan.get("night_hold_floor_soc")
        if phase == "deep_night"
        else plan.get("evening_entry_floor_soc")
        if phase == "evening"
        else plan.get("morning_floor_soc")
        or config.get("min_soc_percent")
        or 35.0
    )
    if phase in ("deep_night", "evening", "predawn") and soc_f < floor:
        today["overnight_soc_stress_ticks"] = int(today.get("overnight_soc_stress_ticks") or 0) + 1
        today["last_overnight_stress_at"] = datetime.now().isoformat(timespec="seconds")

    if phase in ("deep_night", "evening"):
        try:
            verbrauch = float(live.get("verbrauch_kw") or 0)
            pv = float(live.get("pv_kw") or 0)
            base_kw = float(plan.get("overnight_reserve_kwh") or 24.0) / 12.0
            if pv < 0.5 and verbrauch > base_kw + UNEXPECTED_CONSUMPTION_KW_MARGIN:
                today["overnight_unexpected_load_ticks"] = int(
                    today.get("overnight_unexpected_load_ticks") or 0
                ) + 1
        except (TypeError, ValueError):
            pass

    state["steering_learning"] = sl
