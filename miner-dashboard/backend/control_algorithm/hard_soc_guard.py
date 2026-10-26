# -*- coding: utf-8 -*-
"""Harte Min-/Max-SoC-Regeln: eine Prüfung, die der Daemon beim Start ausführt.

Die Regeln lesen Min und Max aus der Nutzer-Konfiguration (Standard 35 % / 95 %).
Liegt der SoC auf oder über Max, wird gestaffelt not-eingeschaltet
(höchste Priorität zuerst; Nachschub nur bei Ladeüberschuss unter 80 % PV).
Liegt er unter Min, wird alles Laufende not-abgeschaltet.
Nachtsperre, Budget und Halt-Band dürfen das nicht aushebeln.

Am 16. September 2026 hat eine spätere Änderung im uncommitteten Arbeitsstand
die Nachtsperre vor die Max-Regel gestellt. Diese Prüfung startet den
Steuerungsdaemon nicht, wenn das wieder passiert.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

from control_algorithm.battery_protection import (
    normalize_max_soc,
    normalize_min_soc,
    select_max_soc_resume_devices,
)
from control_algorithm.config import DEFAULT_CONFIG
from control_algorithm.load_scheduler import plan_load_actions
from control_algorithm.planner import enabled_devices

LogFn = Callable[[str], None]

_FILL = {
    "ok": True,
    "over_max": False,
    "capacity_limited": False,
    "excess_charge_kw": 0.0,
    "charge_kw": 0.0,
    "desired_charge_kw": 0.0,
    "flex": {"should_start_now": False},
    "flex_stack": [],
}


class HardSocRulesMissing(RuntimeError):
    """Die harten SoC-Regeln sind nicht mehr wirksam."""


def _devices() -> List[Dict[str, Any]]:
    return [
        {"ip": "10.0.0.1", "enabled": True, "power_kw": 6.0, "priority": 10},
        {"ip": "10.0.0.2", "enabled": True, "power_kw": 1.5, "priority": 3},
    ]


def _config(min_soc: float, max_soc: float) -> Dict[str, Any]:
    return {
        "enabled": True,
        "min_soc_percent": min_soc,
        "max_soc_percent": max_soc,
        "max_soc_release_percent": max(min_soc + 1.0, max_soc - 5.0),
        "storage_kwh": 41.0,
        "min_device_runtime_hours": 1.0,
        "devices": _devices(),
    }


def _plan(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    with patch(
        "control_algorithm.fill_schedule.compute_fill_schedule",
        return_value=dict(_FILL),
    ), patch(
        "control_algorithm.load_scheduler.miner_surplus_kwh",
        return_value=20.0,
    ):
        return plan_load_actions(config, live, state, "/tmp", 40.0, 10.0, 1.0)


def verify_hard_soc_rules() -> List[str]:
    """Leere Liste = Regeln wirken. Sonst eine Zeile je Verletzung."""
    failures: List[str] = []
    if float(DEFAULT_CONFIG.get("min_soc_percent") or 0) != 35.0:
        failures.append(
            f"Standard-Min-SoC ist {DEFAULT_CONFIG.get('min_soc_percent')}, erwartet 35."
        )
    if float(DEFAULT_CONFIG.get("max_soc_percent") or 0) != 95.0:
        failures.append(
            f"Standard-Max-SoC ist {DEFAULT_CONFIG.get('max_soc_percent')}, erwartet 95."
        )

    blocked = {
        "night_lock_active": True,
        "night_lock_force": True,
        "night_lock_reason": "7-Tage-Prognose",
        "max_soc_hold_released": True,
        "load_over_budget_reason": "Budget 0",
    }
    actions, _msgs = _plan(
        _config(35.0, 95.0),
        {"soc_percent": 95.0, "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "paused"}},
        dict(blocked),
    )
    resumes = [a for a in actions if a.get("action") == "resume"]
    if {a.get("ip") for a in resumes} != {"10.0.0.1"}:
        failures.append(
            "Bei SoC = Max 95 % startet nicht nur der Miner mit höchster Priorität."
        )
    if any(a.get("action") == "pause" for a in actions):
        failures.append("Bei SoC = Max schaltet eine andere Regel trotzdem ab.")
    if resumes and not all(a.get("emergency") and a.get("source") == "max_soc_protection" for a in resumes):
        failures.append("Max-Einschalten ist kein Notfall mehr (Quelle max_soc_protection).")

    actions, _msgs = _plan(
        _config(40.0, 90.0),
        {
            "soc_percent": 90.0,
            "pv_kw": 20.0,
            "battery_power_kw": 2.0,
            "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "running"},
        },
        dict(blocked),
    )
    own = [a for a in actions if a.get("action") == "resume"]
    if [a.get("ip") for a in own] != ["10.0.0.1"] and {a.get("ip") for a in own} != {"10.0.0.1"}:
        failures.append("Eigenes Max 90 % wird nicht verwendet (nicht fest 95 %).")
    if any(a.get("action") == "pause" for a in actions):
        failures.append("Über dem eigenen Max wird noch abgeschaltet.")

    actions, _msgs = _plan(
        _config(35.0, 95.0),
        {
            "soc_percent": 34.9,
            "miner_status": {"10.0.0.1": "running", "10.0.0.2": "running"},
        },
        {"night_lock_active": False, "max_soc_protection_active": True},
    )
    pauses = [a for a in actions if a.get("action") == "pause"]
    if {a.get("ip") for a in pauses} != {"10.0.0.1", "10.0.0.2"}:
        failures.append("Unter Min 35 % wird nicht alles not-abgeschaltet.")
    if any(a.get("action") == "resume" for a in actions):
        failures.append("Unter Min wird noch eingeschaltet.")
    if pauses and not all(a.get("emergency") and a.get("source") == "min_soc" for a in pauses):
        failures.append("Min-Abschaltung ist kein Notfall mehr (Quelle min_soc).")

    actions, _msgs = _plan(
        _config(42.0, 95.0),
        {"soc_percent": 41.0, "miner_status": {"10.0.0.1": "running", "10.0.0.2": "paused"}},
        {},
    )
    if {a.get("ip") for a in actions if a.get("action") == "pause"} != {"10.0.0.1"}:
        failures.append("Eigenes Min 42 % wird nicht verwendet (nicht fest 35 %).")

    actions, _msgs = _plan(
        _config(35.0, 95.0),
        {"soc_percent": 50.0, "miner_status": {"10.0.0.1": "paused", "10.0.0.2": "running"}},
        dict(blocked),
    )
    if any(a.get("source") == "max_soc_protection" for a in actions):
        failures.append("Zwischen Min und Max greift die Max-Notregel.")
    return failures


def seal_hard_soc_actions(
    actions: List[Dict[str, Any]],
    config: Dict[str, Any],
    live: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Letzte Instanz im Tick: Nachtsperre, Budget und Halt dürfen die Grenzen nicht umdrehen.

    Ein menschlicher Haltebefehl liegt hinter diesem Schritt und bleibt unangetastet.
    """
    notes: List[str] = []
    soc = live.get("soc_percent")
    try:
        soc_f = float(soc) if soc is not None else None
    except (TypeError, ValueError):
        return list(actions), notes
    if soc_f is None:
        return list(actions), notes

    min_soc = normalize_min_soc(config)
    max_soc = normalize_max_soc(config)
    status: Dict[str, Any] = dict(live.get("miner_status") or {})
    devices = enabled_devices(config)
    kept = list(actions)

    if soc_f < min_soc:
        before = len(kept)
        kept = [a for a in kept if a.get("action") != "resume"]
        have = {str(a.get("ip")) for a in kept if a.get("action") == "pause"}
        added = 0
        for d in devices:
            ip = str(d.get("ip") or "")
            if not ip or str(status.get(ip) or "").lower() != "running" or ip in have:
                continue
            kept.append(
                {
                    "ip": ip,
                    "action": "pause",
                    "reason": (
                        f"Harte Grenze Min-SoC ({min_soc:.0f}%): "
                        f"aktuell {soc_f:.0f}% – Notabschaltung"
                    ),
                    "force": True,
                    "emergency": True,
                    "source": "min_soc",
                }
            )
            added += 1
        if added or len(kept) != before:
            notes.append(
                f"Harte Grenze Min-SoC {min_soc:.0f}% wiederhergestellt "
                f"(SoC {soc_f:.0f}%, {added} Notabschaltung(en))."
            )
        return kept, notes

    if soc_f >= max_soc:
        before = len(kept)
        kept = [a for a in kept if a.get("action") != "pause"]
        have = {str(a.get("ip")) for a in kept if a.get("action") == "resume"}
        targets, detail = select_max_soc_resume_devices(config, live)
        added = 0
        for d in targets:
            ip = str(d.get("ip") or "")
            if not ip or str(status.get(ip) or "").lower() == "running" or ip in have:
                continue
            kept.append(
                {
                    "ip": ip,
                    "action": "resume",
                    "reason": (
                        f"Harte Grenze Max-SoC ({max_soc:.0f}%): "
                        f"aktuell {soc_f:.0f}% – gestaffelt Noteinschalten ({detail})"
                    ),
                    "force": True,
                    "emergency": True,
                    "urgent": True,
                    "source": "max_soc_protection",
                }
            )
            added += 1
        if added or len(kept) != before:
            notes.append(
                f"Harte Grenze Max-SoC {max_soc:.0f}% wiederhergestellt "
                f"(SoC {soc_f:.0f}%, {added} gestaffelte Noteinschaltung(en) · {detail})."
            )
    return kept, notes


def enforce_hard_soc_rules(log_fn: Optional[LogFn] = None) -> None:
    """Beim Daemon-Start. Bei Verletzung laut protokollieren und abbrechen."""
    failures = verify_hard_soc_rules()
    write = log_fn or (lambda line: print(line, flush=True))
    if failures:
        for line in failures:
            write(f"[HART-FEHLER] {line}")
        write(
            "[HART-FEHLER] Steuerungsdaemon startet nicht: "
            "harte Min-/Max-SoC-Regeln fehlen oder sind verdrängt."
        )
        raise HardSocRulesMissing("; ".join(failures))
    write(
        "[HART] SoC-Regeln geprüft und aktiv: Min-Standard 35 %, Max-Standard 95 %, "
        "Nutzerwerte aus der Konfiguration. Vorrang vor Nachtsperre, Budget und Halt-Band. "
        "Unter dem eigenen Min Not-aus, ab dem eigenen Max gestaffelt Not-ein "
        "(höchste Prio zuerst, Nachschub bei Ladeüberschuss unter 80 % PV)."
    )
