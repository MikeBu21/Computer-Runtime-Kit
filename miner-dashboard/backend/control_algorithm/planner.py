# -*- coding: utf-8 -*-
"""SOC-Ziel-Planung und Miner-Ein/Aus-Schaltung (mit Prioritäten)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.config import format_device_priority, normalize_device_priority


class _PriorityLabelMap:
    """Kompatibel zu dict.get() – Anzeige für Priorität 0–10."""

    def get(self, key: Any, default: Any = None) -> str:
        if key is None and default is not None:
            return format_device_priority(default)
        return format_device_priority(key if key is not None else 5)


PRIORITY_LABEL = _PriorityLabelMap()


def _parse_time(t: str) -> Optional[Tuple[int, int]]:
    t = (t or "").strip()
    if not t or ":" not in t:
        return None
    parts = t.split(":")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def priority_rank(device: Dict[str, Any]) -> float:
    """Sortier-Rang: niedrigere Zahl = höhere Priorität (10→0.0 … 0→10.0)."""
    return 10.0 - normalize_device_priority(device.get("priority"))


def _priority_rank(device: Dict[str, Any]) -> float:
    return priority_rank(device)


def highest_paused_priority_rank(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, str],
) -> Optional[float]:
    """Niedrigster Rang (= höchste Prioritätsstufe) unter den pausierten Geräten."""
    ranks = [
        priority_rank(d)
        for d in devices
        if miner_status.get(d.get("ip")) != "running"
    ]
    return min(ranks) if ranks else None


def paused_devices_highest_tier(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Pausierte Geräte der höchsten noch fehlenden Prioritätsstufe (zuerst einschalten)."""
    tier = highest_paused_priority_rank(devices, miner_status)
    if tier is None:
        return []
    return [
        d
        for d in devices
        if miner_status.get(d.get("ip")) != "running" and priority_rank(d) == tier
    ]


def find_priority_violations(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, str],
) -> List[Dict[str, Any]]:
    """
    Laufende Geräte niedrigerer Stufe, obwohl höherwertige Stufe noch pausiert ist.
    Rückgabe sortiert zum Abschalten (niedrigste Priorität zuerst).
    """
    tier = highest_paused_priority_rank(devices, miner_status)
    if tier is None:
        return []
    running_lower = [
        d
        for d in devices
        if miner_status.get(d.get("ip")) == "running" and priority_rank(d) > tier
    ]
    return sort_for_pause(running_lower)


def sort_for_resume(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Höchste Priorität zuerst."""
    from control_algorithm.config import sort_devices_by_priority

    return sort_devices_by_priority(devices)


def sort_for_pause(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Niedrigste Priorität zuerst (zuerst abschalten)."""
    return sorted(devices, key=lambda d: (-_priority_rank(d), str(d.get("ip", ""))))


def soc_targets_in_horizon(
    soc_targets: List[Dict[str, Any]],
    now: Optional[datetime] = None,
    horizon_days: int = 3,
) -> List[Dict[str, Any]]:
    """Alle SOC-Zielzeitpunkte innerhalb des rollierenden Horizonts (now … now+horizon_days)."""
    now = now or datetime.now()
    horizon_end = now + timedelta(days=horizon_days)
    result: List[Dict[str, Any]] = []
    for st in soc_targets or []:
        tm = _parse_time(st.get("time", ""))
        if tm is None:
            continue
        h, m = tm
        target_soc = float(st.get("soc_percent", 0))
        target_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target_dt <= now:
            target_dt += timedelta(days=1)
        while target_dt <= horizon_end:
            result.append(
                {
                    "time": st.get("time"),
                    "soc_percent": target_soc,
                    "target_datetime": target_dt.isoformat(),
                    "minutes_until": int((target_dt - now).total_seconds() / 60),
                }
            )
            target_dt += timedelta(days=1)
    result.sort(key=lambda x: x["target_datetime"])
    return result


def next_soc_target(
    soc_targets: List[Dict[str, Any]], now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = now or datetime.now()
    candidates = []
    for st in soc_targets or []:
        tm = _parse_time(st.get("time", ""))
        if tm is None:
            continue
        h, m = tm
        target_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target_dt <= now:
            target_dt += timedelta(days=1)
        candidates.append((target_dt, st))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    target_dt, st = candidates[0]
    return {
        "time": st.get("time"),
        "soc_percent": float(st.get("soc_percent", 0)),
        "target_datetime": target_dt.isoformat(),
        "minutes_until": int((target_dt - now).total_seconds() / 60),
    }


def enabled_devices(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    from control_algorithm.config import sort_devices_by_priority

    devices = [
        d
        for d in (config.get("devices") or [])
        if d.get("enabled") and float(d.get("power_kw") or 0) > 0
    ]
    return sort_devices_by_priority(devices)


def miner_load_kw(devices: List[Dict[str, Any]], miner_status: Dict[str, str]) -> float:
    total = 0.0
    for d in devices:
        ip = d.get("ip")
        if miner_status.get(ip) == "running":
            total += float(d.get("power_kw") or 0)
    return total


def _miner_pv_budget_kw(live: Dict[str, Any], conservative: bool = False) -> float:
    """Verfügbare PV-Leistung für Miner (kW), optional konservativ."""
    pv_kw = float(live.get("pv_kw") or 0)
    if pv_kw < 0.2:
        return 0.0
    margin = 0.80 if conservative else 0.90
    return max(0.0, pv_kw * margin)


def _enforce_pv_budget(
    devices: List[Dict[str, Any]],
    miner_status: Dict[str, str],
    budget_kw: float,
    actions: List[Dict[str, Any]],
    messages: List[str],
) -> None:
    """Schaltet Geräte mit niedriger Priorität ab, wenn Miner-Last > PV-Budget."""
    if budget_kw <= 0:
        return
    running = [d for d in devices if miner_status.get(d.get("ip")) == "running"]
    load = miner_load_kw(running, miner_status)
    if load <= budget_kw:
        return
    for d in sort_for_pause(running):
        ip = d.get("ip")
        if miner_status.get(ip) != "running":
            continue
        if any(a.get("ip") == ip and a.get("action") == "pause" for a in actions):
            continue
        pwr = float(d.get("power_kw") or 0)
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": (
                    f"PV-Budget ({budget_kw:.1f} kW): Priorität "
                    f"{PRIORITY_LABEL.get(normalize_device_priority(d.get('priority')), 'Mittel')}"
                ),
            }
        )
        messages.append(
            f"Steuerung plant Gerät {ip} auszuschalten (Priorität niedriger, "
            f"Miner-Last {load:.1f} kW > verfügbare PV ca. {budget_kw:.1f} kW)."
        )
        load -= pwr
        if load <= budget_kw:
            break


