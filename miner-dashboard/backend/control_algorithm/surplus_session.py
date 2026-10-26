# -*- coding: utf-8 -*-
"""
Überschuss-Session: ein laufender Überschuss-Zug als Planführung.

Bisher war die Schaltung ein Zweipunktregler gegen `miner_budget_kw`, der jeden
Tick neu entschied und nichts mitzählte – deshalb Zickzack statt „Plan halten".
Hier bekommt ein einmal gestarteter Zug ein Ziel in kWh, zählt mit was schon
gefressen wurde und hält, bis die Menge abgebaut oder eine echte Notbremse da
ist. Weiche Schichten (Morgen-Floor, Anker-Reserve) drosseln nur.

Verbindlich ist die Energiemenge, nicht die Laufzeit: die Geräteverteilung darf
sich bei Wetterwechsel ändern, solange `delivered_kwh` Richtung `goal_kwh` läuft.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

SESSION_KEY = "surplus_session"

# Nach Neustart/Lücke nicht rückwirkend Energie gutschreiben
_MAX_ACCRUAL_MINUTES = 30.0
# Unter dieser Restmenge lohnt kein Weiterziehen
_MIN_REMAINING_KWH = 0.3


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now()


def _parse(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _f(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def get_session(state: Dict[str, Any]) -> Dict[str, Any]:
    session = (state or {}).get(SESSION_KEY)
    return dict(session) if isinstance(session, dict) else {}


def _store(state: Dict[str, Any], session: Dict[str, Any]) -> Dict[str, Any]:
    state[SESSION_KEY] = session
    return session


def is_active(state: Dict[str, Any]) -> bool:
    return bool(get_session(state).get("active"))


def is_running(state: Dict[str, Any]) -> bool:
    """Aktiv und nicht von einer Risiko-Schicht pausiert."""
    session = get_session(state)
    return bool(session.get("active")) and not session.get("suspended_by")


def session_devices(state: Dict[str, Any]) -> List[str]:
    return [str(x) for x in (get_session(state).get("devices") or [])]


def session_holds(state: Dict[str, Any], ip: Optional[str] = None) -> bool:
    """Darf eine weiche Pause dieses Gerät abschalten? False = nur drosseln."""
    if not is_running(state):
        return False
    if ip is None:
        return True
    return str(ip) in set(session_devices(state))


def remaining_kwh(state: Dict[str, Any]) -> float:
    session = get_session(state)
    if not session.get("active"):
        return 0.0
    return max(0.0, _f(session.get("goal_kwh")) - _f(session.get("delivered_kwh")))


def _neuron_goal_cap(
    state: Dict[str, Any],
    config: Dict[str, Any],
    today: Dict[str, Any],
) -> Optional[float]:
    """SoC-Prio-Neuron formt das Ziel, statt später Schaltungen zu blockieren.

    `recommended_continuous_kw` ist eine Dauerleistung; über die Reststunden des
    Tages wird daraus eine Energiemenge, die das Session-Ziel deckelt.
    """
    try:
        from control_algorithm.soc_priority_neuron import neuron_applies

        if not neuron_applies(config, state):
            return None
        neuron = dict(state.get("soc_priority_neuron") or {})
        if neuron.get("baseline_unreachable"):
            return None
        hours = _f(today.get("hours_left_today"))
        if hours <= 0.25:
            return None
        return max(0.0, _f(neuron.get("recommended_continuous_kw")) * hours)
    except Exception:
        return None


def neuron_allowed_devices(
    state: Dict[str, Any],
    config: Dict[str, Any],
) -> Optional[List[str]]:
    """Vom Neuron freigegebene Geräte, oder None wenn es sich heraushält."""
    try:
        from control_algorithm.soc_priority_neuron import neuron_applies

        if not neuron_applies(config, state):
            return None
        neuron = dict(state.get("soc_priority_neuron") or {})
        if neuron.get("baseline_unreachable"):
            return None
        allowed = neuron.get("recommended_on_ips")
        return [str(x) for x in allowed] if isinstance(allowed, list) else None
    except Exception:
        return None


def open_session(
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    *,
    devices: Optional[List[str]] = None,
    trigger: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Überschuss-Zug eröffnen; Ziel kommt aus dem Tages-Überschuss.

    Läuft bereits eine Session, werden nur die Geräte ergänzt.
    """
    now = _now(now)
    session = get_session(state)
    if session.get("active"):
        return add_devices(state, devices or [], now=now)

    today = dict(state.get("today_surplus") or {})
    goal_kwh = _f(today.get("kwh"))
    neuron_cap = _neuron_goal_cap(state, config, today)
    if neuron_cap is not None and neuron_cap < goal_kwh:
        goal_kwh = neuron_cap
    if not today.get("ok") or goal_kwh < _MIN_REMAINING_KWH:
        return session

    goal_meta = dict((state.get("curve_plan") or {}).get("steering_goal") or {})
    soc_now = live.get("soc_percent")
    if soc_now is None:
        soc_now = state.get("live_soc")

    session = {
        "active": True,
        "date": now.date().isoformat(),
        "started_at": now.isoformat(timespec="seconds"),
        "last_update": now.isoformat(timespec="seconds"),
        "trigger": str(trigger or ""),
        "goal_kwh": round(goal_kwh, 2),
        "goal_kwh_initial": round(goal_kwh, 2),
        "goal_capped_by_neuron": bool(neuron_cap is not None and neuron_cap < _f(today.get("kwh"))),
        "goal_soc_pct": goal_meta.get("target_peak_pct"),
        "reserve_soc_pct": today.get("reserve_soc_pct"),
        "delivered_kwh": 0.0,
        "start_soc": _f(soc_now, 0.0) if soc_now is not None else None,
        "devices": [str(x) for x in (devices or []) if x],
        "suspended_by": None,
        "suspended_until": None,
        "suspend_count": 0,
        "end_reason": None,
    }
    return _store(state, session)


