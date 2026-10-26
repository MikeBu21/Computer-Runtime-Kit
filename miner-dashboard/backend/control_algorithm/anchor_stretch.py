# -*- coding: utf-8 -*-
"""
Abend-/Nachmittag-Anker-Streckung: Flex (Prio 2) aus, Hydro (Anker) möglichst lange.

Ziel: SoC nicht unter Min (28 %) – abzüglich Grundverbrauch
  • heute bis Mitternacht
  • morgen früh bis PV-Start
und nur wenn die morgige PV/Bilanz das zulässt.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.planner import enabled_devices, sort_for_resume


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _base_kw(config: Dict[str, Any], state: Dict[str, Any]) -> Tuple[float, str]:
    """Durchschnittlicher Grundverbrauch (ohne Miner) in kW."""
    reserve = {}
    try:
        from control_algorithm.weather_survival import _overnight_reserve_calc

        reserve = _overnight_reserve_calc(config, state) or {}
    except Exception:
        reserve = {}
    daily = reserve.get("base_consumption_daily_kwh")
    if daily and float(daily) > 0:
        return float(daily) / 24.0, str(reserve.get("base_consumption_source") or "Wochenplan")
    # Fallback ~2 kW Hausgrundlast
    return 2.0, "Fallback 2 kW"


def _tomorrow_pv_ok(config: Dict[str, Any], state: Dict[str, Any]) -> Tuple[bool, float, float]:
    """Morgen-PV reicht für Entladen Richtung Min+Reserve."""
    tomorrow = _f(state.get("pv_yield_tomorrow_kwh"))
    try:
        from control_algorithm.reference_anchor import effective_perfect_day_kwh

        perfect = float(effective_perfect_day_kwh(state, config) or 0)
    except Exception:
        perfect = _f(config.get("pv_kwp")) * _f(config.get("perfect_weather_sun_hours"), 6.0)
    # „Budget morgen reicht“: klar über Normalwetter-Schwelle
    ok = tomorrow >= max(35.0, perfect * 0.45)
    return ok, tomorrow, perfect


def _pv_start_tomorrow(state: Dict[str, Any], now: datetime) -> Tuple[datetime, str]:
    try:
        from control_algorithm.late_start import next_pv_start_deadline

        dt, note = next_pv_start_deadline(state, now)
        if dt is not None:
            # Wenn Deadline noch heute ist, nimm morgen früh als Reserve-Ende
            if dt.date() == now.date():
                from control_algorithm.late_start import first_pv_hour_from_hours, _hours_for_date

                tm = now.date() + timedelta(days=1)
                h = first_pv_hour_from_hours(_hours_for_date(state, tm)) or 7
                dt = datetime.combine(tm, datetime.min.time()).replace(hour=h, minute=0)
                note = f"PV-Start morgen ~{h:02d}:00"
            return dt, note
    except Exception:
        pass
    tm = now.date() + timedelta(days=1)
    dt = datetime.combine(tm, datetime.min.time()).replace(hour=7, minute=0)
    return dt, "PV-Start morgen ~07:00 (Fallback)"


def compute_anchor_stretch_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Wenn aktiv: nur Anker (Hydro) laufen lassen, Flex/Spill pausieren.

    available_kwh = (SoC − min_soc)/100 × Speicher
                    − Grundlast×(jetzt→Mitternacht)
                    − Grundlast×(Mitternacht→PV-Start morgen)
    """
    now = now or datetime.now()
    devices = enabled_devices(config)
    ordered = sort_for_resume(devices)
    empty = {
        "ok": False,
        "active": False,
        "reason": "no_devices",
    }
    if not ordered:
        return empty

    anchor = ordered[0]
    anchor_ip = str(anchor.get("ip") or "")
    anchor_kw = _f(anchor.get("power_kw"))
    flex_ips = [str(d.get("ip") or "") for d in ordered[1:] if d.get("ip")]

    soc = live.get("soc_percent")
    if soc is None:
        soc = state.get("live_soc")
    soc = _f(soc, default=-1.0)
    storage = _f(config.get("storage_kwh"))
    min_soc = _f(config.get("min_soc_percent"), 28.0)
    max_soc = _f(config.get("max_soc_percent"), 97.0)
    if storage <= 0 or soc < 0 or anchor_kw <= 0.1:
        return {**empty, "reason": "no_soc_or_anchor"}

    pv_kw = _f(live.get("pv_kw"), _f(state.get("live_pv_kw")))
    try:
        from control_algorithm.weather_survival import live_grid_export_kw
        from control_algorithm.steering_learning import export_threshold_kw

        export_kw = float(live_grid_export_kw(live) or 0)
        thr = float(export_threshold_kw(state) or 0.15)
    except Exception:
        export_kw = 0.0
        thr = 0.15

    # Bei klarem Export / starkem Surplus weiter stapeln – nicht strecken
    if export_kw >= max(thr, 0.25) and soc >= max_soc - 3:
        return {
            "ok": True,
            "active": False,
            "reason": "export_needs_flex",
            "export_kw": round(export_kw, 2),
            "anchor_ip": anchor_ip,
            "anchor_kw": anchor_kw,
        }

    # Steile Füllkurve / drohendes Max-SoC: Flex hat Vorrang vor Anker-Streckung
    # (sonst bleibt Luftgekühlt aus, obwohl der Plan „sofort“ sagt und der Speicher
    # mit mehreren kW Richtung Max läuft).
    fill = state.get("fill_schedule") or {}
    flex = fill.get("flex") or {}
    excess_charge = _f(fill.get("excess_charge_kw"))
    room_kwh = fill.get("room_to_max_kwh")
    fill_wants_flex = bool(
        fill.get("ok")
        and (
            flex.get("should_start_now")
            or fill.get("flex_stack")
            or fill.get("capacity_limited")
            or excess_charge >= 1.5
        )
    )
    if fill_wants_flex:
        return {
            "ok": True,
            "active": False,
            "reason": "fill_needs_flex",
            "soc_percent": round(soc, 1),
            "excess_charge_kw": round(excess_charge, 2),
            "room_to_max_kwh": room_kwh,
            "flex_ip": flex.get("ip"),
            "anchor_ip": anchor_ip,
            "anchor_kw": anchor_kw,
            "flex_ips": flex_ips,
        }

    # Noch am Max und lädt → Füll/Export hat Vorrang
    batt = live.get("battery_power_kw")
    if batt is None:
        batt = state.get("live_battery_kw")
    charge_kw = max(0.0, _f(batt)) if batt is not None else 0.0
    if soc >= max_soc - 0.2 and charge_kw > 0.4:
        return {
            "ok": True,
            "active": False,
            "reason": "still_charging_at_max",
            "soc_percent": round(soc, 1),
            "charge_kw": round(charge_kw, 2),
            "anchor_ip": anchor_ip,
            "anchor_kw": anchor_kw,
        }

    tomorrow_ok, tomorrow_pv, perfect = _tomorrow_pv_ok(config, state)
    if not tomorrow_ok:
        return {
            "ok": True,
            "active": False,
            "reason": "tomorrow_budget_low",
            "tomorrow_pv_kwh": round(tomorrow_pv, 1),
            "perfect_day_kwh": round(perfect, 1),
            "anchor_ip": anchor_ip,
            "anchor_kw": anchor_kw,
        }

    base_kw, base_src = _base_kw(config, state)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    hours_to_midnight = max(0.0, (midnight - now).total_seconds() / 3600.0)
    pv_start, pv_note = _pv_start_tomorrow(state, now)
    hours_midnight_to_pv = max(0.0, (pv_start - midnight).total_seconds() / 3600.0)

    reserve_today_kwh = base_kw * hours_to_midnight
    reserve_morning_kwh = base_kw * hours_midnight_to_pv
    reserve_kwh = reserve_today_kwh + reserve_morning_kwh

    headroom_kwh = max(0.0, (soc - min_soc) / 100.0 * storage)
    available_kwh = max(0.0, headroom_kwh - reserve_kwh)
    hydro_hours = available_kwh / anchor_kw if anchor_kw > 0 else 0.0

    # Aktiv wenn Hydro noch sinnvoll laufen kann (≥ ~45 min) und SoC nicht knapp
    active = (
        available_kwh >= anchor_kw * 0.75
        and soc >= min_soc + 8.0
        and hydro_hours >= 0.75
    )

    # Laufreserve schlägt die Reserve-Bilanz: die mischt Headroom-Drain und
    # Nachtreserve in eine Zahl und ist damit die Ursache des Abend-Nachlaufens.
    drain_plan = state.get("morning_drain_plan") or {}
    if active and drain_plan.get("gate_active"):
        if anchor_ip and str(anchor_ip) not in set(drain_plan.get("allowed_ips") or []):
            active = False
            run_reserve_block = True
        else:
            run_reserve_block = False
    else:
        run_reserve_block = False

    # Abend-Riegel: einmal Reserve erreicht → kein erneutes Hydro bis PV-Start
    # (sonst wird Streckung gegen Mitternacht wieder „aktiv“, weil weniger Stunden Reserve)
    hold_until_raw = state.get("anchor_stretch_hold_until")
    hold_until: Optional[datetime] = None
    if hold_until_raw:
        try:
            hold_until = datetime.fromisoformat(str(hold_until_raw))
        except ValueError:
            hold_until = None
    if hold_until and now >= hold_until:
        state.pop("anchor_stretch_hold_until", None)
        hold_until = None

    evening_hold = bool(hold_until and now < hold_until)
    if evening_hold:
        active = False

    if (
        (not active)
        and available_kwh < anchor_kw * 0.75
        and tomorrow_ok
        and soc >= min_soc + 20
        and now.hour >= 14
    ):
        # Nur ab Nachmittag nach echter Streckung – nicht morgens bei niedrigem SoC
        if not state.get("anchor_stretch_hold_until"):
            state["anchor_stretch_hold_until"] = pv_start.isoformat()
        evening_hold = True
        if not hold_until:
            try:
                hold_until = datetime.fromisoformat(
                    str(state.get("anchor_stretch_hold_until"))
                )
            except Exception:
                hold_until = pv_start
        active = False

    # Ziel-Budget = nur Anker (Flex soll aus)
    target_budget_kw = anchor_kw if active else None

    if evening_hold:
        reason = "evening_hold"
    elif active:
        reason = "stretch_anchor"
    elif run_reserve_block:
        reason = "run_reserve"
    elif available_kwh < anchor_kw * 0.75:
        reason = "insufficient_headroom"
    else:
        reason = "short_runtime"

    # Abschalt-SoC (Dashboard): wann Hydro planmäßig aus
    # active endet wenn available < 0.75 × Anker → SoC-Schwelle
    min_avail_kwh = anchor_kw * 0.75
    soc_hydro_off = min_soc + (min_avail_kwh + reserve_kwh) / storage * 100.0
    soc_reserve_floor = min_soc + reserve_kwh / storage * 100.0
    soc_hydro_off = max(min_soc, min(max_soc, soc_hydro_off))
    soc_reserve_floor = max(min_soc, min(max_soc, soc_reserve_floor))

    return {
        "ok": True,
        "active": active,
        "reason": reason,
        "evening_hold": evening_hold,
        "hold_until": hold_until.isoformat() if hold_until else state.get("anchor_stretch_hold_until"),
        "anchor_ip": anchor_ip,
        "anchor_kw": round(anchor_kw, 2),
        "flex_ips": flex_ips,
        "soc_percent": round(soc, 1),
        "min_soc_percent": min_soc,
        "storage_kwh": storage,
        "headroom_kwh": round(headroom_kwh, 2),
        "base_kw": round(base_kw, 2),
        "base_source": base_src,
        "hours_to_midnight": round(hours_to_midnight, 2),
        "hours_midnight_to_pv": round(hours_midnight_to_pv, 2),
        "pv_start": pv_start.isoformat(),
        "pv_start_note": pv_note,
        "reserve_today_kwh": round(reserve_today_kwh, 2),
        "reserve_morning_kwh": round(reserve_morning_kwh, 2),
        "reserve_kwh": round(reserve_kwh, 2),
        "available_for_anchor_kwh": round(available_kwh, 2),
        "anchor_hours": round(hydro_hours, 2),
        "target_budget_kw": target_budget_kw,
        "tomorrow_pv_kwh": round(tomorrow_pv, 1),
        "tomorrow_ok": tomorrow_ok,
        # Für Dashboard: geplante Aus-Schwellen in %
        "soc_hydro_off_percent": round(soc_hydro_off, 1),
        "soc_reserve_floor_percent": round(soc_reserve_floor, 1),
        "soc_min_hard_percent": min_soc,
        "flex_shed_mode": "gestaffelt_1_pro_tick" if active else "aus",
        "formula": (
            f"verfügbar=({soc:.0f}−{min_soc:.0f})/100×{storage:.0f}−"
            f"Grundlast {base_kw:.1f} kW×({hours_to_midnight:.1f} h heute+"
            f"{hours_midnight_to_pv:.1f} h bis PV)="
            f"{available_kwh:.1f} kWh → Anker ~{hydro_hours:.1f} h @ {anchor_kw:.1f} kW; "
            f"Hydro-Aus ca. {soc_hydro_off:.0f} %"
        ),
    }


