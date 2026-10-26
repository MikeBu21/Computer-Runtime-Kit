# -*- coding: utf-8 -*-
"""
Intraday-PV-Mustererkennung + adaptive Prognose-Korrektur.

1) Vergleicht heutige Morgenprognose / Ist-Kurve mit ähnlichen Tagen
   (yield_history) und schätzt den Tagesertrag neu.
2) Bei Pace-Abweichung über gelernter Schwelle (±~15 %) übernimmt die
   Musterschätzung die effektive Tagesprognose für die Steuerung.
3) Hysterese: wieder OMS, wenn Pace unter die Release-Schwelle fällt.
4) Abend-Lernen bewertet Muster vs. OMS und passt Schwellen/Trust an.
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.slot_profiles import HOUR_SLOT_END, HOUR_SLOT_START, hour_slot_ids


# Basis-Schwellen (werden an Anlagengröße / gelernte Params angepasst)
MIN_SIMILAR_PRED_KWH = 15.0
MIN_ACTUAL_SO_FAR_FRAC = 0.08
MIN_FORECAST_SO_FAR_FRAC = 0.10
MIN_ACTUAL_SO_FAR_ABS = 5.0
MIN_FORECAST_SO_FAR_ABS = 8.0
SIMILARITY_REL_MAX = 0.28  # ±28 % Tagesprognose
MAX_SHIFT_HOURS = 2
TOP_SIMILAR = 5

# Lernbare Default-Parameter (Prozentpunkte / Fraktionen)
DEFAULT_PACE_PARAMS: Dict[str, float] = {
    "engage_pct": 15.0,  # |Pace-Abweichung| ≥ → Musterschätzung
    "release_pct": 10.0,  # Hysterese: wieder OMS unter dieser |Abweichung|
    "positive_cap_pct": 15.0,  # max. Aufschlag vs. OMS bei Überproduktion
    "pattern_trust": 0.65,  # 0–1: Gewicht Muster vs. Pace-Skala
    "min_hour": 9.0,  # früheste volle Stunde für Trigger
    "min_forecast_so_far_frac": 0.12,  # Anteil Morgenprognose als Mindest-Daten
    "score": 55.0,
}

PACE_PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "engage_pct": (8.0, 28.0),
    "release_pct": (4.0, 20.0),
    "positive_cap_pct": (5.0, 25.0),
    "pattern_trust": (0.25, 0.95),
    "min_hour": (7.0, 12.0),
    "min_forecast_so_far_frac": (0.06, 0.25),
    "score": (0.0, 100.0),
}


def _hour_num(hid: str) -> int:
    try:
        return int(str(hid).lstrip("h"))
    except Exception:
        return -1


def _hours_dict(raw: Any) -> Dict[str, float]:
    out: Dict[str, float] = {hid: 0.0 for hid in hour_slot_ids()}
    if not isinstance(raw, dict):
        return out
    for hid in hour_slot_ids():
        try:
            out[hid] = max(0.0, float(raw.get(hid) or 0))
        except (TypeError, ValueError):
            out[hid] = 0.0
    return out


def _sum_hours(hours: Dict[str, float], until_hour_exclusive: int) -> float:
    total = 0.0
    for hid, v in hours.items():
        h = _hour_num(hid)
        if HOUR_SLOT_START <= h < until_hour_exclusive:
            total += float(v)
    return total


def _sum_hours_from(hours: Dict[str, float], from_hour_inclusive: int) -> float:
    total = 0.0
    for hid, v in hours.items():
        h = _hour_num(hid)
        if from_hour_inclusive <= h < HOUR_SLOT_END:
            total += float(v)
    return total


def _shift_hours(hours: Dict[str, float], shift: int) -> Dict[str, float]:
    """Verschiebt Stundenwerte um ±shift (positiv = später)."""
    out = {hid: 0.0 for hid in hour_slot_ids()}
    for hid, v in hours.items():
        h = _hour_num(hid) + shift
        if HOUR_SLOT_START <= h < HOUR_SLOT_END:
            out[f"h{h:02d}"] = float(v)
    return out


def _clamp_param(key: str, value: float) -> float:
    lo, hi = PACE_PARAM_BOUNDS[key]
    return round(max(lo, min(hi, float(value))), 4)


def _ema(old: float, target: float, alpha: float) -> float:
    return old * (1.0 - alpha) + target * alpha


def default_forecast_pace_learning() -> Dict[str, Any]:
    return {
        "params": dict(DEFAULT_PACE_PARAMS),
        "today": _empty_pace_today(date.today().isoformat()),
        "history": [],
        "last_run": None,
        "last_note": None,
        "last_adjustments": [],
    }


def _empty_pace_today(day: str) -> Dict[str, Any]:
    return {
        "date": day,
        "override_ticks": 0,
        "oms_ticks": 0,
        "last_effective_kwh": None,
        "last_pattern_kwh": None,
        "last_pace_ratio": None,
        "engaged_once": False,
    }


def ensure_forecast_pace_learning(state: Dict[str, Any]) -> Dict[str, Any]:
    fl = state.get("forecast_pace_learning")
    if not isinstance(fl, dict):
        fl = default_forecast_pace_learning()
        state["forecast_pace_learning"] = fl
    params = fl.get("params")
    if not isinstance(params, dict):
        params = dict(DEFAULT_PACE_PARAMS)
        fl["params"] = params
    for k, v in DEFAULT_PACE_PARAMS.items():
        if k not in params:
            params[k] = v
    today = date.today().isoformat()
    day = fl.get("today")
    if not isinstance(day, dict) or day.get("date") != today:
        fl["today"] = _empty_pace_today(today)
    state["forecast_pace_learning"] = fl
    return fl


def reset_forecast_pace_learning(state: Dict[str, Any], *, force: bool = False) -> None:
    """Pace-Lernen nur mit force=True zurücksetzen (kein Auto-Wipe)."""
    if not force:
        return
    state["forecast_pace_learning"] = default_forecast_pace_learning()
    state.pop("pv_forecast_override", None)
    state.pop("pv_effective_today_kwh", None)
    state.pop("pv_effective_today_hours", None)


def get_pace_params(state: Dict[str, Any]) -> Dict[str, float]:
    fl = ensure_forecast_pace_learning(state)
    raw = fl.get("params") or {}
    out: Dict[str, float] = {}
    for key, default in DEFAULT_PACE_PARAMS.items():
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            val = default
        out[key] = _clamp_param(key, val)
    # Release immer strikt unter Engage (Hysterese)
    if out["release_pct"] >= out["engage_pct"]:
        out["release_pct"] = _clamp_param(
            "release_pct", max(4.0, out["engage_pct"] - 4.0)
        )
    return out


def _adaptive_min_so_far(forecast_total: float, frac: float, abs_floor: float) -> float:
    return max(abs_floor, float(forecast_total) * float(frac))


def _shape_score(
    today_actual: Dict[str, float],
    hist_actual: Dict[str, float],
    until_hour: int,
) -> float:
    """Niedriger = besser. Relative RMSE über abgeschlossene Stunden."""
    err_sq = 0.0
    n = 0
    for hid in hour_slot_ids():
        h = _hour_num(hid)
        if h >= until_hour:
            continue
        a = float(today_actual.get(hid) or 0)
        b = float(hist_actual.get(hid) or 0)
        if a < 0.5 and b < 0.5:
            continue
        base = max(a, b, 1.0)
        rel = (a - b) / base
        err_sq += rel * rel
        n += 1
    if n < 3:
        return 9.0
    return err_sq / n


def _find_similar_days(
    forecast_total: float,
    history: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    scored: List[Tuple[float, Dict[str, Any]]] = []
    min_pred = max(MIN_SIMILAR_PRED_KWH, forecast_total * 0.35)
    for entry in history:
        pred = float(entry.get("predicted") or 0)
        act = float(entry.get("actual") or 0)
        if pred < min_pred * 0.5 or act < 5:
            continue
        if not entry.get("actual_hours"):
            continue
        rel = abs(pred - forecast_total) / max(forecast_total, 1.0)
        scored.append((rel, entry))
    scored.sort(key=lambda x: x[0])
    close = [e for rel, e in scored if rel <= SIMILARITY_REL_MAX][:TOP_SIMILAR]
    if close:
        return close
    return [e for _, e in scored[: min(3, len(scored))]]


def _best_shift_match(
    today_actual: Dict[str, float],
    hist_actual: Dict[str, float],
    until_hour: int,
) -> Tuple[int, float]:
    best_shift = 0
    best_score = 9.0
    for shift in range(-MAX_SHIFT_HOURS, MAX_SHIFT_HOURS + 1):
        shifted = _shift_hours(hist_actual, shift)
        score = _shape_score(today_actual, shifted, until_hour)
        if score < best_score:
            best_score = score
            best_shift = shift
    return best_shift, best_score


def _shape_day_estimate(
    today_actual: Dict[str, float],
    hist_actual: Dict[str, float],
    until_hour: int,
    shift: int,
    min_so_far: float,
) -> Optional[float]:
    """
    Skaliert die historische Ist-Tageskurve (verschoben) so, dass die
    bisherige Tagesmenge zu heute passt, und liefert die Tagesgesamtschätzung.
    """
    shifted = _shift_hours(hist_actual, shift)
    hist_so_far = _sum_hours(shifted, until_hour)
    today_so_far = _sum_hours(today_actual, until_hour)
    hist_full = sum(shifted.values())
    if hist_so_far < min_so_far or hist_full < min_so_far:
        return None
    if today_so_far < min_so_far:
        return None
    scale = today_so_far / hist_so_far
    if scale < 0.35 or scale > 2.2:
        return None
    return today_so_far + scale * (hist_full - hist_so_far)


def estimate_today_pv_from_patterns(
    *,
    forecast_total: Optional[float],
    forecast_hours: Optional[Dict[str, float]],
    actual_hours: Optional[Dict[str, float]],
    actual_total: Optional[float],
    yield_history: Optional[List[Dict[str, Any]]],
    now: Optional[datetime] = None,
    pace_params: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Liefert eine diagnostische Tages-Schätzung inkl. Begründung.

    Returns dict mit keys:
      estimated_kwh, oms_forecast_kwh, actual_so_far_kwh, pace_ratio,
      similar_days, hour_shift, method, note, confidence
    """
    now = now or datetime.now()
    until_hour = max(HOUR_SLOT_START, min(HOUR_SLOT_END, now.hour))
    fc_total = float(forecast_total or 0)
    fc_hours = _hours_dict(forecast_hours)
    act_hours = _hours_dict(actual_hours)
    act_live = float(actual_total) if actual_total is not None else None
    history = list(yield_history or [])
    params = pace_params or dict(DEFAULT_PACE_PARAMS)

    min_act = _adaptive_min_so_far(
        fc_total, MIN_ACTUAL_SO_FAR_FRAC, MIN_ACTUAL_SO_FAR_ABS
    )
    min_fc = _adaptive_min_so_far(
        fc_total, MIN_FORECAST_SO_FAR_FRAC, MIN_FORECAST_SO_FAR_ABS
    )

    empty = {
        "estimated_kwh": None,
        "oms_forecast_kwh": round(fc_total, 1) if fc_total else None,
        "actual_so_far_kwh": round(act_live, 1) if act_live is not None else None,
        "pace_ratio": None,
        "similar_days": [],
        "hour_shift": 0,
        "method": None,
        "note": "Zu wenig Daten für Muster-Schätzung.",
        "confidence": "low",
        "delta_vs_oms_kwh": None,
        "delta_vs_oms_pct": None,
    }
    if fc_total < MIN_SIMILAR_PRED_KWH:
        return empty

    forecast_so_far = _sum_hours(fc_hours, until_hour)
    actual_so_far = _sum_hours(act_hours, until_hour)
    if act_live is not None and act_live > actual_so_far:
        actual_so_far = act_live

    similar = _find_similar_days(fc_total, history)
    similar_info: List[Dict[str, Any]] = []
    shape_estimates: List[float] = []
    day_ratios: List[float] = []
    best_shift = 0
    best_shift_score = 9.0

    for entry in similar:
        pred = float(entry.get("predicted") or 0)
        act = float(entry.get("actual") or 0)
        if pred > 0:
            day_ratios.append(act / pred)
        hist_act = _hours_dict(entry.get("actual_hours"))
        shift, score = _best_shift_match(act_hours, hist_act, until_hour)
        if score < best_shift_score:
            best_shift_score = score
            best_shift = shift
        shape_est = _shape_day_estimate(
            act_hours, hist_act, until_hour, shift, min_act
        )
        similar_info.append(
            {
                "date": entry.get("date"),
                "predicted": round(pred, 1),
                "actual": round(act, 1),
                "ratio": round(act / pred, 3) if pred > 0 else None,
                "hour_shift": shift,
                "shape_score": round(score, 3),
                "shape_estimate_kwh": round(shape_est, 1) if shape_est is not None else None,
            }
        )
        if shape_est is not None:
            shape_estimates.append(shape_est)

    pace_ratio = None
    pace_estimate = None
    min_hour = int(params.get("min_hour") or 9)
    if (
        forecast_so_far >= min_fc
        and actual_so_far >= min_act * 0.7
        and until_hour >= min_hour
    ):
        pace_ratio = actual_so_far / forecast_so_far
        pace_ratio = max(0.35, min(1.65, pace_ratio))
        remaining = max(0.0, fc_total - forecast_so_far)
        pace_estimate = actual_so_far + remaining * pace_ratio

    similar_ratio = median(day_ratios) if day_ratios else None
    similar_estimate = (
        fc_total * similar_ratio if similar_ratio is not None else None
    )
    shape_median = median(shape_estimates) if shape_estimates else None

    parts: List[Tuple[float, float]] = []
    method_bits: List[str] = []

    late = until_hour >= 12
    mid = until_hour >= 10
    trust = float(params.get("pattern_trust") or 0.65)

    if pace_estimate is not None:
        w = 0.55 if late else (0.40 if mid else 0.25)
        parts.append((w, pace_estimate))
        method_bits.append("Tagespace")
    if shape_median is not None:
        w = (0.30 if late else (0.35 if mid else 0.25)) * trust
        parts.append((w, shape_median))
        method_bits.append("ähnliche Ist-Kurve")
    if similar_estimate is not None:
        w = (0.15 if late else (0.25 if mid else 0.50)) * trust
        parts.append((w, similar_estimate))
        method_bits.append("ähnliche Tagesquote")

    if until_hour < 11 and fc_total > 0:
        parts.append((0.35 if until_hour < 9 else 0.15, fc_total))
        method_bits.append("OMS")

    if not parts:
        empty["note"] = (
            "Muster-Schätzung wartet auf mehr Ist-Stunden "
            f"(bisher {actual_so_far:.0f} kWh, Prognose bis jetzt {forecast_so_far:.0f} kWh)."
        )
        empty["similar_days"] = similar_info
        empty["actual_so_far_kwh"] = round(actual_so_far, 1)
        empty["forecast_so_far_kwh"] = round(forecast_so_far, 1)
        return empty

    wsum = sum(w for w, _ in parts)
    estimated = sum(w * v for w, v in parts) / wsum

    if pace_ratio is not None and pace_ratio < 0.95:
        estimated = min(estimated, fc_total)
        estimated = min(estimated, pace_estimate if pace_estimate is not None else estimated)

    estimated = max(actual_so_far, estimated)
    delta = estimated - fc_total
    delta_pct = (delta / fc_total * 100.0) if fc_total else 0.0

    if abs(delta_pct) < 5:
        confidence = "medium" if similar_info else "low"
        verdict = "passt zur Prognose"
    elif delta_pct <= -8:
        confidence = "high" if (pace_ratio is not None and late) else "medium"
        verdict = "vermutlich zu hoch geschätzt"
    else:
        confidence = "medium"
        verdict = "vermutlich zu niedrig geschätzt"

    sim_txt = ""
    if similar_info:
        dates = ", ".join(str(s["date"]) for s in similar_info[:3] if s.get("date"))
        sim_txt = f" Ähnliche Tage: {dates}."

    shift_txt = ""
    if best_shift != 0 and best_shift_score < 0.35:
        direction = "später" if best_shift > 0 else "früher"
        shift_txt = f" Kurve ca. {abs(best_shift)} h {direction} als Historie."

    note = (
        f"Muster: ~{estimated:.0f} kWh ({verdict}, {delta_pct:+.0f} % vs. OMS "
        f"{fc_total:.0f} kWh). Methoden: {', '.join(method_bits)}."
        f"{sim_txt}{shift_txt}"
    )

    return {
        "estimated_kwh": round(estimated, 1),
        "oms_forecast_kwh": round(fc_total, 1),
        "actual_so_far_kwh": round(actual_so_far, 1),
        "forecast_so_far_kwh": round(forecast_so_far, 1),
        "pace_ratio": round(pace_ratio, 3) if pace_ratio is not None else None,
        "pace_estimate_kwh": round(pace_estimate, 1) if pace_estimate is not None else None,
        "similar_ratio": round(similar_ratio, 3) if similar_ratio is not None else None,
        "similar_estimate_kwh": round(similar_estimate, 1) if similar_estimate is not None else None,
        "shape_estimate_kwh": round(shape_median, 1) if shape_median is not None else None,
        "similar_days": similar_info,
        "hour_shift": best_shift,
        "method": "+".join(method_bits),
        "note": note,
        "confidence": confidence,
        "delta_vs_oms_kwh": round(delta, 1),
        "delta_vs_oms_pct": round(delta_pct, 1),
        "until_hour": until_hour,
    }