def add_devices(
    state: Dict[str, Any],
    devices: List[str],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    session = get_session(state)
    if not session.get("active"):
        return session
    known = [str(x) for x in (session.get("devices") or [])]
    for ip in devices or []:
        if ip and str(ip) not in known:
            known.append(str(ip))
    session["devices"] = known
    return _store(state, session)


def update_session(
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Pro Tick: gefressene Energie fortschreiben und Ziel nachführen."""
    now = _now(now)
    session = get_session(state)
    if not session.get("active"):
        return session

    # Tageswechsel beendet den Zug
    if str(session.get("date") or "") != now.date().isoformat():
        return close_session(state, "tageswechsel", now=now)

    last = _parse(session.get("last_update"))
    session["last_update"] = now.isoformat(timespec="seconds")

    if not session.get("suspended_by") and last is not None:
        minutes = (now - last).total_seconds() / 60.0
        if 0 < minutes <= _MAX_ACCRUAL_MINUTES:
            load_kw = _f(state.get("tick_miner_load_kw"))
            session["delivered_kwh"] = round(
                _f(session.get("delivered_kwh")) + load_kw * minutes / 60.0, 3
            )

    # Ziel nachführen: heutiger Überschuss kann mit dem PV-Pace schrumpfen,
    # das Neuron deckelt zusätzlich. Beides ändert nur das Ziel, nicht den Lauf.
    today = dict(state.get("today_surplus") or {})
    if today.get("ok"):
        delivered = _f(session.get("delivered_kwh"))
        rest = _f(today.get("kwh"))
        neuron_cap = _neuron_goal_cap(state, config, today)
        if neuron_cap is not None and neuron_cap < rest:
            rest = neuron_cap
            session["goal_capped_by_neuron"] = True
        session["goal_kwh"] = round(max(0.0, rest + delivered), 2)

    _store(state, session)

    # Pause-Fenster abgelaufen? Risiko-Schicht entscheidet beim Recheck neu.
    until = _parse(session.get("suspended_until"))
    if session.get("suspended_by") and until is not None and now >= until:
        session["suspended_until"] = None
        _store(state, session)

    if remaining_kwh(state) < _MIN_REMAINING_KWH:
        return close_session(state, "ziel erreicht", now=now)

    return session


def suspend_session(
    state: Dict[str, Any],
    reason: str,
    until: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Risiko-Schicht bremst: Zug pausiert, bleibt aber bestehen."""
    now = _now(now)
    session = get_session(state)
    if not session.get("active"):
        return session
    if not session.get("suspended_by"):
        session["suspend_count"] = int(_f(session.get("suspend_count"))) + 1
        session["suspended_at"] = now.isoformat(timespec="seconds")
    session["suspended_by"] = str(reason or "risiko")
    session["suspended_until"] = until.isoformat(timespec="seconds") if until else None
    return _store(state, session)


def resume_session(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = _now(now)
    session = get_session(state)
    if not session.get("active") or not session.get("suspended_by"):
        return session
    session["suspended_by"] = None
    session["suspended_until"] = None
    session["resumed_at"] = now.isoformat(timespec="seconds")
    session["last_update"] = now.isoformat(timespec="seconds")
    return _store(state, session)


def close_session(
    state: Dict[str, Any],
    reason: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = _now(now)
    session = get_session(state)
    if not session.get("active"):
        return session
    session["active"] = False
    session["suspended_by"] = None
    session["suspended_until"] = None
    session["end_reason"] = str(reason or "")
    session["ended_at"] = now.isoformat(timespec="seconds")
    return _store(state, session)


def session_summary(state: Dict[str, Any]) -> str:
    session = get_session(state)
    if not session.get("active"):
        end = str(session.get("end_reason") or "")
        return f"Keine Überschuss-Session ({end})" if end else "Keine Überschuss-Session"
    rest = remaining_kwh(state)
    if session.get("suspended_by"):
        return (
            f"Überschuss-Session pausiert ({session.get('suspended_by')}) – "
            f"noch {rest:.1f} kWh bis Ziel."
        )
    return (
        f"Überschuss-Session läuft – {_f(session.get('delivered_kwh')):.1f} von "
        f"{_f(session.get('goal_kwh')):.1f} kWh gefressen, noch {rest:.1f} kWh."
    )
