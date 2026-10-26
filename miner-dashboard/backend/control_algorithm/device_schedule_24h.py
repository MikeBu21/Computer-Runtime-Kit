# -*- coding: utf-8 -*-
"""
24h-Gerätevorausschau aus 7-Tage-Stundenplan + aktivem Entladeplan.

Pro Stunde: miner_kwh_planned (kW-Budget) → Geräte nach Priorität zuordnen.
Ergebnis: Ziel-Ein-/Ausschaltzeiten, Budget je Gerät, Δ zum vorherigen Plan.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from control_algorithm.config import format_device_priority, load_miners, normalize_device_priority
from control_algorithm.load_scheduler import (
    DISCHARGE_HOLD_DEADBAND_PCT,
    _discharge_plan,
    _hours_until_morning_target,
    _safe_discharge_budget_kw,
    normalize_min_runtime_hours,
)
from control_algorithm.planner import enabled_devices, sort_for_resume


def _fmt_hm(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m. %H:%M")


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", ""))
    except Exception:
        return None


def _pct_dev(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if old is None or new is None:
        return None
    if abs(float(old)) < 1e-6:
        return None if abs(float(new)) < 1e-6 else 100.0
    return round((float(new) - float(old)) / abs(float(old)) * 100.0, 1)


def _consolidate_windows(
    windows: List[Tuple[datetime, datetime]],
    max_sessions: int,
    *,
    gap_merge_hours: float = 2.0,
    session_bias: float = 0.0,
) -> List[Tuple[datetime, datetime]]:
    """Reduziert Schaltfenster auf max_sessions (längere Blöcke bevorzugen).

    1) Lücken ≤ gap_merge_hours (skaliert mit session_bias) zusammenziehen
    2) Wenn immer noch zu viele: kürzeste Fenster streichen
    """
    if not windows:
        return []
    max_sessions = max(1, int(max_sessions))
    bias = max(0.0, min(1.0, float(session_bias or 0)))
    merge_h = float(gap_merge_hours) * (1.0 + bias)  # bis 2× bei Bias 1
    ordered = sorted(windows, key=lambda w: w[0])
    merged: List[Tuple[datetime, datetime]] = [ordered[0]]
    for a, b in ordered[1:]:
        prev_a, prev_b = merged[-1]
        gap_h = (a - prev_b).total_seconds() / 3600.0
        if gap_h <= merge_h:
            merged[-1] = (prev_a, max(prev_b, b))
        else:
            merged.append((a, b))
    if len(merged) <= max_sessions:
        return merged
    # Längste Fenster behalten
    ranked = sorted(
        range(len(merged)),
        key=lambda i: (merged[i][1] - merged[i][0]).total_seconds(),
        reverse=True,
    )
    keep = set(ranked[:max_sessions])
    return [merged[i] for i in range(len(merged)) if i in keep]


def _min_dev(old_iso: Any, new_dt: Optional[datetime]) -> Optional[float]:
    old = _parse_iso(old_iso)
    if old is None or new_dt is None:
        return None
    return round((new_dt - old).total_seconds() / 60.0, 1)


def _schedule_fingerprint(schedule: Optional[Dict[str, Any]]) -> Tuple:
    if not schedule:
        return ()
    rows = []
    for d in schedule.get("devices") or []:
        rows.append(
            (
                str(d.get("ip") or ""),
                round(float(d.get("budget_kwh") or 0), 1),
                round(float(d.get("planned_hours") or 0), 2),
                str(d.get("on_at") or "")[:16],
                str(d.get("off_at") or "")[:16],
                bool(d.get("in_plan")),
            )
        )
    return (
        round(float(schedule.get("safe_budget_kw") or 0), 1),
        str(schedule.get("discharge_phase") or ""),
        tuple(rows),
    )


def persist_device_schedule_24h(
    state: Dict[str, Any],
    schedule: Dict[str, Any],
) -> None:
    current = state.get("device_schedule_24h")
    if current and _schedule_fingerprint(current) != _schedule_fingerprint(schedule):
        state["device_schedule_24h_prev"] = current
    state["device_schedule_24h"] = schedule
    state["device_schedule_24h_updated_at"] = schedule.get("generated_at")
    # Für Laststeuerung: Soll-Geräte dieser Stunde
    state["device_hour_target_ips"] = list(schedule.get("current_target_ips") or [])


def _flatten_hours_24(
    week_plan: Dict[str, Any],
    now: datetime,
) -> List[Dict[str, Any]]:
    """Nächste 24 Stunden aus dem 7-Tage-Plan (ab aktueller Stunde)."""
    days = week_plan.get("days") or []
    by_date = {str(d.get("day_date")): d for d in days if d.get("day_date")}
    out: List[Dict[str, Any]] = []
    start = now.replace(minute=0, second=0, microsecond=0)
    for i in range(24):
        slot = start + timedelta(hours=i)
        day = by_date.get(slot.date().isoformat())
        if not day:
            out.append(
                {
                    "start": slot,
                    "end": slot + timedelta(hours=1),
                    "hour": slot.hour,
                    "day_date": slot.date().isoformat(),
                    "pv_kwh": 0.0,
                    "miner_budget_kw": 0.0,
                    "soc_projected_pct": None,
                    "steering_mode": "normal",
                    "source": "missing",
                }
            )
            continue
        hour_row = None
        for h in day.get("hours") or []:
            if int(h.get("hour") or -1) == slot.hour:
                hour_row = h
                break
        miner_kw = float((hour_row or {}).get("miner_kwh_planned") or 0)
        out.append(
            {
                "start": slot,
                "end": slot + timedelta(hours=1),
                "hour": slot.hour,
                "day_date": slot.date().isoformat(),
                "pv_kwh": float((hour_row or {}).get("pv_kwh") or 0),
                "miner_budget_kw": miner_kw,
                "soc_projected_pct": (hour_row or {}).get("soc_projected_pct"),
                "steering_mode": day.get("steering_mode") or "normal",
                "source": "week_plan",
            }
        )
    return out


def _allocate_ips(
    budget_kw: float,
    devices_sorted: List[Dict[str, Any]],
    *,
    state: Optional[Dict[str, Any]] = None,
    live_export_kw: float = 0.0,
) -> Tuple[List[str], float]:
    """Geräte nach Priorität in Stundenbudget einfüllen (wie Wochenplan-Annahme)."""
    if state is not None:
        try:
            from control_algorithm.peak_balance_learning import (
                allocate_ips_with_conserve,
                anchor_conserve_bias,
            )
            from control_algorithm.steering_learning import export_threshold_kw

            return allocate_ips_with_conserve(
                budget_kw,
                devices_sorted,
                anchor_conserve=anchor_conserve_bias(state),
                live_export_kw=live_export_kw,
                export_thr=export_threshold_kw(state),
            )
        except Exception:
            pass
    chosen: List[str] = []
    used = 0.0
    if budget_kw < 0.3:
        return chosen, used
    for d in devices_sorted:
        ip = str(d.get("ip") or "")
        pwr = float(d.get("power_kw") or 0)
        if not ip or pwr <= 0:
            continue
        if used + pwr <= budget_kw + 0.2:
            chosen.append(ip)
            used += pwr
            continue
        # Erstes Gerät: auch wenn Budget knapp unter power, wenn ≥ 85 %
        if not chosen and pwr >= 0.5 and budget_kw >= pwr * 0.85:
            chosen.append(ip)
            used += pwr
            break
    return chosen, used


def _discharge_overlay(
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    now: datetime,
) -> Tuple[float, Set[str], Optional[datetime], str, float, str]:
    """
    Aktives Entladen: sicheres kW, Pflicht-IPs, geschätztes Aus-Ende.
    Returns: safe_kw, force_ips, force_until, phase, excess_kwh, reason
    """
    projection = (
        state.get("horizon_projection")
        or state.get("balance_projection")
        or {}
    )
    soc = live.get("soc_percent")
    storage = float(config.get("storage_kwh") or 0)
    checkpoint = float(projection.get("target_soc") or config.get("min_soc_percent") or 40)
    hold_until = checkpoint + DISCHARGE_HOLD_DEADBAND_PCT
    budget_kw = float(projection.get("miner_budget_kw") or 0)
    nt = projection.get("near_term_plan") or {}
    sustained = None
    try:
        if nt.get("sustained_budget_kw") is not None:
            sustained = float(nt["sustained_budget_kw"])
    except (TypeError, ValueError):
        sustained = None
    pre_cap = state.get("overnight_priority_cap_kw")
    try:
        pre_cap_f = float(pre_cap) if pre_cap is not None else None
    except (TypeError, ValueError):
        pre_cap_f = None

    hours_to_morning = _hours_until_morning_target(config, now)
    safe_kw, excess_kwh, safe_reason = _safe_discharge_budget_kw(
        budget_kw=budget_kw,
        sustained_kw=sustained,
        overnight_cap_kw=pre_cap_f,
        projection=projection,
        checkpoint_soc=checkpoint,
        live_soc=float(soc) if soc is not None else None,
        hold_until_soc=hold_until,
        storage_kwh=storage,
        hours_to_target=hours_to_morning,
        pv_kw=float(live.get("pv_kw") or 0),
    )

    plan = _discharge_plan(state)
    phase = str(plan.get("phase") or "idle")
    force_ips: Set[str] = set()
    force_until: Optional[datetime] = None
    if phase in ("ramp", "hold") and plan.get("ips"):
        force_ips = {str(ip) for ip in plan["ips"] if ip}
        burned = float(plan.get("burned_kwh") or 0)
        target = float(plan.get("target_burn_kwh") or 0)
        target_kw = float(plan.get("target_kw") or safe_kw or 1.0)
        remain = max(0.0, target - burned)
        hours_left = remain / max(target_kw, 0.3) if remain > 0 else 0.0
        # Nur verbleibende Brenn-Energie – nicht die volle planned_hours ab Start
        force_until = now + timedelta(hours=min(18.0, max(0.25, hours_left)))
        for s in plan.get("devices") or []:
            ip = str(s.get("ip") or "")
            if ip:
                force_ips.add(ip)
    elif phase == "wind_down" and plan.get("ips"):
        force_ips = set()
        force_until = now

    return safe_kw, force_ips, force_until, phase, float(excess_kwh), safe_reason


def build_device_schedule_24h(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    horizon_end = now + timedelta(hours=24)
    devices = enabled_devices(config)
    devices_sorted = sort_for_resume(devices)
    notes = {
        m.get("ip"): m.get("note") or m.get("model") or m.get("ip")
        for m in load_miners()
        if m.get("ip")
    }
    miner_status = live.get("miner_status") or {}
    power_by_ip = {
        str(d.get("ip")): float(d.get("power_kw") or 0)
        for d in devices
        if d.get("ip")
    }

    week_plan = state.get("energy_week_plan") or {}
    hour_slots = _flatten_hours_24(week_plan, now)
    # Aktuelle Stunde: Live-PV entscheidet Nacht vs. Tag (Stundenprognose kann noch PV-Rest haben)
    live_pv = float(live.get("pv_kw") or 0)
    live_export_kw = 0.0
    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        live_export_kw = float(live_grid_export_kw(live) or 0)
    except Exception:
        live_export_kw = max(0.0, float(live.get("export_kw") or live.get("grid_export_kw") or 0))
    if hour_slots and live_pv < 0.5:
        hour_slots[0] = dict(hour_slots[0])
        hour_slots[0]["pv_kwh"] = min(float(hour_slots[0].get("pv_kwh") or 0), 0.0)
    safe_kw, force_ips, force_until, phase, excess_kwh, safe_reason = _discharge_overlay(
        state, config, live, now
    )
    projection = (
        state.get("horizon_projection")
        or state.get("balance_projection")
        or {}
    )
    checkpoint = float(projection.get("target_soc") or config.get("min_soc_percent") or 40)
    hold_until = checkpoint + DISCHARGE_HOLD_DEADBAND_PCT

    # Pro Stunde: Soll-IPs
    slots_out: List[Dict[str, Any]] = []
    on_hours: Dict[str, List[datetime]] = {str(d.get("ip")): [] for d in devices if d.get("ip")}

    for slot in hour_slots:
        week_budget = float(slot["miner_budget_kw"] or 0)
        pv_h = float(slot["pv_kwh"] or 0)
        hour_w = 1.0
        try:
            from control_algorithm.steering_learning import hour_day_weight

            hour_w = hour_day_weight(state, int(slot.get("hour") or slot["start"].hour))
        except Exception:
            hour_w = 1.0
        # Wie Laststeuerung: nachts / ohne PV nur sicheres Budget, tagsüber Wochenplan
        if pv_h < 0.5:
            budget = min(week_budget, max(safe_kw, 0.0)) if safe_kw > 0 else min(week_budget, 0.0)
            if week_budget > 0 and safe_kw > 0:
                budget = min(week_budget, safe_kw)
            else:
                budget = 0.0 if safe_kw < 0.3 else min(week_budget, safe_kw)
            # Tag-first: Nacht-Budget mit Tageszeit-Gewicht dämpfen (außer Entlade-Overlay)
            budget = budget * hour_w
        else:
            budget = week_budget * max(0.85, hour_w)

        # Entlade-Overlay: Pflicht-Geräte bis force_until (nur wenn Stunde noch in Entlade-Fenster)
        overlay = False
        if force_ips and force_until and slot["start"] < force_until and pv_h < 5.0:
            overlay = True
            force_kw = sum(power_by_ip.get(ip, 0.0) for ip in force_ips)
            budget = max(budget, min(force_kw, max(safe_kw, force_kw)))

        chosen, used = _allocate_ips(
            budget,
            devices_sorted,
            state=state,
            live_export_kw=live_export_kw if slot is hour_slots[0] else 0.0,
        )
        if overlay:
            merged: List[str] = []
            used = 0.0
            for ip in list(force_ips) + [x for x in chosen if x not in force_ips]:
                pwr = power_by_ip.get(ip, 0.0)
                if ip in force_ips:
                    if used + pwr <= max(budget, force_kw if force_ips else budget) + 0.2 or ip in force_ips:
                        if ip not in merged:
                            merged.append(ip)
                            used += pwr
                elif used + pwr <= budget + 0.2:
                    if ip not in merged:
                        merged.append(ip)
                        used += pwr
            # Nur Pflicht-Geräte im Overlay (kein Aufblasen der Nacht-Cohort)
            chosen = [ip for ip in merged if ip in force_ips] or merged[:1]
            used = sum(power_by_ip.get(ip, 0.0) for ip in chosen)

        for ip in chosen:
            on_hours.setdefault(ip, []).append(slot["start"])

        slots_out.append(
            {
                "time": slot["start"].isoformat(timespec="seconds"),
                "time_display": _fmt_hm(slot["start"]),
                "day_date": slot["day_date"],
                "hour": slot["hour"],
                "pv_kwh": round(pv_h, 2),
                "miner_budget_kw": round(budget, 2),
                "week_budget_kw": round(week_budget, 2),
                "soc_projected_pct": slot.get("soc_projected_pct"),
                "steering_mode": slot.get("steering_mode"),
                "ips": chosen,
                "allocated_kw": round(used, 2),
                "discharge_overlay": overlay,
            }
        )

    current_target_ips: List[str] = []
    if slots_out:
        current_target_ips = list(slots_out[0].get("ips") or [])

    prev_src = state.get("device_schedule_24h_prev") or state.get("device_schedule_24h") or {}
    prev_rows = {
        str(r.get("ip")): r
        for r in (prev_src.get("devices") or [])
        if r.get("ip")
    }
    min_runtime_h = normalize_min_runtime_hours(config.get("min_device_runtime_hours"))
    try:
        from control_algorithm.switch_commitment import normalize_max_on_per_day

        max_on_sessions = normalize_max_on_per_day(
            config.get("max_device_on_per_day", 3)
        ) or 3
    except Exception:
        max_on_sessions = 3
    try:
        from control_algorithm.steering_learning import switch_risk_session_bias

        plan_bias = float(switch_risk_session_bias(state))
    except Exception:
        plan_bias = 0.0

    rows: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []

    for d in devices_sorted:
        ip = str(d.get("ip") or "")
        if not ip:
            continue
        pwr = power_by_ip.get(ip, 0.0)
        running = str(miner_status.get(ip) or "").lower() == "running"
        prio_key = normalize_device_priority(d.get("priority"))
        name = notes.get(ip) or ip
        hours = sorted(on_hours.get(ip) or [])
        in_plan = bool(hours)

        # Fenster aus aufeinanderfolgenden Stunden
        windows: List[Tuple[datetime, datetime]] = []
        if hours:
            w_start = hours[0]
            prev_h = hours[0]
            for h in hours[1:]:
                if (h - prev_h) <= timedelta(hours=1, minutes=5):
                    prev_h = h
                else:
                    windows.append((w_start, prev_h + timedelta(hours=1)))
                    w_start = h
                    prev_h = h
            windows.append((w_start, prev_h + timedelta(hours=1)))

        raw_window_count = len(windows)
        windows = _consolidate_windows(
            windows,
            max_on_sessions,
            gap_merge_hours=2.0,
            session_bias=plan_bias,
        )
        if raw_window_count > len(windows):
            # Stundenliste an konsolidierte Fenster anpassen (Plan-Konsistenz)
            hours = []
            for a, b in windows:
                t = a
                while t < b:
                    hours.append(t)
                    t += timedelta(hours=1)
            on_hours[ip] = hours

        # Nächstes / aktuelles Fenster — bevorzugte Anzeige: längstes künftiges Fenster (≥2 h)
        on_at: Optional[datetime] = None
        off_at: Optional[datetime] = None
        if windows:
            active = None
            for a, b in windows:
                if a <= now < b:
                    active = (a, b)
                    break
            future = [(a, b) for a, b in windows if b > now]
            substantial = [(a, b) for a, b in future if (b - a) >= timedelta(hours=2)]
            if running and active:
                on_at, off_at = active
                rt = (state.get("device_runtime") or {}).get(ip) or {}
                started = _parse_iso(rt.get("resumed_at"))
                if started and started <= now:
                    on_at = started
            elif substantial:
                # Nächstes großes Fenster (typisch Morgen-PV)
                on_at, off_at = substantial[0]
            elif future:
                on_at, off_at = future[0]
            else:
                on_at, off_at = windows[0]

        if off_at and off_at > horizon_end:
            off_at = horizon_end

        planned_hours = float(len(hours))
        budget_kwh = round(pwr * planned_hours, 2)
        budget_assigned_kw = pwr if in_plan else 0.0

        if in_plan and running:
            role = "discharge" if ip in force_ips else "planned"
            note = (
                f"Vorausplanung 7-Tage: {planned_hours:.0f} h @ {pwr:.1f} kW "
                f"({budget_kwh:.1f} kWh) in den nächsten 24 h."
            )
        elif in_plan:
            role = "planned"
            note = (
                f"Ziel-Einschaltung {_fmt_hm(on_at)} → Aus {_fmt_hm(off_at)} "
                f"({planned_hours:.0f} h / {budget_kwh:.1f} kWh) laut Stundenbudget."
            )
        elif running:
            role = "shed"
            on_at = None
            off_at = now
            note = "Läuft, aber in 24h-Vorausplanung nicht eingeplant → Ausschalten."
        else:
            role = "standby"
            note = "Aus – in den nächsten 24 h kein Stundenbudget für dieses Gerät."

        if len(windows) > 1:
            extra = ", ".join(
                f"{_fmt_hm(a)}–{_fmt_hm(b)}" for a, b in windows[1:3]
            )
            note += f" Weitere Fenster: {extra}."
        if raw_window_count > len(windows):
            note += (
                f" Planer: {raw_window_count}→{len(windows)} Sessions "
                f"(max {max_on_sessions} EIN/Tag)."
            )

        off_earliest = off_at
        runtime = (state.get("device_runtime") or {}).get(ip) or {}
        if running and off_at is not None and runtime.get("resumed_at"):
            started = _parse_iso(runtime.get("resumed_at"))
            if started is not None:
                earliest = started + timedelta(hours=min_runtime_h)
                if off_at < earliest and phase != "wind_down":
                    off_earliest = earliest
                    note += (
                        f" Frühestes Aus ohne Force: {_fmt_hm(earliest)} "
                        f"(Mindestlaufzeit {min_runtime_h:g} h)."
                    )

        prev = prev_rows.get(ip) or {}
        on_shift_min = _min_dev(prev.get("on_at"), on_at)
        off_shift_min = _min_dev(prev.get("off_at"), off_at)
        hours_dev_pct = _pct_dev(prev.get("planned_hours"), planned_hours if in_plan else 0.0)
        budget_dev_pct = _pct_dev(
            prev.get("budget_kwh"), budget_kwh if budget_kwh else None
        )

        windows_display = [
            {"on": _fmt_hm(a), "off": _fmt_hm(b)} for a, b in windows
        ]

        row = {
            "ip": ip,
            "name": name,
            "priority": format_device_priority(prio_key),
            "priority_key": prio_key,
            "power_kw": round(pwr, 2),
            "status": "Läuft" if running else "Aus",
            "in_plan": in_plan,
            "role": role,
            "budget_kw": round(budget_assigned_kw, 2),
            "budget_kwh": round(budget_kwh, 2),
            "planned_hours": round(planned_hours, 2),
            "on_at": on_at.isoformat(timespec="seconds") if on_at else None,
            "off_at": off_at.isoformat(timespec="seconds") if off_at else None,
            "on_at_display": _fmt_hm(on_at),
            "off_at_display": _fmt_hm(off_at),
            "off_earliest_display": _fmt_hm(off_earliest) if off_earliest else "—",
            "windows": windows_display,
            "prev_on_at_display": prev.get("on_at_display") or "—",
            "prev_off_at_display": prev.get("off_at_display") or "—",
            "on_shift_min": on_shift_min,
            "off_shift_min": off_shift_min,
            "hours_dev_pct": hours_dev_pct,
            "budget_dev_pct": budget_dev_pct,
            "note": note,
        }
        rows.append(row)

        # Events: Ein am Fensterstart (wenn noch bevorstehend oder jetzt), Aus am Ende
        for a, b in windows:
            if now <= a <= horizon_end:
                events.append(
                    {
                        "time": a.isoformat(timespec="seconds"),
                        "time_display": _fmt_hm(a),
                        "action": "Einschalten",
                        "ip": ip,
                        "name": name,
                        "budget_kw": round(pwr, 2),
                        "budget_kwh": round(budget_kwh, 2),
                    }
                )
            elif a <= now < b and running:
                # schon an – kein Ein-Event, nur Aus
                pass
            elif a <= now < b and not running:
                events.append(
                    {
                        "time": now.isoformat(timespec="seconds"),
                        "time_display": _fmt_hm(now),
                        "action": "Einschalten",
                        "ip": ip,
                        "name": name,
                        "budget_kw": round(pwr, 2),
                        "budget_kwh": round(budget_kwh, 2),
                    }
                )
            if now < b <= horizon_end:
                events.append(
                    {
                        "time": b.isoformat(timespec="seconds"),
                        "time_display": _fmt_hm(b),
                        "action": "Ausschalten",
                        "ip": ip,
                        "name": name,
                        "budget_kw": 0.0,
                        "budget_kwh": round(budget_kwh, 2),
                    }
                )

        if role == "shed" and running:
            events.append(
                {
                    "time": now.isoformat(timespec="seconds"),
                    "time_display": _fmt_hm(now),
                    "action": "Ausschalten",
                    "ip": ip,
                    "name": name,
                    "budget_kw": 0.0,
                    "budget_kwh": 0.0,
                }
            )

    events.sort(key=lambda e: (e.get("time") or "", e.get("action") or ""))

    # PV-Rampenstart: erste künftige Stunde mit spürbarer PV (nicht Rest-Abend wenn Live-Nacht)
    pv_start = None
    for s in hour_slots:
        if s["start"] <= now:
            continue
        if float(s.get("pv_kwh") or 0) < 8.0:
            continue
        if float(s.get("miner_budget_kw") or 0) < 3.0 and live_pv < 0.5:
            # Budget noch nicht gesetzt — trotzdem PV-Start merken ab Stunde mit PV
            pass
        if live_pv < 0.5 and s["start"].date() == now.date() and s["hour"] >= 18:
            continue  # heutigen Abend-Rest ignorieren
        pv_start = s
        break

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "horizon_hours": 24,
        "horizon_end": horizon_end.isoformat(timespec="seconds"),
        "source": "energy_week_plan+discharge",
        "safe_budget_kw": round(safe_kw, 2),
        "excess_kwh": round(excess_kwh, 1),
        "hold_until_soc": round(hold_until, 1),
        "checkpoint_soc": round(checkpoint, 1),
        "safe_reason": safe_reason,
        "discharge_phase": phase,
        "plan_ips": list(force_ips),
        "current_target_ips": current_target_ips,
        "pv_ramp_at": pv_start["start"].isoformat(timespec="seconds") if pv_start else None,
        "pv_ramp_at_display": _fmt_hm(pv_start["start"]) if pv_start else "—",
        "devices": rows,
        "events": events,
        "hour_slots": slots_out,
        "limitations": [
            "Vorausplanung aus 7-Tage-Stundenbudget (miner_kwh_planned) + Priorität.",
            "Miner-kW folgt Netto-Überschuss (PV+Hydro−historischer Grundverbrauch), nicht flacher Gleichlast.",
            "Aktives Entladen überlagert die Nachtstunden bis Ziel-kWh / Hold-SoC.",
            "Ausführung tickweise (~3 min); Mindestlaufzeit kann Aus verzögern.",
            "Ändert sich Wetter/Budget, werden neue Zeiten und Δ % angezeigt.",
        ],
    }
