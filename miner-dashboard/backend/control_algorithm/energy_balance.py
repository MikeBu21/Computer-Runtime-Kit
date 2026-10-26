# -*- coding: utf-8 -*-
"""Stündliche Energiebilanz, Risikofaktor und Nacht-Sperre."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.config import device_label_map, normalize_device_priority
from control_algorithm.consumption_history import (
    actual_base_consumption_kwh,
    actual_total_consumption_kwh,
    expected_base_consumption_kwh,
    expected_base_full_day_kwh,
    expected_hydro_full_day_kwh,
    expected_hydro_production_kwh,
    get_hourly_actual_soc_today,
    get_hourly_consumption_breakdown_today,
    get_weekday_profile,
    load_history,
    profile_sample_count,
    weekday_base_consumption_detail,
)
from control_algorithm.planner import (
    PRIORITY_LABEL,
    enabled_devices,
    miner_load_kw,
    next_soc_target,
    sort_for_pause,
)
# #region agent log
def _agent_debug_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    run_id: str = "pre-fix",
) -> None:
    try:
        with open(
            "/home/piminer/.cursor/debug-logs/debug-5c5bad.log",
            "a",
            encoding="utf-8",
        ) as _f:
            _f.write(
                json.dumps(
                    {
                        "sessionId": "5c5bad",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                        "runId": run_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


# #endregion

from control_algorithm.weather_survival import (
    daily_pv_kwh_for_date,
    export_avoidance_recommended,
    merged_soc_targets_in_horizon,
    should_block_proactive_discharge,
    weather_context,
)
from control_algorithm.soc_calibration import (
    apply_net_to_battery_kwh,
    ensure_soc_learning_defaults,
    normalize_base_load_scale,
    normalize_battery_efficiency,
)

DEFAULT_HORIZON_DAYS = 7


def _scaled_base_consumption_kwh(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    start: datetime,
    end: datetime,
) -> float:
    """Plan-Grundverbrauch × gelernte base_load_scale."""
    ensure_soc_learning_defaults(state)
    base = planned_base_consumption_kwh(config, base_dir, start, end, state=state)
    return float(base or 0.0) * normalize_base_load_scale(state.get("base_load_scale"))


def _step_battery_kwh(
    state: Dict[str, Any],
    kwh: float,
    net_kwh: float,
    storage: float,
    max_soc: float,
) -> float:
    """Nettoenergie mit gelerntem Akku-Wirkungsgrad auf Speicher anwenden."""
    ensure_soc_learning_defaults(state)
    return apply_net_to_battery_kwh(
        kwh,
        net_kwh,
        storage,
        max_soc,
        normalize_battery_efficiency(state.get("battery_efficiency")),
        _cap_battery_kwh,
    )


def _plant_perfect_day_kwh(config: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> float:
    """Anlagen-Referenz (kWh/Tag) – Schwellen relativ dazu, nicht für 190-kWp-Parks."""
    try:
        from control_algorithm.reference_anchor import effective_perfect_day_kwh

        ref = float(effective_perfect_day_kwh(state or {}, config) or 0)
        if ref > 1.0:
            return ref
    except Exception:
        pass
    pv_kwp = float(config.get("pv_kwp") or 0)
    sun_h = float(config.get("perfect_weather_sun_hours") or 6.0)
    return max(20.0, pv_kwp * sun_h)


def planning_horizon_days(config: Dict[str, Any]) -> int:
    try:
        d = int(config.get("planning_horizon_days") or DEFAULT_HORIZON_DAYS)
    except (TypeError, ValueError):
        d = DEFAULT_HORIZON_DAYS
    return max(1, min(14, d))


def _binding_constraint_label(binding: Optional[str]) -> str:
    if not binding or binding == "none":
        return "keine"
    if binding == "max_soc":
        return "Speicher-Obergrenze (Max-SoC)"
    if binding == "discharge_above_max":
        return "Entladen (Speicher über Max-SoC)"
    if binding == "proactive_discharge":
        return "Proaktives Entladen (voller Speicher, PV morgen)"
    if binding == "near_term_discharge":
        return "Kurzfrist-Entladung (heute/morgen, Export vermeiden)"
    if binding == "export_avoidance":
        return "Export-Vermeidung (PV nutzen, Speicher voll)"
    if binding == "survival_mode":
        return "Überlebensmodus (echte Knappheit)"
    if binding == "survival_surplus_ok":
        return "Schlechtwetter-Label, Energie reicht"
    if binding == "pre_storm_charge":
        return "Aufladen vor Schlechtwetter"
    if binding.startswith("checkpoint_"):
        parts = binding.split("_", 2)
        if len(parts) >= 3:
            return f"SOC-Ziel {parts[1]} am {parts[2]}"
        return "SOC-Checkpoint"
    return str(binding)


def _build_steering_summary(
    *,
    day_balance_ok: bool,
    day_balance_kwh: float,
    all_devices_fit: bool,
    budget_kw: float,
    total_need_kw: float,
    running_need_kw: float,
    surplus_kwh: float,
    hours_left: float,
    binding_constraint: Optional[str],
    night_lock_active: bool,
    horizon_display: str,
    checkpoints: List[Dict[str, Any]],
    soc_percent: Optional[float],
    target_soc: Optional[float],
    max_soc_percent: Optional[float] = None,
) -> Dict[str, Any]:
    """Erklärt warum Steuerung (7-Tage) von Tagesbilanz (heute) abweichen kann."""
    binding_label = _binding_constraint_label(binding_constraint)
    failed_cps = [c for c in checkpoints if not c.get("reachable", True)]
    max_kw = round(max(0.0, budget_kw), 2)
    limited = day_balance_ok and not all_devices_fit
    max_soc_cfg = float(max_soc_percent or 95.0)
    over_max = soc_percent is not None and soc_percent > max_soc_cfg + 0.3
    blocked = night_lock_active or (max_kw < 0.05 and not over_max)

    if over_max:
        headline = "Speicher voll – Miner zum Entladen"
        detail = (
            f"Akku bei {soc_percent:.0f} % über der eingestellten Obergrenze. "
            f"Miner werden eingeschaltet, um den Speicher zu entladen – nicht abgeschaltet."
        )
    elif soc_percent is not None and max_soc_cfg and soc_percent >= max_soc_cfg - 10:
        headline = "Speicher hoch – Entladen vor PV"
        detail = (
            f"Akku bei {soc_percent:.0f} %. Bei starker PV-Prognose wird entladen, "
            f"damit morgens Platz Richtung SOC-Ziel (~40 %) bleibt und Einspeisung vermieden wird."
        )
    elif night_lock_active:
        headline = "Nacht-Sperre – keine Einschaltungen"
        detail = (
            f"Die 7-Tage-Prognose erreicht mindestens einen SOC-Checkpoint "
            f"(je 40 % um 07:00) nicht. Bindend: {binding_label}."
        )
    elif blocked:
        headline = "Kein Miner-Budget – Speicher an der Untergrenze"
        detail = (
            f"Akku bei {soc_percent if soc_percent is not None else '?'} % – "
            f"für die nächsten {hours_left:.0f} h ist kein sicheres Miner-Budget frei, "
            f"ohne unter {target_soc or 40}% zu fallen."
        )
    elif limited:
        headline = "Tagesbilanz reicht – 7-Tage-Budget limitiert"
        detail = (
            f"Heute rechnerisch {day_balance_kwh:+.0f} kWh Überschuss, aber über "
            f"{horizon_display} reichen {surplus_kwh:.0f} kWh nur für ca. {max_kw:.1f} kW "
            f"(alle Miner bräuchten {total_need_kw:.1f} kW). "
            f"Bindend: {binding_label}. Es werden höchstens {max_kw:.1f} kW gleichzeitig "
            f"freigegeben (Priorität, schrittweise Einschaltung)."
        )
    elif not all_devices_fit:
        headline = "7-Tage-Budget knapp"
        detail = (
            f"Budget {surplus_kwh:.0f} kWh / {max_kw:.1f} kW über {hours_left:.0f} h – "
            f"Gesamtbedarf {total_need_kw:.1f} kW. Bindend: {binding_label}."
        )
    elif day_balance_ok:
        headline = "Tagesbilanz und 7-Tage-Budget passen"
        detail = (
            f"Alle Miner können bis {horizon_display} laufen "
            f"({surplus_kwh:.0f} kWh Budget, {max_kw:.1f} kW)."
        )
    else:
        headline = "Tagesbilanz negativ"
        detail = (
            f"Heute {day_balance_kwh:+.0f} kWh – Steuerung nutzt trotzdem den "
            f"7-Tage-Horizont ({surplus_kwh:.0f} kWh Miner-Budget)."
        )

    return {
        "headline": headline,
        "detail": detail,
        "horizon_scope": (
            f"Steuerung plant rollierend {DEFAULT_HORIZON_DAYS} Tage voraus "
            f"(bis {horizon_display}). Die Tagesbilanz oben gilt nur für diesen Kalendertag."
        ),
        "day_balance_ok": day_balance_ok,
        "all_devices_fit": all_devices_fit,
        "steering_limited_by_horizon": limited or blocked or night_lock_active,
        "budget_kw": max_kw,
        "total_need_kw": round(total_need_kw, 2),
        "running_need_kw": round(running_need_kw, 2),
        "binding_constraint": binding_constraint,
        "binding_label": binding_label,
        "failed_checkpoints": failed_cps,
        "night_lock_active": night_lock_active,
    }


def normalize_risk_factor(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 100.0)
    except (TypeError, ValueError):
        v = 100.0
    return max(0.0, min(100.0, v))


def normalize_consumption_risk_percent(raw: Any) -> float:
    """0–1000 %: Aufschlag auf historischen Verbrauch."""
    try:
        v = float(raw if raw is not None else 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(1000.0, v))


def consumption_risk_scale(config: Dict[str, Any]) -> float:
    """
    Multiplikator für Plan-Verbrauch.
    0 % → 1.0× (historisch), 100 % → 2.0×, 1000 % → 11.0×.
    """
    return 1.0 + normalize_consumption_risk_percent(
        config.get("consumption_risk_percent")
    ) / 100.0


def planned_base_consumption_kwh(
    config: Dict[str, Any],
    base_dir: str,
    start: datetime,
    end: datetime,
    state: Optional[Dict[str, Any]] = None,
) -> float:
    """Historischer Basisverbrauch × Verbrauchsrisiko × optional Muster-Scale."""
    raw = expected_base_consumption_kwh(base_dir, start, end)
    scaled = float(raw or 0.0) * consumption_risk_scale(config)
    if state is not None:
        try:
            from control_algorithm.consumption_pattern_estimate import (
                consumption_pattern_scale,
            )

            scaled *= consumption_pattern_scale(state, config)
        except Exception:
            pass
        try:
            from control_algorithm.storage_band_learning import band_consumption_plan_scale

            scaled *= band_consumption_plan_scale(config, state, live=None)
        except Exception:
            pass
    return scaled

def _pv_kwh_in_hour_range(
    hours_dict: Dict[str, float],
    start_hour: int,
    end_hour: int,
    start_frac: float = 0.0,
    end_frac: float = 1.0,
) -> float:
    """Summiert PV-Prognose kWh für Stunden start_hour…end_hour (0–23)."""
    total = 0.0
    for h in range(max(0, start_hour), min(23, end_hour) + 1):
        hid = f"h{h:02d}"
        val = float(hours_dict.get(hid, 0) or 0)
        if h == start_hour and h == end_hour:
            total += val * max(0.0, end_frac - start_frac)
        elif h == start_hour:
            total += val * max(0.0, 1.0 - start_frac)
        elif h == end_hour:
            total += val * max(0.0, end_frac)
        else:
            total += val
    return round(total, 4)


def _last_available_pv_hours(by_date: Dict[str, Any], d: date) -> Dict[str, float]:
    """Letzte bekannte Wetter-PV-Stunden (gleicher Wochentag bevorzugt)."""
    if not by_date:
        return {}
    try:
        keys = sorted(
            k
            for k, entry in by_date.items()
            if isinstance(entry, dict) and (entry.get("hours") or {})
        )
        if not keys:
            return {}
        same_wd = [
            k for k in keys if date.fromisoformat(str(k)[:10]).weekday() == d.weekday()
        ]
        pick = same_wd[-1] if same_wd else keys[-1]
        hours = (by_date.get(pick) or {}).get("hours") or {}
        return dict(hours) if hours else {}
    except Exception:
        return {}


def _pv_hours_for_date(state: Dict[str, Any], d: date, anchor: Optional[date] = None) -> Dict[str, float]:
    """Stunden-PV-Prognose für ein Kalenderdatum (Wetterdaten; über Prognoseende: Hold-Last)."""
    anchor = anchor or datetime.now().date()
    offset = (d - anchor).days
    # Intraday-Override (Musterschätzung): skalierte Stunden nur für heute
    if offset <= 0:
        try:
            from control_algorithm.pv_pattern_estimate import effective_today_hours

            eff = effective_today_hours(state)
            if eff:
                return dict(eff)
        except Exception:
            pass
    by_date = state.get("pv_forecast_by_date") or {}
    entry = by_date.get(d.isoformat())
    if entry and entry.get("hours"):
        return dict(entry["hours"])
    if offset <= 0:
        today_h = state.get("pv_forecast_today_hours") or {}
        return dict(today_h) if today_h else _last_available_pv_hours(by_date, d)
    if offset == 1:
        tomorrow_h = state.get("pv_forecast_tomorrow_hours") or {}
        if tomorrow_h:
            return dict(tomorrow_h)
    if offset == 2:
        day_after_h = state.get("pv_forecast_day_after_hours") or {}
        if day_after_h:
            return dict(day_after_h)
    # Über OMS-Prognoseende hinaus: keine 0-PV-Löcher – Algo 2 stützt sich auf
    # die erhaltenen Wetterdaten (letzter bekannter Tag / gleicher Wochentag).
    held = _last_available_pv_hours(by_date, d)
    if held:
        return held
    return {}


def _pv_forecast_kwh(
    state: Dict[str, Any],
    start: datetime,
    end: datetime,
) -> float:
    """PV-Prognose kWh zwischen start und end aus State (heute/morgen/übermorgen Stunden-Slots)."""
    total = 0.0

    if start.date() == end.date():
        frac_start = (start.minute + start.second / 60.0) / 60.0
        frac_end = (end.minute + end.second / 60.0) / 60.0
        hours = _pv_hours_for_date(state, start.date())
        return _pv_kwh_in_hour_range(hours, start.hour, end.hour, frac_start, frac_end)

    # Rest des Starttages (inkl. Stunde 23)
    frac_start = (start.minute + start.second / 60.0) / 60.0
    hours_start = _pv_hours_for_date(state, start.date())
    total += _pv_kwh_in_hour_range(hours_start, start.hour, 23, frac_start, 1.0)

    # Volle Zwischentage
    d = start.date() + timedelta(days=1)
    while d < end.date():
        hours = _pv_hours_for_date(state, d)
        total += sum(float(v or 0) for v in (hours or {}).values())
        d += timedelta(days=1)

    # Anfang des Endtages
    if end.date() > start.date():
        frac_end = (end.minute + end.second / 60.0) / 60.0
        hours_end = _pv_hours_for_date(state, end.date())
        total += _pv_kwh_in_hour_range(hours_end, 0, end.hour, 0.0, frac_end)

    return round(total, 4)


def simulate_battery_horizon(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    now: datetime,
    *,
    start_kwh: float,
    miner_kw: float = 0.0,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    apply_risk: bool = True,
    miner_kw_near: Optional[float] = None,
    near_term_hours: Optional[float] = None,
    use_checkpoints: bool = True,
) -> Dict[str, Any]:
    """
    Stündliche Akku-Simulation über rollierenden Horizont.

    Optional Zwei-Phasen-Last:
      miner_kw_near für die ersten near_term_hours, danach miner_kw (nachhaltig).

    use_checkpoints=False: nur kontinuierlicher Min-/Max-SoC (für Reserve-Suche,
    vermeidet Rekursion mit autonomen Abendzielen).
    """
    horizon_days = horizon_days or planning_horizon_days(config)
    storage = float(config.get("storage_kwh") or 0)
    hard_max = float(config.get("max_soc_percent") or 95.0)
    min_soc_floor = float(config.get("min_soc_percent") or 35.0)
    caps = _planning_band_caps(config, state)
    # Planung: Band-Decke; Notfall-Min bleibt hartes Config-Min
    max_soc = float(caps["plan_max"])
    plan_min = float(caps["plan_min"])
    pv_scale = float(caps["pv_scale"])
    max_kwh = storage * max_soc / 100.0 if storage > 0 else 0.0
    risk = (
        normalize_risk_factor(config.get("risk_factor_percent")) / 100.0
        if apply_risk
        else 1.0
    )
    horizon_end = now + timedelta(days=horizon_days)
    if use_checkpoints:
        checkpoints_raw = merged_soc_targets_in_horizon(config, state, now, horizon_days)
    else:
        checkpoints_raw = []
    checkpoint_dts = {
        datetime.fromisoformat(cp["target_datetime"]): cp for cp in checkpoints_raw
    }

    kwh = start_kwh
    start_soc_pct = _kwh_to_soc(start_kwh, storage) or 0.0
    over_max_at_start = start_soc_pct > max_soc + 0.3
    min_soc_pct = 100.0
    max_soc_pct = start_soc_pct
    checkpoint_results: List[Dict[str, Any]] = []
    checkpoint_hit: set = set()
    binding_constraint: Optional[str] = None
    use_phases = (
        miner_kw_near is not None
        and near_term_hours is not None
        and float(near_term_hours) > 0
    )

    cursor = now
    step = timedelta(hours=1)
    while cursor < horizon_end:
        next_cursor = min(cursor + step, horizon_end)
        hours_elapsed = (cursor - now).total_seconds() / 3600.0

        if use_phases and hours_elapsed < float(near_term_hours):
            load_kw = float(miner_kw_near)
        else:
            load_kw = float(miner_kw)

        # Segment an Checkpoint-Zeiten splitten, damit SoC am Zielzeitpunkt
        # und nicht erst nach der vollen Stunde bewertet wird.
        segment_ends = [next_cursor]
        for cp_dt in checkpoint_dts:
            if cursor < cp_dt <= next_cursor:
                segment_ends.append(cp_dt)
        segment_ends = sorted(set(segment_ends))

        seg_start = cursor
        for seg_end in segment_ends:
            frac = (seg_end - seg_start).total_seconds() / 3600.0
            if frac <= 0:
                continue

            base = _scaled_base_consumption_kwh(
                config, state, base_dir, seg_start, seg_end
            )
            hydro = expected_hydro_production_kwh(base_dir, seg_start, seg_end)
            pv = _pv_forecast_kwh(state, seg_start, seg_end) * pv_scale
            production = pv * risk + hydro

            net_kwh = production - base - load_kw * frac
            kwh = _step_battery_kwh(state, kwh, net_kwh, storage, max_soc)

            soc_pct = _kwh_to_soc(kwh, storage)
            if soc_pct is not None:
                min_soc_pct = min(min_soc_pct, soc_pct)
                max_soc_pct = max(max_soc_pct, soc_pct)
                if (
                    load_kw > 0
                    and soc_pct < min_soc_floor
                    and binding_constraint is None
                ):
                    binding_constraint = "min_soc"
                elif (
                    load_kw > 0
                    and soc_pct < plan_min
                    and binding_constraint is None
                    and float(caps.get("underfill_risk") or 0) >= 0.15
                ):
                    # Soft-Floor aus Band (nicht Notfall-Min)
                    binding_constraint = "plan_min_soc"
                if (
                    not over_max_at_start
                    and soc_pct >= max_soc - 0.05
                    and binding_constraint is None
                    and load_kw > 0
                ):
                    binding_constraint = "max_soc"

            if seg_end in checkpoint_dts and seg_end not in checkpoint_hit:
                cp_info = checkpoint_dts[seg_end]
                target_soc = float(cp_info["soc_percent"])
                projected = soc_pct if soc_pct is not None else 0.0
                reachable = projected >= target_soc - 0.5
                checkpoint_results.append(
                    {
                        "time": cp_info.get("time"),
                        "target_soc": target_soc,
                        "projected_soc": projected,
                        "projected_kwh": round(kwh, 2),
                        "target_datetime": cp_info["target_datetime"],
                        "reachable": reachable,
                        "dynamic_reason": cp_info.get("dynamic_reason"),
                    }
                )
                checkpoint_hit.add(seg_end)
                if not reachable and binding_constraint is None:
                    reason = cp_info.get("dynamic_reason") or "checkpoint"
                    if reason == "pre_storm_charge":
                        binding_constraint = "pre_storm_charge"
                    else:
                        binding_constraint = (
                            f"checkpoint_{cp_info.get('time')}_{seg_end.date().isoformat()}"
                        )

            seg_start = seg_end

        cursor = next_cursor

    all_checkpoints_ok = all(cp.get("reachable", True) for cp in checkpoint_results)
    # Bereits über Max-SoC: Miner entladen ist erwünscht – kein Budget-Stop
    max_soc_ok = over_max_at_start or (max_soc_pct <= max_soc + 0.05)

    return {
        "horizon_days": horizon_days,
        "horizon_end": horizon_end.isoformat(),
        "hours_left": round((horizon_end - now).total_seconds() / 3600.0, 2),
        "checkpoints": checkpoint_results,
        "min_soc_in_horizon": round(min_soc_pct, 2) if min_soc_pct <= 100 else None,
        "max_soc_in_horizon": round(max_soc_pct, 2),
        "max_soc_percent": max_soc,
        "hard_max_soc_percent": hard_max,
        "plan_min_soc_percent": plan_min,
        "band_pv_scale": pv_scale,
        "over_max_at_start": over_max_at_start,
        "start_soc_percent": round(start_soc_pct, 2),
        "projected_kwh_at_horizon_end": round(kwh, 2),
        "projected_soc_at_horizon_end": _kwh_to_soc(kwh, storage),
        "all_checkpoints_ok": all_checkpoints_ok,
        "max_soc_ok": max_soc_ok,
        "binding_constraint": binding_constraint,
        "miner_kw": round(float(miner_kw), 3),
        "miner_kw_near": round(float(miner_kw_near), 3) if miner_kw_near is not None else None,
        "near_term_hours": round(float(near_term_hours), 2) if near_term_hours else None,
    }


def _max_miner_kw_for_horizon(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: datetime,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> Tuple[float, str, Dict[str, Any]]:
    """Maximale Miner-Leistung über den Horizont (kW) + optional Near-Term-Meta."""
    empty_meta: Dict[str, Any] = {}
    horizon_days = horizon_days or planning_horizon_days(config)
    storage = float(config.get("storage_kwh") or 0)
    soc = live.get("soc_percent")
    if storage <= 0 or soc is None:
        return 0.0, "no_storage", empty_meta

    start_kwh = float(soc) / 100.0 * storage
    total_kw = sum(float(d.get("power_kw") or 0) for d in enabled_devices(config))
    caps = _planning_band_caps(config, state, live=live)
    hard_max = float(caps["hard_max"])
    max_soc = float(caps["plan_max"])  # Band-Decke für Budget-Suche
    min_soc = float(config.get("min_soc_percent") or 35.0)
    plan_min = float(caps["plan_min"])
    underfill = float(caps.get("underfill_risk") or 0)
    over_max = float(soc) > hard_max + 0.3
    hi = max(1.0, total_kw * 2.0, 50.0)
    lo = 0.0
    binding = "none"
    aspirational_floor = max(70.0, min_soc + 35.0)
    # Bei Underfill: Soft-Floor anheben → konservativeres Budget
    soft_min = max(min_soc, plan_min) if underfill >= 0.15 else min_soc

    def _only_aspirational_evening_fail(sim: Dict[str, Any]) -> bool:
        """True wenn nur unerreichbare hohe Abendziele scheitern, Min-SoC ok."""
        min_h = sim.get("min_soc_in_horizon")
        if min_h is not None and float(min_h) < soft_min - 0.05:
            return False
        failed = [c for c in (sim.get("checkpoints") or []) if not c.get("reachable")]
        if not failed:
            return False
        for c in failed:
            tgt = float(c.get("target_soc") or 0)
            reason = str(c.get("dynamic_reason") or "")
            if reason in ("autonomous_min_soc", "pre_storm_charge"):
                return False
            if reason == "autonomous_evening_reserve":
                if tgt < aspirational_floor:
                    return False
                continue
            # Sonstige Checkpoints: nur soft, wenn Ziel nahe Plan-Max-SoC
            if tgt < max(aspirational_floor, max_soc - 1.0):
                return False
        return True

    def _sim_ok(sim: Dict[str, Any], soft_aspirational: bool = False) -> bool:
        checkpoints_ok = bool(sim.get("all_checkpoints_ok"))
        if not checkpoints_ok:
            if not (soft_aspirational and _only_aspirational_evening_fail(sim)):
                return False
        min_h = sim.get("min_soc_in_horizon")
        if min_h is not None and float(min_h) < soft_min - 0.05:
            return False
        if over_max:
            return True
        return bool(sim.get("max_soc_ok"))

    if over_max and total_kw > 0:
        # Über hartem Max-SoC: volle Entladung freigeben (Export/Überladung vermeiden)
        return round(total_kw, 3), "discharge_above_max", {
            "plan_max_soc_percent": max_soc,
            "plan_min_soc_percent": plan_min,
        }

    for _ in range(24):
        mid = (lo + hi) / 2.0
        sim = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=mid,
            horizon_days=horizon_days,
        )
        if _sim_ok(sim):
            lo = mid
            binding = sim.get("binding_constraint") or "none"
        else:
            hi = mid

    if lo <= 0.001:
        sim0 = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=0.0,
            horizon_days=horizon_days,
        )
        binding = sim0.get("binding_constraint") or "none"
        if over_max and total_kw > 0:
            binding = "discharge_above_max"
        elif _only_aspirational_evening_fail(sim0):
            # Wetter gut / Min-SoC ok, aber aspirationales Abendziel (z. B. Max-SoC)
            # unerreichbar: Budget nach realer Reserve suchen, nicht auf 0 klemmen.
            lo_s, hi_s = 0.0, max(1.0, total_kw * 2.0, 50.0)
            for _ in range(24):
                mid = (lo_s + hi_s) / 2.0
                sim = simulate_battery_horizon(
                    config,
                    state,
                    base_dir,
                    now,
                    start_kwh=start_kwh,
                    miner_kw=mid,
                    horizon_days=horizon_days,
                )
                if _sim_ok(sim, soft_aspirational=True):
                    lo_s = mid
                    binding = (
                        "weather_soft_evening"
                        if not sim.get("all_checkpoints_ok")
                        else (sim.get("binding_constraint") or "none")
                    )
                else:
                    hi_s = mid
            lo = lo_s

    proactive = _proactive_discharge_kw(config, state, live, now)
    if proactive is not None and proactive > lo:
        sim_p = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=proactive,
            horizon_days=horizon_days,
        )
        if sim_p.get("all_checkpoints_ok"):
            lo = max(lo, proactive)
            binding = "proactive_discharge"
        elif float(soc) >= max_soc - 5:
            sim_full = simulate_battery_horizon(
                config,
                state,
                base_dir,
                now,
                start_kwh=start_kwh,
                miner_kw=total_kw,
                horizon_days=horizon_days,
            )
            if sim_full.get("all_checkpoints_ok"):
                lo = max(lo, total_kw)
                binding = "proactive_discharge"

    export_kw = _export_avoidance_kw(config, state, live, now)
    if export_kw is not None and export_kw > lo:
        sim_e = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=export_kw,
            horizon_days=horizon_days,
        )
        if sim_e.get("all_checkpoints_ok"):
            lo = max(lo, export_kw)
            binding = "export_avoidance"
        else:
            # Live-Einspeisung: trotzdem Budget anheben (Priorität: keine Einspeisung).
            # 7-Tage-Sim kann ablehnen (max_soc), aber Live-Export + Min-SoC-Schutz
            # werden im Load-Scheduler abgesichert.
            from control_algorithm.weather_survival import live_grid_export_kw as _live_export

            from control_algorithm.steering_learning import export_threshold_kw as _export_thr

            if _live_export(live) >= _export_thr(state):
                lo = max(lo, export_kw)
                binding = "export_avoidance"

    # Kurzfrist-Boost (heute/morgen): 7-Tage-Gleichlast unterschätzt sonnige Tage.
    # Zwei-Phasen: hohes Budget jetzt, danach nachhaltiges Langfrist-Budget –
    # nur wenn alle 7-Tage-Checkpoints (inkl. Pre-Storm-Reserve) halten.
    near_meta: Dict[str, Any] = {}
    if horizon_days >= 3 and total_kw > 0:
        near_kw, near_binding, near_meta = _near_term_discharge_boost(
            config,
            state,
            live,
            base_dir,
            now,
            start_kwh=start_kwh,
            long_term_kw=lo,
            total_kw=total_kw,
            max_soc=max_soc,
        )
        if near_kw is not None and near_kw > lo + 0.3:
            lo = near_kw
            binding = near_binding or "near_term_discharge"

    ctx = weather_context(config, state, days=horizon_days)
    today_mode = (ctx.get("days") or [{}])[0].get("steering_mode") if ctx.get("days") else "normal"
    if today_mode == "survival_mode" and total_kw > 0 and lo > 0.05:
        # Nur drosseln wenn die berechnete Last die Checkpoints wirklich gefährdet.
        # Große PV/Speicher + kleines Gerät: Survival-Label allein → kein 12%-Deckel.
        sim_full = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=lo,
            horizon_days=horizon_days,
        )
        if sim_full.get("all_checkpoints_ok"):
            # Überschuss: Gerät(e) dürfen weiterlaufen (ggf. 24h)
            if binding in ("none", ""):
                binding = "survival_surplus_ok"
        else:
            # Echte Knappheit: höchstes Last-Niveau suchen, das Checkpoints hält
            hi_s = lo
            lo_s = 0.0
            best = 0.0
            for _ in range(18):
                mid = (hi_s + lo_s) / 2.0
                sim_m = simulate_battery_horizon(
                    config,
                    state,
                    base_dir,
                    now,
                    start_kwh=start_kwh,
                    miner_kw=mid,
                    horizon_days=horizon_days,
                )
                if sim_m.get("all_checkpoints_ok"):
                    best = mid
                    lo_s = mid
                else:
                    hi_s = mid
            lo = round(best, 3)
            binding = "survival_mode"

    near_meta = dict(near_meta or {})
    near_meta.setdefault("plan_max_soc_percent", max_soc)
    near_meta.setdefault("plan_min_soc_percent", plan_min)
    near_meta.setdefault("band_underfill_risk", underfill)
    near_meta.setdefault("band_overfill_risk", float(caps.get("overfill_risk") or 0))
    near_meta.setdefault("soft_min_soc_percent", soft_min)
    return round(lo, 3), str(binding), near_meta


def _near_term_discharge_boost(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: datetime,
    *,
    start_kwh: float,
    long_term_kw: float,
    total_kw: float,
    max_soc: float,
) -> Tuple[Optional[float], Optional[str], Dict[str, Any]]:
    """
    Zwei-Phasen-Boost: heute/morgen mehr Miner, danach nachhaltiges Budget.

    Erlaubt nur so viel Near-Term-Last, dass bei Simulation
      [nahe Stunden @ near_kw] → [Rest @ sustained_kw]
    alle 7-Tage-Checkpoints halten (40 %-Ziele + Pre-Storm-Reserve ≈ Max-SoC).

    sustained_kw liegt bewusst unter dem 7-Tage-Maximum (Sicherheitsmarge),
    damit nach dem Entladen noch Reserve für schlechtere Folgetage bleibt.
    """
    empty: Dict[str, Any] = {}
    try:
        from control_algorithm.pv_pattern_estimate import effective_today_pv_kwh

        today_pv = float(effective_today_pv_kwh(state))
    except Exception:
        today_pv = float(state.get("pv_yield_today_kwh") or 0)
    tomorrow_pv = float(state.get("pv_yield_tomorrow_kwh") or 0)
    pv_live = float(live.get("pv_kw") or 0)
    soc = live.get("soc_percent")
    if soc is None:
        return None, None, empty

    # Niemals Near-Term-Entlade-Boost unter/nahe Min-SoC – sonst entlädt
    # ein hoher Tages-Surplus-Forecast die Batterie trotz Floor-Verletzung.
    min_soc_gate = float(config.get("min_soc_percent") or 35.0)
    if float(soc) < min_soc_gate + 8.0:
        return None, None, empty

    strong_ref = _plant_perfect_day_kwh(config, state)
    pv_kwp = float(config.get("pv_kwp") or 0)
    live_sun_kw = max(6.0, pv_kwp * 0.35)
    # Relativ zur Anlagengröße (früher hart 500/350/25 – blockierte 16-kWp-Anlagen)
    strong_sun = (
        today_pv + tomorrow_pv >= strong_ref * 1.15
        or tomorrow_pv >= strong_ref * 0.85
        or today_pv >= strong_ref * 0.85
        or (pv_live >= live_sun_kw and today_pv >= strong_ref * 0.45)
    )
    if not strong_sun:
        return None, None, empty

    # Boost nur bis heutiges PV-Ende (nicht über Nacht – sonst killen 5.5 kW
    # den Morgen-Min-SoC und das Budget bleibt ~1 kW trotz guter Wetterdaten).
    try:
        from control_algorithm.weather_survival import last_pv_hour_for_date

        last_h = int(last_pv_hour_for_date(state, now.date()) or 20)
    except Exception:
        last_h = 20
    last_h = max(16, min(22, last_h))
    phase_end = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=last_h, minute=0
    )
    near_hours = (phase_end - now).total_seconds() / 3600.0
    if near_hours < 2.0:
        return None, None, empty

    # Droht Max-SoC im Kurzhorizont mit langfristigem Budget?
    sim_probe = simulate_battery_horizon(
        config,
        state,
        base_dir,
        now,
        start_kwh=start_kwh,
        miner_kw=max(0.0, long_term_kw),
        horizon_days=2,
    )
    hits_max = float(sim_probe.get("max_soc_in_horizon") or 0) >= max_soc - 0.5
    near_full = float(soc) >= max_soc - 25
    if not hits_max and not near_full and pv_live < live_sun_kw:
        return None, None, empty

    full_horizon = planning_horizon_days(config)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    try:
        morning_floor = float(config.get("morning_soc_percent") or min_soc)
    except (TypeError, ValueError):
        morning_floor = min_soc
    morning_floor = max(min_soc, morning_floor)

    def _checkpoints_with_margin(sim: Dict[str, Any], margin: float = 1.5) -> bool:
        if not sim.get("all_checkpoints_ok"):
            return False
        for cp in sim.get("checkpoints") or []:
            proj = float(cp.get("projected_soc") or 0)
            tgt = float(cp.get("target_soc") or 0)
            if proj < tgt + margin:
                return False
        min_h = sim.get("min_soc_in_horizon")
        # Near-Term darf den Morgen-Floor (z. B. 33 %) nicht unterbieten
        if min_h is not None and float(min_h) < morning_floor + 1.0:
            return False
        if min_h is not None and float(min_h) < min_soc + 1.0:
            return False
        return True

    # Nachhaltige Phase: unter 7-Tage-Max, mit Marge für Folgetage
    hi_s = max(0.0, float(long_term_kw))
    lo_s = 0.0
    sustained = 0.0
    for _ in range(18):
        mid = (lo_s + hi_s) / 2.0
        sim_s = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=mid,
            horizon_days=full_horizon,
        )
        if _checkpoints_with_margin(sim_s, margin=2.0):
            sustained = mid
            lo_s = mid
        else:
            hi_s = mid
    if sustained <= 0.05:
        sustained = max(0.0, float(long_term_kw) * 0.75)

    # Nach PV-Ende: bei gutem Wetter in der Prüfung nachts Miner aus.
    # Sonst unterschätzt Dauerbetrieb über Nacht das Tages-Surplus (Hydro bleibt aus).
    night_kw = float(sustained)
    if today_pv >= strong_ref * 0.8 and tomorrow_pv >= strong_ref * 0.65:
        night_kw = 0.0

    # Near-Term so hoch wie möglich, solange Zwei-Phasen-Plan Checkpoints hält
    hi = max(night_kw, total_kw)
    lo = night_kw
    best: Optional[float] = None
    best_sim: Optional[Dict[str, Any]] = None

    for _ in range(22):
        mid = (lo + hi) / 2.0
        sim = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=start_kwh,
            miner_kw=night_kw,
            miner_kw_near=mid,
            near_term_hours=near_hours,
            horizon_days=full_horizon,
        )
        if sim.get("all_checkpoints_ok"):
            best = mid
            best_sim = sim
            lo = mid
        else:
            hi = mid

    if best is None or best <= max(night_kw, sustained) + 0.3:
        return None, None, empty

    reserve_note = None
    for cp in best_sim.get("checkpoints") or []:
        if cp.get("dynamic_reason") == "pre_storm_charge":
            reserve_note = (
                f"Reserve-Ziel {cp.get('target_soc')} % am "
                f"{str(cp.get('target_datetime') or '')[:16]} (vor Schlechtwetter)"
            )
            break

    meta = {
        "near_term_hours": round(near_hours, 1),
        "near_term_until": phase_end.isoformat(),
        "sustained_budget_kw": round(sustained, 3),
        "near_term_budget_kw": round(min(best, total_kw), 3),
        "night_budget_kw": round(night_kw, 3),
        "min_soc_in_plan": best_sim.get("min_soc_in_horizon"),
        "max_soc_in_plan": best_sim.get("max_soc_in_horizon"),
        "reserve_note": reserve_note,
        "plan_ok": True,
    }
    return round(min(best, total_kw), 3), "near_term_discharge", meta


def compute_horizon_projection(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> Dict[str, Any]:
    """Rollierende Mehr-Tage-Prognose mit SOC-Checkpoints und Max-SoC."""
    now = now or datetime.now()
    horizon_days = horizon_days or planning_horizon_days(config)
    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    risk = normalize_risk_factor(config.get("risk_factor_percent")) / 100.0
    soc = live.get("soc_percent")

    if storage <= 0 or soc is None:
        return {
            "reachable": True,
            "projected_soc": soc,
            "target_soc": None,
            "buffer_kwh": 0.0,
            "horizon_days": horizon_days,
            "note": "no_storage_or_soc",
        }

    current_soc = float(soc)
    current_kwh = current_soc / 100.0 * storage
    min_kwh = min_soc / 100.0 * storage
    buffer_kwh = max(0.0, current_kwh - min_kwh)

    # Harte Floor-Sperre: unter Min-SoC kein Miner-Budget (auch nicht Near-Term).
    if current_soc < min_soc:
        return {
            "reachable": False,
            "projected_soc": current_soc,
            "projected_kwh_raw": current_kwh,
            "target_soc": min_soc,
            "target_time": None,
            "target_datetime": None,
            "horizon_days": horizon_days,
            "horizon_end": (now + timedelta(days=horizon_days)).isoformat(),
            "hours_left": float(horizon_days * 24),
            "checkpoints": [],
            "min_soc_in_horizon": current_soc,
            "max_soc_in_horizon": current_soc,
            "max_soc_percent": max_soc,
            "binding_constraint": "min_soc_emergency",
            "binding_checkpoint": None,
            "over_max_at_start": False,
            "miner_surplus_kwh": 0.0,
            "miner_surplus_kwh_raw": 0.0,
            "miner_budget_kw": 0.0,
            "miner_budget_kw_raw": 0.0,
            "planned_runtime_hours": 0.0,
            "effective_runtime_hours": 0.0,
            "proactive_discharge_kw": None,
            "near_term_plan": {
                "near_term_budget_kw": 0.0,
                "sustained_budget_kw": 0.0,
                "night_budget_kw": 0.0,
                "plan_ok": False,
                "blocked_by": "min_soc_emergency",
            },
            "note": "min_soc_emergency",
            "buffer_kwh": round(buffer_kwh, 2),
            "risk_factor_percent": round(risk * 100.0, 1),
        }

    devices = enabled_devices(config)
    miner_status = live.get("miner_status") or {}
    current_miner_kw = miner_load_kw(devices, miner_status)

    sim = simulate_battery_horizon(
        config,
        state,
        base_dir,
        now,
        start_kwh=current_kwh,
        miner_kw=current_miner_kw,
        horizon_days=horizon_days,
    )
    horizon_end = datetime.fromisoformat(str(sim["horizon_end"]))
    checkpoints = sim.get("checkpoints") or []
    reachable = bool(sim.get("all_checkpoints_ok"))

    binding_cp = next((c for c in checkpoints if not c.get("reachable", True)), None)
    binding_cp = binding_cp or (checkpoints[0] if checkpoints else None)

    target_soc = binding_cp.get("target_soc") if binding_cp else None
    projected_soc = binding_cp.get("projected_soc") if binding_cp else None
    target_time = binding_cp.get("time") if binding_cp else None
    target_datetime = binding_cp.get("target_datetime") if binding_cp else None

    base_consumption_raw = expected_base_consumption_kwh(base_dir, now, horizon_end)
    cons_scale = consumption_risk_scale(config)
    try:
        from control_algorithm.consumption_pattern_estimate import (
            consumption_pattern_scale,
        )

        pattern_scale = consumption_pattern_scale(state, config)
    except Exception:
        pattern_scale = 1.0
    base_consumption = float(base_consumption_raw or 0.0) * cons_scale * pattern_scale
    hydro_kwh = expected_hydro_production_kwh(base_dir, now, horizon_end)
    pv_kwh = _pv_forecast_kwh(state, now, horizon_end)
    effective_buffer = buffer_kwh
    effective_production = pv_kwh + hydro_kwh
    net_kwh = effective_buffer + effective_production - base_consumption

    max_miner_kw, surplus_binding, near_meta = _max_miner_kw_for_horizon(
        config, state, live, base_dir, now, horizon_days
    )
    proactive_kw = _proactive_discharge_kw(config, state, live, now)
    tomorrow_pv = float(state.get("pv_yield_tomorrow_kwh") or 0)
    if proactive_kw and proactive_kw > max_miner_kw:
        if current_soc >= max_soc - 15 and tomorrow_pv >= 250:
            sim_p = simulate_battery_horizon(
                config,
                state,
                base_dir,
                now,
                start_kwh=current_kwh,
                miner_kw=proactive_kw,
                horizon_days=horizon_days,
            )
            if sim_p.get("all_checkpoints_ok"):
                max_miner_kw = proactive_kw
                if surplus_binding in ("max_soc", "none"):
                    surplus_binding = "proactive_discharge"

    # Erreichbarkeit: Plan-Budget (Zwei-Phasen oder Gleichlast) für Anzeige,
    # nicht den aktuellen Dauerbetrieb – sonst zeigt Dashboard SoC→0 % sobald
    # Hydro tagsüber läuft (obwohl nachts Budget 0 vorgesehen ist).
    if near_meta.get("near_term_hours") and surplus_binding == "near_term_discharge":
        sim_plan = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=current_kwh,
            miner_kw=float(near_meta.get("night_budget_kw")
                           if near_meta.get("night_budget_kw") is not None
                           else near_meta.get("sustained_budget_kw") or 0),
            miner_kw_near=float(near_meta.get("near_term_budget_kw") or max_miner_kw),
            near_term_hours=float(near_meta.get("near_term_hours") or 0),
            horizon_days=horizon_days,
        )
    else:
        sim_plan = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=current_kwh,
            miner_kw=max_miner_kw,
            horizon_days=horizon_days,
        )
    reachable = bool(sim_plan.get("all_checkpoints_ok"))
    checkpoints = sim_plan.get("checkpoints") or checkpoints
    binding_cp = next((c for c in checkpoints if not c.get("reachable", True)), None)
    binding_cp = binding_cp or (checkpoints[0] if checkpoints else binding_cp)
    if binding_cp:
        target_soc = binding_cp.get("target_soc")
        projected_soc = binding_cp.get("projected_soc")
        target_time = binding_cp.get("time")
        target_datetime = binding_cp.get("target_datetime")
    # min/max aus Plan-Sim für Dashboard
    plan_min = sim_plan.get("min_soc_in_horizon")
    plan_max = sim_plan.get("max_soc_in_horizon")
    hours_left = float(sim.get("hours_left") or horizon_days * 24)
    raw_surplus = round(max_miner_kw * hours_left, 2)
    miner_surplus = round(raw_surplus * risk, 2)
    raw_budget_kw = round(max_miner_kw, 3)
    miner_budget_kw = round(raw_budget_kw * risk, 3)

    return {
        "reachable": reachable,
        "projected_soc": projected_soc,
        "projected_kwh_raw": sim.get("projected_kwh_at_horizon_end"),
        "target_soc": target_soc,
        "target_time": target_time,
        "target_datetime": target_datetime,
        "horizon_days": horizon_days,
        "horizon_end": sim.get("horizon_end"),
        "hours_left": hours_left,
        "checkpoints": checkpoints,
        "min_soc_in_horizon": plan_min if plan_min is not None else sim.get("min_soc_in_horizon"),
        "max_soc_in_horizon": plan_max if plan_max is not None else sim.get("max_soc_in_horizon"),
        "max_soc_percent": max_soc,
        "binding_constraint": (
            surplus_binding
            if surplus_binding
            and surplus_binding
            not in (
                "none",
                "no_storage",
            )
            else (sim.get("binding_constraint") or surplus_binding)
        ),
        "binding_checkpoint": binding_cp,
        "over_max_at_start": bool(sim.get("over_max_at_start")),
        "miner_surplus_kwh": miner_surplus,
        "miner_surplus_kwh_raw": raw_surplus,
        "miner_budget_kw": miner_budget_kw,
        "miner_budget_kw_raw": raw_budget_kw,
        "planned_runtime_hours": round(raw_surplus / max(raw_budget_kw, 0.001), 2) if raw_budget_kw > 0 else 0.0,
        "effective_runtime_hours": round(miner_surplus / max(miner_budget_kw, 0.001), 2) if miner_budget_kw > 0 else 0.0,
        "proactive_discharge_kw": proactive_kw,
        "weather_context": weather_context(config, state, days=horizon_days),
        "pv_yield_tomorrow_kwh": float(state.get("pv_yield_tomorrow_kwh") or 0),
        "current_miner_kw": round(current_miner_kw, 3),
        "buffer_kwh": round(buffer_kwh, 2),
        "effective_buffer_kwh": round(effective_buffer, 2),
        "base_consumption_kwh": round(base_consumption, 2),
        "base_consumption_kwh_raw": round(float(base_consumption_raw or 0.0), 2),
        "pv_forecast_kwh": round(pv_kwh, 2),
        "hydro_forecast_kwh": round(hydro_kwh, 2),
        "effective_production_kwh": round(effective_production, 2),
        "net_kwh": round(net_kwh, 2),
        "risk_factor_percent": round(risk * 100.0, 1),
        "consumption_risk_percent": normalize_consumption_risk_percent(
            config.get("consumption_risk_percent")
        ),
        "consumption_risk_scale": round(cons_scale, 3),
        "profile_samples": profile_sample_count(base_dir),
        "near_term_plan": near_meta or None,
        "sustained_budget_kw": near_meta.get("sustained_budget_kw"),
        "near_term_hours": near_meta.get("near_term_hours"),
        "near_term_until": near_meta.get("near_term_until"),
        "reserve_note": near_meta.get("reserve_note"),
    }


def compute_balance_projection(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Abwärtskompatibel: delegiert an rollierende Horizont-Prognose."""
    return compute_horizon_projection(config, state, live, base_dir, now)


