# -*- coding: utf-8 -*-
"""Abend-Kalibrierung: OMS-Prognose vs. Ist-PV, Performance-Ratio lernen."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from control_algorithm.config import config_fingerprint
from control_algorithm.oms_pv_forecast import (
    PR_MAX,
    PR_MIN,
    normalize_performance_ratio,
)
from control_algorithm.reference_anchor import (
    anchor_status_message,
    is_reference_anchor_active,
    maybe_apply_pv_reference_change,
)
from control_algorithm.live_data import get_actual_pv_kwh_today
from control_algorithm.slot_profiles import (
    CALIBRATION_TOLERANCE,
    COARSE_ERROR_THRESHOLD,
    SLOT_FACTOR_MAX,
    SLOT_FACTOR_MIN,
    TIME_SLOTS,
    default_slot_factors,
    default_hour_slot_factors,
    format_hour_slot_summary,
    format_slot_summary,
    get_actual_pv_kwh_by_hours_today,
    get_actual_pv_kwh_by_slots_today,
    hour_slot_ids,
    hour_slot_label,
    normalize_hour_slot_factors,
    normalize_sun_hours,
    perfect_day_kwh,
)


def maybe_reset_on_config_change(
    state: Dict[str, Any], config: Dict[str, Any]
) -> tuple[Dict[str, Any], bool]:
    """
    Config-Fingerprint aktualisieren; kWp/Sonnenstunden → Referenz-Anker.

    Lerndaten (Steering-/Pace-/Yield-/SoC-History und Parameter) werden
    bewusst NICHT gelöscht – nur weiter vom Algo trainiert.
    """
    state, anchor_activated = maybe_apply_pv_reference_change(state, config)

    fp = config_fingerprint(config)
    if state.get("config_fingerprint") != fp:
        # Nur Fingerprint + Tages-Morgenprognose (kein Trainingsgut)
        state["config_fingerprint"] = fp
        state["morning_pv_forecast_kwh"] = None
        state["morning_forecast_slots"] = None
        state["morning_forecast_hours"] = None
        state["morning_forecast_date"] = None
        state["learning_preserved_on_config_change"] = True
        # Kein reset_steering_learning / reset_forecast_pace / yield_history-Wipe.
    if "performance_ratio" not in state:
        state["performance_ratio"] = normalize_performance_ratio(
            state.get("yield_factor", 1.0)
        )
    if "slot_factors" not in state:
        state["slot_factors"] = default_slot_factors()
    if "hour_slot_factors" not in state:
        state["hour_slot_factors"] = default_hour_slot_factors()
    return state, anchor_activated

def store_morning_forecast(
    state: Dict[str, Any],
    forecast_kwh: float,
    forecast_slots: Dict[str, float],
    day: Optional[date] = None,
    forecast_hours: Optional[Dict[str, float]] = None,
) -> None:
    """
    Speichert die aktuelle PV-Referenzprognose (früher: einmaliger Morgenanker).

    Wird bei jedem guten Wetter-Fetch (~alle 2 h) aktualisiert, damit Safety/Planung
    der Live-OMS folgen – nicht einem eingefrorenen Mitternachts-Wert.
    """
    d = day or date.today()
    state["morning_pv_forecast_kwh"] = round(float(forecast_kwh), 2)
    state["morning_forecast_slots"] = {k: round(float(v), 2) for k, v in forecast_slots.items()}
    if forecast_hours is not None:
        state["morning_forecast_hours"] = {
            k: round(float(v), 2) for k, v in forecast_hours.items()
        }
    state["morning_forecast_date"] = d.isoformat()


# Tageszähler vs. integrierte Stunden: >15 % Abweichung = hängende/lückenhafte Lesung.
INCONSISTENT_ACTUAL_MAX_REL = 0.15
# Mindestens so viele Stunden mit Ist, bevor Tages-/Stundensumme verglichen wird.
INCONSISTENT_MIN_HOUR_BINS = 8


def _sum_hour_kwh(hours: Optional[Dict[str, Any]]) -> float:
    return round(sum(float(v or 0) for v in (hours or {}).values()), 2)


def actual_totals_inconsistent(
    daily_kwh: float,
    actual_hours: Optional[Dict[str, Any]],
    max_rel: float = INCONSISTENT_ACTUAL_MAX_REL,
) -> bool:
    """True, wenn Kreisdiagramm-Tages-Ist und Stundensumme klar nicht zusammenpassen."""
    filled = [
        float(v or 0)
        for v in (actual_hours or {}).values()
        if float(v or 0) >= 0.1
    ]
    if len(filled) < INCONSISTENT_MIN_HOUR_BINS:
        return False
    hourly = _sum_hour_kwh(actual_hours)
    daily = float(daily_kwh or 0)
    if daily < 5 or hourly < 5:
        return False
    return abs(hourly - daily) / max(daily, hourly) > max_rel


def _relative_error(actual: float, predicted: float) -> float:
    if actual <= 0:
        return 1.0 if predicted > 0 else 0.0
    return abs(actual - predicted) / actual


def _adjust_performance_ratio(current: float, ratio: float, coarse: bool) -> float:
    if coarse:
        new_pr = current * 0.5 + ratio * 0.5
    else:
        new_pr = current * 0.7 + ratio * 0.3
    return round(max(PR_MIN, min(PR_MAX, new_pr)), 4)


def _adjust_hour_slot_factor(current: float, ratio: float, coarse: bool) -> float:
    if coarse:
        new_f = current * 0.5 + ratio * 0.5
    else:
        new_f = current * 0.7 + ratio * 0.3
    return round(max(SLOT_FACTOR_MIN, min(SLOT_FACTOR_MAX, new_f)), 4)


def run_evening_calibration(
    state: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: str,
    min_predicted_kwh: float = 1.0,
    min_actual_kwh: float = 5.0,
) -> Dict[str, Any]:
    """
    Vergleicht Morgen-Prognose (OMS, angepasst mit PR) mit Ist-PV (Kreisdiagramm).
    Passt Performance Ratio an wenn Abweichung > ±10 %.
    """
    predicted_total = state.get("morning_pv_forecast_kwh")
    predicted_slots: Dict[str, float] = dict(state.get("morning_forecast_slots") or {})
    predicted_hours: Dict[str, float] = dict(state.get("morning_forecast_hours") or {})
    forecast_date = state.get("morning_forecast_date")
    today = date.today().isoformat()

    if forecast_date != today or predicted_total is None:
        state["last_calibration"] = datetime.now().isoformat()
        state["last_calibration_note"] = "skipped_no_morning_forecast"
        return state

    actual_total = get_actual_pv_kwh_today(base_dir)
    actual_slots = get_actual_pv_kwh_by_slots_today(base_dir)
    actual_hours = get_actual_pv_kwh_by_hours_today(base_dir)

    if float(predicted_total) < min_predicted_kwh or actual_total < min_actual_kwh:
        state["last_calibration"] = datetime.now().isoformat()
        state["last_calibration_note"] = "skipped_low_values"
        return state

    total_err = _relative_error(actual_total, float(predicted_total))
    hit = total_err <= CALIBRATION_TOLERANCE
    days = int(state.get("days_since_reset", 0))
    # Grobe 50/50-Mischung nur am ersten Lerntag – später wäre ein 50 %-Fehler
    # eher Wetter/Messlücke als ein echter PR-Sprung der Anlage.
    coarse = days == 0

    pr = normalize_performance_ratio(state.get("performance_ratio"))
    hour_factors = normalize_hour_slot_factors(state.get("hour_slot_factors"))
    adjustments: List[str] = []

    anchor_msg = anchor_status_message(state, config)
    if anchor_msg:
        adjustments.append(anchor_msg)

    if actual_totals_inconsistent(actual_total, actual_hours):
        hourly_sum = _sum_hour_kwh(actual_hours)
        note = (
            f"Kalibrierung übersprungen: Tages-Ist {actual_total:.0f} kWh weicht von "
            f"Stundensumme {hourly_sum:.0f} kWh ab (Datenlesung unvollständig/hängend)."
        )
        adjustments.append(note)
        history: List[Dict[str, Any]] = list(state.get("yield_history") or [])
        history.append(
            {
                "date": today,
                "predicted": round(float(predicted_total), 2),
                "actual": round(actual_total, 2),
                "hourly_sum": hourly_sum,
                "error_ratio": round(total_err, 4),
                "hit": False,
                "excluded": True,
                "skipped_inconsistent_actual": True,
                "predicted_slots": predicted_slots,
                "actual_slots": actual_slots,
                "predicted_hours": predicted_hours,
                "actual_hours": actual_hours,
                "performance_ratio": pr,
                "hour_slot_factors": hour_factors,
            }
        )
        state["yield_history"] = history[-14:]
        state["last_calibration"] = datetime.now().isoformat()
        state["last_calibration_note"] = "skipped_inconsistent_actual"
        state["last_calibration_adjustments"] = adjustments
        state["last_calibration_slot_summary"] = format_slot_summary(
            predicted_slots, actual_slots
        )
        state["last_calibration_hour_summary"] = format_hour_slot_summary(
            predicted_hours, actual_hours, hour_factors
        )
        return state

    # Ausreißer (z. B. Schnee, Wechselrichter, hängende Lesung): PR nicht anfassen.
    skip_pr = (not hit) and days > 0 and total_err >= COARSE_ERROR_THRESHOLD

    if is_reference_anchor_active(state):
        state["last_calibration"] = datetime.now().isoformat()
        state["last_calibration_note"] = "skipped_reference_anchor_period"
        history: List[Dict[str, Any]] = list(state.get("yield_history") or [])
        history.append(
            {
                "date": today,
                "predicted": round(float(predicted_total), 2),
                "actual": round(actual_total, 2),
                "error_ratio": round(total_err, 4),
                "hit": hit,
                "predicted_slots": predicted_slots,
                "actual_slots": actual_slots,
                "predicted_hours": predicted_hours,
                "actual_hours": actual_hours,
                "anchor_period": True,
            }
        )
        state["yield_history"] = history[-14:]
        state["last_calibration_adjustments"] = adjustments
        return state

    if skip_pr:
        adjustments.append(
            f"PR unverändert {pr:.3f}: Tagesfehler {total_err:.0%} "
            f"(Ist {actual_total:.0f} / Prognose {float(predicted_total):.0f} kWh) "
            f"ist ein Ausreißer, kein Anlagensprung."
        )
    elif not hit:
        ratio = actual_total / float(predicted_total) if float(predicted_total) > 0 else 1.0
        old_pr = pr
        pr = _adjust_performance_ratio(old_pr, ratio, coarse)
        state["performance_ratio"] = pr
        state["yield_factor"] = pr
        if coarse:
            pv_kwp = float(config.get("pv_kwp") or 0)
            sun_h = normalize_sun_hours(config.get("perfect_weather_sun_hours"))
            perfect = perfect_day_kwh(pv_kwp, sun_h)
            adjustments.append(
                f"Grobe PR-Anpassung (Tag {days + 1}): Ist {actual_total:.0f} kWh vs. "
                f"Prognose {float(predicted_total):.0f} kWh "
                f"(Referenz Vollsonne: {perfect:.0f} kWh)."
            )
        adjustments.append(
            f"Performance Ratio: {old_pr:.3f} → {pr:.3f} "
            f"(Ist {actual_total:.1f} / Prognose {float(predicted_total):.1f} kWh, "
            f"ratio={ratio:.3f})."
        )
    else:
        adjustments.append(
            f"Tages-Prognose passt (±{CALIBRATION_TOLERANCE * 100:.0f} %): "
            f"Ist {actual_total:.1f} / Prognose {float(predicted_total):.1f} kWh – "
            f"PR bleibt {pr:.3f}."
        )

    if not skip_pr:
        min_hour_kwh = 0.1
        for hid in hour_slot_ids():
            pred_h = float(predicted_hours.get(hid, 0))
            act_h = float(actual_hours.get(hid, 0))
            if pred_h < min_hour_kwh or act_h < min_hour_kwh:
                continue
            hour_err = _relative_error(act_h, pred_h)
            if hour_err <= CALIBRATION_TOLERANCE:
                continue
            ratio = act_h / pred_h if pred_h > 0 else 1.0
            old_f = hour_factors[hid]
            hour_factors[hid] = _adjust_hour_slot_factor(old_f, ratio, coarse)
            adjustments.append(
                f"Stunde {hour_slot_label(hid)}: Faktor {old_f:.3f} → {hour_factors[hid]:.3f} "
                f"(Ist {act_h:.1f} / Prognose {pred_h:.1f} kWh)."
            )
        state["hour_slot_factors"] = hour_factors

    history: List[Dict[str, Any]] = list(state.get("yield_history") or [])
    history.append(
        {
            "date": today,
            "predicted": round(float(predicted_total), 2),
            "actual": round(actual_total, 2),
            "error_ratio": round(total_err, 4),
            "hit": hit,
            "predicted_slots": predicted_slots,
            "actual_slots": actual_slots,
            "predicted_hours": predicted_hours,
            "actual_hours": actual_hours,
            "coarse": coarse,
            "performance_ratio": pr,
            "hour_slot_factors": hour_factors,
            "excluded": bool(skip_pr),
        }
    )
    history = history[-14:]
    state["yield_history"] = history
    state["accuracy_score"] = compute_accuracy_score(history)
    state["days_since_reset"] = days + 1
    state["last_calibration"] = datetime.now().isoformat()
    state["last_calibration_note"] = (
        "skipped_outlier" if skip_pr else ("ok" if adjustments else "within_tolerance")
    )
    state["last_calibration_adjustments"] = adjustments
    state["last_calibration_slot_summary"] = format_slot_summary(predicted_slots, actual_slots)
    state["last_calibration_hour_summary"] = format_hour_slot_summary(
        predicted_hours, actual_hours, hour_factors
    )
    state["eta_days_to_90"] = estimate_days_to_90(state)
    return state


def compute_accuracy_score(history: List[Dict[str, Any]], window: int = 7) -> float:
    recent = [
        h
        for h in history
        if h.get("actual", 0) >= 5
        and not h.get("excluded")
        and not h.get("skipped_inconsistent_actual")
        and not h.get("calibration_reverted")
    ][-window:]
    if not recent:
        return 0.0
    hits = sum(1 for h in recent if h.get("hit"))
    return round(100.0 * hits / len(recent), 1)


def _previous_good_calibration(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Letzter brauchbarer Kalibrierstand vor dem letzten (vergifteten) Eintrag."""
    prior = history[:-1] if history else []
    for entry in reversed(prior):
        if entry.get("excluded") or entry.get("skipped_inconsistent_actual"):
            continue
        if entry.get("calibration_reverted"):
            continue
        pr = entry.get("performance_ratio")
        if pr is None:
            continue
        hours = entry.get("actual_hours") or {}
        if hours and actual_totals_inconsistent(float(entry.get("actual") or 0), hours):
            continue
        return entry
    return None


