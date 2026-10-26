# -*- coding: utf-8 -*-
"""
Prioritätsleiter der Steuerung: eine Stelle, die sagt wer bei Konflikt gewinnt.

Bisher hatte jede Schicht ihre eigene fest verdrahtete Reihenfolge (Fast-Paths im
Lastplaner, Post-Filter im Tick, Same-Tick-Arbiter). Hier steht die Leiter einmal,
dazu ein Trace, der pro Tick festhält wer gewonnen hat und warum.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# Stärkste Stufe zuerst.
PRIORITY_LADDER = (
    "hard_stop",  # Min-SoC, Nacht-Sperre, hartes Max-SoC, schwere Anomalie
    "plan_session",  # laufender Überschuss-Zug (Fill/Band/Export)
    "plan_regular",  # normales Budget-Resume / Budget-Shed
    "soft_reserve",  # Morgen-Floor, Anker-Reserve, Streckung
    "anti_flatter",  # reine Zeit-Commitments
)

PRIORITY_LABEL = {
    "hard_stop": "Notbremse",
    "plan_session": "Überschuss-Session",
    "plan_regular": "Budget-Plan",
    "soft_reserve": "Reserve/Floor",
    "anti_flatter": "Anti-Flatter",
}

_TRACE_LIMIT = 60


def priority_rank(level: Optional[str]) -> int:
    """Kleiner = stärker. Unbekanntes landet ganz unten."""
    try:
        return PRIORITY_LADDER.index(str(level))
    except ValueError:
        return len(PRIORITY_LADDER)


def is_stronger(level_a: Optional[str], level_b: Optional[str]) -> bool:
    return priority_rank(level_a) < priority_rank(level_b)


def classify_action(
    act: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> str:
    """Action-Dict auf eine Stufe der Leiter abbilden.

    Liest nur vorhandene Flags (emergency, safety, source, session, fill_urgency,
    budget_shed, morning_floor) – keine neuen Felder im Lastplaner nötig.
    """
    if not isinstance(act, dict):
        return "anti_flatter"

    # Notbremsen: bestehende Klassifikation wiederverwenden, damit sich am
    # heutigen Verhalten der Sicherheitspfade nichts verschiebt.
    try:
        from control_algorithm.switch_commitment import (
            is_budget_shed_pause,
            is_emergency_action,
            is_fill_urgency_resume,
        )
    except Exception:  # pragma: no cover - nur bei kaputtem Import
        return "plan_regular"

    if is_emergency_action(act):
        return "hard_stop"

    if act.get("session") or str(act.get("source") or "") == "surplus_session":
        return "plan_session"

    st = state or {}
    session = st.get("surplus_session") or {}
    if session.get("active") and not session.get("suspended_by"):
        ip = str(act.get("ip") or "")
        if ip and ip in {str(x) for x in (session.get("devices") or [])}:
            return "plan_session"

    if is_fill_urgency_resume(act):
        return "plan_session"

    if is_budget_shed_pause(act):
        # Morgen-Floor / Anker-Reserve sind weich, reines Budget gehört zum Plan.
        if act.get("morning_floor") or "anker-reserve" in str(act.get("reason") or "").lower():
            return "soft_reserve"
        return "plan_regular"

    return "plan_regular"


def record_arbiter_decision(
    state: Dict[str, Any],
    *,
    ip: str,
    winner: Optional[Dict[str, Any]],
    losers: Optional[List[Dict[str, Any]]] = None,
    why: str = "",
    now: Optional[datetime] = None,
) -> None:
    """Gewinner/Verlierer eines Konflikts in state['arbiter_trace'] festhalten."""
    if not isinstance(state, dict) or not winner:
        return
    now = now or datetime.now()
    level = classify_action(winner, state)
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "ip": str(ip or ""),
        "winner": {
            "action": str(winner.get("action") or ""),
            "level": level,
            "reason": str(winner.get("reason") or "")[:160],
        },
        "losers": [
            {
                "action": str(a.get("action") or ""),
                "level": classify_action(a, state),
                "reason": str(a.get("reason") or "")[:160],
            }
            for a in (losers or [])
        ],
        "why": str(why or "")[:160],
    }
    trace = state.get("arbiter_trace")
    if not isinstance(trace, list):
        trace = []
    trace.append(entry)
    state["arbiter_trace"] = trace[-_TRACE_LIMIT:]

    # control_owner = stärkste Stufe, die in diesem Tick etwas durchgesetzt hat
    tick_key = now.isoformat(timespec="minutes")
    owner = state.get("control_owner") or {}
    if owner.get("tick") != tick_key or is_stronger(level, owner.get("level")):
        state["control_owner"] = {
            "tick": tick_key,
            "level": level,
            "label": PRIORITY_LABEL.get(level, level),
            "ip": str(ip or ""),
            "reason": str(winner.get("reason") or "")[:160],
        }


def control_owner_label(state: Dict[str, Any]) -> str:
    owner = (state or {}).get("control_owner") or {}
    return str(owner.get("label") or "")