def required_soc_at_datetime(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    at_dt: datetime,
    *,
    horizon_days: Optional[int] = None,
    miner_kw: float = 0.0,
) -> Dict[str, Any]:
    """
    Minimaler SoC am Zeitpunkt at_dt, damit die Rest-Horizont-Simulation
    (ohne/mit Miner-Last) nie unter Min-SoC fällt. Obergrenze = Max-SoC.
    """
    horizon_days = horizon_days or planning_horizon_days(config)
    cache_key = (
        f"{at_dt.replace(second=0, microsecond=0).isoformat()}"
        f"|{int(horizon_days)}|{round(float(miner_kw), 2)}"
    )
    cache = state.setdefault("_required_soc_cache", {})
    if isinstance(cache, dict) and cache_key in cache:
        return dict(cache[cache_key])

    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    if storage <= 0:
        return {
            "required_soc_percent": min_soc,
            "min_soc_percent": min_soc,
            "max_soc_percent": max_soc,
            "ok": False,
            "note": "no_storage",
        }

    min_kwh = min_soc / 100.0 * storage
    max_kwh = max_soc / 100.0 * storage
    lo, hi = min_kwh, max_kwh
    best = max_kwh
    last_sim: Dict[str, Any] = {}

    def _ok(sim: Dict[str, Any]) -> bool:
        min_h = sim.get("min_soc_in_horizon")
        if min_h is None:
            return False
        return float(min_h) >= min_soc - 0.05

    for _ in range(14):
        mid = (lo + hi) / 2.0
        sim = simulate_battery_horizon(
            config,
            state,
            base_dir,
            at_dt,
            start_kwh=mid,
            miner_kw=float(miner_kw),
            horizon_days=horizon_days,
            use_checkpoints=False,
        )
        last_sim = sim
        if _ok(sim):
            best = mid
            hi = mid
        else:
            lo = mid

    # Verifizieren / ggf. Max nötig
    sim_best = simulate_battery_horizon(
        config,
        state,
        base_dir,
        at_dt,
        start_kwh=best,
        miner_kw=float(miner_kw),
        horizon_days=horizon_days,
        use_checkpoints=False,
    )
    if not _ok(sim_best):
        # Auch bei Max-SoC nicht haltbar (z. B. früher 0-PV am Horizont-Rand):
        # kein aspirationales 97 %-Ziel erzwingen – Nachtreserve aus Wetter/Verbrauch.
        try:
            from control_algorithm.weather_survival import _overnight_reserve_calc

            fb_pct = float(
                (_overnight_reserve_calc(config, state) or {}).get(
                    "evening_reserve_percent"
                )
                or min_soc
            )
        except Exception:
            fb_pct = min_soc
        fb_pct = max(min_soc, min(max_soc, fb_pct))
        best = fb_pct / 100.0 * storage
        sim_best = simulate_battery_horizon(
            config,
            state,
            base_dir,
            at_dt,
            start_kwh=best,
            miner_kw=0.0,
            horizon_days=horizon_days,
            use_checkpoints=False,
        )
        req_soc = fb_pct
        out = {
            "required_soc_percent": round(req_soc, 1),
            "min_soc_percent": min_soc,
            "max_soc_percent": max_soc,
            "at_datetime": at_dt.isoformat(),
            "horizon_days": horizon_days,
            "miner_kw": float(miner_kw),
            "min_soc_in_horizon": sim_best.get("min_soc_in_horizon"),
            "max_soc_in_horizon": sim_best.get("max_soc_in_horizon"),
            "ok": False,
            "unreachable_at_max": True,
            "note": (
                "Horizont-Reserve bei Max-SoC nicht haltbar – Fallback auf "
                f"Nachtreserve {req_soc:.0f} % (Wetterdaten weiter nutzen, "
                "kein erzwungenes Max-SoC-Ziel)."
            ),
        }
        cache[cache_key] = dict(out)
        return out

    req_soc = _kwh_to_soc(best, storage) or min_soc
    req_soc = max(min_soc, min(max_soc, float(req_soc)))
    out = {
        "required_soc_percent": round(req_soc, 1),
        "min_soc_percent": min_soc,
        "max_soc_percent": max_soc,
        "at_datetime": at_dt.isoformat(),
        "horizon_days": horizon_days,
        "miner_kw": float(miner_kw),
        "min_soc_in_horizon": sim_best.get("min_soc_in_horizon"),
        "max_soc_in_horizon": sim_best.get("max_soc_in_horizon"),
        "ok": _ok(sim_best),
        "note": (
            "Minimaler SoC bei Produktionsende, damit 7-Tage-Plan "
            f"≥ {min_soc:.0f} % und ≤ {max_soc:.0f} % bleibt (Miner aus in dieser Prüfung)."
        ),
    }
    cache[cache_key] = dict(out)
    return out