def apply_anchor_stretch_to_budget(
    budget_kw: float,
    stretch: Dict[str, Any],
) -> float:
    """
    Im Streck-Modus: Budget = Ankerleistung (nicht darunter kappen).
    Sonst würde z. B. max_soc-Budget 0.9 kW Hydro sofort wieder abwerfen.
    """
    if not stretch.get("active"):
        return budget_kw
    target = stretch.get("target_budget_kw")
    if target is None:
        return budget_kw
    return float(target)


def stretch_pause_actions(
    stretch: Dict[str, Any],
    miner_status: Dict[str, str],
    devices: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str], float]:
    """
    Flex/Spill gestaffelt pausieren (niedrigste Prio zuerst, max. 1 Gerät/Tick),
    damit der Anker (höchste Prio) am längsten läuft.
    """
    from control_algorithm.planner import sort_for_pause

    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    shed = 0.0
    if not stretch.get("active"):
        return actions, messages, shed

    flex_set = {str(x) for x in (stretch.get("flex_ips") or [])}
    running_flex = [
        d
        for d in devices
        if str(d.get("ip") or "") in flex_set
        and str(miner_status.get(d.get("ip")) or "").lower() == "running"
    ]
    if not running_flex:
        return actions, messages, shed

    # Nur die niedrigste Priorität in diesem Tick – nächste Stufe folgt im Folgetick.
    # Soft-Aus/Tag: Geräte unter Empfehlung bevorzugen (keine harte Sperre).
    ordered = sort_for_pause(running_flex)
    try:
        from control_algorithm.switch_commitment import (
            normalize_max_off_per_day,
            off_count_today,
            runtime_entry,
        )

        max_off = normalize_max_off_per_day(
            (stretch.get("_config") or {}).get("max_device_off_per_day", 3)
        )
        st = stretch.get("_state") if isinstance(stretch.get("_state"), dict) else {}
        under = []
        for d in ordered:
            ip = str(d.get("ip") or "")
            cnt = off_count_today(runtime_entry(st, ip)) if st else 0
            if max_off <= 0 or cnt < max_off:
                under.append(d)
        candidate = (under or ordered)[0]
    except Exception:
        candidate = ordered[0]
    ip = str(candidate.get("ip") or "")
    pwr = _f(candidate.get("power_kw"))
    still_running = len(running_flex) - 1
    actions.append(
        {
            "ip": ip,
            "action": "pause",
            "reason": (
                f"Anker-Streckung gestaffelt: niedrigste Prio aus "
                f"({still_running} Flex-Stufen bleiben) – Hydro möglichst lange "
                f"(~{stretch.get('anchor_hours')} h, Reserve "
                f"{stretch.get('reserve_kwh')} kWh bis {stretch.get('pv_start_note')}, "
                f"Min {stretch.get('min_soc_percent')} %)."
            ),
            "force": True,
            "budget_shed": True,
        }
    )
    miner_status[ip] = "paused"
    shed = pwr
    messages.append(
        f"Anker-Streckung: gestaffelt aus – zuerst {ip} "
        f"({pwr:.1f} kW). Noch {still_running} niedrigere Stufe(n); "
        f"Anker {stretch.get('anchor_ip')} @ {stretch.get('anchor_kw')} kW "
        f"läuft weiter (~{stretch.get('anchor_hours')} h)."
    )
    return actions, messages, shed


