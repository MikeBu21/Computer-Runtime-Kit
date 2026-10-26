# -*- coding: utf-8 -*-
"""
Prioritäts-Laufzeitplanung: Surplus zuerst an höchste Priorität (möglichst lange),
dann nächste Stufe, unterste nur als Spill/Export-Absorber.

Kein Neural Net – gierige Energiezuteilung unter Min-SoC-Reserve (bereits im Surplus).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from control_algorithm.config import normalize_device_priority
from control_algorithm.planner import PRIORITY_LABEL, sort_for_resume

# Unter dieser geplanten Dauer lohnt kein planmäßiges Einschalten (außer Export)
MIN_MEANINGFUL_HOURS = 0.75
# Spill (niedrigste Stufe) braucht klaren Rest oder Export
SPILL_MIN_HOURS = 1.0


def _device_role(index: int, n: int) -> str:
    """index 0 = höchste Priorität."""
    if n <= 0:
        return "anchor"
    if index == 0:
        return "anchor"
    if index == n - 1 and n >= 3:
        return "spill"
    return "flex"


def compute_priority_runtime_plan(
    devices: List[Dict[str, Any]],
    surplus_kwh: float,
    hours_left: float,
    *,
    min_soc_percent: float = 28.0,
    export_mode: bool = False,
    discharge_mode: bool = False,
) -> Dict[str, Any]:
    """
    Greedy: höchste Priorität bekommt so viele Stunden wie Surplus / Leistung erlaubt
    (gedeckelt auf hours_left), Rest an die nächste Stufe usw.

    Returns u.a. planned_hours je IP und preferred_continuous_kw (Summe der Geräte,
    die sinnvoll durchlaufen können).
    """
    ordered = sort_for_resume(list(devices))
    n = len(ordered)
    horizon_h = max(0.1, float(hours_left or 0.1))
    remaining = max(0.0, float(surplus_kwh or 0.0))

    rows: List[Dict[str, Any]] = []
    for i, d in enumerate(ordered):
        ip = str(d.get("ip") or "")
        pwr = max(0.0, float(d.get("power_kw") or 0))
        prio = normalize_device_priority(d.get("priority"))
        role = _device_role(i, n)
        if pwr <= 0 or not ip:
            rows.append(
                {
                    "ip": ip,
                    "name": ip,
                    "priority": prio,
                    "priority_label": PRIORITY_LABEL.get(prio, str(prio)),
                    "power_kw": pwr,
                    "role": role,
                    "planned_hours": 0.0,
                    "planned_kwh": 0.0,
                    "prefer_on": False,
                }
            )
            continue

        max_h = remaining / pwr
        planned_h = min(horizon_h, max_h)
        if planned_h < 0.05:
            planned_h = 0.0
        planned_kwh = round(planned_h * pwr, 2)
        remaining = max(0.0, remaining - planned_kwh)

        prefer_on = planned_h >= MIN_MEANINGFUL_HOURS
        if role == "spill":
            # Spill nur bei Restlaufzeit oder wenn Export/Entladen Überschuss braucht
            prefer_on = planned_h >= SPILL_MIN_HOURS or (
                (export_mode or discharge_mode) and planned_h >= 0.25
            )
        elif role == "flex" and not prefer_on:
            # Bei Entladen (über Max): Flex freigeben auch ohne Surplus-Stunden
            # (Surplus wird gierig dem Anker zugeteilt).
            # Bei Export: kurze Restlaufzeit reicht.
            if discharge_mode:
                prefer_on = True
            else:
                prefer_on = export_mode and planned_h >= 0.35

        rows.append(
            {
                "ip": ip,
                "priority": prio,
                "priority_label": PRIORITY_LABEL.get(prio, str(prio)),
                "power_kw": round(pwr, 2),
                "role": role,
                "planned_hours": round(planned_h, 2),
                "planned_kwh": planned_kwh,
                "prefer_on": prefer_on,
            }
        )

    preferred_kw = round(
        sum(float(r["power_kw"]) for r in rows if r.get("prefer_on")),
        2,
    )
    prefer_ips = {r["ip"] for r in rows if r.get("prefer_on") and r.get("ip")}

    anchor = next((r for r in rows if r.get("role") == "anchor"), None)
    summary_parts: List[str] = []
    if anchor and float(anchor.get("planned_hours") or 0) > 0:
        summary_parts.append(
            f"Prio {anchor.get('priority_label')} (Anker): "
            f"~{anchor['planned_hours']:.1f} h / {anchor['planned_kwh']:.0f} kWh"
        )
    for r in rows:
        if r.get("role") == "anchor":
            continue
        h = float(r.get("planned_hours") or 0)
        if h < 0.05:
            if r.get("role") == "spill":
                summary_parts.append(
                    f"Prio {r.get('priority_label')} (Spill): nur bei Export/Restüberschuss"
                )
            continue
        label = "Flex" if r.get("role") == "flex" else "Spill"
        summary_parts.append(
            f"Prio {r.get('priority_label')} ({label}): "
            f"~{h:.1f} h / {r['planned_kwh']:.0f} kWh"
        )

    return {
        "devices": rows,
        "by_ip": {r["ip"]: r for r in rows if r.get("ip")},
        "prefer_ips": prefer_ips,
        "preferred_continuous_kw": preferred_kw,
        "surplus_kwh": round(float(surplus_kwh or 0), 2),
        "hours_left": round(horizon_h, 2),
        "min_soc_percent": float(min_soc_percent),
        "export_mode": bool(export_mode),
        "discharge_mode": bool(discharge_mode),
        "summary": " · ".join(summary_parts) if summary_parts else "Kein Miner-Surplus für Laufzeitplan.",
    }


def may_resume_by_priority_plan(
    device: Dict[str, Any],
    plan: Dict[str, Any],
    *,
    export_mode: bool = False,
    discharge_mode: bool = False,
    higher_tier_running: bool = True,
    conserve_for_high_priority: bool = False,
) -> tuple[bool, str]:
    """
    Darf dieses Gerät jetzt eingeschaltet werden laut Laufzeitplan?

    - Anker: ja wenn geplant oder Export/Entladen
    - Flex: ja wenn prefer_on oder (Export und höhere Stufe läuft)
    - Spill: nur Export/Entladen oder klarer Rest (prefer_on)

    conserve_for_high_priority (Morgen-Floor / knappes Wetter):
    Spill nie; Flex nur wenn prefer_on – damit hohe Prio durchlaufen kann.
    """
    ip = str(device.get("ip") or "")
    row = (plan.get("by_ip") or {}).get(ip) or {}
    role = str(row.get("role") or "flex")
    prefer = bool(row.get("prefer_on"))
    planned_h = float(row.get("planned_hours") or 0)

    if not higher_tier_running and role != "anchor":
        return False, "höhere Priorität noch aus"

    if role == "anchor":
        if prefer or export_mode or discharge_mode or planned_h >= 0.35:
            return True, "Anker-Priorität"
        return False, "Surplus reicht nicht für sinnvolle Anker-Laufzeit"

    if conserve_for_high_priority:
        if role == "spill":
            return (
                False,
                "Morgen-Floor/Energiesparen – Spill bleibt aus (hohe Prio zuerst)",
            )
        if role == "flex" and not prefer:
            return (
                False,
                "Morgen-Floor/Energiesparen – niedrige Flex-Prio bleibt aus",
            )

    if role == "spill":
        # Spill nie proaktiv entladen (Spätstart) – nur Live-Export oder klarer Rest.
        # Sonst fressen T21s nachts Reserve, die hohe Prio am schwachen Tag braucht.
        if export_mode:
            return True, "Spill nur zur Export-/Überschuss-Aufnahme"
        if prefer and planned_h >= SPILL_MIN_HOURS:
            return True, "Rest-Surplus nach höheren Prioritäten"
        return False, "Spill wartet – Surplus geht zuerst an höhere Prioritäten"

    # flex
    if prefer:
        return True, "Flex-Priorität laut Laufzeitplan"
    if export_mode and higher_tier_running:
        return True, "Flex zur Export-/Überschuss-Aufnahme"
    # Proaktives Entladen: prefer_on oder klare Restlaufzeit
    if discharge_mode and higher_tier_running and (prefer or planned_h >= 0.35):
        return True, "Flex-Entladen laut Laufzeitplan"
    return False, "Flex noch nicht – Surplus reserviert für längere Anker-Laufzeit"


def allocate_surplus_by_priority(
    devices: List[Dict[str, Any]],
    surplus_kwh: float,
    hours_left: float,
) -> Dict[str, Dict[str, float]]:
    """
    Für UI-Balken: kWh/kW-Zuteilung gierig nach Priorität
    (statt proportionalem Anteil).
    """
    plan = compute_priority_runtime_plan(
        devices,
        surplus_kwh,
        hours_left,
        export_mode=False,
        discharge_mode=False,
    )
    out: Dict[str, Dict[str, float]] = {}
    for row in plan.get("devices") or []:
        ip = row.get("ip")
        if not ip:
            continue
        out[str(ip)] = {
            "allocated_kwh": float(row.get("planned_kwh") or 0),
            "allocated_kw": round(
                float(row.get("planned_kwh") or 0) / max(0.1, float(hours_left or 0.1)),
                2,
            ),
            "planned_hours": float(row.get("planned_hours") or 0),
            "role": str(row.get("role") or ""),
        }
    return out


def is_energy_stress(
    *,
    soc: Optional[float],
    min_soc: float,
    running_load_kw: float,
    budget_kw: float,
) -> bool:
    """Engpass: nahe Min-SoC oder Last klar über Budget → Flex früher abwerfen."""
    if soc is not None and float(soc) <= float(min_soc) + 8.0:
        return True
    if budget_kw >= 0 and running_load_kw > budget_kw + 1.5:
        return True
    return False


def should_force_pause_flex(
    device: Dict[str, Any],
    plan: Dict[str, Any],
    *,
    energy_stress: bool,
    export_mode: bool,
) -> bool:
    """
    Bei Energie-Stress: Flex/Spill trotz Mindestlaufzeit pausieren dürfen,
    damit Anker + Min-SoC geschützt bleiben. Bei Export Spill/Flex behalten
    (außer energy_stress bereits für Morgen-Floor mit export_mode=False gesetzt).
    """
    if export_mode or not energy_stress:
        return False
    ip = str(device.get("ip") or "")
    row = (plan.get("by_ip") or {}).get(ip) or {}
    role = str(row.get("role") or "flex")
    if not row:
        for d in plan.get("devices") or []:
            if str(d.get("ip") or "") == ip:
                role = str(d.get("role") or "flex")
                break
    return role in ("flex", "spill")
