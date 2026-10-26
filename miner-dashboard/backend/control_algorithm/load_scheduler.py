# -*- coding: utf-8 -*-
"""Profilbasierte Laststeuerung mit Prioritäten und Mindestlaufzeit."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.energy_balance import compute_horizon_projection, miner_surplus_kwh
from control_algorithm.config import device_label_map, normalize_device_priority
from control_algorithm.planner import (
    PRIORITY_LABEL,
    enabled_devices,
    find_priority_violations,
    miner_load_kw,
    next_soc_target,
    paused_devices_highest_tier,
    priority_rank,
    sort_for_pause,
    sort_for_resume,
)
from control_algorithm.weather_survival import live_grid_export_kw
from control_algorithm.steering_learning import (
    export_threshold_kw,
    get_steering_params,
    soc_preempt_level,
)
from control_algorithm.priority_runtime import (
    compute_priority_runtime_plan,
    is_energy_stress,
    may_resume_by_priority_plan,
    should_force_pause_flex,
)

BUDGET_PAUSE_MARGIN_KW = 1.0
BUDGET_RESUME_MARGIN_KW = 1.0
# Totband über Morgenziel beim Entladen (SoC-Hold)
DISCHARGE_HOLD_DEADBAND_PCT = 3.0
PROJECTION_SAFE_MARGIN_PCT = 2.0
ADJUST_PULSE_KWH = 8.0
# Prognose unter Morgen-Floor + Margin → niedrige Prio abwerfen trotz Export-Guard


def _session_last_device(
    state: Dict[str, Any],
    ip: str,
    sim_status: Dict[str, Any],
) -> bool:
    """Letztes laufende Gerät eines Überschuss-Zugs?

    Weiche Schichten (Morgen-Floor, Anker-Reserve, Budget) dürfen staffeln, aber
    nicht den ganzen Zug beenden – sonst fängt der Algo nach jeder Wolke neu an.
    Notbremsen laufen hier nicht durch, die setzen `force`/`emergency`.
    """
    try:
        from control_algorithm.surplus_session import is_running, session_devices

        if not is_running(state):
            return False
        running = [
            d
            for d in session_devices(state)
            if str(sim_status.get(d) or "").lower() == "running"
        ]
        return len(running) <= 1 and str(ip) in set(running)
    except Exception:
        return False
MORNING_FLOOR_MARGIN_PCT = 2.0
# Nur der nächste Morgen (nicht Tag+2/…) steuert Sofort-Shed – sonst Abschalten
# trotz starker PV morgen / Entladebedarf (Max-SoC-Platz).
MORNING_FLOOR_LOOKAHEAD_HOURS = 36.0


def _pv_absorption_mode(
    pv_kw: float,
    soc: float,
    max_soc: float,
    binding: str = "",
) -> bool:
    """Tages-PV-Absorption: Max-SoC-Bindung bei Sonne, nicht über Max / nicht nachts."""
    try:
        if float(pv_kw or 0) < 5.0:
            return False
        if float(soc or 0) >= float(max_soc or 95.0) - 0.5:
            return False
    except (TypeError, ValueError):
        return False
    return str(binding or "") == "max_soc"


def should_defer_night_flex_resume(
    state: Dict[str, Any],
    device: Dict[str, Any],
    priority_plan: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Flex/Spill nachts zurückstellen wenn 24h-Absorption tagsüber abdeckbar ist
    (Schaltbudget für PV-Stunden sparen).
    """
    now = now or datetime.now()
    h = now.hour
    if 6 <= h < 22:
        return False, ""
    ip = str(device.get("ip") or "")
    role = _device_plan_role(ip, priority_plan)
    if role == "anchor":
        return False, ""
    absorb = state.get("absorption_plan_24h") or {}
    if absorb.get("error"):
        return False, ""
    total = float(absorb.get("total_need_kwh") or 0)
    if total < 3.0:
        return False, ""
    hourly = absorb.get("hourly_need_kwh") or []
    day_need = 0.0
    for i in range(min(24, len(hourly))):
        hour_of_day = (h + i) % 24
        if 6 <= hour_of_day < 22:
            day_need += float(hourly[i] or 0)
    if day_need >= total * 0.55:
        return True, (
            f"Tag-first: ~{day_need:.0f} kWh Absorption tagsüber geplant – "
            f"{role} nachts zurückgestellt (Schaltbudget für PV-Stunden)."
        )
    return False, ""


def _discharge_plan(state: Dict[str, Any]) -> Dict[str, Any]:
    plan = state.get("discharge_plan")
    return dict(plan) if isinstance(plan, dict) else {}


def _hours_until_morning_target(
    config: Dict[str, Any], now: Optional[datetime] = None
) -> float:
    now = now or datetime.now()
    target = next_soc_target(config.get("soc_targets") or [], now)
    if not target:
        nxt = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return max(1.0, (nxt - now).total_seconds() / 3600.0)
    try:
        dt = datetime.fromisoformat(str(target["target_datetime"]))
        return max(1.0, (dt - now).total_seconds() / 3600.0)
    except Exception:
        return 8.0


def _safe_discharge_budget_kw(
    *,
    budget_kw: float,
    sustained_kw: Optional[float],
    overnight_cap_kw: Optional[float],
    projection: Dict[str, Any],
    checkpoint_soc: float,
    live_soc: Optional[float],
    hold_until_soc: float,
    storage_kwh: float,
    hours_to_target: float,
    pv_kw: float,
) -> Tuple[float, float, str]:
    """Sichere Miner-Last und zu verbrennende kWh bis Hold-Ziel."""
    projected = projection.get("projected_soc")
    try:
        projected_f = float(projected) if projected is not None else None
    except (TypeError, ValueError):
        projected_f = None

    burn_kwh = 0.0
    if live_soc is not None and storage_kwh > 0:
        burn_kwh = max(
            0.0, (float(live_soc) - float(hold_until_soc)) / 100.0 * storage_kwh
        )

    if projected_f is not None and projected_f < float(checkpoint_soc) + PROJECTION_SAFE_MARGIN_PCT:
        return 0.0, 0.0, (
            f"Prognose {projected_f:.1f}% < Ziel {checkpoint_soc:.0f}%+"
            f"{PROJECTION_SAFE_MARGIN_PCT:.0f} – Last zurücknehmen"
        )

    safe = float(budget_kw)
    caps: List[str] = []
    if sustained_kw is not None:
        boost_cap = max(float(sustained_kw), float(sustained_kw) * 1.35)
        if float(pv_kw) >= 40:
            boost_cap = max(
                boost_cap,
                min(float(budget_kw), float(sustained_kw) + float(pv_kw) * 0.15),
            )
        if safe > boost_cap:
            safe = boost_cap
            caps.append(f"sustained/boost≤{boost_cap:.1f} kW")
    if overnight_cap_kw is not None and safe > float(overnight_cap_kw):
        safe = float(overnight_cap_kw)
        caps.append(f"Haltbarkeit≤{overnight_cap_kw:.1f} kW")

    hours = max(1.0, float(hours_to_target))
    if burn_kwh > 0:
        energy_kw = burn_kwh / hours
        energy_cap = energy_kw * 1.25
        if safe > energy_cap:
            min_one = 5.5 if burn_kwh >= ADJUST_PULSE_KWH else energy_cap
            safe = min(safe, max(energy_cap, min(min_one, safe)))
            caps.append(f"Energie≤{energy_cap:.1f} kW ({burn_kwh:.0f} kWh / {hours:.0f} h)")
    else:
        safe = 0.0
        caps.append("kein SoC-Überschuss bis Hold-Ziel")

    reason = ", ".join(caps) if caps else "Budget unverändert"
    return round(max(0.0, safe), 3), round(burn_kwh, 2), reason


def _checkpoint_datetime(cp: Dict[str, Any], now: Optional[datetime] = None) -> Optional[datetime]:
    """Zielzeit eines Checkpoints, falls bekannt."""
    raw = cp.get("target_datetime") or cp.get("datetime")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if getattr(dt, "tzinfo", None) is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        except (TypeError, ValueError):
            pass
    try:
        mins = float(cp.get("minutes_until"))
        base = now or datetime.now()
        return base + timedelta(minutes=mins)
    except (TypeError, ValueError):
        return None


def morning_floor_threatened(
    projection: Dict[str, Any],
    *,
    min_soc: float,
    morning_floor: float,
    margin: float = MORNING_FLOOR_MARGIN_PCT,
    now: Optional[datetime] = None,
    lookahead_hours: float = MORNING_FLOOR_LOOKAHEAD_HOURS,
) -> Tuple[bool, Optional[float], str]:
    """
    True wenn der nächste Morgen-Checkpoint (innerhalb lookahead) unter Floor+margin
    liegt bzw. nicht erreichbar ist – dann Export-Guard weichen / Energiesparen.

    Spätere Tage allein (z. B. 06:00 in 36+ h) lösen keinen Sofort-Shed aus:
    sonst wird bei gutem morgigen PV fälschlich abgeschaltet, obwohl der Speicher
    für Max-SoC-Platz entladen werden muss.
    """
    floor = max(float(min_soc), float(morning_floor))
    cps = list(projection.get("checkpoints") or [])
    now = now or datetime.now()
    horizon_h = max(6.0, float(lookahead_hours))

    def _fail(proj: Any, need: float, reachable: Any) -> bool:
        try:
            p = float(proj)
        except (TypeError, ValueError):
            return reachable is False
        if reachable is False:
            return True
        return p < need + float(margin)

    def _is_morning(cp: Dict[str, Any]) -> bool:
        time_s = str(cp.get("time") or "")
        try:
            return 5 <= int(time_s.split(":")[0]) <= 9
        except (TypeError, ValueError, IndexError):
            return False

    # Nächster Morgen-Checkpoint: mit Zeitstempel im Lookahead, sonst erster in Liste
    dated: List[Tuple[datetime, Dict[str, Any]]] = []
    undated: List[Dict[str, Any]] = []
    for cp in cps:
        if not _is_morning(cp):
            continue
        dt = _checkpoint_datetime(cp, now)
        if dt is None:
            undated.append(cp)
            continue
        hours = (dt - now).total_seconds() / 3600.0
        if hours < -1.0:
            continue
        if hours > horizon_h:
            continue
        dated.append((dt, cp))

    next_morning: Optional[Dict[str, Any]] = None
    if dated:
        dated.sort(key=lambda x: x[0])
        next_morning = dated[0][1]
    elif undated:
        next_morning = undated[0]

    if next_morning is not None:
        tgt = float(
            next_morning.get("soc_percent")
            or next_morning.get("target_soc")
            or floor
        )
        need = max(floor, tgt)
        time_s = str(next_morning.get("time") or "checkpoint")
        if _fail(next_morning.get("projected_soc"), need, next_morning.get("reachable", True)):
            try:
                p = float(next_morning.get("projected_soc"))
            except (TypeError, ValueError):
                p = need - margin - 1.0
            return True, p, time_s
        # Nächster Morgen ok → kein Sofort-Shed wegen späterer Tage
        return False, None, ""

    # Kein Morgen-Checkpoint im Lookahead: Horizon-/Binding-Fallback nur wenn
    # projected_soc klar unter Floor (kein Tag+2-Fehlschlag über all_checkpoints_ok).
    try:
        p = float(projection.get("projected_soc"))
        if p < floor + float(margin) and projection.get("all_checkpoints_ok") is False:
            # Nur wenn überhaupt kein Checkpoint die nächste Nacht abbildet
            return True, p, "binding"
    except (TypeError, ValueError):
        pass
    return False, None, ""


def sustainable_morning_cap_kw(
    config: Dict[str, Any],
    live: Dict[str, Any],
    projection: Dict[str, Any],
    priority_plan: Dict[str, Any],
    *,
    morning_floor: float,
) -> float:
    """Nachhaltige Last bis Morgen-Floor (niedrige Prio nur bei Bedarf).

    Wichtig: Nicht blind preferred_continuous_kw (Surplus-Plan) übernehmen –
    bei großem Surplus ist das oft die volle Flotte und blockiert Energiesparen.
    """
    nt = projection.get("near_term_plan") or {}
    if nt.get("sustained_budget_kw") is not None:
        try:
            return max(0.0, float(nt["sustained_budget_kw"]))
        except (TypeError, ValueError):
            pass

    soc = live.get("soc_percent")
    storage = float(config.get("storage_kwh") or 0)
    headroom_cap: Optional[float] = None
    if soc is not None and storage > 0:
        hours = 10.0
        # Stunden bis zum relevanten Morgen-Checkpoint (nicht pauschal 10 h)
        for cp in projection.get("checkpoints") or []:
            time_s = str(cp.get("time") or "")
            try:
                h = int(time_s.split(":")[0])
            except (TypeError, ValueError, IndexError):
                continue
            if not (5 <= h <= 9):
                continue
            minutes = cp.get("minutes_until")
            if minutes is not None:
                try:
                    hours = max(0.5, float(minutes) / 60.0)
                    break
                except (TypeError, ValueError):
                    pass
            td = cp.get("target_datetime") or cp.get("datetime")
            if td:
                try:
                    tgt = datetime.fromisoformat(str(td).replace("Z", ""))
                    hours = max(0.5, (tgt - datetime.now()).total_seconds() / 3600.0)
                    break
                except Exception:
                    pass
        # Fallback: binding_checkpoint / horizon target
        if hours <= 10.05:
            for key in ("binding_checkpoint",):
                bcp = projection.get(key) or {}
                td = bcp.get("target_datetime")
                if td:
                    try:
                        tgt = datetime.fromisoformat(str(td).replace("Z", ""))
                        delta_h = (tgt - datetime.now()).total_seconds() / 3600.0
                        if delta_h > hours:
                            hours = max(0.5, delta_h)
                    except Exception:
                        pass
            try:
                td = projection.get("target_datetime")
                if td:
                    tgt = datetime.fromisoformat(str(td).replace("Z", ""))
                    delta_h = (tgt - datetime.now()).total_seconds() / 3600.0
                    if delta_h > hours:
                        hours = max(0.5, delta_h)
            except Exception:
                pass
        try:
            risk = float(config.get("risk_factor_percent") or 85.0) / 100.0
        except (TypeError, ValueError):
            risk = 0.85
        risk = max(0.5, min(1.0, risk))
        headroom_kwh = max(0.0, (float(soc) - float(morning_floor)) / 100.0 * storage)
        try:
            house_kw = float(
                live.get("house_kw")
                or live.get("wohnhaus_kw")
                or 0.0
            )
        except (TypeError, ValueError):
            house_kw = 0.0
        headroom_cap = max(0.0, headroom_kwh * risk / hours - max(0.0, house_kw))
        headroom_cap = round(headroom_cap, 3)

    pref = float(priority_plan.get("preferred_continuous_kw") or 0)
    # Nur Anker-Leistung als Untergrenze / Fallback (nicht volle Surplus-Flotte)
    anchor_kw = 0.0
    for r in priority_plan.get("devices") or []:
        if str(r.get("role") or "") == "anchor":
            try:
                anchor_kw += float(r.get("power_kw") or 0)
            except (TypeError, ValueError):
                pass

    if headroom_cap is not None:
        # preferred nur als Deckel, nie als Aufbläher über Headroom
        if pref > 0:
            return max(anchor_kw, min(pref, headroom_cap))
        return max(anchor_kw, headroom_cap)
    if pref > 0 and anchor_kw > 0 and pref > anchor_kw * 3:
        # Surplus-Plan = volle Flotte, aber kein Headroom → nur Anker
        return round(anchor_kw, 3)
    if pref > 0:
        return pref
    return round(anchor_kw, 3)


def _device_plan_role(ip: str, priority_plan: Dict[str, Any]) -> str:
    row = (priority_plan.get("by_ip") or {}).get(str(ip) or "") or {}
    if not row:
        for d in priority_plan.get("devices") or []:
            if str(d.get("ip") or "") == str(ip):
                return str(d.get("role") or "flex")
        return "flex"
    return str(row.get("role") or "flex")