def simulate_soc_at_target(
    config: Dict[str, Any],
    live: Dict[str, Any],
    pv_forecast_kwh_rest_of_day: float,
    next_target: Dict[str, Any],
) -> Dict[str, Any]:
    """Vereinfachte Prognose ob SOC-Ziel erreichbar ist."""
    storage = float(config.get("storage_kwh") or 0)
    if not config.get("has_storage") or storage <= 0:
        return {"reachable": True, "note": "no_storage"}

    soc = live.get("soc_percent")
    if soc is None:
        return {"reachable": False, "note": "no_soc"}

    soc_kwh = (float(soc) / 100.0) * storage
    target_soc = float(next_target.get("soc_percent", 0))
    target_kwh = (target_soc / 100.0) * storage
    delta = target_kwh - soc_kwh

    devices = enabled_devices(config)
    miner_kw = miner_load_kw(devices, live.get("miner_status") or {})
    pv_kw = float(live.get("pv_kw") or 0)
    minutes = max(1, int(next_target.get("minutes_until", 60)))
    hours = minutes / 60.0

    verbrauch = float(live.get("verbrauch_kw") or 0)
    haus_kw = max(0.0, verbrauch - miner_kw)
    battery_kw = float(live.get("battery_power_kw") or 0)

    pv_kwh_period = pv_forecast_kwh_rest_of_day * (hours / 24.0) + pv_kw * hours * 0.5
    net_effect = pv_kwh_period - haus_kw * hours - miner_kw * hours + battery_kw * hours

    return {
        "reachable": net_effect >= delta - storage * 0.05,
        "delta_kwh": round(delta, 2),
        "estimated_change_kwh": round(net_effect, 2),
        "current_soc": soc,
        "target_soc": target_soc,
    }


def plan_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    pv_today_kwh: float,
    pv_tomorrow_kwh: float,
    yield_factor: float,
    state: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Delegiert an profilbasierte Laststeuerung (load_scheduler)."""
    from control_algorithm.load_scheduler import plan_load_actions

    return plan_load_actions(
        config,
        live,
        state or {},
        base_dir or "",
        pv_today_kwh,
        pv_tomorrow_kwh,
        yield_factor,
    )


def switch_times_from_state(state: Dict[str, Any]) -> Dict[str, float]:
    """Letzte Schaltzeit pro Miner aus persistentem device_runtime (Unix-Timestamp)."""
    times: Dict[str, float] = {}
    for ip, entry in (state.get("device_runtime") or {}).items():
        at = (entry or {}).get("last_action_at")
        if not at:
            continue
        try:
            times[str(ip)] = datetime.fromisoformat(str(at)).timestamp()
        except Exception:
            continue
    return times


def apply_hysteresis(
    actions: List[Dict[str, Any]],
    last_applied: Dict[str, str],
    min_interval_seconds: int = 300,
    last_times: Optional[Dict[str, float]] = None,
    miner_status: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Verhindert zu häufiges Umschalten desselben Miners (force-Aktionen durchreichen).

    Wichtig: Ein erneutes resume darf nicht verworfen werden, wenn das Gerät
    tatsächlich aus ist – sonst bleibt z. B. Hydro dauerhaft aus, weil zuletzt
    „resume“ gemerkt wurde, während der Miner längst pausiert ist.
    """
    import time

    last_times = last_times or {}
    miner_status = miner_status or {}
    now = time.time()
    filtered = []
    for act in actions:
        if act.get("force"):
            filtered.append(act)
            continue
        ip = act["ip"]
        action = act["action"]
        status = str(miner_status.get(ip) or "").lower()
        actually_off = status in ("paused", "stopped", "off", "idle", "error", "")
        actually_on = status == "running"
        # Korrektur-Resume: Gerät ist aus, Plan will ein → immer durchlassen
        corrective_resume = action == "resume" and actually_off
        # Korrektur-Pause: Gerät läuft, Plan will aus → Interval egal bei force (oben);
        # hier nur Duplikat-Skip lockern wenn Status nicht passt
        if last_applied.get(ip) == action and not corrective_resume:
            continue
        if (
            not corrective_resume
            and ip in last_times
            and (now - last_times[ip]) < min_interval_seconds
        ):
            act = dict(act)
            act["blocked_hysteresis"] = True
            act["hysteresis_remaining_s"] = round(
                min_interval_seconds - (now - last_times[ip])
            )
            continue
        # Pause für Gerät das schon aus ist: überspringen (Status-Sync)
        if action == "pause" and actually_off:
            continue
        if action == "resume" and actually_on:
            continue
        filtered.append(act)
    return filtered
