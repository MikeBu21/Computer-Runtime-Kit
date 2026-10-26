# -*- coding: utf-8 -*-
"""
Intraday-Verbrauchs-Mustererkennung + adaptive Plan-Korrektur.

Analog zum PV-Musterschätzer:
  1) Pace: Ist-Grundverbrauch vs. historischer Erwartung bis jetzt
  2) Bei |Abweichung| ≥ engage → Override skaliert den Plan-Verbrauch
  3) Unter release wieder Historie (Hysterese)
  4) Unterverbrauch → Plan nach unten → mehr Miner-Budget
     Mehrverbrauch → Plan nach oben (gedeckelt) → konservativer
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.consumption_history import (
    actual_base_consumption_kwh,
    expected_base_consumption_kwh,
    expected_base_full_day_kwh,
)


DEFAULT_PARAMS: Dict[str, float] = {
    "engage_pct": 15.0,
    "release_pct": 10.0,
    "negative_cap_pct": 25.0,  # max. Absenkung bei Unterverbrauch
    "positive_cap_pct": 15.0,  # max. Aufschlag bei Mehrverbrauch
    "min_hour": 8.0,
    "min_expected_so_far_kwh": 8.0,
    "pattern_trust": 0.70,
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normalize_enabled(raw: Any, default: bool = True) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    s = str(raw).strip().lower()
    return s in ("1", "true", "yes", "on", "ja")


def get_pattern_params(config: Dict[str, Any]) -> Dict[str, float]:
    """Liest konfigurierbare Schwellen (Dashboard), Fallback Defaults."""
    out = dict(DEFAULT_PARAMS)

    def _f(key: str, cfg_key: str, lo: float, hi: float) -> None:
        raw = config.get(cfg_key)
        if raw is None:
            return
        try:
            out[key] = _clamp(float(raw), lo, hi)
        except (TypeError, ValueError):
            pass

    _f("engage_pct", "consumption_pattern_engage_pct", 5.0, 40.0)
    _f("release_pct", "consumption_pattern_release_pct", 3.0, 30.0)
    _f("negative_cap_pct", "consumption_pattern_negative_cap_pct", 5.0, 50.0)
    _f("positive_cap_pct", "consumption_pattern_positive_cap_pct", 5.0, 40.0)
    _f("min_hour", "consumption_pattern_min_hour", 6.0, 14.0)
    _f("min_expected_so_far_kwh", "consumption_pattern_min_expected_so_far_kwh", 3.0, 80.0)
    _f("pattern_trust", "consumption_pattern_trust", 0.25, 0.95)

    # Release muss unter Engage bleiben
    if out["release_pct"] >= out["engage_pct"]:
        out["release_pct"] = max(3.0, out["engage_pct"] - 3.0)
    return out


def estimate_today_consumption_from_patterns(
    base_dir: str,
    *,
    now: Optional[datetime] = None,
    forecast_base_kwh: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Pace-Muster für heutigen Grundverbrauch (ohne schaltbare Geräte).
    Returns Dict mit estimated_kwh, pace_ratio, delta_vs_forecast_pct, note, …
    """
    now = now or datetime.now()
    empty: Dict[str, Any] = {
        "estimated_kwh": None,
        "forecast_kwh": None,
        "delta_vs_forecast_pct": None,
        "pace_ratio": None,
        "pace_estimate_kwh": None,
        "actual_so_far_kwh": None,
        "expected_so_far_kwh": None,
        "note": "Zu wenig Daten für Verbrauchs-Musterschätzung.",
    }

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fc = float(forecast_base_kwh) if forecast_base_kwh is not None else float(
        expected_base_full_day_kwh(base_dir, now.date()) or 0
    )
    if fc <= 0.5:
        empty["note"] = "Keine Tages-Prognose für Grundverbrauch verfügbar."
        return empty

    expected_so_far = float(expected_base_consumption_kwh(base_dir, midnight, now) or 0)
    actual_so_far = float(actual_base_consumption_kwh(base_dir, midnight, now) or 0)
    empty["forecast_kwh"] = round(fc, 1)
    empty["actual_so_far_kwh"] = round(actual_so_far, 1)
    empty["expected_so_far_kwh"] = round(expected_so_far, 1)

    until_hour = now.hour + now.minute / 60.0
    if expected_so_far < 3.0 or until_hour < 7.0:
        empty["note"] = (
            "Muster-Schätzung wartet auf mehr Ist-Stunden "
            f"(bisher {actual_so_far:.0f} kWh, erwartet bis jetzt "
            f"{expected_so_far:.0f} kWh)."
        )
        return empty

    pace = actual_so_far / max(expected_so_far, 0.01)
    pace = _clamp(pace, 0.40, 1.60)
    remaining = max(0.0, fc - expected_so_far)
    pace_estimate = actual_so_far + remaining * pace
    estimated = max(actual_so_far, pace_estimate)
    delta_pct = ((estimated - fc) / fc * 100.0) if fc > 0.05 else 0.0

    if abs(delta_pct) < 5:
        verdict = "passt zur Prognose"
    elif delta_pct <= -8:
        verdict = "vermutlich zu hoch geschätzt (Unterverbrauch)"
    else:
        verdict = "vermutlich zu niedrig geschätzt (Mehrverbrauch)"

    note = (
        f"Muster: ~{estimated:.0f} kWh ({verdict}, {delta_pct:+.0f} % vs. Plan "
        f"{fc:.0f} kWh). Pace Grundlast {pace * 100:.0f} % · "
        f"bisher {actual_so_far:.0f} kWh (erwartet {expected_so_far:.0f})."
    )
    return {
        "estimated_kwh": round(estimated, 1),
        "forecast_kwh": round(fc, 1),
        "delta_vs_forecast_pct": round(delta_pct, 1),
        "pace_ratio": round(pace, 3),
        "pace_estimate_kwh": round(pace_estimate, 1),
        "actual_so_far_kwh": round(actual_so_far, 1),
        "expected_so_far_kwh": round(expected_so_far, 1),
        "note": note,
        "until_hour": round(until_hour, 2),
    }


