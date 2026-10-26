# -*- coding: utf-8 -*-
"""Anomalie-Erkennung: Mehr- und Unterverbrauch vs. historischer Stunden-Schnitt."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.consumption_history import (
    archived_history_days,
    corrected_base_consumption_for_anomaly,
)
from control_algorithm.planner import (
    enabled_devices,
    paused_devices_highest_tier,
    sort_for_resume,
)


def _anomaly_pause_all_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    excess_pct: float,
) -> List[Dict[str, Any]]:
    """Sofort alle laufenden Miner pausieren (Sicherheits-Fast-Path)."""
    actions: List[Dict[str, Any]] = []
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    reason = f"Anomalie: Verbrauch {excess_pct:.0f} % über historischem Schnitt"
    for d in enabled_devices(config):
        ip = d.get("ip")
        if not ip or miner_status.get(ip) != "running":
            continue
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": reason,
                "source": "anomaly",
                "force": True,
                "emergency": True,
            }
        )
    return actions


def anomaly_pause_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Alle laufenden Geräte pausieren bei aktiver Überverbrauchs-Anomalie."""
    if not state.get("anomaly_active"):
        return [], []
    excess = float(state.get("anomaly_excess_pct") or 0)
    actions = _anomaly_pause_all_actions(config, live, excess)
    if actions:
        return actions, [
            f"Anomalie-Sicherheitsabschaltung – {len(actions)} Gerät(e) werden abgeschaltet."
        ]
    return [], [
        f"Anomalie aktiv ({excess:.0f} % über Schnitt) – keine laufenden Geräte."
    ]


def _low_anomaly_energy_save_blocked(state: Dict[str, Any]) -> bool:
    """Morgen-Floor / Anker-Streckung / Budget-Cap haben Vorrang vor Extra-Last."""
    stretch = state.get("anchor_stretch") or {}
    if state.get("morning_floor_threatened") or (
        stretch.get("active") and stretch.get("reason") == "stretch_anchor"
    ):
        return True
    try:
        load = float(state.get("tick_miner_load_kw") or 0)
        budget = float(
            state.get("morning_sustainable_cap_kw")
            or state.get("tick_miner_budget_kw")
            or 0
        )
        if budget > 0 and load > budget + 1.0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _anomaly_low_resume_all_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    deficit_pct: float,
) -> List[Dict[str, Any]]:
    """Alle pausierten Miner einschalten (Sicherheits-Fast-Path)."""
    actions: List[Dict[str, Any]] = []
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    reason = f"Anomalie: Verbrauch {deficit_pct:.0f} % unter historischem Schnitt"
    paused = [
        d
        for d in enabled_devices(config)
        if miner_status.get(d.get("ip")) in ("paused", "stopped", "off", "idle")
    ]
    for d in sort_for_resume(paused):
        ip = d.get("ip")
        if not ip:
            continue
        actions.append(
            {
                "ip": ip,
                "action": "resume",
                "reason": reason,
                "source": "anomaly_low",
                "force": True,
                "emergency": True,
            }
        )
    return actions


