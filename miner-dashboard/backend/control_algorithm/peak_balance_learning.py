# -*- coding: utf-8 -*-
"""
Lehrneuron Prio-Balance: Spitzen-PV vs. Export und Anker-Laufzeit.

Lernt aus Ticks + Abendauswertung:
  - Export an Spitzen → mehr Spill/Parallel-Last (peak_spill_boost)
  - Kein Export aber zu viele Niedrig-Prio parallel → Anker länger (anchor_conserve_bias)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.steering_learning import (
    DEFAULT_PARAMS,
    PARAM_BOUNDS,
    _clamp_param,
    _ema,
    ensure_steering_learning,
    export_threshold_kw,
    get_steering_params,
)

# Erweiterung der steering_learning-Parameter (werden in steering_learning.py registriert)
PEAK_PARAM_KEYS = (
    "peak_spill_boost",
    "anchor_conserve_bias",
    "peak_export_sensitivity",
)

PEAK_DEFAULTS: Dict[str, float] = {
    "peak_spill_boost": 0.0,
    "anchor_conserve_bias": 0.0,
    "peak_export_sensitivity": 1.0,
}

PEAK_BOUNDS: Dict[str, Tuple[float, float]] = {
    "peak_spill_boost": (0.0, 1.0),
    "anchor_conserve_bias": (0.0, 1.0),
    "peak_export_sensitivity": (0.5, 2.0),
}

PEAK_TODAY_KEYS = (
    "peak_pv_ticks",
    "peak_export_miss_ticks",
    "peak_export_miss_kw_sum",
    "peak_overparallel_ticks",
    "peak_good_absorb_ticks",
)


def peak_spill_boost(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("peak_spill_boost") or 0.0)


def anchor_conserve_bias(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("anchor_conserve_bias") or 0.0)


def peak_export_sensitivity(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("peak_export_sensitivity") or 1.0)


def _is_peak_hour(hour: Optional[int] = None) -> bool:
    h = datetime.now().hour if hour is None else int(hour)
    return 9 <= h < 16


def _peak_pv_threshold_kw(config: Dict[str, Any]) -> float:
    pv_kwp = float(config.get("pv_kwp") or 0)
    return max(3.0, pv_kwp * 0.12)


def _classify_running_roles(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, Any],
) -> Dict[str, int]:
    """Zählt laufende Geräte nach Anker/Flex/Spill."""
    from control_algorithm.planner import sort_for_resume
    from control_algorithm.priority_runtime import _device_role

    ordered = sort_for_resume(list(devices))
    n = len(ordered)
    counts = {"anchor": 0, "flex": 0, "spill": 0}
    for i, d in enumerate(ordered):
        ip = str(d.get("ip") or "")
        if not ip:
            continue
        if str(miner_status.get(ip) or "").lower() != "running":
            continue
        role = _device_role(i, n)
        counts[role] = int(counts.get(role) or 0) + 1
    return counts


def peak_export_risk(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> bool:
    """
    Spitzen-Export-Gefahr: hoher SoC + PV-Spitze + Export nahe Schwelle.
    """
    soc = live.get("soc_percent")
    if soc is None:
        return False
    max_soc = float(config.get("max_soc_percent") or 95.0)
    if float(soc) < max_soc - 10.0:
        return False
    pv_kw = float(live.get("pv_kw") or 0)
    if not _is_peak_hour() or pv_kw < _peak_pv_threshold_kw(config):
        return False
    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        export_kw = float(live_grid_export_kw(live) or 0)
    except Exception:
        export_kw = max(0.0, float(live.get("export_kw") or live.get("grid_export_kw") or 0))
    sens = peak_export_sensitivity(state)
    thr = export_threshold_kw(state) * sens
    if export_kw >= thr:
        return True
    if export_kw >= thr * 0.45 and peak_spill_boost(state) >= 0.25:
        return True
    pv_surplus = max(0.0, pv_kw - float(live.get("verbrauch_kw") or 0))
    if float(soc) >= max_soc - 5.0 and pv_surplus >= 2.0 and export_kw >= thr * 0.3:
        return True
    return False


def should_peak_spill_resume(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    *,
    export_mode: bool = False,
) -> bool:
    """Spill früher einschalten wenn gelerntes Peak-Risiko ohne vollen export_mode."""
    if export_mode:
        return True
    if peak_spill_boost(state) < 0.15:
        return False
    return peak_export_risk(config, state, live)


def should_conserve_for_anchor(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    *,
    export_mode: bool = False,
) -> bool:
    """Anker bevorzugen – nur wenn kein Live-Export und Bias hoch genug."""
    if export_mode:
        return False
    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        export_kw = float(live_grid_export_kw(live) or 0)
    except Exception:
        export_kw = max(0.0, float(live.get("export_kw") or live.get("grid_export_kw") or 0))
    if export_kw >= export_threshold_kw(state):
        return False
    return anchor_conserve_bias(state) >= 0.2


def record_peak_balance_tick(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> None:
    """Jeder Daemon-Tick während PV-Spitze: Export vs. Prio-Mischung zählen."""
    sl = ensure_steering_learning(state)
    today = sl["today"]
    soc = live.get("soc_percent")
    if soc is None:
        return

    pv_kw = float(live.get("pv_kw") or 0)
    if not _is_peak_hour() or pv_kw < _peak_pv_threshold_kw(config):
        return

    try:
        from control_algorithm.weather_survival import live_grid_export_kw
        from control_algorithm.planner import enabled_devices, miner_load_kw
    except Exception:
        return

    export_kw = float(live_grid_export_kw(live) or 0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    soc_f = float(soc)
    thr = export_threshold_kw(state) * peak_export_sensitivity(state)
    high_soc = soc_f >= max_soc - 5.0

    today["peak_pv_ticks"] = int(today.get("peak_pv_ticks") or 0) + 1

    devices = enabled_devices(config)
    miner_status = live.get("miner_status") or {}
    roles = _classify_running_roles(devices, miner_status)
    miner_kw = miner_load_kw(devices, miner_status)
    low_running = int(roles.get("flex") or 0) + int(roles.get("spill") or 0)

    if high_soc and export_kw >= max(thr, 0.12):
        today["peak_export_miss_ticks"] = int(today.get("peak_export_miss_ticks") or 0) + 1
        today["peak_export_miss_kw_sum"] = float(today.get("peak_export_miss_kw_sum") or 0) + export_kw
        # Sofort-Lernen bei starkem Spitzen-Export
        if export_kw >= 0.8:
            params = dict(sl.get("params") or {})
            boost = float(params.get("peak_spill_boost") or 0)
            sens = float(params.get("peak_export_sensitivity") or 1.0)
            params["peak_spill_boost"] = _clamp_param(
                "peak_spill_boost", _ema(boost, min(1.0, boost + 0.12), 0.55)
            )
            params["peak_export_sensitivity"] = _clamp_param(
                "peak_export_sensitivity", _ema(sens, min(2.0, sens + 0.08), 0.45)
            )
            sl["params"] = params
            sl["last_lehr_note"] = (
                f"Lehrneuron: Spitzen-Export {export_kw:.1f} kW → "
                f"Spill-Boost {params['peak_spill_boost']:.2f}, "
                f"Sens {params['peak_export_sensitivity']:.2f}."
            )
    elif high_soc and export_kw < thr and miner_kw >= 1.0:
        today["peak_good_absorb_ticks"] = int(today.get("peak_good_absorb_ticks") or 0) + 1
        if low_running >= 2 and soc_f >= min_soc + 10.0:
            today["peak_overparallel_ticks"] = int(today.get("peak_overparallel_ticks") or 0) + 1

    state["steering_learning"] = sl


def run_peak_balance_evening_learning(
    state: Dict[str, Any],
    config: Dict[str, Any],
    analysis: Dict[str, Any],
    params: Optional[Dict[str, float]] = None,
) -> List[str]:
    """
    Abend-Lernschritt für Lehrneuron. Returns Anpassungs-Texte für last_adjustments.
    Aktualisiert params in-place (falls übergeben) und state.
    """
    sl = ensure_steering_learning(state)
    if params is None:
        params = get_steering_params(state)
    else:
        for key in DEFAULT_PARAMS:
            if key not in params:
                params[key] = float(DEFAULT_PARAMS[key])
    today = sl.get("today") or {}
    adjustments: List[str] = []

    peak_miss = int(today.get("peak_export_miss_ticks") or 0)
    peak_miss_kw = float(today.get("peak_export_miss_kw_sum") or 0)
    overparallel = int(today.get("peak_overparallel_ticks") or 0)
    good_absorb = int(today.get("peak_good_absorb_ticks") or 0)
    peak_pv = int(today.get("peak_pv_ticks") or 0)

    miss_kwh = float(analysis.get("miss_export_kwh") or 0)
    peak_export_kw = float(analysis.get("peak_export_kw") or 0)

    # Export-Miss an Spitzen → mehr Spill
    if peak_miss >= 3 or (peak_miss >= 1 and peak_miss_kw >= 3.0):
        params["peak_spill_boost"] = _clamp_param(
            "peak_spill_boost",
            _ema(params["peak_spill_boost"], min(1.0, params["peak_spill_boost"] + 0.18), 0.6),
        )
        params["peak_export_sensitivity"] = _clamp_param(
            "peak_export_sensitivity",
            _ema(
                params["peak_export_sensitivity"],
                min(2.0, params["peak_export_sensitivity"] + 0.12),
                0.55,
            ),
        )
        params["soc_preempt_margin"] = _clamp_param(
            "soc_preempt_margin",
            _ema(params["soc_preempt_margin"], params["soc_preempt_margin"] + 1.0, 0.45),
        )
        params["anchor_conserve_bias"] = _clamp_param(
            "anchor_conserve_bias",
            _ema(params["anchor_conserve_bias"], max(0.0, params["anchor_conserve_bias"] - 0.1), 0.4),
        )
        adjustments.append(
            f"Lehrneuron: Spitzen-Export ({peak_miss} Ticks, Σ{peak_miss_kw:.1f} kW) "
            f"→ Spill-Boost {params['peak_spill_boost']:.2f}, "
            f"Sens {params['peak_export_sensitivity']:.2f}."
        )
    elif miss_kwh >= 5.0 or peak_export_kw >= 2.0:
        params["peak_spill_boost"] = _clamp_param(
            "peak_spill_boost",
            _ema(params["peak_spill_boost"], min(1.0, params["peak_spill_boost"] + 0.1), 0.5),
        )
        adjustments.append(
            f"Lehrneuron: Tages-Export-Miss ~{miss_kwh:.1f} kWh "
            f"(Peak {peak_export_kw:.1f} kW) → Spill-Boost {params['peak_spill_boost']:.2f}."
        )

    # Overparallel ohne Export → Anker länger, weniger Niedrig-Prio parallel
    if overparallel >= 4 and peak_miss < 2 and good_absorb >= 2:
        params["anchor_conserve_bias"] = _clamp_param(
            "anchor_conserve_bias",
            _ema(
                params["anchor_conserve_bias"],
                min(1.0, params["anchor_conserve_bias"] + 0.15),
                0.55,
            ),
        )
        params["top_prio_extra_runtime_h"] = _clamp_param(
            "top_prio_extra_runtime_h",
            _ema(
                params["top_prio_extra_runtime_h"],
                min(8.0, params["top_prio_extra_runtime_h"] + 0.75),
                0.5,
            ),
        )
        adjustments.append(
            f"Lehrneuron: Kein Spitzen-Export aber {overparallel}× zu viele Niedrig-Prio "
            f"→ Anker-Bias {params['anchor_conserve_bias']:.2f}, "
            f"+{params['top_prio_extra_runtime_h']:.1f} h Top-Prio."
        )
    elif overparallel >= 8 and peak_miss == 0:
        params["anchor_conserve_bias"] = _clamp_param(
            "anchor_conserve_bias",
            _ema(params["anchor_conserve_bias"], min(1.0, params["anchor_conserve_bias"] + 0.08), 0.45),
        )
        adjustments.append(
            f"Lehrneuron: Viele Parallel-Ticks ({overparallel}) ohne Export "
            f"→ Anker-Bias {params['anchor_conserve_bias']:.2f}."
        )

    # Guter Tag: leicht entspannen
    if (
        peak_pv >= 5
        and peak_miss == 0
        and miss_kwh < 1.0
        and overparallel < 3
        and good_absorb >= 3
    ):
        for key in PEAK_PARAM_KEYS:
            default = PEAK_DEFAULTS[key]
            params[key] = _clamp_param(key, _ema(params[key], default, 0.08))
        adjustments.append(
            "Lehrneuron: Spitzen gut absorbiert – Parameter leicht Richtung Standard."
        )

    if adjustments:
        sl["last_lehr_note"] = adjustments[-1]
    sl["params"] = params
    state["steering_learning"] = sl
    return adjustments


def allocate_ips_with_conserve(
    budget_kw: float,
    devices_sorted: List[Dict[str, Any]],
    *,
    anchor_conserve: float = 0.0,
    live_export_kw: float = 0.0,
    export_thr: float = 0.05,
) -> Tuple[List[str], float]:
    """
    Stunden-Budget nach Prio – mit Anker-Konservierung wenn kein Export.
    """
    from control_algorithm.config import normalize_device_priority

    chosen: List[str] = []
    used = 0.0
    if budget_kw < 0.3:
        return chosen, used

    export_active = live_export_kw >= export_thr
    bias = anchor_conserve if not export_active else 0.0

    if bias >= 0.15 and devices_sorted:
        top_prio = max(normalize_device_priority(d.get("priority")) for d in devices_sorted)
        anchor_kw = sum(
            float(d.get("power_kw") or 0)
            for d in devices_sorted
            if normalize_device_priority(d.get("priority")) >= top_prio - 0.05
        )
        min_for_low = anchor_kw * (1.0 + bias * 0.5)
        if budget_kw < min_for_low + 0.2:
            for d in devices_sorted:
                ip = str(d.get("ip") or "")
                pwr = float(d.get("power_kw") or 0)
                if not ip or pwr <= 0:
                    continue
                if normalize_device_priority(d.get("priority")) < top_prio - 0.05:
                    continue
                if used + pwr <= budget_kw + 0.2:
                    chosen.append(ip)
                    used += pwr
            if chosen:
                return chosen, used

    for d in devices_sorted:
        ip = str(d.get("ip") or "")
        pwr = float(d.get("power_kw") or 0)
        if not ip or pwr <= 0:
            continue
        if used + pwr <= budget_kw + 0.2:
            chosen.append(ip)
            used += pwr
            continue
        if not chosen and pwr >= 0.5 and budget_kw >= pwr * 0.85:
            chosen.append(ip)
            used += pwr
            break
    return chosen, used
