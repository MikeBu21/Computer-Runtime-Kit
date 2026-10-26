# -*- coding: utf-8 -*-
"""Berechnetes SoC-Ziel für die Planung.

Das Ziel ist eine Eigenschaft der Anlage (Speicher, kWp, schaltbare
Verbraucher), nicht der heutige Ertrag. Zu wenig PV ist eine
Zielverfehlung: die Zahl bleibt stehen.

Neu gerechnet wird nur, wenn sich die Anlage ändert: Verbraucher dazu
oder weg, nicht mehr erreichbar, nicht mehr schaltbar, andere kWp oder
andere Speichergröße. Beim ersten Mal wird der bereits gelernte Wert
übernommen, damit eine bestehende Anlage nicht springt.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Set

from control_algorithm.steering_learning import (
    _clamp_param,
    ensure_steering_learning,
    get_steering_params,
)

# Unterkante des Lern-Bands. Darunter schreibt das Band den Wert wieder hoch.
_BAND_FLOOR = 82.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _blocked_ips(candidate_ips: List[str]) -> Set[str]:
    """Nicht schaltbar: als fehlend markiert oder SSH-Sperre. Kein TCP-Check."""
    blocked: Set[str] = set()
    miners_by_ip: Dict[str, Dict[str, Any]] = {}
    try:
        from control_algorithm.config import load_miners

        for miner in load_miners() or []:
            ip = str((miner or {}).get("ip") or "")
            if ip:
                miners_by_ip[ip] = miner
    except Exception:
        miners_by_ip = {}
    circuit_open = None
    try:
        from miner_control import _ssh_circuit_open

        circuit_open = _ssh_circuit_open
    except Exception:
        circuit_open = None
    for ip in candidate_ips:
        miner = miners_by_ip.get(ip) or {}
        state = str(miner.get("recovery_state") or "").lower()
        try:
            streak = int(miner.get("recovery_miss_streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        if state == "missing" or streak >= 5:
            blocked.add(ip)
            continue
        if circuit_open is not None:
            try:
                if circuit_open(ip):
                    blocked.add(ip)
            except Exception:
                pass
    return blocked


def plant_signature(config: Dict[str, Any]) -> Dict[str, Any]:
    """Stabiler Fingerabdruck aus Speicher, kWp und schaltbarer Last."""
    storage = round(_f(config.get("storage_kwh")), 2)
    east = _f(config.get("pv_kwp_east"))
    west = _f(config.get("pv_kwp_west"))
    kwp = _f(config.get("pv_kwp"))
    plant_kwp = round(max(kwp, east + west), 2)
    raw: List[tuple] = []
    for device in config.get("devices") or []:
        if not device.get("enabled"):
            continue
        ip = str(device.get("ip") or "")
        power = round(_f(device.get("power_kw")), 2)
        if not ip or power <= 0:
            continue
        raw.append((ip, power))
    blocked = _blocked_ips([ip for ip, _power in raw])
    devices = [[ip, power] for ip, power in raw if ip not in blocked]
    devices.sort()
    load_kw = round(sum(power for _ip, power in devices), 2)
    return {
        "storage_kwh": storage,
        "pv_kwp": plant_kwp,
        "load_kw": load_kw,
        "devices": devices,
    }


def _clamp_target(pct: float, min_soc: float, max_soc: float) -> float:
    hi = min(max_soc, 95.0)
    lo = min_soc + 5.0
    if _BAND_FLOOR < hi:
        lo = max(lo, _BAND_FLOOR)
    if lo > hi:
        lo = hi
    # Ganze Prozent, wie sie im Kästchen stehen. 88,7 aus dem Lernwert bleibt 89.
    return float(round(max(lo, min(hi, float(pct)))))


def _rescale(
    old_pct: float,
    old_sig: Dict[str, Any],
    new_sig: Dict[str, Any],
    *,
    max_soc: float,
    min_soc: float,
) -> float:
    """Abstand zu Max skaliert mit Last, kWp und Speicher.

    Weniger schaltbare kW oder mehr kWp vergrößert den Abstand (Ziel sinkt).
    Mehr Speicher verkleinert den Abstand in Prozent (Ziel steigt).
    """
    gap = max(0.5, max_soc - float(old_pct))
    scale = 1.0
    old_load = _f(old_sig.get("load_kw"))
    new_load = _f(new_sig.get("load_kw"))
    if old_load > 0.05 and new_load > 0.05:
        scale *= old_load / new_load
    elif new_load <= 0.05 < old_load:
        scale *= 2.5
    elif old_load <= 0.05 < new_load:
        scale *= 0.6
    old_kwp = _f(old_sig.get("pv_kwp"))
    new_kwp = _f(new_sig.get("pv_kwp"))
    if old_kwp > 0.05 and new_kwp > 0.05:
        scale *= new_kwp / old_kwp
    old_storage = _f(old_sig.get("storage_kwh"))
    new_storage = _f(new_sig.get("storage_kwh"))
    if old_storage > 0.05 and new_storage > 0.05:
        scale *= old_storage / new_storage
    scale = max(0.25, min(4.0, scale))
    return _clamp_target(max_soc - gap * scale, min_soc, max_soc)


def _write_band(state: Dict[str, Any], pct: float) -> None:
    learning = ensure_steering_learning(state)
    params = learning.setdefault("params", {})
    params["band_max_target_pct"] = _clamp_param("band_max_target_pct", pct)


def refresh_planning_target(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Ziel in den State schreiben. Gleicher Fingerabdruck ändert nichts
    außer dem gelernten Band (Überfüllung), nie wegen zu wenig PV.
    """
    signature = plant_signature(config)
    max_soc = _f(config.get("max_soc_percent"), 95.0)
    min_soc = _f(config.get("min_soc_percent"), 28.0)
    learned = _clamp_target(
        _f(get_steering_params(state).get("band_max_target_pct"), 90.0),
        min_soc,
        max_soc,
    )
    stored = state.get("planning_target")
    if not isinstance(stored, dict):
        stored = {}
    old_signature = stored.get("signature")
    now = datetime.now().isoformat(timespec="seconds")
    if old_signature == signature and stored.get("soc_percent") is not None:
        pct = learned
        if abs(_f(stored.get("soc_percent")) - pct) >= 0.05:
            stored = {
                "soc_percent": pct,
                "signature": signature,
                "reason": "gelernt",
                "updated_at": now,
            }
            _write_band(state, pct)
            state["planning_target"] = stored
        return state["planning_target"]
    if not old_signature:
        pct = learned
        reason = "uebernahme"
    else:
        pct = _rescale(
            _f(stored.get("soc_percent"), learned),
            old_signature if isinstance(old_signature, dict) else {},
            signature,
            max_soc=max_soc,
            min_soc=min_soc,
        )
        reason = "anlage"
    _write_band(state, pct)
    state["planning_target"] = {
        "soc_percent": pct,
        "signature": signature,
        "reason": reason,
        "updated_at": now,
    }
    return state["planning_target"]


def current_target_percent(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[float]:
    try:
        info = refresh_planning_target(config, state)
        return float(info["soc_percent"])
    except Exception:
        return None