def _scale_remaining_hours(
    oms_hours: Dict[str, float],
    *,
    until_hour: int,
    actual_so_far: float,
    target_day_kwh: float,
) -> Dict[str, float]:
    """Vergangene Stunden ≈ Ist-Anteil, Zukunft skaliert auf Restziel."""
    out = _hours_dict(oms_hours)
    remaining_oms = _sum_hours_from(out, until_hour)
    target_remaining = max(0.0, float(target_day_kwh) - float(actual_so_far))
    if remaining_oms <= 0.05:
        # Kein Rest mehr → Ist als Tagesprofil belassen / Nullen
        for hid in hour_slot_ids():
            if _hour_num(hid) >= until_hour:
                out[hid] = 0.0
        return out
    scale = target_remaining / remaining_oms
    scale = max(0.15, min(2.5, scale))
    for hid in hour_slot_ids():
        if _hour_num(hid) >= until_hour:
            out[hid] = round(float(out[hid]) * scale, 3)
    return out


def _blend_effective_kwh(
    *,
    oms_kwh: float,
    pattern_kwh: Optional[float],
    pace_estimate: Optional[float],
    pace_ratio: Optional[float],
    trust: float,
    positive_cap_pct: float,
) -> Tuple[float, str]:
    """Mischt Muster- und Pace-Schätzung; deckelt positive Abweichung."""
    candidates: List[Tuple[float, float, str]] = []
    if pace_estimate is not None:
        candidates.append((1.0 - trust, pace_estimate, "pace"))
    if pattern_kwh is not None:
        candidates.append((trust, pattern_kwh, "pattern"))
    if not candidates:
        return oms_kwh, "oms"
    wsum = sum(w for w, _, _ in candidates)
    blended = sum(w * v for w, v, _ in candidates) / wsum
    source = "+".join(s for _, _, s in candidates)

    if pace_ratio is not None and pace_ratio < 1.0:
        # Defensiv: nie über OMS wenn Pace schon hinterher
        blended = min(blended, oms_kwh)
    else:
        cap = oms_kwh * (1.0 + positive_cap_pct / 100.0)
        blended = min(blended, cap)

    return max(0.0, blended), source


