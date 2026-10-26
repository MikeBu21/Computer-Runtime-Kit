# -*- coding: utf-8 -*-
"""
Anti-Flatter: Schalt-Commitments + harte Tages-Einschaltgrenze.

Nach Pause/Resume gilt eine Mindesthaltezeit (Commitment). Gegensätzliche
Nicht-Notfall-Schaltungen werden verworfen.

Harte Regel: max N Einschaltungen (resume) pro Gerät und Kalendertag.
Danach Sperre bis Tagesende (Mitternacht). Nur echte Notfälle dürfen
die Sperre brechen; blockierte Resumes fließen ins Risiko-Lernneuron.

Soft-Tageswert für Ausschaltungen bleibt Empfehlung (Energiesparen/
Morgen-Floor müssen weiterlaufen können).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Gründe / Flags die Commitments und Tageslimit überstimmen
_EMERGENCY_REASON_MARKERS = (
    "min-soc",
    "min_soc",
    "mindest-soc",
    "mindest soc",
    "minimum soc",
    "notstrom",
    "anomalie",
    "pv-sicherheit",
    "pv_safety",
    "nacht-sperre",
    "nacht_sperre",
    "laufreserve",
    "prioritätsreihenfolge",
    "prioritätsverletzung",
)


def normalize_max_off_per_day(raw: Any) -> int:
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        n = 3
    return max(0, min(48, n))


def normalize_max_on_per_day(raw: Any) -> int:
    """Max. Einschaltungen (resume) pro Gerät und Tag. 0 = aus."""
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        n = 3
    return max(0, min(48, n))


def hard_daily_on_limit_enabled(config: Optional[Dict[str, Any]]) -> bool:
    raw = (config or {}).get("hard_daily_on_limit", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "off", "nein", "no")
    return bool(raw)

def normalize_commitment_off_hours(raw: Any) -> float:
    try:
        h = float(raw)
    except (TypeError, ValueError):
        h = 2.0
    return max(0.25, min(24.0, h))


def normalize_export_override_kw(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 2.0
    return max(0.0, min(50.0, v))


def normalize_fill_override_hours_to_max(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 2.0
    return max(0.25, min(12.0, v))


def normalize_fill_override_excess_kw(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 5.0
    return max(0.5, min(100.0, v))


def normalize_budget_override_kw(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 3.0
    return max(0.5, min(50.0, v))


# EMS-Muster: SoC-Hysterese / Delta für bedingte Freigabe von Energiespar-Off
_SOC_BAND_HYST_PCT = 1.0
_SOC_PAUSE_DELTA_PCT = 8.0


def _resolve_live_soc(
    state: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
    act: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Live-SoC aus Aktion, Live-Dict oder State (für Pause-Kontext / Preemption)."""
    candidates: List[Any] = []
    if act:
        candidates.extend([act.get("soc"), act.get("soc_percent")])
    if live:
        candidates.extend(
            [
                live.get("soc"),
                live.get("soc_percent"),
                live.get("battery_soc"),
            ]
        )
    if state:
        candidates.extend(
            [
                state.get("live_soc"),
                (state.get("curve_plan") or {}).get("soc_percent"),
                (state.get("fill_schedule") or {}).get("soc_percent"),
            ]
        )
    for raw in candidates:
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _planning_max_soc(
    config: Optional[Dict[str, Any]],
    state: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> float:
    """Plan-Decke (Speicher-Band) bzw. Config-Max."""
    cfg = config or {}
    try:
        from control_algorithm.storage_band_learning import effective_planning_max_soc

        return float(
            effective_planning_max_soc(cfg, state or {}, live=live)
        )
    except Exception:
        try:
            return float(cfg.get("max_soc_percent") or 95.0)
        except (TypeError, ValueError):
            return 95.0


def ems_fill_preempts_energy_save(
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]],
    entry: Optional[Dict[str, Any]] = None,
    *,
    live: Optional[Dict[str, Any]] = None,
) -> bool:
    """Fill/Band/Delta dürfen Energiespar-Off (Floor/Reserve) preemten.

    Priorität wie industrielle EMS: Fill/Export/Band > Morgen-Floor-Anti-Flatter.
    """
    cfg = config or {}
    plan = dict(state.get("fill_schedule") or {})
    if plan.get("over_max") or plan.get("capacity_limited"):
        return True
    if state.get("max_soc_override_active") or state.get("max_soc_protection_active"):
        return True

    live_soc = _resolve_live_soc(state, live)
    ceiling = _planning_max_soc(cfg, state, live)
    if live_soc is not None and live_soc >= ceiling - _SOC_BAND_HYST_PCT:
        return True

    entry = entry or {}
    pause_raw = entry.get("pause_soc")
    if live_soc is not None and pause_raw is not None and pause_raw != "":
        try:
            pause_soc = float(pause_raw)
            if live_soc >= pause_soc + _SOC_PAUSE_DELTA_PCT:
                return True
        except (TypeError, ValueError):
            pass

    # Füll-Urgency (hours_to_max / excess / overflow) – Kern des bisherigen Bugs
    if state_has_fill_max_soc_urgency(state, cfg):
        return True
    return False