def _enforce_priority_before_switch(
    actions: List[Dict[str, Any]],
    devices: List[Dict[str, Any]],
    sim_status: Dict[str, str],
    running_load: float,
    miner_notes: Optional[Dict[str, str]] = None,
    live_status: Optional[Dict[str, str]] = None,
    state: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, str], float, List[str]]:
    """Vorm Schalten: kein Flex/Spill-Resume wenn höhere Prio noch aus ist.

    Ersetzt unsichere Resumes durch Resume der höchsten fehlenden Stufe.
    Wenn der Anker live noch aus ist, starten wir diesen Tick nur den Anker
    (Flex wartet) – sonst Flattern: Flex ein → Priorität aus → wieder ein.

    Morgen-Floor/Budget-Shed committed_off: höhere Stufe nicht force-resumen
    (sonst Floor-Shed ↔ Priority-ein alle ~3 min). Unsicheren Spill-Resume verwerfen.
    """
    miner_notes = miner_notes or {}
    live_status = live_status or {}
    state = state or {}
    config = config or {}
    live = live or {}
    messages: List[str] = []
    by_ip = {str(d.get("ip") or ""): d for d in devices if d.get("ip")}
    out: List[Dict[str, Any]] = []
    ordered = sort_for_resume(devices)
    anchor_ip = str((ordered[0].get("ip") if ordered else "") or "")
    anchor_live_on = (
        str(live_status.get(anchor_ip) or "").lower() == "running" if anchor_ip else True
    )

    def _name(ip: str) -> str:
        return miner_notes.get(ip) or (by_ip.get(ip) or {}).get("name") or ip

    def _energy_save_off(ip: str) -> bool:
        try:
            from control_algorithm.switch_commitment import (
                is_energy_save_off_commitment,
                runtime_entry,
            )

            return is_energy_save_off_commitment(runtime_entry(state, ip), state=state)
        except Exception:
            return False

    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        live_export = float(live_grid_export_kw(live))
    except Exception:
        live_export = 0.0
    if live_export <= 0:
        try:
            live_export = float(live.get("einspeisung_kw") or live.get("export_kw") or 0)
        except (TypeError, ValueError):
            live_export = 0.0
        if live_export < 0:
            live_export = abs(live_export)
    try:
        from control_algorithm.switch_commitment import normalize_export_override_kw

        export_override = normalize_export_override_kw(
            config.get("export_commitment_override_kw")
        )
    except Exception:
        export_override = 2.0
    allow_undo_floor = bool(
        state.get("min_soc_lock_active")
        or state.get("fill_urgency_active")
        or live_export >= export_override > 0
        or float(
            (state.get("horizon_projection") or state.get("balance_projection") or {}).get(
                "proactive_discharge_kw"
            )
            or 0
        )
        > 0.5
        or (not anchor_live_on and live_export >= export_override > 0)
    )
    if not allow_undo_floor:
        try:
            from control_algorithm.switch_commitment import state_has_fill_max_soc_urgency

            allow_undo_floor = state_has_fill_max_soc_urgency(state, config)
        except Exception:
            pass

    # Sim neu aus Live-Status – nicht aus bereits „vorgemerkt running“ der Planung,
    # sonst werden alle Resumes als „schon an“ verworfen und nichts geschaltet.
    sim_status = {
        str(d.get("ip")): str(live_status.get(str(d.get("ip"))) or "paused")
        for d in devices
        if d.get("ip")
    }
    running_load = miner_load_kw(devices, sim_status)

    # Pausen zuerst auf sim anwenden
    for a in actions:
        if a.get("action") != "pause":
            continue
        ip = str(a.get("ip") or "")
        if not ip:
            continue
        out.append(a)
        if str(sim_status.get(ip) or "").lower() == "running":
            sim_status[ip] = "paused"
            running_load -= float((by_ip.get(ip) or {}).get("power_kw") or 0)

    for a in actions:
        if a.get("action") != "resume":
            if a.get("action") != "pause":
                out.append(a)
            continue
        ip = str(a.get("ip") or "")
        d = by_ip.get(ip)
        if not d:
            continue
        if str(sim_status.get(ip) or "").lower() == "running":
            continue
        if any(x.get("ip") == ip and x.get("action") == "resume" for x in out):
            continue

        # Höhere Priorität noch aus? Dann zuerst die einschalten, nicht diese.
        higher_off = [
            x
            for x in ordered
            if priority_rank(x) < priority_rank(d)
            and str(sim_status.get(x.get("ip")) or "").lower() != "running"
        ]
        if higher_off:
            hi = higher_off[0]
            hip = str(hi.get("ip") or "")
            if not hip:
                continue
            # Floor/Budget-Shed: höhere Stufe nicht zurückholen – Spill-Resume verwerfen
            if _energy_save_off(hip) and not allow_undo_floor:
                messages.append(
                    f"Einschalten {_name(ip)} verworfen – höhere Prio {_name(hip)} "
                    f"ist Morgen-Floor/Budget-Aus (Anti-Flatter, keine Cascade)."
                )
                continue
            # Anker live aus: diesen Tick nur Anker, keine Umleitung Flex←Spill
            if not anchor_live_on and hip != anchor_ip:
                if _energy_save_off(anchor_ip) and not allow_undo_floor:
                    messages.append(
                        f"Einschalten {_name(ip)} verworfen – Anker {_name(anchor_ip)} "
                        f"ist Floor/Budget-Aus (Anti-Flatter)."
                    )
                    continue
                if anchor_ip and not any(
                    x.get("ip") == anchor_ip and x.get("action") == "resume" for x in out
                ):
                    out.append(
                        {
                            "ip": anchor_ip,
                            "action": "resume",
                            "reason": (
                                f"Vorm Schalten: zuerst {_name(anchor_ip)} "
                                f"(Anker live aus) – {_name(ip)} wartet"
                            ),
                            "force": True,
                        }
                    )
                    sim_status[anchor_ip] = "running"
                    running_load += float(
                        (by_ip.get(anchor_ip) or {}).get("power_kw") or 0
                    )
                messages.append(
                    f"Einschalten {_name(ip)} zurückgestellt – "
                    f"Anker {_name(anchor_ip)} muss zuerst laufen."
                )
                continue
            if any(x.get("ip") == hip and x.get("action") == "resume" for x in out):
                messages.append(
                    f"Einschalten {_name(ip)} übersprungen – "
                    f"zuerst {_name(hip)} (höhere Prio)."
                )
                continue
            out.append(
                {
                    "ip": hip,
                    "action": "resume",
                    "reason": (
                        f"Vorm Schalten geprüft: {_name(ip)} darf nicht starten, "
                        f"solange {_name(hip)} aus ist – höhere Priorität zuerst"
                    ),
                    "force": True,
                }
            )
            messages.append(
                f"Einschalten umgeleitet: zuerst {_name(hip)} (statt {_name(ip)})."
            )
            sim_status[hip] = "running"
            running_load += float(hi.get("power_kw") or 0)
            continue

        # Anker live aus: nur Anker diesen Tick – Flex/Spill warten
        if (
            not anchor_live_on
            and anchor_ip
            and ip != anchor_ip
        ):
            if _energy_save_off(anchor_ip) and not allow_undo_floor:
                messages.append(
                    f"Einschalten {_name(ip)} verworfen – Anker {_name(anchor_ip)} "
                    f"ist Floor/Budget-Aus (Anti-Flatter)."
                )
                continue
            if not any(x.get("ip") == anchor_ip and x.get("action") == "resume" for x in out):
                out.append(
                    {
                        "ip": anchor_ip,
                        "action": "resume",
                        "reason": (
                            f"Vorm Schalten: zuerst {_name(anchor_ip)} "
                            f"(Anker live aus) – {_name(ip)} wartet"
                        ),
                        "force": True,
                    }
                )
                sim_status[anchor_ip] = "running"
                running_load += float((by_ip.get(anchor_ip) or {}).get("power_kw") or 0)
            messages.append(
                f"Einschalten {_name(ip)} zurückgestellt – "
                f"Anker {_name(anchor_ip)} muss zuerst laufen."
            )
            continue

        # Eigene IP Floor/Budget-Off: Spill/Low-Fill nicht wieder rein
        if _energy_save_off(ip) and not allow_undo_floor:
            messages.append(
                f"Einschalten {_name(ip)} verworfen – Gerät ist Morgen-Floor/"
                f"Budget-Aus (Anti-Flatter)."
            )
            continue

        out.append(a)
        sim_status[ip] = "running"
        running_load += float(d.get("power_kw") or 0)
    return out, sim_status, running_load, messages


