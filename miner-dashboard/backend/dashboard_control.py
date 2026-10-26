#!/usr/bin/env python3
"""Steuerungswerte aus dem Dashboard-Stromfluss (eine Quelle für Anzeige + Automatik)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

BACKEND_DIR = Path(__file__).resolve().parent
EM_DIR = BACKEND_DIR / "energy_monitor"

# Rolle → Datenquelle (wie in Statistik-Konfiguration)
CONTROL_BINDINGS: Dict[str, str] = {
    "grid_kw": "field.kelag",
    "battery_soc": "flow.battery_soc",
    "battery_power_kw": "flow.battery_power",
    "verbrauch_kw": "flow.verbrauch_gesamt",
    "pv_kw": "field.pv",
    "wohnhaus_kw": "field.wohnhaus",
    "betrieb_kw": "flow.betrieb_anzeige",
    "hydro_kw": "field.wasserkraft",
}

# Sichtbare Stromfluss-Felder → Steuerungsrolle (für API-Dokumentation)
FLOW_KEY_TO_ROLE: Dict[str, str] = {
    "pv": "pv_kw",
    "wasserkraft": "hydro_kw",
    "kelag": "grid_kw",
    "wohnhaus": "wohnhaus_kw",
    "betrieb": "betrieb_kw",
    "battery": "battery_soc",
}

CONTROL_ROLE_LABELS: Dict[str, str] = {
    "grid_kw": "Netz / Kelag (Automatik-Schwelle)",
    "battery_soc": "Batterie Ladezustand %",
    "battery_power_kw": "Batterie Leistung kW",
    "verbrauch_kw": "Gesamtverbrauch kW",
    "pv_kw": "PV-Erzeugung kW",
    "wohnhaus_kw": "Wohnhaus-Verbrauch kW",
    "betrieb_kw": "Betriebsverbrauch kW",
    "hydro_kw": "Wasserkraft kW",
}


def _read_txt_kw(filename: str) -> Optional[float]:
    path = EM_DIR / filename
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw or raw == "n/a":
            return None
        return float(str(raw).replace(",", "."))
    except Exception:
        return None


def _build_flow_context(base_dir: Optional[str] = None) -> Dict[str, Any]:
    base = base_dir or str(BACKEND_DIR)
    try:
        from dashboard_battery import load_battery_data, load_battery_soc
        from miner_flow_data import build_flow_response

        bat = load_battery_data()
        flow = build_flow_response(base, load_battery_soc, bat.get("power_kw"))
        flow["battery_soc"] = bat.get("soc") or flow.get("battery_soc")
        flow["battery_power_kw"] = bat.get("power_kw")
        return {"flow": flow, "battery_kw": bat.get("power_kw"), "battery_soc": _parse_soc(flow.get("battery_soc"))}
    except Exception:
        return {"flow": {}, "battery_kw": None, "battery_soc": None}


def _parse_soc(raw: Any) -> Optional[float]:
    if raw in (None, "", "n/a"):
        return None
    try:
        return float(str(raw).replace("%", "").replace(",", ".").strip())
    except ValueError:
        return None


def _role_visible(role: str, dash_cfg: Dict[str, Any]) -> bool:
    from dashboard_config import hidden_builtins

    hidden = hidden_builtins(dash_cfg)
    mapping = {
        "grid_kw": "kelag",
        "pv_kw": "pv",
        "wohnhaus_kw": "wohnhaus",
        "betrieb_kw": "betrieb",
        "hydro_kw": "wasserkraft",
        "battery_soc": "battery",
        "battery_power_kw": "battery",
    }
    key = mapping.get(role)
    if key and key in hidden:
        return False
    return True


def get_control_values(base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Alle Steuerungswerte aus den im Dashboard sichtbaren Quellen."""
    from dashboard_config import load_config as load_dash_config
    from dashboard_sources import resolve_source_value

    ctx = _build_flow_context(base_dir)
    flow = ctx["flow"]
    dash_cfg = load_dash_config()

    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for role, source in CONTROL_BINDINGS.items():
        if not _role_visible(role, dash_cfg):
            values[role] = None
            continue
        val = resolve_source_value(
            source,
            flow=flow,
            dash_cfg=dash_cfg,
            battery_kw=ctx.get("battery_kw"),
            battery_soc=ctx.get("battery_soc"),
        )
        values[role] = val
        sources[role] = source

    # Sichtbare eigene Felder
    custom: Dict[str, Any] = {}
    for field in dash_cfg.get("custom_flow_fields") or []:
        if not field.get("enabled", True):
            continue
        src = f"custom.{field['id']}"
        val = resolve_source_value(
            src,
            flow=flow,
            dash_cfg=dash_cfg,
            battery_kw=ctx.get("battery_kw"),
            battery_soc=ctx.get("battery_soc"),
        )
        custom[field["id"]] = {
            "name": field.get("name"),
            "value_kw": val,
            "source": src,
        }

    return {
        "values": values,
        "sources": sources,
        "custom_fields": custom,
        "flow_hidden": list(dash_cfg.get("flow_hidden_builtins") or []),
        "bindings": [
            {
                "role": role,
                "label": CONTROL_ROLE_LABELS.get(role, role),
                "source": source,
                "value": values.get(role),
                "visible": _role_visible(role, dash_cfg),
            }
            for role, source in CONTROL_BINDINGS.items()
        ],
    }


def get_grid_kw_for_control(base_dir: Optional[str] = None) -> Optional[float]:
    """Netz/Kelag für Energie-Automatik (ersetzt direktes get_kelag_value)."""
    return resolve_control_source_kw(base_dir, "field.kelag")


def resolve_control_source_kw(
    base_dir: Optional[str] = None, source: str = "field.kelag"
) -> Optional[float]:
    """Beliebige Dashboard-Quelle (wie Kreisdiagramm) als kW-Wert."""
    from dashboard_config import load_config as load_dash_config
    from dashboard_sources import resolve_source_value

    ctx = _build_flow_context(base_dir)
    flow = ctx["flow"]
    dash_cfg = load_dash_config()
    val = resolve_source_value(
        source,
        flow=flow,
        dash_cfg=dash_cfg,
        battery_kw=ctx.get("battery_kw"),
        battery_soc=ctx.get("battery_soc"),
    )
    if val is None and source == "field.kelag":
        try:
            from ha_entity_mapping import load_config
            from energy_monitor.ha_live_reader import get_kelag_value

            return get_kelag_value(load_config())
        except Exception:
            return _read_txt_kw("energy_value.txt")
    if val is None:
        return None
    return float(val)


def resolve_control_source_soc(
    base_dir: Optional[str] = None, source: str = "flow.battery_soc"
) -> Optional[float]:
    """Beliebige Dashboard-Quelle als Batterie-SOC (%)."""
    from dashboard_sources import resolve_source_value

    ctx = _build_flow_context(base_dir)
    flow = ctx["flow"]
    val = resolve_source_value(
        source,
        flow=flow,
        battery_kw=ctx.get("battery_kw"),
        battery_soc=ctx.get("battery_soc"),
    )
    if val is None:
        if source == "flow.battery_soc":
            try:
                from dashboard_battery import load_battery_soc

                raw = load_battery_soc()
                if raw not in (None, "", "n/a"):
                    return _parse_soc(raw)
            except Exception:
                pass
        return None
    soc = _parse_soc(val)
    if soc is not None and 0 <= soc <= 100:
        return soc
    return None