def maybe_revert_inconsistent_last_calibration(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Macht eine Abendkalibrierung rückgängig, die auf hängender Tageslesung
    (Tages-Ist klar unter Stundensumme) den PR nach unten gezogen hat.
    """
    history: List[Dict[str, Any]] = list(state.get("yield_history") or [])
    if not history:
        return state
    last = history[-1]
    if last.get("calibration_reverted") or last.get("skipped_inconsistent_actual"):
        return state
    daily = float(last.get("actual") or 0)
    hours = last.get("actual_hours") or {}
    if not actual_totals_inconsistent(daily, hours):
        return state
    prev = _previous_good_calibration(history)
    if prev is None:
        return state
    old_pr = normalize_performance_ratio(state.get("performance_ratio"))
    restore_pr = normalize_performance_ratio(prev.get("performance_ratio"))
    hourly_sum = _sum_hour_kwh(hours)
    state["performance_ratio"] = restore_pr
    state["yield_factor"] = restore_pr
    if prev.get("hour_slot_factors"):
        state["hour_slot_factors"] = normalize_hour_slot_factors(prev.get("hour_slot_factors"))
    last["calibration_reverted"] = True
    last["excluded"] = True
    last["reverted_reason"] = "inconsistent_actual_vs_hours"
    last["hourly_sum"] = hourly_sum
    history[-1] = last
    state["yield_history"] = history
    state["accuracy_score"] = compute_accuracy_score(history)
    state["last_calibration"] = datetime.now().isoformat()
    state["last_calibration_note"] = "reverted_inconsistent_actual"
    state["last_calibration_adjustments"] = [
        f"Kalibrierung {last.get('date')} verworfen: Tages-Ist {daily:.0f} kWh "
        f"passt nicht zur Stundensumme {hourly_sum:.0f} kWh (Datenlesung unvollständig). "
        f"PR {old_pr:.3f} → {restore_pr:.3f} (Stand {prev.get('date')})."
    ]
    # Morgen-Anker neu setzen, sonst bleibt die gedrückte Prognose für heute stehen.
    state["morning_forecast_date"] = None
    state["force_weather_refresh"] = True
    return state


def estimate_days_to_90(state: Dict[str, Any]) -> int:
    acc = float(state.get("accuracy_score", 0))
    days = int(state.get("days_since_reset", 0))
    if acc >= 90:
        return max(1, 3 - min(days, 2))
    base = max(3, 14 - days)
    if acc >= 85:
        return min(base, 3)
    return min(28, base + max(0, 3 - days // 3))


def eta_message(state: Dict[str, Any]) -> str:
    days = int(state.get("eta_days_to_90", 14))
    acc = state.get("accuracy_score", 0)
    pr = normalize_performance_ratio(state.get("performance_ratio"))
    if acc and float(acc) >= 90:
        return (
            f"Trefferquote über 90 %. Performance Ratio (PR): {pr:.3f}. "
            f"Nach Parameteränderung erneut ca. {days} Tage bis zur Quote."
        )
    return (
        f"PV-Prognose: open-meteo-solar-forecast mit PR {pr:.3f} "
        f"und 18 Stunden-Faktoren (05–22 Uhr). "
        f"Ca. {days} Tage bis >90 % Trefferquote (±10 % Toleranz)."
    )
