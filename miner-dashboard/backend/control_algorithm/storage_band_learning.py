# -*- coding: utf-8 -*-
"""
Speicher-Band-Neuron: hält den SOC im konfigurierten Mittelfeld.

Ziel:
  - nicht unter Morgen-Floor (morning_soc_percent, z. B. 46 %)
  - nicht über Max-SoC (max_soc_percent, z. B. 95 %)
  - bei schwachem Wetter / hohem Verbrauch: Obergrenze eher ~ evening_soc (90 %)
    und 2–3-Tage-Horizont berücksichtigen (heute, morgen, übermorgen)

Lernen per Tick (Sofort-EMA) + Abendauswertung (langsamer).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.steering_learning import (
    DEFAULT_PARAMS,
    _clamp_param,
    _ema,
    ensure_steering_learning,
    get_steering_params,
)

BAND_PARAM_KEYS = (
    "band_max_target_pct",
    "band_min_cushion_pct",
    "band_weather_guard",
    "band_consumption_guard",
    "band_horizon_weight",
)

BAND_DEFAULTS: Dict[str, float] = {
    "band_max_target_pct": 90.0,
    "band_min_cushion_pct": 2.0,
    "band_weather_guard": 0.35,
    "band_consumption_guard": 0.25,
    "band_horizon_weight": 0.65,
}

BAND_BOUNDS: Dict[str, Tuple[float, float]] = {
    "band_max_target_pct": (82.0, 95.0),
    "band_min_cushion_pct": (0.0, 12.0),
    "band_weather_guard": (0.0, 1.0),
    "band_consumption_guard": (0.0, 1.0),
    "band_horizon_weight": (0.2, 1.0),
}

BAND_TODAY_KEYS = (
    "band_low_ticks",
    "band_high_ticks",
    "band_over_max_ticks",
    "band_in_target_ticks",
    "band_export_high_ticks",
)


def _morning_floor_pct(config: Dict[str, Any]) -> float:
    raw = config.get("morning_soc_percent")
    if raw is None:
        raw = config.get("min_soc_percent") or 35.0
    return float(raw)


def _horizon_pv_cons(config: Dict[str, Any], state: Dict[str, Any]) -> Tuple[List[float], List[float]]:
    pv = [
        float(state.get("pv_yield_today_kwh") or 0),
        float(state.get("pv_yield_tomorrow_kwh") or 0),
        float(state.get("pv_yield_day_after_kwh") or 0),
    ]
    est = state.get("consumption_estimates") or {}
    base = (est.get("base_only") or {}).get("by_day_kwh") or []
    cons: List[float] = []
    for i in range(3):
        if i < len(base):
            try:
                cons.append(float(base[i] or 0))
            except (TypeError, ValueError):
                cons.append(0.0)
        elif cons:
            cons.append(cons[-1])
        else:
            cons.append(0.0)
    if not any(c > 10 for c in cons):
        for i, snap in enumerate((state.get("energy_budget_snapshots") or [])[:3]):
            daily = (snap or {}).get("consumption_daily") or {}
            try:
                cons[i] = float(daily.get("base_kwh") or cons[i])
            except (TypeError, ValueError):
                pass
    return pv, cons


def horizon_stress(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """
    Grobe 3-Tage-Simulation: Überfüllungs- / Unterfüllungsrisiko (0–1).
    """
    storage = float(config.get("storage_kwh") or 0)
    if storage <= 0:
        return {"overfill": 0.0, "underfill": 0.0}

    max_soc = float(config.get("max_soc_percent") or 95.0)
    morning = _morning_floor_pct(config)
    soc = None
    if live:
        soc = live.get("soc_percent")
    if soc is None:
        proj = state.get("balance_projection") or {}
        soc = proj.get("soc_percent")
    try:
        soc_f = float(soc if soc is not None else morning)
    except (TypeError, ValueError):
        soc_f = morning

    pv_days, cons_days = _horizon_pv_cons(config, state)
    kwh = soc_f / 100.0 * storage
    max_kwh = max_soc / 100.0 * storage
    min_kwh = morning / 100.0 * storage
    evening_kwh = float(config.get("evening_soc_percent") or 90.0) / 100.0 * storage

    overfill = 0.0
    underfill = 0.0
    sim_kwh = kwh
    for pv, cons in zip(pv_days, cons_days):
        sim_kwh += float(pv) - float(cons)
        if max_kwh > 0 and sim_kwh > evening_kwh:
            overfill = max(
                overfill,
                min(1.0, (sim_kwh - evening_kwh) / max(max_kwh - evening_kwh, 1.0)),
            )
        if min_kwh > 0 and sim_kwh < min_kwh * 1.08:
            underfill = max(
                underfill,
                min(1.0, (min_kwh * 1.08 - sim_kwh) / max(min_kwh * 0.25, 1.0)),
            )
    return {"overfill": round(overfill, 3), "underfill": round(underfill, 3)}


def storage_band_limits(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Effektives SOC-Mittelfeld (Floor … Ceiling) inkl. Horizont-Anpassung."""
    params = get_steering_params(state)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    morning = _morning_floor_pct(config)
    evening = float(config.get("evening_soc_percent") or 90.0)

    ceiling = min(
        max_soc,
        float(params.get("band_max_target_pct") or BAND_DEFAULTS["band_max_target_pct"]),
    )
    floor = morning + float(params.get("band_min_cushion_pct") or 0.0)
    floor = min(floor, max(morning, ceiling - 8.0))

    w_guard = float(params.get("band_weather_guard") or 0.0)
    c_guard = float(params.get("band_consumption_guard") or 0.0)
    h_weight = float(params.get("band_horizon_weight") or 0.65)
    stress = horizon_stress(config, state, live=live)

    if stress["overfill"] > 0.08:
        pull = stress["overfill"] * w_guard * h_weight * max(0.0, max_soc - evening)
        ceiling = max(evening - 1.0, ceiling - pull)
    if stress["underfill"] > 0.08:
        push = stress["underfill"] * c_guard * h_weight * 5.0
        floor = min(max(morning, ceiling - 8.0), floor + push)

    note_parts: List[str] = []
    if stress["overfill"] > 0.15:
        note_parts.append(f"Horizont Überfüllung {stress['overfill']:.0%}")
    if stress["underfill"] > 0.15:
        note_parts.append(f"Horizont Unterfüllung {stress['underfill']:.0%}")

    return {
        "floor_pct": round(floor, 2),
        "ceiling_pct": round(ceiling, 2),
        "max_pct": max_soc,
        "morning_pct": morning,
        "evening_pct": evening,
        "overfill_risk": stress["overfill"],
        "underfill_risk": stress["underfill"],
        "note": " · ".join(note_parts) if note_parts else None,
    }


