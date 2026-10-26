# -*- coding: utf-8 -*-
"""
Entscheidungs-Cockpit: Layer-Status, Signalbus Algo1/2→3, Geräte-Zeitplan.

Baut strukturierte Transparenz aus bestehenden State-Keys – ändert keine Schaltlogik.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.config import (
    device_label_map,
    format_device_priority,
    normalize_device_priority,
)
from control_algorithm.planner import PRIORITY_LABEL, enabled_devices
from control_algorithm.weather_survival import live_grid_export_kw


def _pct(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _iso_display(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", ""))
        return dt.strftime("%d.%m. %H:%M")
    except Exception:
        s = str(iso)
        return s[:16].replace("T", " ") if len(s) >= 16 else s


def _relative_start_label(iso: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """Menschliche EIN-Zeit: 'jetzt', 'in 25 min', 'ab 15:40'."""
    dt = _parse_iso(iso)
    if dt is None:
        return None
    now = now or datetime.now()
    mins = (dt - now).total_seconds() / 60.0
    clock = dt.strftime("%H:%M")
    if mins <= 2:
        return f"jetzt ({clock})"
    if mins < 60:
        return f"ab {clock} (in {int(round(mins))} min)"
    hours = mins / 60.0
    if hours < 24:
        return f"ab {clock} (in {hours:.1f} h)"
    return f"ab {_iso_display(iso)}"


def _parse_iso(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", ""))
    except Exception:
        return None


def _build_activation_timeline(
    devices: List[Dict[str, Any]],
    state: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    """Kurzüberblick: wann welches Zusatzgerät geplant einschaltet."""
    fill = state.get("fill_schedule") or {}
    notes = device_label_map()
    upcoming: List[Dict[str, Any]] = []
    for d in devices:
        if d.get("running"):
            continue
        on = d.get("planned_on")
        if not on:
            continue
        dt = _parse_iso(on)
        if dt is None:
            continue
        upcoming.append(
            {
                "ip": d.get("ip"),
                "name": d.get("name"),
                "power_kw": d.get("power_kw"),
                "priority_label": d.get("priority_label"),
                "planned_on": on,
                "planned_on_display": d.get("planned_on_display"),
                "start_in_display": d.get("start_in_display") or _relative_start_label(on, now),
                "wait_note": d.get("wait_note"),
                "reason": (d.get("reason") or "")[:160],
                "minutes_until": round(max(0.0, (dt - now).total_seconds() / 60.0), 1),
            }
        )
    upcoming.sort(key=lambda x: float(x.get("minutes_until") or 0))
    next_extra = upcoming[0] if upcoming else None

    lines: List[str] = []
    if next_extra:
        lines.append(
            f"Nächstes Zusatzgerät: {next_extra.get('name')} "
            f"{next_extra.get('start_in_display')} "
            f"({next_extra.get('power_kw')} kW, Prio {next_extra.get('priority_label')})"
        )
    else:
        lines.append("Kein zusätzliches Gerät geplant – wartet auf Surplus/Export oder Füllplan.")

    if fill.get("ok"):
        pv_end = fill.get("pv_end_display")
        eta_steep = fill.get("eta_max_display")
        eta_flex = (fill.get("flex") or {}).get("eta_max_with_flex_display")
        bits = []
        if pv_end:
            bits.append(f"PV-Ende ~{pv_end}")
        if eta_steep:
            bits.append(f"Max-SoC ohne Flex ~{eta_steep}")
        if eta_flex:
            bits.append(f"mit Flex ~{eta_flex}")
        if bits:
            lines.append(" · ".join(bits))
        flex = fill.get("flex") or {}
        if flex.get("planned_on") and flex.get("start_in_hours") is not None:
            nm = notes.get(str(flex.get("ip") or ""), flex.get("ip"))
            lines.append(
                f"Füllplan {nm}: {_relative_start_label(flex.get('planned_on'), now)}"
                + (
                    " · sofort"
                    if flex.get("should_start_now")
                    else f" · in {flex.get('start_in_hours')} h"
                )
            )

    return {
        "summary": " | ".join(lines),
        "lines": lines,
        "next_extra_device": next_extra,
        "upcoming": upcoming,
        "pv_end_display": fill.get("pv_end_display"),
        "eta_max_without_flex": fill.get("eta_max_display"),
        "eta_max_with_flex": (fill.get("flex") or {}).get("eta_max_with_flex_display"),
        "updated_at": now.isoformat(),
    }


def _layer_chip(
    *,
    key: str,
    label: str,
    status: str,
    detail: str,
    value: Optional[float] = None,
    unit: str = "%",
    active: bool = False,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,  # planned | intervened
        "detail": detail,
        "value": value,
        "unit": unit,
        "active": bool(active),
    }


def _build_layers(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> List[Dict[str, Any]]:
    layers: List[Dict[str, Any]] = []

    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    soc = live.get("soc_percent")
    if soc is None:
        soc = state.get("live_soc")
    soc_f = _pct(soc)
    bat_lock = bool(state.get("min_soc_lock_active"))
    if soc_f is not None and soc_f < min_soc:
        layers.append(
            _layer_chip(
                key="battery_guard",
                label="Batterie-Schutz",
                status="intervened",
                detail=(
                    state.get("min_soc_lock_reason")
                    or f"SoC {soc_f:.0f}% unter Min {min_soc:.0f}% – Last gesperrt"
                ),
                value=soc_f,
                active=True,
            )
        )
    elif soc_f is not None and soc_f > max_soc:
        layers.append(
            _layer_chip(
                key="battery_guard",
                label="Batterie-Schutz",
                status="intervened",
                detail=f"SoC {soc_f:.0f}% über Max {max_soc:.0f}% – Entladen priorisieren",
                value=soc_f,
                active=True,
            )
        )
    else:
        layers.append(
            _layer_chip(
                key="battery_guard",
                label="Batterie-Schutz",
                status="planned",
                detail=f"SoC im Fenster {min_soc:.0f}–{max_soc:.0f} %",
                value=soc_f,
                active=bool(bat_lock),
            )
        )

    ov = state.get("pv_forecast_override") or {}
    pace = _pct(ov.get("pace_deviation_pct"))
    if ov.get("active"):
        layers.append(
            _layer_chip(
                key="prod_pattern",
                label="Produktion Muster",
                status="intervened",
                detail=f"Override aktiv ({ov.get('reason') or 'Pace'})",
                value=pace,
                active=True,
            )
        )
    else:
        layers.append(
            _layer_chip(
                key="prod_pattern",
                label="Produktion Muster",
                status="planned",
                detail="wie geplant (kein Override)",
                value=pace,
                active=False,
            )
        )

    safety_on = bool(state.get("pv_safety_active"))
    layers.append(
        _layer_chip(
            key="prod_safety",
            label="Produktion Sicherheit",
            status="intervened" if safety_on else "planned",
            detail=(
                state.get("pv_safety_reason")
                or ("PV-Sicherheitsabschaltung aktiv" if safety_on else "wie geplant")
            ),
            value=_pct(state.get("pv_safety_deviation_pct")),
            active=safety_on,
        )
    )

    night = bool(state.get("night_lock_active"))
    binding = str(
        (state.get("balance_projection") or {}).get("binding_constraint")
        or (state.get("horizon_projection") or {}).get("binding_constraint")
        or ""
    )
    if night:
        layers.append(
            _layer_chip(
                key="weather_balance",
                label="Wetter / Bilanz",
                status="intervened",
                detail=str(state.get("night_lock_reason") or "Nacht-Sperre aktiv")[:120],
                value=None,
                active=True,
            )
        )
    elif binding and binding not in ("none", ""):
        layers.append(
            _layer_chip(
                key="weather_balance",
                label="Wetter / Bilanz",
                status="intervened",
                detail=f"Bindung: {binding}",
                value=None,
                active=True,
            )
        )
    else:
        layers.append(
            _layer_chip(
                key="weather_balance",
                label="Wetter / Bilanz",
                status="planned",
                detail="wie geplant",
                value=None,
                active=False,
            )
        )

    anom = bool(state.get("anomaly_active"))
    excess = _pct(state.get("anomaly_excess_pct"))
    if anom:
        layers.append(
            _layer_chip(
                key="cons_anomaly",
                label="Verbrauch Anomalie",
                status="intervened",
                detail=f"Anomalie gegriffen (+{excess if excess is not None else '?'} %)",
                value=excess,
                active=True,
            )
        )
        layers.append(
            _layer_chip(
                key="cons_history",
                label="Verbrauch Historie",
                status="intervened",
                detail="Historie übersteuert (Anomalie)",
                value=excess,
                active=True,
            )
        )
    else:
        layers.append(
            _layer_chip(
                key="cons_history",
                label="Verbrauch Historie",
                status="planned",
                detail="wie geplant",
                value=None,
                active=False,
            )
        )
        layers.append(
            _layer_chip(
                key="cons_anomaly",
                label="Verbrauch Anomalie",
                status="planned",
                detail="keine Anomalie",
                value=None,
                active=False,
            )
        )

    risk = _pct(
        (state.get("balance_projection") or {}).get("risk_factor_percent")
        or config.get("risk_factor_percent")
        or 100
    )
    risk_active = risk is not None and risk < 99.5
    layers.append(
        _layer_chip(
            key="risk_reserve",
            label="Autopilot Risiko",
            status="intervened" if risk_active else "planned",
            detail=(
                f"Reserve zurück: Risk-Faktor {risk:.0f} %"
                if risk_active
                else "voller Risk-Faktor (100 %)"
            ),
            value=risk,
            active=bool(risk_active),
        )
    )

    late = state.get("late_start_plan") or {}
    if late.get("defer") and not late.get("urgent"):
        layers.append(
            _layer_chip(
                key="late_start",
                label="Spätstart",
                status="intervened",
                detail=str(late.get("summary") or "warten")[:140],
                value=_pct(late.get("hours_needed")),
                unit="h",
                active=True,
            )
        )
    else:
        layers.append(
            _layer_chip(
                key="late_start",
                label="Spätstart",
                status="planned",
                detail=str(late.get("summary") or "nicht aktiv / Fenster offen")[:140],
                value=None,
                active=False,
            )
        )

    return layers


def _build_signals(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> List[Dict[str, Any]]:
    ov = state.get("pv_forecast_override") or {}
    pace = _pct(ov.get("pace_deviation_pct"))
    safety_on = bool(state.get("pv_safety_active"))
    export_kw = live_grid_export_kw(live)
    proj = state.get("balance_projection") or state.get("horizon_projection") or {}
    budget = _pct(proj.get("miner_budget_kw"))
    if budget is not None:
        try:
            budget = round(float(proj.get("miner_budget_kw") or 0), 2)
        except (TypeError, ValueError):
            budget = None
    binding = str(proj.get("binding_constraint") or "")
    late = state.get("late_start_plan") or {}
    night = bool(state.get("night_lock_active"))
    anom = bool(state.get("anomaly_active"))

    # Algo 1 – Produktion
    a1_dir = "neutral"
    a1_active = False
    a1_text = "Produktion im Plan – kein Eingriff an Algo 3."
    a1_nums: Dict[str, Any] = {
        "pace_deviation_pct": pace,
        "effective_kwh": ov.get("effective_kwh"),
        "pv_safety_deviation_pct": _pct(state.get("pv_safety_deviation_pct")),
    }
    if safety_on:
        a1_dir = "weniger_verbrauch"
        a1_active = True
        a1_text = (
            f"Algo 1 → Algo 3: weniger verbrauchen – PV-Sicherheit "
            f"(Pace {a1_nums['pv_safety_deviation_pct']} %)."
        )
    elif ov.get("active") and pace is not None and pace <= -10:
        a1_dir = "weniger_verbrauch"
        a1_active = True
        a1_text = (
            f"Algo 1 → Algo 3: knapper / weniger – Muster-Override Pace {pace} % "
            f"(eff. {ov.get('effective_kwh')} kWh)."
        )
    elif ov.get("active") and pace is not None and pace >= 10:
        a1_dir = "mehr_verbrauch"
        a1_active = True
        a1_text = (
            f"Algo 1 → Algo 3: mehr Spielraum – Pace +{pace} % "
            f"(eff. {ov.get('effective_kwh')} kWh)."
        )

    # Algo 2 – Bilanz / Export
    a2_dir = "neutral"
    a2_active = False
    a2_text = "Bilanz im Plan – kein Extra-Signal an Algo 3."
    a2_nums: Dict[str, Any] = {
        "export_kw": round(export_kw, 2),
        "miner_budget_kw": budget,
        "excess_kwh": late.get("excess_kwh"),
        "latest_start": late.get("latest_start"),
        "binding": binding or None,
    }
    if night or anom:
        a2_dir = "weniger_verbrauch"
        a2_active = True
        why = "Nacht-Sperre" if night else f"Anomalie +{state.get('anomaly_excess_pct')} %"
        a2_text = f"Algo 2 → Algo 3: weniger verbrauchen – {why}."
    elif export_kw >= 0.05 or binding == "export_avoidance":
        a2_dir = "mehr_verbrauch"
        a2_active = True
        a2_text = (
            f"Algo 2 → Algo 3: mehr verbrauchen – Einspeisungsrisiko "
            f"({export_kw:.2f} kW Export, Budget {budget} kW)."
        )
    elif late.get("defer") and not late.get("urgent"):
        a2_dir = "warten"
        a2_active = True
        a2_text = (
            f"Algo 2 → Algo 3: warten – Spätstart bis "
            f"{_iso_display(late.get('latest_start'))} "
            f"(Überschuss {late.get('excess_kwh')} kWh nur für Anker/"
            f"höchste Prio, ~{late.get('miner_kw')} kW – nicht alle parallel)."
        )
    elif binding in ("max_soc", "proactive_discharge", "near_term_discharge"):
        a2_dir = "mehr_verbrauch"
        a2_active = True
        a2_text = f"Algo 2 → Algo 3: mehr verbrauchen – Bindung {binding}."

    # Algo 3 – Last
    actions = state.get("planned_actions") or []
    resumes = sum(1 for a in actions if a.get("action") == "resume")
    pauses = sum(1 for a in actions if a.get("action") == "pause")
    a3_dir = "neutral"
    if resumes and not pauses:
        a3_dir = "mehr_verbrauch"
    elif pauses and not resumes:
        a3_dir = "weniger_verbrauch"
    elif resumes and pauses:
        a3_dir = "gemischt"
    a3_text = (
        f"Algo 3 Laststeuerung: {resumes}× EIN, {pauses}× AUS "
        f"(Budget {budget} kW)."
        if actions
        else f"Algo 3: keine Schaltaktion diesen Tick (Budget {budget} kW)."
    )

    return [
        {
            "id": "algo1",
            "name": "Algo 1 · Produktion",
            "direction": a1_dir,
            "active": a1_active,
            "text": a1_text,
            "numbers": a1_nums,
        },
        {
            "id": "algo2",
            "name": "Algo 2 · Bilanz / Export",
            "direction": a2_dir,
            "active": a2_active,
            "text": a2_text,
            "numbers": a2_nums,
        },
        {
            "id": "algo3",
            "name": "Algo 3 · Laststeuerung",
            "direction": a3_dir,
            "active": bool(actions),
            "text": a3_text,
            "numbers": {
                "resume_count": resumes,
                "pause_count": pauses,
                "miner_budget_kw": budget,
            },
        },
    ]


def _prio_hours_by_ip(state: Dict[str, Any]) -> Dict[str, float]:
    plan = state.get("priority_runtime_plan") or {}
    out: Dict[str, float] = {}
    for d in plan.get("devices") or []:
        ip = d.get("ip")
        if not ip:
            continue
        try:
            out[str(ip)] = float(d.get("planned_hours") or 0)
        except (TypeError, ValueError):
            out[str(ip)] = 0.0
    return out


def _device_deviation_pct(
    ip: str,
    state: Dict[str, Any],
    planned_on: Optional[str],
    planned_hours: float,
    soc_gap_pct: Optional[float],
) -> Tuple[Optional[float], str]:
    """Abweichung: Zeitplan vs. Ist, sonst SoC-Gap als Kontext."""
    prev = {
        str(d.get("ip")): d
        for d in (state.get("device_schedule_snapshot") or [])
        if d.get("ip")
    }
    snap = prev.get(ip) or {}
    runtime = (state.get("device_runtime") or {}).get(ip) or {}

    planned_prev = _parse_iso(snap.get("planned_on"))
    actual = _parse_iso(runtime.get("resumed_at") or runtime.get("last_action_at"))
    if (
        planned_prev
        and actual
        and runtime.get("last_action") == "resume"
        and planned_hours > 0.05
    ):
        delta_h = abs((actual - planned_prev).total_seconds()) / 3600.0
        pct = round(min(100.0, (delta_h / max(planned_hours, 0.25)) * 100.0), 1)
        return pct, f"Startzeit Δ {delta_h:.2f} h / Fenster {planned_hours:.1f} h"

    if soc_gap_pct is not None:
        return abs(soc_gap_pct), "SoC-Lücke Plan vs. Prognose (Geräte-Kontext)"

    if planned_on and not actual:
        return None, "noch kein Ist-Start zum Vergleich"
    return None, "—"


def _allocate_late_discharge_slots(
    config: Dict[str, Any],
    late: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """
    Spätstart-Entladen nach Priorität: der Anker (höchste Prio) bekommt die
    gesamte Überschuss-Energie allein – so lange wie möglich. Parallelstart
    aller Geräte würde den Speicher zu schnell leeren.

    Zeiten kommen aus late_start_plan (bereits mit Anker-kW gerechnet).
    Rest-kWh nach dem Anker gehen nur dann an die nächste Prio, wenn der
    Anker die Energie bis zur Deadline physikalisch nicht schafft.
    """
    from control_algorithm.late_start import LATE_START_BUFFER_HOURS
    from control_algorithm.planner import sort_for_resume

    excess = float(late.get("excess_kwh") or 0)
    if excess < 0.4:
        return {}

    deadline = _parse_iso(late.get("deadline"))
    latest = _parse_iso(late.get("latest_start"))
    hours_plan = float(late.get("hours_needed") or 0)
    if latest is None:
        return {}
    if deadline is None and hours_plan > 0:
        deadline = latest + timedelta(hours=hours_plan + LATE_START_BUFFER_HOURS)

    ordered = sort_for_resume(enabled_devices(config))
    if not ordered:
        return {}

    slots: Dict[str, Dict[str, Any]] = {}
    remaining = excess
    cursor = latest

    # 1) Anker: kanonischer Spätstart aus dem Plan
    anchor = ordered[0]
    a_ip = str(anchor.get("ip") or "")
    a_pwr = float(anchor.get("power_kw") or 0)
    if a_ip and a_pwr > 0.05:
        hours = hours_plan if hours_plan > 0.05 else (remaining / a_pwr)
        off = latest + timedelta(hours=hours)
        if deadline is not None and off > deadline:
            off = deadline
            hours = max(0.25, (off - latest).total_seconds() / 3600.0)
        kwh = round(min(remaining, hours * a_pwr), 2)
        prio_lbl = PRIORITY_LABEL.get(
            normalize_device_priority(anchor.get("priority")), "hoch"
        )
        slots[a_ip] = {
            "planned_on": latest.isoformat(),
            "planned_off": off.isoformat(),
            "hours": round(hours, 2),
            "kwh": kwh,
            "role": "anchor",
            "reason": (
                f"Spätstart Anker Prio {prio_lbl}: {kwh:.1f} kWh / {hours:.1f} h allein "
                f"bis ~{_iso_display(off.isoformat())} – niedrigere Prio wartet."
            ),
        }
        remaining = max(0.0, remaining - kwh)
        cursor = off

    # 2) Nur Rest nach Anker (selten) – nächste Prio nacheinander, nicht parallel
    for d in ordered[1:]:
        if remaining < 0.4:
            break
        ip = str(d.get("ip") or "")
        pwr = float(d.get("power_kw") or 0)
        if not ip or pwr <= 0.05:
            continue
        hours = remaining / pwr
        if hours < 0.25:
            break
        start = cursor
        off = start + timedelta(hours=hours)
        if deadline is not None and off > deadline:
            off = deadline
            hours = max(0.0, (off - start).total_seconds() / 3600.0)
            if hours < 0.25:
                break
        kwh = round(hours * pwr, 2)
        slots[ip] = {
            "planned_on": start.isoformat(),
            "planned_off": off.isoformat(),
            "hours": round(hours, 2),
            "kwh": kwh,
            "role": "spill",
            "reason": f"Rest nach Anker: {kwh:.1f} kWh / {hours:.1f} h (sequentiell)",
        }
        remaining = max(0.0, remaining - kwh)
        cursor = off
    return slots


def _build_devices(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
    soc_gap_pct: Optional[float],
) -> List[Dict[str, Any]]:
    notes = device_label_map()
    miner_status: Dict[str, str] = dict(live.get("miner_status") or {})
    planned_by_ip = {
        a["ip"]: a for a in (state.get("planned_actions") or []) if a.get("ip")
    }
    hours_map = _prio_hours_by_ip(state)
    late = state.get("late_start_plan") or {}
    night = bool(state.get("night_lock_active"))
    safety = bool(state.get("pv_safety_active"))
    anom = bool(state.get("anomaly_active"))
    locked = night or safety or anom

    late_slots: Dict[str, Dict[str, Any]] = {}
    if (
        late.get("defer")
        and not late.get("urgent")
        and float(late.get("excess_kwh") or 0) >= 0.4
    ):
        late_slots = _allocate_late_discharge_slots(config, late)

    sched_by_ip: Dict[str, Dict[str, Any]] = {}
    for row in (state.get("device_schedule_24h") or {}).get("devices") or []:
        ip = str(row.get("ip") or "")
        if ip:
            sched_by_ip[ip] = row
    budget_updated = state.get("device_schedule_24h_updated_at")

    rows: List[Dict[str, Any]] = []
    for d in enabled_devices(config):
        ip = str(d.get("ip") or "")
        if not ip:
            continue
        running = str(miner_status.get(ip) or "").lower() == "running"
        act = planned_by_ip.get(ip)
        action = act.get("action") if act else None
        reason = str((act or {}).get("reason") or "")
        prio = normalize_device_priority(d.get("priority"))
        planned_h = float(hours_map.get(ip) or 0)

        planned_on: Optional[str] = None
        planned_off: Optional[str] = None
        wait_note = None
        slot_hours = planned_h

        if locked and (running or action == "pause" or night or safety):
            planned_off = now.isoformat()
            planned_on = None
            if night:
                reason = reason or str(state.get("night_lock_reason") or "Nacht-Sperre")
            elif safety:
                reason = reason or str(state.get("pv_safety_reason") or "PV-Sicherheit")
            elif anom:
                reason = reason or f"Anomalie +{state.get('anomaly_excess_pct')} %"
            wait_note = "gesperrt"
        elif action == "resume":
            planned_on = now.isoformat()
            if planned_h > 0.05:
                planned_off = (now + timedelta(hours=planned_h)).isoformat()
        elif action == "pause":
            planned_off = now.isoformat()
        elif ip in late_slots and not running:
            slot = late_slots[ip]
            planned_on = slot.get("planned_on")
            planned_off = slot.get("planned_off")
            slot_hours = float(slot.get("hours") or 0)
            reason = reason or str(slot.get("reason") or late.get("summary") or "Spätstart")
            wait_note = "spätstart" if slot.get("role") == "anchor" else "spätstart-rest"
        elif late_slots and not running and late.get("defer"):
            # Andere Geräte: bewusst kein paralleler Start
            wait_note = "wartet (höhere Prio bekommt Entlade-Energie)"
            reason = (
                "Kein paralleler Spätstart – Priorität: Anker möglichst lange allein, "
                "sonst wäre der Speicher zu schnell leer."
            )
        elif running and planned_h > 0.05:
            planned_on = (
                runtime_iso
                if (runtime_iso := (state.get("device_runtime") or {}).get(ip, {}).get("resumed_at"))
                else now.isoformat()
            )
            start_dt = _parse_iso(planned_on) or now
            planned_off = (start_dt + timedelta(hours=planned_h)).isoformat()
            reason = reason or "läuft – Budget-Fenster"
        else:
            # Füll-Rechnung: geplante EIN-Zeit für Flex gegen Max-SoC
            fill = state.get("fill_schedule") or {}
            flex = fill.get("flex") or {}
            if (
                not running
                and flex.get("ip") == ip
                and flex.get("planned_on")
            ):
                planned_on = str(flex.get("planned_on"))
                reason = reason or str(flex.get("reason") or "Füll-Rechnung bis Max-SoC")
                wait_note = "füll-plan"
                if planned_h > 0.05:
                    start_dt = _parse_iso(planned_on) or now
                    planned_off = (start_dt + timedelta(hours=planned_h)).isoformat()
            else:
                wait_note = "wartet auf Surplus/Export"
                reason = reason or "kein Schaltfenster diesen Tick"

        dev_pct, dev_note = _device_deviation_pct(
            ip, state, planned_on, slot_hours or float(late.get("hours_needed") or 1), soc_gap_pct
        )

        start_in = _relative_start_label(planned_on, now) if planned_on else None
        on_disp = start_in or _iso_display(planned_on)
        off_disp = _iso_display(planned_off)
        if planned_off and planned_on:
            off_dt = _parse_iso(planned_off)
            if off_dt is not None:
                off_disp = f"{off_dt.strftime('%H:%M')} ({_iso_display(planned_off)})"

        sched = sched_by_ip.get(ip) or {}
        budget_kwh_24h = sched.get("budget_kwh")
        budget_hours_24h = sched.get("planned_hours")

        rows.append(
            {
                "ip": ip,
                "name": notes.get(ip) or ip,
                "priority": prio,
                "priority_label": PRIORITY_LABEL.get(prio) or format_device_priority(prio),
                "power_kw": float(d.get("power_kw") or 0),
                "status": "Läuft" if running else "Aus",
                "running": running,
                "planned_action": (
                    "Einschalten"
                    if action == "resume"
                    else ("Ausschalten" if action == "pause" else "Unverändert")
                ),
                "planned_on": planned_on,
                "planned_off": planned_off,
                "planned_on_display": on_disp,
                "planned_off_display": off_disp,
                "start_in_display": start_in,
                "planned_hours": round(slot_hours, 2) if slot_hours else None,
                "budget_kwh_24h": budget_kwh_24h,
                "budget_hours_24h": budget_hours_24h,
                "budget_updated_at": budget_updated,
                "deviation_pct": dev_pct,
                "deviation_note": dev_note,
                "reason": reason[:200] if reason else None,
                "wait_note": wait_note,
            }
        )

    rows.sort(key=lambda r: (-float(r.get("priority") or 0), str(r.get("name") or "")))
    return rows


def _build_score(
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> Dict[str, Any]:
    proj = state.get("balance_projection") or state.get("horizon_projection") or {}
    target = proj.get("target_soc")
    projected = proj.get("projected_soc")
    soc_gap = None
    try:
        if target is not None and projected is not None:
            soc_gap = round(float(projected) - float(target), 1)
    except (TypeError, ValueError):
        soc_gap = None

    ov = state.get("pv_forecast_override") or {}
    pace = _pct(ov.get("pace_deviation_pct"))
    pace_abs = abs(pace) if pace is not None else None
    soc_abs = abs(soc_gap) if soc_gap is not None else None

    # Plan-Score 0–100: niedriger = größere Abweichung
    penalties = []
    if pace_abs is not None:
        penalties.append(min(40.0, pace_abs * 0.8))
    if soc_abs is not None:
        penalties.append(min(40.0, soc_abs * 0.5))
    if state.get("anomaly_active"):
        penalties.append(15.0)
    if state.get("pv_safety_active"):
        penalties.append(20.0)
    score = round(max(0.0, 100.0 - sum(penalties)), 1)

    return {
        "plan_score": score,
        "soc_gap_pct": soc_gap,
        "soc_gap_abs_pct": soc_abs,
        "pace_deviation_pct": pace,
        "pace_abs_pct": pace_abs,
        "target_soc": _pct(target),
        "projected_soc": _pct(projected),
        "current_soc": _pct(live.get("soc_percent")),
        "summary": (
            f"Plan-Score {score}/100 · SoC-Lücke "
            f"{soc_gap if soc_gap is not None else '—'} %-Pkt · Pace "
            f"{pace if pace is not None else '—'} %"
        ),
    }


def build_decision_board(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Gesamtes Entscheidungs-Cockpit für API/UI."""
    now = now or datetime.now()
    live = live or {}
    try:
        from control_algorithm.fill_schedule import compute_fill_schedule

        if not (state.get("fill_schedule") or {}).get("ok"):
            state["fill_schedule"] = compute_fill_schedule(config, state, live, now)
    except Exception:
        pass
    score = _build_score(state, live)
    layers = _build_layers(config, state, live)
    signals = _build_signals(config, state, live)
    devices = _build_devices(
        config, state, live, now, score.get("soc_gap_pct")
    )
    intervened = [L for L in layers if L.get("status") == "intervened"]
    stretch = state.get("anchor_stretch") or {}
    if not stretch.get("ok"):
        try:
            from control_algorithm.anchor_stretch import compute_anchor_stretch_plan

            stretch = compute_anchor_stretch_plan(config, state, live, now)
            state["anchor_stretch"] = stretch
        except Exception:
            stretch = {}
    shutoff = {
        "soc_now": stretch.get("soc_percent"),
        "soc_hydro_off_percent": stretch.get("soc_hydro_off_percent"),
        "soc_reserve_floor_percent": stretch.get("soc_reserve_floor_percent"),
        "soc_min_hard_percent": stretch.get("soc_min_hard_percent")
        or float(config.get("min_soc_percent") or 28),
        "soc_max_percent": float(config.get("max_soc_percent") or 97),
        "anchor_hours": stretch.get("anchor_hours"),
        "stretch_active": bool(stretch.get("active")),
        "flex_shed_mode": stretch.get("flex_shed_mode") or "aus",
        "reserve_kwh": stretch.get("reserve_kwh"),
        "pv_start_note": stretch.get("pv_start_note"),
        "formula": stretch.get("formula"),
        "note": (
            f"Hydro planmäßig aus ca. {stretch.get('soc_hydro_off_percent')} % "
            f"(Reserve bis {stretch.get('pv_start_note')}); "
            f"Flex gestaffelt (niedrigste Prio zuerst) bei Anker-Streckung; "
            f"hart aus unter {stretch.get('soc_min_hard_percent') or config.get('min_soc_percent')} %."
        )
        if stretch.get("soc_hydro_off_percent") is not None
        else None,
    }
    # Geplante AUS-SoC an Geräte hängen (Anzeige)
    anchor_ip = str(stretch.get("anchor_ip") or "")
    flex_ips = {str(x) for x in (stretch.get("flex_ips") or [])}
    for d in devices:
        ip = str(d.get("ip") or "")
        if ip == anchor_ip and stretch.get("soc_hydro_off_percent") is not None:
            d["planned_off_soc_percent"] = stretch.get("soc_hydro_off_percent")
            prev = d.get("planned_off_display")
            soc_txt = f"ca. {stretch.get('soc_hydro_off_percent')} % SoC"
            d["planned_off_display"] = (
                f"{soc_txt} · {prev}" if prev and "SoC" not in str(prev) else soc_txt
            )
        elif ip in flex_ips and stretch.get("active"):
            d["planned_off_soc_percent"] = stretch.get("soc_percent")
            if not d.get("planned_off_display") or "gestaffelt" not in str(
                d.get("planned_off_display") or ""
            ):
                d["planned_off_display"] = "gestaffelt (Streckung)"
    activation = _build_activation_timeline(devices, state, now)
    logic_gate: Dict[str, Any] = {}
    try:
        from control_algorithm.logic_gate import build_logic_gate

        logic_gate = build_logic_gate(
            config,
            state,
            live,
            devices=devices,
            signals=signals,
            score=score,
            now=now,
        )
    except Exception as e:
        logic_gate = {"error": str(e), "title": "Logikgattersteuerungsalgorithmus Autopilot"}
    return {
        "updated_at": now.isoformat(),
        "title": "Logikgattersteuerungsalgorithmus Autopilot",
        "score": score,
        "layers": layers,
        "intervened_count": len(intervened),
        "signals": signals,
        "devices": devices,
        "device_count": len(devices),
        "activation_timeline": activation,
        "fill_schedule": state.get("fill_schedule"),
        "anchor_stretch": stretch,
        "shutoff_soc": shutoff,
        "curve_plan": state.get("curve_plan"),
        "logic_gate": logic_gate,
    }