def is_min_runtime_protected(
    ip: str,
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    device: Optional[Dict[str, Any]] = None,
    devices: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """True wenn planmäßiges Pause wegen Mindestlaufzeit (inkl. Lern-Extra) blockiert ist."""
    try:
        from control_algorithm.switch_commitment import algo_runtime_locks_enabled

        if not algo_runtime_locks_enabled(config):
            return False
    except Exception:
        pass
    try:
        from control_algorithm.steering_learning import effective_min_runtime_hours

        min_runtime_h = effective_min_runtime_hours(config, state, live, device, devices)
    except Exception:
        min_runtime_h = normalize_min_runtime_hours(config.get("min_device_runtime_hours"))
    runtime: Dict[str, Any] = dict(state.get("device_runtime") or {})
    info = runtime.get(ip) or {}
    resumed_at = info.get("resumed_at")
    if not resumed_at:
        return False
    try:
        started = datetime.fromisoformat(str(resumed_at))
        elapsed_h = (datetime.now() - started).total_seconds() / 3600.0
        return elapsed_h < min_runtime_h
    except Exception:
        return False


def _append_budget_shed_log(
    state: Dict[str, Any],
    ip: str,
    name: str,
    reason: str,
    priority: str,
) -> None:
    log: List[Dict[str, Any]] = list(state.get("budget_shed_log") or [])
    log.append(
        {
            "time": datetime.now().strftime("%H:%M"),
            "iso": datetime.now().isoformat(),
            "ip": ip,
            "name": name,
            "priority": priority,
            "reason": reason,
        }
    )
    state["budget_shed_log"] = log[-50:]


def plan_runtime_remaining_h(
    ip: str,
    priority_plan: Optional[Dict[str, Any]],
) -> float:
    """Zugeteilte Restlaufzeit laut Prioritäts-/Surplus-Plan (Stunden)."""
    if not isinstance(priority_plan, dict):
        return 0.0
    row = (priority_plan.get("by_ip") or {}).get(str(ip) or "") or {}
    try:
        return max(0.0, float(row.get("planned_hours") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def normalize_plan_hold_hours(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 1.0)
    except (TypeError, ValueError):
        v = 1.0
    return max(0.0, min(48.0, v))


def should_hold_through_budget_dip(
    device: Dict[str, Any],
    priority_plan: Optional[Dict[str, Any]],
    config: Dict[str, Any],
    *,
    near_min_soc: bool = False,
) -> bool:
    """
    Kurzfristige Verbrauchsspitzen (noch keine Anomalie) sollen Geräte nicht
    vorzeitig aus dem Plan reißen: Solange der Laufzeitplan dem Gerät noch
    Stunden zuteilt, weiterlaufen lassen. Safety (nahe Min-SoC) hat Vorrang.
    """
    if near_min_soc:
        return False
    hold_h = normalize_plan_hold_hours(config.get("plan_budget_hold_hours"))
    if hold_h <= 0:
        return False
    ip = str(device.get("ip") or "")
    return plan_runtime_remaining_h(ip, priority_plan) >= hold_h


def pick_pause_candidate(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, str],
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    *,
    priority_plan: Optional[Dict[str, Any]] = None,
    energy_stress: bool = False,
    export_mode: bool = False,
    near_min_soc: bool = False,
    respect_plan_hold: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Niedrigste Priorität zuerst; überspringt Mindestlaufzeit-geschützte Geräte
    und gelernte Hold-Phase der höchsten Priorität (früher an / länger laufen).

    Bei Energie-Stress: Flex/Spill trotz Mindestlaufzeit abwerfen, damit der
    Anker (höchste Prio) länger laufen kann und Min-SoC gehalten wird.

    Plan-Hold: Geräte mit Restlaufzeit im Surplus-Plan werden bei normalen
    Budget-Dips nicht gewählt (Anti-Flapping) – außer Safety (nahe Min-SoC)
    oder respect_plan_hold=False (Morgen-Floor / Anker-Streckung).

    Soft-Aus/Tag: Empfehlung – bevorzugt Geräte unter der Empfehlungszahl,
    blockiert aber niemanden (Werk muss Energiesparen können).

    Gibt None zurück wenn kein planmäßiges Abschalten möglich.
    """
    try:
        from control_algorithm.steering_learning import should_hold_top_priority
    except Exception:
        should_hold_top_priority = None  # type: ignore
    try:
        from control_algorithm.switch_commitment import (
            normalize_max_off_per_day,
            off_count_today,
            runtime_entry,
        )
    except Exception:
        normalize_max_off_per_day = None  # type: ignore
        off_count_today = None  # type: ignore
        runtime_entry = None  # type: ignore

    plan = priority_plan if isinstance(priority_plan, dict) else {}
    running = [d for d in devices if miner_status.get(d.get("ip")) == "running"]
    viable: List[Dict[str, Any]] = []
    for d in sort_for_pause(running):
        ip = d.get("ip")
        if live and should_hold_top_priority and should_hold_top_priority(
            config, state, live, d, devices
        ):
            continue
        if respect_plan_hold and should_hold_through_budget_dip(
            d, plan, config, near_min_soc=near_min_soc
        ):
            continue
        force_flex = should_force_pause_flex(
            d, plan, energy_stress=energy_stress, export_mode=export_mode
        )
        if (
            not force_flex
            and is_min_runtime_protected(ip, state, config, live=live, device=d, devices=devices)
        ):
            continue
        viable.append(d)
    if not viable:
        return None

    # Empfehlung: unter Soft-Aus/Tag bevorzugen, sonst niedrigste Prio wie bisher
    max_off = 3
    if normalize_max_off_per_day is not None:
        max_off = normalize_max_off_per_day(config.get("max_device_off_per_day", 3))
    try:
        from control_algorithm.switch_commitment import (
            hard_daily_on_limit_enabled,
            normalize_max_on_per_day,
            on_count_today as _on_count_today,
        )

        max_on = normalize_max_on_per_day(config.get("max_device_on_per_day", 3))
        hard_on = hard_daily_on_limit_enabled(config)
    except Exception:
        max_on = 3
        hard_on = True
        _on_count_today = None  # type: ignore

    # Harte EIN-Grenze: Geräte ohne Rest-Einschaltbudget nicht abschalten
    # (könnten heute nicht mehr zurück), außer nur die sind viable.
    if hard_on and max_on > 0 and _on_count_today is not None and runtime_entry is not None:
        with_budget: List[Dict[str, Any]] = []
        for d in viable:
            ip = str(d.get("ip") or "")
            cnt = _on_count_today(runtime_entry(state, ip))
            if cnt < max_on:
                with_budget.append(d)
        if with_budget:
            viable = with_budget

    if max_off > 0 and off_count_today is not None and runtime_entry is not None:
        under: List[Dict[str, Any]] = []
        for d in viable:
            ip = str(d.get("ip") or "")
            cnt = off_count_today(runtime_entry(state, ip))
            if cnt < max_off:
                under.append(d)
        if under:
            return sort_for_pause(under)[0]
    return viable[0]


_pick_budget_pause_candidate = pick_pause_candidate


def normalize_min_runtime_hours(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 3.0)
    except (TypeError, ValueError):
        v = 3.0
    return max(0.5, min(24.0, v))


def is_min_soc_breached(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """True wenn Live-SoC unter dem Mindest-SoC liegt (mit Erholungs-Hysterese).

    Einmal aktiv: Sperre bleibt bis SoC ≥ Min + 1 %-Punkt, damit nicht bei
    27.9 ↔ 28.1 ständig freigegeben/gesperrt wird.
    """
    soc = live.get("soc_percent")
    if soc is None:
        return False
    min_soc = float(config.get("min_soc_percent") or 35.0)
    soc_f = float(soc)
    if soc_f < min_soc:
        return True
    if state and state.get("min_soc_lock_active") and soc_f < min_soc + 1.0:
        return True
    return False


def check_min_soc_emergency(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Sofort alle Geräte abschalten wenn SOC unter Mindest-SoC (mit Hysterese).

    Liefert auch dann eine Warnmeldung, wenn bereits alles aus ist – damit
    die Tick-Logik Lastplanung/Resume blockieren kann (nicht nur Pause senden).
    """
    from control_algorithm.battery_protection import min_soc_protection_actions

    return min_soc_protection_actions(config, live, state)


def night_lock_pause_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Alle laufenden Geräte pausieren bei aktiver Nacht-Sperre."""
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    if not state.get("night_lock_active"):
        return actions, messages
    from control_algorithm.energy_balance import night_lock_enabled

    if not night_lock_enabled(config):
        return actions, messages

    reason = state.get("night_lock_reason") or "Nacht-Sperre aktiv"
    # Harte Max-Grenze: über Max-SoC nicht abschalten, Last muss aufnehmen.
    try:
        from control_algorithm.battery_protection import normalize_max_soc

        soc_now = live.get("soc_percent")
        if soc_now is not None and float(soc_now) >= normalize_max_soc(config):
            messages.append(
                f"Nacht-Sperre ausgesetzt – SoC {float(soc_now):.0f}% ≥ Max "
                f"{normalize_max_soc(config):.0f}%."
            )
            return actions, messages
    except (TypeError, ValueError):
        pass
    # force nur bei echter Min-SoC-Gefahr – sonst Mindestlaufzeit einhalten
    use_force = bool(state.get("night_lock_force"))
    devices = enabled_devices(config)
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    for d in devices:
        ip = d["ip"]
        if miner_status.get(ip) != "running":
            continue
        if not use_force and is_min_runtime_protected(
            ip, state, config, live=live, device=d, devices=devices
        ):
            continue
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": f"Nacht-Sperre: {reason}",
                "force": use_force,
                "emergency": bool(use_force),
                "source": "night_lock",
            }
        )
    if actions:
        messages.append(
            f"Nacht-Sperre aktiv – {len(actions)} Gerät(e) werden abgeschaltet"
            + (" (force)" if use_force else " (nach Mindestlaufzeit)")
            + "."
        )
    elif state.get("night_lock_active"):
        messages.append(f"Nacht-Sperre aktiv: {reason}")
    return actions, messages


def normalize_min_off_hours(raw: Any) -> float:
    try:
        v = float(raw if raw is not None else 1.0)
    except (TypeError, ValueError):
        v = 1.0
    return max(0.0, min(12.0, v))


def _stack_power_until(devices: List[Dict[str, Any]], need_kw: float) -> float:
    """Summe Geräteleistung nach Resume-Priorität bis need_kw erreicht."""
    stacked = 0.0
    for d in sort_for_resume(devices):
        stacked += float(d.get("power_kw") or 0)
        if stacked + 0.05 >= need_kw:
            break
    return stacked


def _stack_power_within(devices: List[Dict[str, Any]], limit_kw: float) -> float:
    """
    Summe Geräteleistung nach Resume-Priorität, die limit_kw nicht überschreitet.
    0.0 wenn schon das kleinste Gerät nicht hineinpasst – dann müsste die
    Differenz aus dem Speicher kommen, was keine Einspeisungsvermeidung ist.
    """
    tol = max(0.3, limit_kw * 0.15)
    stacked = 0.0
    for d in sort_for_resume(devices):
        pwr = float(d.get("power_kw") or 0)
        if stacked + pwr > limit_kw + tol:
            break
        stacked += pwr
    return stacked


def weather_run_through_floor(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    devices: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Gutwetter-/Surplus-Boden: Miner durchlaufen, Flex früher bei hohem SoC,
    kein Abschalten nur wegen aspirationalem 0-Budget.
    """
    empty = {
        "active": False,
        "floor_kw": 0.0,
        "treat_as_export": False,
        "no_budget_pause": False,
        "high_soc": False,
    }
    soc = live.get("soc_percent")
    if soc is None or not devices:
        return empty
    min_soc = float(config.get("min_soc_percent") or 35.0)
    max_soc = float(config.get("max_soc_percent") or 95.0)
    try:
        from control_algorithm.storage_band_learning import (
            effective_planning_max_soc,
            effective_planning_min_soc,
        )

        plan_max = effective_planning_max_soc(config, state, live=live)
        plan_min = effective_planning_min_soc(config, state, live=live)
    except Exception:
        plan_max = max_soc
        plan_min = float(config.get("morning_soc_percent") or min_soc)
    soc_f = float(soc)
    if soc_f < plan_min - 2.0:
        return empty

    pv_kw = float(live.get("pv_kw") or 0)
    verbrauch_kw = float(live.get("verbrauch_kw") or 0)
    surplus = max(0.0, pv_kw - verbrauch_kw)
    export_kw = live_grid_export_kw(live)
    thr = export_threshold_kw(state)
    preempt = soc_preempt_level(config, state)
    high_soc = soc_f >= max(preempt, plan_max - 5.0)
    at_max = soc_f >= plan_max - 1.0

    try:
        from control_algorithm.reference_anchor import effective_perfect_day_kwh

        perfect = float(effective_perfect_day_kwh(state, config) or 0)
    except Exception:
        perfect = float(config.get("pv_kwp") or 0) * float(
            config.get("perfect_weather_sun_hours") or 6
        )
    try:
        from control_algorithm.pv_pattern_estimate import effective_today_pv_kwh

        today_pv = float(effective_today_pv_kwh(state) or 0)
    except Exception:
        today_pv = float(state.get("pv_yield_today_kwh") or 0)
    pv_kwp = float(config.get("pv_kwp") or 0)
    strong_day = today_pv >= max(40.0, perfect * 0.65) or (
        pv_kw >= max(4.0, pv_kwp * 0.3) and today_pv >= max(25.0, perfect * 0.4)
    )
    if not strong_day and not high_soc and export_kw < thr:
        return empty

    ordered = sort_for_resume(devices)
    first_pwr = float(ordered[0].get("power_kw") or 0) if ordered else 0.0
    floor = 0.0
    treat_export = False
    no_pause = False

    if strong_day and pv_kw >= 2.0 and soc_f >= min_soc + 8.0:
        # Anker durchlaufen (kein Ein/Aus wegen Budget 0)
        floor = max(floor, first_pwr)
        no_pause = True

    if high_soc and pv_kw >= 0.5:
        need = max(surplus + 0.8, export_kw + 0.5, pv_kw * (0.9 if at_max else 0.75))
        floor = max(floor, _stack_power_until(devices, need), first_pwr)
        treat_export = True
        no_pause = True

    if export_kw >= thr and soc_f >= min_soc + 5.0:
        # Nur Last zuschalten, die Überschuss/Einspeisung wirklich tragen –
        # sonst deckt der Speicher die Differenz (nachts: PV 0, Fremdeinspeisung
        # klein, Gerät groß). Passt kein Gerät, ist es kein Export-Fall.
        fits_kw = _stack_power_within(devices, max(surplus, export_kw))
        if fits_kw > 0:
            floor = max(floor, fits_kw)
            treat_export = True

    if floor <= 0:
        return empty
    return {
        "active": True,
        "floor_kw": round(floor, 3),
        "treat_as_export": treat_export,
        "no_budget_pause": no_pause,
        "high_soc": high_soc,
        "strong_day": strong_day,
    }


def off_stability_remaining_h(
    ip: str,
    state: Dict[str, Any],
    config: Dict[str, Any],
) -> float:
    """
    Verbleibende empfohlene Ruhezeit nach Pause (Stunden).
    Keine harte Sperre – nur Planungs-Hinweis gegen Schaltflackern.
    """
    min_off_h = normalize_min_off_hours(config.get("min_device_off_hours"))
    if min_off_h <= 0:
        return 0.0
    runtime: Dict[str, Any] = dict(state.get("device_runtime") or {})
    info = runtime.get(ip) or {}
    paused_at = info.get("paused_at")
    if not paused_at:
        return 0.0
    try:
        started = datetime.fromisoformat(str(paused_at))
        elapsed_h = (datetime.now() - started).total_seconds() / 3600.0
        return max(0.0, min_off_h - elapsed_h)
    except Exception:
        return 0.0


def is_min_off_protected(
    ip: str,
    state: Dict[str, Any],
    config: Dict[str, Any],
) -> bool:
    """
    Harte Resume-Sperre während der Aus-Ruhezeit (Anti-Flapping).
    Nach Budget-Pause: Gerät bleibt aus, bis min_device_off_hours um sind
    bzw. ein dringender Resume (Export/Safety) kommt.
    """
    return off_stability_remaining_h(ip, state, config) > 0.0


def should_defer_resume_for_stability(
    ip: str,
    state: Dict[str, Any],
    config: Dict[str, Any],
    *,
    urgent: bool,
) -> bool:
    """
    Bei Normalbetrieb Aus-Ruhezeit einhalten; bei Export/Entladen/Safety nie verzögern.
    """
    if urgent:
        return False
    return is_min_off_protected(ip, state, config)


def _critical_deviation(value: Any, threshold: Any) -> bool:
    """Kritisch ab dem Doppelten der eingestellten Schwelle, mindestens 40 Punkte."""
    try:
        measured = abs(float(value))
        limit = float(threshold if threshold is not None else 20.0)
    except (TypeError, ValueError):
        return False
    return measured >= max(limit * 2.0, 40.0)


def protection_shutdown_is_emergency(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
    kind: str,
) -> bool:
    """PV-Sicherheit und Anomalie schalten nur im echten Notfall alles ab.

    Im Fenster zwischen Min- und Max-SoC bleibt die Planung stehen, außer die
    gemessene Abweichung ist kritisch. Unter Min-SoC ist es ein Notfall.
    Ab der harten Max-Grenze entscheidet die harte SoC-Regel, nicht diese Schicht.
    """
    from control_algorithm.battery_protection import normalize_max_soc, normalize_min_soc

    try:
        soc = (live or {}).get("soc_percent")
        soc_f = float(soc) if soc is not None else None
    except (TypeError, ValueError):
        soc_f = None
    if soc_f is None:
        return True
    if soc_f >= normalize_max_soc(config):
        return False
    if soc_f < normalize_min_soc(config):
        return True
    if kind == "pv":
        return _critical_deviation(
            state.get("pv_safety_deviation_pct"),
            config.get("pv_safety_threshold_percent", 20.0),
        )
    return _critical_deviation(
        state.get("anomaly_excess_pct"),
        config.get("anomaly_threshold_percent", 20.0),
    )


def plan_load_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
    base_dir: str,
    pv_today_kwh: float,
    pv_tomorrow_kwh: float,
    yield_factor: float,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Plant pause/resume nach Energiebilanz, Priorität und Mindestlaufzeit.
    """
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []

    if not config.get("enabled"):
        messages.append("Steuerungsalgorithmus ist deaktiviert.")
        return actions, messages

    devices = enabled_devices(config)
    if not devices:
        messages.append("Keine Geräte für die Steuerung aktiviert.")
        return actions, messages

    miner_status: Dict[str, str] = live.get("miner_status") or {}
    soc = live.get("soc_percent")
    min_soc = float(config.get("min_soc_percent") or 35.0)
    min_runtime_h = normalize_min_runtime_hours(config.get("min_device_runtime_hours"))

    # Füll-Rechnung immer aktualisieren (Cockpit + Flex-Trigger)
    try:
        from control_algorithm.fill_schedule import (
            apply_max_soc_override_latch,
            compute_fill_schedule,
            fill_schedule_resume_actions,
            sync_fill_urgency_from_plan,
        )

        state["fill_schedule"] = compute_fill_schedule(config, state, live)
        _fill0 = dict(state.get("fill_schedule") or {})
        apply_max_soc_override_latch(
            state,
            config,
            soc=float(soc) if soc is not None else None,
            over_max=bool(_fill0.get("over_max")),
        )
        sync_fill_urgency_from_plan(state, config, _fill0)
        from control_algorithm.battery_protection import sync_max_soc_protection_state

        sync_max_soc_protection_state(
            state, config, live, over_max=bool(_fill0.get("over_max"))
        )
        if state.get("max_soc_override_just_cleared"):
            messages.append(
                "Stufe 1: Halt erreicht – alle aus, bis der Speicher wieder am Max-SoC ist."
            )
    except Exception as e:
        state["fill_schedule"] = {"ok": False, "reason": str(e)}
        fill_schedule_resume_actions = None  # type: ignore
        # Kein Sticky bei Fehler
        if not state.get("max_soc_override_active"):
            state["fill_urgency_active"] = False

    if state.get("load_over_budget_reason"):
        messages.append(str(state["load_over_budget_reason"]))

    # Harte SoC-Grenzen vor Nachtsperre, Budget und Halt-Band.
    # Unter Min: alles aus. Über Max: gestaffelt ein (höchste Prio, dann PV-Budget).
    try:
        soc_hard = float(soc) if soc is not None else None
    except (TypeError, ValueError):
        soc_hard = None
    if soc_hard is not None:
        from control_algorithm.battery_protection import (
            max_soc_protection_actions,
            min_soc_protection_actions,
            normalize_max_soc,
            normalize_min_soc,
        )

        min_hard = normalize_min_soc(config)
        max_hard = normalize_max_soc(config)
        if soc_hard < min_hard:
            state["min_soc_lock_active"] = True
            ha, hm = min_soc_protection_actions(config, live, state)
            actions.extend(ha)
            messages.extend(hm)
            messages.append(
                f"Harte Grenze Min-SoC {min_hard:.0f}% – Notabschaltung, "
                "keine andere Regel schaltet ein."
            )
            messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
            messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
            return actions, messages
        if soc_hard >= max_hard:
            state["max_soc_protection_active"] = True
            state["max_soc_override_active"] = True
            ma, mm = max_soc_protection_actions(config, live, state)
            actions.extend(ma)
            messages.extend(mm)
            messages.append(
                f"Harte Grenze Max-SoC {max_hard:.0f}% – gestaffelt Noteinschalten, "
                "Nachtsperre und übrige Planung haben keinen Vorrang."
            )
            messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
            messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
            return actions, messages

    if state.get("min_soc_lock_active"):
        messages.append(
            state.get("min_soc_lock_reason")
            or "Batterie-Schutz Min-SoC – Algorithmus gesperrt, kein Resume."
        )
        messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
        messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
        return actions, messages

    # Nachtsperre nur unterhalb der harten Max-Grenze. Darüber ist oben schon zurückgekehrt.
    # Ausgeschalteter Dashboard-Schalter: keine Zwangspause, übrige Planung läuft.
    from control_algorithm.energy_balance import night_lock_enabled

    if state.get("night_lock_active") and night_lock_enabled(config):
        nl_actions, nl_msgs = night_lock_pause_actions(config, live, state)
        actions.extend(nl_actions)
        messages.extend(nl_msgs)
        messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
        messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
        return actions, messages

    if state.get("max_soc_protection_active"):
        from control_algorithm.battery_protection import max_soc_protection_actions

        ma, mm = max_soc_protection_actions(config, live, state)
        actions.extend(ma)
        messages.extend(mm)
        messages.append(
            "Batterie-Schutz Max-SoC – Algorithmus nur Einschalten, "
            "Abschaltung am Halt-SoC (Stufe 1)."
        )
        messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
        messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
        return actions, messages

    if state.get("max_soc_hold_released"):
        from control_algorithm.battery_protection import (
            max_soc_hold_pause_actions,
            normalize_max_soc,
        )

        max_now = normalize_max_soc(config)
        soc_now = live.get("soc_percent")
        still_below_max = soc_now is None or float(soc_now) < max_now - 0.05
        if still_below_max:
            ha, hm = max_soc_hold_pause_actions(config, live, state)
            actions.extend(ha)
            messages.extend(hm)
            messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
            messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
            return actions, messages

    if state.get("pv_safety_active") and protection_shutdown_is_emergency(
        config, live, state, "pv"
    ):
        from control_algorithm.pv_safety_watchdog import pv_safety_pause_actions

        sa, sm = pv_safety_pause_actions(config, live, state)
        actions.extend(sa)
        messages.extend(sm)
        messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
        messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
        return actions, messages
    if state.get("pv_safety_active"):
        messages.append(
            "PV-Sicherheit vermerkt, SoC zwischen Min und Max und Abweichung "
            "nicht kritisch – keine Notabschaltung, Planung läuft weiter."
        )

    if state.get("anomaly_active") and protection_shutdown_is_emergency(
        config, live, state, "anomaly"
    ):
        from control_algorithm.anomaly_detection import anomaly_pause_actions

        # Bremse, kein Gegenplan: der Zug pausiert und läuft nach der Anomalie weiter
        try:
            from control_algorithm.surplus_session import suspend_session

            suspend_session(state, "anomaly")
        except Exception:
            pass
        aa, am = anomaly_pause_actions(config, live, state)
        actions.extend(aa)
        messages.extend(am)
        messages.append(
            f"Anomalie aktiv ({state.get('anomaly_excess_pct', '?')} % über Schnitt) – "
            f"Sicherheits-Fast-Path: keine Budget-Lastplanung / kein Resume."
        )
        messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
        messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
        return actions, messages
    if state.get("anomaly_active"):
        messages.append(
            f"Anomalie vermerkt ({state.get('anomaly_excess_pct', '?')} % über Schnitt), "
            "SoC zwischen Min und Max – keine Notabschaltung, Planung läuft weiter."
        )

    if state.get("anomaly_low_active"):
        from control_algorithm.anomaly_detection import anomaly_low_resume_actions

        # Über Max / Kapazität: kein Early-Return – sonst bleibt niedrige Prio aus
        # obwohl Speicher über Max und Füllplan einschalten will.
        fill_early = dict(state.get("fill_schedule") or {})
        over_max_hard = bool(
            fill_early.get("over_max")
            or fill_early.get("capacity_limited")
            or state.get("max_soc_override_active")
        )
        if over_max_hard:
            messages.append(
                f"Unterverbrauch-Anomalie (−{state.get('anomaly_deficit_pct', '?')} % "
                f"unter Schnitt) – Max-SoC-Override hat Vorrang (kein Fast-Path-Abbruch)."
            )
            # weiter normale Lastplanung / Füll-Resume
        elif state.get("morning_floor_threatened") or (
            (state.get("anchor_stretch") or {}).get("active")
        ):
            messages.append(
                f"Unterverbrauch-Anomalie (−{state.get('anomaly_deficit_pct', '?')} % "
                f"unter Schnitt) – Morgen-Floor/Streckung hat Vorrang "
                f"(kein Fast-Path-Resume, aber Abschalten weiter erlaubt)."
            )
            # WICHTIG: nicht returnen – sonst bleibt Hydro/Anker ewig an,
            # weil Morgen-Floor-Shed nie geplant wird.
        else:
            la, lm = anomaly_low_resume_actions(config, live, state)
            actions.extend(la)
            messages.extend(lm)
            messages.append(
                f"Unterverbrauch-Anomalie (−{state.get('anomaly_deficit_pct', '?')} % unter Schnitt) – "
                f"Sicherheits-Fast-Path: keine Budget-Lastplanung."
            )
            messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
            messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
            return actions, messages

    from control_algorithm.anomaly_detection import is_anomaly_resume_blocked

    resume_blocked = is_anomaly_resume_blocked(state, config)

    projection = (
        state.get("horizon_projection")
        or state.get("balance_projection")
        or compute_horizon_projection(config, state, live, base_dir)
    )
    target = next_soc_target(config.get("soc_targets") or [])
    surplus_kwh = miner_surplus_kwh(config, state, live, base_dir)

    miner_notes = device_label_map()
    sim_status = dict(miner_status)
    running_load = miner_load_kw(devices, sim_status)

    # --- Prioritäts-Invariante: keine niedrigere Stufe läuft, solange höhere Stufe aus ---
    # force=True: Mindestlaufzeit gilt hier nicht – Verletzung muss sofort enden.
    while True:
        violations = find_priority_violations(devices, sim_status)
        if not violations:
            break
        candidate = violations[0]
        ip = candidate["ip"]
        pwr = float(candidate.get("power_kw") or 0)
        prio = PRIORITY_LABEL.get(
            normalize_device_priority(candidate.get("priority")), "Mittel"
        )
        protected = is_min_runtime_protected(
            ip, state, config, live=live, device=candidate, devices=devices
        )
        # Zuerst höhere Stufe holen – Low nicht force-killen, sonst Flattern.
        missing = paused_devices_highest_tier(devices, sim_status)
        if missing:
            hi = sort_for_resume(missing)[0]
            hip = str(hi.get("ip") or "")
            if hip and not any(
                a.get("ip") == hip and a.get("action") == "resume" for a in actions
            ):
                actions.append(
                    {
                        "ip": hip,
                        "action": "resume",
                        "reason": (
                            "Prioritätsreihenfolge: höhere Stufe zuerst "
                            f"({PRIORITY_LABEL.get(normalize_device_priority(hi.get('priority')), 'hoch')}) "
                            "– Flex/Spill darf nicht allein laufen"
                        ),
                        "force": True,
                    }
                )
                messages.append(
                    f"Steuerung plant Gerät {hip} einzuschalten "
                    f"(Prioritätskorrektur – fehlende höhere Stufe)."
                )
                sim_status[hip] = "running"
                running_load += float(hi.get("power_kw") or 0)
            break
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": (
                    f"Prioritätsreihenfolge: {prio} darf nicht laufen, "
                    f"solange höherwertige Geräte noch aus sind"
                ),
                "force": True,
            }
        )
        messages.append(
            f"Steuerung plant Gerät {ip} auszuschalten "
            f"(Prioritätsverletzung, {prio}"
            f"{', trotz Mindestlaufzeit' if protected else ''})."
        )
        _append_budget_shed_log(
            state,
            ip,
            miner_notes.get(ip, ip),
            "Prioritätsverletzung",
            prio,
        )
        sim_status[ip] = "paused"
        running_load -= pwr
        break

    if target and soc is not None:
        target_soc = float(target["soc_percent"])
        current_soc = float(soc)
        messages.append(
            f"SOC-Ziel {target_soc:.0f} % (7-Tage-Horizont) – "
            f"Prognose bindend: {projection.get('projected_soc', '?')} %, "
            f"Miner-Budget: {surplus_kwh:.1f} kWh."
        )

        # SOC-Ziel um 07:00 ist ein Minimum zur Zielzeit – kein Tages-Maximum.
        # Hoher SOC am Abend ist erwünscht, solange die Prognose das Ziel erreicht.

    hours_left = float(projection.get("hours_left") or 72.0)
    try:
        if not projection.get("hours_left"):
            target_dt = datetime.fromisoformat(str((projection or {}).get("horizon_end") or ""))
            hours_left = max(0.1, (target_dt - datetime.now()).total_seconds() / 3600.0)
    except Exception:
        hours_left = 72.0
    budget_kw = float(projection.get("miner_budget_kw") or (surplus_kwh / hours_left if hours_left > 0 else 0))
    pv_kw = float(live.get("pv_kw") or 0)
    night_mode = pv_kw < 0.5
    # Morgen-Floor für Entladen/Nacht-Gate – NICHT projection.target_soc
    # (das kann bei Binding max_soc die Obergrenze 85 % sein und Resumes blockieren).
    try:
        morning_floor = float(
            config.get("morning_soc_percent")
            if config.get("morning_soc_percent") is not None
            else (config.get("min_soc_percent") or 40.0)
        )
    except (TypeError, ValueError):
        morning_floor = float(config.get("min_soc_percent") or 40.0)
    morning_floor = max(float(min_soc), morning_floor)
    checkpoint_soc = morning_floor
    max_soc = float(config.get("max_soc_percent") or 95.0)
    total_kw = sum(float(d.get("power_kw") or 0) for d in devices)
    tomorrow_pv = float(
        state.get("pv_yield_tomorrow_kwh")
        or projection.get("pv_yield_tomorrow_kwh")
        or 0
    )
    # Füll-Plan: Budget anheben wenn Flex gegen Max-SoC jetzt dran ist
    fill = state.get("fill_schedule") or {}
    flex = fill.get("flex") or {}
    stack = fill.get("flex_stack") or []
    curve = state.get("curve_plan") or {}
    # Zu steile Ladekurve / Füll-Plan: Anker (Hydro) zuerst – sonst blockiert
    # die Prioritätsregel Flex (Luft), und Budget 0.5 kW startet niemanden.
    if (
        fill.get("ok")
        and not state.get("min_soc_lock_active")
        and soc is not None
        and float(soc) > min_soc + 1
    ):
        steep = (
            bool(flex.get("should_start_now"))
            or str(curve.get("steer") or "") == "mehr_last_frueher"
            or float(fill.get("excess_charge_kw") or 0) >= 1.0
        )
        if steep and devices:
            anchor = sort_for_resume(devices)[0]
            anchor_ip = str(anchor.get("ip") or "")
            anchor_kw = float(anchor.get("power_kw") or 0)
            if (
                anchor_ip
                and anchor_kw > 0
                and str(sim_status.get(anchor_ip) or "").lower() != "running"
            ):
                budget_kw = max(budget_kw, running_load + anchor_kw)
                messages.append(
                    f"Ladekurve glätten: Anker {anchor_ip} zuerst "
                    f"(Ist-Ladung {fill.get('charge_kw')} kW ≫ Soll "
                    f"{fill.get('desired_charge_kw')} kW) – Budget ≥ {budget_kw:.1f} kW."
                )
    if fill.get("ok") and not state.get("min_soc_lock_active"):
        need_kw = 0.0
        if stack:
            need_kw = sum(
                float(x.get("power_kw") or 0)
                for x in stack
                if str(sim_status.get(x.get("ip")) or "").lower() != "running"
            )
        elif flex.get("should_start_now"):
            flex_ip = str(flex.get("ip") or "")
            flex_kw = float(flex.get("power_kw") or 0)
            if flex_ip and str(sim_status.get(flex_ip) or "").lower() != "running" and flex_kw > 0:
                need_kw = flex_kw
        if need_kw > 0:
            budget_kw = max(budget_kw, running_load + need_kw)
            messages.append(
                f"Füll-Rechnung: Flex bald/jetzt nötig (Max ~{fill.get('eta_max_display')}, "
                f"Rest {fill.get('room_to_max_kwh')} kWh @ {fill.get('charge_kw')} kW) – "
                f"Budget ≥ {budget_kw:.1f} kW."
            )
    if fill.get("capacity_limited"):
        messages.append(
            "Füll-Rechnung: Kapazitätsgrenze – alle steuerbaren Lasten laufen, "
            f"Speicher trotzdem ~{fill.get('soc_percent')} % / Ladung {fill.get('charge_kw')} kW."
        )
    # Max-SoC-Dringlichkeit einmal früh berechnen (Pause-Skip + Resume-Override)
    fill_urgent = False
    try:
        from control_algorithm.fill_schedule import sync_fill_urgency_from_plan

        fill_urgent = bool(sync_fill_urgency_from_plan(state, config, fill))
    except Exception:
        fill_urgent = bool(
            flex.get("should_start_now")
            or fill.get("flex_stack")
            or fill.get("over_max")
            or state.get("max_soc_override_active")
        )
        state["fill_urgency_active"] = fill_urgent
    if fill_urgent and state.get("max_soc_override_active"):
        messages.append(
            "Max-SoC-Override aktiv: Speicher ≥ Max – Last einschalten; "
            "am Halt-SoC schaltet Stufe 1 alles aus."
        )
    # Anker-Streckung: Flex aus, Hydro möglichst lange (Reserve bis PV-Start)
    try:
        from control_algorithm.anchor_stretch import (
            apply_anchor_stretch_to_budget,
            compute_anchor_stretch_plan,
            stretch_exhausted_pause_anchor,
            stretch_pause_actions,
        )

        stretch = compute_anchor_stretch_plan(config, state, live)
        state["anchor_stretch"] = stretch
    except Exception as e:
        stretch = {"ok": False, "active": False, "reason": str(e)}
        state["anchor_stretch"] = stretch
        stretch_pause_actions = None  # type: ignore
        stretch_exhausted_pause_anchor = None  # type: ignore
        apply_anchor_stretch_to_budget = None  # type: ignore

    proactive_kw = float(projection.get("proactive_discharge_kw") or 0)
    binding = str(projection.get("binding_constraint") or "")
    live_export_kw = live_grid_export_kw(live)
    learn_thr = export_threshold_kw(state)
    learn_params = get_steering_params(state)
    soc_ok_for_export = soc is not None and float(soc) >= min_soc + 5.0
    run_through = weather_run_through_floor(config, state, live, devices)
    # Streck-Modus: Gutwetter darf Flex nicht „einfrieren“ – Anker soll allein laufen
    if stretch.get("active") and run_through.get("no_budget_pause"):
        run_through = dict(run_through)
        run_through["no_budget_pause"] = False
        # Floor nur Anker – nicht Hydro+Luft
        run_through["floor_kw"] = float(stretch.get("anchor_kw") or run_through.get("floor_kw") or 0)
    # Entladen wenn Speicher zu voll / starke PV morgen (Richtung Morgen-Checkpoint ~40 %),
    # damit tagsüber Platz bleibt und Einspeisung vermieden wird. Keine pauschale Nachtsperre –
    # die Energiebilanz (Budget/Checkpoints) entscheidet, wie viel Last geht.
    try:
        from control_algorithm.energy_balance import _plant_perfect_day_kwh

        _perfect = _plant_perfect_day_kwh(config, state)
    except Exception:
        _perfect = 96.0
    discharge_mode = soc is not None and (
        float(soc) >= max_soc - 0.05
        or proactive_kw > 0.5
        or (
            binding in ("max_soc", "proactive_discharge", "near_term_discharge")
            and tomorrow_pv >= max(40.0, _perfect * 0.55)
            and float(soc) >= checkpoint_soc + 5
        )
        or (run_through.get("high_soc") and pv_kw >= 1.0)
    )
    # Live-Einspeisung vermeiden – auch nachts bei Hydro (PV=0) und SOC sicher
    export_mode = (
        (binding == "export_avoidance" and (pv_kw >= 0.5 or live_export_kw >= learn_thr))
        or (live_export_kw >= learn_thr and soc_ok_for_export)
        or bool(run_through.get("treat_as_export"))
    )
    lehr_peak_spill_boost = 0.0
    try:
        from control_algorithm.peak_balance_learning import (
            peak_spill_boost,
            should_peak_spill_resume,
        )

        lehr_peak_spill_boost = peak_spill_boost(state)
        if not export_mode and should_peak_spill_resume(
            config, state, live, export_mode=export_mode
        ):
            export_mode = True
            messages.append(
                "Lehrneuron: Spitzen-Export-Risiko – Spill-Absorption aktiv."
            )
    except Exception:
        pass

    # Anker-Streckung erst NACH Export-Erkennung: bei Live-Export nicht auf Anker kappen
    if stretch.get("active") and apply_anchor_stretch_to_budget and not export_mode:
        prev_budget = budget_kw
        budget_kw = apply_anchor_stretch_to_budget(budget_kw, stretch)
        messages.append(
            f"Anker-Streckung: Budget {prev_budget:.1f} → {budget_kw:.1f} kW "
            f"(nur Hydro), ~{stretch.get('anchor_hours')} h möglich, "
            f"Reserve {stretch.get('reserve_kwh')} kWh bis PV "
            f"({stretch.get('pv_start_note')})."
        )
    elif stretch.get("active") and export_mode:
        messages.append(
            "Anker-Streckung ausgesetzt – Live-Einspeisung hat Vorrang "
            f"({live_export_kw:.2f} kW)."
        )

    if run_through.get("active") and float(run_through.get("floor_kw") or 0) > budget_kw:
        budget_kw = float(run_through["floor_kw"])
        messages.append(
            f"Wetter-Durchlauf: Mindest-Budget {budget_kw:.1f} kW "
            f"(gutes Wetter / Surplus, kein Flattern, Flex bei hohem SoC)."
        )

    # Spätestmöglicher Start: Entladen aus Speicher nur wenn/so spät nötig
    late_plan: Dict[str, Any] = {}
    try:
        from control_algorithm.late_start import compute_late_start_plan

        over_max = soc is not None and float(soc) > max_soc + 0.3
        urgent_export = bool(
            live_export_kw >= learn_thr or over_max or (export_mode and pv_kw >= 1.0)
        )
        late_plan = compute_late_start_plan(
            config,
            state,
            live,
            projection=projection,
            urgent_export=urgent_export,
        )
        state["late_start_plan"] = late_plan
        if late_plan.get("summary"):
            messages.append(str(late_plan["summary"]))
    except Exception as e:
        late_plan = {"defer": False, "error": str(e)}
        state["late_start_plan"] = late_plan

    defer_start = bool(late_plan.get("defer")) and not bool(late_plan.get("urgent"))

    # --- Laufreserve g: einzige Schranke für Speicherbetrieb nach PV-Ende ---
    # Trägt der Speicher die Geräte nicht bis zum nächsten PV-Break-even, gehen
    # sie aus. Der Headroom-Drain wandert damit in den Morgen, wo der
    # Nachtverbrauch bereits bekannt ist statt prognostiziert.
    # gate_active setzt voraus, dass der Speicher die Quelle ist (PV+Hydro unter
    # Grundlast). Einspeisung ist dann Messartefakt oder kurze Hydro-Spitze und
    # darf die Schranke nicht aushebeln – nur echter Überlauf über dem harten Max
    # geht vor, dort ist Verheizen billiger als Abregeln.
    drain_plan = state.get("morning_drain_plan") or {}
    run_reserve_blocked: set = set()
    if drain_plan.get("gate_active") and not (
        soc is not None and float(soc) > max_soc + 0.3
    ):
        allowed_ips = set(drain_plan.get("allowed_ips") or [])
        for d in devices:
            ip = str(d.get("ip") or "")
            if ip in allowed_ips:
                continue
            run_reserve_blocked.add(ip)
            if sim_status.get(ip) != "running":
                continue
            actions.append(
                {
                    "ip": ip,
                    "action": "pause",
                    "reason": (
                        f"Laufreserve {float(drain_plan.get('g_kwh') or 0):.1f} kWh – "
                        f"Speicher trägt nicht bis zum PV-Break-even "
                        f"{str(drain_plan.get('break_even_at') or '')[11:16]}; "
                        f"Drain wandert in den Morgen"
                    ),
                    "force": False,
                    "run_reserve": True,
                }
            )
            sim_status[ip] = "paused"
            running_load = max(0.0, running_load - float(d.get("power_kw") or 0))
        if run_reserve_blocked and drain_plan.get("summary"):
            messages.append(str(drain_plan["summary"]))

    # Hat die Laufreserve Geräte freigegeben, hat sie den Zeitpunkt bereits
    # aus der Energiebilanz bestimmt – der Spätstart darf ihn nicht nochmal
    # nach hinten schieben, sonst blockieren sich zwei Regler gegenseitig.
    if defer_start and drain_plan.get("gate_active") and drain_plan.get("allowed_ips"):
        defer_start = False
        messages.append(
            "Spätstart übersteuert – Laufreserve trägt bis zum PV-Break-even "
            f"{str(drain_plan.get('break_even_at') or '')[11:16]}."
        )

    morning_threat, morning_proj, morning_why = morning_floor_threatened(
        projection,
        min_soc=min_soc,
        morning_floor=checkpoint_soc,
        now=datetime.now(),
    )
    state["morning_floor_threatened"] = morning_threat
    # Tagsüber bei starker PV: Morgen-Floor nicht Last kappen – sonst Export/Max-SoC
    # obwohl die Overnight-Prognose nur bei Dauer-Volllast knappt.
    daytime_pv_hold = bool(
        pv_kw >= 12.0
        and soc is not None
        and float(soc) >= float(checkpoint_soc) + 8.0
    )
    state["daytime_pv_hold"] = daytime_pv_hold
    morning_shed = bool(morning_threat) and (not fill_urgent) and (not daytime_pv_hold)
    if morning_threat:
        proj_txt = f"{morning_proj:.0f}" if morning_proj is not None else "?"
        messages.append(
            f"Morgen-Floor gefährdet: Prognose ~{proj_txt} % "
            f"({morning_why or 'checkpoint'}) vs. Ziel ≥{checkpoint_soc:.0f} % – "
            f"niedrige Priorität nur bei Bedarf, hohe möglichst durchlaufen."
        )
        if daytime_pv_hold:
            messages.append(
                f"Morgen-Floor-Shed ausgesetzt – Tages-PV {pv_kw:.1f} kW, "
                f"SOC {float(soc):.0f} % (Absorption vor Overnight-Sparen)."
            )
    elif proactive_kw > 0.5:
        messages.append(
            f"Nächster Morgen-Floor ok – Entladen/Export aktiv "
            f"(proaktiv {proactive_kw:.1f} kW, PV morgen ~{tomorrow_pv:.0f} kWh), "
            f"kein Sofort-Shed wegen späterer Tage."
        )

    conserve_for_high_priority_lehr = bool(morning_threat)
    try:
        from control_algorithm.peak_balance_learning import should_conserve_for_anchor

        if should_conserve_for_anchor(config, state, live, export_mode=export_mode):
            conserve_for_high_priority_lehr = True
    except Exception:
        pass

    try:
        from control_algorithm.decision_arbitrator import reserve_priority_active

        reserve_priority = reserve_priority_active(
            binding=binding,
            budget_kw=budget_kw,
            morning_threat=bool(morning_threat),
        )
    except Exception:
        reserve_priority = bool(morning_threat)
    state["reserve_priority_active"] = reserve_priority

    # Vorladung/Reserve: Entladen nur bei echter Einspeisung, nicht volle Flotte halten
    if str(binding) in ("pre_storm_charge", "survival_mode") and live_export_kw < learn_thr:
        discharge_mode = False
        if binding != "export_avoidance":
            export_mode = False
    if reserve_priority and live_export_kw < learn_thr and run_through.get("no_budget_pause"):
        run_through = dict(run_through)
        run_through["no_budget_pause"] = False
        messages.append(
            "Schiedsrichter: Reserve/Vorladung – kein Gutwetter-Durchlauf, "
            "gestaffelt sparen (nicht alles aus, mathematisches Cap)."
        )

    if export_mode:
        # Budget so hoch, dass Surplus/Export weg ist – Anker + Flex (Luftgekühlt)
        try:
            first_pwr = float(sort_for_resume(devices)[0].get("power_kw") or 0)
        except Exception:
            first_pwr = 0.0
        surplus_now = max(0.0, pv_kw - float(live.get("verbrauch_kw") or 0))
        high_soc = soc is not None and float(soc) >= max(
            soc_preempt_level(config, state), max_soc - 5.0
        )
        # Zusätzlich zur laufenden Last: Export + Puffer müssen weggesaugt werden
        # (früher nur export_kw → Budget blieb bei 1× Anker trotz 4 kW Einspeisung).
        stack_need = max(
            running_load + live_export_kw + 0.5,
            live_export_kw + first_pwr + 0.5,
            surplus_now + (0.8 if high_soc else 0.0),
            first_pwr if live_export_kw >= learn_thr or high_soc else 0.0,
            pv_kw * 0.85 if binding == "export_avoidance" or high_soc else 0.0,
        )
        guard_floor = (
            float(state.get("export_guard_floor_kw") or 0)
            if state.get("export_guard_active")
            else 0.0
        )
        # Bei Morgen-Risiko: sticky Floor nicht die volle Flotte halten;
        # Horizon-Budget (oft 31.6 kW) darf Cap nicht aufblasen.
        # Ausnahme Max-SoC/Kapazität (fill_urgent): nicht kappen – sonst Abschalten
        # trotz Überproduktion / Speicher über Max (Export wird nur schlimmer).
        if morning_shed:
            stack_need = max(
                live_export_kw + first_pwr + 0.5 if live_export_kw >= learn_thr else 0.0,
                first_pwr if live_export_kw >= learn_thr else 0.0,
            )
            guard_floor = 0.0
            stacked = _stack_power_until(devices, stack_need) if stack_need > 0 else 0.0
            effective_budget = max(
                stacked,
                live_export_kw + 0.5 if live_export_kw >= learn_thr else 0.0,
            )
            messages.append(
                f"Export-Vermeidung (Morgen-Floor): Einspeisung {live_export_kw:.2f} kW, "
                f"SOC {float(soc):.0f} % – Budget nur Absorb {effective_budget:.1f} kW "
                f"(keine volle Flotte)."
            )
        else:
            stacked = _stack_power_until(devices, stack_need) if stack_need > 0 else 0.0
            effective_budget = max(
                budget_kw,
                stacked,
                float(run_through.get("floor_kw") or 0),
                guard_floor,
                running_load if (morning_threat and not morning_shed) else 0.0,
                total_kw * 0.5
                if (
                    binding == "export_avoidance"
                    and live_export_kw >= 2.0
                )
                else 0.0,
            )
            if (morning_threat and not morning_shed):
                messages.append(
                    f"Export-Vermeidung: Morgen-Floor erkannt, aber Max-SoC/Kapazität – "
                    f"keine Absorb-Kürzung (Budget {effective_budget:.1f} kW, "
                    f"Einspeisung {live_export_kw:.2f} kW, SOC {float(soc):.0f} %)."
                )
            else:
                messages.append(
                    f"Export-Vermeidung: Einspeisung {live_export_kw:.2f} kW, "
                    f"SOC {float(soc):.0f} % (Min {min_soc:.0f} %), PV {pv_kw:.1f} kW – "
                    f"Miner-Budget {effective_budget:.1f} kW (Priorität: keine Einspeisung, "
                    f"Lern-Schwelle {learn_thr:.2f} kW / Preempt {learn_params['soc_preempt_margin']:.1f} %)."
                )
        budget_kw = effective_budget
        # Sticky Floor aktualisieren (Nacht: Akteur-2 darf nicht sofort gegensteuern)
        # Ausnahme Morgen-Floor: Floor senken / nicht hochlatched halten.
        # Bei fill_urgent (Über-Max): Floor hoch halten, nicht auf Absorb drosseln.
        try:
            from control_algorithm.decision_arbitrator import (
                compute_export_absorb_floor,
                lower_export_guard_for_morning,
                maybe_clear_export_guard,
                update_export_guard,
            )

            if morning_shed and live_export_kw < learn_thr:
                maybe_clear_export_guard(
                    state,
                    soc=float(soc) if soc is not None else None,
                    checkpoint_soc=checkpoint_soc,
                    force=True,
                )
            elif morning_shed:
                lower_export_guard_for_morning(
                    state,
                    floor_kw=min(effective_budget, total_kw)
                    if total_kw > 0
                    else effective_budget,
                    reason=(
                        "Morgen-Floor: Export nur mit nötiger Last – "
                        "niedrige Prio abwerfen"
                    ),
                )
            else:
                absorb_only = compute_export_absorb_floor(
                    devices,
                    live_export_kw=live_export_kw,
                    learn_thr=learn_thr,
                    pv_surplus_kw=max(
                        0.0, pv_kw - float(live.get("verbrauch_kw") or 0)
                    ),
                    peak_spill_boost=lehr_peak_spill_boost,
                )
                guard_floor_kw = (
                    max(absorb_only, effective_budget)
                    if live_export_kw >= learn_thr
                    else min(effective_budget, total_kw)
                    if total_kw > 0
                    else effective_budget
                )
                update_export_guard(
                    state,
                    active=True,
                    floor_kw=guard_floor_kw,
                    reason=(
                        "Max-SoC/Kapazität – Export absorbieren, kein Morgen-Floor-Cut"
                        if (morning_threat and not morning_shed)
                        else (
                            f"Einspeisung absorbieren (Floor {guard_floor_kw:.1f} kW)"
                            if live_export_kw >= learn_thr
                            else "Einspeisung vermeiden – Vorrang vor Reserve-Budget"
                        )
                    ),
                    soc=float(soc) if soc is not None else None,
                    checkpoint_soc=checkpoint_soc,
                    allow_lower=True,
                )
        except Exception:
            pass

    # Laufzeitplan: Surplus gierig an höchste Prio (Anker), dann Flex, Spill zuletzt
    priority_plan = compute_priority_runtime_plan(
        devices,
        surplus_kwh,
        hours_left,
        min_soc_percent=min_soc,
        export_mode=export_mode,
        discharge_mode=discharge_mode,
    )
    try:
        from control_algorithm.soc_priority_neuron import (
            merge_neuron_into_priority_plan,
            neuron_applies,
            soc_priority_info_messages,
        )

        neuron = state.get("soc_priority_neuron") or {}
        if neuron.get("ok"):
            if neuron_applies(config, state):
                priority_plan = merge_neuron_into_priority_plan(priority_plan, neuron)
                for line in soc_priority_info_messages(state):
                    messages.append(line)
            elif neuron.get("shadow"):
                rollout = neuron.get("rollout") or {}
                days_left = int(rollout.get("days_remaining") or 0)
                messages.append(
                    f"SoC-Prio-Neuron (Shadow, noch {days_left} Tage bis Live): "
                    f"{neuron.get('recommended_continuous_kw')} kW · "
                    f"{len(neuron.get('recommended_on_ips') or [])} Geräte · "
                    f"bindend {neuron.get('binding_checkpoint') or '—'}"
                )
    except Exception:
        pass
    state["priority_runtime_plan"] = {
        "summary": priority_plan.get("summary"),
        "preferred_continuous_kw": priority_plan.get("preferred_continuous_kw"),
        "devices": priority_plan.get("devices"),
        "hours_left": priority_plan.get("hours_left"),
        "surplus_kwh": priority_plan.get("surplus_kwh"),
    }
    if priority_plan.get("summary"):
        messages.append(f"Laufzeitplan: {priority_plan['summary']}.")

    # Schiedsrichter: mathematisches Cap zwischen Export-Absorb und Reserve
    balanced_cap = budget_kw
    export_absorb_floor = 0.0
    cap_mode = "budget"
    try:
        from control_algorithm.decision_arbitrator import (
            compute_balanced_load_cap,
            compute_export_absorb_floor,
            reconcile_export_guard,
        )

        pv_surplus_now = max(0.0, pv_kw - float(live.get("verbrauch_kw") or 0))
        export_absorb_floor = compute_export_absorb_floor(
            devices,
            live_export_kw=live_export_kw,
            learn_thr=learn_thr,
            pv_surplus_kw=pv_surplus_now,
            peak_spill_boost=lehr_peak_spill_boost,
        )
        sustainable_for_cap = None
        if reserve_priority or budget_kw < 0.05:
            if str(binding) not in ("pre_storm_charge", "survival_mode"):
                sustainable_for_cap = sustainable_morning_cap_kw(
                    config,
                    live,
                    projection,
                    priority_plan,
                    morning_floor=checkpoint_soc,
                )
            state["balanced_sustainable_cap_kw"] = sustainable_for_cap

        balanced_cap, export_floor_calc, cap_mode = compute_balanced_load_cap(
            budget_kw=budget_kw,
            preferred_kw=float(priority_plan.get("preferred_continuous_kw") or 0),
            priority_plan=priority_plan,
            export_absorb_floor=export_absorb_floor,
            reserve_priority=reserve_priority,
            live_export_kw=live_export_kw,
            learn_thr=learn_thr,
            sustainable_cap=sustainable_for_cap,
            binding="morning_floor" if morning_threat else binding,
        )
        state["balanced_load_cap_kw"] = balanced_cap
        state["balanced_cap_mode"] = cap_mode
        state["export_absorb_floor_kw"] = export_absorb_floor

        messages.extend(
            reconcile_export_guard(
                state,
                binding=binding,
                live_export_kw=live_export_kw,
                learn_thr=learn_thr,
                soc=float(soc) if soc is not None else None,
                checkpoint_soc=checkpoint_soc,
                balanced_cap=balanced_cap,
                export_absorb_floor=export_absorb_floor,
                reserve_priority=reserve_priority,
            )
        )

        if reserve_priority and live_export_kw < learn_thr:
            # Über Max / Override: Budget nicht auf Anker-Cap kürzen –
            # sonst bleibt niedrige Prio aus obwohl Speicher über Max.
            if fill_urgent or bool(fill.get("over_max")) or bool(
                state.get("max_soc_override_active")
            ):
                messages.append(
                    f"Schiedsrichter ({cap_mode}): Cap {balanced_cap:.1f} kW "
                    f"ignoriert – Max-SoC-Override / Über-Max (Budget {budget_kw:.1f} kW)."
                )
            else:
                prev_b = budget_kw
                budget_kw = balanced_cap
                if prev_b > balanced_cap + 0.05 or str(binding) in (
                    "pre_storm_charge",
                    "survival_mode",
                ):
                    messages.append(
                        f"Schiedsrichter ({cap_mode}): Budget {prev_b:.1f} → "
                        f"{budget_kw:.1f} kW – teilweise Last, gestaffelt sparen."
                    )
    except Exception as e:
        state["balanced_load_cap_kw"] = budget_kw
        messages.append(f"Schiedsrichter-Cap nicht berechnet: {e}")

    # Nachhaltiges Morgen-Budget: volle Flotte nur wenn Prognose den Floor hält.
    # Bei Max-SoC/Kapazitätsgrenze (fill_urgent) nicht kappen.
    if morning_shed:
        sustainable = sustainable_morning_cap_kw(
            config,
            live,
            projection,
            priority_plan,
            morning_floor=checkpoint_soc,
        )
        absorb = 0.0
        if live_export_kw >= learn_thr and soc_ok_for_export:
            absorb = _stack_power_until(devices, live_export_kw + 0.5)
        cap = max(absorb, sustainable)
        state["morning_sustainable_cap_kw"] = round(cap, 3)
        if cap >= 0 and budget_kw > cap + 0.5:
            messages.append(
                f"Morgen-Cap: Budget {budget_kw:.1f} → {cap:.1f} kW "
                f"(nachhaltig {sustainable:.1f} kW"
                + (f", Export-Absorb {absorb:.1f} kW" if absorb > 0 else "")
                + ")."
            )
            budget_kw = cap
            try:
                from control_algorithm.decision_arbitrator import (
                    lower_export_guard_for_morning,
                    maybe_clear_export_guard,
                )

                if live_export_kw < learn_thr:
                    maybe_clear_export_guard(
                        state,
                        soc=float(soc) if soc is not None else None,
                        checkpoint_soc=checkpoint_soc,
                        force=True,
                    )
                else:
                    lower_export_guard_for_morning(
                        state,
                        floor_kw=cap,
                        reason="Morgen-Floor Cap – gestaffelt niedrige Prio aus",
                    )
            except Exception:
                pass
    elif (morning_threat and not morning_shed):
        state["morning_sustainable_cap_kw"] = None
        messages.append(
            "Morgen-Cap ausgesetzt – Max-SoC/Kapazität (Speicher über Max / "
            "volle Produktion, kein Lastabbau)."
        )
    elif reserve_priority and live_export_kw < learn_thr and balanced_cap + 0.05 < running_load:
        state["morning_sustainable_cap_kw"] = round(balanced_cap, 3)
        if budget_kw > balanced_cap + 0.5:
            messages.append(
                f"Reserve-Cap: Last {running_load:.1f} kW → Ziel {balanced_cap:.1f} kW "
                f"({cap_mode}, gestaffelt niedrige Prio aus)."
            )
            budget_kw = balanced_cap
    else:
        state["morning_sustainable_cap_kw"] = None

    energy_stress = bool(morning_threat) or is_energy_stress(
        soc=float(soc) if soc is not None else None,
        min_soc=min_soc,
        running_load_kw=running_load,
        budget_kw=budget_kw,
    )
    near_min_soc = bool(
        soc is not None and float(soc) <= float(min_soc) + 8.0
    )

    if export_mode:
        # Sofort priorisierte Geräte einschalten (nicht durch 7-Tage-Surplus-Check blockieren)
        if (
            not resume_blocked
            and soc_ok_for_export
            and (
                live_export_kw >= learn_thr
                or binding == "export_avoidance"
                or run_through.get("treat_as_export")
            )
        ):
            high_soc = bool(run_through.get("high_soc")) or (
                soc is not None
                and float(soc) >= max(soc_preempt_level(config, state), max_soc - 5.0)
            )
            surplus_now = max(0.0, pv_kw - float(live.get("verbrauch_kw") or 0))
            try:
                first_pwr = float(sort_for_resume(devices)[0].get("power_kw") or 0)
            except Exception:
                first_pwr = 0.0
            tier_paused = paused_devices_highest_tier(devices, sim_status)
            for d in sort_for_resume(tier_paused):
                ip = d["ip"]
                pwr = float(d.get("power_kw") or 0)
                if pwr <= 0:
                    continue
                if ip in run_reserve_blocked:
                    continue
                # Kein großes Gerät für eine kleine Einspeisung – die Differenz
                # käme aus dem Speicher und wäre keine Einspeisungsvermeidung.
                # Bei hohem SoC mit PV ist Überfahren gewollt (Abregeln droht).
                headroom_kw = max(live_export_kw, surplus_now)
                if not (high_soc and pv_kw >= 0.5) and pwr > headroom_kw * 1.15 + 0.3:
                    messages.append(
                        f"Export: {ip} bleibt aus – {pwr:.1f} kW Gerät gegen "
                        f"{headroom_kw:.2f} kW Einspeisung, Rest käme aus dem Speicher."
                    )
                    continue
                if morning_shed:
                    role = _device_plan_role(ip, priority_plan)
                    # Kurzer Morgen-Export darf Spill/niedrige Flex nicht einschalten –
                    # sonst fressen sie 3h Mindestlauf die Reserve für hohe Prio.
                    if role == "spill":
                        messages.append(
                            f"Export: {ip} bleibt aus – Morgen-Floor, Spill "
                            f"nur wenn hohe Prioritäten gesichert sind."
                        )
                        continue
                    if role == "flex":
                        row = (priority_plan.get("by_ip") or {}).get(str(ip)) or {}
                        if not row.get("prefer_on"):
                            messages.append(
                                f"Export: {ip} bleibt aus – Morgen-Floor, "
                                f"niedrige Flex-Prio spart für hohe."
                            )
                            continue
                        if live_export_kw < learn_thr:
                            continue
                        if running_load >= live_export_kw + 0.5:
                            continue
                if running_load + pwr > budget_kw + BUDGET_RESUME_MARGIN_KW:
                    continue
                ok, why = may_resume_by_priority_plan(
                    d,
                    priority_plan,
                    export_mode=True,
                    discharge_mode=discharge_mode,
                    higher_tier_running=True,
                    conserve_for_high_priority=conserve_for_high_priority_lehr,
                )
                if not ok:
                    messages.append(f"Export: {ip} noch nicht – {why}.")
                    continue
                prio = PRIORITY_LABEL.get(
                    normalize_device_priority(d.get("priority")), "Mittel"
                )
                actions.append(
                    {
                        "ip": ip,
                        "action": "resume",
                        "reason": (
                            f"Netzeinspeisung {live_export_kw:.2f} kW vermeiden – "
                            f"Gerät ein (Priorität {prio}, {why}), "
                            f"SOC {float(soc):.0f} % ≥ Min+5"
                        ),
                        "force": False,
                        "urgent": True,
                    }
                )
                messages.append(
                    f"Steuerung plant Gerät {ip} einzuschalten "
                    f"(Einspeisung vermeiden, Priorität {prio})."
                )
                running_load += pwr
                sim_status[ip] = "running"
                # Bei hohem SoC / Surplus weiter stapeln (Luftgekühlt), nicht nach
                # winzigem Export (0.2 kW) schon stoppen – sonst bleibt Einspeisung.
                if high_soc or surplus_now >= first_pwr * 0.5:
                    if running_load >= budget_kw - BUDGET_RESUME_MARGIN_KW:
                        break
                    if running_load >= max(surplus_now, live_export_kw) + 0.5 and not high_soc:
                        break
                    continue
                if live_export_kw > 0 and running_load >= live_export_kw + 0.3:
                    break
                if running_load >= budget_kw - BUDGET_RESUME_MARGIN_KW:
                    break
    elif discharge_mode:
        nt = projection.get("near_term_plan") or {}
        morning_target = checkpoint_soc
        drain_tgt = drain_plan.get("target_soc_percent")
        if drain_plan.get("ok") and drain_tgt is not None:
            # Laufreserve ersetzt den 33-%-Drain: kein Nachlaufen auf Morgen-Floor.
            morning_target = float(drain_tgt)
        if binding == "near_term_discharge" and nt.get("sustained_budget_kw") is not None:
            until = str(nt.get("near_term_until") or "")[:16]
            messages.append(
                f"Zwei-Phasen-Plan: bis {until} Budget {budget_kw:.1f} kW "
                f"(Export vermeiden / Headroom vor Sonne), danach nachhaltig "
                f"~{nt.get('sustained_budget_kw')} kW – Ziel morgens ca. {morning_target:.0f} % "
                f"(PV morgen ca. {tomorrow_pv:.0f} kWh)."
            )
        else:
            messages.append(
                f"Speicher {float(soc):.0f} % – Entladen Richtung ~{morning_target:.0f} % "
                f"bis zur PV (Budget {budget_kw:.1f} kW, PV morgen ca. {tomorrow_pv:.0f} kWh)."
            )
        if defer_start:
            messages.append(
                "Proaktives Entladen zurückgestellt (Spätstart) – "
                f"frühestens ~{str(late_plan.get('latest_start') or '')[11:16]}."
            )
        elif not resume_blocked:
            tier_paused = paused_devices_highest_tier(devices, sim_status)
            # Höchste Stufe muss passen – sonst startet nie Hydro, nur später Flex
            if tier_paused:
                first = sort_for_resume(tier_paused)[0]
                need = running_load + float(first.get("power_kw") or 0)
                if need > budget_kw + BUDGET_RESUME_MARGIN_KW:
                    budget_kw = max(budget_kw, need)
                    messages.append(
                        f"Spätstart/Entladen: Budget ≥ {budget_kw:.1f} kW "
                        f"für höchste Priorität ({first.get('ip')})."
                    )
            for d in sort_for_resume(tier_paused):
                ip = d["ip"]
                pwr = float(d.get("power_kw") or 0)
                if running_load + pwr > budget_kw + BUDGET_RESUME_MARGIN_KW:
                    continue
                if str(ip) in run_reserve_blocked:
                    continue
                ok, why = may_resume_by_priority_plan(
                    d,
                    priority_plan,
                    export_mode=False,
                    discharge_mode=True,
                    higher_tier_running=True,
                    conserve_for_high_priority=conserve_for_high_priority_lehr,
                )
                if not ok:
                    if morning_threat:
                        messages.append(f"Spätstart/Entladen: {ip} – {why}.")
                    continue
                defer_n, defer_why = should_defer_night_flex_resume(
                    state, d, priority_plan
                )
                if defer_n:
                    messages.append(f"Spätstart/Entladen: {ip} – {defer_why}")
                    continue
                prio = PRIORITY_LABEL.get(
                    normalize_device_priority(d.get("priority")), "Mittel"
                )
                ls = late_plan.get("latest_start")
                tgt_soc = late_plan.get("target_soc_percent")
                if tgt_soc is None:
                    tgt_soc = morning_target
                actions.append(
                    {
                        "ip": ip,
                        "action": "resume",
                        "reason": (
                            f"Spätstart-Fenster: Speicher {float(soc):.0f} % → "
                            f"{float(tgt_soc):.0f} % "
                            f"bis PV (morgen {tomorrow_pv:.0f} kWh), "
                            f"Priorität {prio} ({why})"
                            + (f", Planstart {str(ls)[11:16]}" if ls else "")
                        ),
                        "force": False,
                        "urgent": True,
                    }
                )
                messages.append(
                    f"Steuerung plant Gerät {ip} einzuschalten "
                    f"(Spätstart-Entladen, Priorität {prio})."
                )
                running_load += pwr
                sim_status[ip] = "running"
                if running_load >= budget_kw - BUDGET_RESUME_MARGIN_KW:
                    break

    # --- Budget-Laststeuerung: zuerst Last abbauen, dann ggf. einschalten ---
    pause_threshold_kw = budget_kw + BUDGET_PAUSE_MARGIN_KW
    resume_threshold_kw = budget_kw - BUDGET_RESUME_MARGIN_KW
    # Zusätzlich: Last über dem sinnvollen Dauer-Soll (Anker zuerst) abbauen
    preferred_kw = float(priority_plan.get("preferred_continuous_kw") or 0)
    if stretch.get("active"):
        preferred_kw = float(stretch.get("anchor_kw") or preferred_kw or 0)
    if (
        (not export_mode or morning_threat)
        and preferred_kw > 0
        and running_load > preferred_kw + BUDGET_PAUSE_MARGIN_KW
    ):
        # Nur wenn Horizon-Budget nicht großzügiger ist
        pause_threshold_kw = min(
            pause_threshold_kw,
            preferred_kw + BUDGET_PAUSE_MARGIN_KW,
        )

    # Phase A: Max-SoC-Dringlichkeit → keine Morgen-Floor-/Budget-Pausen,
    # Cap anheben damit Shed-Schleifen nicht greifen.
    # Ausnahme Schlechtwetter-Vorladung: Sparen hat Vorrang vor Flex-Füllung –
    # aber NICHT wenn Speicher schon über Max (Override / over_max).
    if (
        str(binding) in ("pre_storm_charge", "survival_mode")
        and live_export_kw < learn_thr
        and reserve_priority
        and not bool(fill.get("over_max"))
        and not bool(state.get("max_soc_override_active"))
    ):
        fill_urgent = False
        state["fill_urgency_active"] = False
    if fill_urgent:
        need_kw = 0.0
        if stack:
            need_kw = sum(
                float(x.get("power_kw") or 0)
                for x in stack
                if str(sim_status.get(x.get("ip")) or "").lower() != "running"
            )
        elif flex.get("should_start_now"):
            flex_ip = str(flex.get("ip") or "")
            flex_kw = float(flex.get("power_kw") or 0)
            if (
                flex_ip
                and str(sim_status.get(flex_ip) or "").lower() != "running"
                and flex_kw > 0
            ):
                need_kw = flex_kw
        budget_kw = max(budget_kw, running_load + need_kw)
        pause_threshold_kw = max(
            pause_threshold_kw,
            running_load + need_kw + BUDGET_PAUSE_MARGIN_KW,
        )
        messages.append(
            "Max-SoC-Füll: Morgen-Floor-/Budget-Pause ausgesetzt – zuerst Last erhöhen."
        )

    # Unterverbrauch darf Budget-Shed blockieren – aber nicht wenn Morgen-Floor/
    # Streckung/Überhang Energiesparen brauchen (Mo historisch hoher Verbrauch).
    energy_save_shed = bool(
        morning_threat
        or running_load > pause_threshold_kw + 0.5
        or (
            stretch.get("active")
            and not export_mode
            and float(stretch.get("target_budget_kw") or 0) + 0.5 < running_load
        )
    )
    skip_budget_shed = False
    if fill_urgent:
        skip_budget_shed = True  # Pause-Pfade bewusst übersprungen (Message oben)
    elif state.get("anomaly_low_active") and not energy_save_shed:
        messages.append(
            "Budget-Abschaltung übersprungen – Unterverbrauch-Anomalie regelt "
            "zusätzliche Einschaltungen."
        )
        skip_budget_shed = True
    elif state.get("anomaly_low_active") and energy_save_shed:
        messages.append(
            "Unterverbrauch-Anomalie weicht – Morgen-Floor/Streckung/Budget "
            "hat Vorrang beim Lastabbau."
        )
        actions = [
            a
            for a in actions
            if not (
                a.get("action") == "resume"
                and (
                    a.get("source") == "anomaly_low"
                    or "Unterverbrauch" in str(a.get("reason") or "")
                )
            )
        ]

    if skip_budget_shed:
        pass
    elif stretch.get("active") and stretch_pause_actions and not export_mode and not (
        (fill.get("flex") or {}).get("should_start_now")
        or fill.get("flex_stack")
        or float(fill.get("excess_charge_kw") or 0) >= 1.5
        or fill.get("capacity_limited")
    ):
        # Flex-Resumes aus früherem Tick-Pfad verwerfen
        flex_set = {str(x) for x in (stretch.get("flex_ips") or [])}
        actions = [
            a
            for a in actions
            if not (
                a.get("action") == "resume"
                and str(a.get("ip") or "") in flex_set
            )
        ]
        stretch_ctx = dict(stretch)
        stretch_ctx["_state"] = state
        stretch_ctx["_config"] = config
        sa, sm, _shed = stretch_pause_actions(stretch_ctx, sim_status, devices)
        already_pause = {
            a.get("ip") for a in actions if a.get("action") == "pause"
        }
        for a in sa:
            if a.get("ip") in already_pause:
                continue
            actions.append(a)
        running_load = miner_load_kw(devices, sim_status)
        messages.extend(sm)
        fill_schedule_resume_actions = None  # type: ignore
    elif stretch.get("active") and (
        export_mode
        or (fill.get("flex") or {}).get("should_start_now")
        or fill.get("flex_stack")
        or float(fill.get("excess_charge_kw") or 0) >= 1.5
        or fill.get("capacity_limited")
    ):
        messages.append(
            "Anker-Streckung weicht – "
            + (
                f"Live-Export {live_export_kw:.2f} kW hat Vorrang."
                if export_mode
                else "Füllkurve / Flex gegen Max-SoC hat Vorrang."
            )
        )
    elif (
        not stretch.get("active")
        and stretch_exhausted_pause_anchor
        and stretch.get("reason") in (
            "insufficient_headroom",
            "short_runtime",
            "evening_hold",
        )
    ):
        ea, em = stretch_exhausted_pause_anchor(stretch, sim_status, devices)
        already_pause = {
            a.get("ip") for a in actions if a.get("action") == "pause"
        }
        for a in ea:
            if a.get("ip") in already_pause:
                continue
            actions.append(a)
        if ea:
            anchor_ip = str(stretch.get("anchor_ip") or "")
            actions = [
                a
                for a in actions
                if not (
                    a.get("action") == "resume"
                    and str(a.get("ip") or "") == anchor_ip
                )
            ]
            running_load = miner_load_kw(devices, sim_status)
            messages.extend(em)
            fill_schedule_resume_actions = None  # type: ignore
        # Auch wenn Anker schon aus: kein Gutwetter-„bleiben an“ über die Reserve hinaus
        elif (export_mode or run_through.get("no_budget_pause")) and not morning_threat:
            messages.append(
                "Anker-Reserve erreicht – kein Durchlauf mehr; "
                f"SoC {stretch.get('soc_percent')} %, verfügbar "
                f"{stretch.get('available_for_anchor_kwh')} kWh."
            )
        else:
            # Gestaffelt: max. 1 Pause/Tick (niedrigste Prio), Anker bleibt länger
            # (auch bei Morgen-Floor trotz Export-Modus)
            while running_load > pause_threshold_kw and budget_kw >= 0:
                candidate = pick_pause_candidate(
                    devices,
                    sim_status,
                    state,
                    config,
                    live=live,
                    priority_plan=priority_plan,
                    energy_stress=True,
                    export_mode=False,
                    near_min_soc=True,
                    respect_plan_hold=False,
                )
                if candidate is None:
                    break
                ip = candidate["ip"]
                if _session_last_device(state, ip, sim_status):
                    messages.append(
                        f"Anker-Reserve drosselt, beendet aber den Überschuss-Zug nicht – "
                        f"{ip} bleibt als letztes Session-Gerät an."
                    )
                    break
                pwr = float(candidate.get("power_kw") or 0)
                prio = PRIORITY_LABEL.get(
                    normalize_device_priority(candidate.get("priority")), "Mittel"
                )
                actions.append(
                    {
                        "ip": ip,
                        "action": "pause",
                        "reason": (
                            f"Anker-Reserve / Budget ({budget_kw:.1f} kW): "
                            f"Last {running_load:.1f} kW – {prio} aus (gestaffelt 1/Tick)"
                            + (" · Morgen-Floor" if morning_threat else "")
                        ),
                        # Anti-Flatter: kein force – Commitment/Min-Laufzeit greifen
                        "force": False,
                        "morning_floor": bool(morning_threat),
                        "budget_shed": True,
                        "load_overhang_kw": round(
                            max(0.0, running_load - budget_kw), 2
                        ),
                    }
                )
                sim_status[ip] = "paused"
                running_load -= pwr
                messages.append(
                    f"Gestaffelte Abschaltung: {ip} ({prio}) – "
                    f"weitere Stufen im nächsten Tick, Anker bleibt."
                )
                break
    elif morning_threat and running_load > pause_threshold_kw and budget_kw >= 0:
        # Morgen-Floor: Export-/Durchlauf-Halt weicht – niedrige Prio zuerst (1/Tick)
        candidate = pick_pause_candidate(
            devices,
            sim_status,
            state,
            config,
            live=live,
            priority_plan=priority_plan,
            energy_stress=True,
            export_mode=False,
            near_min_soc=True,
            respect_plan_hold=False,
        )
        if candidate is None:
            messages.append(
                f"Morgen-Floor gefährdet (Last {running_load:.1f} kW > Cap "
                f"{pause_threshold_kw:.1f} kW), aber kein abwerfbares Gerät "
                f"(Mindestlaufzeit/Anker-Hold)."
            )
        elif _session_last_device(state, candidate.get("ip"), sim_status):
            messages.append(
                f"Morgen-Floor drosselt, beendet aber den Überschuss-Zug nicht – "
                f"{candidate.get('ip')} bleibt als letztes Session-Gerät an."
            )
        else:
            ip = candidate["ip"]
            pwr = float(candidate.get("power_kw") or 0)
            prio = PRIORITY_LABEL.get(
                normalize_device_priority(candidate.get("priority")), "Mittel"
            )
            actions.append(
                {
                    "ip": ip,
                    "action": "pause",
                    "reason": (
                        f"Morgen-Floor (≥{checkpoint_soc:.0f} %): Last "
                        f"{running_load:.1f} kW > nachhaltig {budget_kw:.1f} kW – "
                        f"{prio} aus (gestaffelt 1/Tick, hohe Prio bleibt)"
                    ),
                    # Anti-Flatter: kein force – sonst umgeht Spätstart/Export die Sperren nicht allein
                    "force": False,
                    "morning_floor": True,
                    "budget_shed": True,
                    "load_overhang_kw": round(max(0.0, running_load - budget_kw), 2),
                }
            )
            sim_status[ip] = "paused"
            running_load -= pwr
            messages.append(
                f"Morgen-Schutz: {ip} ({prio}) gestaffelt aus – "
                f"weitere niedrige Prioritäten im nächsten Tick."
            )
    elif (export_mode or run_through.get("no_budget_pause")) and not morning_threat and not reserve_priority:
        # Gutwetter-Durchlauf / Export: kein Abschalten nur wegen 7-Tage-Budget 0
        # (verhindert Hydro aus → 15 min später wieder ein).
        # Ausnahme Reserve/Vorladung: Schiedsrichter-Cap erlaubt gestaffeltes Sparen.
        if running_load > pause_threshold_kw + 2.0 and not run_through.get("no_budget_pause"):
            messages.append(
                f"Export-Modus: Last {running_load:.1f} kW über Budget {budget_kw:.1f} kW – "
                f"kein planmäßiges Abschalten während Surplus-Aufnahme."
            )
        else:
            messages.append(
                "Wetter-Durchlauf/Export: keine Budget-Abschaltung – Miner bleiben an."
            )
    else:
        # Gestaffelt: nur 1 Gerät/Tick (niedrigste Prio zuerst)
        # Plan-Hold: Geräte mit Restlaufzeit im Surplus-Plan laufen durch
        # kurzfristige Dips weiter (einmal aus wenn Plan-Budget weg / Safety).
        while running_load > pause_threshold_kw and budget_kw >= 0:
            candidate = pick_pause_candidate(
                devices,
                sim_status,
                state,
                config,
                live=live,
                priority_plan=priority_plan,
                energy_stress=energy_stress
                or running_load > budget_kw + 1.5
                or (preferred_kw > 0 and running_load > preferred_kw + 1.5),
                export_mode=export_mode,
                near_min_soc=near_min_soc,
                respect_plan_hold=True,
            )
            if candidate is None:
                held = [
                    d
                    for d in devices
                    if sim_status.get(d.get("ip")) == "running"
                    and should_hold_through_budget_dip(
                        d, priority_plan, config, near_min_soc=near_min_soc
                    )
                ]
                if held and not near_min_soc:
                    messages.append(
                        f"Kurzfristige Budget-Überschreitung "
                        f"({running_load:.1f} kW > {pause_threshold_kw:.1f} kW) – "
                        f"{len(held)} Gerät(e) laufen weiter laut Plan-Restlaufzeit "
                        f"(kein Ein-/Aus-Flattern; Safety bei Min-SoC greift weiter)."
                    )
                else:
                    messages.append(
                        f"Budget überschritten ({running_load:.1f} kW > {pause_threshold_kw:.1f} kW), "
                        f"aber alle laufenden Geräte in Mindestlaufzeit – kein planmäßiges Abschalten."
                    )
                break
            ip = candidate["ip"]
            if _session_last_device(state, ip, sim_status):
                messages.append(
                    f"Budget drosselt, beendet aber den Überschuss-Zug nicht – "
                    f"{ip} bleibt als letztes Session-Gerät an."
                )
                break
            pwr = float(candidate.get("power_kw") or 0)
            prio = PRIORITY_LABEL.get(
                normalize_device_priority(candidate.get("priority")), "Mittel"
            )
            force_flex = should_force_pause_flex(
                candidate,
                priority_plan,
                energy_stress=near_min_soc
                or energy_stress
                or running_load > budget_kw + 1.5
                or (preferred_kw > 0 and running_load > preferred_kw + 1.5),
                export_mode=export_mode,
            )
            # Force nur noch bei Safety (nahe Min-SoC / Nacht). Das SoC-Prio-Neuron
            # wirkt auf der Planebene über merge_neuron_into_priority_plan und
            # schaltet nicht mehr selbst – sonst entsteht ein zweiter Planer.
            use_force = bool(
                (night_mode and running_load > budget_kw)
                or (force_flex and near_min_soc)
            )
            remain_h = plan_runtime_remaining_h(ip, priority_plan)
            actions.append(
                {
                    "ip": ip,
                    "action": "pause",
                    "reason": (
                        f"Energie-Budget ({budget_kw:.1f} kW effektiv): "
                        f"Last {running_load:.1f} kW – niedrigste abschaltbare Priorität ({prio})"
                        + (
                            f" · Plan-Rest {remain_h:.1f} h aufgebraucht"
                            if remain_h < normalize_plan_hold_hours(
                                config.get("plan_budget_hold_hours")
                            )
                            else ""
                        )
                        + (" · Flex für Anker-Laufzeit" if force_flex else "")
                        + (" · Nacht ohne PV" if night_mode else "")
                        + " · gestaffelt 1/Tick"
                    ),
                    "force": use_force,
                    "budget_shed": True,
                    "load_overhang_kw": round(max(0.0, running_load - budget_kw), 2),
                }
            )
            messages.append(
                f"Steuerung plant Gerät {ip} auszuschalten "
                f"(Priorität {prio}, Budget überschritten, gestaffelt"
                + (", Flex weicht Anker" if force_flex else "")
                + ")."
            )
            _append_budget_shed_log(
                state,
                ip,
                miner_notes.get(ip, ip),
                f"Budget: {running_load:.1f} kW > {budget_kw:.1f} kW",
                prio,
            )
            sim_status[ip] = "paused"
            running_load -= pwr
            break

    # Anker-Streckung: keine Flex-Resumes (auch nicht aus Füll-Plan / Entladen)
    # Ausnahme: Füllkurve braucht Flex jetzt gegen Max-SoC – oder Live-Export.
    fill_want_flex = bool(
        fill_urgent
        or (fill.get("flex") or {}).get("should_start_now")
        or fill.get("flex_stack")
        or float(fill.get("excess_charge_kw") or 0) >= 1.5
        or fill.get("capacity_limited")
    )
    if stretch.get("active") and not fill_want_flex and not export_mode:
        flex_set = {str(x) for x in (stretch.get("flex_ips") or [])}
        actions = [
            a
            for a in actions
            if not (
                a.get("action") == "resume"
                and str(a.get("ip") or "") in flex_set
            )
        ]
        # Füll-Resume überspringen
        fill_schedule_resume_actions = None  # type: ignore
    elif stretch.get("active") and (fill_want_flex or export_mode):
        messages.append(
            "Anker-Streckung pausiert – "
            + (
                f"Live-Export {live_export_kw:.2f} kW hat Vorrang."
                if export_mode
                else (
                    f"Füllkurve braucht Flex jetzt "
                    f"(Ladung {fill.get('charge_kw')} kW ≫ Soll {fill.get('desired_charge_kw')} kW)."
                )
            )
        )

    if soc is not None and float(soc) <= min_soc + 1:
        messages.append("SOC nahe Mindest-SoC – keine Einschaltungen geplant.")
    elif (
        night_mode
        and soc is not None
        and float(soc) <= checkpoint_soc + 2
        and not export_mode
        and live_export_kw < learn_thr
    ):
        messages.append(
            f"Nacht ohne PV: SOC {float(soc):.0f} % nahe Checkpoint-Ziel "
            f"{checkpoint_soc:.0f} % – keine Einschaltungen."
        )
    elif find_priority_violations(devices, sim_status):
        messages.append(
            "Einschalten blockiert – höherwertige Priorität noch aus, "
            "niedrigere Stufe darf nicht starten."
        )
    elif resume_blocked:
        messages.append("Anomalie kürzlich beendet – Einschaltungen noch gesperrt.")
    elif defer_start and night_mode:
        messages.append(
            "Einschalten (Nacht) zurückgestellt – Spätstart wartet auf "
            f"~{str(late_plan.get('latest_start') or '')[11:16]} "
            f"(Ziel {late_plan.get('target_soc_percent', '—')} %)."
        )
    elif running_load <= resume_threshold_kw:
        tier_paused = paused_devices_highest_tier(devices, sim_status)
        resumed = False
        for d in sort_for_resume(tier_paused):
            ip = d["ip"]
            pwr = float(d.get("power_kw") or 0)
            if running_load + pwr > resume_threshold_kw:
                continue
            if should_defer_resume_for_stability(
                ip, state, config, urgent=False
            ):
                rem = off_stability_remaining_h(ip, state, config)
                messages.append(
                    f"Kein Einschalten {ip}: Aus-Ruhezeit noch {rem:.2f} h "
                    f"(Anti-Flapping – einmal aus bis Budget wieder trägt)."
                )
                continue
            if str(ip) in run_reserve_blocked:
                messages.append(
                    f"Kein Einschalten {ip}: Laufreserve "
                    f"{float(drain_plan.get('g_kwh') or 0):.1f} kWh – Speicher "
                    f"trägt nicht bis zum PV-Break-even."
                )
                continue
            if morning_shed:
                role = _device_plan_role(ip, priority_plan)
                if role == "spill" or (
                    role == "flex"
                    and not ((priority_plan.get("by_ip") or {}).get(str(ip)) or {}).get(
                        "prefer_on"
                    )
                ):
                    messages.append(
                        f"Kein Einschalten {ip}: Morgen-Floor – "
                        f"niedrige Priorität ({role}) bleibt aus, hohe sollen durchlaufen."
                    )
                    continue
            defer_n, defer_why = should_defer_night_flex_resume(
                state, d, priority_plan
            )
            if defer_n:
                messages.append(f"Kein Einschalten {ip}: {defer_why}")
                continue
            # Bei Export-Vermeidung: 7-Tage-Surplus-Gate überspringen
            # (sonst blockiert need_kwh = power × 168h praktisch jedes Gerät)
            need_kwh = pwr * hours_left
            if not export_mode and surplus_kwh < need_kwh * 0.3:
                continue
            ok, why = may_resume_by_priority_plan(
                d,
                priority_plan,
                export_mode=export_mode,
                discharge_mode=discharge_mode,
                higher_tier_running=True,
                conserve_for_high_priority=conserve_for_high_priority_lehr,
            )
            if not ok:
                messages.append(
                    f"Kein Einschalten {ip}: {why} "
                    f"(Surplus {surplus_kwh:.0f} kWh → längere Laufzeit höherer Prio)."
                )
                continue
            prio = PRIORITY_LABEL.get(
                normalize_device_priority(d.get("priority")), "Mittel"
            )
            actions.append(
                {
                    "ip": ip,
                    "action": "resume",
                    "reason": (
                        f"Energie-Budget ({surplus_kwh:.1f} kWh / {budget_kw:.1f} kW), "
                        f"Priorität {prio} ({why})"
                        + (" · Einspeisung vermeiden" if export_mode else "")
                    ),
                    "force": False,
                }
            )
            messages.append(
                f"Steuerung plant Gerät {ip} einzuschalten (Priorität {prio}, {why})."
            )
            resumed = True
            break
        if not resumed and tier_paused:
            horizon_h = max(1, int(round(float(hours_left or 0))))
            messages.append(
                f"Kein Einschalten diesen Tick: Miner-Budget {budget_kw:.1f} kW "
                f"(Last {running_load:.1f} kW) – nächstes Gerät würde Grenze überschreiten "
                f"oder Budget ({surplus_kwh:.1f} kWh / {horizon_h} h) reicht nicht "
                f"bzw. ist für höhere Priorität reserviert."
            )
        elif not resumed and not tier_paused and running_load < 0.1:
            messages.append(
                f"Alle Miner aus – Miner-Budget erlaubt max. {budget_kw:.1f} kW "
                f"({surplus_kwh:.1f} kWh bis Horizontende)."
            )

    # Füll-Rechnung: Flex gegen Max-SoC (auch wenn 7-Tage-Budget nur den Anker deckt).
    # Max-SoC-Dringlichkeit darf den Morgen-Floor-Gate für Resumes überstimmen –
    # sonst erzeugt nur Spätstart Resumes ohne Füll-Flags und Anti-Flatter blockiert.
    # fill_urgent wurde früh berechnet.
    if (
        fill_schedule_resume_actions
        and not resume_blocked
        and (not morning_threat or fill_urgent)
        and not state.get("min_soc_lock_active")
        and soc is not None
        and float(soc) > min_soc + 1
    ):
        already = {a["ip"] for a in actions if a.get("action") == "resume"}
        fa, fm, running_load = fill_schedule_resume_actions(
            config, state, live, sim_status, running_load
        )
        # Bei Morgen-Floor + Last ≥ Cap: Spill/niedrige Flex nicht tickweise nachziehen.
        # Ausnahme fill_urgent / Max-SoC-Override: Last muss zurück unter Max.
        if morning_shed and not fill_urgent and not state.get("max_soc_override_active"):
            keep_fa: List[Dict[str, Any]] = []
            for a in fa:
                ip = str(a.get("ip") or "")
                role = _device_plan_role(ip, priority_plan)
                prefer = bool(
                    ((priority_plan.get("by_ip") or {}).get(ip) or {}).get("prefer_on")
                )
                # Cap-Überhang: niedrige Prio nie; sonst nur wenn prefer_on/Anker
                over_cap = running_load >= pause_threshold_kw
                if role == "spill" or (role == "flex" and not prefer and over_cap):
                    messages.append(
                        f"Füll-Resume {ip} unterdrückt – Morgen-Floor, "
                        f"niedrige Prio ({role or 'flex'}) bleibt aus (Anti-Flatter)."
                    )
                    # fill_schedule_resume_actions hat sim_status schon angehoben
                    if str(sim_status.get(ip) or "").lower() == "running":
                        pwr = 0.0
                        for d in devices:
                            if str(d.get("ip") or "") == ip:
                                pwr = float(d.get("power_kw") or 0)
                                break
                        sim_status[ip] = "paused"
                        running_load = max(0.0, running_load - pwr)
                    continue
                keep_fa.append(a)
            fa = keep_fa
        elif morning_threat or fill_urgent:
            messages.append(
                "Füll-Resume trotz Morgen-Floor – Max-SoC/Kapazität "
                "(vorheriger Floor-Shed wird zurückgenommen)."
            )
        for a in fa:
            ip = str(a.get("ip") or "")
            if ip in run_reserve_blocked:
                pwr = 0.0
                for d in devices:
                    if str(d.get("ip") or "") == ip:
                        pwr = float(d.get("power_kw") or 0)
                        break
                if str(sim_status.get(ip) or "").lower() == "running":
                    sim_status[ip] = "paused"
                    running_load = max(0.0, running_load - pwr)
                messages.append(
                    f"Füll-Resume {ip} unterdrückt – Laufreserve "
                    f"{float(drain_plan.get('g_kwh') or 0):.1f} kWh, "
                    "Speicher ist nach PV-Ende die Quelle."
                )
                continue
            if a.get("ip") in already:
                # Bestehenden Resume (z. B. Spätstart) mit Füll-Dringlichkeit stempeln
                for prev in actions:
                    if prev.get("ip") == a.get("ip") and prev.get("action") == "resume":
                        prev["fill_urgency"] = bool(
                            prev.get("fill_urgency") or a.get("fill_urgency")
                        )
                        if a.get("source"):
                            prev.setdefault("source", a.get("source"))
                continue
            actions.append(a)
            sim_status[a["ip"]] = "running"
        messages.extend(fm)

    elif not resume_blocked and running_load > budget_kw:
        messages.append(
            "Kein Einschalten: Budget reicht für laufende Last nicht – "
            "zuerst werden niedrigere Prioritäten abgeschaltet."
        )

    # Abschluss: Prioritäts-Invariante erneut prüfen (z. B. nach Resume-Planung)
    # Unsichere Flex-Resumes verwerfen und fehlende höhere Stufe nachziehen.
    actions, sim_status, running_load, fix_msgs = _enforce_priority_before_switch(
        actions,
        devices,
        sim_status,
        running_load,
        miner_notes,
        live_status=miner_status,
        state=state,
        config=config,
        live=live,
    )
    messages.extend(fix_msgs)
    for d in find_priority_violations(devices, sim_status):
        ip = d.get("ip")
        if not ip or sim_status.get(ip) != "running":
            continue
        if any(a.get("ip") == ip and a.get("action") == "pause" for a in actions):
            continue
        prio = PRIORITY_LABEL.get(normalize_device_priority(d.get("priority")), "Mittel")
        # Höhere Stufe wird diesen Tick geholt → Low nicht force-killen
        if any(a.get("action") == "resume" for a in actions):
            messages.append(
                f"Prioritätslücke: {ip} ({prio}) bleibt an – zuerst höhere Stufe einschalten."
            )
            continue
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": (
                    f"Prioritätsreihenfolge: {prio} darf nicht laufen, "
                    f"solange höherwertige Geräte noch aus sind"
                ),
                "force": True,
            }
        )
        messages.append(
            f"Steuerung plant Gerät {ip} auszuschalten (Prioritätsverletzung, {prio})."
        )
        sim_status[ip] = "paused"
        # Kein automatisches Flex-Resume hier – _enforce_priority_before_switch
        # hat Anker ggf. schon nachgezogen; Flex erst wenn Anker live läuft.
        break

    if not messages:
        messages.append("Keine Änderungen geplant – aktuelle Werte im Zielbereich.")

    # Überschuss-Zug eröffnen/ergänzen. Hier laufen alle regulären Resume-Pfade
    # zusammen (Export, Budget, Füllplan); die Sicherheits-Fast-Paths oben sind
    # schon vorher zurückgekehrt und eröffnen bewusst keine Session.
    try:
        from control_algorithm.surplus_session import (
            add_devices,
            is_active,
            open_session,
        )

        resumes_now = [a for a in actions if a.get("action") == "resume" and a.get("ip")]
        session_ips = sorted(
            {str(a.get("ip")) for a in resumes_now}
            | {
                str(d.get("ip"))
                for d in devices
                if d.get("ip") and sim_status.get(d.get("ip")) == "running"
            }
        )
        if is_active(state):
            add_devices(state, session_ips)
        elif resumes_now:
            from control_algorithm.surplus_session import neuron_allowed_devices

            # Neuron formt das Ziel mit: nicht freigegebene Geräte gehören nicht
            # in den Zug – es blockiert sie aber nicht mehr beim Schalten.
            allowed = neuron_allowed_devices(state, config)
            if allowed:
                session_ips = [ip for ip in session_ips if ip in set(allowed)] or session_ips
            trigger = "export" if export_mode else ("fill" if fill_urgent else "budget")
            open_session(state, config, live, devices=session_ips, trigger=trigger)
            if is_active(state):
                from control_algorithm.surplus_session import session_summary

                messages.append(session_summary(state))
    except Exception as exc:  # pragma: no cover
        state["surplus_session_error"] = str(exc)

    # Für Anti-Flatter Budget-Override (committed_on): Last vs. Cap dieses Ticks
    state["tick_miner_load_kw"] = round(float(running_load), 2)
    state["tick_miner_budget_kw"] = round(float(budget_kw), 2)

    messages.append(f"Berechneter PV-Ertrag heute: ca. {pv_today_kwh:.1f} kWh.")
    messages.append(f"Berechneter PV-Ertrag morgen: ca. {pv_tomorrow_kwh:.1f} kWh.")
    messages.append(f"Lernfaktor (yield_factor): {yield_factor:.3f}")
    total_kw = sum(float(d.get("power_kw") or 0) for d in devices)
    messages.append(
        f"Eingetr. Miner-Leistung gesamt: {total_kw:.1f} kW ({len(devices)} Gerät(e))."
    )
    messages.append(f"Mindestlaufzeit pro Aktivierung: {min_runtime_h:g} h.")
    min_off_h = normalize_min_off_hours(config.get("min_device_off_hours"))
    if min_off_h > 0:
        messages.append(
            f"Ruhezeit-Empfehlung nach Pause: {min_off_h:g} h "
            f"(keine harte Sperre; bei Export/Budget sofort Start möglich)."
        )

    return actions, messages


def apply_device_runtime_guards(
    actions: List[Dict[str, Any]],
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Blockiert Pause vor Mindestlaufzeit und Resume vor Aus-Ruhezeit (Anti-Flap)."""
    runtime: Dict[str, Any] = dict(state.get("device_runtime") or {})
    now = datetime.now()
    filtered: List[Dict[str, Any]] = []
    devices = enabled_devices(config)
    by_ip = {d.get("ip"): d for d in devices if d.get("ip")}

    try:
        from control_algorithm.steering_learning import (
            effective_min_runtime_hours,
            should_hold_top_priority,
        )
    except Exception:
        effective_min_runtime_hours = None  # type: ignore
        should_hold_top_priority = None  # type: ignore

    for act in actions:
        ip = act.get("ip")
        action = act.get("action")
        device = by_ip.get(ip)

        if action == "resume":
            urgent = bool(
                act.get("urgent")
                or act.get("force")
                or act.get("fill_urgency")
                or act.get("commitment_fill_override")
            )
            if should_defer_resume_for_stability(
                str(ip or ""), state, config, urgent=urgent
            ):
                act = dict(act)
                act["blocked_min_off"] = True
                act["min_off_remaining_h"] = round(
                    off_stability_remaining_h(str(ip or ""), state, config), 2
                )
                continue
            filtered.append(act)
            continue

        if action != "pause" or act.get("force"):
            filtered.append(act)
            continue
        # Morgen-Floor / Budget-Shed: Mindestlaufzeit weicht – sonst bleiben
        # Spill/Flex nach kurzem Export 3 h an und fressen Reserve für hohe Prio.
        if (
            act.get("budget_shed")
            or act.get("morning_floor")
            or act.get("commitment_budget_override")
            or act.get("run_reserve")
        ):
            filtered.append(act)
            continue
        if live and device and should_hold_top_priority:
            try:
                if should_hold_top_priority(config, state, live, device, devices):
                    act = dict(act)
                    act["blocked_top_prio_hold"] = True
                    act["blocked_min_runtime"] = True
                    continue
            except Exception:
                pass
        if effective_min_runtime_hours:
            min_runtime_h = effective_min_runtime_hours(
                config, state, live, device, devices
            )
        else:
            min_runtime_h = normalize_min_runtime_hours(
                config.get("min_device_runtime_hours")
            )
        info = runtime.get(ip) or {}
        resumed_at = info.get("resumed_at")
        if not resumed_at:
            filtered.append(act)
            continue
        try:
            started = datetime.fromisoformat(str(resumed_at))
            elapsed_h = (now - started).total_seconds() / 3600.0
            if elapsed_h >= min_runtime_h:
                filtered.append(act)
            else:
                act = dict(act)
                act["blocked_min_runtime"] = True
                act["min_runtime_remaining_h"] = round(min_runtime_h - elapsed_h, 2)
        except Exception:
            filtered.append(act)
    return filtered


def apply_device_state_updates(
    state: Dict[str, Any],
    applied_actions: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Aktualisiert device_runtime inkl. Anti-Flatter-Commitments nach Schaltungen."""
    try:
        from control_algorithm.switch_commitment import update_runtime_commitments
        from control_algorithm.config import load_config

        cfg = config if isinstance(config, dict) else load_config()
        return update_runtime_commitments(state, applied_actions, cfg)
    except Exception:
        runtime: Dict[str, Any] = dict(state.get("device_runtime") or {})
        now = datetime.now().isoformat()
        for act in applied_actions:
            ip = act.get("ip")
            action = act.get("action")
            if not ip or not action:
                continue
            entry = dict(runtime.get(ip) or {})
            entry["last_action"] = action
            entry["last_action_at"] = now
            if action == "resume":
                entry["resumed_at"] = now
                entry["paused_at"] = None
            elif action == "pause":
                entry["resumed_at"] = None
                entry["paused_at"] = now
            runtime[ip] = entry
        state["device_runtime"] = runtime
        return state


def update_device_runtime(
    state: Dict[str, Any],
    executed_actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Kompatibilitäts-Wrapper: nur wirklich ausgeführte Aktionen."""
    return apply_device_state_updates(
        state,
        [act for act in executed_actions if act.get("executed")],
    )
