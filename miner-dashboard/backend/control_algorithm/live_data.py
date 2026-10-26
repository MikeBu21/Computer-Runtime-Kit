# -*- coding: utf-8 -*-
"""Live-Daten aus bestehendem Dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from control_algorithm.config import BACKEND_DIR


def _parse_float(raw: Any) -> Optional[float]:
    if raw is None or raw == "" or raw == "n/a":
        return None
    try:
        return float(str(raw).replace(",", ".").strip())
    except ValueError:
        return None


def load_live_snapshot(base_dir: Optional[str] = None) -> Dict[str, Any]:
    base = base_dir or str(BACKEND_DIR)
    try:
        from dashboard_battery import load_battery_data, load_battery_soc
        from miner_flow_data import build_flow_response, compute_aktueller_verbrauch_kw

        bat = load_battery_data()
        flow = build_flow_response(
            base,
            load_battery_soc,
            bat.get("power_kw"),
        )
        flow["battery_soc"] = bat.get("soc") or flow.get("battery_soc")
        flow["battery_power_kw"] = bat.get("power_kw")

        pv_kw = _parse_float(flow.get("pv"))
        grid_kw = _parse_float(flow.get("grid"))
        verbrauch = _parse_float(flow.get("verbrauch_gesamt"))
        # Flow-Key "haus" = Wohnhauslast (ohne steuerbare Last); kanonisch house_kw/wohnhaus_kw
        house_kw = _parse_float(flow.get("haus") or flow.get("wohnhaus"))
        hydro_kw = _parse_float(flow.get("hydro") or flow.get("wasserkraft"))
        battery_power = bat.get("power_kw")
        soc = _parse_float(flow.get("battery_soc") or bat.get("soc"))

        return {
            "soc_percent": soc,
            "pv_kw": pv_kw or 0.0,
            "grid_kw": grid_kw or 0.0,  # +Bezug / -Einspeisung
            "verbrauch_kw": verbrauch or 0.0,
            "battery_power_kw": battery_power or 0.0,  # +Laden / -Entladen
            "house_kw": house_kw or 0.0,
            "wohnhaus_kw": house_kw or 0.0,
            "hydro_kw": hydro_kw or 0.0,
            "miner_status": _combined_device_status(),
            "aktueller_verbrauch_kw": compute_aktueller_verbrauch_kw(base, battery_power),
        }
    except Exception as e:
        return {"error": str(e), "soc_percent": None, "pv_kw": 0.0, "miner_status": {}}


def _combined_device_status() -> Dict[str, str]:
    """Miner-SSH-Status + HA-Steckdosen/Schalter (entity_id als Schlüssel)."""
    from dashboard_miner_status import load_miner_status_map

    status = dict(load_miner_status_map())
    try:
        from device_registry import get_ha_device_status_map

        status.update(get_ha_device_status_map())
    except Exception:
        pass
    return status


def get_actual_pv_kwh_today(base_dir: Optional[str] = None) -> float:
    base = base_dir or str(BACKEND_DIR)
    try:
        from energy_stats_24h import get_stats_24h

        stats = get_stats_24h(base)
        gen = stats.get("generation") or stats.get("generation_kwh") or {}
        return float(gen.get("pv", 0) or 0)
    except Exception:
        from control_algorithm.paths import energy_file

        path = energy_file(base, "energy_stats_24h.json")
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                g = data.get("generation_kwh") or {}
                return float(g.get("pv", 0) or 0)
            except Exception:
                pass
    return 0.0


def load_miner_credentials(ip: str, miners: List[Dict[str, Any]]) -> Dict[str, str]:
    for m in miners:
        if m.get("ip") == ip:
            return {
                "username": m.get("username", "root"),
                "password": m.get("password", ""),
            }
    return {"username": "root", "password": ""}
