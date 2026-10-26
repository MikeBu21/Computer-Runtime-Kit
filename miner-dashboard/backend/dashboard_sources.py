#!/usr/bin/env python3
"""Auflösung von Datenquellen für Stromfluss, Kreis- und Liniendiagramme."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from energy_stats_24h import _battery_entladung_kw, _battery_ladung_kw, _grid_bezug_kw, _grid_verkauf_kw, _parse_kw, _parse_kw_signed

FIELD_TO_FLOW_KEY: Dict[str, str] = {
    "pv": "pv",
    "wasserkraft": "hydro",
    "kelag": "grid",
    "wohnhaus": "haus",
    "betrieb": "betrieb_anzeige",
}

FIELD_TXT_FILES: Dict[str, list] = {
    "pv": ["pv_value.txt"],
    "wasserkraft": ["wasserkraft_value.txt"],
    "kelag": ["energy_value.txt", "kelag_value.txt"],
    "wohnhaus": ["wohnhaus_value.txt"],
    "betrieb": ["betrieb_value.txt", "betrieb_anzeige_value.txt"],
}


def _read_field_txt(field_key: str) -> Optional[float]:
    from pathlib import Path

    base_dir = str(Path(__file__).resolve().parent)
    signed = field_key == "kelag"
    for name in FIELD_TXT_FILES.get(field_key, []):
        raw = ""
        try:
            from ram_cache import is_enabled, read_text

            if is_enabled():
                raw = (
                    read_text(base_dir, name, default="", scope="energy_monitor") or ""
                ).strip()
        except Exception:
            raw = ""
        if not raw:
            path = Path(base_dir) / "energy_monitor" / name
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
        if not raw or raw == "n/a":
            continue
        try:
            return _parse_kw_signed(raw) if signed else _parse_kw(raw)
        except Exception:
            continue
    return None


def _custom_field_value(field: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
    from ha_entity_mapping import compute_field_value

    meta = {"value_type": "power_kw"}
    if not snapshot:
        return None
    return compute_field_value(
        {"enabled": True, "sensors": field.get("sensors") or []},
        meta,
        snapshot,
    )


def _load_ha_snapshot() -> Optional[Dict[str, Any]]:
    try:
        from energy_monitor.ha_live_reader import load_snapshot

        return load_snapshot()
    except Exception:
        return None


def _field_live_kw(field_key: str, cfg: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
    from ha_entity_mapping import FIELD_DEFINITIONS, compute_field_value, load_config, normalize_field_entry

    ha_cfg = cfg if cfg.get("fields") else load_config()
    entry = normalize_field_entry((ha_cfg.get("fields") or {}).get(field_key) or {})
    if not entry.get("enabled") or not entry.get("sensors"):
        return None
    if not snapshot:
        return None
    meta = FIELD_DEFINITIONS.get(field_key, {"value_type": "power_kw"})
    return compute_field_value(entry, meta, snapshot)


def _field_live_percent(field_key: str, cfg: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
    from ha_entity_mapping import FIELD_DEFINITIONS, compute_field_value, load_config, normalize_field_entry

    ha_cfg = cfg if cfg.get("fields") else load_config()
    entry = normalize_field_entry((ha_cfg.get("fields") or {}).get(field_key) or {})
    if not entry.get("enabled") or not entry.get("sensors"):
        return None
    if not snapshot:
        return None
    meta = FIELD_DEFINITIONS.get(field_key, {"value_type": "percent"})
    return compute_field_value(entry, meta, snapshot)


def resolve_source_value(
    source: str,
    *,
    flow: Dict[str, Any],
    ha_cfg: Optional[Dict[str, Any]] = None,
    dash_cfg: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    battery_kw: Optional[float] = None,
    battery_soc: Optional[float] = None,
) -> Optional[float]:
    """Liefert numerischen Wert für kW oder % je nach Quelle."""
    from dashboard_config import load_config as load_dash_config
    from ha_entity_mapping import load_config as load_ha_config

    ha_cfg = ha_cfg or load_ha_config()
    dash_cfg = dash_cfg or load_dash_config()
    src = str(source or "").strip()
    if snapshot is None and src.startswith(("field.", "custom.")):
        snapshot = _load_ha_snapshot()

    if src.startswith("field."):
        key = src[6:]
        meta = __import__("ha_entity_mapping", fromlist=["FIELD_DEFINITIONS"]).FIELD_DEFINITIONS.get(key, {})
        if meta.get("value_type") == "percent":
            return _field_live_percent(key, ha_cfg, snapshot)
        val = _field_live_kw(key, ha_cfg, snapshot)
        if val is not None:
            return val
        flow_key = FIELD_TO_FLOW_KEY.get(key)
        if flow_key:
            raw = flow.get(flow_key)
            if raw is not None and raw != "n/a":
                if meta.get("value_type") == "power_kw_signed":
                    return _parse_kw_signed(raw)
                return _parse_kw(raw)
        txt_val = _read_field_txt(key)
        if txt_val is not None:
            return txt_val
        return None

    if src.startswith("custom."):
        custom_id = src[7:]
        for field in dash_cfg.get("custom_flow_fields") or []:
            if field.get("id") == custom_id and field.get("enabled"):
                return _custom_field_value(field, snapshot)
        return None

    if src.startswith("flow."):
        key = src[5:]
        if key == "battery_soc":
            if battery_soc is not None:
                return float(battery_soc)
            raw = flow.get("battery_soc")
            if raw in (None, "", "n/a"):
                return None
            try:
                return float(str(raw).replace("%", "").replace(",", ".").strip())
            except ValueError:
                return None
        if key == "battery_power":
            if battery_kw is not None:
                return float(battery_kw)
            return flow.get("battery_power_kw")
        raw = flow.get(key)
        if raw is None and key == "grid":
            raw = flow.get("kelag")
        if key in ("grid", "kelag"):
            return _parse_kw_signed(raw)
        return _parse_kw(raw)

    if src == "computed.erzeugung":
        pv = resolve_source_value("field.pv", flow=flow, ha_cfg=ha_cfg, snapshot=snapshot)
        hydro = resolve_source_value("field.wasserkraft", flow=flow, ha_cfg=ha_cfg, snapshot=snapshot)
        if pv is None and hydro is None:
            pv = _parse_kw(flow.get("pv"))
            hydro = _parse_kw(flow.get("hydro"))
            if pv <= 0 and hydro <= 0:
                return None
        return round((pv or 0.0) + (hydro or 0.0), 3)

    if src == "computed.grid_bezug":
        # Eigener Bezug-Sensor hat Vorrang vor signed Kelag-Netto
        bezug = resolve_source_value(
            "field.bezug", flow=flow, ha_cfg=ha_cfg, snapshot=snapshot
        )
        if bezug is not None:
            return max(0.0, float(bezug))
        grid = resolve_source_value("field.kelag", flow=flow, ha_cfg=ha_cfg, snapshot=snapshot)
        if grid is None:
            grid = _parse_kw_signed(flow.get("grid"))
        return _grid_bezug_kw(grid)

    if src == "computed.grid_verkauf":
        # Eigene Einspeisung hat Vorrang vor negativem Kelag-Netto
        lieferung = resolve_source_value(
            "field.lieferung", flow=flow, ha_cfg=ha_cfg, snapshot=snapshot
        )
        if lieferung is not None:
            return max(0.0, float(lieferung))
        grid = resolve_source_value("field.kelag", flow=flow, ha_cfg=ha_cfg, snapshot=snapshot)
        if grid is None:
            grid = _parse_kw_signed(flow.get("grid"))
        return _grid_verkauf_kw(grid)

    if src == "computed.battery_ladung":
        p = battery_kw
        if p is None:
            p = flow.get("battery_power_kw")
        return _battery_ladung_kw(p)

    if src == "computed.battery_entladung":
        p = battery_kw
        if p is None:
            p = flow.get("battery_power_kw")
        return _battery_entladung_kw(p)

    return None


def resolve_custom_flow_values(
    dash_cfg: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for field in dash_cfg.get("custom_flow_fields") or []:
        if not field.get("enabled"):
            continue
        val = _custom_field_value(field, snapshot)
        out[field["id"]] = {
            "id": field["id"],
            "name": field.get("name"),
            "icon": field.get("icon", "⚡"),
            "value_kw": val,
            "display": f"{val:.3f}" if val is not None else "n/a",
        }
    return out


def integration_kw_for_pie(source: str, flow: Dict[str, Any], battery_kw: Optional[float]) -> float:
    """kW-Wert für kWh-Akkumulation."""
    if source in ("flow.battery_soc", "field.battery_soc"):
        return 0.0
    val = resolve_source_value(
        source,
        flow=flow,
        battery_kw=battery_kw,
        battery_soc=flow.get("battery_soc") if isinstance(flow.get("battery_soc"), (int, float)) else None,
    )
    if val is None:
        return 0.0
    # Netz/Kelag: Bezug und Einspeisung getrennt über computed.* — Feldwert kann negativ sein
    if source == "field.kelag":
        return abs(float(val))
    return max(0.0, float(val))


def build_pie_response(
    chart_kwh: Dict[str, float],
    slices: List[Dict[str, Any]],
) -> Dict[str, Any]:
    labels: List[str] = []
    values: List[float] = []
    colors: List[str] = []
    items: List[Dict[str, Any]] = []
    for sl in slices:
        if not sl.get("enabled"):
            continue
        sid = sl["id"]
        val = round(float(chart_kwh.get(sid, 0.0)), 2)
        labels.append(sl.get("label") or sid)
        values.append(val)
        colors.append(sl.get("color") or "#888888")
        items.append({"id": sid, "label": sl.get("label"), "value": val, "color": sl.get("color")})
    total = round(sum(values), 2)
    return {"labels": labels, "values": values, "colors": colors, "items": items, "total": total}
