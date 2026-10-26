# -*- coding: utf-8 -*-
"""
Soll-Kurven laut Skizze:
  Blau = Batterie-% (glatt Richtung Max ~97 % gegen Abend)
  Rot  = Verbraucher (Stufen) – Winkel/Trend möglichst früh aus Historie + Ist

Wird stündlich (Loop) und im Daemon-Tick aktualisiert.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 4 %/h → 45° auf der Soll-Kurve (sichtbarer Steuerwinkel).
_ANGLE_SCALE_PCT_PER_H = 4.0
# Bestehende Lastkorrektur in kW (slope_error).
_SLOPE_ERROR_KW = 0.4
# Sichtbare Winkel-Schwelle für Lastkorrektur (Grad).
_ANGLE_CORR_DEG = 6.0
# Verbrauchswinkel: Abend/Morgen-Ratio → Grad; Klasse wechselt bei ±12°.
_CONS_ANGLE_STEIL_RATIO = 1.35
_CONS_ANGLE_FLACH_RATIO = 0.85


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _hour_frac(dt: datetime) -> float:
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0


def slope_pct_to_deg(pct_per_h: float) -> float:
    """SoC-Steigung (%/h) in sichtbaren Steuerwinkel (Grad)."""
    return math.degrees(math.atan(_f(pct_per_h) / _ANGLE_SCALE_PCT_PER_H))


def consumption_ratio_to_deg(ratio: float) -> float:
    """Abend/Morgen-Verhältnis → Grad (1.0 = 0°, ausgewogen)."""
    return math.degrees(math.atan(_f(ratio, 1.0) - 1.0))


def _hourly_base_profile(state: Dict[str, Any]) -> Tuple[List[float], str]:
    """Ø-Grundverbrauch je Stunde (kWh/h ≈ kW) aus Wochen-/Historienprofil."""
    try:
        from control_algorithm.consumption_history import get_consumption_profile

        prof = get_consumption_profile(None) or {}
        hourly = list(prof.get("hourly_base_kwh_avg") or [])
        if len(hourly) >= 24 and sum(hourly) > 0:
            return [max(0.0, _f(x)) for x in hourly[:24]], "consumption_history"
    except Exception:
        pass
    week = (state.get("energy_week_plan") or {}).get("days") or []
    for day in week[:3]:
        # Falls Stundenprofil fehlt: flat from daily base
        daily = _f((day.get("daily_totals") or {}).get("base_kwh"))
        if daily > 0:
            flat = daily / 24.0
            return [flat] * 24, "week_plan_flat"
    return [2.0] * 24, "fallback_2kw"


def _hour_id(h: int) -> str:
    return f"h{int(h):02d}"


_STROM_CAPTION = (
    "Durchgezogen = Ist. Gestrichelt cyan = Bilanz-Plan (PV−Last). "
    "Gestrichelt violett = Steuerungsziel des Algorithmus."
)


def _empty_hour_row(h: int) -> Dict[str, Any]:
    return {
        "hour": int(h),
        "pv_calc_kw": 0.0,
        "pv_act_kw": None,
        "cons_calc_kw": 0.0,
        "cons_act_kw": None,
        "soc_calc_pct": None,
        "soc_goal_pct": None,
        "soc_act_pct": None,
    }


def _empty_stromprofil(now_h: int = 0, reason: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": reason is None,
        "hours": [_empty_hour_row(h) for h in range(24)],
        "now_hour": int(now_h),
        "caption": _STROM_CAPTION,
    }
    if reason:
        out["reason"] = reason
    return out


def build_stromprofil(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
    soc_target_by_hour: Optional[Dict[int, float]] = None,
) -> Dict[str, Any]:
    """
    24h-Profil wie Stromprofil: PV, Verbrauch, SoC.
    berechnet = durchgezogen, Ist = nur bis jetzt (gestrichelt).
    kWh/h ≈ kW Mittelwert der Stunde.
    """
    now_h = int(now.hour) if now else 0
    try:
        return _build_stromprofil_inner(
            config, state, live, now, soc_target_by_hour, now_h
        )
    except Exception as e:
        return _empty_stromprofil(now_h, str(e))


def _planning_storage_and_max(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> Tuple[float, float]:
    storage = _f(config.get("storage_kwh"), 41.0)
    hard_max = _f(config.get("max_soc_percent"), 97.0)
    plan_max = hard_max
    try:
        from control_algorithm.storage_band_learning import effective_planning_max_soc

        plan_max = float(effective_planning_max_soc(config, state, live=live))
    except Exception:
        pass
    plan_max = max(5.0, min(100.0, plan_max))
    return storage, plan_max


def _chain_soc_calc_pct(
    *,
    pv_calc: List[float],
    cons_calc: List[float],
    soc_act: List[Optional[float]],
    live_soc: Optional[float],
    now_h: int,
    storage: float,
    plan_max: float,
    pv_act: Optional[List[Optional[float]]] = None,
    cons_act: Optional[List[Optional[float]]] = None,
    cons_plan_future: Optional[List[float]] = None,
) -> Dict[int, float]:
    """
    SoC-Kurve: Vergangenheit = Ist-Bilanz (kein fiktiver Miner-Plan),
    ab Jetzt = Plan (PV_Plan − Last_Plan).

    Verhindert Absturz auf 0 % und den Sprung bei „Jetzt“, der entsteht wenn
    geplante Miner-kWh rückwirkend die Batterie „leer rechnen“.
    """
    if storage <= 0:
        return {}

    def _cap(kwh: float) -> float:
        cap = min(storage, plan_max / 100.0 * storage)
        return max(0.0, min(cap, kwh))

    def _at(seq: Optional[List[Any]], h: int) -> Optional[float]:
        if not seq or h < 0 or h >= len(seq):
            return None
        v = seq[h]
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _energy_for_hour(h: int) -> Tuple[float, float]:
        """PV/Last für Stunde h: Vergangenheit bevorzugt Ist, Zukunft Plan."""
        if h < now_h:
            pv_a = _at(pv_act, h)
            cons_a = _at(cons_act, h)
            pv = pv_a if pv_a is not None else _f(pv_calc[h] if h < len(pv_calc) else 0.0)
            # Vergangenheit: echte Last, nie den Tages-Miner-Plan rückwirkend
            if cons_a is not None and cons_a > 0.05:
                cons = cons_a
            else:
                cons = _f(cons_calc[h] if h < len(cons_calc) else 0.0)
            return max(0.0, pv), max(0.0, cons)
        pv = _f(pv_calc[h] if h < len(pv_calc) else 0.0)
        if cons_plan_future is not None and h < len(cons_plan_future):
            cons = _f(cons_plan_future[h])
        else:
            cons = _f(cons_calc[h] if h < len(cons_calc) else 0.0)
        return max(0.0, pv), max(0.0, cons)

    # Start: erster Ist-SoC, sonst Live — ohne Plan-Miner rückwärts „aufblasen/leeren“
    seed_soc: Optional[float] = None
    first_h: Optional[int] = None
    for i, s in enumerate(soc_act[:24]):
        if s is not None:
            first_h = i
            seed_soc = float(s)
            break
    if seed_soc is None and live_soc is not None:
        seed_soc = float(live_soc)
        first_h = max(0, min(23, int(now_h)))
    if seed_soc is None:
        return {}

    end_kwh = seed_soc / 100.0 * storage
    if first_h is not None and first_h > 0:
        for hb in range(first_h, 0, -1):
            pv_b, cons_b = _energy_for_hour(hb)
            end_kwh = end_kwh - pv_b + cons_b
    pv0, cons0 = _energy_for_hour(0)
    chained = _cap(end_kwh - pv0 + cons0)

    out: Dict[int, float] = {}
    for h in range(24):
        # Vergangenheit: gemessenen SoC verankern (keine fiktive Leer-Rechnung)
        if h < now_h and h < len(soc_act) and soc_act[h] is not None:
            chained = float(soc_act[h]) / 100.0 * storage
        elif h == now_h and live_soc is not None:
            chained = float(live_soc) / 100.0 * storage
        else:
            pv_h, cons_h = _energy_for_hour(h)
            chained = _cap(chained + pv_h - cons_h)
        out[h] = round(max(0.0, min(100.0, chained / storage * 100.0)), 1)
    return out


def _lerp(a: float, b: float, t: float) -> float:
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def _clamp_soc(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def build_steering_soc_goal_24h(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    24h-Steuerungsziel-SoC (nicht PV−Last-Bilanz).

    plan_max (Band), PV-Prognose, Grundlast, Overnight-Floors,
    Vorentladen bei gutem Wetter. Nie unter min_soc.
    """
    now = now or datetime.now()
    now_h = int(now.hour)
    storage, plan_max = _planning_storage_and_max(config, state, live)
    min_soc = _f(config.get("min_soc_percent"), 28.0)
    morning = _f(config.get("morning_soc_percent"), 33.0)
    try:
        from control_algorithm.storage_band_learning import effective_planning_min_soc

        morning = max(morning, float(effective_planning_min_soc(config, state, live=live)))
    except Exception:
        pass

    or_plan = state.get("overnight_reserve_plan") or {}
    floor_night = max(
        min_soc,
        morning,
        _f(or_plan.get("night_hold_floor_soc"), morning),
        _f(or_plan.get("morning_floor_soc"), morning),
    )
    floor_eve = max(
        floor_night,
        _f(or_plan.get("evening_entry_floor_soc"), floor_night),
    )

    try:
        from control_algorithm.energy_balance import _plant_perfect_day_kwh

        perfect = float(_plant_perfect_day_kwh(config, state) or 0)
    except Exception:
        perfect = 0.0
    tomorrow_pv = _f(state.get("pv_yield_tomorrow_kwh"))
    today_pv = _f(
        state.get("pv_yield_today_kwh") or state.get("morning_pv_forecast_kwh")
    )
    if perfect > 10 and tomorrow_pv < perfect * 0.55:
        floor_eve = max(floor_eve, min(plan_max, plan_max - 5.0))
    elif perfect > 10 and tomorrow_pv < perfect * 0.75:
        floor_eve = max(floor_eve, min(plan_max, (plan_max + floor_night) * 0.5))

    hourly_base, _ = _hourly_base_profile(state)
    pv_map: Dict[str, float] = dict(state.get("pv_forecast_today_hours") or {})
    week = (state.get("energy_week_plan") or {}).get("days") or []
    if week:
        for row in (week[0].get("hours") or []):
            try:
                hh = int(row.get("hour"))
            except (TypeError, ValueError):
                continue
            hid = _hour_id(hh)
            if _f(pv_map.get(hid)) <= 0:
                pv_map[hid] = _f(row.get("pv_kwh"))

    pv_hours = [_f(pv_map.get(_hour_id(h))) for h in range(24)]
    base_hours = [
        _f(hourly_base[h] if h < len(hourly_base) else 1.0) for h in range(24)
    ]
    pv_start = next((h for h in range(24) if pv_hours[h] > 0.15), 7)
    peak_h = _target_peak_hour(config, state, now)
    pv_end = max(peak_h, _pv_end_hour(state, now))

    live_soc = live.get("soc_percent")
    if live_soc is None:
        live_soc = state.get("live_soc")
    live_soc_f = _f(live_soc, floor_night)

    start_soc = floor_night
    try:
        from control_algorithm.config import BACKEND_DIR
        from control_algorithm.consumption_history import get_hourly_actual_soc_today

        acts = get_hourly_actual_soc_today(str(BACKEND_DIR)) or []
        for s in acts:
            if s is not None:
                start_soc = float(s)
                break
        else:
            start_soc = live_soc_f
    except Exception:
        start_soc = live_soc_f

    net_kwh = sum(pv_hours[h] - base_hours[h] for h in range(24))
    reachable = start_soc + (100.0 * net_kwh / storage) if storage > 0 else plan_max
    # Ziel bleibt der berechnete Planwert. reachable ist nur die Prognose:
    # zu wenig PV ist eine Zielverfehlung und senkt das Ziel nicht.
    target_peak = _clamp_soc(plan_max, floor_night, plan_max)

    headroom_am = floor_night
    good_sun = perfect > 10 and (
        today_pv >= perfect * 0.55 or tomorrow_pv >= perfect * 0.55
    )
    if good_sun and start_soc > morning + 8:
        try:
            from control_algorithm.energy_balance import _proactive_headroom_soc

            headroom_am = float(
                _proactive_headroom_soc(
                    plan_max,
                    max(tomorrow_pv, today_pv * 0.5),
                    storage_kwh=storage,
                    morning_target=morning,
                    perfect_day_kwh=perfect,
                )
            )
        except Exception:
            headroom_am = morning
        headroom_am = _clamp_soc(headroom_am, floor_night, start_soc)

    # Morgen-Floor = Algo-Vorschlag für nächsten Morgen; nie unter min_soc
    morning_floor = max(min_soc, floor_night, morning)

    def _base_drain_pct(h: int) -> float:
        """Historische Grundlast → SoC-% (für leichte Wellenform)."""
        if storage <= 0:
            return 0.0
        return max(0.05, base_hours[h] / storage * 100.0)

    goals: List[float] = [0.0] * 24
    for h in range(0, peak_h + 1):
        if h < pv_start:
            if good_sun and start_soc > headroom_am + 2 and pv_start > 0:
                t = h / max(1.0, float(pv_start))
                g = _lerp(start_soc, headroom_am, t)
            else:
                g = morning_floor if h < 5 else _lerp(morning_floor, start_soc, 0.35)
        else:
            span = max(1.0, float(peak_h - pv_start))
            t = (h - pv_start) / span
            base_am = headroom_am if good_sun else max(morning_floor, start_soc * 0.9)
            g = _lerp(base_am, target_peak, t)
        goals[h] = round(_clamp_soc(g, min_soc, plan_max), 1)

    def _evening_slope(start_h: int, start_soc_v: float) -> None:
        """
        Sanfter Winkel von start_h+1 … 23 Richtung Morgen-Floor.
        Basis: historische Grundlast-Gewichte (leichte Schwankung), kein Plateau bei Max.
        """
        if start_h >= 23:
            return
        # Zeitraum bis ~06:00 Folgetag ≈ Rest heute + 6 h → Anteil bis Mitternacht
        hours_to_morning = max(1, (23 - start_h) + 6)
        drop_total = max(0.0, start_soc_v - morning_floor)
        # Bis Mitternacht nur den Anteil der Nacht, der heute noch liegt
        drop_tonight = drop_total * float(23 - start_h) / float(hours_to_morning)
        # Mindestens sichtbarer Abfall (~0.8 %/h), maximal ~3.5 %/h
        n = max(1, 23 - start_h)
        raw = [_base_drain_pct(h) for h in range(start_h + 1, 24)]
        wsum = sum(raw) or float(n)
        weights = [r / wsum for r in raw]
        soc = float(start_soc_v)
        # Wenn Reserve für schwachen Folgetag hoch: nicht bis morning_floor, aber trotzdem Winkel
        target_end = morning_floor
        if perfect > 10 and tomorrow_pv < perfect * 0.55:
            target_end = max(morning_floor, min(start_soc_v - 4.0, floor_eve))
            drop_tonight = max(0.0, soc - target_end) * 0.55
        else:
            # Sicherstellen, dass die Linie nicht flach bei Max klebt
            drop_tonight = max(drop_tonight, min(soc - morning_floor, 1.2 * n))
            drop_tonight = min(drop_tonight, soc - morning_floor)
        for i, h in enumerate(range(start_h + 1, 24)):
            step = drop_tonight * weights[i]
            # Cap pro Stunde für eleganten Winkel
            step = max(0.4, min(3.2, step))
            soc = soc - step
            soc = _clamp_soc(soc, target_end, plan_max)
            goals[h] = round(soc, 1)

    # Nach Peak: Winkel Richtung Morgen-Floor (Grundlast-gewichtet)
    _evening_slope(peak_h, float(goals[peak_h]))

    # Ab Jetzt: Live, Rest mit gleichem eleganten Winkel
    if 0 <= now_h <= 23:
        goals[now_h] = round(_clamp_soc(live_soc_f, min_soc, plan_max), 1)
        if now_h < peak_h:
            for h in range(now_h + 1, peak_h + 1):
                t = (h - now_h) / max(1.0, float(peak_h - now_h))
                goals[h] = round(
                    _clamp_soc(_lerp(live_soc_f, target_peak, t), min_soc, plan_max), 1
                )
            _evening_slope(peak_h, float(goals[peak_h]))
        else:
            _evening_slope(now_h, float(goals[now_h]))

    return {
        "ok": True,
        "hours": goals,
        "target_peak_pct": round(target_peak, 1),
        "floor_night_pct": round(morning_floor, 1),
        "floor_eve_pct": round(floor_eve, 1),
        "headroom_am_pct": round(headroom_am, 1),
        "plan_max_pct": round(plan_max, 1),
        "pv_start_hour": pv_start,
        "peak_hour": peak_h,
        "reachable_pct": round(reachable, 1),
        "net_kwh": round(net_kwh, 1),
        "good_sun": good_sun,
        "date": now.date().isoformat(),
        "now_hour": now_h,
    }


