# -*- coding: utf-8 -*-
"""
Lineare Speicher-Füll-Rechnung mit flacher Zielkurve bis PV-Ende.

Problem der „kurz vor Max einschalten“-Logik:
  Bei steiler Ladung (z. B. 6,7 kW) und spätem Flex-Start bleibt nach
  Einschalten von Luftgekühlt (3,5 kW) immer noch ~3,2 kW Ladung –
  der Speicher trifft 97 % zu früh, Rest-PV geht in Einspeisung.

Bessere Idee (flacherer Winkel):
  Ziel: Max-SoC erst gegen PV-Ende erreichen.
  Soll-Ladung = Restplatz / Stunden_bis_PV-Ende
  Ist-Ladung = PV − Haus − laufende Miner
  Überschuss-Ladung = Ist − Soll
  → Flex so früh starten, dass
      Restplatz = Ist×t_vor + (Ist−Flex)×t_flex
      mit t_vor + t_flex = Stunden_bis_PV-Ende
  → t_flex = (Ist×T − Restplatz) / Flex_kW
  → Start = PV-Ende − t_flex  (oder sofort, wenn t_flex ≥ T)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.config import normalize_device_priority
from control_algorithm.planner import enabled_devices, sort_for_resume


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _house_kw(live: Dict[str, Any], state: Dict[str, Any]) -> float:
    for key in ("house_kw", "wohnhaus_kw"):
        if live.get(key) is not None:
            return max(0.0, _f(live.get(key)))
    try:
        from pathlib import Path
        import json
        from control_algorithm.config import BACKEND_DIR

        en_path = Path(BACKEND_DIR) / "energy_monitor" / "enline_values.json"
        if en_path.is_file():
            en = json.loads(en_path.read_text(encoding="utf-8"))
            w = (en.get("values_kw") or {}).get("wohnhaus")
            if w is not None:
                return max(0.0, _f(w))
    except Exception:
        pass
    if live.get("verbrauch_kw") is not None:
        return max(0.0, _f(live.get("verbrauch_kw")))
    for key in ("live_house_kw", "live_verbrauch_kw"):
        if state.get(key) is not None:
            return max(0.0, _f(state.get(key)))
    return 0.0


def _pv_end_datetime(state: Dict[str, Any], now: datetime) -> Tuple[datetime, float, str]:
    """Ende der heutigen PV (für flache Füllkurve) + Stunden bis dahin."""
    try:
        from control_algorithm.weather_survival import last_pv_hour_for_date

        last_h = int(last_pv_hour_for_date(state, now.date()) or 20)
    except Exception:
        last_h = 20
    last_h = max(now.hour, min(22, last_h))
    end = datetime.combine(now.date(), datetime.min.time()).replace(
        hour=last_h, minute=0
    )
    if end <= now:
        # PV-Tag ist vorbei – kein künstlicher 45-min-Horizont mehr.
        # Der hat Soll-Ladung = Restplatz/0,75 h erfunden und nachts
        # Geräte angeworfen, obwohl nichts mehr produziert wird.
        end = now
        note = "PV-Ende erreicht"
        hours = 0.0
    else:
        note = f"PV-Ende heute ~{last_h:02d}:00"
        hours = max(0.25, (end - now).total_seconds() / 3600.0)
    return end, hours, note


def compute_fill_schedule(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Flache Zielkurve bis PV-Ende → frühere Flex-Einschaltung gegen Max-SoC.
    """
    now = now or datetime.now()
    storage = _f(config.get("storage_kwh"))
    hard_max = _f(config.get("max_soc_percent"), 97.0)
    try:
        from control_algorithm.storage_band_learning import effective_planning_max_soc

        plan_max = float(effective_planning_max_soc(config, state, live=live))
    except Exception:
        plan_max = hard_max
    # Füllkurve zielt auf das Plan-Band; das harte Max bleibt nur Notbremse.
    max_soc = min(hard_max, max(5.0, plan_max))
    min_soc = _f(config.get("min_soc_percent"), 28.0)
    soc = live.get("soc_percent")
    if soc is None:
        soc = state.get("live_soc")
    soc = _f(soc, default=-1.0)
    if storage <= 0 or soc < 0:
        return {"ok": False, "reason": "no_soc_or_storage"}

    pv_kw = _f(live.get("pv_kw"), _f(state.get("live_pv_kw")))
    house_kw = _house_kw(live, state)
    batt_kw = live.get("battery_power_kw")
    if batt_kw is None:
        batt_kw = state.get("live_battery_kw")
    batt_kw = _f(batt_kw) if batt_kw is not None else None

    devices = enabled_devices(config)
    ranked = sort_for_resume(devices)
    if not ranked:
        return {"ok": False, "reason": "no_devices"}

    miner_status: Dict[str, str] = dict(live.get("miner_status") or {})
    running = [
        d for d in ranked if str(miner_status.get(d.get("ip")) or "").lower() == "running"
    ]
    paused = [
        d for d in ranked if str(miner_status.get(d.get("ip")) or "").lower() != "running"
    ]
    running_kw = sum(_f(d.get("power_kw")) for d in running)
    anchor = ranked[0]
    anchor_kw = _f(anchor.get("power_kw"))
    flex_candidates = [
        d
        for d in paused
        if normalize_device_priority(d.get("priority"))
        < normalize_device_priority(anchor.get("priority"))
    ]
    if not flex_candidates and paused:
        flex_candidates = list(paused)
    next_flex = flex_candidates[0] if flex_candidates else None

    room_kwh = max(0.0, (max_soc - soc) / 100.0 * storage)
    model_charge = max(0.0, pv_kw - house_kw - running_kw)
    if batt_kw is not None and batt_kw > 0.05:
        # Live: Speicher lädt – Ist-Ladung nicht unterschätzen (Max-SoC-Schutz)
        charge_kw = max(model_charge, batt_kw)
        charge_source = "live_battery"
    elif batt_kw is not None and batt_kw < -0.05:
        # Live: Speicher entlädt – kein Füllen Richtung Oberkante (Modell sonst False-Overflow)
        charge_kw = 0.0
        charge_source = "live_battery_discharge"
    else:
        charge_kw = model_charge
        charge_source = "pv_minus_house_minus_miners"

    pv_end, hours_to_end, pv_end_note = _pv_end_datetime(state, now)

    # Steile Kurve (nur Info): wann wäre Max bei aktueller Ladung?
    hours_to_max_steep: Optional[float] = None
    eta_max_steep: Optional[datetime] = None
    if charge_kw > 0.15 and room_kwh > 0.05:
        hours_to_max_steep = room_kwh / charge_kw
        eta_max_steep = now + timedelta(hours=hours_to_max_steep)
    elif room_kwh <= 0.05:
        hours_to_max_steep = 0.0
        eta_max_steep = now

    pv_ended = hours_to_end <= 0.0 or "PV-Ende erreicht" in pv_end_note

    # Flache Soll-Ladung: Restplatz über den ganzen PV-Resttag verteilen.
    # Nach PV-Ende gibt es nichts mehr zu verteilen – Soll = 0.
    desired_charge_kw = (
        0.0 if pv_ended else (room_kwh / hours_to_end if hours_to_end > 0 else 0.0)
    )
    excess_charge_kw = max(0.0, charge_kw - desired_charge_kw)

    trigger_soc = max(min_soc + 5.0, max_soc - 10.0)
    # Hart: ab Max-SoC → Last ein. Kleine Hysterese nur über State-Latch
    # (siehe apply_max_soc_override_latch), hier die Messschwelle.
    over_plan_band = soc >= max_soc - 0.05
    over_max = soc >= hard_max - 0.05
    # Tagsüber: Füll-Start am Plan-Band. Nach PV-Ende zählt nur das harte Max
    # (95 %) – das gelernte Band (z. B. 89 %) darf nachts keine Miner starten.
    fill_now = over_max if pv_ended else (over_plan_band or over_max)
    flex_plan: Optional[Dict[str, Any]] = None
    flex_stack: List[Dict[str, Any]] = []

    # Hartes Max: Flex auch ohne PV (Entladen zurück unter 95 %).
    # Plan-Band nach Sonnenuntergang: Surplus-/Budgetlogik, nicht Füllkurve.
    if next_flex and (fill_now or (not pv_ended and pv_kw >= 0.4)):
        flex_kw = _f(next_flex.get("power_kw"))
        ip = str(next_flex.get("ip") or "")
        charge_with_flex = max(0.0, charge_kw - flex_kw)

        # Energie, die bis PV-Ende ohne Flex in den Speicher ginge
        energy_no_flex = charge_kw * hours_to_end
        overflow_kwh = max(0.0, energy_no_flex - room_kwh)

        if overflow_kwh < 0.3 and not fill_now:
            # Bis zum Ziel reicht die Energie genau oder nicht.
            # Dann keine Geräte einschalten, auch wenn der SoC schon
            # über der alten Frühstart-Schwelle (Ziel minus 10) liegt.
            flex_plan = {
                "ip": ip,
                "power_kw": flex_kw,
                "priority": normalize_device_priority(next_flex.get("priority")),
                "planned_on": None,
                "start_in_hours": None,
                "reason": (
                    f"Flache Kurve ok: Soll-Ladung {desired_charge_kw:.1f} kW, "
                    f"Ist {charge_kw:.1f} kW, Überlauf ~{overflow_kwh:.1f} kWh "
                    f"bis {pv_end.strftime('%H:%M')} – Flex noch nicht nötig."
                ),
                "should_start_now": False,
                "charge_with_flex_kw": round(charge_with_flex, 2),
                "overflow_kwh": round(overflow_kwh, 2),
                "hours_flex_needed": 0.0,
            }
        elif flex_kw <= 0.05:
            flex_plan = None
        else:
            # t_flex so, dass Restplatz genau mit Ist vor / (Ist−Flex) nach Start aufgeht
            # room = charge * T - flex * t_flex  →  t_flex = (charge*T - room) / flex
            hours_flex = overflow_kwh / flex_kw
            # Wenn auch mit Flex über den Tag noch Überlauf: sofort starten
            energy_with_flex_full_day = charge_with_flex * hours_to_end
            still_overflow = energy_with_flex_full_day > room_kwh + 0.3

            if fill_now or still_overflow or hours_flex >= hours_to_end - 0.05:
                start_in = 0.0
                planned_on = now
                should_now = True
                timing_note = (
                    f"sofort (auch mit Flex bleibt Ladung ~{charge_with_flex:.1f} kW "
                    f"> Soll {desired_charge_kw:.1f} kW – Kurve so flach wie möglich)"
                )
            else:
                start_in = max(0.0, hours_to_end - hours_flex)
                planned_on = now + timedelta(hours=start_in)
                should_now = start_in <= 0.15
                # Zu steile Ist-Ladung: Flex früher als reine t_flex-Uhr
                if excess_charge_kw >= max(1.0, desired_charge_kw * 1.25):
                    should_now = True
                    timing_note = (
                        f"sofort (Ist-Ladung {charge_kw:.1f} kW ≫ Soll "
                        f"{desired_charge_kw:.1f} kW – Kurve glätten)"
                    )
                    start_in = 0.0
                    planned_on = now
                else:
                    timing_note = (
                        f"ab {planned_on.strftime('%H:%M')} "
                        f"({hours_flex:.1f} h Flex bis PV-Ende {pv_end.strftime('%H:%M')})"
                    )

            # Ziel-ETA Max mit Flex ab planned_on (Näherung)
            if start_in <= 0:
                if charge_with_flex > 0.15:
                    eta_flat = now + timedelta(hours=room_kwh / charge_with_flex)
                else:
                    eta_flat = pv_end
            else:
                # vor Flex: charge_kw, danach charge_with_flex
                filled_before = charge_kw * start_in
                room_after = max(0.0, room_kwh - filled_before)
                if charge_with_flex > 0.15 and room_after > 0:
                    eta_flat = planned_on + timedelta(hours=room_after / charge_with_flex)
                else:
                    eta_flat = pv_end

            flex_plan = {
                "ip": ip,
                "power_kw": flex_kw,
                "priority": normalize_device_priority(next_flex.get("priority")),
                "planned_on": planned_on.isoformat(),
                "start_in_hours": round(start_in, 2),
                "reason": (
                    f"Flache Füllkurve bis {pv_end.strftime('%H:%M')}: "
                    f"Restplatz {room_kwh:.1f} kWh, Soll-Ladung {desired_charge_kw:.1f} kW, "
                    f"Ist {charge_kw:.1f} kW (Überlauf {overflow_kwh:.1f} kWh). "
                    f"Ohne Flex Max schon ~"
                    f"{eta_max_steep.strftime('%H:%M') if eta_max_steep else '—'}; "
                    f"mit {ip} ({flex_kw:.1f} kW) Ladung ~{charge_with_flex:.1f} kW → "
                    f"{timing_note}."
                ),
                "should_start_now": bool(should_now),
                "charge_with_flex_kw": round(charge_with_flex, 2),
                "overflow_kwh": round(overflow_kwh, 2),
                "hours_flex_needed": round(min(hours_flex, hours_to_end), 2),
                "eta_max_with_flex": eta_flat.isoformat(),
                "eta_max_with_flex_display": eta_flat.strftime("%H:%M"),
            }

            # Mehrere Flex-Stufen stacken, wenn eine allein den Überlauf nicht schafft
            # oder der Speicher schon am/über Max ist.
            if should_now and (still_overflow or fill_now):
                stacked_kw = 0.0
                for d in flex_candidates:
                    dip = str(d.get("ip") or "")
                    dpwr = _f(d.get("power_kw"))
                    if not dip or dpwr <= 0.05:
                        continue
                    stacked_kw += dpwr
                    flex_stack.append(
                        {
                            "ip": dip,
                            "power_kw": dpwr,
                            "priority": normalize_device_priority(d.get("priority")),
                            "should_start_now": True,
                            "reason": (
                                f"Flex-Stack gegen Max-SoC: kumuliert {stacked_kw:.1f} kW "
                                f"(Soll-Ladung {desired_charge_kw:.1f} kW, Ist {charge_kw:.1f} kW)."
                            ),
                        }
                    )
                    charge_after = max(0.0, charge_kw - stacked_kw)
                    if not over_max and charge_after * hours_to_end <= room_kwh + 0.3:
                        break
                    if over_max:
                        # Über hartem Max: alle pausierten Flex-Lasten nutzen
                        continue
                if flex_stack:
                    flex_plan["stack_kw"] = round(stacked_kw, 2)
                    flex_plan["stack_count"] = len(flex_stack)
                    flex_plan["charge_with_flex_kw"] = round(
                        max(0.0, charge_kw - stacked_kw), 2
                    )

    # Nur wenn wirklich noch geladen/Export-Druck besteht – nicht bei reinem
    # Halten/Entladen genau am Max-SoC.
    capacity_limited = (
        not paused
        and pv_kw >= 0.4
        and charge_kw > 0.15
        and (over_max or excess_charge_kw > 0.3)
    )

    return {
        "ok": True,
        "updated_at": now.isoformat(),
        "mode": "flat_to_pv_end",
        "soc_percent": round(soc, 1),
        "max_soc_percent": max_soc,
        "plan_max_percent": round(max_soc, 1),
        "hard_max_percent": round(hard_max, 1),
        "over_plan_band": bool(over_plan_band),
        "storage_kwh": storage,
        "room_to_max_kwh": round(room_kwh, 2),
        "pv_kw": round(pv_kw, 2),
        "house_kw": round(house_kw, 2),
        "running_miner_kw": round(running_kw, 2),
        "charge_kw": round(charge_kw, 2),
        "charge_source": charge_source,
        "desired_charge_kw": round(desired_charge_kw, 2),
        "excess_charge_kw": round(excess_charge_kw, 2),
        "hours_to_pv_end": round(hours_to_end, 2),
        "pv_end": pv_end.isoformat(),
        "pv_end_display": pv_end.strftime("%H:%M"),
        "pv_end_note": pv_end_note,
        "hours_to_max": round(hours_to_max_steep, 2) if hours_to_max_steep is not None else None,
        "eta_max_soc": eta_max_steep.isoformat() if eta_max_steep else None,
        "eta_max_display": eta_max_steep.strftime("%H:%M") if eta_max_steep else None,
        "trigger_soc_percent": round(trigger_soc, 1),
        "anchor_ip": anchor.get("ip"),
        "anchor_kw": anchor_kw,
        "flex": flex_plan,
        "flex_stack": flex_stack,
        "over_max": bool(over_max),
        "pv_ended": bool(pv_ended),
        "fill_now": bool(fill_now),
        "capacity_limited": capacity_limited,
        "formula": (
            f"Restplatz=({max_soc:.0f}−{soc:.0f})/100×{storage:.0f}={room_kwh:.1f} kWh; "
            f"T bis PV-Ende={hours_to_end:.1f} h → Soll-Ladung={desired_charge_kw:.1f} kW; "
            f"Ist-Ladung≈{charge_kw:.1f} kW; "
            f"Überlauf≈max(0, Ist×T−Rest)="
            f"{max(0.0, charge_kw * hours_to_end - room_kwh):.1f} kWh; "
            f"t_Flex=Überlauf/Flex_kW."
        ),
    }