def anomaly_low_resume_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Pausierte Geräte einschalten bei aktiver Unterverbrauchs-Anomalie."""
    if not state.get("anomaly_low_active"):
        return [], []
    if _low_anomaly_energy_save_blocked(state):
        deficit = float(state.get("anomaly_deficit_pct") or 0)
        return [], [
            f"Unterverbrauch-Anomalie ({deficit:.0f} % unter Schnitt) – "
            f"Morgen-Floor/Streckung/Budget-Cap blockiert Fast-Path-Resume."
        ]
    deficit = float(state.get("anomaly_deficit_pct") or 0)
    actions = _anomaly_low_resume_all_actions(config, live, deficit)
    if actions:
        return actions, [
            f"Unterverbrauch-Sicherheits-Fast-Path – {len(actions)} Gerät(e) werden eingeschaltet."
        ]
    return [], [
        f"Unterverbrauch-Anomalie ({deficit:.0f} % unter Schnitt) – keine pausierten Geräte."
    ]


def normalize_threshold(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 20.0)
    except (TypeError, ValueError):
        v = 20.0
    # Obergrenze bewusst hoch: z. B. 220 % = sehr tolerant (kein hartes Cap bei 200).
    return max(1.0, min(1000.0, v))


def normalize_window_hours(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 1.5)
    except (TypeError, ValueError):
        v = 1.5
    return max(0.5, min(12.0, v))


def normalize_min_history_days(raw: Any) -> int:
    try:
        v = int(float(raw if raw is not None else 7))
    except (TypeError, ValueError):
        v = 7
    return max(1, min(60, v))


def normalize_clear_stable_minutes(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 20.0)
    except (TypeError, ValueError):
        v = 20.0
    return max(5.0, min(120.0, v))


def normalize_resume_delay_minutes(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 30.0)
    except (TypeError, ValueError):
        v = 30.0
    return max(10.0, min(120.0, v))


def normalize_pause_interval_minutes(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 15.0)
    except (TypeError, ValueError):
        v = 15.0
    return max(5.0, min(60.0, v))


def normalize_enabled(raw: Any, default: bool = True) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    s = str(raw).strip().lower()
    return s in ("1", "true", "yes", "on", "ja")


def _reset_anomaly_tracking(state: Dict[str, Any]) -> None:
    state["anomaly_below_threshold_since"] = None
    state["anomaly_last_pause_at"] = None


def _clear_anomaly_state(state: Dict[str, Any], now: datetime) -> None:
    state["anomaly_active"] = False
    state["anomaly_excess_pct"] = None
    state["anomaly_below_threshold_since"] = None
    state["anomaly_cleared_at"] = now.isoformat()
    # Bremse gelöst: pausierter Überschuss-Zug läuft weiter (gleiche Session)
    try:
        from control_algorithm.surplus_session import get_session, resume_session

        if str((get_session(state) or {}).get("suspended_by") or "") == "anomaly":
            resume_session(state, now=now)
    except Exception:
        pass


def _reset_low_anomaly_tracking(state: Dict[str, Any]) -> None:
    state["anomaly_low_above_threshold_since"] = None
    state["anomaly_low_last_resume_at"] = None


def _clear_low_anomaly_state(state: Dict[str, Any], now: datetime) -> None:
    state["anomaly_low_active"] = False
    state["anomaly_deficit_pct"] = None
    state["anomaly_low_above_threshold_since"] = None
    state["anomaly_low_cleared_at"] = now.isoformat()


def _minutes_since(iso_ts: Optional[str], now: datetime) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        started = datetime.fromisoformat(str(iso_ts))
        return (now - started).total_seconds() / 60.0
    except Exception:
        return None


def is_anomaly_resume_blocked(
    state: Dict[str, Any],
    config: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """True wenn nach Mehrverbrauchs-Anomalie-Ende noch keine Einschaltungen erlaubt sind."""
    now = now or datetime.now()
    # Unterverbrauch-Anomalie darf Einschalten – Sperre gilt nur nach Hoch-Anomalie
    if state.get("anomaly_low_active"):
        return False
    cleared_at = state.get("anomaly_cleared_at")
    if not cleared_at:
        return False
    delay_min = normalize_resume_delay_minutes(config.get("anomaly_resume_delay_minutes"))
    elapsed = _minutes_since(cleared_at, now)
    return elapsed is not None and elapsed < delay_min


def _maybe_end_anomaly(
    state: Dict[str, Any],
    config: Dict[str, Any],
    now: datetime,
    messages: List[str],
) -> bool:
    """Beendet Hoch-Anomalie erst nach stabiler Unter-Schwelle-Phase."""
    stable_min = normalize_clear_stable_minutes(config.get("anomaly_clear_stable_minutes"))
    since = state.get("anomaly_below_threshold_since")
    if not since:
        state["anomaly_below_threshold_since"] = now.isoformat()
        return False
    elapsed = _minutes_since(since, now)
    if elapsed is not None and elapsed >= stable_min:
        _clear_anomaly_state(state, now)
        messages.append(
            f"Anomalie beendet: Verbrauch seit {stable_min:.0f} min im historischen Bereich."
        )
        return True
    return False


def _maybe_end_low_anomaly(
    state: Dict[str, Any],
    config: Dict[str, Any],
    now: datetime,
    messages: List[str],
) -> bool:
    """Beendet Unterverbrauch-Anomalie nach stabiler Rückkehr in den Normalbereich."""
    stable_min = normalize_clear_stable_minutes(
        config.get("anomaly_low_clear_stable_minutes")
        if config.get("anomaly_low_clear_stable_minutes") is not None
        else config.get("anomaly_clear_stable_minutes")
    )
    since = state.get("anomaly_low_above_threshold_since")
    if not since:
        state["anomaly_low_above_threshold_since"] = now.isoformat()
        return False
    elapsed = _minutes_since(since, now)
    if elapsed is not None and elapsed >= stable_min:
        _clear_low_anomaly_state(state, now)
        messages.append(
            f"Unterverbrauch-Anomalie beendet: Verbrauch seit {stable_min:.0f} min "
            f"wieder im historischen Bereich."
        )
        return True
    return False


def _pick_resume_candidate(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    tier = paused_devices_highest_tier(devices, miner_status)
    if not tier:
        return None
    ordered = sort_for_resume(tier)
    return ordered[0] if ordered else None


def _handle_high_anomaly(
    *,
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
    excess_pct: float,
    threshold: float,
    pause_interval_min: float,
    actions: List[Dict[str, Any]],
    messages: List[str],
) -> None:
    # Unterverbrauch kann nicht parallel aktiv sein
    if state.get("anomaly_low_active"):
        _clear_low_anomaly_state(state, now)
        _reset_low_anomaly_tracking(state)

    state["anomaly_below_threshold_since"] = None
    state["anomaly_active"] = True
    state["anomaly_excess_pct"] = excess_pct

    devices = enabled_devices(config)
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    running = [d for d in devices if miner_status.get(d.get("ip")) == "running"]
    if not running:
        messages.append(
            f"Anomalie erkannt: Verbrauch liegt {excess_pct:.0f} % über Schnitt. "
            f"Keine laufenden Geräte zum Abschalten."
        )
        return

    pause_actions = _anomaly_pause_all_actions(config, live, excess_pct)
    actions.extend(pause_actions)
    state["anomaly_last_pause_at"] = now.isoformat()
    messages.append(
        f"Anomalie erkannt: Verbrauch liegt {excess_pct:.0f} % über Schnitt. "
        f"{len(pause_actions)} Gerät(e) werden zur Sicherung abgeschaltet (Sicherheits-Fast-Path)."
    )


def _handle_low_anomaly(
    *,
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
    deficit_pct: float,
    resume_interval_min: float,
    actions: List[Dict[str, Any]],
    messages: List[str],
) -> None:
    state["anomaly_low_above_threshold_since"] = None
    state["anomaly_low_active"] = True
    state["anomaly_deficit_pct"] = deficit_pct

    devices = enabled_devices(config)
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    paused = [
        d
        for d in devices
        if miner_status.get(d.get("ip")) in ("paused", "stopped", "off", "idle")
    ]
    if not paused:
        messages.append(
            f"Unterverbrauch erkannt: Verbrauch liegt {deficit_pct:.0f} % unter Schnitt. "
            f"Keine pausierten Geräte zum Einschalten."
        )
        return

    if _low_anomaly_energy_save_blocked(state):
        messages.append(
            f"Unterverbrauch erkannt (−{deficit_pct:.0f} %), aber Morgen-Floor/"
            f"Anker-Streckung/Budget-Cap aktiv – kein Extra-Einschalten (Energiesparen)."
        )
        return

    resume_actions = _anomaly_low_resume_all_actions(config, live, deficit_pct)
    actions.extend(resume_actions)
    state["anomaly_low_last_resume_at"] = now.isoformat()
    messages.append(
        f"Unterverbrauch erkannt: Verbrauch liegt {deficit_pct:.0f} % unter Schnitt. "
        f"{len(resume_actions)} Gerät(e) werden eingeschaltet (Sicherheits-Fast-Path)."
    )


def check_anomaly(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """
    Prüft Verbrauchs-Anomalien:
      - Mehrverbrauch → Lastabbau (pause)
      - Unterverbrauch (optional) → mehr Last (resume)
    Returns (state, actions, messages).
    """
    now = now or datetime.now()
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []

    threshold = normalize_threshold(config.get("anomaly_threshold_percent"))
    low_enabled = normalize_enabled(config.get("anomaly_low_enabled"), True)
    low_threshold = normalize_threshold(
        config.get("anomaly_low_threshold_percent")
        if config.get("anomaly_low_threshold_percent") is not None
        else config.get("anomaly_threshold_percent")
    )
    window_h = normalize_window_hours(config.get("anomaly_window_hours"))
    min_history_days = normalize_min_history_days(config.get("anomaly_min_history_days"))
    pause_interval_min = normalize_pause_interval_minutes(
        config.get("anomaly_pause_interval_minutes")
    )
    resume_interval_min = normalize_pause_interval_minutes(
        config.get("anomaly_low_resume_interval_minutes")
        if config.get("anomaly_low_resume_interval_minutes") is not None
        else config.get("anomaly_pause_interval_minutes")
    )
    archived_days = archived_history_days(base_dir)

    if archived_days < min_history_days:
        if state.get("anomaly_active"):
            _clear_anomaly_state(state, now)
            _reset_anomaly_tracking(state)
            messages.append(
                f"Anomalie beendet: erst {archived_days} von {min_history_days} "
                f"archivierten Tagen – noch keine belastbare Statistik."
            )
        if state.get("anomaly_low_active"):
            _clear_low_anomaly_state(state, now)
            _reset_low_anomaly_tracking(state)
            messages.append(
                f"Unterverbrauch-Anomalie beendet: erst {archived_days} von "
                f"{min_history_days} archivierten Tagen."
            )
        return state, actions, messages

    actual, expected = corrected_base_consumption_for_anomaly(
        config, live, base_dir, window_h, now, state
    )
    if expected <= 0.1:
        if state.get("anomaly_active"):
            _clear_anomaly_state(state, now)
            _reset_anomaly_tracking(state)
        else:
            state["anomaly_active"] = False
            state["anomaly_excess_pct"] = None
        if state.get("anomaly_low_active"):
            _clear_low_anomaly_state(state, now)
            _reset_low_anomaly_tracking(state)
        else:
            state["anomaly_low_active"] = False
            state["anomaly_deficit_pct"] = None
        return state, actions, messages

    excess_ratio = (actual - expected) / expected
    excess_pct = round(excess_ratio * 100.0, 1)
    deficit_ratio = (expected - actual) / expected
    deficit_pct = round(deficit_ratio * 100.0, 1)

    # --- Hoch: Mehrverbrauch ---
    if excess_ratio > threshold / 100.0:
        _handle_high_anomaly(
            state=state,
            config=config,
            live=live,
            now=now,
            excess_pct=excess_pct,
            threshold=threshold,
            pause_interval_min=pause_interval_min,
            actions=actions,
            messages=messages,
        )
        return state, actions, messages

    if state.get("anomaly_active"):
        _maybe_end_anomaly(state, config, now, messages)
    else:
        state["anomaly_below_threshold_since"] = None

    # --- Niedrig: Unterverbrauch (nur wenn keine Hoch-Anomalie mehr aktiv) ---
    if state.get("anomaly_active"):
        if state.get("anomaly_low_active"):
            _clear_low_anomaly_state(state, now)
            _reset_low_anomaly_tracking(state)
        return state, actions, messages

    if not low_enabled:
        if state.get("anomaly_low_active"):
            _clear_low_anomaly_state(state, now)
            _reset_low_anomaly_tracking(state)
            messages.append("Unterverbrauch-Anomalie deaktiviert – Status zurückgesetzt.")
        return state, actions, messages

    if deficit_ratio > low_threshold / 100.0:
        _handle_low_anomaly(
            state=state,
            config=config,
            live=live,
            now=now,
            deficit_pct=deficit_pct,
            resume_interval_min=resume_interval_min,
            actions=actions,
            messages=messages,
        )
        return state, actions, messages

    if state.get("anomaly_low_active"):
        _maybe_end_low_anomaly(state, config, now, messages)
    else:
        state["anomaly_low_above_threshold_since"] = None

    return state, actions, messages
