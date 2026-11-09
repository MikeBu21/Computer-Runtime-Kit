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

        role_sums = flow.get("role_sums") if isinstance(flow.get("role_sums"), dict) else {}
        roles = flow.get("flow_point_roles") if isinstance(flow.get("flow_point_roles"), dict) else {}

        pv_raw = _parse_float(flow.get("pv")) or 0.0
        hydro_raw = _parse_float(flow.get("hydro") or flow.get("wasserkraft")) or 0.0
        grid_raw = _parse_float(flow.get("grid")) or 0.0
        battery_power = bat.get("power_kw")
        soc = _parse_float(flow.get("battery_soc") or bat.get("soc"))

        erzeugung_kw = _parse_float(role_sums.get("erzeugung"))
        if erzeugung_kw is None:
            erzeugung_kw = pv_raw + hydro_raw

        # Zusatz-Produzenten (Rolle Erzeugung) in pv_kw falten, damit bestehende
        # pv_kw-/pv+hydro-Pfade die Rollensumme sehen, ohne Hydro-Forecast zu brechen.
        hydro_in_erz = hydro_raw if roles.get("wasserkraft", "erzeugung") == "erzeugung" else 0.0
        pv_in_erz = pv_raw if roles.get("pv", "erzeugung") == "erzeugung" else 0.0
        extra_erz = float(erzeugung_kw) - pv_in_erz - hydro_in_erz
        pv_kw = pv_in_erz + max(0.0, extra_erz)
        hydro_kw = hydro_in_erz

        grid_kw = _parse_float(role_sums.get("netz"))
        if grid_kw is None:
            grid_kw = grid_raw

        verbrauch = _parse_float(flow.get("verbrauch_gesamt"))
        if verbrauch is None:
            verbrauch = _parse_float(role_sums.get("verbraucher"))
        # Hauslast = Summe Rolle Verbraucher (früher nur Wohnhaus)
        house_kw = _parse_float(role_sums.get("verbraucher"))
        if house_kw is None:
            house_kw = _parse_float(flow.get("haus") or flow.get("wohnhaus"))
        if verbrauch is None:
            verbrauch = house_kw

        bat_role = _parse_float(role_sums.get("speicher"))
        if bat_role is not None:
            battery_power = bat_role

        return {
            "soc_percent": soc,
            "pv_kw": pv_kw or 0.0,
            "grid_kw": grid_kw or 0.0,  # +Bezug / -Einspeisung
            "verbrauch_kw": verbrauch or 0.0,
            "battery_power_kw": battery_power or 0.0,  # +Laden / -Entladen
            "house_kw": house_kw or 0.0,
            "wohnhaus_kw": house_kw or 0.0,
            "hydro_kw": hydro_kw or 0.0,
            "erzeugung_kw": float(erzeugung_kw or 0.0),
            "role_sums": role_sums,
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