def compute_fill_urgency(
    plan: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """True wenn Füllkurve jetzt Last will und Max-SoC-Dringlichkeit vorliegt.

    Wichtig: over_max / capacity_limited gelten auch ohne pausierte Flex-Lasten.
    Sonst bleibt Urgency False wenn schon alles läuft – und Morgen-Floor darf
    Verbraucher abwerfen, obwohl Speicher über Max weiter geladen wird / PV voll.
    """
    config = config or {}
    if plan.get("pv_ended") and not plan.get("over_max"):
        return False
    # Zuerst Kapazität/Über-Max – unabhängig von Flex-Startsignalen
    if plan.get("over_max") or plan.get("capacity_limited"):
        return True

    flex = plan.get("flex") or {}
    should_now = bool(flex.get("should_start_now") or plan.get("flex_stack"))
    if not should_now:
        return False
    try:
        hours_thresh = float(config.get("fill_commitment_override_hours_to_max", 2.0))
    except (TypeError, ValueError):
        hours_thresh = 2.0
    hours_thresh = max(0.25, min(12.0, hours_thresh))
    try:
        excess_thresh = float(config.get("fill_commitment_override_excess_kw", 5.0))
    except (TypeError, ValueError):
        excess_thresh = 5.0
    excess_thresh = max(0.5, min(100.0, excess_thresh))

    excess = _f(plan.get("excess_charge_kw"))
    overflow = _f(flex.get("overflow_kwh"))
    hours_raw = plan.get("hours_to_max")
    hours_to_max: Optional[float] = None
    if hours_raw is not None:
        try:
            hours_to_max = float(hours_raw)
        except (TypeError, ValueError):
            hours_to_max = None

    if overflow >= 0.3:
        return True
    if excess >= excess_thresh:
        return True
    if hours_to_max is not None and hours_to_max <= hours_thresh:
        return True
    return False


def apply_max_soc_override_latch(
    state: Dict[str, Any],
    config: Dict[str, Any],
    *,
    soc: Optional[float],
    over_max: bool,
) -> bool:
    """Hartes Über-Max-Override mit Halt-Band (Stufe 1).

    - SOC ≥ Max → Latch EIN (Last erzwingen). Halt-Freigabe wird aufgehoben.
    - SOC ≤ Halt (`max_soc_release_percent`) und Latch war an → Latch AUS,
      `max_soc_hold_released` (alles aus, bis wieder Max).

    Returns True wenn Override jetzt aktiv (einschließlich Latch zwischen Halt und Max).
    Setzt state['max_soc_override_just_cleared'] wenn der Latch in diesem Tick fällt.
    """
    from control_algorithm.battery_protection import max_soc_release_percent

    try:
        max_soc = float(config.get("max_soc_percent") or 95.0)
    except (TypeError, ValueError):
        max_soc = 95.0
    release = max_soc_release_percent(config)
    was = bool(state.get("max_soc_override_active"))
    state["max_soc_override_just_cleared"] = False
    if over_max or (soc is not None and float(soc) >= max_soc - 0.05):
        state["max_soc_override_active"] = True
        state["max_soc_override_cleared_at"] = None
        state["max_soc_hold_released"] = False
        if not was:
            state["max_soc_override_engaged_at"] = datetime.now().isoformat()
        return True
    if was and soc is not None and float(soc) <= release:
        state["max_soc_override_active"] = False
        state["max_soc_override_cleared_at"] = datetime.now().isoformat()
        state["max_soc_override_just_cleared"] = True
        state["max_soc_hold_released"] = True
        # Urgency darf nicht kleben – sonst blockiert Morgen-Floor weiter das Abschalten
        state["fill_urgency_active"] = False
        return False
    active = bool(state.get("max_soc_override_active"))
    if not active and not over_max:
        # Unter Max und kein Latch: Urgency nur aus aktuellem Füllplan (nicht sticky)
        pass
    return active


def sync_fill_urgency_from_plan(
    state: Dict[str, Any],
    config: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
) -> bool:
    """fill_urgency_active aus aktuellem Plan + Override setzen (kein Sticky-True)."""
    plan = dict(plan or state.get("fill_schedule") or {})
    urgent = bool(compute_fill_urgency(plan, config))
    if state.get("max_soc_override_active"):
        urgent = True
    if state.get("max_soc_override_just_cleared"):
        urgent = False
    state["fill_urgency_active"] = urgent
    return urgent


def fill_schedule_resume_actions(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    miner_status: Dict[str, str],
    running_load: float,
) -> tuple[List[Dict[str, Any]], List[str], float]:
    """Wenn flache Füllkurve Flex jetzt braucht → Resume (ggf. mehrere Stufen).

    Nie Flex vor dem Anker: läuft die höchste Priorität noch nicht, wird zuerst
    der Anker eingeschaltet – sonst Prioritätsverletzung (Luft/Heiz vor Hydro).
    """
    from control_algorithm.planner import find_priority_violations

    plan = compute_fill_schedule(config, state, live)
    state["fill_schedule"] = plan
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    if not plan.get("ok"):
        return actions, messages, running_load
    if state.get("min_soc_lock_active"):
        return actions, messages, running_load

    fill_urgency = compute_fill_urgency(plan, config)
    devices = enabled_devices(config)
    # Nach PV-Ende zählt nur hartes Max – Plan-Band darf keine Miner starten.
    if plan.get("pv_ended") and not plan.get("over_max"):
        return actions, messages, running_load
    from control_algorithm.morning_drain import device_allowed_on_battery

    def _reserve_ok(ip: str) -> bool:
        if plan.get("over_max"):
            return True
        ok, why = device_allowed_on_battery(state, ip)
        if not ok:
            messages.append(f"Füll-Resume {ip} unterdrückt – {why}")
        return ok

    # Anker zuerst, wenn Füllkurve Last will und Anker aus ist
    want_load = bool(
        (plan.get("flex") or {}).get("should_start_now")
        or plan.get("flex_stack")
        or float(plan.get("excess_charge_kw") or 0) >= 1.0
    )
    if want_load and devices:
        anchor = sort_for_resume(devices)[0]
        anchor_ip = str(anchor.get("ip") or "")
        if (
            anchor_ip
            and str(miner_status.get(anchor_ip) or "").lower() != "running"
            and _reserve_ok(anchor_ip)
        ):
            pwr = _f(anchor.get("power_kw"))
            actions.append(
                {
                    "ip": anchor_ip,
                    "action": "resume",
                    "reason": (
                        "Flache Füllkurve: Anker (höchste Prio) zuerst – "
                        f"Soll {plan.get('desired_charge_kw')} kW, "
                        f"Ist {plan.get('charge_kw')} kW"
                    ),
                    "force": False,
                    "urgent": True,
                    "source": "fill_schedule",
                    "fill_urgency": fill_urgency,
                }
            )
            miner_status[anchor_ip] = "running"
            running_load += pwr
            messages.append(
                f"Füll-Plan (flach): Anker {anchor_ip} zuerst ein "
                f"(niedrigere Prio wartet)."
            )
            return actions, messages, running_load

    stack = list(plan.get("flex_stack") or [])
    flex = plan.get("flex") or {}
    if not stack and flex.get("should_start_now") and flex.get("ip"):
        stack = [
            {
                "ip": flex.get("ip"),
                "power_kw": flex.get("power_kw"),
                "reason": flex.get("reason"),
            }
        ]
    if not stack:
        if plan.get("capacity_limited"):
            messages.append(
                "Füll-Plan: keine weitere steuerbare Last – Speicher am/über Max, "
                f"Ist-Ladung ~{plan.get('charge_kw')} kW (Kapazitätsgrenze)."
            )
        return actions, messages, running_load

    for item in stack:
        ip = str(item.get("ip") or "")
        if not ip:
            continue
        if str(miner_status.get(ip) or "").lower() == "running":
            continue
        if any(a.get("ip") == ip for a in actions):
            continue
        if not _reserve_ok(ip):
            continue
        # Simulieren: würde Resume eine Prioritätsverletzung erzeugen?
        sim = dict(miner_status)
        sim[ip] = "running"
        if find_priority_violations(devices, sim):
            # Höhere Stufe fehlt → Anker nachziehen statt Flex zu starten
            missing = [
                d
                for d in sort_for_resume(devices)
                if str(miner_status.get(d.get("ip")) or "").lower() != "running"
            ]
            if missing:
                hi = missing[0]
                hip = str(hi.get("ip") or "")
                if hip and not any(a.get("ip") == hip for a in actions) and _reserve_ok(hip):
                    pwr_hi = _f(hi.get("power_kw"))
                    actions.append(
                        {
                            "ip": hip,
                            "action": "resume",
                            "reason": (
                                "Füll-Plan: zuerst höhere Priorität "
                                f"{hip} – {ip} wartet"
                            ),
                            "force": False,
                            "urgent": True,
                            "source": "fill_schedule",
                            "fill_urgency": fill_urgency,
                        }
                    )
                    miner_status[hip] = "running"
                    running_load += pwr_hi
                    messages.append(
                        f"Füll-Plan: {ip} nicht ein – zuerst Anker/höhere Prio {hip}."
                    )
            else:
                messages.append(
                    f"Füll-Plan: {ip} übersprungen – höhere Priorität noch aus."
                )
            continue
        pwr = _f(item.get("power_kw"))
        actions.append(
            {
                "ip": ip,
                "action": "resume",
                "reason": item.get("reason")
                or flex.get("reason")
                or "Flache Füllkurve: Flex gegen Max-SoC",
                "force": False,
                "urgent": True,
                "source": "fill_schedule",
                "fill_urgency": fill_urgency,
            }
        )
        miner_status[ip] = "running"
        running_load += pwr
        messages.append(
            f"Füll-Plan (flach): {ip} ein – Soll {plan.get('desired_charge_kw')} kW, "
            f"Ist {plan.get('charge_kw')} kW, Überlauf {flex.get('overflow_kwh')} kWh "
            f"bis PV-Ende {plan.get('pv_end_display')}."
        )
    return actions, messages, running_load
