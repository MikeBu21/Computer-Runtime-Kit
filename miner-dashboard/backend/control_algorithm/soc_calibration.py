# -*- coding: utf-8 -*-
"""SoC-Lernen: Morgenprognose vs. Ist-SOC → Grundlast-Skala und Akku-Wirkungsgrad."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.consumption_history import get_hourly_actual_soc_today

BASE_LOAD_SCALE_MIN = 0.5
BASE_LOAD_SCALE_MAX = 1.5
BATTERY_EFF_MIN = 0.75
BATTERY_EFF_MAX = 1.0
DEFAULT_BASE_LOAD_SCALE = 1.0
DEFAULT_BATTERY_EFFICIENCY = 0.95
SOC_MAE_TOLERANCE_PCT = 5.0
SOC_BIAS_TOLERANCE_PCT = 3.0
MIN_COMPARE_HOURS = 4


def normalize_base_load_scale(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = DEFAULT_BASE_LOAD_SCALE
    return round(max(BASE_LOAD_SCALE_MIN, min(BASE_LOAD_SCALE_MAX, v)), 4)


def normalize_battery_efficiency(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = DEFAULT_BATTERY_EFFICIENCY
    return round(max(BATTERY_EFF_MIN, min(BATTERY_EFF_MAX, v)), 4)


def ensure_soc_learning_defaults(state: Dict[str, Any]) -> None:
    if "base_load_scale" not in state or state.get("base_load_scale") is None:
        state["base_load_scale"] = DEFAULT_BASE_LOAD_SCALE
    else:
        state["base_load_scale"] = normalize_base_load_scale(state.get("base_load_scale"))
    if "battery_efficiency" not in state or state.get("battery_efficiency") is None:
        state["battery_efficiency"] = DEFAULT_BATTERY_EFFICIENCY
    else:
        state["battery_efficiency"] = normalize_battery_efficiency(
            state.get("battery_efficiency")
        )
    if "soc_history" not in state or state.get("soc_history") is None:
        state["soc_history"] = []
    if "soc_accuracy_score" not in state or state.get("soc_accuracy_score") is None:
        state["soc_accuracy_score"] = 0.0
    if "soc_days_since_reset" not in state or state.get("soc_days_since_reset") is None:
        state["soc_days_since_reset"] = 0


def reset_soc_learning(state: Dict[str, Any], *, force: bool = False) -> None:
    """SoC-Lernen nur mit force=True zurücksetzen (kein Auto-Wipe)."""
    if not force:
        return
    state["base_load_scale"] = DEFAULT_BASE_LOAD_SCALE
    state["battery_efficiency"] = DEFAULT_BATTERY_EFFICIENCY
    state["soc_history"] = []
    state["soc_accuracy_score"] = 0.0
    state["soc_days_since_reset"] = 0
    state["morning_soc_forecast_hours"] = None
    state["morning_soc_forecast_date"] = None
    state["morning_soc_start_pct"] = None
    state["last_soc_calibration"] = None
    state["last_soc_calibration_note"] = None
    state["last_soc_calibration_adjustments"] = []


def store_morning_soc_forecast(
    state: Dict[str, Any],
    hours: Dict[str, float],
    start_soc_pct: Optional[float] = None,
    day: Optional[date] = None,
) -> None:
    """Speichert die Einfrier-Prognose des Tages (einmal pro Tag)."""
    d = day or date.today()
    if state.get("morning_soc_forecast_date") == d.isoformat():
        return
    cleaned: Dict[str, float] = {}
    for k, v in (hours or {}).items():
        if v is None:
            continue
        try:
            cleaned[str(k)] = round(float(v), 2)
        except (TypeError, ValueError):
            continue
    if not cleaned:
        return
    state["morning_soc_forecast_hours"] = cleaned
    state["morning_soc_forecast_date"] = d.isoformat()
    if start_soc_pct is not None:
        state["morning_soc_start_pct"] = round(float(start_soc_pct), 2)


def apply_net_to_battery_kwh(
    kwh: float,
    net_kwh: float,
    storage: float,
    max_soc_percent: Optional[float],
    efficiency: float,
    cap_fn,
) -> float:
    """
    Nettoenergie auf den Speicher anwenden.
    Laden: * Wirkungsgrad; Entladen: / Wirkungsgrad (Verluste).
    """
    eff = normalize_battery_efficiency(efficiency)
    if net_kwh >= 0:
        kwh = kwh + net_kwh * eff
    else:
        kwh = kwh + net_kwh / max(eff, 0.5)
    return cap_fn(kwh, storage, max_soc_percent)


def _adjust_blend(current: float, target: float, coarse: bool, lo: float, hi: float) -> float:
    if coarse:
        new_v = current * 0.5 + target * 0.5
    else:
        new_v = current * 0.7 + target * 0.3
    return round(max(lo, min(hi, new_v)), 4)


def _hour_key(h: int) -> str:
    return f"h{h:02d}"


def _forecast_map(raw: Any) -> Dict[int, float]:
    out: Dict[int, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        try:
            if isinstance(k, int):
                hour = k
            else:
                s = str(k)
                hour = int(s[1:]) if s.startswith("h") else int(s)
            out[hour] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def compute_soc_accuracy_score(history: List[Dict[str, Any]], window: int = 7) -> float:
    recent = [h for h in history if h.get("compare_hours", 0) >= MIN_COMPARE_HOURS][-window:]
    if not recent:
        return 0.0
    hits = sum(1 for h in recent if h.get("hit"))
    return round(100.0 * hits / len(recent), 1)


def soc_eta_message(state: Dict[str, Any]) -> str:
    ensure_soc_learning_defaults(state)
    acc = float(state.get("soc_accuracy_score") or 0)
    days = int(state.get("soc_days_since_reset") or 0)
    scale = normalize_base_load_scale(state.get("base_load_scale"))
    eff = normalize_battery_efficiency(state.get("battery_efficiency"))
    if acc >= 90:
        return (
            f"SoC-Prognose >90 % Trefferquote (±{SOC_MAE_TOLERANCE_PCT:.0f} %-Punkte). "
            f"Grundlast-Skala {scale:.3f}, Akku-Wirkungsgrad {eff:.3f}."
        )
    eta = max(3, 14 - days)
    return (
        f"SoC lernt mit: Grundlast-Skala {scale:.3f}, Akku-Wirkungsgrad {eff:.3f}. "
        f"Trefferquote {acc:.0f} % · ca. {eta} Tage bis >90 % "
        f"(±{SOC_MAE_TOLERANCE_PCT:.0f} %-Punkte MAE)."
    )


def run_soc_calibration(
    state: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: str,
) -> Dict[str, Any]:
    """
    Vergleicht eingefrorene Morgen-SoC-Prognose mit Ist-SoC (Timeseries).
    Passt base_load_scale und battery_efficiency an.
    """
    ensure_soc_learning_defaults(state)
    today = date.today().isoformat()
    forecast_date = state.get("morning_soc_forecast_date")
    predicted = _forecast_map(state.get("morning_soc_forecast_hours"))
    adjustments: List[str] = []

    if forecast_date != today or not predicted:
        state["last_soc_calibration"] = datetime.now().isoformat()
        state["last_soc_calibration_note"] = "skipped_no_morning_soc_forecast"
        state["last_soc_calibration_adjustments"] = [
            "SoC-Kalibrierung übersprungen: keine Morgenprognose für heute."
        ]
        return state

    storage = float(config.get("storage_kwh") or 0)
    if storage <= 0:
        state["last_soc_calibration"] = datetime.now().isoformat()
        state["last_soc_calibration_note"] = "skipped_no_storage"
        return state

    actual_list = get_hourly_actual_soc_today(base_dir)
    pairs: List[Tuple[int, float, float]] = []
    for h, pred in sorted(predicted.items()):
        if h < 0 or h > 23:
            continue
        if h >= len(actual_list) or actual_list[h] is None:
            continue
        try:
            act = float(actual_list[h])
        except (TypeError, ValueError):
            continue
        pairs.append((h, float(pred), act))

    if len(pairs) < MIN_COMPARE_HOURS:
        state["last_soc_calibration"] = datetime.now().isoformat()
        state["last_soc_calibration_note"] = "skipped_few_samples"
        state["last_soc_calibration_adjustments"] = [
            f"SoC-Kalibrierung übersprungen: nur {len(pairs)} Stunden mit Prog./Ist "
            f"(mindestens {MIN_COMPARE_HOURS} nötig)."
        ]
        return state

    errors = [p - a for _, p, a in pairs]
    mae = sum(abs(e) for e in errors) / len(errors)
    mean_bias = sum(errors) / len(errors)  # >0 → Prognose zu hoch
    hit = mae <= SOC_MAE_TOLERANCE_PCT

    scale = normalize_base_load_scale(state.get("base_load_scale"))
    eff = normalize_battery_efficiency(state.get("battery_efficiency"))
    days = int(state.get("soc_days_since_reset") or 0)
    coarse = days == 0 or mae >= SOC_MAE_TOLERANCE_PCT * 2

    adjustments.append(
        f"SoC heute: MAE {mae:.1f} %-Punkte, Bias {mean_bias:+.1f} "
        f"({len(pairs)} Stunden, Prog.−Ist)."
    )

    if hit and abs(mean_bias) <= SOC_BIAS_TOLERANCE_PCT:
        adjustments.append(
            f"SoC-Prognose passt (±{SOC_MAE_TOLERANCE_PCT:.0f} %-Punkte MAE) – "
            f"Skala {scale:.3f}, Wirkungsgrad {eff:.3f} bleiben."
        )
    else:
        # Bias > 0: SoC zu hoch geschätzt → mehr Grundlast und/oder weniger Wirkungsgrad
        if abs(mean_bias) > SOC_BIAS_TOLERANCE_PCT:
            # ~2 % Skala pro 5 %-Punkte Bias, gedämpft
            scale_target = scale * (1.0 + max(-0.2, min(0.2, mean_bias / 100.0 * 4.0)))
            old_scale = scale
            scale = _adjust_blend(
                old_scale,
                scale_target,
                coarse,
                BASE_LOAD_SCALE_MIN,
                BASE_LOAD_SCALE_MAX,
            )
            if abs(scale - old_scale) >= 0.005:
                adjustments.append(
                    f"Grundlast-Skala: {old_scale:.3f} → {scale:.3f} "
                    f"(Bias {mean_bias:+.1f} %-Punkte)."
                )
            state["base_load_scale"] = scale

        # Ladephasen: wenn Ist-Anstieg hinter Prognose zurückbleibt → Wirkungsgrad senken
        charge_gaps: List[float] = []
        for i in range(1, len(pairs)):
            h0, p0, a0 = pairs[i - 1]
            h1, p1, a1 = pairs[i]
            if h1 != h0 + 1:
                continue
            pred_d = p1 - p0
            act_d = a1 - a0
            if pred_d > 1.0:
                charge_gaps.append(pred_d - act_d)
        if charge_gaps:
            avg_gap = sum(charge_gaps) / len(charge_gaps)
            if avg_gap > 1.5:
                # Zu optimistisch beim Laden
                eff_target = eff * (1.0 - min(0.12, avg_gap / 50.0))
                old_eff = eff
                eff = _adjust_blend(
                    old_eff,
                    eff_target,
                    coarse,
                    BATTERY_EFF_MIN,
                    BATTERY_EFF_MAX,
                )
                if abs(eff - old_eff) >= 0.005:
                    adjustments.append(
                        f"Akku-Wirkungsgrad: {old_eff:.3f} → {eff:.3f} "
                        f"(Lade-Δ zu hoch um {avg_gap:.1f} %-Punkte)."
                    )
                state["battery_efficiency"] = eff
            elif avg_gap < -1.5 and eff < BATTERY_EFF_MAX - 0.001:
                eff_target = eff * (1.0 + min(0.08, abs(avg_gap) / 60.0))
                old_eff = eff
                eff = _adjust_blend(
                    old_eff,
                    eff_target,
                    coarse,
                    BATTERY_EFF_MIN,
                    BATTERY_EFF_MAX,
                )
                if abs(eff - old_eff) >= 0.005:
                    adjustments.append(
                        f"Akku-Wirkungsgrad: {old_eff:.3f} → {eff:.3f} "
                        f"(Ist lädt stärker als Prognose)."
                    )
                state["battery_efficiency"] = eff

    history: List[Dict[str, Any]] = list(state.get("soc_history") or [])
    history.append(
        {
            "date": today,
            "mae_pct": round(mae, 2),
            "bias_pct": round(mean_bias, 2),
            "hit": hit,
            "compare_hours": len(pairs),
            "coarse": coarse,
            "base_load_scale": scale,
            "battery_efficiency": eff,
            "predicted_hours": { _hour_key(h): round(p, 2) for h, p, _ in pairs },
            "actual_hours": { _hour_key(h): round(a, 2) for h, _, a in pairs },
        }
    )
    history = history[-30:]
    state["soc_history"] = history
    state["soc_accuracy_score"] = compute_soc_accuracy_score(history)
    state["soc_days_since_reset"] = days + 1
    state["base_load_scale"] = scale
    state["battery_efficiency"] = eff
    state["last_soc_calibration"] = datetime.now().isoformat()
    state["last_soc_calibration_note"] = "ok"
    state["last_soc_calibration_adjustments"] = adjustments
    return state
