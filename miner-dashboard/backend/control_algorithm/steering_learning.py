# -*- coding: utf-8 -*-
"""
Adaptive Steuerungs-Lernzellen (keine echten Neural Nets).

Lernen aus:
  1) Speicher/Export: Einspeisung bei hohem SOC + zu wenig Verbraucher
  2) Wetterprognose: Ist-PV vs. Morgenprognose → frühere/spätere Last
  3) Gerätesteuerung: Parameter, die Export-Vermeidung und Vorentladen steuern

Abendkalibrierung passt die Parameter langsam an (EMA, harte Grenzen),
damit kleine Fehlermuster (wie „zu spät bei >97 %“) seltener werden.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# Grenzen – Regeln bleiben dominant, Lernen nur Feintuning
PARAM_BOUNDS = {
    "soc_preempt_margin": (2.0, 12.0),  # % unter max_soc: präventiv Verbraucher
    "export_threshold_kw": (0.03, 0.25),  # ab wann Live-Export zählt
    "headroom_extra_soc": (0.0, 8.0),  # zusätzliche Vorentlade-Reserve
    "forecast_underestimate_boost": (0.0, 0.45),  # 0–1: PV oft höher als Prognose
    # Höchste Priorität: länger laufen / nicht abschalten bei Export-Risiko
    "top_prio_extra_runtime_h": (0.0, 8.0),
    "top_prio_hold_below_max": (3.0, 15.0),  # %-Pkt unter Max: Top-Prio halten
    "score": (0.0, 100.0),
    # Internes Risiko-Management (Schaltbudget / Zielverfehlung)
    "switch_risk_extra_runtime_h": (0.0, 6.0),  # längere An-Bindung
    "switch_risk_extra_off_h": (0.0, 6.0),  # längere Aus-Bindung → weniger Zyklen
    "switch_risk_session_bias": (0.0, 1.0),  # 0–1: Planer bevorzugt längere Fenster
    # Tag/Nacht-Neuron: Absorptions-Budget eher tagsüber vs. nachts
    "day_night_shift_bias": (-1.0, 1.0),  # −1 = alles Tag, +1 = mehr Nacht
    "night_absorption_max_fraction": (0.0, 0.4),  # max. Anteil 24h-Budget nachts
    # Lehrneuron Prio-Balance (Spitzen-Export vs. Anker-Laufzeit)
    "peak_spill_boost": (0.0, 1.0),
    "anchor_conserve_bias": (0.0, 1.0),
    "peak_export_sensitivity": (0.5, 2.0),
    # Speicher-Band-Neuron: SOC im Mittelfeld (Floor … Decke)
    "band_max_target_pct": (82.0, 95.0),
    "band_min_cushion_pct": (0.0, 12.0),
    "band_weather_guard": (0.0, 1.0),
    "band_consumption_guard": (0.0, 1.0),
    "band_horizon_weight": (0.2, 1.0),
    # Lehrneuron SoC-Priorität: Checkpoint-sichere Geräteauswahl
    "soc_prio_margin_pct": (2.0, 8.0),
    "soc_prio_anchor_stretch": (0.0, 4.0),
    "soc_prio_flex_cut_bias": (0.0, 1.0),
    "soc_prio_forecast_trust": (0.3, 1.0),
    # Nachtreserve-Neuron
    "overnight_reserve_margin_pct": (1.0, 12.0),
    # Anker-Fenster-Neuron
    "anchor_window_safety_pct": (0.0, 15.0),
    "anchor_window_early_start_bias": (0.0, 1.0),
    "anchor_window_forecast_trust": (0.3, 1.0),
    # Morgen-Drain-Neuron (Laufreserve g)
    "drain_base_load_bias": (0.6, 1.6),
    "drain_night_risk_frac": (0.05, 0.60),
    "drain_floor_margin_kwh_pct": (0.0, 8.0),
    "drain_pv_trust": (0.5, 1.0),
    "drain_pv_onset_bias_min": (-60.0, 60.0),
    "drain_device_kw_bias": (0.7, 1.3),
}

DEFAULT_PARAMS: Dict[str, float] = {
    "soc_preempt_margin": 4.0,
    "export_threshold_kw": 0.05,
    "headroom_extra_soc": 1.0,
    "forecast_underestimate_boost": 0.0,
    # Nicht zu lang „festhalten“ – sonst Flackern gegen Budget/Nachtlogik
    "top_prio_extra_runtime_h": 1.0,
    "top_prio_hold_below_max": 6.0,
    "score": 55.0,
    "switch_risk_extra_runtime_h": 0.0,
    "switch_risk_extra_off_h": 0.0,
    "switch_risk_session_bias": 0.0,
    "day_night_shift_bias": -0.35,
    "night_absorption_max_fraction": 0.15,
    "peak_spill_boost": 0.0,
    "anchor_conserve_bias": 0.0,
    "peak_export_sensitivity": 1.0,
    "band_max_target_pct": 90.0,
    "band_min_cushion_pct": 2.0,
    "band_weather_guard": 0.35,
    "band_consumption_guard": 0.25,
    "band_horizon_weight": 0.65,
    "soc_prio_margin_pct": 5.0,
    "soc_prio_anchor_stretch": 0.0,
    "soc_prio_flex_cut_bias": 0.0,
    "soc_prio_forecast_trust": 0.85,
    "overnight_reserve_margin_pct": 4.0,
    "anchor_window_safety_pct": 3.0,
    "anchor_window_early_start_bias": 0.0,
    "anchor_window_forecast_trust": 0.85,
    "drain_base_load_bias": 1.0,
    "drain_night_risk_frac": 0.25,
    "drain_floor_margin_kwh_pct": 2.0,
    "drain_pv_trust": 0.8,
    "drain_pv_onset_bias_min": 0.0,
    "drain_device_kw_bias": 1.0,
}


def default_steering_learning() -> Dict[str, Any]:
    return {
        "params": dict(DEFAULT_PARAMS),
        "today": _empty_today(date.today().isoformat()),
        "history": [],
        "last_run": None,
        "last_note": None,
        "last_adjustments": [],
    }


def _empty_today(day: str) -> Dict[str, Any]:
    return {
        "date": day,
        "miss_ticks": 0,
        "export_miss_kw_sum": 0.0,
        "high_soc_ticks": 0,
        "good_absorb_ticks": 0,
        "last_miss_at": None,
        "min_soc_breach_ticks": 0,
        "min_soc_breach_while_load_ticks": 0,
        "near_min_soc_ticks": 0,
        "band_ok_ticks": 0,
        "last_min_breach_at": None,
        # Risiko-Neuron: Einschaltlimit blockiert + Zielverfehlung
        "on_limit_blocks": 0,
        "on_limit_export_kw_sum": 0.0,
        "on_limit_miss_ticks": 0,
        "last_on_limit_block_at": None,
        # Tag/Nacht-Neuron
        "morning_export_kwh": 0.0,
        "night_switch_count": 0,
        "night_switch_wasted": 0,
        "day_min_soc_breach": 0,
        # Lehrneuron Prio-Balance
        "peak_pv_ticks": 0,
        "peak_export_miss_ticks": 0,
        "peak_export_miss_kw_sum": 0.0,
        "peak_overparallel_ticks": 0,
        "peak_good_absorb_ticks": 0,
        # Speicher-Band-Neuron
        "band_low_ticks": 0,
        "band_high_ticks": 0,
        "band_over_max_ticks": 0,
        "band_in_target_ticks": 0,
        "band_export_high_ticks": 0,
        # Lehrneuron SoC-Priorität
        "soc_prio_ticks": 0,
        "soc_prio_miss_ticks": 0,
        "soc_prio_tight_ticks": 0,
        "soc_prio_anchor_cut_ticks": 0,
        "soc_prio_export_peak_ticks": 0,
        "soc_prio_headroom_sum": 0.0,
        # Nachtreserve
        "overnight_soc_stress_ticks": 0,
        "overnight_unexpected_load_ticks": 0,
        "last_overnight_stress_at": None,
        # Anker-Fenster
        "aw_window_ticks": 0,
        "aw_running_ticks": 0,
        "aw_break_ticks": 0,
        "aw_soc_violation_ticks": 0,
        "aw_headroom_sum": 0.0,
        "aw_headroom_samples": 0,
        # Morgen-Drain (Laufreserve g)
        "drain_ticks": 0,
        "drain_active_ticks": 0,
        "drain_undershoot_ticks": 0,
        "drain_no_start_ticks": 0,
        "drain_curtail_ticks": 0,
        "drain_arrival_soc_err_sum": 0.0,
        "drain_arrival_samples": 0,
        # Globaler Lern-Sperrschalter bei PV-Ausfall / Produktionsanomalie
        "pv_outage_learning_block": False,
        "pv_safety_ticks": 0,
        "pv_outage_block_reason": None,
    }


def ensure_steering_learning(state: Dict[str, Any]) -> Dict[str, Any]:
    sl = state.get("steering_learning")
    if not isinstance(sl, dict):
        sl = default_steering_learning()
        state["steering_learning"] = sl
    params = sl.get("params")
    if not isinstance(params, dict):
        params = dict(DEFAULT_PARAMS)
        sl["params"] = params
    for k, v in DEFAULT_PARAMS.items():
        if k not in params:
            params[k] = v
    today = date.today().isoformat()
    day = sl.get("today")
    if not isinstance(day, dict) or day.get("date") != today:
        sl["today"] = _empty_today(today)
    else:
        # Fehlende Keys nach Code-Update nachziehen
        for k, v in _empty_today(today).items():
            if k not in day:
                day[k] = v
    state["steering_learning"] = sl
    return sl


def reset_steering_learning(state: Dict[str, Any], *, force: bool = False) -> None:
    """
    Lerndaten zurücksetzen – standardmäßig verweigert.

    Nur mit force=True (manuell/Tests). Automatische Aufrufe dürfen Training
    nicht löschen.
    """
    if not force:
        # No-op: Config-Änderungen & Co. dürfen Training nicht vernichten.
        return
    state["steering_learning"] = default_steering_learning()


def mark_pv_outage_learning_block(
    state: Dict[str, Any],
    *,
    reason: str = "pv_outage",
) -> None:
    """Heute als PV-Ausfall markieren → Abend-Lernen sperren."""
    sl = ensure_steering_learning(state)
    today = sl["today"]
    today["pv_outage_learning_block"] = True
    today["pv_outage_block_reason"] = str(reason or "pv_outage")[:160]
    state["steering_learning"] = sl


def learning_blocked_by_pv_outage(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    Globaler Lern-Sperrschalter bei PV-Ausfall / Produktionsanomalie.

    True wenn PV-Sicherheit heute griff oder Ist-PV extrem unter der
    Morgenprognose liegt (z. B. Schnee) – damit Ausreißer-Tage Parameter
    nicht verdrehen.
    """
    if not config.get("has_pv", True):
        return False, ""
    if config.get("steering_learning_block_on_pv_outage", True) is False:
        return False, ""

    sl = ensure_steering_learning(state)
    today = sl.get("today") or {}
    if today.get("pv_outage_learning_block") or int(today.get("pv_safety_ticks") or 0) >= 1:
        reason = today.get("pv_outage_block_reason") or "pv_safety_heute"
        return True, f"PV-Ausfall-Lernsperre ({reason})"

    if state.get("pv_safety_active"):
        return True, "PV-Ausfall-Lernsperre (pv_safety_active)"

    triggered = state.get("pv_safety_triggered_at")
    if triggered:
        try:
            trig_day = str(triggered)[:10]
            if trig_day == date.today().isoformat():
                return True, "PV-Ausfall-Lernsperre (pv_safety heute ausgelöst)"
        except Exception:
            pass

    predicted = state.get("morning_pv_forecast_kwh")
    actual = state.get("live_pv_kwh_actual")
    try:
        pred_f = float(predicted) if predicted is not None else None
        act_f = float(actual) if actual is not None else None
    except (TypeError, ValueError):
        pred_f = act_f = None
    if pred_f is not None and act_f is not None and pred_f >= 5.0:
        ratio = act_f / pred_f if pred_f > 0 else 0.0
        # Wie bestehendes „pv_very_low“: ≤35 % der Prognose oder fast null bei erwartetem Tag
        if ratio <= 0.35:
            return True, (
                f"PV-Ausfall-Lernsperre (Ist {act_f:.1f} kWh = {ratio:.0%} der "
                f"Prognose {pred_f:.1f} kWh)"
            )
        if act_f < 8.0 and pred_f >= 15.0:
            return True, (
                f"PV-Ausfall-Lernsperre (Ist {act_f:.1f} kWh bei Prognose {pred_f:.1f} kWh)"
            )
    return False, ""