def effective_planning_max_soc(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    live: Optional[Dict[str, Any]] = None,
) -> float:
    """Planungs-Obergrenze (unter config max_soc).

    Liegt ein berechnetes Planungsziel vor, ist das die Decke.
    Die Wetter-Horizont-Absenkung und ein heute unerreichbarer SoC
    ersetzen es nicht.
    """
    planned = state.get("planning_target") if isinstance(state, dict) else None
    if isinstance(planned, dict) and planned.get("signature") and planned.get("soc_percent") is not None:
        try:
            pct = float(planned["soc_percent"])
            max_soc = float(config.get("max_soc_percent") or 95.0)
            return min(max_soc, pct)
        except (TypeError, ValueError):
            pass
    return float(storage_band_limits(config, state, live=live)["ceiling_pct"])


def effective_planning_min_soc(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    live: Optional[Dict[str, Any]] = None,
) -> float:
    """Planungs-Untergrenze (über morning_soc)."""
    return float(storage_band_limits(config, state, live=live)["floor_pct"])


def band_consumption_plan_scale(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    live: Optional[Dict[str, Any]] = None,
) -> float:
    """Zusätzlicher Verbrauchs-Puffer für Planung (1.0 = neutral)."""
    params = get_steering_params(state)
    guard = float(params.get("band_consumption_guard") or 0.0)
    stress = horizon_stress(config, state, live=live)
    extra = guard * 0.22 * stress["underfill"]
    # Muster-Mehrverbrauch heute leicht mitnehmen
    pat = state.get("consumption_pattern_estimate") or {}
    try:
        delta = float(pat.get("delta_vs_forecast_pct") or 0)
        if delta > 10:
            extra += min(0.12, guard * delta / 400.0)
    except (TypeError, ValueError):
        pass
    return round(1.0 + extra, 4)


def band_pv_plan_scale(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    live: Optional[Dict[str, Any]] = None,
) -> float:
    """PV-Dämpfung bei schwachem Horizont (1.0 = neutral)."""
    params = get_steering_params(state)
    guard = float(params.get("band_weather_guard") or 0.0)
    stress = horizon_stress(config, state, live=live)
    discount = guard * 0.18 * stress["overfill"]
    return round(max(0.75, 1.0 - discount), 4)


