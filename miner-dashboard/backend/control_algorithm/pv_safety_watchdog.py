# -*- coding: utf-8 -*-
"""
PV-Sicherheits-Watchdog (3. Wetter-Sicherheitslayer).

Layer-Überblick:
  1) Pace-Override (±~15 %): Musterschätzung korrigiert die Tagesprognose
     (nur Sicherheit bei Schnee/String-Ausfall)
  2) Survival / Nacht-Sperre: Mehrtages-Schlechtwetter / SOC-Checkpoints
  3) Dieser Watchdog (−~20 %): harte Sicherheitsabschaltung + Stunden-Recheck

Pace-Referenz ist immer die **aktuelle Wetter-OMS**, nicht die Morgen-Freeze.
Bei Max-SoC / Überlauf weicht dieser Layer dem Batterie-Schutz.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.planner import enabled_devices
from control_algorithm.slot_profiles import HOUR_SLOT_END, HOUR_SLOT_START, hour_slot_ids


# Nach einem Clear nicht sofort neu triggern (sonst Flip-Flop im selben Zug)
PV_SAFETY_COOLDOWN_MINUTES = 30.0
# Deadband unter der Plan-Decke, damit der Vorrang nicht an der Kante pendelt
PLAN_BAND_HYSTERESIS_PCT = 1.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normalize_enabled(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    s = str(raw).strip().lower()
    return s in ("1", "true", "yes", "on", "ja")


def normalize_threshold_percent(raw: Any, default: float = 20.0) -> float:
    try:
        v = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        v = default
    return _clamp(v, 10.0, 60.0)


def normalize_clear_percent(raw: Any, default: float = 12.0) -> float:
    try:
        v = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        v = default
    return _clamp(v, 5.0, 50.0)


def normalize_recheck_minutes(raw: Any, default: float = 60.0) -> float:
    try:
        v = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        v = default
    return _clamp(v, 15.0, 180.0)


def normalize_min_hour(raw: Any, default: float = 9.0) -> int:
    try:
        v = int(float(raw if raw is not None else default))
    except (TypeError, ValueError):
        v = int(default)
    return int(_clamp(v, 7, 14))


def normalize_confirm_hours(raw: Any, default: float = 2.0) -> int:
    try:
        v = int(float(raw if raw is not None else default))
    except (TypeError, ValueError):
        v = int(default)
    return int(_clamp(v, 1, 4))


def normalize_min_forecast_so_far(raw: Any, default: float = 8.0) -> float:
    try:
        v = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        v = default
    return _clamp(v, 3.0, 80.0)


def _hour_num(hid: str) -> int:
    try:
        return int(str(hid).lstrip("h"))
    except Exception:
        return -1


def _hours_dict(raw: Any) -> Dict[str, float]:
    out = {hid: 0.0 for hid in hour_slot_ids()}
    if not isinstance(raw, dict):
        return out
    for hid in hour_slot_ids():
        try:
            out[hid] = max(0.0, float(raw.get(hid) or 0))
        except (TypeError, ValueError):
            out[hid] = 0.0
    return out


def _sum_range(hours: Dict[str, float], start: int, end_exclusive: int) -> float:
    total = 0.0
    for hid, v in hours.items():
        h = _hour_num(hid)
        if start <= h < end_exclusive:
            total += float(v)
    return total


def _minutes_since(iso_ts: Optional[str], now: datetime) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        started = datetime.fromisoformat(str(iso_ts))
        return (now - started).total_seconds() / 60.0
    except Exception:
        return None


def _pace_deviation_pct(actual: float, forecast: float) -> Optional[float]:
    if forecast <= 0.05:
        return None
    return (actual / forecast - 1.0) * 100.0


def compute_pv_pace_signals(
    state: Dict[str, Any],
    live: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    confirm_hours: int = 2,
) -> Dict[str, Any]:
    """Tages-Pace, Kurzfenster-Pace und Live-Stress-Signale.

    Referenz ist immer die **aktuelle Wetter-OMS** (`pv_forecast_today_hours`),
    nicht die eingefrorene Morgenprognose. Muster/Override ist nur Stress-Hinweis
    (Schnee/String), nicht die Pace-Basis.
    """
    now = now or datetime.now()
    until = max(HOUR_SLOT_START, min(HOUR_SLOT_END, now.hour))
    # Aktuelle OMS zuerst; Morgen-Freeze nur Fallback wenn OMS-Stunden fehlen
    oms_hours = _hours_dict(state.get("pv_forecast_today_hours"))
    morning_hours = _hours_dict(state.get("morning_forecast_hours"))
    if sum(oms_hours.values()) > 0.05:
        ref_hours = oms_hours
        ref_source = "oms_current"
    else:
        ref_hours = morning_hours if sum(morning_hours.values()) > 0.05 else oms_hours
        ref_source = "morning_freeze" if sum(morning_hours.values()) > 0.05 else "oms_current"

    act_hours = _hours_dict(state.get("hour_actual_today"))
    actual_so_far = float(state.get("live_pv_kwh_actual") or 0)
    hour_sum = _sum_range(act_hours, HOUR_SLOT_START, until)
    if hour_sum > actual_so_far:
        actual_so_far = hour_sum

    forecast_so_far = _sum_range(ref_hours, HOUR_SLOT_START, until)
    day_dev = _pace_deviation_pct(actual_so_far, forecast_so_far)

    win_start = max(HOUR_SLOT_START, until - max(1, confirm_hours))
    fc_win = _sum_range(ref_hours, win_start, until)
    act_win = _sum_range(act_hours, win_start, until)
    window_dev = _pace_deviation_pct(act_win, fc_win)

    # Erwartete Leistung aktuelle Stunde (kWh/h ≈ kW Mittel)
    hid = f"h{until:02d}" if until < HOUR_SLOT_END else None
    expected_kw = float(ref_hours.get(hid) or 0) if hid else 0.0
    live_kw = float(live.get("pv_kw") or state.get("live_pv_kw") or 0)
    live_ratio = (live_kw / expected_kw) if expected_kw >= 0.4 else None

    override = state.get("pv_forecast_override") or {}
    override_active = bool(override.get("active"))

    soc_now = live.get("soc_percent")
    if soc_now is None:
        soc_now = state.get("live_soc")
    soc_prev = state.get("pv_safety_last_soc")
    soc_falling = False
    if soc_now is not None and soc_prev is not None:
        try:
            soc_falling = float(soc_now) < float(soc_prev) - 0.4
        except (TypeError, ValueError):
            soc_falling = False

    stress = bool(
        override_active
        or (live_ratio is not None and live_ratio < 0.45)
        or soc_falling
        or (day_dev is not None and day_dev <= -35.0)
    )

    return {
        "until_hour": until,
        "actual_so_far_kwh": round(actual_so_far, 2),
        "forecast_so_far_kwh": round(forecast_so_far, 2),
        "forecast_ref": ref_source,
        "day_deviation_pct": round(day_dev, 1) if day_dev is not None else None,
        "window_start_hour": win_start,
        "window_actual_kwh": round(act_win, 2),
        "window_forecast_kwh": round(fc_win, 2),
        "window_deviation_pct": round(window_dev, 1) if window_dev is not None else None,
        "live_pv_kw": round(live_kw, 2),
        "expected_hour_kw": round(expected_kw, 2),
        "live_ratio": round(live_ratio, 3) if live_ratio is not None else None,
        "override_active": override_active,
        "soc_falling": soc_falling,
        "stress": stress,
        "soc_now": float(soc_now) if soc_now is not None else None,
    }


def _should_trigger(
    signals: Dict[str, Any],
    *,
    threshold: float,
    min_hour: int,
    min_forecast_so_far: float,
) -> Tuple[bool, str]:
    if int(signals.get("until_hour") or 0) < min_hour:
        return False, "too_early"
    if float(signals.get("forecast_so_far_kwh") or 0) < min_forecast_so_far:
        return False, "insufficient_forecast"
    day_dev = signals.get("day_deviation_pct")
    win_dev = signals.get("window_deviation_pct")
    if day_dev is None:
        return False, "no_day_pace"
    if day_dev > -threshold:
        return False, "day_pace_ok"
    # Kurzfenster muss mitziehen (etwas weicher: 85 % der Schwelle)
    win_need = -threshold * 0.85
    if win_dev is None or win_dev > win_need:
        return False, "window_not_confirmed"
    if not signals.get("stress"):
        return False, "no_stress_signal"
    return True, (
        f"day {day_dev:.0f}% / window {win_dev:.0f}% / stress "
        f"(override={signals.get('override_active')}, "
        f"live_ratio={signals.get('live_ratio')}, "
        f"soc_falling={signals.get('soc_falling')})"
    )


def _should_clear(
    signals: Dict[str, Any],
    *,
    clear_pct: float,
) -> Tuple[bool, str]:
    day_dev = signals.get("day_deviation_pct")
    win_dev = signals.get("window_deviation_pct")
    live_ratio = signals.get("live_ratio")
    if day_dev is None:
        return False, "no_pace"
    # Primär: Tages-Pace wieder über Clear-Schwelle
    if day_dev >= -clear_pct:
        # Kurzfenster sollte nicht weiter stark negativ sein
        if win_dev is None or win_dev >= -clear_pct - 5:
            return True, f"recovered day {day_dev:.0f}% / window {win_dev}"
    # Spektakuläre Live-Erholung (Sonne kommt durch Wolken/Schnee)
    if (
        live_ratio is not None
        and live_ratio >= 0.75
        and day_dev >= -clear_pct - 8
        and (win_dev is None or win_dev >= -clear_pct - 5)
    ):
        return True, f"live recovery ratio {live_ratio:.0%}, day {day_dev:.0f}%"
    return False, "still_underperforming"


def _pause_all_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    reason: str,
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    for d in enabled_devices(config):
        ip = d.get("ip")
        if not ip:
            continue
        if miner_status.get(ip) != "running":
            continue
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": reason,
                "force": True,
                "emergency": True,
                "source": "pv_safety",
            }
        )
    return actions


def is_pv_safety_resume_blocked(state: Dict[str, Any]) -> bool:
    if _max_soc_blocks_pv_safety(state):
        return False
    return bool(state.get("pv_safety_active"))


def pv_safety_pause_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Alle laufenden Geräte pausieren bei aktiver PV-Sicherheitssperre."""
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    if not state.get("pv_safety_active") or state.get("pv_safety_suspended"):
        return actions, messages
    if _max_soc_blocks_pv_safety(state, live, config):
        return actions, messages
    # Zug pausieren statt beenden – beim Recheck läuft dieselbe Session weiter
    _brake_session(
        state,
        datetime.now(),
        "pv_safety",
        normalize_recheck_minutes(config.get("pv_safety_recheck_minutes")),
    )
    reason = (
        f"PV-Sicherheitsabschaltung aktiv "
        f"(Pace {state.get('pv_safety_deviation_pct')} %)"
    )
    actions = _pause_all_actions(config, live, reason)
    if actions:
        messages.append(
            f"PV-Sicherheitsabschaltung – {len(actions)} Gerät(e) werden abgeschaltet."
        )
    else:
        messages.append(reason)
    return actions, messages