def get_steering_params(state: Dict[str, Any]) -> Dict[str, float]:
    sl = ensure_steering_learning(state)
    raw = sl.get("params") or {}
    out: Dict[str, float] = {}
    for key, default in DEFAULT_PARAMS.items():
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            val = default
        lo, hi = PARAM_BOUNDS[key]
        out[key] = max(lo, min(hi, val))
    return out


def _clamp_param(key: str, value: float) -> float:
    lo, hi = PARAM_BOUNDS[key]
    return round(max(lo, min(hi, float(value))), 4)


def export_threshold_kw(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["export_threshold_kw"])


def switch_risk_extra_runtime_h(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["switch_risk_extra_runtime_h"])


def switch_risk_extra_off_hours(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["switch_risk_extra_off_h"])


def switch_risk_session_bias(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["switch_risk_session_bias"])


def day_night_shift_bias(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["day_night_shift_bias"])


def night_absorption_max_fraction(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["night_absorption_max_fraction"])


def hour_day_weight(state: Dict[str, Any], hour: int) -> float:
    """
    Tageszeit-Gewicht für Stundenplanung (0.05–1.0).
    Nacht 22–06 standardmäßig niedrig; day_night_shift_bias verschiebt Richtung Nacht.
    """
    bias = day_night_shift_bias(state)
    night_base = 0.2 - 0.15 * bias  # bias +1 → ~0.35, bias -1 → ~0.05
    day_base = 1.0 - 0.25 * max(0.0, bias)
    is_night = hour >= 22 or hour < 6
    w = night_base if is_night else day_base
    return round(max(0.05, min(1.0, w)), 3)