def today_end_of_production_recommendation(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Eine Kennzahl für heute: empfohlener Speicherstand, wenn die PV/Produktion
    heute endet – abgeleitet aus der 7-Tage-Horizontrechnung (Min-/Max-SoC).
    """
    from control_algorithm.weather_survival import last_pv_hour_for_date

    now = now or datetime.now()
    live = live or {}
    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    horizon_days = planning_horizon_days(config)

    last_h = last_pv_hour_for_date(state, now.date())
    last_h = max(12, min(22, int(last_h or 18)))
    end_dt = now.replace(hour=last_h, minute=0, second=0, microsecond=0)
    if end_dt <= now:
        # Sonne schon weg: Empfehlung gilt „jetzt / als Nächstes“
        end_dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    required = required_soc_at_datetime(
        config, state, base_dir, end_dt, horizon_days=horizon_days, miner_kw=0.0
    )
    recommended = float(required.get("required_soc_percent") or min_soc)

    # Prognose: wo landet der Speicher bis PV-Ende bei aktuellen Bedingungen (ohne Miner)?
    projected_at_end = None
    soc_now = live.get("soc_percent")
    if soc_now is None:
        soc_now = state.get("live_soc")
    if soc_now is not None and storage > 0:
        start_kwh = float(soc_now) / 100.0 * storage
        if end_dt > now:
            sim_to_end = simulate_battery_horizon(
                config,
                state,
                base_dir,
                now,
                start_kwh=start_kwh,
                miner_kw=0.0,
                horizon_days=max(1, int((end_dt - now).total_seconds() / 86400.0) + 1),
                use_checkpoints=False,
            )
            # Checkpoint / Ende: SoC am Ende des kurzen Fensters grob aus Horizontende
            # Fein: stündlich bis end_dt neu simulieren
            kwh = start_kwh
            cursor = now
            step = timedelta(hours=1)
            while cursor < end_dt:
                nxt = min(cursor + step, end_dt)
                frac = (nxt - cursor).total_seconds() / 3600.0
                base = planned_base_consumption_kwh(
                    config, base_dir, cursor, nxt, state=state
                )
                hydro = expected_hydro_production_kwh(base_dir, cursor, nxt)
                pv = _pv_forecast_kwh(state, cursor, nxt)
                kwh = _cap_battery_kwh(kwh + pv + hydro - base, storage, max_soc)
                cursor = nxt
            projected_at_end = _kwh_to_soc(kwh, storage)
        else:
            projected_at_end = float(soc_now)

    gap = None
    if projected_at_end is not None:
        gap = round(float(projected_at_end) - recommended, 1)

    status = "ok"
    if gap is not None and gap < -2:
        status = "below"
    elif gap is not None and float(projected_at_end or 0) > max_soc - 0.5:
        status = "near_max"

    return {
        "recommended_soc_percent": round(recommended, 1),
        "at_time": end_dt.strftime("%H:%M"),
        "at_datetime": end_dt.isoformat(),
        "last_pv_hour": last_h,
        "projected_soc_percent": (
            round(float(projected_at_end), 1) if projected_at_end is not None else None
        ),
        "current_soc_percent": float(soc_now) if soc_now is not None else None,
        "gap_soc_percent": gap,
        "status": status,
        "min_soc_percent": min_soc,
        "max_soc_percent": max_soc,
        "horizon_days": horizon_days,
        "min_soc_in_horizon_if_met": required.get("min_soc_in_horizon"),
        "ok": bool(required.get("ok")),
        "summary": (
            f"Heute empfohlen bei Produktionsende (~{end_dt.strftime('%H:%M')}): "
            f"{recommended:.0f} % – damit der 7-Tage-Plan ≥ {min_soc:.0f} % bleibt "
            f"und ≤ {max_soc:.0f} % bleibt."
        ),
        "calculation_note": (
            "Binäre Suche: kleinster SoC am PV-Ende, bei dem die stündliche "
            f"{horizon_days}-Tage-Simulation (PV-/Verbrauchsprognose, Miner aus) "
            f"nie unter {min_soc:.0f} % fällt; Deckel {max_soc:.0f} %."
        ),
        "required_detail": required,
    }


def _pv_pace_scale(state: Dict[str, Any], live: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Beobachteter PV-Pace als Skalierung der Rest-Prognose.

    Der Pace formt hier das Tagesziel – die Abschaltung im Watchdog ist davon
    unabhängig und bleibt Bremse. Läuft unabhängig von `pv_safety_enabled`.
    """
    out = {"scale": 1.0, "day_deviation_pct": None, "forecast_ref": None}
    try:
        from control_algorithm.pv_safety_watchdog import compute_pv_pace_signals

        signals = compute_pv_pace_signals(state, live, now=now)
    except Exception:
        return out
    out["day_deviation_pct"] = signals.get("day_deviation_pct")
    out["forecast_ref"] = signals.get("forecast_ref")
    dev = signals.get("day_deviation_pct")
    # Erst ab spürbarer Prognosemenge, sonst rauscht der Morgen
    if dev is None or float(signals.get("forecast_so_far_kwh") or 0) < 1.0:
        return out
    out["scale"] = max(0.35, min(1.15, 1.0 + float(dev) / 100.0))
    return out


def compute_today_surplus(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Überschuss-Menge für den Rest des heutigen Tages (kWh).

    Gegenstück zum rollierenden 7-Tage-Budget: `miner_surplus_kwh` verteilt das
    Wochenbudget als Dauerleistung und liegt damit oft unter der kleinsten
    Gerätestufe. Hier zählt, was heute tatsächlich übrig bleibt:

        heute = Akku_jetzt + Rest-PV + Rest-Hydro − Rest-Grundlast − Abendreserve

    Der 7-Tage-Horizont bleibt als Deckel darüber, damit der Morgen-Checkpoint
    geschützt bleibt.
    """
    now = now or datetime.now()
    storage = float(config.get("storage_kwh") or 0)
    soc_now = live.get("soc_percent")
    if soc_now is None:
        soc_now = state.get("live_soc")

    out: Dict[str, Any] = {
        "ok": False,
        "kwh": 0.0,
        "computed_at": now.isoformat(timespec="seconds"),
        "capped_by": None,
    }

    if not config.get("has_storage") or storage <= 0 or soc_now is None:
        out["reason"] = "kein Speicher / kein SoC"
        return out

    end_today = now.replace(hour=23, minute=59, second=0, microsecond=0)
    hours_left = max(0.0, (end_today - now).total_seconds() / 3600.0)
    if hours_left < 0.25:
        out["reason"] = "Tagesende erreicht"
        return out

    pace = _pv_pace_scale(state, live, now)
    rest_pv_raw = float(_pv_forecast_kwh(state, now, end_today) or 0.0)
    rest_pv = rest_pv_raw * float(pace["scale"])
    rest_hydro = float(expected_hydro_production_kwh(base_dir, now, end_today) or 0.0)
    rest_base = float(
        planned_base_consumption_kwh(config, base_dir, now, end_today, state=state) or 0.0
    )

    # Abendreserve aus der Steuerungsziel-Kurve (steigt wenn morgen schwach wird)
    goal = dict((state.get("curve_plan") or {}).get("steering_goal") or {})
    reserve_pct = goal.get("floor_eve_pct")
    if reserve_pct is None:
        reserve_pct = max(
            float(config.get("min_soc_percent") or 35.0),
            float(config.get("morning_soc_percent") or 33.0),
        )
    reserve_pct = float(reserve_pct)

    start_kwh = float(soc_now) / 100.0 * storage
    reserve_kwh = reserve_pct / 100.0 * storage
    today_kwh = start_kwh + rest_pv + rest_hydro - rest_base - reserve_kwh
    today_kwh = max(0.0, today_kwh)

    # Wochendeckel: nie mehr als der rollierende Horizont erlaubt
    projection = state.get("horizon_projection") or state.get("balance_projection") or {}
    week_kwh = projection.get("miner_surplus_kwh")
    capped_by = None
    if week_kwh is not None:
        try:
            week_kwh = float(week_kwh)
            if today_kwh > week_kwh:
                today_kwh = max(0.0, week_kwh)
                capped_by = "horizon_7d"
        except (TypeError, ValueError):
            week_kwh = None

    out.update(
        {
            "ok": True,
            "kwh": round(today_kwh, 2),
            "hours_left_today": round(hours_left, 2),
            "rest_pv_kwh": round(rest_pv, 2),
            "rest_pv_raw_kwh": round(rest_pv_raw, 2),
            "rest_hydro_kwh": round(rest_hydro, 2),
            "rest_base_kwh": round(rest_base, 2),
            "start_kwh": round(start_kwh, 2),
            "reserve_soc_pct": round(reserve_pct, 1),
            "reserve_kwh": round(reserve_kwh, 2),
            "week_cap_kwh": round(float(week_kwh), 2) if week_kwh is not None else None,
            "capped_by": capped_by,
            "pace_scale": round(float(pace["scale"]), 3),
            "pace_deviation_pct": pace["day_deviation_pct"],
            "forecast_ref": pace["forecast_ref"],
            "continuous_kw": round(today_kwh / hours_left, 2) if hours_left > 0.25 else 0.0,
        }
    )
    return out


def night_lock_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """Schalter im Dashboard. Fehlt der Wert, bleibt die Nachtsperre an."""
    raw = (config or {}).get("night_lock_enabled", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "off", "aus", "nein")
    return bool(raw)


def apply_night_lock_switch(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Bei ausgeschaltetem Schalter keine Zwangssperre an den Planer durchreichen."""
    if night_lock_enabled(config):
        return state
    state["night_lock_active"] = False
    state["night_lock_force"] = False
    state["night_lock_reason"] = None
    return state


def maybe_run_hourly_balance(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Energiebilanz-Prognose (jeder Tick) und Nacht-Sperre.
    Nacht-Sperre nur wenn selbst ohne Miner die SOC-Checkpoints nicht erreichbar sind.
    Überschreitung der aktuellen Last gegenüber dem Budget → Laststeuerung (kein Lock).
    """
    now = now or datetime.now()
    key = f"hourly_balance_{now.date().isoformat()}_{now.hour}"
    projection_stale = state.get("last_hourly_balance_key") != key

    projection = compute_horizon_projection(config, state, live, base_dir, now)
    state["horizon_projection"] = projection
    state["balance_projection"] = projection
    state["projected_soc_at_target"] = projection.get("projected_soc")
    # Tageszahl für die Überschuss-Session; Wochenbudget wirkt darin als Deckel.
    # Defensiv: ein Fehler hier darf den Tick nicht abbrechen.
    try:
        state["today_surplus"] = compute_today_surplus(config, state, live, base_dir, now)
    except Exception as exc:  # pragma: no cover
        state["today_surplus"] = {"ok": False, "kwh": 0.0, "reason": str(exc)}
    if projection_stale:
        state["last_hourly_balance_key"] = key

    if not config.get("has_storage") or float(config.get("storage_kwh") or 0) <= 0:
        state["night_lock_active"] = False
        state["night_lock_reason"] = None
        state["night_lock_force"] = False
        return apply_night_lock_switch(config, state)

    # Keine manuellen/autonomen Checkpoints: trotzdem Min-SoC über Simulation schützen
    if projection.get("target_soc") is None:
        soc = live.get("soc_percent")
        storage = float(config.get("storage_kwh") or 0)
        min_soc = float(config.get("min_soc_percent") or 35.0)
        horizon_days = int(projection.get("horizon_days") or planning_horizon_days(config))
        min_soc_threatened = False
        min_in_horizon = None
        if soc is not None and storage > 0:
            sim0 = simulate_battery_horizon(
                config,
                state,
                base_dir,
                now,
                start_kwh=float(soc) / 100.0 * storage,
                miner_kw=0.0,
                horizon_days=horizon_days,
            )
            min_in_horizon = sim0.get("min_soc_in_horizon")
            min_soc_threatened = (
                min_in_horizon is not None and float(min_in_horizon) < min_soc + 0.5
            )
        if min_soc_threatened:
            state["night_lock_active"] = True
            state["night_lock_force"] = True
            state["night_lock_reason"] = (
                f"Autonom (ohne SoC-Uhrzeiten): auch ohne Miner Min-SoC-Gefahr "
                f"(Tief ~{float(min_in_horizon):.2f}%) unter {min_soc:.0f} %"
            )
            state["load_over_budget"] = False
            state["load_over_budget_reason"] = None
        else:
            state["night_lock_active"] = False
            state["night_lock_reason"] = None
            state["night_lock_force"] = False
        return apply_night_lock_switch(config, state)

    budget_kw = float(
        projection.get("miner_budget_kw_raw") or projection.get("miner_budget_kw") or 0
    )
    current_kw = float(projection.get("current_miner_kw") or 0)
    horizon_days = int(projection.get("horizon_days") or planning_horizon_days(config))

    if budget_kw >= 0.05:
        state["night_lock_active"] = False
        state["night_lock_reason"] = None
        state["night_lock_force"] = False
        binding = str(projection.get("binding_constraint") or "")
        # Kurzfrist-Boost: 7-Tage-Checkpoint kann mit voller Last eng sein –
        # kein Lastabbau solange Ist-Last im (angehobenen) Budget liegt.
        near_term_ok = (
            binding == "near_term_discharge" and current_kw <= budget_kw + 0.5
        )
        if not projection.get("reachable") and not near_term_ok:
            state["load_over_budget"] = True
            binding_cp = projection.get("binding_checkpoint") or {}
            state["load_over_budget_reason"] = (
                f"Aktuelle Miner-Last {current_kw:.1f} kW über Budget {budget_kw:.1f} kW – "
                f"Prognose mit Ist-Last: SOC {binding_cp.get('projected_soc', '?')}% "
                f"< Ziel {binding_cp.get('target_soc', projection.get('target_soc'))}% "
                f"am {binding_cp.get('time', projection.get('target_time'))} "
                f"({str(binding_cp.get('target_datetime') or projection.get('target_datetime') or '')[:10]}). "
                f"Laststeuerung reduziert schrittweise nach Priorität."
            )
        else:
            state["load_over_budget"] = False
            state["load_over_budget_reason"] = None
            if projection.get("near_term_plan"):
                nt = projection["near_term_plan"]
                sustained = nt.get("sustained_budget_kw")
                until = str(nt.get("near_term_until") or "")[:16]
                reserve = nt.get("reserve_note") or ""
                state["near_term_note"] = (
                    f"Zwei-Phasen-Plan: bis {until} Budget {budget_kw:.1f} kW, "
                    f"danach nachhaltig ~{sustained} kW (40 %-Reserve hält)."
                    + (f" {reserve}" if reserve else "")
                )
            else:
                state.pop("near_term_note", None)
        return apply_night_lock_switch(config, state)

    state["load_over_budget"] = False
    state["load_over_budget_reason"] = None
    soc = live.get("soc_percent")
    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    sim0: Dict[str, Any] = {}
    if soc is not None and storage > 0:
        sim0 = simulate_battery_horizon(
            config,
            state,
            base_dir,
            now,
            start_kwh=float(soc) / 100.0 * storage,
            miner_kw=0.0,
            horizon_days=horizon_days,
        )

    checkpoints_ok_at_zero = bool(sim0.get("all_checkpoints_ok"))
    min_in_horizon = sim0.get("min_soc_in_horizon")
    min_soc_threatened = (
        min_in_horizon is not None and float(min_in_horizon) < min_soc + 0.5
    )

    binding = projection.get("binding_checkpoint") or {}
    try:
        fail_target = float(
            binding.get("target_soc")
            if binding.get("target_soc") is not None
            else (projection.get("target_soc") or 0)
        )
    except (TypeError, ValueError):
        fail_target = 0.0
    # Hohes Tagesziel (z. B. 94 % um 17:00) unerreichbar ≠ Not-Sperre:
    # Miner-Budget ist bereits 0; force-Nacht-Sperre würde nur schalten/flackern.
    aspirational_high = fail_target >= max(70.0, min_soc + 35.0)

    if checkpoints_ok_at_zero and not min_soc_threatened:
        state["night_lock_active"] = False
        state["night_lock_reason"] = None
        state["night_lock_force"] = False
        return apply_night_lock_switch(config, state)

    if not min_soc_threatened and aspirational_high:
        state["night_lock_active"] = False
        state["night_lock_force"] = False
        state["night_lock_reason"] = None
        state["load_over_budget"] = True
        state["load_over_budget_reason"] = (
            f"Tagesziel {fail_target:.0f}% am {binding.get('time') or projection.get('target_time')} "
            f"auch ohne Miner unerreichbar (Prognose ~"
            f"{binding.get('projected_soc', projection.get('projected_soc'))}%) – "
            f"Budget 0 kW zum Laden, aber keine force-Nacht-Sperre (Min-SoC ok)."
        )
        return apply_night_lock_switch(config, state)

    fail_time = binding.get("time") or projection.get("target_time")
    fail_date = str(binding.get("target_datetime") or projection.get("target_datetime") or "")[:10]
    state["night_lock_active"] = True
    # force nur bei echter Min-SoC-Gefahr im Horizont
    state["night_lock_force"] = bool(min_soc_threatened)
    state["night_lock_reason"] = (
        f"7-Tage-Prognose: auch ohne Miner "
        + (
            f"Min-SoC-Gefahr (Tief ~{min_in_horizon}%) "
            if min_soc_threatened
            else f"SOC {binding.get('projected_soc', projection.get('projected_soc', '?'))}% "
            f"< Ziel {binding.get('target_soc', projection.get('target_soc'))}% "
        )
        + f"am {fail_time} ({fail_date}) "
        f"(PV {projection.get('pv_forecast_kwh')} + Hydro {projection.get('hydro_forecast_kwh')} kWh, "
        f"Grundverbrauch {projection.get('base_consumption_kwh')} kWh)"
    )
    return apply_night_lock_switch(config, state)


def miner_surplus_kwh(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> float:
    """Verfügbare Miner-Energie über rollierenden 7-Tage-Horizont (kWh, inkl. Risikofaktor)."""
    now = now or datetime.now()
    storage = float(config.get("storage_kwh") or 0)
    if storage <= 0:
        return 0.0
    soc = live.get("soc_percent")
    if soc is None:
        return 0.0

    projection = (
        state.get("horizon_projection")
        or state.get("balance_projection")
        or compute_horizon_projection(config, state, live, base_dir, now)
    )
    if projection.get("target_soc") is None:
        return 0.0

    cached = projection.get("miner_surplus_kwh")
    if cached is not None and (
        state.get("horizon_projection") is projection or state.get("balance_projection") is projection
    ):
        return round(float(cached), 2)

    max_kw, _binding, _meta = _max_miner_kw_for_horizon(config, state, live, base_dir, now)
    hours_left = float(projection.get("hours_left") or DEFAULT_HORIZON_DAYS * 24)
    surplus = max(0.0, max_kw * hours_left)
    return round(surplus, 2)


def _battery_kwh_at(
    config: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    start_kwh: float,
    start_dt: datetime,
    end_dt: datetime,
) -> float:
    """Prognostiziert Akku-kWh am Ende von end_dt (ohne Miner-Last)."""
    if end_dt <= start_dt:
        return start_kwh
    storage = float(config.get("storage_kwh") or 0)
    caps = _planning_band_caps(config, state)
    max_soc = float(caps["plan_max"])
    base = planned_base_consumption_kwh(
        config, base_dir, start_dt, end_dt, state=state
    )
    hydro = expected_hydro_production_kwh(base_dir, start_dt, end_dt)
    pv = _pv_forecast_kwh(state, start_dt, end_dt) * float(caps.get("pv_scale") or 1.0)
    result = start_kwh + pv + hydro - base
    if storage > 0:
        return _cap_battery_kwh(result, storage, max_soc)
    return max(0.0, result)


def _max_soc_kwh(config: Dict[str, Any]) -> float:
    storage = float(config.get("storage_kwh") or 0)
    if storage <= 0:
        return 0.0
    max_soc = float(config.get("max_soc_percent") or 95.0)
    return max_soc / 100.0 * storage


def _planning_band_caps(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Einmalige Band-Caps für Wochen-/Horizon-Planung.

    plan_max/plan_min kommen aus dem Speicher-Band-Neuron; hartes Config-Max
    bleibt nur Notfall. pv_scale dämpft PV bei Overfill-Stress.
    """
    hard_max = float(config.get("max_soc_percent") or 95.0)
    hard_min = float(config.get("min_soc_percent") or 35.0)
    morning = config.get("morning_soc_percent")
    try:
        morning_f = float(morning) if morning is not None else hard_min
    except (TypeError, ValueError):
        morning_f = hard_min
    plan_max = hard_max
    plan_min = morning_f
    pv_scale = 1.0
    overfill = 0.0
    underfill = 0.0
    note = None
    try:
        from control_algorithm.storage_band_learning import (
            band_pv_plan_scale,
            effective_planning_max_soc,
            effective_planning_min_soc,
            storage_band_limits,
        )

        plan_max = float(effective_planning_max_soc(config, state, live=live))
        plan_min = float(effective_planning_min_soc(config, state, live=live))
        pv_scale = float(band_pv_plan_scale(config, state, live=live))
        limits = storage_band_limits(config, state, live=live)
        overfill = float(limits.get("overfill_risk") or 0)
        underfill = float(limits.get("underfill_risk") or 0)
        note = limits.get("note")
    except Exception:
        pass
    plan_max = max(hard_min + 5.0, min(hard_max, plan_max))
    plan_min = max(hard_min, min(plan_max - 5.0, plan_min))
    pv_scale = max(0.75, min(1.0, pv_scale))
    return {
        "plan_max": round(plan_max, 2),
        "plan_min": round(plan_min, 2),
        "hard_max": hard_max,
        "hard_min": hard_min,
        "pv_scale": round(pv_scale, 4),
        "overfill_risk": overfill,
        "underfill_risk": underfill,
        "note": note,
    }


def _morning_soc_target(config: Dict[str, Any]) -> float:
    """Morgen-SoC-Ziel: manuelle soc_targets, sonst morning_soc_percent (~33 %)."""
    targets = config.get("soc_targets") or []
    morning = []
    for t in targets:
        try:
            hhmm = str(t.get("time") or "")
            hour = int(hhmm.split(":")[0])
            if 5 <= hour <= 9:
                morning.append(float(t.get("soc_percent")))
        except Exception:
            continue
    if morning:
        return min(morning)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    morning_cfg = config.get("morning_soc_percent")
    if morning_cfg is not None:
        try:
            return max(min_soc, float(morning_cfg))
        except (TypeError, ValueError):
            pass
    return min_soc


def _proactive_headroom_soc(
    max_soc: float,
    tomorrow_pv: float,
    *,
    storage_kwh: float = 0.0,
    morning_target: float = 40.0,
    perfect_day_kwh: float = 0.0,
) -> float:
    """
    Ziel-SOC vor morgendlicher PV – je mehr PV morgen im Verhältnis zum
    freien Speicherplatz, desto näher an den Morgen-Checkpoint (~40 %),
    damit tagsüber Platz bleibt und Einspeisung vermieden wird.
    """
    morning_target = max(float(morning_target), 20.0)
    # Fill-Ratio gegen Speicher: gilt auch für ~40 kWh Hausspeicher (früher >50)
    if storage_kwh > 10 and tomorrow_pv > 0:
        room_kwh = max(0.0, (max_soc - morning_target) / 100.0 * storage_kwh)
        if room_kwh <= 1:
            return morning_target
        fill_ratio = tomorrow_pv / room_kwh
        # PV füllt den Speicher vom Morgen-Ziel aus mehrfach → klar auf Checkpoint
        if fill_ratio >= 1.1:
            return morning_target
        if fill_ratio >= 0.75:
            # zwischen max-10 und Morgen-Ziel
            return round(morning_target + (max_soc - 10 - morning_target) * (1.1 - fill_ratio) / 0.35, 1)
        if fill_ratio >= 0.45:
            return max(morning_target, max_soc - 18)

    # Absolute Stufen relativ zur Anlagen-Referenz (nicht hart 250–900 kWh)
    ref = max(float(perfect_day_kwh or 0), 40.0)
    if tomorrow_pv >= ref * 2.2:
        return morning_target
    if tomorrow_pv >= ref * 1.7:
        return max(morning_target, max_soc - 25)
    if tomorrow_pv >= ref * 1.2:
        return max(morning_target, max_soc - 15)
    if tomorrow_pv >= ref * 0.85:
        return max(morning_target, max_soc - 10)
    if tomorrow_pv >= ref * 0.55:
        return max_soc - 5
    return max_soc - 3


def _export_avoidance_kw(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
) -> Optional[float]:
    """
    Miner-Sollleistung gegen Netzeinspeisung / Max-SoC-Schutz.

    Bei Live-Export: Menge egal – Orientierung an eingestelltem Max-SoC
    (je näher an der Oberkante, desto mehr Flotte), nicht am Wochen-Dauerbudget.
    Checkpoints/Min-SoC werden in _max_miner_kw_for_horizon weiter abgesichert.
    """
    from control_algorithm.weather_survival import (
        export_avoidance_recommended,
        live_grid_export_kw,
    )

    if not export_avoidance_recommended(config, state, live):
        return None
    devices = enabled_devices(config)
    total_kw = sum(float(d.get("power_kw") or 0) for d in devices)
    if total_kw <= 0:
        return None

    from control_algorithm.steering_learning import export_threshold_kw

    export_kw = live_grid_export_kw(live)
    thr = export_threshold_kw(state)
    pv_kw = float(live.get("pv_kw") or 0)
    verbrauch_kw = float(live.get("verbrauch_kw") or 0)
    surplus_now = max(0.0, pv_kw - verbrauch_kw)
    soc = live.get("soc_percent")
    max_soc = float(config.get("max_soc_percent") or 95.0)
    soc_f = float(soc) if soc is not None else 0.0
    headroom_pct = max(0.0, max_soc - soc_f)
    high_soc = soc is not None and soc_f >= max_soc - 5.0
    at_max = soc is not None and soc_f >= max_soc - 1.0

    try:
        from control_algorithm.planner import miner_load_kw, sort_for_resume

        ordered = sort_for_resume(devices)
        running_kw = float(
            miner_load_kw(devices, dict(live.get("miner_status") or {}))
        )
    except Exception:
        ordered = devices
        running_kw = 0.0
    first_pwr = float(ordered[0].get("power_kw") or 0) if ordered else 0.0

    def _stack_until(need_kw: float) -> float:
        stacked = 0.0
        for d in ordered:
            stacked += float(d.get("power_kw") or 0)
            if stacked + 0.05 >= need_kw:
                break
        return stacked

    # --- Live-Einspeisung: Höhe egal, Härte nach Abstand zu Max-SoC ---
    if export_kw >= thr:
        # Mindestens laufende Last + nächste Stufe (Export wegdrücken)
        min_stack = _stack_until(running_kw + max(first_pwr, 1.0))
        if headroom_pct <= 2.0 or at_max:
            # An/über Oberkante → volle Flotte
            absorb = total_kw
        elif headroom_pct <= 10.0:
            # Nahe Max-SoC (z. B. 75–85 %) → aggressiv, Wochenplan nachrangig
            absorb = max(min_stack, total_kw * 0.85, _stack_until(total_kw * 0.85))
        elif headroom_pct <= 20.0:
            absorb = max(min_stack, total_kw * 0.65)
        else:
            absorb = max(min_stack, first_pwr * 2 if first_pwr > 0 else min_stack)
        return round(min(total_kw, absorb), 3)

    # --- Preempt nahe Max ohne messbaren Export ---
    target_absorb = max(surplus_now * 0.95, first_pwr if high_soc else 0.0)
    if high_soc:
        target_absorb = max(
            target_absorb,
            surplus_now + 0.8,
            pv_kw * 0.9 if at_max else pv_kw * 0.75,
            _stack_until(max(surplus_now, first_pwr + 0.1, running_kw + first_pwr)),
            total_kw * (0.85 if at_max else 0.6),
        )
    absorb = max(target_absorb, pv_kw * 0.85, total_kw * 0.5, first_pwr)
    return round(min(total_kw, absorb), 3)


def _effective_tomorrow_pv_for_discharge(
    state: Dict[str, Any],
    *,
    perfect_day_kwh: float,
) -> float:
    """PV-morgen für Vorentladen – Rohstrahlung nicht durch PR-Delle killen.

    Nach schwachen Ist-Tagen kann PR stark fallen (z. B. 0.73) und die
    Tagesprognose knapp unter die Vorentlade-Schwelle drücken, obwohl Open-Meteo
    Roh ~Vollsonne zeigt (~1000 kWh). Dann bleibt der Speicher zu voll und Max-SoC
    wird tagsüber gerissen. Bei starker Roh-Prognose mind. 85 % vom Roh ansetzen.
    """
    adj = 0.0
    try:
        adj = float(state.get("pv_yield_tomorrow_kwh") or 0)
    except (TypeError, ValueError):
        adj = 0.0
    det = state.get("pv_forecast_detail") or {}
    raw = 0.0
    try:
        raw = float(det.get("tomorrow_raw_kwh") or 0)
    except (TypeError, ValueError):
        raw = 0.0
    perfect = max(40.0, float(perfect_day_kwh or 0))
    # Starke Rohstrahlung (≥70 % Vollsonne-Referenz): PR darf nicht unter 85 % Roh
    if raw >= perfect * 0.70:
        floor = raw * 0.85
        if floor > adj:
            return round(floor, 2)
    return round(adj, 2)


def _proactive_discharge_kw(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
) -> Optional[float]:
    """
    Zusätzliche Miner-Leistung wenn Speicher voll / PV-reich und morgen/heute
    viel Sonne kommt – Ziel: morgens nahe Checkpoint (~40 %), Export vermeiden.
    """
    if should_block_proactive_discharge(config, state, live):
        return None
    soc = live.get("soc_percent")
    storage = float(config.get("storage_kwh") or 0)
    if soc is None or storage <= 0:
        return None

    max_soc = float(config.get("max_soc_percent") or 95.0)
    current_soc = float(soc)

    # Laufreserve hat Vorrang: trägt der Speicher die Geräte nicht bis zum
    # nächsten PV-Break-even, wird der Überschuss nicht mehr über die Nacht
    # gestreckt – er wird am Morgen gezogen. Hartes Max-SoC bleibt Notbremse.
    drain_plan = state.get("morning_drain_plan") or {}
    if (
        drain_plan.get("gate_active")
        and not drain_plan.get("allowed_ips")
        and current_soc <= max_soc - 0.3
    ):
        return None
    perfect = _plant_perfect_day_kwh(config, state)
    tomorrow_pv = _effective_tomorrow_pv_for_discharge(state, perfect_day_kwh=perfect)
    try:
        from control_algorithm.pv_pattern_estimate import effective_today_pv_kwh

        today_pv = float(effective_today_pv_kwh(state))
    except Exception:
        today_pv = float(state.get("pv_yield_today_kwh") or 0)
    pv_live = float(live.get("pv_kw") or 0)
    morning_target = _morning_soc_target(config)

    # Schwellen relativ zur Anlagen-Referenz (Wetterdaten), nicht absolut 200/400 kWh
    if (
        tomorrow_pv < perfect * 0.55
        and today_pv < perfect * 0.55
        and current_soc < max_soc - 5
    ):
        return None
    if current_soc < morning_target + 3 and tomorrow_pv < perfect * 0.9:
        return None

    headroom_soc = _proactive_headroom_soc(
        max_soc,
        max(tomorrow_pv, today_pv * 0.5),
        storage_kwh=storage,
        morning_target=morning_target,
        perfect_day_kwh=perfect,
    )
    try:
        from control_algorithm.storage_band_learning import effective_planning_max_soc

        band_max = effective_planning_max_soc(config, state)
        headroom_soc = min(headroom_soc, band_max)
    except Exception:
        pass
    try:
        from control_algorithm.steering_learning import headroom_extra_soc

        # Gelernt: bei Export-Misses / PV-Unterschätzung früher Vorentladen
        headroom_soc = max(morning_target, headroom_soc - headroom_extra_soc(state))
    except Exception:
        pass
    if (
        current_soc < headroom_soc - 2
        and tomorrow_pv < perfect * 0.75
        and today_pv < perfect * 0.9
    ):
        return None

    current_kwh = current_soc / 100.0 * storage
    target_kwh = headroom_soc / 100.0 * storage
    excess_kwh = max(0.0, current_kwh - target_kwh)

    max_kwh = max_soc / 100.0 * storage
    spare_kwh = max(0.0, max_kwh - current_kwh)

    # Reserve für morgige PV: wenn PV den freien Platz klar übersteigt → stärker entladen
    if tomorrow_pv >= perfect * 0.6:
        pv_overflow = max(0.0, tomorrow_pv - spare_kwh)
        if pv_overflow > max(5.0, storage * 0.15):
            # so viel vorbereiten, dass morgens nahe morning_target Platz ist
            need_for_pv = max(0.0, tomorrow_pv * 0.55 - spare_kwh)
            excess_kwh = max(excess_kwh, need_for_pv)

    # Rest-PV heute gegen freien Speicherplatz
    end_today = now.replace(hour=23, minute=59, second=0, microsecond=0)
    hours_left_today = (
        max(0.0, (end_today - now).total_seconds() / 3600.0) if end_today > now else 0.0
    )
    if hours_left_today > 0.5:
        pv_rest = float(_pv_forecast_kwh(state, now, end_today) or 0)
        overflow_today = max(0.0, pv_rest - spare_kwh)
        if overflow_today > 5.0:
            excess_kwh = max(excess_kwh, overflow_today * 0.55)

    if excess_kwh < 1.0 and current_soc < max_soc - 3:
        return None

    next_morning = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if next_morning <= now:
        next_morning += timedelta(days=1)
    # SOC-Ziel-Zeit aus Config bevorzugen
    for t in config.get("soc_targets") or []:
        try:
            hh, mm = str(t.get("time") or "07:00").split(":")[:2]
            cand = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if cand <= now:
                cand += timedelta(days=1)
            if abs(float(t.get("soc_percent")) - morning_target) < 0.5:
                next_morning = cand
                break
        except Exception:
            pass

    hours_until_sun = max(2.0, (next_morning - now).total_seconds() / 3600.0)
    if hours_left_today > 0.5 and pv_live > 5:
        hours_until_sun = min(hours_until_sun, max(2.0, hours_left_today))

    # Grundverbrauch bis morgen mindert nötige Miner-Last etwas
    try:
        from control_algorithm.config import BACKEND_DIR

        base_until = float(
            planned_base_consumption_kwh(
                config, str(BACKEND_DIR), now, next_morning, state=state
            )
            or 0
        )
        excess_kwh = max(0.0, excess_kwh - base_until * 0.5)
    except Exception:
        pass

    pv_ref = max(tomorrow_pv, today_pv * 0.4)
    pv_factor = min(1.6, 0.55 + pv_ref / max(perfect, 40.0)) if pv_ref > 0 else 0.5
    discharge_kw = (excess_kwh / hours_until_sun) * pv_factor

    if tomorrow_pv >= perfect * 1.5 or today_pv >= perfect * 1.5:
        discharge_kw *= 1.2
    if current_soc > max_soc:
        discharge_kw = max(discharge_kw, excess_kwh / max(3.0, hours_until_sun * 0.5))

    total_kw = sum(float(d.get("power_kw") or 0) for d in enabled_devices(config))
    if total_kw <= 0:
        return None
    return round(min(total_kw, max(0.0, discharge_kw)), 3)


def _cap_battery_kwh(
    kwh: float,
    storage: float,
    max_soc_percent: Optional[float] = None,
) -> float:
    cap = storage
    if storage > 0 and max_soc_percent is not None:
        cap = min(cap, max_soc_percent / 100.0 * storage)
    if cap > 0:
        return max(0.0, min(cap, kwh))
    return max(0.0, kwh)


def _kwh_to_soc(kwh: float, storage: float) -> Optional[float]:
    if storage <= 0:
        return None
    return round(max(0.0, min(100.0, kwh / storage * 100.0)), 2)


def _miner_all_day_kwh(devices: List[Dict[str, Any]]) -> float:
    """Zusatzverbrauch wenn alle aktivierten Miner 24 h laufen (Obergrenze)."""
    return round(sum(float(d.get("power_kw") or 0) for d in devices) * 24.0, 2)


def _planned_miner_kwh_from_week_day(day: Optional[Dict[str, Any]]) -> Optional[float]:
    if not day:
        return None
    totals = day.get("daily_totals") or {}
    raw = totals.get("miner_kwh")
    if raw is None:
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _build_miner_budget_breakdown(
    *,
    storage_now_kwh: Optional[float],
    pv_until_kwh: float,
    hydro_until_kwh: float,
    base_consumption_kwh: float,
    risk_pct: float,
    projected_kwh: Optional[float],
    projected_soc: Optional[float],
    target_kwh: Optional[float],
    target_soc: Optional[float],
    target_time: Optional[str],
    hours_left: float,
    available_kwh: float,
    is_forecast: bool,
    max_soc_percent: Optional[float] = None,
    horizon_end: Optional[str] = None,
    binding_constraint: Optional[str] = None,
    checkpoints: Optional[List[Dict[str, Any]]] = None,
    storage_kwh: Optional[float] = None,
) -> Dict[str, Any]:
    """Strukturierte Miner-Budget-Formel für die UI."""
    storage_now = float(storage_now_kwh or 0)
    pv = float(pv_until_kwh or 0)
    hydro = float(hydro_until_kwh or 0)
    base = float(base_consumption_kwh or 0)
    risk = float(risk_pct or 100.0)
    production = round((pv + hydro) * risk / 100.0, 2) if risk != 100.0 else round(pv + hydro, 2)
    projected = float(projected_kwh or 0)
    target_store = float(target_kwh or 0)
    mode = "Prognose" if is_forecast else "Live"
    tt = target_time or "Ziel"
    risk_note = f" (× Risiko {risk:.0f}%)" if risk != 100.0 else ""
    horizon_label = "7-Tage rollierend"
    if horizon_end:
        try:
            he = datetime.fromisoformat(str(horizon_end))
            horizon_label = f"7-Tage rollierend bis {he.strftime('%d.%m.%Y %H:%M')}"
        except Exception:
            pass

    steps: List[Dict[str, Any]] = [
        {
            "id": "storage_now",
            "label": "Akku jetzt",
            "value_kwh": round(storage_now, 1),
            "op": "+",
            "row_type": "item",
            "source_short": "Live · Energiemonitor",
            "help": (
                "Aktueller Batteriestand live vom Energiemonitor (SOC % × Speichergröße). "
                "Das ist der Startpunkt der Rechnung."
            ),
        },
        {
            "id": "pv_until",
            "label": f"PV bis Horizont-Ende",
            "value_kwh": round(pv, 1),
            "op": "+",
            "row_type": "item",
            "source_short": f"Wetter · Open-Meteo{risk_note}",
            "help": (
                f"Erwartete Solar-Erzeugung über den rollierenden 7-Tage-Horizont{risk_note}. "
                "Quelle: Open-Meteo-Wetterprognose (stündliche kWh, alle ~3 h aktualisiert)."
            ),
        },
        {
            "id": "hydro_until",
            "label": f"Hydro bis Horizont-Ende",
            "value_kwh": round(hydro, 1),
            "op": "+",
            "row_type": "item",
            "source_short": "Historie · Wochentagsprofil",
            "help": (
                f"Erwartete Wasserkraft über den 7-Tage-Horizont. "
                "Quelle: Durchschnitt aus archiviertem Wochentags-Verbrauchsprofil (Hydro-Anteil)."
            ),
        },
        {
            "id": "base_consumption",
            "label": f"Grundverbrauch bis Horizont-Ende",
            "value_kwh": round(base, 1),
            "op": "−",
            "row_type": "item",
            "source_short": "Historie · Haus ohne Miner",
            "help": (
                f"Geschätzter Hausverbrauch ohne Miner über den 7-Tage-Horizont. "
                "Quelle: Wochentags-Historie (Grundlast-Profil)."
            ),
        },
        {
            "id": "projected_storage",
            "label": "Speicher prognostiziert (bindender Checkpoint)",
            "value_kwh": round(projected, 1),
            "op": "=",
            "row_type": "subtotal",
            "sub": f"{projected_soc} %" if projected_soc is not None else None,
            "source_short": "Summe der Zeilen darüber",
            "help": (
                "Zwischenergebnis ohne Miner-Last am kritischsten SOC-Checkpoint "
                "(niedrigste Prognose gegenüber Ziel)."
            ),
        },
    ]

    if max_soc_percent is not None and storage_kwh:
        steps.append(
            {
                "id": "max_soc",
                "label": f"Max-SoC ({max_soc_percent:.0f} %)",
                "value_kwh": round(max_soc_percent / 100.0 * float(storage_kwh), 1),
                "op": "≤",
                "row_type": "item",
                "source_short": "Einstellung · Obergrenze",
                "help": (
                    f"Während des gesamten 7-Tage-Horizonts darf der Speicher {max_soc_percent:.0f} % "
                    "nicht überschreiten (kontinuierliche Obergrenze für Miner-Budget)."
                ),
            }
        )

    steps.extend(
        [
            {
                "id": "target_storage",
                "label": f"Ziel-Speicher ({target_soc or '—'} % · bindend)",
                "value_kwh": round(target_store, 1),
                "op": "−",
                "row_type": "item",
                "source_short": "Einstellung · SOC-Ziel",
                "help": (
                    f"Mindest-Speicher an jedem SOC-Checkpoint im Horizont "
                    f"(z. B. {tt}). Alle Checkpoints müssen erreichbar sein."
                ),
            },
            {
                "id": "available",
                "label": "Miner-Budget verfügbar",
                "value_kwh": round(available_kwh, 1),
                "op": "=",
                "row_type": "total",
                "highlight": True,
                "source_short": "Ergebnis · für Miner nutzbar",
                "help": (
                    "Maximale Miner-Energie über den 7-Tage-Horizont, sodass alle SOC-Checkpoints "
                    f"≥ Ziel bleiben und Max-SoC ({max_soc_percent or 95:.0f} %) nie überschritten wird."
                ),
            },
        ]
    )

    cp_summary = ""
    if checkpoints:
        cp_parts = [
            f"{c.get('time')} ({c.get('target_datetime', '')[:10]}): {c.get('projected_soc')}%"
            for c in checkpoints[:3]
        ]
        cp_summary = " Checkpoints: " + ", ".join(cp_parts) + "."

    return {
        "mode": mode,
        "target_time": target_time,
        "hours_left": round(hours_left, 1),
        "horizon_label": horizon_label,
        "horizon_end": horizon_end,
        "binding_constraint": binding_constraint,
        "max_soc_percent": max_soc_percent,
        "checkpoints": checkpoints or [],
        "storage_now_kwh": round(storage_now, 1),
        "pv_until_kwh": round(pv, 1),
        "hydro_until_kwh": round(hydro, 1),
        "production_until_kwh": round(pv + hydro, 1),
        "risk_percent": round(risk, 0),
        "base_consumption_kwh": round(base, 1),
        "projected_storage_kwh": round(projected, 1),
        "projected_soc_percent": projected_soc,
        "target_storage_kwh": round(target_store, 1),
        "target_soc_percent": target_soc,
        "available_kwh": round(available_kwh, 1),
        "summary": (
            f"Wie viel Energie die Miner über {horizon_label} verbrauchen dürfen, "
            f"ohne SOC-Ziele zu verfehlen oder Max-SoC zu überschreiten."
        ),
        "note": (
            f"Horizont: jetzt bis {horizon_end or 'now+3d'} ({hours_left:.1f} h). "
            f"Bindende Nebenbedingung: {binding_constraint or 'keine'}.{cp_summary} "
            "Miner-Strom ist hier nicht abgezogen – nur Hausverbrauch (Grundlast)."
        ),
        "steps": steps,
    }


def _control_action_key(action: str) -> str:
    if action in ("Ausschalten", "pause"):
        return "pause"
    if action in ("Einschalten", "resume"):
        return "resume"
    if action in ("Budget knapp", "einschränken", "warn"):
        return "warn"
    return "keep"


def _build_day_control_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    devices: List[Dict[str, Any]],
    miner_notes: Dict[str, str],
    device_rows: List[Dict[str, Any]],
    *,
    balances_ok: bool,
    balance_kwh: float,
    target_time: Optional[str],
    is_forecast: bool,
    surplus_kwh: float,
    hours_left: float,
    all_budget_fit: bool,
    night_lock_active: bool,
) -> List[Dict[str, Any]]:
    """Geplante Miner-Steuerung pro Gerät für einen Tagesblock."""
    if not devices:
        return []

    def _entry(
        row: Dict[str, Any],
        action: str,
        when: str,
        reason: str,
        *,
        action_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "time": when,
            "name": row.get("name") or row.get("ip"),
            "ip": row.get("ip"),
            "priority": row.get("priority"),
            "power_kw": row.get("power_kw"),
            "status": row.get("status"),
            "need_kwh": row.get("need_kwh"),
            "action": action,
            "action_key": action_key or _control_action_key(action),
            "reason": reason,
            "is_forecast": is_forecast,
        }

    planned_by_ip = {
        a["ip"]: a for a in (state.get("planned_actions") or []) if a.get("ip")
    }

    if not is_forecast:
        plans: List[Dict[str, Any]] = []
        has_explicit = False
        for row in device_rows:
            ip = row.get("ip")
            act = planned_by_ip.get(ip)
            if act and act.get("action") in ("pause", "resume"):
                has_explicit = True
                label = "Ausschalten" if act.get("action") == "pause" else "Einschalten"
                when = "Sofort" if act.get("force") else (target_time or "nächster Tick")
                plans.append(
                    _entry(
                        row,
                        label,
                        when,
                        str(act.get("reason") or "Steuerungsalgorithmus"),
                        action_key=act.get("action"),
                    )
                )
        if has_explicit:
            for row in device_rows:
                if any(p.get("ip") == row.get("ip") for p in plans):
                    continue
                plans.append(
                    _entry(
                        row,
                        "Weiterlaufen",
                        "—",
                        "Keine Abschaltung für dieses Gerät geplant.",
                    )
                )
            return sorted(plans, key=lambda p: (p.get("action_key") != "pause", p.get("name") or ""))

        if night_lock_active:
            for row in device_rows:
                if row.get("status") == "running":
                    plans.append(
                        _entry(
                            row,
                            "Ausschalten",
                            target_time or "Sofort",
                            state.get("night_lock_reason")
                            or "Nacht-Sperre – Speicher-Prognose reicht nicht bis SOC-Ziel.",
                        )
                    )
                else:
                    plans.append(
                        _entry(row, "Bleibt aus", "—", "Bereits aus – Nacht-Sperre aktiv.")
                    )
            return plans

        if all_budget_fit and balances_ok:
            return [
                _entry(
                    row,
                    "Weiterlaufen",
                    f"bis {target_time}" if target_time else "—",
                    (
                        f"Budget reicht ({surplus_kwh:.0f} kWh verfügbar, "
                        f"Bedarf {float(row.get('need_kwh') or 0):.0f} kWh bis {target_time or 'Ziel'})."
                    ),
                )
                for row in device_rows
            ]

        if not all_budget_fit:
            remaining = max(0.0, float(surplus_kwh or 0))
            pause_ips: set = set()
            devices_by_ip = {d.get("ip"): d for d in devices}
            running_sorted = sort_for_pause(
                [
                    devices_by_ip[r["ip"]]
                    for r in device_rows
                    if r.get("status") == "running" and r.get("ip") in devices_by_ip
                ]
            )
            for d in running_sorted:
                row = next((r for r in device_rows if r.get("ip") == d.get("ip")), None)
                if not row:
                    continue
                need = float(row.get("need_kwh") or 0)
                if remaining + 0.01 < need:
                    pause_ips.add(row.get("ip"))
                else:
                    remaining -= need
            for row in device_rows:
                ip = row.get("ip")
                need = float(row.get("need_kwh") or 0)
                alloc = float(row.get("allocated_kwh") or 0)
                if ip in pause_ips:
                    plans.append(
                        _entry(
                            row,
                            "Ausschalten",
                            target_time or "bei knappem Budget",
                            (
                                f"Miner-Budget reicht nicht – Bedarf {need:.0f} kWh, "
                                f"nur {alloc:.0f} kWh zugeteilt bis {target_time or 'Ziel'}."
                            ),
                        )
                    )
                elif row.get("status") == "running":
                    plans.append(
                        _entry(
                            row,
                            "Weiterlaufen",
                            f"bis {target_time}" if target_time else "—",
                            f"Anteil {alloc:.0f} kWh im Budget – Priorität höher als abgeschaltete Geräte.",
                        )
                    )
                else:
                    plans.append(
                        _entry(row, "Bleibt aus", "—", "Derzeit aus – Einschalten nur bei ausreichend Budget.")
                    )
            return plans

    deficit = max(0.0, -balance_kwh)
    if deficit <= 0.01 and all_budget_fit:
        return [
            _entry(
                row,
                "Weiterlaufen",
                f"bis {target_time}" if target_time else "—",
                "Tagesbilanz und Miner-Budget reichen – kein Abschalten nötig.",
            )
            for row in device_rows
        ]

    if deficit <= 0.01:
        return [
            _entry(
                row,
                "Weiterlaufen",
                f"bis {target_time}" if target_time else "—",
                "Miner-Budget bis Zielzeit reicht für dieses Gerät.",
            )
            for row in device_rows
        ]

    remaining_deficit = deficit
    pause_ips: set = set()
    when = target_time or "über den Tag"
    for d in sort_for_pause(devices):
        if remaining_deficit <= 0.01:
            break
        ip = d.get("ip")
        pwr = float(d.get("power_kw") or 0)
        window_kwh = round(pwr * hours_left, 1)
        pause_ips.add(ip)
        remaining_deficit -= window_kwh

    plans = []
    for row in device_rows:
        ip = row.get("ip")
        need = float(row.get("need_kwh") or 0)
        if ip in pause_ips:
            prio = row.get("priority") or "Mittel"
            plans.append(
                _entry(
                    row,
                    "Ausschalten",
                    when,
                    (
                        f"Tagesbilanz −{deficit:.0f} kWh – {prio}-Priorität pausieren "
                        f"({need:.0f} kWh bis {target_time or 'Ziel'})."
                    ),
                )
            )
        else:
            plans.append(
                _entry(
                    row,
                    "Weiterlaufen",
                    f"bis {target_time}" if target_time else "—",
                    "Tagesbilanz – dieses Gerät hat ausreichend Priorität/Budget.",
                )
            )

    if remaining_deficit > 0.01:
        plans.append(
            {
                "time": when,
                "name": "Hinweis",
                "ip": None,
                "priority": None,
                "power_kw": None,
                "status": None,
                "need_kwh": None,
                "action": "Budget knapp",
                "action_key": "warn",
                "reason": (
                    f"Selbst geplante Abschaltungen reichen nicht – "
                    f"es fehlen noch ca. {remaining_deficit:.0f} kWh."
                ),
                "is_forecast": is_forecast,
            }
        )
    return plans


def _build_consumption_balance_block(
    config: Dict[str, Any],
    state: Dict[str, Any],
    devices: List[Dict[str, Any]],
    miner_notes: Dict[str, str],
    base_dir: str,
    day_date: date,
    production_total_kwh: float,
    storage_kwh_now: Optional[float],
    target_time: Optional[str],
    is_forecast: bool,
) -> Dict[str, Any]:
    """Verbrauchs-Hero, Tagesbilanz und Steuerungsplan für einen Snapshot."""
    storage = float(config.get("storage_kwh") or 0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    min_kwh = min_soc / 100.0 * storage if storage > 0 else 0.0
    start_kwh = float(storage_kwh_now or 0)
    storage_buffer = round(max(0.0, start_kwh - min_kwh), 2)

    profile = weekday_base_consumption_detail(base_dir, day_date)
    base_kwh = round(float(profile.get("total_kwh") or 0), 2)
    miner_cap = _miner_all_day_kwh(devices)
    available_kwh = round(float(production_total_kwh or 0) + storage_buffer, 2)
    # Keine 24h-Volllast in der Schätzung – Miner-Anteil kommt aus dem Wochenplan
    # (budget_kw × geplante Stunden). Hier nur Grundverbrauch + Cap als Referenz.
    miner_planned = 0.0
    consumption_total = round(base_kwh + miner_planned, 2)
    balance_kwh = round(available_kwh - base_kwh, 2)
    balances_ok = balance_kwh >= -0.01

    # #region agent log
    if not is_forecast:
        _agent_debug_log(
            "H2",
            "energy_balance.py:_build_consumption_balance_block",
            "day_balance excludes miner from consumption_total",
            {
                "base_kwh": base_kwh,
                "miner_planned_kwh": miner_planned,
                "consumption_total_kwh": consumption_total,
                "production_kwh": production_total_kwh,
                "available_kwh": available_kwh,
                "balance_kwh": balance_kwh,
            },
        )
    # #endregion

    return {
        "consumption_daily": {
            "base_kwh": base_kwh,
            "miner_all_day_kwh": miner_cap,
            "miner_planned_kwh": miner_planned,
            "total_with_miners_kwh": consumption_total,
            "weekday_name": profile.get("weekday_name"),
            "sample_count": profile.get("sample_count"),
            "sample_dates": profile.get("sample_dates"),
            "source_note": profile.get("source_note"),
        },
        "day_balance": {
            "available_kwh": available_kwh,
            "production_kwh": round(float(production_total_kwh or 0), 2),
            "storage_buffer_kwh": storage_buffer,
            "consumption_kwh": consumption_total,
            "balance_kwh": balance_kwh,
            "balances_ok": balances_ok,
            "scope_note": (
                "Nur heute (24 h Kalendertag). Die Steuerung entscheidet nach dem "
                "rollierenden 7-Tage-Horizont weiter unten."
            ),
            "formula": (
                f"Verfügbares Budget {available_kwh:.1f} kWh "
                f"(Produktion {production_total_kwh:.1f} + Speicher-Puffer {storage_buffer:.1f}) "
                f"− Verbrauch mit Minern {consumption_total:.1f} kWh "
                f"= {balance_kwh:+.1f} kWh"
            ),
        },
        "control_plan": [],  # wird nach device_rows gesetzt
    }


def _build_energy_budget_snapshot_for_day(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: datetime,
    pv_daily_kwh: float,
    hydro_daily_kwh: float,
    *,
    day_label: str,
    day_date: date,
    is_forecast: bool,
    include_so_far: bool = False,
) -> Dict[str, Any]:
    """Einzelner Budget-Snapshot für einen Kalendertag (live oder Prognose-Kette)."""
    storage = float(config.get("storage_kwh") or 0)
    soc = live.get("soc_percent")
    projection = compute_horizon_projection(config, state, live, base_dir, now)

    surplus_state = dict(state)
    surplus_state["horizon_projection"] = projection
    surplus_state["balance_projection"] = projection
    surplus_kwh = miner_surplus_kwh(config, surplus_state, live, base_dir, now)
    target = next_soc_target(config.get("soc_targets") or [], now)

    so_far_block: Dict[str, Any] = {}
    if include_so_far:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        so_far_expected = expected_base_consumption_kwh(base_dir, midnight, now)
        so_far_actual = actual_base_consumption_kwh(base_dir, midnight, now)
        so_far_diff_pct = None
        if so_far_expected > 0.05:
            so_far_diff_pct = round((so_far_actual - so_far_expected) / so_far_expected * 100.0, 1)
        so_far_block = {
            "expected_kwh": round(so_far_expected, 2),
            "actual_kwh": round(so_far_actual, 2),
            "diff_pct": so_far_diff_pct,
            "until": now.strftime("%H:%M"),
        }

    pv_until = float(projection.get("pv_forecast_kwh") or 0)
    hydro_until = float(projection.get("hydro_forecast_kwh") or 0)
    risk_pct = float(projection.get("risk_factor_percent") or 100.0)
    production_until_total = round(pv_until + hydro_until, 2)
    production_effective = round(float(projection.get("effective_production_kwh") or 0), 2)

    current_kwh = round(float(soc) / 100.0 * storage, 2) if soc is not None and storage > 0 else None
    target_soc = projection.get("target_soc")
    target_kwh = (
        round(float(target_soc) / 100.0 * storage, 2)
        if target_soc is not None and storage > 0
        else None
    )
    binding_cp = projection.get("binding_checkpoint") or {}
    projected_soc = binding_cp.get("projected_soc") or projection.get("projected_soc")
    projected_kwh = binding_cp.get("projected_kwh")
    if projected_kwh is None and projected_soc is not None and storage > 0:
        projected_kwh = round(float(projected_soc) / 100.0 * storage, 2)

    hours_left = float(projection.get("hours_left") or DEFAULT_HORIZON_DAYS * 24)
    target_time = projection.get("target_time") or (target.get("time") if target else None)
    horizon_end = projection.get("horizon_end")
    horizon_days = int(projection.get("horizon_days") or DEFAULT_HORIZON_DAYS)
    checkpoints = projection.get("checkpoints") or []
    binding_constraint = projection.get("binding_constraint")
    max_soc = float(projection.get("max_soc_percent") or config.get("max_soc_percent") or 95.0)
    horizon_display = f"{horizon_days} Tage"
    try:
        if horizon_end:
            he_dt = datetime.fromisoformat(str(horizon_end))
            horizon_display = he_dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        pass
    try:
        target_dt = datetime.fromisoformat(str(horizon_end or projection.get("target_datetime")))
    except Exception:
        target_dt = None

    budget_kw = round(
        float(projection.get("miner_budget_kw") or (surplus_kwh / hours_left if hours_left > 0 else 0)),
        2,
    )

    forecast_note = "Prognose" if is_forecast else "Live"
    formula = (
        f"Miner-Budget ({forecast_note}, 7-Tage rollierend bis {horizon_display}) ≈ {surplus_kwh:.1f} kWh "
        f"({budget_kw:.1f} kW über {hours_left:.1f} h). "
        f"Alle SOC-Checkpoints ≥ {target_soc}% bei {target_time}, Max-SoC {max_soc:.0f} % kontinuierlich. "
        f"Bindend: {binding_constraint or 'keine'}."
    )

    miner_notes = device_label_map()
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    devices = enabled_devices(config)

    total_need_kwh = 0.0
    total_need_kw = 0.0
    running_need_kw = 0.0
    device_rows: List[Dict[str, Any]] = []
    for d in devices:
        ip = d.get("ip")
        pwr = float(d.get("power_kw") or 0)
        need_kwh = round(pwr * hours_left, 2)
        total_need_kwh += need_kwh
        total_need_kw += pwr
        running = miner_status.get(ip) == "running"
        if running:
            running_need_kw += pwr
        prio_key = normalize_device_priority(d.get("priority"))
        device_rows.append(
            {
                "ip": ip,
                "name": miner_notes.get(ip, ip),
                "priority": PRIORITY_LABEL.get(prio_key, "5"),
                "priority_key": prio_key,
                "power_kw": round(pwr, 2),
                "status": "running" if running else "paused",
                "need_kwh": need_kwh,
            }
        )

    total_need_kwh = round(total_need_kwh, 2)
    total_need_kw = round(total_need_kw, 2)
    running_need_kw = round(running_need_kw, 2)
    all_fit = surplus_kwh >= total_need_kwh - 0.01

    # Greedy nach Priorität: höchste Prio bekommt Surplus zuerst (längste Laufzeit)
    try:
        from control_algorithm.priority_runtime import allocate_surplus_by_priority

        prio_alloc = allocate_surplus_by_priority(devices, surplus_kwh, hours_left)
    except Exception:
        prio_alloc = {}

    max_bar = max(surplus_kwh, total_need_kwh, 1.0)
    for row in device_rows:
        need = float(row["need_kwh"])
        alloc_row = prio_alloc.get(str(row.get("ip") or "")) or {}
        if alloc_row:
            allocated_kwh = float(alloc_row.get("allocated_kwh") or 0)
            allocated_kw = float(alloc_row.get("allocated_kw") or 0)
            row["planned_hours"] = alloc_row.get("planned_hours")
            row["runtime_role"] = alloc_row.get("role")
        elif all_fit:
            allocated_kwh = need
            allocated_kw = round(allocated_kwh / hours_left, 2) if hours_left > 0 else 0.0
        elif total_need_kwh > 0:
            share = surplus_kwh / total_need_kwh
            allocated_kwh = round(need * share, 2)
            allocated_kw = round(allocated_kwh / hours_left, 2) if hours_left > 0 else 0.0
        else:
            allocated_kwh = 0.0
            allocated_kw = 0.0
        row["allocated_kwh"] = allocated_kwh
        row["allocated_kw"] = allocated_kw
        row["bar_need_pct"] = round(need / max_bar * 100.0, 1)
        row["bar_budget_pct"] = round(min(allocated_kwh, surplus_kwh) / max_bar * 100.0, 1)

    base_consumption = float(projection.get("base_consumption_kwh") or 0)

    pv_daily = round(float(pv_daily_kwh or 0), 2)
    hydro_daily = round(float(hydro_daily_kwh or 0), 2)
    production_daily_total = round(pv_daily + hydro_daily, 2)

    consumption_balance = _build_consumption_balance_block(
        config,
        state,
        devices,
        miner_notes,
        base_dir,
        day_date,
        production_daily_total,
        current_kwh,
        target_time,
        is_forecast,
    )

    nl_active = bool(state.get("night_lock_active")) if not is_forecast else False
    steering_summary = _build_steering_summary(
        day_balance_ok=consumption_balance["day_balance"]["balances_ok"],
        day_balance_kwh=float(consumption_balance["day_balance"]["balance_kwh"] or 0),
        all_devices_fit=all_fit,
        budget_kw=budget_kw,
        total_need_kw=total_need_kw,
        running_need_kw=running_need_kw,
        surplus_kwh=surplus_kwh,
        hours_left=hours_left,
        binding_constraint=binding_constraint,
        night_lock_active=nl_active,
        horizon_display=horizon_display,
        checkpoints=checkpoints,
        soc_percent=float(soc) if soc is not None else None,
        target_soc=float(target_soc) if target_soc is not None else None,
        max_soc_percent=max_soc,
    )

    miner_budget_breakdown = _build_miner_budget_breakdown(
        storage_now_kwh=current_kwh,
        pv_until_kwh=pv_until,
        hydro_until_kwh=hydro_until,
        base_consumption_kwh=base_consumption,
        risk_pct=risk_pct,
        projected_kwh=projected_kwh,
        projected_soc=projected_soc,
        target_kwh=target_kwh,
        target_soc=target_soc,
        target_time=target_time,
        hours_left=hours_left,
        available_kwh=surplus_kwh,
        is_forecast=is_forecast,
        max_soc_percent=max_soc,
        horizon_end=horizon_end,
        binding_constraint=binding_constraint,
        checkpoints=checkpoints,
        storage_kwh=storage if storage > 0 else None,
    )

    control_plan = _build_day_control_plan(
        config,
        state,
        devices,
        miner_notes,
        device_rows,
        balances_ok=consumption_balance["day_balance"]["balances_ok"],
        balance_kwh=consumption_balance["day_balance"]["balance_kwh"],
        target_time=horizon_display,
        is_forecast=is_forecast,
        surplus_kwh=surplus_kwh,
        hours_left=hours_left,
        all_budget_fit=all_fit,
        night_lock_active=bool(state.get("night_lock_active")) if not is_forecast else False,
    )
    consumption_balance["control_plan"] = control_plan

    snap: Dict[str, Any] = {
        "day_label": day_label,
        "day_date": day_date.isoformat(),
        "is_forecast": is_forecast,
        "target_time": target_time,
        "horizon_days": horizon_days,
        "horizon_end": horizon_end,
        "horizon_display": horizon_display,
        "checkpoints": checkpoints,
        "max_soc_percent": max_soc,
        "binding_constraint": binding_constraint,
        "binding_label": _binding_constraint_label(binding_constraint),
        "steering_summary": steering_summary,
        "hours_until_target": round(hours_left, 2),
        "production_daily": {
            "pv_kwh": pv_daily,
            "hydro_kwh": hydro_daily,
            "total_kwh": production_daily_total,
            "source_note": "Wetter PV (~3 h) + Hydro Wochentagsprofil",
        },
        "production_until_target": {
            "pv_kwh": pv_until,
            "hydro_kwh": hydro_until,
            "total_kwh": production_until_total,
            "effective_kwh": production_effective,
            "risk_factor_percent": risk_pct,
        },
        "storage": {
            "soc_percent": soc,
            "kwh_now": current_kwh,
            "target_soc_percent": target_soc,
            "target_kwh": target_kwh,
            "projected_soc_percent": projected_soc,
            "projected_kwh": projected_kwh,
            "storage_capacity_kwh": storage if storage > 0 else None,
        },
        "miner_budget": {
            "available_kwh": surplus_kwh,
            "available_kw": budget_kw,
            "total_need_kwh": total_need_kwh,
            "total_need_kw": total_need_kw,
            "running_need_kw": running_need_kw,
            "all_devices_fit": all_fit,
            "bar_available_pct": round(surplus_kwh / max_bar * 100.0, 1),
            "bar_need_pct": round(total_need_kwh / max_bar * 100.0, 1),
            "formula": formula,
            "breakdown": miner_budget_breakdown,
            "devices": device_rows,
        },
        "night_lock_active": bool(state.get("night_lock_active")) if not is_forecast else False,
        "_target_datetime": target_dt,
        "_projected_kwh": projected_kwh,
        **consumption_balance,
    }
    if include_so_far:
        snap["so_far"] = so_far_block
    return snap


def build_multi_day_energy_snapshots(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Sieben Budget-Snapshots (Heute + 6 Tage) mit geketteter SOC-Prognose."""
    now = now or datetime.now()
    storage = float(config.get("storage_kwh") or 0)
    today = now.date()
    horizon = planning_horizon_days(config)

    _DAY_LABELS = ["Heute", "Morgen", "Übermorgen", "Tag 4", "Tag 5", "Tag 6", "Tag 7"]

    snapshots: List[Dict[str, Any]] = []
    chained_kwh: Optional[float] = None
    if live.get("soc_percent") is not None and storage > 0:
        chained_kwh = float(live["soc_percent"]) / 100.0 * storage

    for offset in range(horizon):
        label = _DAY_LABELS[offset] if offset < len(_DAY_LABELS) else f"+{offset} T"
        day_date = today + timedelta(days=offset)
        day_start = now if offset == 0 else datetime.combine(day_date, datetime.min.time())
        pv_daily = daily_pv_kwh_for_date(state, day_date, today)
        hydro_daily = expected_hydro_full_day_kwh(base_dir, day_date)

        day_live = dict(live)
        if offset > 0 and chained_kwh is not None and storage > 0:
            day_live["soc_percent"] = _kwh_to_soc(chained_kwh, storage)
        elif offset > 0:
            day_live["soc_percent"] = None

        snap = _build_energy_budget_snapshot_for_day(
            config,
            state,
            day_live,
            base_dir,
            day_start,
            pv_daily,
            hydro_daily,
            day_label=label,
            day_date=day_date,
            is_forecast=(offset > 0),
            include_so_far=(offset == 0),
        )
        target_dt = snap.pop("_target_datetime", None)
        projected_kwh = snap.pop("_projected_kwh", None)
        snapshots.append(snap)

        if offset < horizon - 1 and storage > 0 and chained_kwh is not None:
            end_of_day = datetime.combine(day_date, datetime.max.time()).replace(
                hour=23, minute=59, second=59, microsecond=0
            )
            kwh_at_target = projected_kwh
            if kwh_at_target is None:
                kwh_at_target = chained_kwh
            if target_dt and target_dt < end_of_day:
                chained_kwh = _battery_kwh_at(
                    config, state, base_dir, float(kwh_at_target), target_dt, end_of_day
                )
            elif target_dt and target_dt >= end_of_day:
                chained_kwh = float(kwh_at_target)
            else:
                chained_kwh = _battery_kwh_at(
                    config, state, base_dir, chained_kwh, day_start, end_of_day
                )

    return snapshots


def _hourly_profile_for_date(base_dir: str, day_date: date) -> Tuple[List[float], List[float]]:
    history = load_history(base_dir)
    prof = get_weekday_profile(history, day_date.weekday(), base_dir)
    base = list(prof.get("hourly_base_kwh_avg") or [0.0] * 24)
    hydro = list(prof.get("hourly_hydro_kwh_avg") or [0.0] * 24)
    return base, hydro


def _night_consumption_drop_hour(base_dir: str, day_date: date) -> int:
    """Stunde mit deutlich gesunkenem Basisverbrauch (Nachtbeginn, aus Wochentagsprofil)."""
    hourly, _ = _hourly_profile_for_date(base_dir, day_date)
    if not hourly or max(hourly) <= 0:
        return 22
    median = sorted(hourly)[len(hourly) // 2]
    threshold = median * 0.55
    for h in range(18, 24):
        if float(hourly[h]) <= threshold:
            return h
    return 22


# Spitze darf bis Fleet; Mittelwert über PV-Stunden bleibt ≈ 7-Tage-Gleichlast.
DAYTIME_MINER_PEAK_FACTOR = 2.0


def _hourly_net_surplus_kwh(
    pv_hours: Dict[str, Any],
    hourly_base: List[float],
    hourly_hydro: List[float],
) -> Tuple[List[float], List[float], List[int]]:
    """Pro Stunde: PV, Netto-Überschuss (PV+Hydro−Grundlast), aktive PV-Stunden."""
    nets: List[float] = []
    pvs: List[float] = []
    active: List[int] = []
    for h in range(24):
        hid = f"h{h:02d}"
        pv = float((pv_hours or {}).get(hid, 0) or 0)
        base = float(hourly_base[h] if h < len(hourly_base) else 0) or 0.0
        hydro = float(hourly_hydro[h] if h < len(hourly_hydro) else 0) or 0.0
        pvs.append(pv)
        nets.append(max(0.0, pv + hydro - base))
        if pv > 0.05 or nets[-1] > 0.05:
            active.append(h)
    return nets, pvs, active


def day_surplus_kwh(pv_kwh: float, hydro_kwh: float, base_kwh: float) -> float:
    """Tages-Nettoüberschuss, den Miner maximal fressen dürfen (sonst Netz/Akku)."""
    return max(0.0, float(pv_kwh or 0) + float(hydro_kwh or 0) - float(base_kwh or 0))


def cap_planned_miner_to_day_surplus(
    planned_miner_kwh: float,
    *,
    pv_kwh: float,
    hydro_kwh: float = 0.0,
    base_kwh: float,
) -> float:
    """
    Harte Energie-Invariante: geplanter Miner-Anteil ≤ Tagesüberschuss.

    Verhindert die wiederkehrende UI-/Plan-Lüge
    „Grundlast 14 kWh + Flotte 10 kW × viele Stunden ≈ 120 kWh“
    an Tagen mit nur ~35 kWh PV.
    """
    planned = max(0.0, float(planned_miner_kwh or 0))
    cap = day_surplus_kwh(pv_kwh, hydro_kwh, base_kwh)
    return round(min(planned, cap), 2)


def shape_hourly_miner_budgets(
    *,
    budget_kw: float,
    pv_hours: Dict[str, Any],
    hourly_base: List[float],
    hourly_hydro: List[float],
    total_device_kw: float,
    steering_mode: str = "normal",  # API-Stabilität; keine Phantom-Nachtfüllung mehr
) -> List[float]:
    """
    Verteilt das Horizont-Budget über den Tag nach Netto-Überschuss.

    PV + Hydro − Grundlast peakt mittags; Spitzenstunden bekommen mehr Miner-kW.
    **Energie-wahr:** je Stunde gilt miner_kWh ≤ Nettoüberschuss dieser Stunde;
    Tages-Summe ≤ max(0, PV+Hydro−Grundlast).
    Keine Phantom-Schulterlast (früher budget×0.5 ohne PV) — die hat
    Verbrauchsschätzungen auf 100+ kWh getrieben bei nur ~35 kWh PV.

    budget_kw bleibt die Leistungs-Obergrenze (Mittel/Peak-Form), nicht „kW × 24 h“.
    steering_mode wird für die Form nicht mehr missbraucht (Survival steckt im Budget).
    """
    del steering_mode  # bewusst ungenutzt – Signatur stabil für Aufrufer
    out = [0.0] * 24
    budget = max(0.0, float(budget_kw or 0))
    fleet = max(0.0, float(total_device_kw or 0))
    if budget < 0.05:
        return out

    peak_cap = fleet if fleet > 0.5 else budget * DAYTIME_MINER_PEAK_FACTOR

    nets, pvs, active = _hourly_net_surplus_kwh(pv_hours, hourly_base, hourly_hydro)

    if not active:
        # Ohne PV: kein erfundenes Miner-Soll (früher budget×0.5 über 5–22 Uhr).
        return out

    net_sum = sum(nets[h] for h in active)
    use_net = net_sum >= 0.5
    weights = {h: (nets[h] if use_net else pvs[h]) for h in active}
    wsum = sum(weights.values()) or 1.0
    n_active = len(active)

    for h in active:
        shaped = budget * n_active * float(weights[h]) / wsum
        # Harte Kappe: keine geplante Last über dem stündlichen Überschuss
        energy_cap = float(nets[h])
        out[h] = round(min(peak_cap, max(0.0, shaped), energy_cap), 3)

    # Tages-Invariante: sum(max(0, stündlich)) kann > (PV+Hydro−Grundlast) sein.
    # Skaliere auf echten Tagesüberschuss, sonst wieder „Miner > Netto“.
    day_prod = sum(pvs) + sum(
        float(hourly_hydro[h] if h < len(hourly_hydro) else 0) or 0.0 for h in range(24)
    )
    day_base = sum(
        float(hourly_base[h] if h < len(hourly_base) else 0) or 0.0 for h in range(24)
    )
    day_cap = max(0.0, day_prod - day_base)
    shaped_sum = sum(out)
    if shaped_sum > day_cap + 0.05 and shaped_sum > 0 and day_cap >= 0:
        scale = day_cap / shaped_sum
        out = [round(v * scale, 3) for v in out]

    return out


def current_hour_shaped_miner_kw(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
    budget_kw: Optional[float] = None,
) -> Optional[float]:
    """Miner-Soll-kW der aktuellen Stunde aus Netto-Überschuss-Kurve."""
    now = now or datetime.now()
    projection = (
        state.get("horizon_projection")
        or state.get("balance_projection")
        or {}
    )
    sustained = float(
        budget_kw
        if budget_kw is not None
        else (projection.get("miner_budget_kw") or 0)
    )
    if sustained < 0.05:
        return None

    total_kw = sum(float(d.get("power_kw") or 0) for d in enabled_devices(config))
    if total_kw <= 0:
        return None

    day = now.date()
    try:
        base_scale = float(state.get("base_load_scale") or 1.0)
    except (TypeError, ValueError):
        base_scale = 1.0
    hourly_base, hourly_hydro = _hourly_profile_for_date(base_dir, day)
    hourly_base = [float(v or 0) * base_scale for v in hourly_base]
    pv_hours = dict(_pv_hours_for_date(state, day, day) or {})
    live_pv = float(live.get("pv_kw") or 0)
    if live_pv >= 0.5:
        hid = f"h{now.hour:02d}"
        pv_hours[hid] = max(float(pv_hours.get(hid, 0) or 0), live_pv)

    ctx = projection.get("weather_context") if isinstance(projection, dict) else None
    steering = "normal"
    if isinstance(ctx, dict):
        for d in ctx.get("days") or []:
            if int(d.get("offset") or -1) == 0:
                steering = str(d.get("steering_mode") or "normal")
                break

    shaped = shape_hourly_miner_budgets(
        budget_kw=sustained,
        pv_hours=pv_hours,
        hourly_base=hourly_base,
        hourly_hydro=hourly_hydro,
        total_device_kw=total_kw,
        steering_mode=steering,
    )
    return float(shaped[now.hour])


def compute_24h_absorption_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    week_plan: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    night_fraction: Optional[float] = None,
) -> Dict[str, Any]:
    """
    24h-Bedarf an Miner-Energie, damit kein Export entsteht (Wetter + Speicher).

    night_fraction: max. Anteil des Bedarfs, der nachts (22–06) eingeplant werden darf.
    """
    now = now or datetime.now()
    storage = float(config.get("storage_kwh") or 0)
    caps = _planning_band_caps(config, state, live=live)
    max_soc = float(caps["plan_max"])
    min_soc = float(config.get("min_soc_percent") or 35.0)
    soc = live.get("soc_percent")
    week_plan = week_plan or state.get("energy_week_plan") or {}

    try:
        from control_algorithm.steering_learning import night_absorption_max_fraction

        night_frac = (
            float(night_fraction)
            if night_fraction is not None
            else night_absorption_max_fraction(state)
        )
    except Exception:
        night_frac = float(night_fraction) if night_fraction is not None else 0.15
    night_frac = max(0.0, min(0.4, night_frac))

    hourly_need = [0.0] * 24
    hourly_net = [0.0] * 24
    slots: List[Dict[str, Any]] = []

    for day in week_plan.get("days") or []:
        day_date_s = str(day.get("day_date") or "")
        for row in day.get("hours") or []:
            h = int(row.get("hour") or 0)
            try:
                slot_dt = datetime.combine(
                    date.fromisoformat(day_date_s), datetime.min.time()
                ).replace(hour=h)
            except ValueError:
                continue
            if slot_dt < now.replace(minute=0, second=0, microsecond=0):
                continue
            if slot_dt >= now + timedelta(hours=24):
                continue
            pv = float(row.get("pv_kwh") or 0)
            hydro = float(row.get("hydro_kwh") or 0)
            base = float(row.get("base_kwh") or 0)
            net = max(0.0, pv + hydro - base)
            idx = min(23, max(0, int((slot_dt - now.replace(minute=0, second=0, microsecond=0)).total_seconds() // 3600)))
            hourly_net[idx] += net
            slots.append({"time": slot_dt, "net_kwh": net, "hour_index": idx})

    gross_surplus = sum(hourly_net)
    headroom_kwh = 0.0
    reserve_kwh = 0.0
    if storage > 0 and soc is not None:
        soc_f = float(soc)
        headroom_kwh = max(0.0, (max_soc - soc_f) / 100.0 * storage)
        reserve_kwh = max(0.0, (soc_f - min_soc) / 100.0 * storage * 0.25)

    total_need = max(0.0, gross_surplus - headroom_kwh - reserve_kwh)
    if total_need < 0.05 and gross_surplus > 0.5:
        total_need = max(0.0, gross_surplus * 0.3)

    if total_need > 0.05 and sum(hourly_net) > 0.05:
        for i, net in enumerate(hourly_net):
            if net <= 0:
                continue
            share = total_need * net / sum(hourly_net)
            hour_of_day = (now.hour + i) % 24
            is_night = hour_of_day >= 22 or hour_of_day < 6
            if is_night:
                share *= night_frac / max(night_frac, 0.05) if night_frac > 0 else 0.2
            hourly_need[i] = round(share, 3)

        night_sum = sum(
            hourly_need[i]
            for i in range(24)
            if (now.hour + i) % 24 >= 22 or (now.hour + i) % 24 < 6
        )
        day_sum = sum(hourly_need) - night_sum
        max_night = total_need * night_frac
        if night_sum > max_night and day_sum > 0:
            scale = max_night / night_sum if night_sum > 0 else 0.0
            for i in range(24):
                hour_of_day = (now.hour + i) % 24
                if hour_of_day >= 22 or hour_of_day < 6:
                    hourly_need[i] = round(hourly_need[i] * scale, 3)
            remainder = total_need - sum(hourly_need)
            day_indices = [
                i
                for i in range(24)
                if 6 <= (now.hour + i) % 24 < 22 and hourly_net[i] > 0
            ]
            if day_indices and remainder > 0:
                per = remainder / len(day_indices)
                for i in day_indices:
                    hourly_need[i] = round(hourly_need[i] + per, 3)

    total_device_kw = sum(float(d.get("power_kw") or 0) for d in enabled_devices(config))
    active_hours = sum(1 for v in hourly_need if v > 0.05)
    devices_needed_kw = 0.0
    if active_hours > 0 and total_need > 0:
        devices_needed_kw = round(min(total_device_kw, total_need / max(active_hours, 1)), 2)

    return {
        "total_need_kwh": round(total_need, 2),
        "hourly_need_kwh": hourly_need,
        "hourly_net_surplus_kwh": [round(v, 2) for v in hourly_net],
        "devices_needed_kw": devices_needed_kw,
        "gross_surplus_kwh": round(gross_surplus, 2),
        "headroom_kwh": round(headroom_kwh, 2),
        "plan_max_soc_percent": max_soc,
        "plan_min_soc_percent": float(caps["plan_min"]),
        "band_overfill_risk": float(caps.get("overfill_risk") or 0),
        "band_underfill_risk": float(caps.get("underfill_risk") or 0),
        "night_fraction": round(night_frac, 3),
        "generated_at": now.isoformat(),
    }


def build_weekly_energy_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """7-Tage-Stundenplan für Dashboard (Produktion, Verbrauch, SOC projiziert/ist)."""
    now = now or datetime.now()
    storage = float(config.get("storage_kwh") or 0)
    caps = _planning_band_caps(config, state, live=live)
    plan_max = float(caps["plan_max"])
    plan_min = float(caps["plan_min"])
    max_soc = float(caps["hard_max"])
    today = now.date()
    horizon = planning_horizon_days(config)
    projection = state.get("horizon_projection") or compute_horizon_projection(
        config, state, live, base_dir, now
    )
    budget_kw = float(projection.get("miner_budget_kw") or 0)
    # #region agent log
    _agent_debug_log(
        "H3",
        "energy_balance.py:build_weekly_energy_plan",
        "week_plan budget source",
        {
            "budget_kw": budget_kw,
            "budget_kw_raw": projection.get("miner_budget_kw_raw"),
            "surplus_kwh": projection.get("miner_surplus_kwh"),
            "binding": projection.get("binding_constraint"),
            "used_stored_projection": state.get("horizon_projection") is not None,
        },
    )
    # #endregion
    total_device_kw = sum(float(d.get("power_kw") or 0) for d in enabled_devices(config))
    try:
        base_scale = float(state.get("base_load_scale") or 1.0)
    except (TypeError, ValueError):
        base_scale = 1.0
    ctx = projection.get("weather_context")
    if not isinstance(ctx, dict):
        ctx = weather_context(config, state, days=horizon)
    days_ctx = {int(d.get("offset") or 0): d for d in (ctx.get("days") or [])}

    start_kwh: Optional[float] = None
    if live.get("soc_percent") is not None and storage > 0:
        start_kwh = float(live["soc_percent"]) / 100.0 * storage

    # Ist-SOC der letzten 24 h (heute, stündlich) aus Timeseries
    hourly_soc_actual = get_hourly_actual_soc_today(base_dir)
    hourly_cons_actual: List[float] = [0.0] * 24
    hourly_pv_actual: Dict[str, float] = {}
    try:
        br = get_hourly_consumption_breakdown_today(base_dir) or {}
        tot = list(br.get("total_kwh") or [])
        for i, v in enumerate(tot[:24]):
            hourly_cons_actual[i] = max(0.0, float(v or 0))
    except Exception:
        pass
    try:
        from control_algorithm.slot_profiles import get_actual_pv_kwh_by_hours_today

        hourly_pv_actual = dict(
            get_actual_pv_kwh_by_hours_today(base_dir, now) or {}
        )
    except Exception:
        hourly_pv_actual = {}

    plan_days: List[Dict[str, Any]] = []
    chained_kwh = start_kwh
    weekday_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

    for offset in range(horizon):
        day_date = today + timedelta(days=offset)
        day_ctx = days_ctx.get(offset) or {}
        day_start = now if offset == 0 else datetime.combine(day_date, datetime.min.time())
        profile = weekday_base_consumption_detail(base_dir, day_date)
        hourly_base_raw, hourly_hydro = _hourly_profile_for_date(base_dir, day_date)
        hourly_base = [float(v or 0) * base_scale for v in hourly_base_raw]
        pv_hours = dict(_pv_hours_for_date(state, day_date, today) or {})
        if offset == 0 and float(live.get("pv_kw") or 0) > 0.5:
            hid_now = f"h{now.hour:02d}"
            pv_hours[hid_now] = max(
                float(pv_hours.get(hid_now, 0) or 0), float(live.get("pv_kw") or 0)
            )
        miner_by_hour = shape_hourly_miner_budgets(
            budget_kw=budget_kw,
            pv_hours=pv_hours,
            hourly_base=hourly_base,
            hourly_hydro=hourly_hydro,
            total_device_kw=total_device_kw,
            steering_mode=str(day_ctx.get("steering_mode") or "normal"),
        )

        if offset == 0 and chained_kwh is None:
            chained_kwh = 0.0

        hours_out: List[Dict[str, Any]] = []
        daily_pv = 0.0
        daily_base = 0.0
        daily_hydro = 0.0
        daily_miner = 0.0

        # Tag 0: Kette ab Tagesbeginn (erster Ist-SoC), nicht ab Live-Nachmittag —
        # sonst bleiben alle vergangenen Nachtstunden flach auf aktuellem SoC (~100%).
        if offset == 0 and storage > 0:
            seed_soc: Optional[float] = None
            first_h: Optional[int] = None
            for i, s in enumerate(hourly_soc_actual):
                if s is not None:
                    first_h = i
                    seed_soc = float(s)
                    break
            if seed_soc is None and live.get("soc_percent") is not None:
                seed_soc = float(live["soc_percent"])
                first_h = 0
            if seed_soc is not None:
                end_kwh = seed_soc / 100.0 * storage

                def _past_energy(hb: int) -> Tuple[float, float]:
                    """Vergangenheit: Ist-PV/Ist-Last, kein fiktiver Miner-Plan."""
                    hid_b = f"h{hb:02d}"
                    pv_a = hourly_pv_actual.get(hid_b)
                    if pv_a is not None:
                        pv_b = float(pv_a)
                    else:
                        pv_b = float(pv_hours.get(hid_b, 0) or 0)
                    hydro_b = float(hourly_hydro[hb] if hb < len(hourly_hydro) else 0)
                    production_b = (pv_b + hydro_b) * float(caps.get("pv_scale") or 1.0)
                    if hb < len(hourly_cons_actual) and hourly_cons_actual[hb] > 0.05:
                        load_b = float(hourly_cons_actual[hb])
                    else:
                        load_b = float(hourly_base[hb] if hb < len(hourly_base) else 0)
                    return production_b, load_b

                # hourly_soc_actual[h] = Ende Stunde h → bis Mitternacht zurückrechnen
                if first_h is not None and first_h > 0:
                    for hb in range(first_h, 0, -1):
                        production_b, load_b = _past_energy(hb)
                        end_kwh = end_kwh - production_b + load_b
                production_b, load_b = _past_energy(0)
                chained_kwh = _cap_battery_kwh(
                    end_kwh - production_b + load_b,
                    storage,
                    plan_max,
                )

        for h in range(24):
            hid = f"h{h:02d}"
            pv_kwh = float(pv_hours.get(hid, 0) or 0)
            base_kwh = float(hourly_base[h] if h < len(hourly_base) else 0)
            hydro_kwh = float(hourly_hydro[h] if h < len(hourly_hydro) else 0)
            hour_dt = datetime.combine(day_date, datetime.min.time()).replace(hour=h)
            is_past = offset == 0 and hour_dt < now.replace(minute=0, second=0, microsecond=0)
            is_current = offset == 0 and h == now.hour

            miner_kwh = float(miner_by_hour[h] if h < len(miner_by_hour) else 0.0)

            if chained_kwh is not None and storage > 0:
                if is_current and live.get("soc_percent") is not None:
                    # Ab „Jetzt“: an Live-SoC anbinden, dann weiter in die Zukunft rechnen
                    chained_kwh = float(live["soc_percent"]) / 100.0 * storage
                elif is_past and h < len(hourly_soc_actual) and hourly_soc_actual[h] is not None:
                    # Vergangenheit: Ist-SoC verankern — kein Leer-Rechnen durch Miner-Plan
                    chained_kwh = float(hourly_soc_actual[h]) / 100.0 * storage
                elif is_past:
                    # Kein Ist-SoC: Bilanz mit Ist-PV/Ist-Last (ohne geplanten Miner)
                    pv_a = hourly_pv_actual.get(hid)
                    production = (
                        float(pv_a) if pv_a is not None else pv_kwh
                    ) + hydro_kwh
                    production *= float(caps.get("pv_scale") or 1.0)
                    if h < len(hourly_cons_actual) and hourly_cons_actual[h] > 0.05:
                        load_kwh = float(hourly_cons_actual[h])
                    else:
                        load_kwh = base_kwh
                    chained_kwh = _cap_battery_kwh(
                        chained_kwh + production - load_kwh,
                        storage,
                        plan_max,
                    )
                else:
                    # Zukunft: Plan-Bilanz (PV/Hydro − Grundlast − Miner)
                    production = (pv_kwh + hydro_kwh) * float(caps.get("pv_scale") or 1.0)
                    chained_kwh = _cap_battery_kwh(
                        chained_kwh + production - base_kwh - miner_kwh,
                        storage,
                        plan_max,
                    )

            soc_proj = _kwh_to_soc(chained_kwh, storage) if chained_kwh is not None and storage > 0 else None
            soc_actual = None
            if offset == 0:
                if is_current and live.get("soc_percent") is not None:
                    soc_actual = round(float(live["soc_percent"]), 1)
                elif h < len(hourly_soc_actual) and hourly_soc_actual[h] is not None:
                    # Vergangene Stunden: gespeicherter Ist-SOC (24h-Timeseries)
                    soc_actual = hourly_soc_actual[h]

            hours_out.append(
                {
                    "hour": h,
                    "time_label": f"{h:02d}:00",
                    "pv_kwh": round(pv_kwh, 2),
                    "hydro_kwh": round(hydro_kwh, 2),
                    "base_kwh": round(base_kwh, 2),
                    "miner_kwh_planned": round(miner_kwh, 2),
                    "soc_projected_pct": soc_proj,
                    "soc_actual_pct": soc_actual,
                    "is_past": is_past,
                    "is_current": is_current,
                }
            )
            daily_pv += pv_kwh
            daily_base += base_kwh
            daily_hydro += hydro_kwh
            daily_miner += miner_kwh

        # Belt-and-suspenders: Tages-Miner nie über PV+Hydro−Grundlast
        daily_miner = cap_planned_miner_to_day_surplus(
            daily_miner,
            pv_kwh=daily_pv,
            hydro_kwh=daily_hydro,
            base_kwh=daily_base,
        )

        wd = weekday_names[day_date.weekday()]
        plan_days.append(
            {
                "day_label": "Heute" if offset == 0 else wd,
                "day_date": day_date.isoformat(),
                "weekday": wd,
                "offset": offset,
                "weather_tier": day_ctx.get("weather_tier", "normal"),
                "steering_mode": day_ctx.get("steering_mode", "normal"),
                "daily_totals": {
                    "pv_kwh": round(daily_pv, 1),
                    "base_kwh": round(daily_base, 1),
                    "hydro_kwh": round(daily_hydro, 1),
                    "miner_kwh": round(daily_miner, 1),
                },
                "last_pv_hour": day_ctx.get("last_pv_hour"),
                "night_consumption_drop_hour": _night_consumption_drop_hour(base_dir, day_date),
                "hours": hours_out,
            }
        )

    return {
        "horizon_days": horizon,
        "generated_at": now.isoformat(),
        "bad_streak_days": int(ctx.get("bad_streak_days") or 0),
        "bad_streak_start_offset": int(ctx.get("bad_streak_start_offset") or 0),
        "reference_kwh": ctx.get("reference_kwh"),
        "risk_factor_percent": float(projection.get("risk_factor_percent") or 100.0),
        "plan_max_soc_percent": plan_max,
        "plan_min_soc_percent": plan_min,
        "hard_max_soc_percent": max_soc,
        "band_pv_scale": float(caps.get("pv_scale") or 1.0),
        "band_overfill_risk": float(caps.get("overfill_risk") or 0),
        "band_underfill_risk": float(caps.get("underfill_risk") or 0),
        "band_note": caps.get("note"),
        "days": plan_days,
    }


def _consumption_pattern_estimate(
    *,
    forecast_total_kwh: Optional[float],
    forecast_base_kwh: Optional[float],
    total_actual_kwh: float,
    base_actual_kwh: float,
    expected_base_so_far_kwh: float,
    now: datetime,
) -> Dict[str, Any]:
    """Intraday-Muster für Verbrauch (Pace auf Grundlast + geplante Miner)."""
    empty: Dict[str, Any] = {
        "estimated_kwh": None,
        "forecast_kwh": None,
        "delta_vs_forecast_pct": None,
        "pace_ratio": None,
        "actual_so_far_kwh": round(total_actual_kwh, 1),
        "expected_so_far_kwh": None,
        "note": "Zu wenig Daten für Muster-Schätzung.",
    }
    if forecast_total_kwh is None or forecast_base_kwh is None:
        return empty

    fc_total = float(forecast_total_kwh)
    fc_base = float(forecast_base_kwh)
    miner_planned = max(0.0, fc_total - fc_base)
    miner_actual = max(0.0, float(total_actual_kwh) - float(base_actual_kwh))
    expected_so_far = float(expected_base_so_far_kwh) + miner_actual
    empty["forecast_kwh"] = round(fc_total, 1)
    empty["expected_so_far_kwh"] = round(expected_so_far, 1)

    min_expected = 8.0
    min_hour = 8
    until_hour = now.hour + now.minute / 60.0
    if expected_base_so_far_kwh < min_expected or until_hour < min_hour:
        empty["note"] = (
            "Muster-Schätzung wartet auf mehr Ist-Stunden "
            f"(bisher {total_actual_kwh:.0f} kWh, erwartet bis jetzt "
            f"{expected_so_far:.0f} kWh)."
        )
        return empty

    pace = float(base_actual_kwh) / max(expected_base_so_far_kwh, 0.01)
    pace = max(0.40, min(1.60, pace))
    remaining_base = max(0.0, fc_base - float(expected_base_so_far_kwh))
    remaining_miner = max(0.0, miner_planned - miner_actual)
    estimated = float(total_actual_kwh) + remaining_base * pace + remaining_miner
    estimated = max(float(total_actual_kwh), estimated)
    delta_pct = ((estimated - fc_total) / fc_total * 100.0) if fc_total > 0.05 else 0.0

    if abs(delta_pct) < 5:
        verdict = "passt zur Prognose"
    elif delta_pct <= -8:
        verdict = "vermutlich zu hoch geschätzt"
    else:
        verdict = "vermutlich zu niedrig geschätzt"

    note = (
        f"Muster: ~{estimated:.0f} kWh ({verdict}, {delta_pct:+.0f} % vs. Plan "
        f"{fc_total:.0f} kWh). Pace Grundlast {pace * 100:.0f} % · "
        f"bisher {total_actual_kwh:.0f} kWh (davon Grund {base_actual_kwh:.0f})."
    )
    return {
        "estimated_kwh": round(estimated, 1),
        "forecast_kwh": round(fc_total, 1),
        "delta_vs_forecast_pct": round(delta_pct, 1),
        "pace_ratio": round(pace, 3),
        "actual_so_far_kwh": round(total_actual_kwh, 1),
        "expected_so_far_kwh": round(expected_so_far, 1),
        "base_pace_estimate_kwh": round(
            float(base_actual_kwh) + remaining_base * pace, 1
        ),
        "note": note,
    }


def _controllable_devices_summary(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Schaltbare Geräte (Steuerung): Summe kW + kurze Liste für UI-Hinweis."""
    if not config:
        return {"power_kw": 0.0, "count": 0, "labels": []}
    devices = enabled_devices(config)
    labels_map = device_label_map()
    labels: List[str] = []
    total_kw = 0.0
    for d in devices:
        pwr = float(d.get("power_kw") or 0)
        if pwr <= 0:
            continue
        total_kw += pwr
        key = str(d.get("ip") or d.get("entity_id") or "")
        name = (
            str(d.get("name") or d.get("label") or "").strip()
            or labels_map.get(key)
            or key
            or "Gerät"
        )
        labels.append(f"{name} {pwr:.1f} kW")
    return {
        "power_kw": round(total_kw, 2),
        "count": len(labels),
        "labels": labels[:8],
    }


def build_consumption_estimate_summary(
    snapshots: List[Dict[str, Any]],
    base_dir: str,
    now: Optional[datetime] = None,
    week_plan: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """7-Tage-Verbrauchsschätzung + Ist heute für Info-Kacheln (mit/ohne Miner)."""
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base_actual = actual_base_consumption_kwh(base_dir, midnight, now)
    total_actual = actual_total_consumption_kwh(base_dir, midnight, now)
    expected_base_so_far = expected_base_consumption_kwh(base_dir, midnight, now)
    horizon = len(snapshots) if snapshots else DEFAULT_HORIZON_DAYS
    plan_days = list((week_plan or {}).get("days") or [])

    base_vals: List[Optional[float]] = []
    total_vals: List[Optional[float]] = []
    for i, snap in enumerate(snapshots[:horizon]):
        daily = snap.get("consumption_daily") or {}
        try:
            base_kwh = float(daily.get("base_kwh") or 0)
        except (TypeError, ValueError):
            base_kwh = 0.0
        prod = snap.get("production_daily") or {}
        try:
            pv_kwh = float(prod.get("pv_kwh") or 0)
        except (TypeError, ValueError):
            pv_kwh = 0.0
        try:
            hydro_kwh = float(prod.get("hydro_kwh") or 0)
        except (TypeError, ValueError):
            hydro_kwh = 0.0
        planned = _planned_miner_kwh_from_week_day(plan_days[i] if i < len(plan_days) else None)
        if planned is None:
            raw_planned = daily.get("miner_planned_kwh")
            if raw_planned is not None:
                try:
                    planned = float(raw_planned)
                except (TypeError, ValueError):
                    planned = None
        if planned is None:
            # letzter Fallback: nicht 24h-Volllast, sondern 0 wenn unbekannt
            planned = 0.0
        raw_planned = float(planned)
        # Invariante: Miner nur aus Tagesüberschuss (PV+Hydro−Grundlast)
        planned = cap_planned_miner_to_day_surplus(
            raw_planned,
            pv_kwh=pv_kwh,
            hydro_kwh=hydro_kwh,
            base_kwh=base_kwh,
        )
        if raw_planned > planned + 0.5:
            _agent_debug_log(
                "H6",
                "energy_balance.py:build_consumption_estimate_summary",
                "capped inflated miner_planned to day surplus",
                {
                    "day_index": i,
                    "raw_planned_kwh": round(raw_planned, 2),
                    "capped_planned_kwh": planned,
                    "pv_kwh": round(pv_kwh, 2),
                    "hydro_kwh": round(hydro_kwh, 2),
                    "base_kwh": round(base_kwh, 2),
                    "day_surplus_kwh": round(
                        day_surplus_kwh(pv_kwh, hydro_kwh, base_kwh), 2
                    ),
                },
            )
        # Snapshot mit Wochenplan-Miner aktualisieren (für UI-Konsistenz)
        daily = dict(daily)
        daily["miner_planned_kwh"] = round(float(planned), 2)
        daily["miner_planned_kwh_raw"] = round(raw_planned, 2)
        daily["total_with_miners_kwh"] = round(base_kwh + float(planned), 2)
        snap["consumption_daily"] = daily
        base_vals.append(round(base_kwh, 2))
        total_vals.append(round(base_kwh + float(planned), 2))
    while len(base_vals) < 7:
        base_vals.append(None)
        total_vals.append(None)

    def _pack(vals: List[Optional[float]]) -> Dict[str, Any]:
        return {
            "today_kwh": vals[0],
            "tomorrow_kwh": vals[1],
            "day_after_kwh": vals[2],
            "day_3_kwh": vals[3],
            "day_4_kwh": vals[4],
            "day_5_kwh": vals[5],
            "day_6_kwh": vals[6],
            "by_day_kwh": vals[:horizon],
        }

    total = _pack(total_vals)
    total["actual_today_kwh"] = round(total_actual, 2)

    base = _pack(base_vals)
    base["actual_today_kwh"] = round(base_actual, 2)

    controllable = _controllable_devices_summary(config)
    pattern = _consumption_pattern_estimate(
        forecast_total_kwh=total.get("today_kwh"),
        forecast_base_kwh=base.get("today_kwh"),
        total_actual_kwh=total_actual,
        base_actual_kwh=base_actual,
        expected_base_so_far_kwh=expected_base_so_far,
        now=now,
    )

    gv_today = base.get("today_kwh")
    gv_note = (
        "Haus/Normalbetrieb ohne schaltbare Geräte (Miner/Flex). "
        f"Steuerung kann {controllable['count']} Geräte "
        f"({controllable['power_kw']:.1f} kW) schalten."
    )
    if controllable.get("labels"):
        gv_note += " " + ", ".join(controllable["labels"][:4])
        if controllable["count"] > 4:
            gv_note += f" u.a."

    grundverbrauch = {
        "today_kwh": gv_today,
        "actual_today_kwh": round(base_actual, 2),
        "expected_so_far_kwh": round(expected_base_so_far, 2),
        "controllable_power_kw": controllable["power_kw"],
        "controllable_count": controllable["count"],
        "note": gv_note,
    }

    # #region agent log
    snap0 = snapshots[0] if snapshots else {}
    day_bal = snap0.get("day_balance") or {}
    _agent_debug_log(
        "H1",
        "energy_balance.py:build_consumption_estimate_summary",
        "consumption card totals",
        {
            "ui_total_today_kwh": total.get("today_kwh"),
            "ui_base_today_kwh": base.get("today_kwh"),
            "week_plan_miner_kwh": _planned_miner_kwh_from_week_day(
                plan_days[0] if plan_days else None
            ),
            "snapshot_miner_planned_kwh": (snap0.get("consumption_daily") or {}).get(
                "miner_planned_kwh"
            ),
            "day_balance_consumption_kwh": day_bal.get("consumption_kwh"),
            "day_balance_production_kwh": day_bal.get("production_kwh"),
            "day_balance_ok": day_bal.get("balances_ok"),
        },
    )
    # #endregion

    return {
        "total_with_miners": total,
        "base_only": base,
        "pattern": pattern,
        "grundverbrauch": grundverbrauch,
        "actual_until": now.strftime("%H:%M"),
    }


def build_energy_budget_snapshot(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Live-Snapshot für UI (heute) — Abwärtskompatibilität zu energy_budget_snapshots[0]."""
    snapshots = build_multi_day_energy_snapshots(config, state, live, base_dir, now)
    if snapshots:
        return snapshots[0]
    now = now or datetime.now()
    try:
        from control_algorithm.pv_pattern_estimate import effective_today_pv_kwh

        today_pv = float(effective_today_pv_kwh(state))
    except Exception:
        today_pv = float(state.get("pv_yield_today_kwh") or 0)
    return _build_energy_budget_snapshot_for_day(
        config,
        state,
        live,
        base_dir,
        now,
        today_pv,
        expected_hydro_full_day_kwh(base_dir, now.date()),
        day_label="Heute",
        day_date=now.date(),
        is_forecast=False,
        include_so_far=True,
    )