def _blend_scale(
    *,
    pace_ratio: float,
    trust: float,
    negative_cap_pct: float,
    positive_cap_pct: float,
) -> Tuple[float, str]:
    """
    Skala für Plan-Verbrauch ab jetzt.
    pace < 1 → Unterverbrauch → scale < 1 (mehr Budget).
    """
    # pace-basiert, leicht Richtung 1.0 gezogen wenn Trust niedrig
    raw = 1.0 + (pace_ratio - 1.0) * trust
    if pace_ratio < 1.0:
        lo = 1.0 - negative_cap_pct / 100.0
        scale = max(lo, raw)
        return round(scale, 4), "under_pace"
    hi = 1.0 + positive_cap_pct / 100.0
    scale = min(hi, raw)
    return round(scale, 4), "over_pace"


def apply_intraday_consumption_override(
    state: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Pace vs. Historie → bei |Abweichung| ≥ engage Override (Scale),
    unter release wieder 1.0×. Schreibt consumption_pattern_override.
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    enabled = normalize_enabled(config.get("consumption_pattern_enabled"), True)
    params = get_pattern_params(config)

    pattern = state.get("consumption_pattern_estimate") or {}
    prev = state.get("consumption_pattern_override")
    if not isinstance(prev, dict) or prev.get("date") != today:
        prev = {"date": today, "active": False}

    empty_ov = {
        "date": today,
        "active": False,
        "effective_scale": 1.0,
        "pace_ratio": None,
        "pace_deviation_pct": None,
        "engage_pct": params["engage_pct"],
        "release_pct": params["release_pct"],
        "reason": "disabled" if not enabled else "init",
        "estimated_kwh": None,
        "forecast_kwh": None,
    }

    if not enabled:
        state["consumption_pattern_override"] = empty_ov
        return state

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    expected_so_far = float(expected_base_consumption_kwh(base_dir, midnight, now) or 0)
    actual_so_far = float(actual_base_consumption_kwh(base_dir, midnight, now) or 0)
    until_hour = now.hour + now.minute / 60.0
    min_hour = float(params["min_hour"])
    min_fc = float(params["min_expected_so_far_kwh"])

    pace_ratio = None
    deviation_pct = None
    enough = (
        expected_so_far >= min_fc
        and until_hour >= min_hour
        and actual_so_far >= max(2.0, min_fc * 0.25)
    )
    if enough and expected_so_far > 0:
        pace_ratio = actual_so_far / expected_so_far
        pace_ratio = _clamp(pace_ratio, 0.35, 1.80)
        deviation_pct = (pace_ratio - 1.0) * 100.0

    was_active = bool(prev.get("active"))
    active = was_active
    reason = "within_band"
    engage = float(params["engage_pct"])
    release = float(params["release_pct"])

    if not enough:
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
                reason = f"history |{deviation_pct:.0f}%| < {engage:.0f}%"

    scale = 1.0
    source = "history"
    if active and pace_ratio is not None:
        scale, source = _blend_scale(
            pace_ratio=pace_ratio,
            trust=float(params["pattern_trust"]),
            negative_cap_pct=float(params["negative_cap_pct"]),
            positive_cap_pct=float(params["positive_cap_pct"]),
        )

    est_kwh = pattern.get("estimated_kwh")
    fc_kwh = pattern.get("forecast_kwh")
    state["consumption_pattern_override"] = {
        "date": today,
        "active": active,
        "effective_scale": round(scale, 4),
        "pace_ratio": round(pace_ratio, 3) if pace_ratio is not None else None,
        "pace_deviation_pct": round(deviation_pct, 1) if deviation_pct is not None else None,
        "engage_pct": engage,
        "release_pct": release,
        "reason": reason,
        "source": source,
        "estimated_kwh": est_kwh,
        "forecast_kwh": fc_kwh,
        "actual_so_far_kwh": round(actual_so_far, 1),
        "expected_so_far_kwh": round(expected_so_far, 1),
        "params": params,
    }
    return state


def consumption_pattern_scale(
    state: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> float:
    """Multiplikator für Plan-Grundverbrauch (1.0 = unverändert)."""
    if not state:
        return 1.0
    if config is not None and not normalize_enabled(
        config.get("consumption_pattern_enabled"), True
    ):
        return 1.0
    ov = state.get("consumption_pattern_override") or {}
    if not ov.get("active"):
        return 1.0
    try:
        s = float(ov.get("effective_scale") or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return _clamp(s, 0.40, 1.60)


def update_state_consumption_pattern(
    state: Dict[str, Any],
    config: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Berechnet Musterschätzung und wendet Override an."""
    now = now or datetime.now()
    try:
        fc = expected_base_full_day_kwh(base_dir, now.date())
    except Exception:
        fc = None
    result = estimate_today_consumption_from_patterns(
        base_dir, now=now, forecast_base_kwh=fc
    )
    state["consumption_pattern_estimate"] = result
    apply_intraday_consumption_override(state, config, base_dir, now=now)
    return state


def consumption_pattern_info_messages(state: Dict[str, Any]) -> List[str]:
    """Kurze Statuszeilen für Tick-Log / UI."""
    lines: List[str] = []
    est = state.get("consumption_pattern_estimate") or {}
    ov = state.get("consumption_pattern_override") or {}
    if est.get("note") and est.get("estimated_kwh") is not None:
        lines.append(f"Verbrauchs-Muster: {est['note']}")
    elif est.get("note"):
        lines.append(f"Verbrauchs-Muster: {est['note']}")
    if ov.get("active"):
        scale = ov.get("effective_scale")
        dev = ov.get("pace_deviation_pct")
        lines.append(
            f"Verbrauchs-Override aktiv: Plan ×{scale} "
            f"(Pace {dev:+.0f} % · {ov.get('reason')})."
        )
    elif ov.get("pace_deviation_pct") is not None:
        lines.append(
            f"Verbrauchs-Pace {ov['pace_deviation_pct']:+.0f} % "
            f"(Schwelle ±{ov.get('engage_pct', 15):.0f} %) – Historie bleibt."
        )
    return lines