def record_on_limit_block(
    state: Dict[str, Any],
    *,
    ip: str,
    export_kw: float = 0.0,
    now: Optional[datetime] = None,
) -> None:
    """Tick: Einschalten wegen Tageslimit verweigert → Risiko-Neuron speist."""
    sl = ensure_steering_learning(state)
    today = sl["today"]
    today["on_limit_blocks"] = int(today.get("on_limit_blocks") or 0) + 1
    today["on_limit_export_kw_sum"] = float(today.get("on_limit_export_kw_sum") or 0) + max(
        0.0, float(export_kw or 0)
    )
    today["last_on_limit_block_at"] = (now or datetime.now()).isoformat(timespec="seconds")
    # Sofort-Lernen bei Block + spürbarem Export (Zielverfehlung im Moment)
    if float(export_kw or 0) >= 0.5:
        today["on_limit_miss_ticks"] = int(today.get("on_limit_miss_ticks") or 0) + 1
        params = dict(sl.get("params") or {})
        # Länger an / länger aus → weniger Zyklen, bessere Planung
        try:
            rt = float(params.get("switch_risk_extra_runtime_h") or 0)
        except (TypeError, ValueError):
            rt = 0.0
        try:
            off = float(params.get("switch_risk_extra_off_h") or 0)
        except (TypeError, ValueError):
            off = 0.0
        try:
            bias = float(params.get("switch_risk_session_bias") or 0)
        except (TypeError, ValueError):
            bias = 0.0
        params["switch_risk_extra_runtime_h"] = _clamp_param(
            "switch_risk_extra_runtime_h", _ema(rt, rt + 0.35, 0.55)
        )
        params["switch_risk_extra_off_h"] = _clamp_param(
            "switch_risk_extra_off_h", _ema(off, off + 0.25, 0.5)
        )
        params["switch_risk_session_bias"] = _clamp_param(
            "switch_risk_session_bias", _ema(bias, min(1.0, bias + 0.08), 0.6)
        )
        # Früher einschalten lernen (weniger Not-Resume am Limit)
        try:
            margin = float(params.get("soc_preempt_margin") or DEFAULT_PARAMS["soc_preempt_margin"])
        except (TypeError, ValueError):
            margin = DEFAULT_PARAMS["soc_preempt_margin"]
        params["soc_preempt_margin"] = _clamp_param(
            "soc_preempt_margin", _ema(margin, margin + 0.4, 0.45)
        )
        sl["params"] = params
        sl["last_risk_note"] = (
            f"Risiko-Neuron: EIN-Limit blockiert {ip} bei Export {float(export_kw):.1f} kW "
            f"→ längere Sessions (runtime+{params['switch_risk_extra_runtime_h']:.1f} h, "
            f"off+{params['switch_risk_extra_off_h']:.1f} h, bias "
            f"{params['switch_risk_session_bias']:.2f})."
        )
    state["steering_learning"] = sl


def soc_preempt_level(config: Dict[str, Any], state: Dict[str, Any]) -> float:
    """SOC ab dem präventiv Last eingeschaltet wird (unter max_soc)."""
    max_soc = float(config.get("max_soc_percent") or 95.0)
    p = get_steering_params(state)
    margin = p["soc_preempt_margin"] + 0.5 * p["headroom_extra_soc"]
    # Bei systematisch unterschätzter PV früher eingreifen
    margin += 4.0 * p["forecast_underestimate_boost"]
    return max_soc - margin


def headroom_extra_soc(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["headroom_extra_soc"])


def top_prio_extra_runtime_h(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state)["top_prio_extra_runtime_h"])


def is_highest_priority_device(
    device: Dict[str, Any],
    devices: List[Dict[str, Any]],
) -> bool:
    """True wenn Gerät die höchste Prioritätsstufe unter enabled devices hat."""
    from control_algorithm.config import normalize_device_priority

    if not devices:
        return False
    top = max(normalize_device_priority(d.get("priority")) for d in devices)
    return normalize_device_priority(device.get("priority")) >= top - 0.05


def should_hold_top_priority(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    device: Dict[str, Any],
    devices: List[Dict[str, Any]],
) -> bool:
    """
    Höchste Priorität nicht abschalten, wenn Speicher nahe Max / Export-Risiko
    und Min-SOC sicher – Lektion: früher an / länger laufen bei Volllast-Tagen.
    """
    if not is_highest_priority_device(device, devices):
        return False
    soc = live.get("soc_percent")
    if soc is None:
        return False
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    if float(soc) < min_soc + 5.0:
        return False
    p = get_steering_params(state)
    hold_from = max_soc - float(p["top_prio_hold_below_max"])
    pv_kw = float(live.get("pv_kw") or 0)
    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        export_kw = live_grid_export_kw(live)
    except Exception:
        export_kw = 0.0
    # Halten wenn nahe Max-SOC und (PV oder Einspeisung)
    if float(soc) >= hold_from and (pv_kw >= 0.3 or export_kw >= export_threshold_kw(state)):
        return True
    # Auch halten wenn Export-Vermeidung/Preempt aktiv wäre
    if float(soc) >= soc_preempt_level(config, state) and pv_kw >= 0.3:
        return True
    # Gutwetter-Durchlauf: Top-Prio nicht wegen Budget-0 abschalten solange PV trägt
    try:
        from control_algorithm.reference_anchor import effective_perfect_day_kwh

        perfect = float(effective_perfect_day_kwh(state, config) or 0)
    except Exception:
        perfect = float(config.get("pv_kwp") or 0) * float(
            config.get("perfect_weather_sun_hours") or 6
        )
    try:
        from control_algorithm.pv_pattern_estimate import effective_today_pv_kwh

        today_pv = float(effective_today_pv_kwh(state) or 0)
    except Exception:
        today_pv = float(state.get("pv_yield_today_kwh") or 0)
    pv_kwp = float(config.get("pv_kwp") or 0)
    strong_day = today_pv >= max(40.0, perfect * 0.65) or (
        pv_kw >= max(4.0, pv_kwp * 0.3) and today_pv >= max(25.0, perfect * 0.4)
    )
    if strong_day and float(soc) >= min_soc + 8.0 and pv_kw >= 2.0:
        return True
    try:
        from control_algorithm.soc_priority_neuron import anchor_hold_allowed_by_neuron

        if anchor_hold_allowed_by_neuron(config, state, device, devices):
            return True
    except Exception:
        pass
    return False


def effective_min_runtime_hours(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]],
    device: Optional[Dict[str, Any]],
    devices: Optional[List[Dict[str, Any]]] = None,
) -> float:
    """Mindestlaufzeit inkl. gelernter Extra-Zeit für höchste Priorität."""
    try:
        from control_algorithm.load_scheduler import normalize_min_runtime_hours
    except Exception:
        def normalize_min_runtime_hours(raw: Any) -> float:
            try:
                return max(0.5, min(24.0, float(raw if raw is not None else 3.0)))
            except (TypeError, ValueError):
                return 3.0

    base = normalize_min_runtime_hours(config.get("min_device_runtime_hours"))
    if not device or not live:
        return base
    devs = devices
    if devs is None:
        try:
            from control_algorithm.planner import enabled_devices

            devs = enabled_devices(config)
        except Exception:
            return base
    if not is_highest_priority_device(device, devs):
        return base
    extra = top_prio_extra_runtime_h(state)
    # Extra nur wenn Speicher/Export-Risiko (sonst normale Mindestlaufzeit)
    soc = live.get("soc_percent")
    max_soc = float(config.get("max_soc_percent") or 95.0)
    pv_kw = float(live.get("pv_kw") or 0)
    if soc is not None and float(soc) >= max_soc - 15 and pv_kw >= 0.2:
        return min(24.0, base + extra)
    if soc is not None and float(soc) >= soc_preempt_level(config, state):
        return min(24.0, base + extra)
    return base


