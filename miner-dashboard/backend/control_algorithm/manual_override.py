# -*- coding: utf-8 -*-
"""
Manuelle Fremdsteuerung: Hold-Countdown pro Gerät.

Wenn Live-Status vom zuletzt vom Algo gesetzten Soll abweicht
(Dashboard / HA / Miner-UI), startet ein Countdown. Bis Ablauf schaltet
der Algo dieses Gerät weder ein noch aus – Ausnahme: Min-SoC-/Notfall-Pause.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

SETTLE_SECONDS = 90.0


def normalize_manual_override_hold_hours(raw: Any) -> float:
    try:
        h = float(raw)
    except (TypeError, ValueError):
        h = 2.0
    return max(0.0, min(48.0, h))


def _parse_iso(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (TypeError, ValueError):
        return None


def _device_label(config: Optional[Dict[str, Any]], ip: str) -> str:
    for d in (config or {}).get("devices") or []:
        if str(d.get("ip") or "") == ip:
            name = str(d.get("name") or d.get("note") or "").strip()
            return name or ip
    return ip


def _status_on(status: Any) -> bool:
    return str(status or "").lower() == "running"


def _status_off(status: Any) -> bool:
    return str(status or "").lower() in ("paused", "stopped", "off", "idle")


def expected_from_last_action(last_action: Any) -> Optional[str]:
    """'on' / 'off' / None wenn kein Algo-Soll bekannt."""
    la = str(last_action or "").lower().strip()
    if la == "resume":
        return "on"
    if la == "pause":
        return "off"
    return None


def runtime_entry(state: Dict[str, Any], ip: str) -> Dict[str, Any]:
    runtime = state.setdefault("device_runtime", {})
    if not isinstance(runtime, dict):
        state["device_runtime"] = {}
        runtime = state["device_runtime"]
    entry = runtime.get(ip)
    if not isinstance(entry, dict):
        entry = {}
        runtime[ip] = entry
    return entry


def is_manual_hold_active(
    entry: Optional[Dict[str, Any]], now: Optional[datetime] = None
) -> bool:
    now = now or datetime.now()
    if not entry:
        return False
    until = _parse_iso(entry.get("manual_hold_until"))
    return until is not None and until > now


def manual_hold_until(
    entry: Optional[Dict[str, Any]], now: Optional[datetime] = None
) -> Optional[datetime]:
    now = now or datetime.now()
    if not entry:
        return None
    until = _parse_iso(entry.get("manual_hold_until"))
    if until is None or until <= now:
        return None
    return until


def _clear_hold(entry: Dict[str, Any]) -> None:
    entry.pop("manual_hold_until", None)
    entry.pop("manual_hold_reason", None)
    entry.pop("manual_hold_since", None)


def _set_hold(
    entry: Dict[str, Any],
    *,
    reason: str,
    hold_hours: float,
    now: datetime,
) -> datetime:
    until = now + timedelta(hours=hold_hours)
    if not entry.get("manual_hold_since"):
        entry["manual_hold_since"] = now.isoformat()
    entry["manual_hold_until"] = until.isoformat()
    entry["manual_hold_reason"] = reason
    return until


def _recently_commanded(
    ip: str,
    recently_commanded_ips: Optional[Iterable[str]],
    last_action_times: Optional[Dict[str, float]],
    now: datetime,
    settle_seconds: float,
) -> bool:
    if recently_commanded_ips is not None:
        recent_set = {str(x) for x in recently_commanded_ips}
        if ip in recent_set:
            return True
    if last_action_times:
        ts = last_action_times.get(ip)
        if ts is not None:
            try:
                age = now.timestamp() - float(ts)
            except (TypeError, ValueError):
                age = settle_seconds + 1.0
            if age < settle_seconds:
                return True
    return False


def detect_and_update_manual_holds(
    state: Dict[str, Any],
    live_status: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    recently_commanded_ips: Optional[Iterable[str]] = None,
    last_action_times: Optional[Dict[str, float]] = None,
    settle_seconds: float = SETTLE_SECONDS,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Erkennt Fremdsteuerung (Live ≠ Algo-Soll) und setzt/verlängert Holds.

    Hold wird nur bei neuer Abweichung gesetzt bzw. bei Richtungswechsel
    (fremd_ein ↔ fremd_aus) verlängert – nicht jeden Tick bei gleichem Mismatch.
    Nach Ablauf wird last_action an den Live-Status angeglichen, damit der
    Algo wieder frei entscheiden kann und nicht sofort neu sperrt.

    Returns:
        (state, messages)
    """
    now = now or datetime.now()
    config = config or {}
    messages: List[str] = []
    hold_h = normalize_manual_override_hold_hours(
        config.get("manual_override_hold_hours", 2.0)
    )
    if hold_h <= 0:
        # Feature aus: abgelaufene/aktive Holds aufräumen
        runtime = state.get("device_runtime") or {}
        if isinstance(runtime, dict):
            for entry in runtime.values():
                if isinstance(entry, dict) and entry.get("manual_hold_until"):
                    _clear_hold(entry)
        return state, messages

    device_ips: Set[str] = set()
    for d in config.get("devices") or []:
        ip = str(d.get("ip") or "")
        if ip:
            device_ips.add(ip)
    for ip in (live_status or {}):
        if ip:
            device_ips.add(str(ip))
    runtime = state.get("device_runtime") or {}
    if isinstance(runtime, dict):
        for ip in runtime:
            if ip:
                device_ips.add(str(ip))

    for ip in sorted(device_ips):
        entry = runtime_entry(state, ip)
        status = live_status.get(ip)

        # Abgelaufene Holds: freigeben und Soll an Live anpassen
        until = _parse_iso(entry.get("manual_hold_until"))
        if until is not None and until <= now:
            if status is not None and str(status).strip() != "":
                if _status_on(status):
                    entry["last_action"] = "resume"
                elif _status_off(status):
                    entry["last_action"] = "pause"
            _clear_hold(entry)

        if _recently_commanded(
            ip, recently_commanded_ips, last_action_times, now, settle_seconds
        ):
            continue

        expected = expected_from_last_action(entry.get("last_action"))
        if expected is None:
            continue

        if status is None or str(status).strip() == "":
            continue

        mismatch_reason: Optional[str] = None
        if expected == "on" and _status_off(status):
            mismatch_reason = "fremd_aus"
        elif expected == "off" and _status_on(status):
            mismatch_reason = "fremd_ein"

        if mismatch_reason is None:
            continue

        # Bereits aktiver Hold mit gleicher Richtung: Timer nicht neu starten
        if is_manual_hold_active(entry, now):
            prev_reason = str(entry.get("manual_hold_reason") or "")
            if prev_reason == mismatch_reason:
                continue
            # Richtungswechsel (Nutzer hat erneut umgeschaltet) → Timer neu
            new_until = _set_hold(
                entry, reason=mismatch_reason, hold_hours=hold_h, now=now
            )
            label = _device_label(config, ip)
            direction = "EIN" if mismatch_reason == "fremd_ein" else "AUS"
            messages.append(
                f"Fremdsteuerung: {label} erneut manuell {direction} – "
                f"Sperre verlängert bis {new_until.strftime('%H:%M')}."
            )
            continue

        new_until = _set_hold(
            entry, reason=mismatch_reason, hold_hours=hold_h, now=now
        )
        label = _device_label(config, ip)
        direction = "EIN" if mismatch_reason == "fremd_ein" else "AUS"
        messages.append(
            f"Fremdsteuerung: {label} manuell {direction} – "
            f"Algo gesperrt bis {new_until.strftime('%H:%M')}."
        )

    return state, messages