def apply_intraday_forecast_override(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Pace vs. Morgenprognose → bei |Abweichung| ≥ engage Musterschätzung,
    unter release wieder OMS. Schreibt effektive Stunden/Tages-kWh für Steuerung.
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    params = get_pace_params(state)
    fl = ensure_forecast_pace_learning(state)
    today_obs = fl["today"]

    oms_kwh = float(
        state.get("pv_yield_today_kwh") or state.get("morning_pv_forecast_kwh") or 0
    )
    morning_kwh = float(state.get("morning_pv_forecast_kwh") or oms_kwh or 0)
    # Pace-Referenz: aktuelle Wetter-OMS (Basis). Morgen-Freeze nur Fallback.
    oms_hours = _hours_dict(state.get("pv_forecast_today_hours"))
    morning_hours = _hours_dict(state.get("morning_forecast_hours"))
    if sum(oms_hours.values()) > 0.05:
        ref_hours = oms_hours
    else:
        ref_hours = morning_hours if sum(morning_hours.values()) > 0.05 else oms_hours
    act_hours = _hours_dict(state.get("hour_actual_today"))
    actual_so_far = float(state.get("live_pv_kwh_actual") or 0)
    hour_sum = _sum_hours(act_hours, now.hour)
    if hour_sum > actual_so_far:
        actual_so_far = hour_sum

    until_hour = max(HOUR_SLOT_START, min(HOUR_SLOT_END, now.hour))
    forecast_so_far = _sum_hours(ref_hours, until_hour)
    if forecast_so_far <= 0:
        forecast_so_far = _sum_hours(morning_hours, until_hour)

    pattern = state.get("pv_pattern_estimate") or {}
    prev = state.get("pv_forecast_override")
    if not isinstance(prev, dict) or prev.get("date") != today:
        prev = {"date": today, "active": False}

    min_fc = max(
        MIN_FORECAST_SO_FAR_ABS,
        max(oms_kwh, morning_kwh) * float(params["min_forecast_so_far_frac"]),
    )
    min_hour = int(params["min_hour"])
    engage = float(params["engage_pct"])
    release = float(params["release_pct"])

    pace_ratio = None
    deviation_pct = None
    enough_data = (
        oms_kwh >= MIN_SIMILAR_PRED_KWH
        and forecast_so_far >= min_fc
        and until_hour >= min_hour
        and actual_so_far >= max(3.0, min_fc * 0.35)
    )
    if enough_data and forecast_so_far > 0:
        pace_ratio = actual_so_far / forecast_so_far
        deviation_pct = (pace_ratio - 1.0) * 100.0

    was_active = bool(prev.get("active"))
    active = was_active
    reason = "within_band"

    if not enough_data:
        active = False
        reason = "insufficient_data"
    elif deviation_pct is not None:
        if was_active:
            if abs(deviation_pct) < release:
                active = False
                reason = f"release |{deviation_pct:.0f}%| < {release:.0f}%"
            else:
                active = True
                reason = f"hold |{deviation_pct:.0f}%| ≥ {release:.0f}%"
        else:
            if abs(deviation_pct) >= engage:
                active = True
                reason = f"engage |{deviation_pct:.0f}%| ≥ {engage:.0f}%"
            else:
                active = False
                reason = f"oms |{deviation_pct:.0f}%| < {engage:.0f}%"

    pattern_kwh = pattern.get("estimated_kwh")
    pace_estimate = pattern.get("pace_estimate_kwh")
    if pace_estimate is None and pace_ratio is not None and oms_kwh > 0:
        rem = max(0.0, oms_kwh - forecast_so_far)
        pace_estimate = actual_so_far + rem * max(0.35, min(1.65, pace_ratio))

    effective_kwh = oms_kwh
    source = "oms"
    if active:
        effective_kwh, source = _blend_effective_kwh(
            oms_kwh=oms_kwh,
            pattern_kwh=float(pattern_kwh) if pattern_kwh is not None else None,
            pace_estimate=float(pace_estimate) if pace_estimate is not None else None,
            pace_ratio=pace_ratio,
            trust=float(params["pattern_trust"]),
            positive_cap_pct=float(params["positive_cap_pct"]),
        )
        # Mindestens bisherige Ist-Menge
        effective_kwh = max(actual_so_far, effective_kwh)
        # Bei Unterproduktion: Muster darf OMS nicht nach oben ziehen
        if pace_ratio is not None and pace_ratio < 1.0:
            effective_kwh = min(effective_kwh, oms_kwh)

    if active:
        eff_hours = _scale_remaining_hours(
            oms_hours,
            until_hour=until_hour,
            actual_so_far=actual_so_far,
            target_day_kwh=effective_kwh,
        )
        # Vergangene Stunden grob an Ist anbinden (Summe bis until_hour)
        past_target = min(actual_so_far, effective_kwh)
        past_oms = _sum_hours(oms_hours, until_hour)
        if past_oms > 0.05:
            past_scale = past_target / past_oms
            for hid in hour_slot_ids():
                if _hour_num(hid) < until_hour:
                    eff_hours[hid] = round(float(oms_hours[hid]) * past_scale, 3)
        state["pv_effective_today_kwh"] = round(effective_kwh, 2)
        state["pv_effective_today_hours"] = eff_hours
        today_obs["override_ticks"] = int(today_obs.get("override_ticks") or 0) + 1
        today_obs["engaged_once"] = True
        today_obs["last_effective_kwh"] = round(effective_kwh, 2)
        if pattern_kwh is not None:
            today_obs["last_pattern_kwh"] = float(pattern_kwh)
    else:
        state["pv_effective_today_kwh"] = round(oms_kwh, 2) if oms_kwh else None
        state["pv_effective_today_hours"] = None
        today_obs["oms_ticks"] = int(today_obs.get("oms_ticks") or 0) + 1

    if pace_ratio is not None:
        today_obs["last_pace_ratio"] = round(pace_ratio, 4)

    override = {
        "date": today,
        "active": active,
        "reason": reason,
        "source": source if active else "oms",
        "pace_ratio": round(pace_ratio, 4) if pace_ratio is not None else None,
        "pace_deviation_pct": round(deviation_pct, 1) if deviation_pct is not None else None,
        "actual_so_far_kwh": round(actual_so_far, 2),
        "forecast_so_far_kwh": round(forecast_so_far, 2),
        "oms_kwh": round(oms_kwh, 2),
        "morning_kwh": round(morning_kwh, 2),
        "effective_kwh": round(effective_kwh, 2) if active else round(oms_kwh, 2),
        "pattern_kwh": round(float(pattern_kwh), 2) if pattern_kwh is not None else None,
        "engage_pct": engage,
        "release_pct": release,
        "positive_cap_pct": float(params["positive_cap_pct"]),
        "pattern_trust": float(params["pattern_trust"]),
        "enough_data": enough_data,
        "until_hour": until_hour,
        "updated_at": now.isoformat(timespec="seconds"),
        "activated_at": prev.get("activated_at")
        if was_active and active
        else (now.isoformat(timespec="seconds") if active else None),
    }
    state["pv_forecast_override"] = override
    fl["today"] = today_obs
    state["forecast_pace_learning"] = fl

    # Diagnose-Felder auch in Muster-Ergebnis spiegeln
    if isinstance(pattern, dict):
        pattern = dict(pattern)
        pattern["override_active"] = active
        pattern["override_reason"] = reason
        pattern["effective_kwh"] = override["effective_kwh"]
        pattern["pace_deviation_pct"] = override["pace_deviation_pct"]
        state["pv_pattern_estimate"] = pattern

    return state


def effective_today_pv_kwh(state: Dict[str, Any]) -> float:
    """Tages-PV für Steuerung: Override wenn aktiv, sonst OMS."""
    ov = state.get("pv_forecast_override") or {}
    if ov.get("active") and ov.get("effective_kwh") is not None:
        return float(ov["effective_kwh"])
    if state.get("pv_effective_today_kwh") is not None and ov.get("active"):
        return float(state["pv_effective_today_kwh"])
    return float(state.get("pv_yield_today_kwh") or 0)


def effective_today_hours(state: Dict[str, Any]) -> Optional[Dict[str, float]]:
    ov = state.get("pv_forecast_override") or {}
    if ov.get("active"):
        hours = state.get("pv_effective_today_hours")
        if isinstance(hours, dict) and hours:
            return dict(hours)
    return None


# Verlauf der Tages-Schätzungen für das 24-h-Liniendiagramm.
# Tick liegt bei 10 min; kürzer als 5 min wird der letzte Punkt nur nachgezogen.
TRACE_KEEP_SECONDS = 24 * 3600
TRACE_MIN_INTERVAL_SECONDS = 5 * 60
TRACE_MAX_POINTS = 320


def _parse_trace_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _trace_kwh(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return round(value, 1)


def record_pv_estimate_trace(
    state: Dict[str, Any], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Hält Erzeugungsschätzung (OMS) und Musterschätzung der letzten 24 h."""
    now = now or datetime.now()
    pattern = state.get("pv_pattern_estimate") or {}
    point = {
        "t": now.replace(microsecond=0).isoformat(timespec="seconds"),
        "oms": _trace_kwh(state.get("pv_yield_today_kwh")),
        "pattern": _trace_kwh(pattern.get("estimated_kwh"))
        if isinstance(pattern, dict)
        else None,
    }
    raw = state.get("pv_estimate_trace")
    samples: List[Dict[str, Any]] = []
    cutoff = now.timestamp() - TRACE_KEEP_SECONDS
    if isinstance(raw, list):
        for sample in raw:
            if not isinstance(sample, dict):
                continue
            ts = _parse_trace_ts(sample.get("t"))
            if ts is None or ts.timestamp() < cutoff:
                continue
            samples.append(
                {
                    "t": ts.replace(microsecond=0).isoformat(timespec="seconds"),
                    "oms": _trace_kwh(sample.get("oms")),
                    "pattern": _trace_kwh(sample.get("pattern")),
                }
            )
    if samples:
        last_ts = _parse_trace_ts(samples[-1].get("t"))
        if (
            last_ts is not None
            and (now - last_ts).total_seconds() < TRACE_MIN_INTERVAL_SECONDS
        ):
            samples[-1]["oms"] = point["oms"]
            samples[-1]["pattern"] = point["pattern"]
            state["pv_estimate_trace"] = samples[-TRACE_MAX_POINTS:]
            return state
    samples.append(point)
    state["pv_estimate_trace"] = samples[-TRACE_MAX_POINTS:]
    return state


def pv_estimate_trace_payload(
    state: Dict[str, Any],
    pattern_est: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Serie für die UI, rechts immer mit dem aktuellen Stand abgeschlossen."""
    now = now or datetime.now()
    samples: List[Dict[str, Any]] = []
    raw = state.get("pv_estimate_trace")
    cutoff = now.timestamp() - TRACE_KEEP_SECONDS
    if isinstance(raw, list):
        for sample in raw:
            if not isinstance(sample, dict):
                continue
            ts = _parse_trace_ts(sample.get("t"))
            if ts is None or ts.timestamp() < cutoff or ts > now:
                continue
            samples.append(
                {
                    "t": ts.replace(microsecond=0).isoformat(timespec="seconds"),
                    "oms": _trace_kwh(sample.get("oms")),
                    "pattern": _trace_kwh(sample.get("pattern")),
                }
            )
    if pattern_est is None:
        pattern_est = state.get("pv_pattern_estimate") or {}
    tip = {
        "t": now.replace(microsecond=0).isoformat(timespec="seconds"),
        "oms": _trace_kwh(state.get("pv_yield_today_kwh")),
        "pattern": _trace_kwh((pattern_est or {}).get("estimated_kwh"))
        if isinstance(pattern_est, dict)
        else None,
    }
    if tip["oms"] is None and tip["pattern"] is None:
        return samples
    if samples:
        last_ts = _parse_trace_ts(samples[-1].get("t"))
        if last_ts is not None and (now - last_ts).total_seconds() < 45:
            samples[-1] = tip
        else:
            samples.append(tip)
    else:
        samples.append(tip)
    return samples


def update_state_pattern_estimate(
    state: Dict[str, Any], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Berechnet Musterschätzung und wendet adaptive Override-Logik an."""
    params = get_pace_params(state)
    # Musterschätzung / Pace: aktuelle Wetter-OMS als Basis (Morgen nur Fallback)
    forecast_hours = state.get("pv_forecast_today_hours") or state.get(
        "morning_forecast_hours"
    )
    forecast_total = state.get("pv_yield_today_kwh") or state.get(
        "morning_pv_forecast_kwh"
    )
    result = estimate_today_pv_from_patterns(
        forecast_total=forecast_total,
        forecast_hours=forecast_hours,
        actual_hours=state.get("hour_actual_today"),
        actual_total=state.get("live_pv_kwh_actual"),
        yield_history=state.get("yield_history"),
        now=now,
        pace_params=params,
    )
    # delta vs. aktueller OMS zusätzlich ausweisen
    oms_now = float(state.get("pv_yield_today_kwh") or forecast_total or 0)
    if result.get("estimated_kwh") is not None and oms_now > 0:
        est = float(result["estimated_kwh"])
        result["delta_vs_live_oms_kwh"] = round(est - oms_now, 1)
        result["delta_vs_live_oms_pct"] = round((est - oms_now) / oms_now * 100.0, 1)
    state["pv_pattern_estimate"] = result
    apply_intraday_forecast_override(state, now=now)
    record_pv_estimate_trace(state, now=now)
    return state


def run_evening_forecast_pace_learning(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Abend: Musterschätzung vs. OMS bewerten → Schwellen/Trust/Score anpassen.
    """
    try:
        from control_algorithm.steering_learning import learning_blocked_by_pv_outage

        # config steckt nicht hier – Sperre über State/Tagesflag/PV-Safety
        blocked, block_reason = learning_blocked_by_pv_outage(
            {"has_pv": True, "steering_learning_block_on_pv_outage": True},
            state,
        )
        if blocked:
            fl = ensure_forecast_pace_learning(state)
            note = (
                f"übersprungen: {block_reason} – Pace-Parameter unverändert."
            )
            fl["last_run"] = datetime.now().isoformat(timespec="seconds")
            fl["last_note"] = "skipped_pv_outage"
            fl["last_adjustments"] = [note]
            state["forecast_pace_learning"] = fl
            return state
    except Exception:
        pass

    fl = ensure_forecast_pace_learning(state)
    params = get_pace_params(state)
    adjustments: List[str] = []
    today_obs = fl.get("today") or {}
    today = date.today().isoformat()

    morning = state.get("morning_pv_forecast_kwh")
    actual = state.get("live_pv_kwh_actual")
    pattern_est = today_obs.get("last_pattern_kwh") or (
        (state.get("pv_pattern_estimate") or {}).get("estimated_kwh")
    )
    last_effective = today_obs.get("last_effective_kwh")
    engaged = bool(today_obs.get("engaged_once"))

    try:
        morning_f = float(morning) if morning is not None else None
        actual_f = float(actual) if actual is not None else None
        pattern_f = float(pattern_est) if pattern_est is not None else None
        effective_f = float(last_effective) if last_effective is not None else None
    except (TypeError, ValueError):
        morning_f = actual_f = pattern_f = effective_f = None

    oms_err = None
    pattern_err = None
    effective_err = None
    if morning_f and actual_f and morning_f >= 5 and actual_f >= 5:
        oms_err = abs(actual_f - morning_f) / actual_f
    if pattern_f and actual_f and actual_f >= 5:
        pattern_err = abs(actual_f - pattern_f) / actual_f
    if effective_f and actual_f and actual_f >= 5 and engaged:
        effective_err = abs(actual_f - effective_f) / actual_f

    verdict = "stable"
    if oms_err is not None and pattern_err is not None and engaged:
        if pattern_err + 0.02 < oms_err:
            # Muster besser → sensibler + mehr Trust
            params["pattern_trust"] = _clamp_param(
                "pattern_trust",
                _ema(params["pattern_trust"], params["pattern_trust"] + 0.08, 0.55),
            )
            params["engage_pct"] = _clamp_param(
                "engage_pct",
                _ema(params["engage_pct"], params["engage_pct"] - 1.0, 0.45),
            )
            params["release_pct"] = _clamp_param(
                "release_pct",
                _ema(params["release_pct"], params["release_pct"] - 0.5, 0.4),
            )
            params["score"] = _clamp_param("score", params["score"] + 6)
            verdict = "pattern_better"
            adjustments.append(
                f"Musterschätzung näher am Ist (Fehler {pattern_err:.0%} vs. OMS "
                f"{oms_err:.0%}) → Trust {params['pattern_trust']:.2f}, "
                f"Engage {params['engage_pct']:.1f} %."
            )
        elif oms_err + 0.02 < pattern_err:
            params["pattern_trust"] = _clamp_param(
                "pattern_trust",
                _ema(params["pattern_trust"], params["pattern_trust"] - 0.08, 0.55),
            )
            params["engage_pct"] = _clamp_param(
                "engage_pct",
                _ema(params["engage_pct"], params["engage_pct"] + 1.5, 0.45),
            )
            params["release_pct"] = _clamp_param(
                "release_pct",
                min(params["engage_pct"] - 3.0, params["release_pct"] + 0.5),
            )
            params["score"] = _clamp_param("score", params["score"] - 5)
            verdict = "oms_better"
            adjustments.append(
                f"OMS näher am Ist als Muster (OMS {oms_err:.0%} vs. Muster "
                f"{pattern_err:.0%}) → vorsichtiger (Engage {params['engage_pct']:.1f} %)."
            )
        else:
            params["score"] = _clamp_param("score", params["score"] + 1)
            adjustments.append(
                f"Muster und OMS ähnlich gut (Fehler ~{oms_err:.0%}) – Parameter stabil."
            )
    elif oms_err is not None and not engaged:
        # Kein Override nötig – wenn OMS sehr falsch war, Schwelle senken
        if oms_err >= 0.20:
            params["engage_pct"] = _clamp_param(
                "engage_pct",
                _ema(params["engage_pct"], params["engage_pct"] - 1.5, 0.5),
            )
            params["score"] = _clamp_param("score", params["score"] - 3)
            verdict = "missed_engage"
            adjustments.append(
                f"OMS wich {oms_err:.0%} ab ohne Override → Engage gesenkt auf "
                f"{params['engage_pct']:.1f} %."
            )
        elif oms_err <= 0.10:
            params["score"] = _clamp_param("score", params["score"] + 2)
            verdict = "oms_ok"
            adjustments.append(
                f"OMS passte gut (±{oms_err:.0%}) ohne Muster-Override – Score +"
            )

    # Anlagengröße: frühere Trigger bei großen Anlagen (mehr kWh früher)
    if morning_f and morning_f >= 80:
        params["min_hour"] = _clamp_param(
            "min_hour", _ema(params["min_hour"], 8.5, 0.15)
        )
    elif morning_f and morning_f <= 40:
        params["min_hour"] = _clamp_param(
            "min_hour", _ema(params["min_hour"], 10.0, 0.15)
        )

    # Release immer unter Engage halten
    if params["release_pct"] >= params["engage_pct"]:
        params["release_pct"] = _clamp_param(
            "release_pct", max(4.0, params["engage_pct"] - 4.0)
        )

    history = list(fl.get("history") or [])
    history.append(
        {
            "date": today,
            "morning_kwh": round(morning_f, 2) if morning_f is not None else None,
            "actual_kwh": round(actual_f, 2) if actual_f is not None else None,
            "pattern_kwh": round(pattern_f, 2) if pattern_f is not None else None,
            "effective_kwh": round(effective_f, 2) if effective_f is not None else None,
            "oms_err": round(oms_err, 4) if oms_err is not None else None,
            "pattern_err": round(pattern_err, 4) if pattern_err is not None else None,
            "effective_err": round(effective_err, 4) if effective_err is not None else None,
            "engaged": engaged,
            "override_ticks": int(today_obs.get("override_ticks") or 0),
            "verdict": verdict,
            "params": dict(params),
            "adjustments": list(adjustments),
        }
    )
    fl["history"] = history[-30:]
    fl["params"] = params
    fl["last_run"] = datetime.now().isoformat(timespec="seconds")
    fl["last_note"] = verdict
    fl["last_adjustments"] = adjustments
    state["forecast_pace_learning"] = fl
    return state


def forecast_pace_info_messages(state: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    ov = state.get("pv_forecast_override") or {}
    params = get_pace_params(state)
    if ov.get("active"):
        msgs.append(
            f"PV-Override aktiv ({ov.get('source')}): effektiv "
            f"{ov.get('effective_kwh')} kWh statt OMS {ov.get('oms_kwh')} kWh "
            f"(Pace {ov.get('pace_deviation_pct')} %, "
            f"Engage/Release {params['engage_pct']:.0f}/{params['release_pct']:.0f} %)."
        )
    elif ov.get("pace_deviation_pct") is not None:
        msgs.append(
            f"PV-Pace {ov.get('pace_deviation_pct')} % vs. Morgenprognose "
            f"(Schwelle ±{params['engage_pct']:.0f} %, Hysterese "
            f"{params['release_pct']:.0f} %) – OMS bleibt führend."
        )
    fl = ensure_forecast_pace_learning(state)
    for a in (fl.get("last_adjustments") or [])[:2]:
        msgs.append(f"Prognose-Lernen: {a}")
    msgs.append(
        f"Prognose-Neuron: Score {params['score']:.0f}/100 · "
        f"Trust {params['pattern_trust']:.2f} · "
        f"Engage {params['engage_pct']:.1f} %"
    )
    return msgs