def apply_steering_goal_freeze(
    state: Dict[str, Any],
    goal: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[Optional[float]]:
    """
    Morgen-Freeze: Vergangenheit der Zielkurve einfrieren.
    Flatline-Freeze (≈ Max den ganzen Tag) wird verworfen und neu gesetzt.
    """
    now = now or datetime.now()
    hours = list(goal.get("hours") or [])
    if len(hours) < 24:
        hours = (hours + [None] * 24)[:24]
    now_h = int(now.hour)
    day_key = now.date().isoformat()
    frozen = state.get("steering_soc_goal_frozen")

    def _is_flatline(vals: List[Any]) -> bool:
        nums = [float(v) for v in vals if v is not None]
        if len(nums) < 4:
            return False
        return (max(nums) - min(nums)) < 5.0

    need_freeze = False
    if not isinstance(frozen, dict) or frozen.get("date") != day_key:
        need_freeze = now_h >= 5 or not isinstance(frozen, dict)
    elif _is_flatline(list((frozen or {}).get("hours") or [])[: max(1, now_h)]):
        # Alte Freeze war praktisch eine Gerade bei Max → neu
        need_freeze = True

    if need_freeze and now_h >= 5:
        state["steering_soc_goal_frozen"] = {
            "date": day_key,
            "hours": [float(x) if x is not None else None for x in hours],
            "frozen_at": now.isoformat(),
            "meta": {
                "target_peak_pct": goal.get("target_peak_pct"),
                "floor_eve_pct": goal.get("floor_eve_pct"),
                "floor_night_pct": goal.get("floor_night_pct"),
            },
        }
        frozen = state.get("steering_soc_goal_frozen") or {}

    frozen_hours = list((frozen or {}).get("hours") or []) if isinstance(frozen, dict) else []
    out: List[Optional[float]] = []
    for h in range(24):
        if h < now_h and h < len(frozen_hours) and frozen_hours[h] is not None:
            out.append(round(float(frozen_hours[h]), 1))
        elif h < len(hours) and hours[h] is not None:
            out.append(round(float(hours[h]), 1))
        else:
            out.append(None)
    return out


def _build_stromprofil_inner(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
    soc_target_by_hour: Optional[Dict[int, float]],
    now_h: int,
) -> Dict[str, Any]:
    from control_algorithm.config import BACKEND_DIR

    base_dir = str(BACKEND_DIR)
    hourly_base, _src = _hourly_base_profile(state)
    week = (state.get("energy_week_plan") or {}).get("days") or []
    today_hours = (week[0].get("hours") if week else None) or []
    pv_calc_map: Dict[str, float] = dict(state.get("pv_forecast_today_hours") or {})
    miner_by_h: Dict[int, float] = {}
    base_by_h: Dict[int, float] = {}
    pv_by_h: Dict[int, float] = {}
    soc_proj: Dict[int, float] = {}
    for row in today_hours:
        try:
            hh = int(row.get("hour"))
        except (TypeError, ValueError):
            continue
        pv_by_h[hh] = _f(row.get("pv_kwh"))
        miner_by_h[hh] = _f(row.get("miner_kwh_planned"))
        base_by_h[hh] = _f(row.get("base_kwh"))
        if row.get("soc_projected_pct") is not None:
            soc_proj[hh] = _f(row.get("soc_projected_pct"))

    pv_act_map: Dict[str, float] = {}
    try:
        from control_algorithm.slot_profiles import get_actual_pv_kwh_by_hours_today

        pv_act_map = dict(get_actual_pv_kwh_by_hours_today(base_dir, now) or {})
    except Exception:
        pv_act_map = {}

    cons_act_total: List[float] = [0.0] * 24
    soc_act: List[Optional[float]] = [None] * 24
    try:
        from control_algorithm.consumption_history import (
            get_hourly_actual_soc_today,
            get_hourly_consumption_breakdown_today,
        )

        br = get_hourly_consumption_breakdown_today(base_dir) or {}
        tot = list(br.get("total_kwh") or [])
        for i, v in enumerate(tot[:24]):
            cons_act_total[i] = max(0.0, _f(v))
        soc_act = list(get_hourly_actual_soc_today(base_dir) or [])
        if len(soc_act) < 24:
            soc_act = (soc_act + [None] * 24)[:24]
    except Exception:
        pass

    live_soc = live.get("soc_percent")
    if live_soc is None:
        live_soc = state.get("live_soc")
    live_pv = _f(live.get("pv_kw"), _f(state.get("live_pv_kw")))
    live_house = _f(live.get("wohnhaus_kw"), _f(live.get("house_kw"), _f(live.get("verbrauch_kw"))))
    live_cons = _f(live.get("verbrauch_kw"))
    target_map = soc_target_by_hour or {}

    pv_calc_list: List[float] = []
    cons_calc_list: List[float] = []
    cons_future_list: List[float] = []
    pv_act_list: List[Optional[float]] = [None] * 24
    for h in range(24):
        hid = _hour_id(h)
        pv_c = pv_by_h.get(h, _f(pv_calc_map.get(hid)))
        base_c = _f(base_by_h.get(h), hourly_base[h] if h < len(hourly_base) else 0.0)
        miner_c = _f(miner_by_h.get(h))
        cons_plan = base_c + miner_c
        # Anzeige Last-Plan: Zukunft inkl. Miner; Vergangenheit ohne fiktive Miner-Last
        # (Miner-Plan wäre sonst rückwirkend und treibt SoC-Plan auf 0 %)
        if h < now_h:
            cons_c = cons_act_total[h] if cons_act_total[h] > 0.05 else base_c
            if cons_c < 0.05 and h < len(hourly_base):
                cons_c = max(cons_c, _f(hourly_base[h]))
        else:
            cons_c = cons_plan
            if cons_c < 0.05 and h < len(hourly_base):
                cons_c = max(cons_c, _f(hourly_base[h]))
        pv_calc_list.append(max(0.0, _f(pv_c)))
        cons_calc_list.append(max(0.0, _f(cons_c)))
        cons_future_list.append(max(0.0, _f(cons_plan)))
        pa = pv_act_map.get(hid)
        pv_act_list[h] = _f(pa) if pa is not None else None

    storage, plan_max = _planning_storage_and_max(config, state, live)
    soc_chained = _chain_soc_calc_pct(
        pv_calc=pv_calc_list,
        cons_calc=cons_calc_list,
        soc_act=soc_act,
        live_soc=_f(live_soc) if live_soc is not None else None,
        now_h=now_h,
        storage=storage,
        plan_max=plan_max,
        pv_act=pv_act_list,
        cons_act=list(cons_act_total),
        cons_plan_future=cons_future_list,
    )

    goal_meta: Dict[str, Any] = {}
    soc_goal_list: List[Optional[float]] = [None] * 24
    try:
        goal_meta = build_steering_soc_goal_24h(config, state, live, now)
        soc_goal_list = apply_steering_goal_freeze(state, goal_meta, now)
    except Exception as e:
        goal_meta = {"ok": False, "reason": str(e)}

    hours: List[Dict[str, Any]] = []
    for h in range(24):
        hid = _hour_id(h)
        pv_c = pv_calc_list[h]
        cons_c = cons_calc_list[h]
        # Primär: eigene Stundenbilanz (Nacht fällt mit Last). Wochenplan nur Fallback.
        soc_c: Optional[float] = soc_chained.get(h)
        if soc_c is None and h in soc_proj:
            soc_c = soc_proj[h]
        elif soc_c is None and h in target_map:
            soc_c = target_map[h]
        if soc_c is None and h == now_h and live_soc is not None:
            soc_c = _f(live_soc)
        soc_g: Optional[float] = None
        if h < len(soc_goal_list) and soc_goal_list[h] is not None:
            soc_g = _f(soc_goal_list[h])
        past = h < now_h
        current = h == now_h
        pv_a = None
        cons_a = None
        soc_a = None
        if past or current:
            pv_a = _f(pv_act_map.get(hid))
            if current and live_pv > 0.05 and pv_a <= 0.05:
                pv_a = live_pv
            cons_a = cons_act_total[h] if h < len(cons_act_total) else 0.0
            if current and _f(cons_a) <= 0.05:
                if live_cons > 0.05:
                    cons_a = live_cons
                elif live_house > 0.05:
                    cons_a = live_house + _f(miner_by_h.get(h))
            if current and live_soc is not None:
                soc_a = _f(live_soc)
            elif h < len(soc_act) and soc_act[h] is not None:
                soc_a = _f(soc_act[h])
        hours.append(
            {
                "hour": h,
                "pv_calc_kw": round(pv_c, 2),
                "pv_act_kw": round(_f(pv_a), 2) if pv_a is not None else None,
                "cons_calc_kw": round(max(0.0, cons_c), 2),
                "cons_act_kw": round(_f(cons_a), 2) if cons_a is not None else None,
                "soc_calc_pct": round(_f(soc_c), 1) if soc_c is not None else None,
                "soc_goal_pct": round(_f(soc_g), 1) if soc_g is not None else None,
                "soc_act_pct": round(_f(soc_a), 1) if soc_a is not None else None,
            }
        )
    return {
        "ok": True,
        "hours": hours,
        "now_hour": now_h,
        "caption": _STROM_CAPTION,
        "steering_goal": {
            k: goal_meta.get(k)
            for k in (
                "ok",
                "target_peak_pct",
                "floor_night_pct",
                "floor_eve_pct",
                "headroom_am_pct",
                "plan_max_pct",
                "reachable_pct",
                "net_kwh",
                "good_sun",
                "reason",
            )
            if goal_meta.get(k) is not None or k == "ok"
        },
    }


def _pv_end_hour(state: Dict[str, Any], now: datetime) -> int:
    try:
        from control_algorithm.weather_survival import last_pv_hour_for_date

        return int(last_pv_hour_for_date(state, now.date()) or 20)
    except Exception:
        return 20


def _target_peak_hour(config: Dict[str, Any], state: Dict[str, Any], now: datetime) -> int:
    """Ziel-Zeit = Produktionsende dieses Tages (letzte PV-Stunde der Prognose).

    Die Stunde wandert mit der Sonne: im Winter früher, im Sommer später.
    Eine feste 18-Uhr-Klammer gibt es nicht. soc_peak_hour in der Config
    kann die Stunde weiter fest vorgeben.
    """
    try:
        evening = int(config.get("soc_peak_hour") or 0)
    except (TypeError, ValueError):
        evening = 0
    if 12 <= evening <= 22:
        return evening
    pv_end = _pv_end_hour(state, now)
    if 12 <= pv_end <= 22:
        return pv_end
    return 18


def compute_consumption_angle(
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Rote Linie: Verbrauchswinkel früh bestimmen.

    morning_kw   = Ø Historie 06–12
    evening_kw   = Ø Historie 18–23
    angle_ratio  = evening / morning  (>1 = steiler Abendverbrauch)
    today_pace   = bisheriger Ist-Grundverbrauch vs. erwartetes Profil
    """
    now = now or datetime.now()
    hourly, src = _hourly_base_profile(state)
    morning = hourly[6:12]
    evening = hourly[18:24]
    m_avg = sum(morning) / max(1, len(morning))
    e_avg = sum(evening) / max(1, len(evening))
    angle_ratio = (e_avg / m_avg) if m_avg > 0.05 else 1.0

    # Früher Ist-Trend: heutige Stunden bis jetzt vs. Profil
    today_hourly = None
    try:
        from control_algorithm.consumption_history import get_hourly_consumption_breakdown_today
        from control_algorithm.config import BACKEND_DIR

        br = get_hourly_consumption_breakdown_today(BACKEND_DIR) or {}
        today_hourly = list(br.get("base_kwh") or [])
    except Exception:
        today_hourly = None

    hour = now.hour
    expected_so_far = sum(hourly[: hour + 1])
    actual_so_far = sum((_f(x) for x in (today_hourly or [])[: hour + 1])) if today_hourly else None
    pace_ratio = None
    if actual_so_far is not None and expected_so_far > 0.2:
        pace_ratio = actual_so_far / expected_so_far

    live_house = _f(live.get("wohnhaus_kw"), _f(live.get("house_kw"), _f(live.get("verbrauch_kw"))))
    if live_house <= 0:
        try:
            from pathlib import Path
            import json
            from control_algorithm.config import BACKEND_DIR

            en = json.loads(
                (Path(BACKEND_DIR) / "energy_monitor" / "enline_values.json").read_text(
                    encoding="utf-8"
                )
            )
            live_house = _f((en.get("values_kw") or {}).get("wohnhaus"))
        except Exception:
            live_house = 0.0

    # Winkel-Klasse für Steuerung (Grad: 1.0 = 0°, 1.35 ≈ +19°)
    angle_deg = consumption_ratio_to_deg(angle_ratio)
    steil_deg = consumption_ratio_to_deg(_CONS_ANGLE_STEIL_RATIO)
    flach_deg = consumption_ratio_to_deg(_CONS_ANGLE_FLACH_RATIO)
    if angle_ratio >= _CONS_ANGLE_STEIL_RATIO:
        shape = "abend_steil"
        note = (
            f"Historie: Abendverbrauch ~{angle_ratio:.2f}× Morgen "
            f"({angle_deg:+.0f}°) – Ladekurve früher abflachen / Flex früher."
        )
    elif angle_ratio <= _CONS_ANGLE_FLACH_RATIO:
        shape = "abend_flach"
        note = (
            f"Historie: Abend eher flach ({angle_deg:+.0f}°) – "
            "mehr Anker-Laufzeit am Nachmittag möglich."
        )
    else:
        shape = "ausgewogen"
        note = (
            f"Historie: ausgewogen (Abend/Morgen={angle_ratio:.2f}, "
            f"Winkel {angle_deg:+.0f}°)."
        )

    if pace_ratio is not None and pace_ratio >= 1.2:
        note += f" Heute bisher +{(pace_ratio - 1) * 100:.0f} % über Profil."
    elif pace_ratio is not None and pace_ratio <= 0.8:
        note += f" Heute bisher {(1 - pace_ratio) * 100:.0f} % unter Profil."

    return {
        "ok": True,
        "updated_at": now.isoformat(),
        "source": src,
        "morning_avg_kw": round(m_avg, 2),
        "evening_avg_kw": round(e_avg, 2),
        "angle_ratio": round(angle_ratio, 3),
        "angle_deg": round(angle_deg, 1),
        "steil_threshold_deg": round(steil_deg, 1),
        "flach_threshold_deg": round(flach_deg, 1),
        "shape": shape,
        "pace_ratio_today": round(pace_ratio, 3) if pace_ratio is not None else None,
        "expected_base_kwh_so_far": round(expected_so_far, 2),
        "actual_base_kwh_so_far": round(actual_so_far, 2) if actual_so_far is not None else None,
        "live_house_kw": round(live_house, 2),
        "hourly_base_kw": [round(x, 2) for x in hourly],
        "note": note,
    }


def compute_target_soc_curve(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Blaue Linie: glatte Soll-SoC-Kurve.

    Von aktuellem SoC linear (stückweise) zum Max bis Peak-Stunde,
    danach Halten/leichtes Entladen Richtung Morgenziel.
    """
    now = now or datetime.now()
    storage = _f(config.get("storage_kwh"), 41.0)
    min_soc = _f(config.get("min_soc_percent"), 28.0)
    hard_max = _f(config.get("max_soc_percent"), 97.0)
    morning = _f(config.get("morning_soc_percent"), 33.0)
    try:
        from control_algorithm.storage_band_learning import (
            effective_planning_max_soc,
            effective_planning_min_soc,
        )

        max_soc = float(effective_planning_max_soc(config, state, live=live))
        plan_min = float(effective_planning_min_soc(config, state, live=live))
        # Floor der Soll-Kurve: Morgenziel + Band-Cushion (nicht Notfall-Min)
        morning = max(morning, plan_min)
    except Exception:
        max_soc = hard_max
    soc = live.get("soc_percent")
    if soc is None:
        soc = state.get("live_soc")
    soc = _f(soc, default=-1.0)
    if soc < 0 or storage <= 0:
        return {"ok": False, "reason": "no_soc"}

    peak_h = _target_peak_hour(config, state, now)
    peak_dt = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=peak_h, minute=0
    )
    if peak_dt <= now:
        # Nach Peak: Ziel = Max halten bis PV-Ende, dann Richtung Morgen
        hours_to_peak = 0.0
        target_now = max_soc
        desired_charge_kw = 0.0
        phase = "after_peak"
    else:
        hours_to_peak = (peak_dt - now).total_seconds() / 3600.0
        room = max(0.0, (max_soc - soc) / 100.0 * storage)
        desired_charge_kw = room / hours_to_peak if hours_to_peak > 0.25 else room * 4
        target_now = soc  # Ist ist Start; Soll-Ladung steuert den Winkel
        phase = "charge_to_peak"

    # Verbrauchswinkel → frühere Abflachung wenn Abend steil
    angle = compute_consumption_angle(state, live, now)
    boost = 1.0
    if angle.get("shape") == "abend_steil":
        boost = 1.15  # etwas früher laden / mehr Last früher = flachere SoC-Kurve
    elif angle.get("shape") == "abend_flach":
        boost = 0.92

    desired_charge_kw *= boost
    # Cap: nicht aggressiver als physikalischer Rest bis PV-Ende
    pv_end_h = _pv_end_hour(state, now)
    pv_end = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=min(22, pv_end_h), minute=0
    )
    if pv_end <= now:
        pv_end = now + timedelta(hours=1)
    hours_pv = max(0.25, (pv_end - now).total_seconds() / 3600.0)
    room = max(0.0, (max_soc - soc) / 100.0 * storage)
    flat_to_pv_end = room / hours_pv
    # Glätten = eher Richtung Peak-Stunde, aber nicht steiler als nötig
    desired_smooth = min(desired_charge_kw, max(flat_to_pv_end, flat_to_pv_end * 0.5))
    if phase == "charge_to_peak":
        desired_smooth = max(desired_smooth, flat_to_pv_end * 0.85)

    fill = state.get("fill_schedule") or {}
    batt = live.get("battery_power_kw")
    if batt is None:
        batt = state.get("live_battery_kw")
    # Live-Entladung: nicht den veralteten Fill-Ist übernehmen (sonst False-Overflow)
    if batt is not None and _f(batt) < -0.05:
        ist_charge = 0.0
    else:
        ist_charge = _f(fill.get("charge_kw"))
        if ist_charge <= 0:
            ist_charge = max(0.0, _f(batt))

    slope_error_kw = ist_charge - desired_smooth
    # Positiv = zu steil laden → mehr Verbraucher früher
    if slope_error_kw > _SLOPE_ERROR_KW:
        steer = "mehr_last_frueher"
    elif slope_error_kw < -_SLOPE_ERROR_KW and soc < max_soc - 5:
        steer = "weniger_last_oder_warten"
    else:
        steer = "kurve_ok"

    now_h = _hour_frac(now)
    end_h = float(peak_h + 2) if phase == "charge_to_peak" else min(23.0, now_h + 4.0)
    if end_h <= now_h:
        end_h = now_h + 2.0
    soll_slope_pct_h = (max_soc - soc) / max(hours_to_peak, 0.25) if phase == "charge_to_peak" else 0.0
    ist_slope_pct_h = (ist_charge / storage) * 100.0 if storage > 0 else 0.0
    soll_deg = slope_pct_to_deg(soll_slope_pct_h)
    ist_deg = slope_pct_to_deg(ist_slope_pct_h)
    delta_deg = ist_deg - soll_deg
    angle_corr = abs(delta_deg) >= _ANGLE_CORR_DEG or abs(slope_error_kw) > _SLOPE_ERROR_KW

    def _target_at(h: float) -> float:
        if phase == "charge_to_peak" and hours_to_peak > 0:
            t_frac = (h - now_h) / max(0.01, hours_to_peak)
            return soc + (max_soc - soc) * min(1.0, max(0.0, t_frac))
        return min(soc, max_soc)

    points: List[Dict[str, Any]] = []
    ist_points: List[Dict[str, Any]] = []
    h = now_h
    while h <= end_h + 0.001:
        points.append({"hour": round(h, 2), "target_soc": round(_target_at(h), 1)})
        ist_soc = soc + ist_slope_pct_h * (h - now_h)
        ist_points.append({"hour": round(h, 2), "soc": round(ist_soc, 1)})
        h += 0.5
    if not points or points[-1]["hour"] < end_h - 0.05:
        points.append({"hour": round(end_h, 2), "target_soc": round(_target_at(end_h), 1)})
        ist_points.append({
            "hour": round(end_h, 2),
            "soc": round(soc + ist_slope_pct_h * (end_h - now_h), 1),
        })

    stretch = state.get("anchor_stretch") or {}
    hydro_off = stretch.get("soc_hydro_off_percent")
    reserve_floor = stretch.get("soc_reserve_floor_percent")

    if abs(delta_deg) < _ANGLE_CORR_DEG:
        angle_plain = (
            f"Winkel ok: Ist {ist_deg:.0f}° liegt im Korridor um Soll {soll_deg:.0f}° "
            f"(±{_ANGLE_CORR_DEG:.0f}°)."
        )
    elif delta_deg > 0:
        angle_plain = (
            f"Ist {ist_deg:.0f}° steiler als Soll {soll_deg:.0f}° "
            f"(Δ {delta_deg:+.0f}°) → zu steil laden, Last einschalten."
        )
    else:
        angle_plain = (
            f"Ist {ist_deg:.0f}° flacher als Soll {soll_deg:.0f}° "
            f"(Δ {delta_deg:+.0f}°) → zu wenig Ladung, Last runter / warten."
        )

    if soc <= min_soc:
        line_action = "notabschaltung"
        line_action_label = f"Notabschaltung – SoC {soc:.0f} % unter Min {min_soc:.0f} %"
        goal = (
            f"Sofort alle Lasten aus: SoC {soc:.0f} % ist unter der Notaus-Linie "
            f"({min_soc:.0f} %)."
        )
    elif soc >= max_soc:
        line_action = "einschaltung"
        line_action_label = f"Einschaltung – SoC {soc:.0f} % über Max {max_soc:.0f} %"
        goal = (
            f"Last einschalten, damit SoC von {soc:.0f} % auf Max {max_soc:.0f} % "
            f"bis {peak_h:02d}:00 zurückgeht."
        )
    elif angle_corr and (delta_deg > 0 or steer == "mehr_last_frueher"):
        line_action = "lastkorrektur_mehr"
        line_action_label = (
            f"Lastkorrektur + – Winkel {delta_deg:+.0f}° / Fehler {slope_error_kw:+.1f} kW"
        )
        goal = angle_plain + (
            f" Mehr Last, Kurve abflachen bis {peak_h:02d}:00 auf {max_soc:.0f} %."
        )
    elif angle_corr:
        line_action = "lastkorrektur_weniger"
        line_action_label = (
            f"Lastkorrektur − – Winkel {delta_deg:+.0f}° / Fehler {slope_error_kw:+.1f} kW"
        )
        goal = angle_plain + (
            f" Weniger Last oder warten, damit SoC bis {peak_h:02d}:00 "
            f"auf {max_soc:.0f} % steigt."
        )
    else:
        line_action = "halten"
        line_action_label = "Kurve ok – im Korridor"
        goal = (
            f"SoC flach auf {max_soc:.0f} % bis {peak_h:02d}:00 halten "
            f"(Soll-Ladung {desired_smooth:.1f} kW, Winkel {soll_deg:.0f}°)."
        )

    chart_caption = _STROM_CAPTION
    try:
        from control_algorithm.battery_protection import max_soc_release_percent

        release_soc = float(max_soc_release_percent(config))
    except Exception:
        release_soc = min(90.0, hard_max - 1.0)

    thresholds = [
        {
            "id": "notaus",
            "soc": round(min_soc, 1),
            "label": f"Min {min_soc:.0f} %",
            "action": "notabschaltung",
            "when": "below",
            "hint": "Notaus",
            "active": soc <= min_soc,
        },
        {
            "id": "max",
            "soc": round(max_soc, 1),
            "label": f"Max {max_soc:.0f} % Ein",
            "action": "einschaltung",
            "when": "above",
            "hint": "Einschaltung",
            "active": soc >= max_soc,
        },
        {
            "id": "halt",
            "soc": round(release_soc, 1),
            "label": f"Halt {release_soc:.0f} % Aus",
            "action": "halt",
            "when": "below",
            "hint": "Abschaltung nach Max",
            "active": soc <= release_soc,
        },
        {
            "id": "morning",
            "soc": round(morning, 1),
            "label": f"Morgenziel {morning:.0f} %",
            "action": "morning",
            "when": "hold",
            "hint": "Nacht-Ziel nach Peak",
            "active": False,
        },
    ]
    if hydro_off is not None:
        thresholds.append({
            "id": "hydro",
            "soc": round(_f(hydro_off), 1),
            "label": f"Hydro aus ca. {_f(hydro_off):.0f} %",
            "action": "hydro_off",
            "when": "below",
            "hint": "Geplante Hydro-Abschaltung",
            "active": False,
        })
    if reserve_floor is not None:
        thresholds.append({
            "id": "reserve",
            "soc": round(_f(reserve_floor), 1),
            "label": f"Reserve {_f(reserve_floor):.0f} %",
            "action": "reserve",
            "when": "below",
            "hint": "Reserve-Boden nach Hydro-Aus",
            "active": False,
        })

    cons_deg = _f(angle.get("angle_deg"))
    cons_steil = _f(angle.get("steil_threshold_deg"), 19.0)
    cons_shape_corr = str(angle.get("shape") or "") in ("abend_steil", "abend_flach")

    display = {
        "goal": goal,
        "goal_short": line_action_label,
        "line_action": line_action,
        "line_action_label": line_action_label,
        "angle_plain": angle_plain,
        "chart_caption": chart_caption,
        "now_hour": round(now_h, 2),
        "end_hour": round(end_h, 2),
        "thresholds": thresholds,
        "angles": {
            "soll_deg": round(soll_deg, 1),
            "ist_deg": round(ist_deg, 1),
            "delta_deg": round(delta_deg, 1),
            "corr_threshold_deg": _ANGLE_CORR_DEG,
            "corr_threshold_kw": _SLOPE_ERROR_KW,
            "corr_active": bool(angle_corr and min_soc < soc < max_soc),
            "soll_slope_pct_per_h": round(soll_slope_pct_h, 3),
            "ist_slope_pct_per_h": round(ist_slope_pct_h, 3),
            "consumption_deg": round(cons_deg, 1),
            "consumption_steil_deg": round(cons_steil, 1),
            "consumption_shape_corr": cons_shape_corr,
        },
        "rules": [
            {
                "id": "notaus",
                "title": "Notabschaltung",
                "rule": f"SoC unter {min_soc:.0f} %",
                "effect": "alle Lasten aus",
                "active": soc <= min_soc,
            },
            {
                "id": "einschalt",
                "title": "Einschaltung",
                "rule": f"SoC über {max_soc:.0f} %",
                "effect": "Last einschalten",
                "active": soc >= max_soc,
            },
            {
                "id": "halt",
                "title": "Halt-Band",
                "rule": f"SoC ≤ {release_soc:.0f} % nach Maximum",
                "effect": "alle aus bis Max",
                "active": soc <= release_soc and bool(state.get("max_soc_hold_released")),
            },
            {
                "id": "winkel",
                "title": "Lastkorrektur",
                "rule": (
                    f"Winkel ändert sich um ≥ {_ANGLE_CORR_DEG:.0f}° "
                    f"(oder |Fehler| > {_SLOPE_ERROR_KW:.1f} kW)"
                ),
                "effect": "Last hoch oder runter",
                "active": bool(angle_corr and min_soc < soc < max_soc),
            },
        ],
    }

    try:
        soc_target_by_hour = {
            int(round(float(p["hour"]))): _f(p.get("target_soc"))
            for p in points
            if p.get("hour") is not None
        }
        stromprofil = build_stromprofil(config, state, live, now, soc_target_by_hour)
        if not (stromprofil.get("hours") or []):
            stromprofil = _empty_stromprofil(int(now_h), stromprofil.get("reason") or "empty")
    except Exception as e:
        stromprofil = _empty_stromprofil(int(now_h), str(e))

    return {
        "ok": True,
        "updated_at": now.isoformat(),
        "phase": phase,
        "soc_percent": round(soc, 1),
        "min_soc_percent": min_soc,
        "max_soc_percent": max_soc,
        "max_soc_release_percent": round(release_soc, 1),
        "hard_max_soc_percent": hard_max,
        "morning_soc_percent": morning,
        "plan_max_soc_percent": max_soc,
        "storage_kwh": storage,
        "peak_hour": peak_h,
        "hours_to_peak": round(hours_to_peak, 2),
        "desired_charge_kw": round(desired_smooth, 2),
        "ist_charge_kw": round(ist_charge, 2),
        "slope_error_kw": round(slope_error_kw, 2),
        "steer": steer,
        "points": points,
        "ist_points": ist_points,
        "consumption_angle": angle,
        "stromprofil": stromprofil,
        "display": display,
        "formula": (
            f"Ziel: {goal} Soll-Ladung {desired_smooth:.1f} kW "
            f"(Ist {ist_charge:.1f} kW, Fehler {slope_error_kw:+.1f} kW, "
            f"Winkel Soll {soll_deg:.0f}° / Ist {ist_deg:.0f}° / Δ {delta_deg:+.0f}°)."
        ),
        "sketch": {
            "green": "PV Plan / Ist",
            "orange": "Last Plan / Ist",
            "blue": "SoC Ist (durchgezogen)",
            "cyan": "SoC Bilanz-Plan (gestrichelt)",
            "violet": "SoC Steuerungsziel (gestrichelt)",
        },
    }


def clear_stale_evening_hold(state: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """
    Abend-Hold nur bis zum nächsten PV-Start des Folgetags.
    Am neuen Morgen (nach Hold-Ende oder neuer Kalendertag nach Peak) lösen.
    """
    now = now or datetime.now()
    raw = state.get("anchor_stretch_hold_until")
    if not raw:
        return False
    try:
        hold_until = datetime.fromisoformat(str(raw))
    except ValueError:
        state.pop("anchor_stretch_hold_until", None)
        return True
    if now >= hold_until:
        state.pop("anchor_stretch_hold_until", None)
        return True
    # Hold zeigt auf übermorgen / zu weit: korrigieren wenn > 20 h in Zukunft
    if (hold_until - now).total_seconds() > 20 * 3600:
        state.pop("anchor_stretch_hold_until", None)
        return True
    # Neuer Tag, PV-Fenster beginnt (ab 5 Uhr) und SoC nahe Morgenziel → Hold weg
    if now.hour >= 5 and hold_until.date() > now.date():
        state.pop("anchor_stretch_hold_until", None)
        return True
    return False