def _max_soc_blocks_pv_safety(
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Batterie-Schutz und Plan-Band haben Vorrang vor der PV-Sperre.

    Die Schwelle hing früher am harten `max_soc_percent` (z. B. 95 %). Zwischen
    Plan-Decke (z. B. 89 %) und hartem Max gewann deshalb die PV-Sperre gegen den
    Füllplan – die beobachtete Mittags-Kampfzone. Maßgeblich ist jetzt das
    Plan-Band; das harte Max bleibt als Notfall darüber.
    """
    if state.get("max_soc_protection_active") or state.get("max_soc_override_active"):
        return True
    plan = dict(state.get("fill_schedule") or {})
    if plan.get("over_max") or plan.get("capacity_limited"):
        return True
    live = live or {}
    try:
        soc = live.get("soc_percent")
        if soc is None:
            soc = state.get("live_soc")
        if soc is None:
            return False
        hard_max = float(plan.get("max_soc_percent") or 95.0)
        band_max = _planning_band_max(state, config, live)
        threshold = min(hard_max, band_max) - PLAN_BAND_HYSTERESIS_PCT
        return float(soc) >= threshold
    except (TypeError, ValueError):
        pass
    return False


def _planning_band_max(
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> float:
    """Plan-Decke aus dem Speicher-Band-Neuron; Fallback hartes Max."""
    fallback = float((state.get("fill_schedule") or {}).get("max_soc_percent") or 95.0)
    if config is None:
        # Ohne Config die bereits gerechnete Decke aus dem State nehmen
        goal = dict((state.get("curve_plan") or {}).get("steering_goal") or {})
        try:
            return float(goal.get("plan_max_pct") or fallback)
        except (TypeError, ValueError):
            return fallback
    try:
        from control_algorithm.storage_band_learning import effective_planning_max_soc

        return float(effective_planning_max_soc(config, state, live=live))
    except Exception:
        return fallback


def check_pv_safety_watchdog(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """
    PV-Unterproduktions-Sicherheitsabschaltung.
    Returns (state, actions, messages).
    """
    now = now or datetime.now()
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []

    if not config.get("has_pv"):
        if state.get("pv_safety_active"):
            _clear_pv_safety(state, now, "no_pv")
        return state, actions, messages

    if not normalize_enabled(config.get("pv_safety_enabled", True)):
        if state.get("pv_safety_active"):
            _clear_pv_safety(state, now, "disabled")
            messages.append("PV-Sicherheitsabschaltung deaktiviert – Sperre aufgehoben.")
        return state, actions, messages

    # Max-SoC / Plan-Band: Batterie-Schutz und Füllplan brauchen Last.
    # Die Sperre ruht dann nur – löschen würde sie sofort neu triggerbar machen.
    if _max_soc_blocks_pv_safety(state, live, config):
        if state.get("pv_safety_active") and not state.get("pv_safety_suspended"):
            _suspend_pv_safety(state, now, "max_soc_priority")
            messages.append(
                "PV-Sicherheitsabschaltung ruht – Batterie-Schutz Max-SoC/Plan-Band hat "
                "Vorrang (Sperre bleibt bestehen, kein Neustart der Prüfung)."
            )
        state["pv_safety_last_skip_reason"] = "max_soc_priority"
        return state, actions, messages

    # Vorrang weg: ruhende Sperre wieder aufnehmen (gleiche Session, kein Neu-Trigger)
    if state.get("pv_safety_suspended"):
        _resume_pv_safety(state, now)
        messages.append("PV-Sicherheitsabschaltung wieder aktiv – Max-SoC-Vorrang beendet.")

    threshold = normalize_threshold_percent(config.get("pv_safety_threshold_percent"))
    clear_pct = normalize_clear_percent(config.get("pv_safety_clear_percent"))
    if clear_pct >= threshold:
        clear_pct = max(5.0, threshold - 5.0)
    recheck_min = normalize_recheck_minutes(config.get("pv_safety_recheck_minutes"))
    min_hour = normalize_min_hour(config.get("pv_safety_min_hour"))
    confirm_hours = normalize_confirm_hours(config.get("pv_safety_confirm_hours"))
    min_fc = normalize_min_forecast_so_far(config.get("pv_safety_min_forecast_so_far_kwh"))

    signals = compute_pv_pace_signals(
        state, live, now=now, confirm_hours=confirm_hours
    )
    if signals.get("soc_now") is not None:
        state["pv_safety_last_soc"] = signals["soc_now"]

    state["pv_safety_signals"] = {
        k: signals[k]
        for k in (
            "day_deviation_pct",
            "window_deviation_pct",
            "live_ratio",
            "stress",
            "actual_so_far_kwh",
            "forecast_so_far_kwh",
            "forecast_ref",
            "until_hour",
        )
        if k in signals
    }

    active = bool(state.get("pv_safety_active"))
    next_recheck = state.get("pv_safety_next_recheck_at")

    if active:
        # Während Sperre: laufenden Zug pausieren und Geräte aus halten.
        # Die Session bleibt bestehen – die Sperre ist Bremse, nicht Gegenplan.
        _brake_session(
            state,
            now,
            "pv_safety",
            recheck_min,
        )
        actions.extend(
            _pause_all_actions(
                config,
                live,
                f"PV-Sicherheitsabschaltung aktiv "
                f"(Pace {signals.get('day_deviation_pct')} %)",
            )
        )

        due = True
        elapsed = _minutes_since(next_recheck, now) if next_recheck else None
        # next_recheck_at liegt in der Zukunft → erst wenn now >= next
        if next_recheck:
            try:
                due = now >= datetime.fromisoformat(str(next_recheck))
            except Exception:
                due = True

        if not due:
            left = None
            try:
                left = (
                    datetime.fromisoformat(str(next_recheck)) - now
                ).total_seconds() / 60.0
            except Exception:
                pass
            messages.append(
                f"PV-Sicherheitsabschaltung aktiv "
                f"(Tages-Pace {signals.get('day_deviation_pct')} %). "
                f"Nächster Recheck in ca. {left:.0f} min."
                if left is not None
                else "PV-Sicherheitsabschaltung aktiv – Recheck ausstehend."
            )
            return state, actions, messages

        # Stunden-Recheck
        clear, clear_reason = _should_clear(signals, clear_pct=clear_pct)
        state["pv_safety_last_recheck_at"] = now.isoformat(timespec="seconds")
        if clear:
            _clear_pv_safety(state, now, clear_reason)
            messages.append(
                f"PV-Sicherheitsabschaltung beendet nach Recheck ({clear_reason})."
            )
            return state, [], messages

        # Weiter gesperrt
        state["pv_safety_next_recheck_at"] = (
            now + timedelta(minutes=recheck_min)
        ).isoformat(timespec="seconds")
        state["pv_safety_deviation_pct"] = signals.get("day_deviation_pct")
        state["pv_safety_reason"] = (
            f"Recheck negativ: Pace {signals.get('day_deviation_pct')} % "
            f"(Clear erst ab −{clear_pct:.0f} %)"
        )
        messages.append(
            f"PV-Sicherheits-Recheck: weiterhin zu wenig Ertrag "
            f"({signals.get('day_deviation_pct')} %). Sperre bleibt "
            f"{recheck_min:.0f} min aktiv."
        )
        return state, actions, messages

    # Nicht aktiv → ggf. triggern
    if _in_cooldown(state, now):
        state["pv_safety_last_skip_reason"] = "cooldown"
        return state, actions, messages

    ok, reason = _should_trigger(
        signals,
        threshold=threshold,
        min_hour=min_hour,
        min_forecast_so_far=min_fc,
    )
    if not ok:
        state["pv_safety_last_skip_reason"] = reason
        return state, actions, messages

    state["pv_safety_active"] = True
    state["pv_safety_triggered_at"] = now.isoformat(timespec="seconds")
    state["pv_safety_next_recheck_at"] = (
        now + timedelta(minutes=recheck_min)
    ).isoformat(timespec="seconds")
    state["pv_safety_deviation_pct"] = signals.get("day_deviation_pct")
    state["pv_safety_reason"] = reason
    state["pv_safety_cleared_at"] = None
    state["pv_safety_suspended"] = False
    _brake_session(state, now, "pv_safety", recheck_min)
    try:
        from control_algorithm.steering_learning import mark_pv_outage_learning_block

        mark_pv_outage_learning_block(
            state,
            reason=f"pv_safety Pace {signals.get('day_deviation_pct')} %",
        )
    except Exception:
        pass
    actions.extend(
        _pause_all_actions(
            config,
            live,
            f"PV-Sicherheitsabschaltung: Pace {signals.get('day_deviation_pct')} % "
            f"(Schwelle −{threshold:.0f} %)",
        )
    )
    messages.append(
        f"PV-Sicherheitsabschaltung: Ertrag {signals.get('day_deviation_pct')} % "
        f"unter aktueller Wetter-OMS (Schwelle −{threshold:.0f} %, Dreifach-Check ok). "
        f"Alle Geräte aus – Recheck in {recheck_min:.0f} min."
    )
    return state, actions, messages


def _clear_pv_safety(state: Dict[str, Any], now: datetime, reason: str) -> None:
    state["pv_safety_active"] = False
    state["pv_safety_suspended"] = False
    state["pv_safety_suspended_reason"] = None
    state["pv_safety_cleared_at"] = now.isoformat(timespec="seconds")
    state["pv_safety_clear_reason"] = reason
    state["pv_safety_next_recheck_at"] = None
    state["pv_safety_reason"] = None
    # Nach echtem Clear kurz sperren, damit nicht im selben Zug neu getriggert wird
    state["pv_safety_cooldown_until"] = (
        now + timedelta(minutes=PV_SAFETY_COOLDOWN_MINUTES)
    ).isoformat(timespec="seconds")
    _release_session(state, now)


def _suspend_pv_safety(state: Dict[str, Any], now: datetime, reason: str) -> None:
    """Sperre ruhen lassen statt löschen.

    Beim Max-SoC-Vorrang wurde die Session früher gelöscht; sobald die Last den
    SoC wieder unter die Schwelle drückte, durfte `_should_trigger` sofort neu
    feuern – das war der Flip-Flop. Jetzt bleibt der Latch erhalten.
    """
    state["pv_safety_suspended"] = True
    state["pv_safety_suspended_reason"] = reason
    state["pv_safety_suspended_at"] = now.isoformat(timespec="seconds")
    _release_session(state, now)


def _resume_pv_safety(state: Dict[str, Any], now: datetime) -> None:
    state["pv_safety_suspended"] = False
    state["pv_safety_suspended_reason"] = None
    state["pv_safety_resumed_at"] = now.isoformat(timespec="seconds")


def _in_cooldown(state: Dict[str, Any], now: datetime) -> bool:
    raw = state.get("pv_safety_cooldown_until")
    if not raw:
        return False
    try:
        return now < datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False


def _brake_session(state: Dict[str, Any], now: datetime, reason: str, until_minutes: float) -> None:
    """Laufenden Überschuss-Zug pausieren statt einen Gegenplan zu fahren."""
    try:
        from control_algorithm.surplus_session import suspend_session

        suspend_session(
            state,
            reason,
            until=now + timedelta(minutes=max(1.0, float(until_minutes))),
            now=now,
        )
    except Exception:
        pass


def _release_session(state: Dict[str, Any], now: datetime) -> None:
    try:
        from control_algorithm.surplus_session import get_session, resume_session

        if str((get_session(state) or {}).get("suspended_by") or "") == "pv_safety":
            resume_session(state, now=now)
    except Exception:
        pass


def pv_safety_info_messages(state: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    if state.get("pv_safety_active"):
        msgs.append(
            f"PV-Sicherheitssperre aktiv: Pace {state.get('pv_safety_deviation_pct')} % "
            f"– {state.get('pv_safety_reason') or 'Unterproduktion'}."
        )
    sig = state.get("pv_safety_signals") or {}
    if sig.get("day_deviation_pct") is not None and not state.get("pv_safety_active"):
        ref = {"oms_current": "Live-OMS", "morning_freeze": "Morgen-Freeze"}.get(
            str(sig.get("forecast_ref") or ""), str(sig.get("forecast_ref") or "—")
        )
        msgs.append(
            f"PV-Sicherheitsmonitor: Tages-Pace {sig.get('day_deviation_pct')} % "
            f"(Referenz {ref}), Fenster {sig.get('window_deviation_pct')} %, "
            f"Stress={'ja' if sig.get('stress') else 'nein'}."
        )
    return msgs