def apply_manual_override_guards(
    actions: List[Dict[str, Any]],
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Blockiert pause/resume für Geräte mit aktivem Manual-Hold.

    Menschliche Ein- und Aus-Befehle stehen über harten SoC-Regeln,
    Nachtsperre, PV-Sicherheit und Anomalie. Auch Notfall-Pausen
    verwerfen ein menschliches Ein nicht.
    """
    now = now or datetime.now()
    config = config or {}
    hold_h = normalize_manual_override_hold_hours(
        config.get("manual_override_hold_hours", 2.0)
    )
    if hold_h <= 0:
        return list(actions), []

    filtered: List[Dict[str, Any]] = []
    messages: List[str] = []
    for act in actions:
        ip = str(act.get("ip") or "")
        if not ip:
            filtered.append(act)
            continue
        entry = runtime_entry(state, ip)
        until = manual_hold_until(entry, now)
        if until is None:
            filtered.append(act)
            continue

        action = str(act.get("action") or "").lower()
        label = _device_label(config, ip)
        reason = str(entry.get("manual_hold_reason") or "fremd")
        src = str(act.get("source") or "")
        messages.append(
            f"Fremdsteuerung: {label} gesperrt bis {until.strftime('%H:%M')} "
            f"({reason}) – {action or '?'} verworfen"
            + (f", Quelle {src}" if src else "")
            + ". Menschlicher Befehl hat Vorrang."
        )
    return filtered, messages


def active_manual_holds(
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Aktive Holds für Logikgatter / Status."""
    now = now or datetime.now()
    out: List[Dict[str, Any]] = []
    runtime = state.get("device_runtime") or {}
    if not isinstance(runtime, dict):
        return out
    for ip, entry in runtime.items():
        if not isinstance(entry, dict):
            continue
        until = manual_hold_until(entry, now)
        if until is None:
            continue
        out.append(
            {
                "ip": str(ip),
                "label": _device_label(config, str(ip)),
                "until": until.isoformat(),
                "until_clock": until.strftime("%H:%M"),
                "reason": entry.get("manual_hold_reason"),
                "since": entry.get("manual_hold_since"),
            }
        )
    out.sort(key=lambda x: x.get("until") or "")
    return out