def record_tick_observation(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> None:
    """Jeden Daemon-Tick: Miss/Absorb/Min-SoC für den heutigen Tag zählen."""
    sl = ensure_steering_learning(state)
    today = sl["today"]
    soc = live.get("soc_percent")
    if soc is None:
        return
    try:
        from control_algorithm.weather_survival import live_grid_export_kw
        from control_algorithm.planner import enabled_devices, miner_load_kw
    except Exception:
        return

    export_kw = live_grid_export_kw(live)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    high_bar = max_soc - 3.0
    soc_f = float(soc)
    devices = enabled_devices(config)
    miner_kw = miner_load_kw(devices, live.get("miner_status") or {})
    thr = export_threshold_kw(state)

    if soc_f >= high_bar:
        today["high_soc_ticks"] = int(today.get("high_soc_ticks") or 0) + 1

    # PV-Sicherheitsabschaltung aktiv → heutiges Lernen abends sperren
    if state.get("pv_safety_active"):
        today["pv_outage_learning_block"] = True
        today["pv_safety_ticks"] = int(today.get("pv_safety_ticks") or 0) + 1
        if not today.get("pv_outage_block_reason"):
            today["pv_outage_block_reason"] = "pv_safety_active"

    # Miss: hoher SOC + spürbare Einspeisung (Ziel: bei vollem Speicher ~0 Export),
    # unabhängig davon ob schon teilweise Last läuft – zu spät / zu wenig zählt.
    if soc_f >= high_bar and export_kw >= max(thr, 0.15):
        today["miss_ticks"] = int(today.get("miss_ticks") or 0) + 1
        today["export_miss_kw_sum"] = float(today.get("export_miss_kw_sum") or 0) + export_kw
        today["last_miss_at"] = datetime.now().isoformat(timespec="seconds")
    elif export_kw < thr and miner_kw >= 1.0 and soc_f >= high_bar - 5:
        today["good_absorb_ticks"] = int(today.get("good_absorb_ticks") or 0) + 1

    # Min-SoC: Unterschreitung zählen (vermeidbar wenn noch Last lief)
    if soc_f < min_soc:
        today["min_soc_breach_ticks"] = int(today.get("min_soc_breach_ticks") or 0) + 1
        today["last_min_breach_at"] = datetime.now().isoformat(timespec="seconds")
        if miner_kw >= 0.3:
            today["min_soc_breach_while_load_ticks"] = (
                int(today.get("min_soc_breach_while_load_ticks") or 0) + 1
            )
    elif soc_f < min_soc + 5.0:
        today["near_min_soc_ticks"] = int(today.get("near_min_soc_ticks") or 0) + 1
    elif min_soc + 5.0 <= soc_f <= max_soc - 2.0:
        today["band_ok_ticks"] = int(today.get("band_ok_ticks") or 0) + 1

    # Tag/Nacht-Neuron: Morgen-Export und Min-SoC tagsüber
    h = datetime.now().hour
    if 6 <= h < 10 and export_kw >= max(thr, 0.15):
        today["morning_export_kwh"] = float(today.get("morning_export_kwh") or 0) + export_kw / 6.0
    if 6 <= h < 22 and soc_f < min_soc:
        today["day_min_soc_breach"] = int(today.get("day_min_soc_breach") or 0) + 1


def record_night_device_resume(state: Dict[str, Any], *, ip: str) -> None:
    """Resume nachts (22–06) gezählt – für Tag/Nacht-Lernen."""
    h = datetime.now().hour
    if 6 <= h < 22:
        return
    sl = ensure_steering_learning(state)
    today = sl["today"]
    today["night_switch_count"] = int(today.get("night_switch_count") or 0) + 1
    state["steering_learning"] = sl


def record_night_switch_wasted(state: Dict[str, Any]) -> None:
    """Nacht-EIN brachte nichts (Export tagsüber trotzdem) – Abendauswertung."""
    sl = ensure_steering_learning(state)
    today = sl["today"]
    today["night_switch_wasted"] = int(today.get("night_switch_wasted") or 0) + 1
    state["steering_learning"] = sl


def analyze_timeseries_soc_band(
    base_dir: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Tageszeitreihe: Minuten unter Min-SoC / über Max-SoC."""
    max_soc = float(config.get("max_soc_percent") or 95.0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    try:
        from energy_timeseries_24h import load_timeseries_state
    except Exception:
        return {
            "min_breach_minutes": 0,
            "max_over_minutes": 0,
            "band_ok_minutes": 0,
            "min_soc_floor": min_soc,
            "max_soc_ceil": max_soc,
        }

    samples = (load_timeseries_state(base_dir).get("samples") or [])
    min_breach = 0
    max_over = 0
    band_ok = 0
    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            soc = s.get("soc")
            if soc is None:
                continue
            soc_f = float(soc)
        except (TypeError, ValueError):
            continue
        if soc_f < min_soc:
            min_breach += 1
        elif soc_f > max_soc:
            max_over += 1
        elif min_soc + 2.0 <= soc_f <= max_soc - 1.0:
            band_ok += 1

    return {
        "min_breach_minutes": min_breach,
        "max_over_minutes": max_over,
        "band_ok_minutes": band_ok,
        "min_soc_floor": min_soc,
        "max_soc_ceil": max_soc,
    }


def analyze_timeseries_export_misses(
    base_dir: str,
    config: Dict[str, Any],
    *,
    min_export_kw: float = 0.2,
) -> Dict[str, Any]:
    """Tageszeitreihe: Minuten mit Export bei (nahezu) vollem Speicher ohne Last."""
    max_soc = float(config.get("max_soc_percent") or 95.0)
    high_soc = max_soc - 3.0
    try:
        from energy_timeseries_24h import load_timeseries_state
    except Exception:
        return {
            "miss_minutes": 0,
            "miss_export_kwh": 0.0,
            "high_soc_minutes": 0,
            "peak_export_kw": 0.0,
        }

    samples = (load_timeseries_state(base_dir).get("samples") or [])
    miss_minutes = 0
    miss_export_kwh = 0.0
    high_soc_minutes = 0
    peak_export = 0.0

    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            soc = s.get("soc")
            netz = s.get("netz")
            if soc is None or netz is None:
                continue
            soc_f = float(soc)
            export = max(0.0, -float(netz))
        except (TypeError, ValueError):
            continue
        peak_export = max(peak_export, export)
        if soc_f < high_soc:
            continue
        high_soc_minutes += 1
        # Bei (nahezu) vollem Speicher zählt jede spürbare Einspeisung als Miss –
        # auch wenn schon teilweise Verbraucher laufen (zu spät / zu wenig).
        if export >= min_export_kw:
            miss_minutes += 1
            miss_export_kwh += export / 60.0

    return {
        "miss_minutes": miss_minutes,
        "miss_export_kwh": round(miss_export_kwh, 2),
        "high_soc_minutes": high_soc_minutes,
        "peak_export_kw": round(peak_export, 2),
    }


def _ema(old: float, target: float, alpha: float) -> float:
    return old * (1.0 - alpha) + target * alpha


def temper_aggressive_learning_for_charge(
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
) -> None:
    """
    Wenn Speicher klar unter dem nächsten Hoch-Ziel liegt: überagressive
    Hold/Extra-Laufzeit dämpfen (Vorparametrisierung gegen Schaltchaos).
    """
    soc = live.get("soc_percent")
    if soc is None:
        return
    targets = config.get("soc_targets") or []
    high_targets = []
    for t in targets:
        try:
            high_targets.append(float(t.get("soc_percent") or 0))
        except (TypeError, ValueError):
            continue
    if not high_targets:
        return
    evening_target = max(high_targets)
    if float(soc) >= evening_target - 15:
        return
    sl = ensure_steering_learning(state)
    params = dict(sl.get("params") or {})
    changed = False
    # Gelerntes „+4 h festhalten“ bei niedrigem SOC kontraproduktiv
    try:
        extra = float(params.get("top_prio_extra_runtime_h") or 0)
    except (TypeError, ValueError):
        extra = 0.0
    if extra > 1.5:
        params["top_prio_extra_runtime_h"] = _clamp_param(
            "top_prio_extra_runtime_h",
            _ema(extra, 1.0, 0.55),
        )
        changed = True
    try:
        hold = float(params.get("top_prio_hold_below_max") or 5)
    except (TypeError, ValueError):
        hold = 5.0
    if hold > 7.0:
        params["top_prio_hold_below_max"] = _clamp_param(
            "top_prio_hold_below_max",
            _ema(hold, 6.0, 0.45),
        )
        changed = True
    if changed:
        sl["params"] = params
        note = (
            f"Schalt-Neuron gedämpft (SOC {float(soc):.0f}% ≪ Ziel {evening_target:.0f}%): "
            f"Extra-Lauf {params.get('top_prio_extra_runtime_h')} h, "
            f"Hold {params.get('top_prio_hold_below_max')} %-Pkt."
        )
        sl["last_temper_note"] = note
        state["steering_learning"] = sl


def run_evening_steering_learning(
    state: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: str,
) -> Dict[str, Any]:
    """
    Abend-Lernschritt: Zeitreihe + Tick-Misses + PV-Prognosefehler → Parameter.
    """
    sl = ensure_steering_learning(state)
    blocked, block_reason = learning_blocked_by_pv_outage(config, state)
    if blocked:
        note = (
            f"übersprungen: {block_reason} – Export-/Band-Parameter unverändert "
            f"(kein Lernen an PV-Ausfall-Tagen)."
        )
        sl["last_run"] = datetime.now().isoformat(timespec="seconds")
        sl["last_note"] = "skipped_pv_outage"
        adjustments: List[str] = [note]
        # Sicherheits-Neuronen (Laufreserve, Nachtreserve) dürfen trotzdem lernen –
        # sonst bleibt das Drain-Neuron auf Werkseinstellung, obwohl der Floor
        # gerissen wurde.
        params = get_steering_params(state)
        analysis = {"pv_forecast_error_pct": 0.0}
        try:
            from control_algorithm.morning_drain import run_morning_drain_evening_learning

            drain_adj = run_morning_drain_evening_learning(
                state, config, analysis, params
            )
            adjustments.extend(drain_adj)
        except Exception as e:
            adjustments.append(f"Morgen-Drain-Lernen Fehler: {e}")
        overnight_stress = int((sl.get("today") or {}).get("overnight_soc_stress_ticks") or 0)
        overnight_unexpected = int(
            (sl.get("today") or {}).get("overnight_unexpected_load_ticks") or 0
        )
        if overnight_stress >= 2 or overnight_unexpected >= 2:
            params["overnight_reserve_margin_pct"] = _clamp_param(
                "overnight_reserve_margin_pct",
                _ema(
                    float(params.get("overnight_reserve_margin_pct") or 4.0),
                    float(params.get("overnight_reserve_margin_pct") or 4.0) + 1.5,
                    0.55,
                ),
            )
            adjustments.append(
                f"Nachtreserve trotz PV-Sperre: Stress {overnight_stress}/"
                f"{overnight_unexpected} → Margin "
                f"{params['overnight_reserve_margin_pct']:.1f} %."
            )
        sl["params"] = params
        sl["last_adjustments"] = adjustments
        history = list(sl.get("history") or [])
        history.append(
            {
                "date": date.today().isoformat(),
                "skipped": True,
                "skip_reason": block_reason,
                "params": dict(params),
                "adjustments": adjustments,
            }
        )
        sl["history"] = history[-90:]
        state["steering_learning"] = sl
        state["steering_learning_skip_reason"] = block_reason
        return state

    params = get_steering_params(state)
    adjustments: List[str] = []
    analysis = analyze_timeseries_export_misses(base_dir, config)
    band = analyze_timeseries_soc_band(base_dir, config)
    analysis = {**analysis, **band}
    today_obs = sl.get("today") or {}

    miss_kwh = float(analysis.get("miss_export_kwh") or 0)
    miss_min = int(analysis.get("miss_minutes") or 0)
    # Tick-Misses grob in kWh (Intervall ~3 min → /20)
    tick_miss = float(today_obs.get("export_miss_kw_sum") or 0) / 20.0
    effective_miss_kwh = max(miss_kwh, tick_miss)

    min_breach_min = int(analysis.get("min_breach_minutes") or 0)
    max_over_min = int(analysis.get("max_over_minutes") or 0)
    tick_min_breach = int(today_obs.get("min_soc_breach_ticks") or 0)
    tick_min_while_load = int(today_obs.get("min_soc_breach_while_load_ticks") or 0)
    # ~3-Min-Ticks → grob Minuten
    effective_min_breach_min = max(min_breach_min, tick_min_breach * 3)
    avoidable_min_breach = tick_min_while_load >= 2 or (
        effective_min_breach_min >= 10 and tick_min_while_load >= 1
    )

    predicted = state.get("morning_pv_forecast_kwh")
    actual = state.get("live_pv_kwh_actual")
    try:
        from control_algorithm.live_data import get_actual_pv_kwh_today

        if actual is None:
            actual = get_actual_pv_kwh_today(base_dir)
    except Exception:
        pass

    forecast_ratio: Optional[float] = None
    if predicted and actual and float(predicted) >= 5 and float(actual) >= 5:
        forecast_ratio = float(actual) / float(predicted)

    # --- Min-SoC: vermeidbar bestrafen, unvermeidbar (Schnee/kein Last) neutral ---
    if effective_min_breach_min >= 5:
        pv_very_low = (
            forecast_ratio is not None and forecast_ratio <= 0.35
        ) or (actual is not None and float(actual) < 8.0)
        devices_were_off = tick_min_while_load == 0
        unavoidable = devices_were_off and (pv_very_low or effective_min_breach_min >= 5)
        if avoidable_min_breach and not (devices_were_off and pv_very_low):
            params["score"] = _clamp_param("score", params["score"] - 15)
            params["top_prio_extra_runtime_h"] = _clamp_param(
                "top_prio_extra_runtime_h",
                _ema(params["top_prio_extra_runtime_h"], 0.0, 0.45),
            )
            params["forecast_underestimate_boost"] = _clamp_param(
                "forecast_underestimate_boost",
                _ema(params["forecast_underestimate_boost"], 0.0, 0.4),
            )
            params["headroom_extra_soc"] = _clamp_param(
                "headroom_extra_soc",
                _ema(params["headroom_extra_soc"], 0.0, 0.35),
            )
            adjustments.append(
                f"Min-SoC unterschritten (~{effective_min_breach_min} min) während Last lief "
                f"→ Score −15, weniger aggressive Dauerlast "
                f"(vermeidbar)."
            )
        elif unavoidable or devices_were_off:
            adjustments.append(
                f"Min-SoC unterschritten (~{effective_min_breach_min} min), aber Geräte waren aus "
                f"/ PV extrem schwach – unvermeidbar, Neuron unverändert."
            )
        else:
            params["score"] = _clamp_param("score", params["score"] - 6)
            adjustments.append(
                f"Min-SoC kurz unterschritten (~{effective_min_breach_min} min) → Score −6."
            )

    # --- Speicher/Export-Miss: höchste Priorität früher an + länger laufen ---
    if effective_miss_kwh >= 8.0 or miss_min >= 45:
        params["soc_preempt_margin"] = _clamp_param(
            "soc_preempt_margin",
            _ema(params["soc_preempt_margin"], params["soc_preempt_margin"] + 3.0, 0.65),
        )
        params["headroom_extra_soc"] = _clamp_param(
            "headroom_extra_soc",
            _ema(params["headroom_extra_soc"], params["headroom_extra_soc"] + 2.0, 0.55),
        )
        params["export_threshold_kw"] = _clamp_param(
            "export_threshold_kw",
            _ema(params["export_threshold_kw"], params["export_threshold_kw"] * 0.7, 0.5),
        )
        params["top_prio_extra_runtime_h"] = _clamp_param(
            "top_prio_extra_runtime_h",
            _ema(params["top_prio_extra_runtime_h"], params["top_prio_extra_runtime_h"] + 3.0, 0.6),
        )
        params["top_prio_hold_below_max"] = _clamp_param(
            "top_prio_hold_below_max",
            _ema(params["top_prio_hold_below_max"], params["top_prio_hold_below_max"] + 3.0, 0.55),
        )
        params["score"] = _clamp_param("score", params["score"] - 12)
        # Sehr starker Miss (wie heute ~14 kWh): Mindest-Lektion für Priorität 1
        if effective_miss_kwh >= 12.0 or miss_min >= 120:
            params["soc_preempt_margin"] = _clamp_param(
                "soc_preempt_margin",
                max(params["soc_preempt_margin"], 7.0),
            )
            params["top_prio_extra_runtime_h"] = _clamp_param(
                "top_prio_extra_runtime_h",
                max(params["top_prio_extra_runtime_h"], 4.0),
            )
            params["top_prio_hold_below_max"] = _clamp_param(
                "top_prio_hold_below_max",
                max(params["top_prio_hold_below_max"], 8.0),
            )
            params["headroom_extra_soc"] = _clamp_param(
                "headroom_extra_soc",
                max(params["headroom_extra_soc"], 2.5),
            )
        adjustments.append(
            f"Export-Miss stark (~{effective_miss_kwh:.1f} kWh / {miss_min} min): "
            f"Priorität 1 früher starten (Preempt {params['soc_preempt_margin']:.1f} %-Pkt) "
            f"und länger laufen (+{params['top_prio_extra_runtime_h']:.1f} h / Hold "
            f"{params['top_prio_hold_below_max']:.1f} %-Pkt unter Max)."
        )
    elif effective_miss_kwh >= 2.0 or miss_min >= 15:
        params["soc_preempt_margin"] = _clamp_param(
            "soc_preempt_margin",
            _ema(params["soc_preempt_margin"], params["soc_preempt_margin"] + 1.5, 0.5),
        )
        params["headroom_extra_soc"] = _clamp_param(
            "headroom_extra_soc",
            _ema(params["headroom_extra_soc"], params["headroom_extra_soc"] + 1.0, 0.45),
        )
        params["export_threshold_kw"] = _clamp_param(
            "export_threshold_kw",
            _ema(params["export_threshold_kw"], params["export_threshold_kw"] * 0.85, 0.4),
        )
        params["top_prio_extra_runtime_h"] = _clamp_param(
            "top_prio_extra_runtime_h",
            _ema(params["top_prio_extra_runtime_h"], params["top_prio_extra_runtime_h"] + 1.5, 0.5),
        )
        params["top_prio_hold_below_max"] = _clamp_param(
            "top_prio_hold_below_max",
            _ema(params["top_prio_hold_below_max"], params["top_prio_hold_below_max"] + 1.5, 0.45),
        )
        params["score"] = _clamp_param("score", params["score"] - 6)
        adjustments.append(
            f"Export-Miss (~{effective_miss_kwh:.1f} kWh): Priorität 1 früher/länger "
            f"(Preempt {params['soc_preempt_margin']:.1f} %, +{params['top_prio_extra_runtime_h']:.1f} h)."
        )
    elif effective_miss_kwh < 0.4 and miss_min < 5:
        # Guter Tag bzgl. Export: belohnen wenn Band gehalten
        band_ok = int(analysis.get("band_ok_minutes") or 0)
        tick_band = int(today_obs.get("good_absorb_ticks") or 0) + int(
            today_obs.get("band_ok_ticks") or 0
        )
        if effective_min_breach_min < 3 and max_over_min < 15:
            params["score"] = _clamp_param("score", params["score"] + 4)
            if band_ok >= 60 or tick_band >= 20:
                for key, default in DEFAULT_PARAMS.items():
                    if key == "score":
                        continue
                    params[key] = _clamp_param(key, _ema(params[key], default, 0.06))
                adjustments.append(
                    "SOC-Band gehalten (kein Export-Miss, kein Min-Bruch) – "
                    f"Score +4, Parameter leicht entspannt."
                )
            elif int(analysis.get("high_soc_minutes") or 0) >= 30:
                for key, default in DEFAULT_PARAMS.items():
                    if key == "score":
                        continue
                    params[key] = _clamp_param(key, _ema(params[key], default, 0.08))
                params["score"] = _clamp_param("score", params["score"] + 1)
                adjustments.append(
                    "Kaum Export bei hohem SOC – Parameter leicht Richtung Standard entspannt."
                )
        elif max_over_min >= 30 and effective_miss_kwh < 0.4:
            # Über Max ohne großen Export-Miss: trotzdem früher einschalten lernen
            params["soc_preempt_margin"] = _clamp_param(
                "soc_preempt_margin",
                _ema(params["soc_preempt_margin"], params["soc_preempt_margin"] + 1.0, 0.4),
            )
            params["score"] = _clamp_param("score", params["score"] - 3)
            adjustments.append(
                f"Lange über Max-SoC (~{max_over_min} min) → Preempt früher, Score −3."
            )

    # --- Wetterprognose: systematische Unter-/Überschätzung ---
    if forecast_ratio is not None:
        if forecast_ratio >= 1.15:
            params["forecast_underestimate_boost"] = _clamp_param(
                "forecast_underestimate_boost",
                _ema(
                    params["forecast_underestimate_boost"],
                    params["forecast_underestimate_boost"] + 0.08,
                    0.5,
                ),
            )
            # Mehr PV als gedacht → früher Platz schaffen
            params["headroom_extra_soc"] = _clamp_param(
                "headroom_extra_soc",
                params["headroom_extra_soc"] + 0.4,
            )
            adjustments.append(
                f"PV-Ist {forecast_ratio:.0%} der Prognose → "
                f"Vorentladen früher (Boost {params['forecast_underestimate_boost']:.2f})."
            )
        elif forecast_ratio <= 0.85:
            params["forecast_underestimate_boost"] = _clamp_param(
                "forecast_underestimate_boost",
                _ema(params["forecast_underestimate_boost"], 0.0, 0.35),
            )
            adjustments.append(
                f"PV-Ist nur {forecast_ratio:.0%} der Prognose → weniger aggressives Vorentladen."
            )

    # --- Risiko-Neuron: EIN-Limit + Zielverfehlung (Export/Max-SoC) ---
    on_blocks = int(today_obs.get("on_limit_blocks") or 0)
    on_miss = int(today_obs.get("on_limit_miss_ticks") or 0)
    on_export_sum = float(today_obs.get("on_limit_export_kw_sum") or 0)
    if on_blocks >= 3 and (on_miss >= 2 or effective_miss_kwh >= 2.0 or max_over_min >= 20):
        params["switch_risk_extra_runtime_h"] = _clamp_param(
            "switch_risk_extra_runtime_h",
            _ema(
                params["switch_risk_extra_runtime_h"],
                params["switch_risk_extra_runtime_h"] + 1.5,
                0.6,
            ),
        )
        params["switch_risk_extra_off_h"] = _clamp_param(
            "switch_risk_extra_off_h",
            _ema(
                params["switch_risk_extra_off_h"],
                params["switch_risk_extra_off_h"] + 1.0,
                0.55,
            ),
        )
        params["switch_risk_session_bias"] = _clamp_param(
            "switch_risk_session_bias",
            _ema(params["switch_risk_session_bias"], 0.85, 0.55),
        )
        params["soc_preempt_margin"] = _clamp_param(
            "soc_preempt_margin",
            _ema(params["soc_preempt_margin"], params["soc_preempt_margin"] + 1.5, 0.5),
        )
        params["score"] = _clamp_param("score", params["score"] - 8)
        adjustments.append(
            f"Risiko-Neuron: {on_blocks}× EIN-Limit-Sperre "
            f"(davon {on_miss} bei Export, Σ{on_export_sum:.1f} kW) + Zielverfehlung "
            f"→ längere/weniger Sessions "
            f"(+{params['switch_risk_extra_runtime_h']:.1f} h An, "
            f"+{params['switch_risk_extra_off_h']:.1f} h Aus, "
            f"Plan-Bias {params['switch_risk_session_bias']:.2f})."
        )
    elif on_blocks >= 1 and on_miss == 0 and effective_miss_kwh < 0.5:
        # Limit greift, Ziel trotzdem ok → leicht entspannen
        params["switch_risk_session_bias"] = _clamp_param(
            "switch_risk_session_bias",
            _ema(params["switch_risk_session_bias"], 0.0, 0.08),
        )
        adjustments.append(
            f"Risiko-Neuron: {on_blocks}× EIN-Limit ohne Zielverfehlung – Bias leicht entspannt."
        )

    # --- Tag/Nacht-Neuron: Absorptions-Timing ---
    morning_exp = float(today_obs.get("morning_export_kwh") or 0)
    night_sw = int(today_obs.get("night_switch_count") or 0)
    night_wasted = int(today_obs.get("night_switch_wasted") or 0)
    day_min = int(today_obs.get("day_min_soc_breach") or 0)
    if morning_exp >= 2.0 and night_sw >= 1:
        params["night_absorption_max_fraction"] = _clamp_param(
            "night_absorption_max_fraction",
            _ema(params["night_absorption_max_fraction"], params["night_absorption_max_fraction"] * 0.65, 0.55),
        )
        params["day_night_shift_bias"] = _clamp_param(
            "day_night_shift_bias",
            _ema(params["day_night_shift_bias"], params["day_night_shift_bias"] - 0.2, 0.5),
        )
        adjustments.append(
            f"Tag/Nacht-Neuron: Morgen-Export ~{morning_exp:.1f} kWh nach {night_sw} Nacht-EIN "
            f"→ weniger Nacht-Anteil ({params['night_absorption_max_fraction']:.2f}), "
            f"Bias {params['day_night_shift_bias']:.2f}."
        )
    elif morning_exp >= 3.0 and night_sw == 0:
        params["night_absorption_max_fraction"] = _clamp_param(
            "night_absorption_max_fraction",
            _ema(params["night_absorption_max_fraction"], params["night_absorption_max_fraction"] + 0.04, 0.35),
        )
        params["day_night_shift_bias"] = _clamp_param(
            "day_night_shift_bias",
            _ema(params["day_night_shift_bias"], params["day_night_shift_bias"] + 0.08, 0.35),
        )
        adjustments.append(
            f"Tag/Nacht-Neuron: Morgen-Export ohne Nacht-Last → leicht mehr Nacht erlaubt "
            f"(Frac {params['night_absorption_max_fraction']:.2f})."
        )
    if day_min >= 2 or (avoidable_min_breach and night_sw >= 2):
        params["night_absorption_max_fraction"] = _clamp_param(
            "night_absorption_max_fraction",
            _ema(params["night_absorption_max_fraction"], params["night_absorption_max_fraction"] * 0.7, 0.5),
        )
        params["day_night_shift_bias"] = _clamp_param(
            "day_night_shift_bias",
            _ema(params["day_night_shift_bias"], -0.5, 0.45),
        )
        adjustments.append(
            f"Tag/Nacht-Neuron: Min-SoC tagsüber ({day_min} Ticks) / aggressive Nacht "
            f"→ Budget stärker in den Tag verschoben."
        )
    if night_wasted >= 1:
        params["night_absorption_max_fraction"] = _clamp_param(
            "night_absorption_max_fraction",
            _ema(params["night_absorption_max_fraction"], 0.08, 0.5),
        )
        adjustments.append(
            f"Tag/Nacht-Neuron: {night_wasted}× nutzloser Nacht-EIN → Nacht-Anteil reduziert."
        )

    # --- Lehrneuron Prio-Balance (Spitzen-Export vs. Anker-Laufzeit) ---
    try:
        from control_algorithm.peak_balance_learning import run_peak_balance_evening_learning

        lehr_adj = run_peak_balance_evening_learning(state, config, analysis, params)
        adjustments.extend(lehr_adj)
    except Exception:
        pass

    # --- Speicher-Band-Neuron (SOC-Mittelfeld / Horizont) ---
    try:
        from control_algorithm.storage_band_learning import run_storage_band_evening_learning

        band_adj = run_storage_band_evening_learning(state, config, analysis, params)
        adjustments.extend(band_adj)
    except Exception:
        pass

    # --- Lehrneuron SoC-Priorität ---
    try:
        from control_algorithm.soc_priority_neuron import run_soc_priority_evening_learning

        soc_prio_adj = run_soc_priority_evening_learning(state, config, analysis, params)
        adjustments.extend(soc_prio_adj)
    except Exception:
        pass

    # --- Morgen-Drain-Neuron (Laufreserve g) ---
    try:
        from control_algorithm.morning_drain import run_morning_drain_evening_learning

        drain_adj = run_morning_drain_evening_learning(state, config, analysis, params)
        adjustments.extend(drain_adj)
    except Exception as e:
        adjustments.append(f"Morgen-Drain-Lernen Fehler: {e}")

    # --- Nachtreserve: Margin nach Stress erhöhen ---
    overnight_stress = int(today_obs.get("overnight_soc_stress_ticks") or 0)
    overnight_unexpected = int(today_obs.get("overnight_unexpected_load_ticks") or 0)
    if overnight_stress >= 2 or overnight_unexpected >= 2:
        params["overnight_reserve_margin_pct"] = _clamp_param(
            "overnight_reserve_margin_pct",
            _ema(
                float(params.get("overnight_reserve_margin_pct") or 4.0),
                float(params.get("overnight_reserve_margin_pct") or 4.0) + 1.5,
                0.55,
            ),
        )
        adjustments.append(
            f"Nachtreserve: Stress-Ticks SOC={overnight_stress}, "
            f"unerwartete Last={overnight_unexpected} → Margin "
            f"{params['overnight_reserve_margin_pct']:.1f} %."
        )
    # Zwischenstand speichern, bevor Anker-Lernen params aus State neu lädt
    sl["params"] = params
    state["steering_learning"] = sl

    # --- Anker-Fenster Abend-Lernen ---
    try:
        aw_adj = run_anchor_window_evening_learning(state, config)
        adjustments.extend(aw_adj)
        params = get_steering_params(state)
    except Exception:
        pass

    history = list(sl.get("history") or [])
    history.append(
        {
            "date": date.today().isoformat(),
            "miss_export_kwh": round(effective_miss_kwh, 2),
            "miss_minutes": miss_min,
            "peak_export_kw": analysis.get("peak_export_kw"),
            "forecast_ratio": round(forecast_ratio, 3) if forecast_ratio is not None else None,
            "min_breach_minutes": effective_min_breach_min,
            "min_breach_while_load_ticks": tick_min_while_load,
            "max_over_minutes": max_over_min,
            "avoidable_min_breach": avoidable_min_breach,
            "on_limit_blocks": on_blocks,
            "on_limit_miss_ticks": on_miss,
            "params": dict(params),
            "adjustments": list(adjustments),
        }
    )
    sl["history"] = history[-90:]
    sl["params"] = params
    sl["last_run"] = datetime.now().isoformat(timespec="seconds")
    sl["last_note"] = "ok" if adjustments else "stable"
    sl["last_adjustments"] = adjustments
    sl["last_analysis"] = analysis
    state["steering_learning"] = sl
    return state


def anchor_window_safety_pct(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("anchor_window_safety_pct") or 3.0)


def anchor_window_early_start_bias(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("anchor_window_early_start_bias") or 0.0)


def anchor_window_forecast_trust(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("anchor_window_forecast_trust") or 0.85)


def run_anchor_window_evening_learning(
    state: Dict[str, Any],
    config: Dict[str, Any],
) -> List[str]:
    """Abend-Lernen für Anker-Fenster (Sicherheit vs. früher Start)."""
    sl = ensure_steering_learning(state)
    params = get_steering_params(state)
    today = sl.get("today") or {}
    window_ticks = int(today.get("aw_window_ticks") or 0)
    if window_ticks < 20:
        return []

    adjustments: List[str] = []
    soc_viol = int(today.get("aw_soc_violation_ticks") or 0)
    running = int(today.get("aw_running_ticks") or 0)
    breaks = int(today.get("aw_break_ticks") or 0)
    headroom_samples = int(today.get("aw_headroom_samples") or 0)
    headroom_sum = float(today.get("aw_headroom_sum") or 0.0)
    avg_headroom = (headroom_sum / headroom_samples) if headroom_samples > 0 else 0.0

    if soc_viol >= 3:
        params["anchor_window_safety_pct"] = _clamp_param(
            "anchor_window_safety_pct",
            _ema(
                float(params.get("anchor_window_safety_pct") or 3.0),
                float(params.get("anchor_window_safety_pct") or 3.0) + 2.0,
                0.6,
            ),
        )
        adjustments.append(
            f"Anker-Fenster: {soc_viol} SoC-Verletzungen → Safety "
            f"{params['anchor_window_safety_pct']:.1f} %."
        )
    elif (
        running >= max(50, int(window_ticks * 0.8))
        and breaks == 0
        and soc_viol == 0
        and avg_headroom >= 15.0
    ):
        params["anchor_window_safety_pct"] = _clamp_param(
            "anchor_window_safety_pct",
            _ema(
                float(params.get("anchor_window_safety_pct") or 3.0),
                float(params.get("anchor_window_safety_pct") or 3.0) - 1.0,
                0.45,
            ),
        )
        params["anchor_window_early_start_bias"] = _clamp_param(
            "anchor_window_early_start_bias",
            _ema(
                float(params.get("anchor_window_early_start_bias") or 0.0),
                float(params.get("anchor_window_early_start_bias") or 0.0) + 0.15,
                0.5,
            ),
        )
        adjustments.append(
            f"Anker-Fenster: stabil mit Headroom ~{avg_headroom:.0f} %-Pkt → "
            f"Safety {params['anchor_window_safety_pct']:.1f} %, "
            f"Frühstart-Bias {params['anchor_window_early_start_bias']:.2f}."
        )

    sl["params"] = params
    state["steering_learning"] = sl
    return adjustments


def learning_status_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    sl = ensure_steering_learning(state)
    params = get_steering_params(state)
    today = sl.get("today") or {}
    return {
        "score": params["score"],
        "params": params,
        "today_miss_ticks": int(today.get("miss_ticks") or 0),
        "today_last_miss_at": today.get("last_miss_at"),
        "last_run": sl.get("last_run"),
        "last_note": sl.get("last_note"),
        "last_adjustments": sl.get("last_adjustments") or [],
        "last_analysis": sl.get("last_analysis"),
        "history_days": len(sl.get("history") or []),
    }


def learning_info_messages(state: Dict[str, Any]) -> List[str]:
    p = get_steering_params(state)
    msgs = [
        f"Steuerungs-Lernen: Score {p['score']:.0f}/100 · "
        f"Prio‑1 früher ab SOC−{p['soc_preempt_margin']:.1f} %-Pkt · "
        f"länger +{p['top_prio_extra_runtime_h']:.1f} h · "
        f"Hold {p['top_prio_hold_below_max']:.1f} %-Pkt unter Max · "
        f"Export-Schwelle {p['export_threshold_kw']:.2f} kW"
    ]
    if (
        p["switch_risk_extra_runtime_h"] > 0.05
        or p["switch_risk_extra_off_h"] > 0.05
        or p["switch_risk_session_bias"] > 0.05
    ):
        msgs.append(
            f"Risiko-Neuron: An+{p['switch_risk_extra_runtime_h']:.1f} h · "
            f"Aus+{p['switch_risk_extra_off_h']:.1f} h · "
            f"Plan-Bias {p['switch_risk_session_bias']:.2f}"
        )
    sl = ensure_steering_learning(state)
    risk_note = sl.get("last_risk_note")
    if risk_note:
        msgs.append(str(risk_note))
    for a in (sl.get("last_adjustments") or [])[:3]:
        msgs.append(f"Lernen: {a}")
    today = sl.get("today") or {}
    if int(today.get("miss_ticks") or 0) > 0:
        msgs.append(
            f"Heute bisher {today['miss_ticks']} Miss-Ticks "
            f"(Export bei hohem SOC, zuletzt {today.get('last_miss_at') or '?'})."
        )
    if int(today.get("on_limit_blocks") or 0) > 0:
        msgs.append(
            f"Heute {today['on_limit_blocks']}× EIN wegen Tageslimit gesperrt"
            f" ({today.get('on_limit_miss_ticks') or 0} mit Export)."
        )
    if (
        float(p.get("peak_spill_boost") or 0) > 0.05
        or float(p.get("anchor_conserve_bias") or 0) > 0.05
    ):
        msgs.append(
            f"Lehrneuron: Spill-Boost {p['peak_spill_boost']:.2f} · "
            f"Anker-Bias {p['anchor_conserve_bias']:.2f} · "
            f"Peak-Sens {p.get('peak_export_sensitivity', 1.0):.2f}"
        )
    lehr_note = sl.get("last_lehr_note")
    if lehr_note:
        msgs.append(str(lehr_note))
    if (
        float(p.get("band_max_target_pct") or 90) < 93.0
        or float(p.get("band_min_cushion_pct") or 0) > 0.5
        or float(p.get("band_weather_guard") or 0) > 0.1
    ):
        msgs.append(
            f"Speicher-Band: Decke {p['band_max_target_pct']:.0f} % · "
            f"Floor+{p['band_min_cushion_pct']:.1f} %-Pkt · "
            f"Wetter-Guard {p.get('band_weather_guard', 0):.2f} · "
            f"Verbrauchs-Guard {p.get('band_consumption_guard', 0):.2f}"
        )
    band_note = sl.get("last_band_note")
    if band_note:
        msgs.append(str(band_note))
    if int(today.get("min_soc_breach_ticks") or 0) > 0:
        msgs.append(
            f"Heute Min-SoC-Unterschreitung: {today['min_soc_breach_ticks']} Ticks"
            f" (davon {today.get('min_soc_breach_while_load_ticks') or 0} mit Last)."
        )
    if float(p.get("soc_prio_margin_pct") or 0) > 0:
        msgs.append(
            f"SoC-Prio-Neuron: Marge {p['soc_prio_margin_pct']:.1f} % · "
            f"Anker-Stretch {p.get('soc_prio_anchor_stretch', 0):.1f} h · "
            f"Flex-Cut {p.get('soc_prio_flex_cut_bias', 0):.2f}"
        )
    soc_prio_note = sl.get("last_soc_prio_note")
    if soc_prio_note:
        msgs.append(str(soc_prio_note))
    return msgs
