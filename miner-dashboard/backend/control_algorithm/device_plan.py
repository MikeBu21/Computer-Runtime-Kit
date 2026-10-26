# -*- coding: utf-8 -*-
"""Geräte-Steuerungsplan für die Web-UI (Tabelle mit Begründung & Formel)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from control_algorithm.config import (
    device_label_map,
    format_device_priority,
    normalize_device_priority,
)
from control_algorithm.energy_balance import compute_horizon_projection, miner_surplus_kwh
from control_algorithm.planner import PRIORITY_LABEL, enabled_devices, next_soc_target
from control_algorithm.load_scheduler import normalize_min_runtime_hours


def _action_label(action: Optional[str]) -> str:
    if action == "resume":
        return "Einschalten"
    if action == "pause":
        return "Ausschalten"
    return "Unverändert"


def _status_label(running: bool) -> str:
    return "Läuft" if running else "Aus"


def _clock_relative(iso: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", ""))
    except Exception:
        return None
    now = now or datetime.now()
    mins = (dt - now).total_seconds() / 60.0
    clock = dt.strftime("%H:%M")
    if mins <= 2:
        return f"jetzt ({clock})"
    if mins < 60:
        return f"ab {clock} (in {int(round(mins))} min)"
    if mins < 24 * 60:
        return f"ab {clock} (in {mins / 60.0:.1f} h)"
    return f"ab {dt.strftime('%d.%m. %H:%M')}"


def _planned_when(
    act: Optional[Dict[str, Any]],
    target: Optional[Dict[str, Any]],
    projection: Optional[Dict[str, Any]] = None,
    *,
    schedule_when: Optional[str] = None,
) -> str:
    if schedule_when:
        return schedule_when
    if act and act.get("force"):
        return "Sofort (Sicherheit)"
    if act and act.get("action") in ("resume", "pause"):
        return "jetzt (nächster Tick)"
    if projection:
        t = projection.get("target_time") or (target.get("time") if target else None) or "07:00"
        try:
            end_raw = projection.get("target_datetime") or projection.get("horizon_end")
            if end_raw:
                dt = datetime.fromisoformat(str(end_raw))
                mins = max(0, int((dt - datetime.now()).total_seconds() / 60))
                return f"Nächstes SOC-Ziel {t} (in ca. {mins} min)"
        except Exception:
            pass
        return f"Nächstes SOC-Ziel {t}"
    if target and target.get("time"):
        mins = target.get("minutes_until")
        if mins is not None:
            return f"Nächstes SOC-Ziel {target['time']} (in ca. {mins} min)"
    return "Nächster Steuerungs-Tick (ca. alle 30 s)"


def _formula_for_device(
    d: Dict[str, Any],
    projection: Dict[str, Any],
    surplus_kwh: float,
    hours_left: float,
    budget_kw: float,
    action: Optional[str],
    act: Optional[Dict[str, Any]] = None,
) -> str:
    prio = PRIORITY_LABEL.get(d.get("priority", 5), "5")
    pwr = float(d.get("power_kw") or 0)
    risk = projection.get("risk_factor_percent", 100)
    pv = projection.get("pv_forecast_kwh", 0)
    hydro = projection.get("hydro_forecast_kwh", 0)
    base = projection.get("base_consumption_kwh", 0)
    buf = projection.get("effective_buffer_kwh", projection.get("buffer_kwh", 0))

    if act and act.get("force") and "Mindest-SoC" in str(act.get("reason", "")):
        return "Sofort-Abschaltung: SOC < Mindest-SoC (Sicherheitsregel, unabhängig von Priorität)."

    target_soc = projection.get("target_soc", "?")
    projected = projection.get("projected_soc", "?")
    core = (
        f"Miner-Budget ≈ Prognose-Speicher {projected}% am bindenden Checkpoint "
        f"(Ziel min. {target_soc}% an allen SOC-Zielen im 7-Tage-Horizont): "
        f"PV {pv} + Hydro {hydro} kWh − Grundverbrauch {base} kWh → Roh-Budget, "
        f"× Risiko {risk}% auf Laufzeit = {surplus_kwh:.1f} kWh "
        f"({hours_left:.1f} h → {budget_kw:.1f} kW effektiv). "
        f"Max-SoC wird kontinuierlich eingehalten."
    )
    if action == "resume":
        return (
            f"{core} Einschalten: Priorität {prio}, Bedarf {pwr:.1f} kW × {hours_left:.1f} h "
            f"= {pwr * hours_left:.1f} kWh; Budget reicht und {prio}-Priorität ist dran."
        )
    if action == "pause":
        return (
            f"{core} Ausschalten: Priorität {prio} – bei knappem Budget werden zuerst "
            f"niedrige Stufen (0…), zuletzt hohe (…10) pausiert."
        )
    return (
        f"{core} Priorität {prio}: Bei knappem PV wird nach Priorität 0–10 geschaltet "
        f"(10 zuletzt aus, zuerst ein)."
    )


def build_device_control_plans(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    pv_tomorrow_kwh: float,
) -> List[Dict[str, Any]]:
    """Erzeugt eine Zeile pro aktiviertem Gerät für die UI-Tabelle."""
    devices = enabled_devices(config)
    if not devices:
        return []

    notes = device_label_map()
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    planned_by_ip = {
        a["ip"]: a for a in (state.get("planned_actions") or []) if a.get("ip")
    }
    projection = (
        state.get("horizon_projection")
        or state.get("balance_projection")
        or compute_horizon_projection(config, state, live, base_dir)
    )
    target = next_soc_target(config.get("soc_targets") or [])
    surplus_kwh = miner_surplus_kwh(config, state, live, base_dir)
    soc = live.get("soc_percent")
    min_soc = float(config.get("min_soc_percent") or 35)
    max_soc = float(config.get("max_soc_percent") or 95)
    min_runtime = normalize_min_runtime_hours(config.get("min_device_runtime_hours"))
    risk = projection.get("risk_factor_percent", config.get("risk_factor_percent", 100))
    base_cons = projection.get("base_consumption_kwh", 0)
    profile_days = projection.get("profile_samples", 0)

    hours_left = float(projection.get("hours_left") or 72.0)
    now = datetime.now()
    try:
        if not projection.get("hours_left"):
            target_dt = datetime.fromisoformat(str(projection.get("horizon_end") or ""))
            hours_left = max(0.1, (target_dt - now).total_seconds() / 3600.0)
    except Exception:
        hours_left = 72.0
    budget_kw = float(projection.get("miner_budget_kw") or (surplus_kwh / hours_left if hours_left > 0 else 0))

    fill = state.get("fill_schedule") or {}
    flex = fill.get("flex") or {}
    late = state.get("late_start_plan") or {}
    late_slots: Dict[str, Dict[str, Any]] = {}
    try:
        from control_algorithm.decision_board import _allocate_late_discharge_slots

        if (
            late.get("defer")
            and not late.get("urgent")
            and float(late.get("excess_kwh") or 0) >= 0.4
        ):
            late_slots = _allocate_late_discharge_slots(config, late)
    except Exception:
        late_slots = {}

    rows: List[Dict[str, Any]] = []
    for d in devices:
        ip = d.get("ip", "")
        note = notes.get(ip) or ip
        running = miner_status.get(ip) == "running"
        act = planned_by_ip.get(ip)
        planned_action = act.get("action") if act else None
        schedule_when = None
        if running and not (act and act.get("action") == "pause"):
            schedule_when = "läuft (kein Zusatzstart nötig)"
        elif act and act.get("action") == "resume":
            schedule_when = "jetzt (nächster Tick)"
        elif act and act.get("action") == "pause":
            schedule_when = "jetzt (nächster Tick) · Ausschalten"
        elif not running and ip in late_slots:
            schedule_when = _clock_relative(str(late_slots[ip].get("planned_on") or ""), now)
            if schedule_when:
                schedule_when = f"{schedule_when} · Spätstart"
        elif not running and flex.get("ip") == ip and flex.get("planned_on"):
            schedule_when = _clock_relative(str(flex.get("planned_on")), now)
            if flex.get("should_start_now"):
                schedule_when = f"{schedule_when} · Füllplan sofort"
            elif flex.get("start_in_hours") is not None:
                schedule_when = (
                    f"{schedule_when} · Füllplan in {flex.get('start_in_hours')} h"
                )
            if fill.get("eta_max_display") or (flex.get("eta_max_with_flex_display")):
                bits = []
                if fill.get("eta_max_display"):
                    bits.append(f"Max ohne Flex ~{fill.get('eta_max_display')}")
                if flex.get("eta_max_with_flex_display"):
                    bits.append(f"mit Flex ~{flex.get('eta_max_with_flex_display')}")
                if fill.get("pv_end_display"):
                    bits.append(f"PV-Ende ~{fill.get('pv_end_display')}")
                if bits and schedule_when:
                    schedule_when = f"{schedule_when} ({' · '.join(bits)})"

        if state.get("night_lock_active"):
            why = (
                f"Nacht-Sperre aktiv. 7-Tage-Prognose: SOC {projection.get('projected_soc', '?')}% "
                f"< Ziel {projection.get('target_soc')}% (bindend: {projection.get('binding_constraint', '?')}). "
                f"PV morgen erwartet: {pv_tomorrow_kwh:.1f} kWh. "
                f"Batterie jetzt: {soc if soc is not None else '?'} %. "
                f"Historischer Grundverbrauch (Profil {profile_days} Tage): "
                f"ca. {base_cons:.1f} kWh bis Horizont-Ende."
            )
            formula = (
                "Nacht-Sperre = Speicher-Prognose reicht nicht für alle SOC-Checkpoints im 7-Tage-Horizont "
                f"(Max-SoC {max_soc:.0f} %). Keine Einschaltungen."
            )
            if running and not act:
                planned_action = "pause"
                act = {"action": "pause", "reason": state.get("night_lock_reason"), "force": True}
        elif state.get("anomaly_active"):
            why = (
                f"Anomalie: Verbrauch +{state.get('anomaly_excess_pct', '?')}% über historischem Schnitt. "
                f"PV morgen: {pv_tomorrow_kwh:.1f} kWh. SOC: {soc if soc is not None else '?'} %. "
                f"Keine neuen Einschaltungen bis Anomalie vorbei."
            )
            formula = "Bei Anomalie werden nur Abschaltungen erlaubt (Sicherheit)."
        elif soc is not None and float(soc) < min_soc:
            why = (
                f"Mindest-SoC ({min_soc:.0f}%) unterschritten – aktuell {float(soc):.0f} %. "
                f"Sofort-Abschaltung aller Geräte."
            )
            formula = "SOC < Mindest-SoC → sofort pause (Force, ignoriert Priorität & Budget)."
        elif act:
            why = (
                f"{act.get('reason', '')}. "
                f"PV morgen erwartet: {pv_tomorrow_kwh:.1f} kWh. "
                f"Batterie aktuell: {soc if soc is not None else '?'} %. "
                f"Miner-Budget: {surplus_kwh:.1f} kWh (Risiko {risk}%). "
                f"Grundverbrauch bis Ziel: ca. {base_cons:.1f} kWh "
                f"(Historie {profile_days} Wochentags-Profile)."
            )
            formula = _formula_for_device(
                d, projection, surplus_kwh, hours_left, budget_kw, planned_action, act
            )
        elif running:
            why = (
                f"Gerät läuft – Mindestlaufzeit {min_runtime:g} h wird eingehalten. "
                f"PV morgen: {pv_tomorrow_kwh:.1f} kWh. SOC: {soc if soc is not None else '?'} %. "
                f"Budget: {surplus_kwh:.1f} kWh."
            )
            formula = _formula_for_device(
                d, projection, surplus_kwh, hours_left, budget_kw, None
            )
        else:
            why = (
                f"Gerät aus – wartet auf ausreichend Budget oder höhere Priorität. "
                f"PV morgen: {pv_tomorrow_kwh:.1f} kWh. SOC: {soc if soc is not None else '?'} %. "
                f"Budget: {surplus_kwh:.1f} kWh, benötigt ca. "
                f"{float(d.get('power_kw') or 0) * hours_left:.1f} kWh "
                f"({float(d.get('power_kw') or 0):.1f} kW × {hours_left:.1f} h)."
            )
            formula = _formula_for_device(
                d, projection, surplus_kwh, hours_left, budget_kw, None
            )

        if act and act.get("blocked_min_runtime"):
            why += (
                f" Geplantes Ausschalten blockiert – Mindestlaufzeit "
                f"noch {act.get('min_runtime_remaining_h', '?')} h."
            )
            planned_action = None
        if act and act.get("blocked_hysteresis"):
            why += (
                f" Geplantes Umschalten blockiert – Hysterese "
                f"noch ca. {act.get('hysteresis_remaining_s', '?')} s."
            )
            planned_action = None

        rows.append(
            {
                "ip": ip,
                "name": note,
                "priority": format_device_priority(d.get("priority", 5)),
                "priority_key": normalize_device_priority(d.get("priority", 5)),
                "power_kw": float(d.get("power_kw") or 0),
                "status": _status_label(running),
                "planned_action": _action_label(planned_action),
                "planned_when": _planned_when(
                    act, target, projection, schedule_when=schedule_when
                ),
                "why": why.strip(),
                "formula": formula,
                "dry_run": bool(config.get("dry_run", True)),
            }
        )
    return rows