def record_storage_band_tick(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> None:
    """Jeder Tick: SOC vs. Mittelfeld zählen; bei Verletzung langsam nachjustieren."""
    if not config.get("has_storage"):
        return
    soc = live.get("soc_percent")
    if soc is None:
        return

    sl = ensure_steering_learning(state)
    today = sl["today"]
    limits = storage_band_limits(config, state, live=live)
    floor = float(limits["floor_pct"])
    ceiling = float(limits["ceiling_pct"])
    max_soc = float(limits["max_pct"])
    soc_f = float(soc)

    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        export_kw = float(live_grid_export_kw(live) or 0)
    except Exception:
        export_kw = max(0.0, float(live.get("export_kw") or live.get("einspeisung_kw") or 0))

    if soc_f < floor:
        today["band_low_ticks"] = int(today.get("band_low_ticks") or 0) + 1
    elif soc_f > max_soc + 0.2:
        today["band_over_max_ticks"] = int(today.get("band_over_max_ticks") or 0) + 1
    elif soc_f > ceiling + 0.2:
        today["band_high_ticks"] = int(today.get("band_high_ticks") or 0) + 1
        if export_kw >= 0.3:
            today["band_export_high_ticks"] = int(today.get("band_export_high_ticks") or 0) + 1
    elif floor <= soc_f <= ceiling:
        today["band_in_target_ticks"] = int(today.get("band_in_target_ticks") or 0) + 1

    params = dict(sl.get("params") or {})
    changed = False

    # Sofort: deutlich über Max oder Export oberhalb der Band-Decke
    if soc_f > max_soc + 0.3 or (soc_f > ceiling + 1.0 and export_kw >= 0.5):
        tgt = float(params.get("band_max_target_pct") or BAND_DEFAULTS["band_max_target_pct"])
        evening_pct = float(limits["evening_pct"])
        if soc_f > max_soc + 0.3:
            new_tgt = _ema(tgt, max(evening_pct - 1.0, tgt - 1.8), 0.55)
        else:
            new_tgt = _ema(tgt, max(evening_pct, tgt - 1.2), 0.5)
        params["band_max_target_pct"] = _clamp_param("band_max_target_pct", new_tgt)
        w_g = float(params.get("band_weather_guard") or 0.0)
        params["band_weather_guard"] = _clamp_param(
            "band_weather_guard", _ema(w_g, min(1.0, w_g + 0.06), 0.45)
        )
        h = float(params.get("headroom_extra_soc") or 0.0)
        params["headroom_extra_soc"] = _clamp_param(
            "headroom_extra_soc", _ema(h, min(8.0, h + 0.35), 0.4)
        )
        changed = True
        sl["last_band_note"] = (
            f"Speicher-Band: SOC {soc_f:.0f} % > Decke {ceiling:.0f} % "
            f"(Max {max_soc:.0f} %) → Ziel-Obergrenze {params['band_max_target_pct']:.0f} %."
        )

    # Sofort: unter Floor
    if soc_f < floor - 0.5:
        cush = float(params.get("band_min_cushion_pct") or 0.0)
        params["band_min_cushion_pct"] = _clamp_param(
            "band_min_cushion_pct", _ema(cush, min(12.0, cush + 0.5), 0.45)
        )
        c_g = float(params.get("band_consumption_guard") or 0.0)
        params["band_consumption_guard"] = _clamp_param(
            "band_consumption_guard", _ema(c_g, min(1.0, c_g + 0.05), 0.4)
        )
        changed = True
        sl["last_band_note"] = (
            f"Speicher-Band: SOC {soc_f:.0f} % < Floor {floor:.0f} % "
            f"→ Puffer +{params['band_min_cushion_pct']:.1f} %-Pkt."
        )

    if changed:
        sl["params"] = params
    state["steering_learning"] = sl


def run_storage_band_evening_learning(
    state: Dict[str, Any],
    config: Dict[str, Any],
    analysis: Dict[str, Any],
    params: Optional[Dict[str, float]] = None,
) -> List[str]:
    """Abend-Lernschritt für Speicher-Band-Neuron."""
    if not config.get("has_storage"):
        return []

    sl = ensure_steering_learning(state)
    if params is None:
        params = get_steering_params(state)
    else:
        for key in DEFAULT_PARAMS:
            if key not in params:
                params[key] = float(DEFAULT_PARAMS[key])

    today = sl.get("today") or {}
    adjustments: List[str] = []

    low = int(today.get("band_low_ticks") or 0)
    high = int(today.get("band_high_ticks") or 0)
    over_max = int(today.get("band_over_max_ticks") or 0)
    in_band = int(today.get("band_in_target_ticks") or 0)
    export_high = int(today.get("band_export_high_ticks") or 0)
    max_over_min = int(analysis.get("max_over_minutes") or 0)
    min_breach = int(analysis.get("min_breach_minutes") or 0)

    stress = horizon_stress(config, state)
    evening = float(config.get("evening_soc_percent") or 90.0)

    # Über Max / hohe Decke / Export → Obergrenze senken
    if over_max >= 2 or high >= 5 or max_over_min >= 25 or export_high >= 3:
        tgt = float(params.get("band_max_target_pct") or evening)
        params["band_max_target_pct"] = _clamp_param(
            "band_max_target_pct",
            _ema(tgt, max(evening - 2.0, tgt - 2.0), 0.55),
        )
        params["band_weather_guard"] = _clamp_param(
            "band_weather_guard",
            _ema(params["band_weather_guard"], min(1.0, params["band_weather_guard"] + 0.12), 0.5),
        )
        params["soc_preempt_margin"] = _clamp_param(
            "soc_preempt_margin",
            _ema(params["soc_preempt_margin"], min(12.0, params["soc_preempt_margin"] + 1.0), 0.45),
        )
        params["headroom_extra_soc"] = _clamp_param(
            "headroom_extra_soc",
            _ema(params["headroom_extra_soc"], min(8.0, params["headroom_extra_soc"] + 0.8), 0.45),
        )
        adjustments.append(
            f"Speicher-Band: über Decke/Max ({over_max}×>{params.get('band_max_target_pct', evening):.0f} %, "
            f"{max_over_min} min > Max) → Obergrenze {params['band_max_target_pct']:.0f} %, "
            f"Wetter-Guard {params['band_weather_guard']:.2f}."
        )
    elif stress["overfill"] >= 0.35:
        tgt = float(params.get("band_max_target_pct") or evening)
        params["band_max_target_pct"] = _clamp_param(
            "band_max_target_pct",
            _ema(tgt, max(evening - 1.0, tgt - 1.5), 0.5),
        )
        params["band_weather_guard"] = _clamp_param(
            "band_weather_guard",
            _ema(params["band_weather_guard"], min(1.0, params["band_weather_guard"] + 0.08), 0.45),
        )
        adjustments.append(
            f"Speicher-Band: Horizont-Überfüllung {stress['overfill']:.0%} "
            f"→ Ziel-Decke {params['band_max_target_pct']:.0f} %."
        )

    # Unter Floor / Min-Bruch → Reserve erhöhen
    if low >= 3 or min_breach >= 8 or stress["underfill"] >= 0.35:
        params["band_min_cushion_pct"] = _clamp_param(
            "band_min_cushion_pct",
            _ema(params["band_min_cushion_pct"], min(12.0, params["band_min_cushion_pct"] + 1.0), 0.5),
        )
        params["band_consumption_guard"] = _clamp_param(
            "band_consumption_guard",
            _ema(
                params["band_consumption_guard"],
                min(1.0, params["band_consumption_guard"] + 0.1),
                0.5,
            ),
        )
        adjustments.append(
            f"Speicher-Band: unter Floor ({low} Ticks) / Min-Bruch {min_breach} min "
            f"→ Floor-Puffer {params['band_min_cushion_pct']:.1f} %, "
            f"Verbrauchs-Guard {params['band_consumption_guard']:.2f}."
        )

    # Guter Tag im Band → leicht entspannen
    if (
        in_band >= 20
        and over_max == 0
        and low <= 1
        and max_over_min < 10
        and stress["overfill"] < 0.2
        and stress["underfill"] < 0.2
    ):
        for key in BAND_PARAM_KEYS:
            params[key] = _clamp_param(key, _ema(params[key], BAND_DEFAULTS[key], 0.07))
        adjustments.append(
            "Speicher-Band: SOC im Mittelfeld – Parameter leicht Richtung Standard."
        )

    if adjustments:
        sl["last_band_note"] = adjustments[-1]
    sl["params"] = params
    state["steering_learning"] = sl
    return adjustments
