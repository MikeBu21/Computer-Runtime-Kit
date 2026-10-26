# -*- coding: utf-8 -*-
"""
Schiedsrichter (Akteur 3) zwischen Einspeisungs-Vermeidung und Reserve-Planung.

Akteur 1: Einspeisung vermeiden / proaktives Entladen Richtung Morgen-Checkpoint
Akteur 2: Energie-Budget / Schlechtwetter-Reserve (Nacht ohne PV drosseln)
Akteur 3: balanciert beide – Export nur mit nötiger Last, Reserve darf darüber hinaus sparen.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

# Bindings aus der Energiebilanz, die Export-/Entlade-Priorität signalisieren
EXPORT_PRIORITY_BINDINGS = frozenset(
    {
        "export_avoidance",
        "proactive_discharge",
        "near_term_discharge",
        "max_soc",
        "discharge_above_max",
    }
)

# Bindings: Speicher aufladen / Reserve – Miner-Last drosseln, Export-Guard nicht hochlatchen
RESERVE_PRIORITY_BINDINGS = frozenset(
    {
        "pre_storm_charge",
        "survival_mode",
        "min_soc",
        "min_soc_emergency",
    }
)


def export_priority_active(
    *,
    discharge_mode: bool,
    export_mode: bool,
    binding: str,
    state: Dict[str, Any],
    live_export_kw: float = 0.0,
    learn_thr: float = 0.0,
) -> bool:
    """True wenn Akteur-1-Logik (kein Einspeisen) gerade führend ist."""
    if live_export_kw >= learn_thr and learn_thr > 0:
        return True
    if export_mode or discharge_mode:
        return True
    if str(binding or "") in EXPORT_PRIORITY_BINDINGS:
        return True
    if reserve_priority_active(binding=binding, budget_kw=0.0, morning_threat=False):
        return False
    return bool(state.get("export_guard_active"))


def reserve_priority_active(
    *,
    binding: str,
    budget_kw: float,
    morning_threat: bool,
) -> bool:
    """True wenn Reserve/Sparen Vorrang vor voller Flotte hat."""
    if morning_threat:
        return True
    b = str(binding or "")
    if b in RESERVE_PRIORITY_BINDINGS:
        return True
    if b.startswith("checkpoint_") and budget_kw < 0.05:
        return True
    return False


def compute_export_absorb_floor(
    devices: List[Dict[str, Any]],
    *,
    live_export_kw: float,
    learn_thr: float,
    pv_surplus_kw: float = 0.0,
    peak_spill_boost: float = 0.0,
) -> float:
    """
    Mindest-Last nur um Live-Einspeisung / PV-Überschuss zu absorbieren –
    nicht die volle Flotte.
    """
    if live_export_kw < learn_thr and pv_surplus_kw < 0.5:
        return 0.0
    need = max(
        live_export_kw + 0.5 if live_export_kw >= learn_thr else 0.0,
        pv_surplus_kw + 0.5 if pv_surplus_kw >= 0.5 else 0.0,
    )
    if need <= 0:
        return 0.0
    if peak_spill_boost > 0.05:
        need = need * (1.0 + peak_spill_boost * 0.35)
    try:
        from control_algorithm.planner import sort_for_resume

        ordered = sort_for_resume(list(devices))
    except Exception:
        ordered = list(devices)
    stacked = 0.0
    for d in ordered:
        stacked += float(d.get("power_kw") or 0)
        if stacked + 0.05 >= need:
            break
    return round(stacked, 3)


def _anchor_kw_from_plan(priority_plan: Dict[str, Any]) -> float:
    total = 0.0
    for row in priority_plan.get("devices") or []:
        if str(row.get("role") or "") == "anchor":
            try:
                total += float(row.get("power_kw") or 0)
            except (TypeError, ValueError):
                pass
    return round(total, 3)


def compute_balanced_load_cap(
    *,
    budget_kw: float,
    preferred_kw: float,
    priority_plan: Dict[str, Any],
    export_absorb_floor: float,
    reserve_priority: bool,
    live_export_kw: float,
    learn_thr: float,
    sustainable_cap: Optional[float] = None,
    binding: str = "",
) -> Tuple[float, float, str]:
    """
    Berechnet nachhaltige Max-Last und Export-Mindest-Floor.

    Returns (effective_cap_kw, export_floor_kw, mode_label)
    """
    anchor_kw = _anchor_kw_from_plan(priority_plan)
    export_floor = 0.0
    if live_export_kw >= learn_thr:
        export_floor = max(0.0, float(export_absorb_floor))

    sus = sustainable_cap
    if sus is not None:
        try:
            sus = max(0.0, float(sus))
        except (TypeError, ValueError):
            sus = None

    # Reserve / Vorladung / Morgen-Floor: mathematisch teilweise Last erlauben
    if reserve_priority and live_export_kw < learn_thr:
        b = str(binding or "")
        # Vorladung/Überleben ohne Einspeisung: nur Anker – auch wenn Füllplan Budget aufbläht
        if b in ("pre_storm_charge", "survival_mode"):
            cap = round(max(0.0, anchor_kw), 3)
            label = (
                "Schlechtwetter-Vorladung"
                if b == "pre_storm_charge"
                else "Überlebensmodus"
            )
            return cap, 0.0, label
        if sus is not None and sus > 0:
            cap = max(anchor_kw, min(sus, preferred_kw if preferred_kw > 0 else sus))
        elif budget_kw > 0.05:
            cap = max(budget_kw, anchor_kw)
        elif preferred_kw > 0.05:
            cap = min(preferred_kw, sus if sus is not None else preferred_kw)
        else:
            cap = anchor_kw
        cap = round(max(0.0, cap), 3)
        label = str(binding or "reserve")
        if label == "pre_storm_charge":
            label = "Schlechtwetter-Vorladung"
        elif label == "survival_mode":
            label = "Überlebensmodus"
        elif label == "morning_floor":
            label = "Morgen-Floor"
        return cap, 0.0, label

    # Live-Einspeisung + optional Reserve-Stress
    if live_export_kw >= learn_thr or export_floor > 0:
        if reserve_priority and sus is not None:
            cap = round(max(export_floor, min(sus, preferred_kw or sus)), 3)
        else:
            cap = round(
                max(
                    budget_kw,
                    export_floor,
                    preferred_kw if budget_kw < 0.05 else 0.0,
                ),
                3,
            )
        return cap, export_floor, "export+reserve" if reserve_priority else "export"

    if budget_kw > 0.05:
        return round(max(budget_kw, preferred_kw), 3), 0.0, "budget"
    if preferred_kw > 0.05:
        return round(preferred_kw, 3), 0.0, "plan"
    return round(anchor_kw, 3), 0.0, "anchor_only"


def reconcile_export_guard(
    state: Dict[str, Any],
    *,
    binding: str,
    live_export_kw: float,
    learn_thr: float,
    soc: Optional[float],
    checkpoint_soc: float,
    balanced_cap: float,
    export_absorb_floor: float,
    reserve_priority: bool,
) -> List[str]:
    """
    Export-Guard an Schiedsrichter-Entscheidung anpassen:
    - Reserve/Vorladung ohne Einspeisung → Guard löschen
    - Live-Einspeisung → Floor nur auf Absorb-Last (nicht volle Flotte)
    - Sonst Floor an nachhaltiges Cap senken
    """
    messages: List[str] = []

    if reserve_priority and live_export_kw < learn_thr:
        if state.get("export_guard_active"):
            maybe_clear_export_guard(
                state,
                soc=soc,
                checkpoint_soc=checkpoint_soc,
                force=True,
            )
            label = str(binding or "Reserve")
            if label == "pre_storm_charge":
                label = "Schlechtwetter-Vorladung"
            messages.append(
                f"Schiedsrichter: Export-Guard aus – {label} "
                f"(keine Live-Einspeisung, Sparen hat Vorrang)."
            )
        return messages

    if live_export_kw >= learn_thr:
        floor = max(export_absorb_floor, 0.0)
        update_export_guard(
            state,
            active=True,
            floor_kw=floor,
            reason=(
                f"Einspeisung {live_export_kw:.2f} kW absorbieren "
                f"(Floor {floor:.1f} kW, nicht volle Flotte)"
            ),
            soc=soc,
            checkpoint_soc=checkpoint_soc,
            allow_lower=True,
        )
        messages.append(
            f"Schiedsrichter: Export-Floor {floor:.1f} kW "
            f"(nur nötige Last gegen {live_export_kw:.2f} kW Einspeisung)."
        )
        return messages

    prev = float(state.get("export_guard_floor_kw") or 0)
    if state.get("export_guard_active") and balanced_cap + 0.05 < prev:
        lower_export_guard_for_morning(
            state,
            floor_kw=balanced_cap,
            reason=(
                f"Schiedsrichter: Cap {balanced_cap:.1f} kW "
                f"(sticky {prev:.1f} kW → nachhaltig)"
            ),
        )
        messages.append(
            f"Schiedsrichter: Export-Guard {prev:.1f} → {balanced_cap:.1f} kW "
            f"(mathematisch nachhaltige Last)."
        )
    elif not state.get("export_guard_active"):
        update_export_guard(
            state,
            active=False,
            floor_kw=0.0,
            reason="",
            soc=soc,
            checkpoint_soc=checkpoint_soc,
        )
    return messages


def update_export_guard(
    state: Dict[str, Any],
    *,
    active: bool,
    floor_kw: float,
    reason: str,
    soc: Optional[float],
    checkpoint_soc: float,
    allow_lower: bool = False,
) -> None:
    """
    Sticky Latch: einmal aktiv, bleibt bis SOC nahe Morgen-Ziel –
    verhindert Nacht-Oszillation (ein → aus → ein).

    allow_lower=True: Floor darf sinken (Morgen-Floor / Schiedsrichter-Cap).
    """
    if active:
        prev = float(state.get("export_guard_floor_kw") or 0)
        state["export_guard_active"] = True
        new_floor = float(floor_kw)
        if allow_lower:
            state["export_guard_floor_kw"] = round(new_floor, 3)
        else:
            state["export_guard_floor_kw"] = round(max(prev, new_floor), 3)
        state["export_guard_reason"] = reason
        return

    if not state.get("export_guard_active"):
        return
    if soc is not None and float(soc) <= float(checkpoint_soc) + 2.0:
        state["export_guard_active"] = False
        state["export_guard_floor_kw"] = 0.0
        state["export_guard_reason"] = None


def maybe_clear_export_guard(
    state: Dict[str, Any],
    *,
    soc: Optional[float],
    checkpoint_soc: float,
    force: bool = False,
) -> None:
    if force or (
        state.get("export_guard_active")
        and soc is not None
        and float(soc) <= float(checkpoint_soc) + 2.0
    ):
        state["export_guard_active"] = False
        state["export_guard_floor_kw"] = 0.0
        state["export_guard_reason"] = None


def lower_export_guard_for_morning(
    state: Dict[str, Any],
    *,
    floor_kw: float,
    reason: str,
) -> None:
    """Export-Guard-Floor auf nachhaltiges Cap senken (nicht sticky max)."""
    state["export_guard_active"] = True
    state["export_guard_floor_kw"] = round(max(0.0, float(floor_kw)), 3)
    state["export_guard_reason"] = reason


def arbitrate_load_decisions(
    *,
    actor1_actions: List[Dict[str, Any]],
    actor2_actions: List[Dict[str, Any]],
    other_actions: List[Dict[str, Any]],
    export_priority: bool,
    export_floor_kw: float,
    devices_by_ip: Dict[str, Dict[str, Any]],
    allow_actor2_resume: bool = False,
    balanced_cap_kw: Optional[float] = None,
    reserve_priority: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Merged geplante Aktionen.

    Bei Export-Vorrang:
    - Resume von Akteur 1 bleiben
    - Pause von Akteur 2 darf Last unter export_floor_kw drücken, wenn
      balanced_cap_kw gesetzt und Reserve-Priorität – gestaffelt sparen
    - Pause auf IP, die Akteur 1 gerade einschaltet → verworfen
    """
    messages: List[str] = []
    actor1_resume_ips: Set[str] = {
        str(a.get("ip"))
        for a in actor1_actions
        if a.get("action") == "resume" and a.get("ip")
    }

    cap = balanced_cap_kw
    floor = float(export_floor_kw)
    if cap is not None and cap + 0.05 < floor and reserve_priority:
        floor = max(0.0, cap)
        messages.append(
            f"Schiedsrichter: Export-Floor auf {floor:.1f} kW begrenzt "
            f"(Reserve-Cap {cap:.1f} kW)."
        )

    if not export_priority:
        return other_actions + actor1_actions + actor2_actions, messages

    if floor > 0:
        messages.append(
            f"Schiedsrichter: Export-Mindest-Last {floor:.1f} kW "
            f"(Budget-Pausen nur darüber hinaus)."
        )
    elif reserve_priority and cap is not None:
        messages.append(
            f"Schiedsrichter: Reserve-Cap {cap:.1f} kW – "
            f"gestaffelt sparen, hohe Priorität bleibt."
        )
    else:
        messages.append(
            "Schiedsrichter: Einspeisungs-Vermeidung (Akteur 1) vor "
            "Reserve-Budget (Akteur 2)."
        )

    filtered_actor2: List[Dict[str, Any]] = []
    simulated_load = float(floor if floor > 0 else (cap or 0.0))

    for act in actor2_actions:
        action = act.get("action")
        ip = str(act.get("ip") or "")
        if action == "pause" and ip in actor1_resume_ips:
            messages.append(
                f"Schiedsrichter verwirft Budget-Aus für {ip}: "
                f"gerade für Einspeisungs-Vermeidung eingeschaltet."
            )
            continue
        if action == "pause":
            pwr = float((devices_by_ip.get(ip) or {}).get("power_kw") or 0)
            min_after = floor if floor > 0 else 0.0
            if cap is not None and reserve_priority and floor <= 0:
                min_after = max(0.0, cap - pwr)
            if simulated_load - pwr + 0.05 < min_after:
                messages.append(
                    f"Schiedsrichter blockiert Budget-Aus für {ip}: "
                    f"würde unter Mindest-Last {min_after:.1f} kW fallen."
                )
                continue
            simulated_load -= pwr
            act = dict(act)
            act["force"] = False
            reason = str(act.get("reason") or "")
            if "Schiedsrichter" not in reason:
                act["reason"] = reason + " · Schiedsrichter: Cap gehalten"
            filtered_actor2.append(act)
            continue
        if action == "resume":
            if allow_actor2_resume:
                pwr = float((devices_by_ip.get(ip) or {}).get("power_kw") or 0)
                simulated_load += pwr
                filtered_actor2.append(act)
                continue
            messages.append(
                f"Schiedsrichter überspringt Budget-Ein für {ip}: "
                f"Export-Vorrang regelt Einschalten."
            )
            continue
        filtered_actor2.append(act)

    merged = _dedupe_by_ip(other_actions + actor1_actions + filtered_actor2, export_priority)
    return merged, messages


def _dedupe_by_ip(
    actions: List[Dict[str, Any]],
    export_priority: bool,
) -> List[Dict[str, Any]]:
    """Pro IP eine Aktion; bei Konflikt Resume (Export/Cohort) schlägt Pause."""
    by_ip: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for act in actions:
        ip = str(act.get("ip") or "")
        if not ip:
            continue
        prev = by_ip.get(ip)
        if prev is None:
            by_ip[ip] = act
            order.append(ip)
            continue
        prev_a = prev.get("action")
        new_a = act.get("action")
        if prev_a == new_a:
            if new_a == "pause" and act.get("force") and not prev.get("force"):
                by_ip[ip] = act
            elif new_a == "resume" or not act.get("force"):
                by_ip[ip] = act
            continue
        if export_priority and new_a == "resume" and prev_a == "pause":
            by_ip[ip] = act
        elif export_priority and new_a == "pause" and prev_a == "resume":
            continue
        elif new_a == "resume" and prev_a == "pause" and not prev.get("force"):
            by_ip[ip] = act
        elif new_a == "pause" and prev_a == "resume" and act.get("force") and not export_priority:
            by_ip[ip] = act
        else:
            by_ip[ip] = act
    return [by_ip[ip] for ip in order]