def stretch_exhausted_pause_anchor(
    stretch: Dict[str, Any],
    miner_status: Dict[str, str],
    devices: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Wenn Reserve-Grenze erreicht (Streckung nicht mehr aktiv wegen Headroom):
    Anker (Hydro) hart pausieren – sonst hält no_budget_pause ihn weiter an.

    Nur ab Nachmittag/Abend – morgens bei niedrigem SoC + PV-Ladung
    darf Hydro für die flache Ladekurve anlaufen.
    """
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    now = now or datetime.now()
    if stretch.get("active"):
        return actions, messages
    if stretch.get("reason") not in (
        "insufficient_headroom",
        "short_runtime",
        "evening_hold",
        "run_reserve",
    ):
        return actions, messages
    # Morgens / Vormittag: keine „Reserve erreicht“-Pause (wird sonst Hydro
    # gegen die Ladekurve ausgeschaltet, obwohl PV den Speicher füllt).
    if stretch.get("reason") != "evening_hold" and now.hour < 14:
        return actions, messages
    if float(stretch.get("soc_percent") or 0) < float(stretch.get("min_soc_percent") or 28) + 20:
        if stretch.get("reason") != "evening_hold":
            return actions, messages
    # Nur wenn Morgen-PV ok war / Reserve-Rechnung vorliegt (nicht bei Export-Modus-Absprung)
    if stretch.get("tomorrow_ok") is False:
        return actions, messages
    if stretch.get("available_for_anchor_kwh") is None:
        return actions, messages

    anchor_ip = str(stretch.get("anchor_ip") or "")
    if not anchor_ip:
        return actions, messages
    if str(miner_status.get(anchor_ip) or "").lower() != "running":
        return actions, messages

    by_ip = {str(d.get("ip") or ""): d for d in devices}
    d = by_ip.get(anchor_ip) or {}
    soc = stretch.get("soc_percent")
    actions.append(
        {
            "ip": anchor_ip,
            "action": "pause",
            "reason": (
                f"Anker-Reserve erreicht: SoC {soc} % – Rest "
                f"{stretch.get('available_for_anchor_kwh')} kWh < "
                f"0,75×{stretch.get('anchor_kw')} kW; "
                f"Grundlast-Reserve {stretch.get('reserve_kwh')} kWh "
                f"bis {stretch.get('pv_start_note')} (Min {stretch.get('min_soc_percent')} %)."
            ),
            "force": True,
        }
    )
    miner_status[anchor_ip] = "paused"
    messages.append(
        f"Anker aus (Reserve): SoC {soc} % – Hydro gestoppt, "
        f"damit bis PV-Start ~{stretch.get('min_soc_percent')} % + Grundlast gehalten werden."
    )
    return actions, messages