def is_fill_resume_action(act: Dict[str, Any]) -> bool:
    if str(act.get("source") or "").lower() == "fill_schedule":
        return True
    reason = str(act.get("reason") or "").lower()
    return "füll" in reason or "fuell" in reason or "fill" in reason


def _fill_wanted_ips(plan: Dict[str, Any]) -> set:
    """IPs, die die Füllkurve jetzt einschalten will (Stack + Flex)."""
    wanted: set = set()
    flex = plan.get("flex") or {}
    ip = str(flex.get("ip") or "")
    if ip:
        wanted.add(ip)
    for item in plan.get("flex_stack") or []:
        sip = str((item or {}).get("ip") or "")
        if sip:
            wanted.add(sip)
    return wanted


def state_has_fill_max_soc_urgency(
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Max-SoC-Dringlichkeit aus fill_schedule (quellenunabhängig)."""
    if state.get("max_soc_protection_active") or state.get("max_soc_override_active"):
        return True
    plan = dict(state.get("fill_schedule") or {})
    if plan.get("ok") is False:
        return False
    try:
        from control_algorithm.fill_schedule import compute_fill_urgency

        return bool(compute_fill_urgency(plan, config or {}))
    except Exception:
        # Fallback ohne Import-Zyklus: gleiche Kernkriterien
        # over_max / capacity_limited zuerst (auch ohne Flex-Start)
        if plan.get("over_max") or plan.get("capacity_limited"):
            return True
        flex = plan.get("flex") or {}
        if not (flex.get("should_start_now") or plan.get("flex_stack")):
            return False
        hours_thresh = normalize_fill_override_hours_to_max(
            (config or {}).get("fill_commitment_override_hours_to_max", 2.0)
        )
        excess_thresh = normalize_fill_override_excess_kw(
            (config or {}).get("fill_commitment_override_excess_kw", 5.0)
        )
        try:
            excess = float(plan.get("excess_charge_kw") or 0)
        except (TypeError, ValueError):
            excess = 0.0
        try:
            overflow = float(flex.get("overflow_kwh") or 0)
        except (TypeError, ValueError):
            overflow = 0.0
        hours_raw = plan.get("hours_to_max")
        try:
            hours_to_max = float(hours_raw) if hours_raw is not None else None
        except (TypeError, ValueError):
            hours_to_max = None
        if overflow >= 0.3:
            return True
        if excess >= excess_thresh:
            return True
        if hours_to_max is not None and hours_to_max <= hours_thresh:
            return True
        return False


def annotate_resumes_for_fill_urgency(
    actions: List[Dict[str, Any]],
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Stempel fill_urgency auf Resumes, die Füll/Max-SoC jetzt will.

    Unabhängig vom Action-Grund (Füll, Spätstart, Export, Budget) – entscheidend
    ist der aktuelle fill_schedule. Damit greifen Arbiter + Off-Commitment-Override
    auch wenn ein anderer Pfad den Resume erzeugt hat.
    """
    if not state_has_fill_max_soc_urgency(state, config):
        return list(actions or [])
    plan = dict(state.get("fill_schedule") or {})
    wanted = _fill_wanted_ips(plan)
    out: List[Dict[str, Any]] = []
    for act in actions or []:
        if act.get("action") != "resume":
            out.append(act)
            continue
        ip = str(act.get("ip") or "")
        if wanted and ip not in wanted:
            out.append(act)
            continue
        stamped = dict(act)
        stamped["fill_urgency"] = True
        out.append(stamped)
    return out


def is_fill_urgency_resume(act: Dict[str, Any]) -> bool:
    """Same-Tick: Füll-Urgency-Resume (ohne emergency zu sein)."""
    if act.get("action") != "resume":
        return False
    return bool(act.get("fill_urgency") or act.get("commitment_fill_override"))


def fill_urgency_allows_off_override(
    act: Dict[str, Any],
    state: Dict[str, Any],
    config: Dict[str, Any],
    entry: Dict[str, Any],
    now: datetime,
    live: Optional[Dict[str, Any]] = None,
) -> bool:
    """Darf Resume Aus-Commitment brechen (Max-SoC-/Band-Dringlichkeit)?

    Quellenunabhängig: Spätstart/Budget/Füll – solange fill_schedule Dringlichkeit
    meldet und die IP vom Füllplan gewollt ist.

    Energiespar-Off (Morgen-Floor/Anker-Reserve) blockiert Fill nicht mehr pauschal:
    EMS-Preemption bei Plan-Band, SoC-Delta seit Pause, hours_to_max/excess oder
    hartem Über-Max. Spill/Flex nur bei Preemption bzw. prefer_on.
    """
    if act.get("action") != "resume":
        return False

    plan = dict(state.get("fill_schedule") or {})
    over_cap_urgent = bool(plan.get("over_max") or plan.get("capacity_limited"))
    preempt = ems_fill_preempts_energy_save(
        state, config, entry, live=live
    ) or over_cap_urgent

    if is_energy_save_off_commitment(entry, now, state):
        if not preempt:
            return False
    if not (
        act.get("fill_urgency")
        or state_has_fill_max_soc_urgency(state, config)
        or preempt
    ):
        return False

    wanted = _fill_wanted_ips(plan)
    ip = str(act.get("ip") or "")
    if wanted and ip not in wanted:
        return False

    # Nur stabile hohe/mittlere Last (prefer_on / Anker) darf Off brechen –
    # bei EMS-Preemption/Über-Max auch Spill/Flex, die der Füllplan will.
    try:
        pplan = state.get("priority_runtime_plan") or {}
        row = (pplan.get("by_ip") or {}).get(ip) or {}
        if not row:
            for d in pplan.get("devices") or []:
                if str(d.get("ip") or "") == ip:
                    row = d
                    break
        role = str(row.get("role") or "")
        prefer = bool(row.get("prefer_on"))
        if role == "spill" and not preempt:
            return False
        if role == "flex" and not prefer and not preempt:
            return False
        # role leer: weiter Heuristik
        if not role and not preempt:
            reason = str(act.get("reason") or "").lower()
            if "nicht so wichtig" in reason or "spill" in reason:
                return False
    except Exception:
        pass

    return True


def is_budget_shed_pause(act: Dict[str, Any]) -> bool:
    """Pause wegen Morgen-Floor / Wochenplan-Budget / gestaffeltem Shed."""
    if act.get("action") != "pause":
        return False
    if act.get("budget_shed") or act.get("morning_floor"):
        return True
    reason = str(act.get("reason") or "").lower()
    markers = (
        "morgen-floor",
        "energie-budget",
        "anker-reserve / budget",
        "anker-reserve",
        "gestaffelt",
        "budget überschritten",
        "nachhaltig",
    )
    return any(m in reason for m in markers)


def pause_kind_from_action(act: Dict[str, Any]) -> str:
    """Klassifiziert Pause für Anti-Flatter (last_pause_kind im Runtime)."""
    if act.get("action") != "pause":
        return "other"
    if act.get("morning_floor") or "morgen-floor" in str(act.get("reason") or "").lower():
        return "morning_floor"
    if act.get("budget_shed") or is_budget_shed_pause(act):
        return "budget_shed"
    return "other"


def is_energy_save_off_commitment(
    entry: Dict[str, Any],
    now: Optional[datetime] = None,
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """True wenn Gerät wegen Morgen-Floor/Budget-Shed noch committed_off ist."""
    if not is_committed_off(entry, now):
        return False
    kind = str(entry.get("last_pause_kind") or "")
    if kind in ("morning_floor", "budget_shed"):
        return True
    # Legacy (vor last_pause_kind): bei aktivem Morgen-Floor jedes Off als Energiesparen
    if state and state.get("morning_floor_threatened"):
        return True
    return False


def budget_pause_allows_on_override(
    act: Dict[str, Any],
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> bool:
    """Darf Budget-/Floor-Pause On-Commitment brechen (Last ≫ Cap)?"""
    if not is_budget_shed_pause(act):
        return False
    # Während Max-SoC-Füll keine Budget-Override-Pausen (Phase A hat Vorrang)
    if state_has_fill_max_soc_urgency(state, config):
        return False
    thresh = normalize_budget_override_kw(
        config.get("budget_commitment_override_kw", 3.0)
    )
    live = live or {}
    overhang: Optional[float] = None
    if act.get("load_overhang_kw") is not None:
        try:
            overhang = float(act.get("load_overhang_kw"))
        except (TypeError, ValueError):
            overhang = None
    if overhang is None:
        try:
            load = float(
                state.get("tick_miner_load_kw")
                or live.get("miner_load_kw")
                or live.get("running_load_kw")
                or 0
            )
        except (TypeError, ValueError):
            load = 0.0
        try:
            budget = float(
                state.get("tick_miner_budget_kw")
                or live.get("miner_budget_kw")
                or live.get("budget_kw")
                or 0
            )
        except (TypeError, ValueError):
            budget = 0.0
        overhang = load - budget
    return float(overhang) >= thresh


def _clear_committed_off_for_ip(
    state: Dict[str, Any],
    ip: str,
    now: datetime,
) -> None:
    """Nach Fill-Override: Aus-Commitment im State beenden."""
    runtime = dict(state.get("device_runtime") or {})
    entry = dict(runtime.get(ip) or {})
    entry["committed_off_until"] = None
    entry["committed_off_cleared_for_fill_at"] = now.isoformat()
    runtime[str(ip)] = entry
    state["device_runtime"] = runtime


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def _today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).date().isoformat()


def runtime_entry(state: Dict[str, Any], ip: str) -> Dict[str, Any]:
    runtime = dict(state.get("device_runtime") or {})
    return dict(runtime.get(ip) or {})


def ensure_off_count_day(entry: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Setzt off_count_today bei Tageswechsel auf 0."""
    today = _today_str(now)
    out = dict(entry)
    if str(out.get("off_count_date") or "") != today:
        out["off_count_date"] = today
        out["off_count_today"] = 0
    else:
        try:
            out["off_count_today"] = int(out.get("off_count_today") or 0)
        except (TypeError, ValueError):
            out["off_count_today"] = 0
    return out


def ensure_on_count_day(entry: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Setzt on_count_today bei Tageswechsel auf 0."""
    today = _today_str(now)
    out = dict(entry)
    if str(out.get("on_count_date") or "") != today:
        out["on_count_date"] = today
        out["on_count_today"] = 0
        out["on_limit_locked"] = False
    else:
        try:
            out["on_count_today"] = int(out.get("on_count_today") or 0)
        except (TypeError, ValueError):
            out["on_count_today"] = 0
    return out


def off_count_today(entry: Dict[str, Any], now: Optional[datetime] = None) -> int:
    return int(ensure_off_count_day(entry, now).get("off_count_today") or 0)


def on_count_today(entry: Dict[str, Any], now: Optional[datetime] = None) -> int:
    return int(ensure_on_count_day(entry, now).get("on_count_today") or 0)


def is_daily_on_limit_reached(
    entry: Dict[str, Any],
    config: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """True wenn harte Einschaltgrenze für heute erreicht ist."""
    if not hard_daily_on_limit_enabled(config):
        return False
    max_on = normalize_max_on_per_day(config.get("max_device_on_per_day", 3))
    if max_on <= 0:
        return False
    return on_count_today(entry, now) >= max_on


def _record_on_limit_block(
    state: Dict[str, Any],
    ip: str,
    reason: str,
    now: datetime,
    live: Optional[Dict[str, Any]] = None,
) -> None:
    """Merkt blockierte Einschaltungen für Risiko-Lernneuron."""
    risk = dict(state.get("switch_risk") or {})
    today = _today_str(now)
    if str(risk.get("date") or "") != today:
        risk = {
            "date": today,
            "blocked_resumes": 0,
            "by_ip": {},
            "events": [],
            "export_kw_at_block_sum": 0.0,
        }
    risk["blocked_resumes"] = int(risk.get("blocked_resumes") or 0) + 1
    by_ip = dict(risk.get("by_ip") or {})
    by_ip[ip] = int(by_ip.get(ip) or 0) + 1
    risk["by_ip"] = by_ip
    export_kw = 0.0
    live = live or {}
    try:
        from control_algorithm.weather_survival import live_grid_export_kw

        export_kw = float(live_grid_export_kw(live))
    except Exception:
        try:
            export_kw = float(live.get("einspeisung_kw") or live.get("export_kw") or 0)
        except (TypeError, ValueError):
            export_kw = 0.0
        if export_kw < 0:
            export_kw = abs(export_kw)
    risk["export_kw_at_block_sum"] = float(risk.get("export_kw_at_block_sum") or 0) + max(
        0.0, export_kw
    )
    events = list(risk.get("events") or [])
    events.append(
        {
            "ts": now.isoformat(timespec="seconds"),
            "ip": ip,
            "reason": (reason or "")[:120],
            "export_kw": round(export_kw, 2),
        }
    )
    risk["events"] = events[-80:]
    state["switch_risk"] = risk
    try:
        from control_algorithm.steering_learning import record_on_limit_block

        record_on_limit_block(state, ip=ip, export_kw=export_kw, now=now)
    except Exception:
        pass


def is_emergency_action(act: Dict[str, Any]) -> bool:
    if act.get("emergency") or act.get("safety"):
        return True
    src = str(act.get("source") or "").lower()
    if src in (
        "min_soc",
        "max_soc_protection",
        "max_soc_hold",
        "anomaly",
        "anomaly_low",
        "pv_safety",
        "priority_violation",
        "night_lock",
        "run_reserve",
    ):
        return True
    if act.get("run_reserve"):
        return True
    reason = str(act.get("reason") or "").lower()
    return any(m in reason for m in _EMERGENCY_REASON_MARKERS)


def commitment_active_until(entry: Dict[str, Any], key: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now()
    until = _parse_iso(entry.get(key))
    if until is None or until <= now:
        return None
    return until


def algo_runtime_locks_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """Mindestlaufzeit und An-/Aus-Bindung des Algorithmus.

    False lässt den Algorithmus frei schalten. Die Sperre nach einem
    menschlichen Schaltbefehl bleibt davon unabhängig.
    """
    raw = (config or {}).get("algo_runtime_locks_enabled", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "off", "aus", "nein")
    return bool(raw)


def is_committed_on(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    return commitment_active_until(entry, "committed_on_until", now) is not None


def is_committed_off(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    return commitment_active_until(entry, "committed_off_until", now) is not None


def _pick_until(
    preferred_iso: Any,
    fallback_hours: float,
    now: datetime,
    *,
    max_ahead_hours: Optional[float] = None,
) -> str:
    """Nimmt Planzeit nur, wenn sie nicht weit über die Mindesthaltedauer hinausragt.

    Ohne Kappe würde ein `next_off_at` von morgen Mittag nach einem
    Abend-Resume 17 h Einschaltzwang erzeugen – genau der 18:39-Fehler.
    """
    hours = max(0.25, float(fallback_hours))
    cap_h = hours
    if max_ahead_hours is not None:
        try:
            cap_h = max(hours, min(24.0, float(max_ahead_hours)))
        except (TypeError, ValueError):
            cap_h = hours
    preferred = _parse_iso(preferred_iso)
    if preferred is not None:
        delta_h = (preferred - now).total_seconds() / 3600.0
        if 0.05 <= delta_h <= cap_h + 0.05:
            return preferred.isoformat()
    return (now + timedelta(hours=hours)).isoformat()


def sync_plan_times_into_runtime(
    state: Dict[str, Any],
    device_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Übernimmt planned_on/planned_off aus Decision-Board in device_runtime."""
    runtime: Dict[str, Any] = dict(state.get("device_runtime") or {})
    for row in device_rows or []:
        ip = str(row.get("ip") or "")
        if not ip:
            continue
        entry = dict(runtime.get(ip) or {})
        planned_on = row.get("planned_on")
        planned_off = row.get("planned_off")
        if planned_on:
            entry["next_on_at"] = str(planned_on)
        if planned_off:
            entry["next_off_at"] = str(planned_off)
        runtime[ip] = entry
    state["device_runtime"] = runtime
    return state


def update_runtime_commitments(
    state: Dict[str, Any],
    applied_actions: List[Dict[str, Any]],
    config: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Nach angewendeten Schaltungen Commitments + Tageszähler setzen."""
    now = now or datetime.now()
    runtime: Dict[str, Any] = dict(state.get("device_runtime") or {})
    off_h = normalize_commitment_off_hours(
        config.get("switch_commitment_off_hours")
    )
    # Risiko-Lernen: längere Aus-Bindung → weniger Zyklen
    try:
        from control_algorithm.steering_learning import switch_risk_extra_off_hours

        off_h = min(24.0, off_h + float(switch_risk_extra_off_hours(state)))
    except Exception:
        pass
    try:
        from control_algorithm.load_scheduler import normalize_min_runtime_hours

        on_h = normalize_min_runtime_hours(
            config.get("switch_commitment_on_hours", config.get("min_device_runtime_hours"))
        )
    except Exception:
        on_h = float(config.get("min_device_runtime_hours") or 1.0)
    try:
        from control_algorithm.steering_learning import switch_risk_extra_runtime_h

        on_h = min(24.0, on_h + float(switch_risk_extra_runtime_h(state)))
    except Exception:
        pass

    max_on = normalize_max_on_per_day(config.get("max_device_on_per_day", 3))

    for act in applied_actions:
        ip = act.get("ip")
        action = act.get("action")
        if not ip or action not in ("pause", "resume"):
            continue
        entry = ensure_on_count_day(
            ensure_off_count_day(dict(runtime.get(ip) or {}), now), now
        )
        entry["last_action"] = action
        entry["last_action_at"] = now.isoformat()
        if action == "pause":
            entry["paused_at"] = now.isoformat()
            entry["resumed_at"] = None
            entry["committed_on_until"] = None
            entry["committed_off_until"] = _pick_until(
                entry.get("next_on_at"), off_h, now
            )
            entry["last_pause_kind"] = pause_kind_from_action(act)
            pause_soc = _resolve_live_soc(state, act=act)
            if pause_soc is not None:
                entry["pause_soc"] = round(pause_soc, 2)
            if not is_emergency_action(act):
                entry["off_count_today"] = int(entry.get("off_count_today") or 0) + 1
                entry["off_count_date"] = _today_str(now)
        else:
            entry["resumed_at"] = now.isoformat()
            entry["paused_at"] = None
            entry["committed_off_until"] = None
            entry["last_pause_kind"] = None
            entry["pause_soc"] = None
            entry["committed_on_until"] = _pick_until(
                entry.get("next_off_at"), on_h, now, max_ahead_hours=on_h
            )
            if not is_emergency_action(act):
                entry["on_count_today"] = int(entry.get("on_count_today") or 0) + 1
                entry["on_count_date"] = _today_str(now)
                if max_on > 0 and int(entry["on_count_today"]) >= max_on:
                    entry["on_limit_locked"] = True
                    # Sperre bis Tagesende (lokale Mitternacht)
                    end = (now + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    entry["on_limit_locked_until"] = end.isoformat()
        runtime[str(ip)] = entry

    state["device_runtime"] = runtime
    return state


def _same_tick_arbiter(
    actions: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Pro IP höchstens eine Aktion; Pause gewinnt bei Konflikt (außer Notfall-Resume).

    Ausnahme: Max-SoC-Override / over_max / Band-/Fill-Preemption → Fill-Resume
    schlägt Morgen-Floor-/Budget-Pause (Speicher darf nicht am Band/Max kleben).
    """
    by_ip: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for act in actions:
        ip = str(act.get("ip") or "")
        if not ip:
            continue
        if ip not in by_ip:
            by_ip[ip] = []
            order.append(ip)
        by_ip[ip].append(act)

    overhang_thresh = normalize_budget_override_kw(
        (config or {}).get("budget_commitment_override_kw", 3.0)
    )
    st = state or {}
    plan = dict(st.get("fill_schedule") or {})
    over_max_hard = bool(
        plan.get("over_max")
        or plan.get("capacity_limited")
        or st.get("max_soc_override_active")
        or st.get("max_soc_protection_active")
    )
    fill_preempts_floor = over_max_hard or ems_fill_preempts_energy_save(
        st, config or {}, None
    )

    try:
        from control_algorithm.surplus_session import is_running as _session_running
        from control_algorithm.surplus_session import session_devices as _session_devices

        session_on = _session_running(st)
        session_ips = set(_session_devices(st)) if session_on else set()
    except Exception:
        session_on = False
        session_ips = set()

    def _note(winner, losers, why):
        try:
            from control_algorithm.control_priority import record_arbiter_decision

            record_arbiter_decision(
                st, ip=str(winner.get("ip") or ""), winner=winner, losers=losers, why=why
            )
        except Exception:
            pass

    out: List[Dict[str, Any]] = []
    for ip in order:
        group = by_ip[ip]
        if len(group) == 1:
            out.append(group[0])
            continue
        pauses = [a for a in group if a.get("action") == "pause"]
        resumes = [a for a in group if a.get("action") == "resume"]
        if pauses and resumes:
            emerg_resume = next((a for a in resumes if is_emergency_action(a)), None)
            emerg_pause = next((a for a in pauses if is_emergency_action(a)), None)
            fill_urgency_resume = next(
                (a for a in resumes if is_fill_urgency_resume(a)), None
            )
            floor_budget_pause = next(
                (a for a in pauses if is_budget_shed_pause(a)), None
            )
            # Laufender Überschuss-Zug: weiche Pausen dürfen ihn nicht kippen
            session_resume = (
                resumes[0] if (session_on and str(ip) in session_ips and resumes) else None
            )
            if emerg_pause:
                out.append(emerg_pause)
                _note(emerg_pause, resumes, "Notbremse schlägt alles")
            elif emerg_resume and not any(
                a.get("morning_floor") or "Morgen-Floor" in str(a.get("reason") or "")
                for a in pauses
            ):
                out.append(emerg_resume)
                _note(emerg_resume, pauses, "Notfall-Resume ohne Morgen-Floor")
            elif session_resume is not None and floor_budget_pause is not None:
                # Plan-Session steht über Floor/Reserve, aber unter jeder Notbremse
                out.append(session_resume)
                _note(session_resume, pauses, "Überschuss-Session schlägt Floor/Budget")
            elif fill_urgency_resume and fill_preempts_floor:
                # Band/Urgency/Über-Max: Einschalten schlägt Morgen-Floor-/Budget-Pause
                out.append(fill_urgency_resume)
                _note(fill_urgency_resume, pauses, "Fill/Band-Preemption")
            elif floor_budget_pause is not None:
                # Floor/Budget mit Überhang schlägt normales Fill-/Export-Resume
                try:
                    oh = float(floor_budget_pause.get("load_overhang_kw") or 0)
                except (TypeError, ValueError):
                    oh = 0.0
                if oh >= overhang_thresh or floor_budget_pause.get("morning_floor"):
                    out.append(floor_budget_pause)
                elif fill_urgency_resume:
                    out.append(fill_urgency_resume)
                else:
                    out.append(floor_budget_pause)
            elif fill_urgency_resume:
                out.append(fill_urgency_resume)
            else:
                # Energy-stress / Morgen-Floor Pause vor Export-Resume
                out.append(pauses[0])
            continue
        # mehrere gleiche Richtung: force/emergency zuerst
        ranked = sorted(
            group,
            key=lambda a: (
                0 if is_emergency_action(a) else 1,
                0 if a.get("force") else 1,
            ),
        )
        out.append(ranked[0])
    return out


def apply_switch_commitment_guards(
    actions: List[Dict[str, Any]],
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Filtert Aktionen gegen Commitments + harte Einschalt-Tagesgrenze.
    Returns (filtered_actions, info_messages).
    """
    now = now or datetime.now()
    live = live or {}
    messages: List[str] = []
    max_off = normalize_max_off_per_day(config.get("max_device_off_per_day", 3))
    max_on = normalize_max_on_per_day(config.get("max_device_on_per_day", 3))
    hard_on = hard_daily_on_limit_enabled(config)
    export_override = normalize_export_override_kw(
        config.get("export_commitment_override_kw")
    )
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

    # Quellenunabhängig: Füll/Max-SoC-Dringlichkeit auf Resumes stempeln
    # (Spätstart/Budget/Füll), bevor Arbiter + Commitment-Filter laufen.
    actions = annotate_resumes_for_fill_urgency(list(actions or []), state, config)
    actions = _same_tick_arbiter(actions, config, state=state)
    filtered: List[Dict[str, Any]] = []

    for act in actions:
        ip = str(act.get("ip") or "")
        action = act.get("action")
        if not ip or action not in ("pause", "resume"):
            filtered.append(act)
            continue

        entry = ensure_on_count_day(
            ensure_off_count_day(runtime_entry(state, ip), now), now
        )
        emergency = is_emergency_action(act)

        if action == "pause":
            # Letzte Einschalt-Budget schon aufgebraucht: Gerät möglichst an lassen
            # (außer Notfall / Budget-Überhang), sonst morgen kein Resume mehr möglich.
            on_cnt = on_count_today(entry, now)
            if (
                hard_on
                and max_on > 0
                and on_cnt >= max_on
                and not emergency
                and not budget_pause_allows_on_override(act, state, config, live)
            ):
                messages.append(
                    f"Tages-Einschaltlimit: Pause {ip} vermieden – "
                    f"schon {on_cnt}× EIN heute (Limit {max_on}), Sperre bis Tagesende."
                )
                continue
            if (
                algo_runtime_locks_enabled(config)
                and not emergency
                and is_committed_on(entry, now)
            ):
                if budget_pause_allows_on_override(act, state, config, live):
                    act = dict(act)
                    act["commitment_budget_override"] = True
                    until = commitment_active_until(entry, "committed_on_until", now)
                    messages.append(
                        f"Anti-Flatter: Pause {ip} trotz An-Commitment – "
                        f"Budget-Überhang "
                        f"(Cap bis {until.strftime('%H:%M') if until else '?'})."
                    )
                    # Soft-Tageswert nur Hinweis – keine harte Sperre
                    count = off_count_today(entry, now)
                    if max_off > 0 and count >= max_off:
                        messages.append(
                            f"Anti-Flatter: Hinweis {ip} schon {count}× Aus/Tag "
                            f"(Empfehlung ≤{max_off}) – Pause trotzdem erlaubt "
                            f"(genauere Planung / nächste Flex-Stufe bevorzugen)."
                        )
                        act["soft_off_recommend_exceeded"] = True
                    if (
                        not emergency
                        and act.get("force")
                        and (
                            act.get("morning_floor")
                            or "Morgen-Floor" in str(act.get("reason") or "")
                            or "gestaffelt" in str(act.get("reason") or "").lower()
                        )
                    ):
                        act["force"] = False
                        act["commitment_soft_force"] = True
                    filtered.append(act)
                    continue
                until = commitment_active_until(entry, "committed_on_until", now)
                messages.append(
                    f"Anti-Flatter: Pause {ip} blockiert – Commitment an bis "
                    f"{until.strftime('%H:%M') if until else '?'}."
                )
                continue
            count = off_count_today(entry, now)
            if max_off > 0 and count >= max_off:
                messages.append(
                    f"Anti-Flatter: Hinweis {ip} schon {count}× Aus/Tag "
                    f"(Empfehlung ≤{max_off}) – Pause trotzdem erlaubt "
                    f"(Werk muss Energiesparen können)."
                )
                act = dict(act)
                act["soft_off_recommend_exceeded"] = True
            # Morgen-Floor / Budget: kein force mehr nötig fürs Commitment-Modell,
            # außer explizit emergency – force für Min-Laufzeit-Bypass nur bei Safety.
            if (
                not emergency
                and act.get("force")
                and (
                    act.get("morning_floor")
                    or "Morgen-Floor" in str(act.get("reason") or "")
                    or "gestaffelt" in str(act.get("reason") or "").lower()
                )
            ):
                act = dict(act)
                act["force"] = False
                act["commitment_soft_force"] = True
            filtered.append(act)
            continue

        # resume — harte Tages-Einschaltgrenze (vor Commitment-Overrides)
        on_cnt = on_count_today(entry, now)
        if hard_on and max_on > 0 and on_cnt >= max_on and not emergency:
            _record_on_limit_block(
                state, ip, str(act.get("reason") or ""), now, live=live
            )
            messages.append(
                f"Tages-Einschaltlimit: Resume {ip} gesperrt – "
                f"schon {on_cnt}× EIN heute (max {max_on}), "
                f"Sperre bis Tagesende. Risiko-Neuron lernt mit."
            )
            continue

        if (
            algo_runtime_locks_enabled(config)
            and not emergency
            and is_committed_off(entry, now)
        ):
            # Echte Export-Spitze darf Off-Commitment brechen
            # (auch bei proaktivem Entladen / Export-Vermeidung ohne „Export“ im Text)
            high_export = live_export >= export_override > 0
            reason_l = str(act.get("reason") or "").lower()
            is_export_resume = (
                "einspeisung" in reason_l
                or "export" in reason_l
                or act.get("urgent")
                or act.get("fill_urgency")
                or "entladen" in reason_l
                or "spätstart" in reason_l
            )
            if high_export and is_export_resume:
                act = dict(act)
                act["commitment_export_override"] = True
                messages.append(
                    f"Anti-Flatter: Resume {ip} trotz Aus-Commitment – "
                    f"Live-Export {live_export:.2f} kW ≥ {export_override:.2f} kW."
                )
                filtered.append(act)
                continue
            # Proaktives Entladen: Budget-/Floor-Off bei Export-Vermeidung zurücknehmen
            proactive = 0.0
            try:
                proactive = float(
                    (state.get("horizon_projection") or state.get("balance_projection") or {}).get(
                        "proactive_discharge_kw"
                    )
                    or 0
                )
            except (TypeError, ValueError):
                proactive = 0.0
            if high_export and proactive > 0.5:
                act = dict(act)
                act["commitment_export_override"] = True
                act["commitment_discharge_override"] = True
                messages.append(
                    f"Anti-Flatter: Resume {ip} trotz Aus-Commitment – "
                    f"proaktives Entladen ({proactive:.1f} kW) bei Live-Export "
                    f"{live_export:.2f} kW."
                )
                filtered.append(act)
                continue
            # Füll/Max-SoC-Dringlichkeit darf Off-Commitment brechen
            if fill_urgency_allows_off_override(
                act, state, config, entry, now, live=live
            ):
                act = dict(act)
                act["commitment_fill_override"] = True
                _clear_committed_off_for_ip(state, ip, now)
                messages.append(
                    f"Anti-Flatter: Resume {ip} trotz Aus-Commitment – "
                    f"Max-SoC-Füll (fill_urgency / Plan)."
                )
                filtered.append(act)
                continue
            until = commitment_active_until(entry, "committed_off_until", now)
            messages.append(
                f"Anti-Flatter: Resume {ip} blockiert – Commitment aus bis "
                f"{until.strftime('%H:%M') if until else '?'}."
            )
            continue

        # Spätstart/Export nicht mehr als urgent markieren (Min-Off / Hysterese greifen)
        if act.get("urgent") and not emergency:
            act = dict(act)
            act["urgent"] = False
            act["commitment_demoted_urgent"] = True
        filtered.append(act)

    return filtered, messages
