#!/usr/bin/env python3
"""Home-Assistant-Entitätszuordnung für Energy Monitor und Dashboard-Sichtbarkeit."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
CONFIG_PATHS = (
    BACKEND_DIR.parent.parent / "config" / "ha_entity_mapping.json",
    BACKEND_DIR / "ha_entity_mapping.json",
)

HA_SNAPSHOT_PATH = Path("/tmp/ha_snapshot.json")

FIELD_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "kelag": {
        "label": "Kelag / Netz",
        "hint": "Netzleistung für Automatik und Stromfluss (kW, + Bezug / − Einspeisung)",
        "value_type": "power_kw",
        "flow_key": "grid",
        "pie_consumption": "netz",
        "pie_consumption_alt": "verkauf",
        "output_files": ["energy_value.txt", "kelag_value.txt"],
    },
    "pv": {
        "label": "PV Gesamt",
        "hint": "Photovoltaik-Leistung",
        "value_type": "power_kw",
        "flow_key": "pv",
        "pie_generation": "pv",
        "output_files": ["pv_value.txt"],
    },
    "wasserkraft": {
        "label": "Wasserkraft",
        "hint": "Erzeugung Wasserkraft",
        "value_type": "power_kw",
        "flow_key": "hydro",
        "pie_generation": "hydro",
        "output_files": ["wasserkraft_value.txt"],
    },
    "wohnhaus": {
        "label": "Wohnhaus",
        "hint": "Haushaltsverbrauch",
        "value_type": "power_kw",
        "flow_key": "haus",
        "pie_consumption": "haus",
        "output_files": ["wohnhaus_value.txt"],
    },
    "betrieb": {
        "label": "Betrieb",
        "hint": "Betriebsverbrauch (sonst berechnet aus Gesamt − Wohnhaus)",
        "value_type": "power_kw",
        "flow_key": "betrieb",
        "pie_consumption": "betrieb",
        "output_files": ["betrieb_value.txt", "betrieb_anzeige_value.txt"],
    },
    "bezug": {
        "label": "Bezug",
        "hint": "Netzbezug (optional, nur Daten)",
        "value_type": "power_kw",
        "output_files": ["bezug_value.txt"],
    },
    "lieferung": {
        "label": "Lieferung / Einspeisung",
        "hint": "Einspeisung ins Netz (optional)",
        "value_type": "power_kw",
        "output_files": ["lieferung_value.txt"],
    },
    "erzeugung_gesamt": {
        "label": "Erzeugung Gesamt",
        "hint": "Summe aller Erzeuger (optional)",
        "value_type": "power_kw",
        "output_files": ["erzeugung_gesamt_value.txt"],
    },
    "eigenverbrauch": {
        "label": "Eigenverbrauch",
        "hint": "Eigenverbrauch (optional)",
        "value_type": "power_kw",
        "output_files": ["eigenverbrauch_value.txt"],
    },
    "betriebsverbrauch": {
        "label": "Betriebsverbrauch",
        "hint": "Betriebsverbrauch gesamt (optional)",
        "value_type": "power_kw",
        "output_files": ["betriebsverbrauch_value.txt"],
    },
    "battery_soc": {
        "label": "Batterie SOC",
        "hint": "Ladestand in %",
        "value_type": "percent",
        "flow_key": "battery",
        "output_files": [],
    },
    "battery_power": {
        "label": "Batterie Leistung",
        "hint": "Lade-/Entladeleistung (+ laden, − entladen)",
        "value_type": "power_kw_signed",
        "flow_key": "battery",
        "pie_generation_charge": "ladung",
        "pie_generation_discharge": "entladung",
        "output_files": [],
    },
}


def normalize_sensors(entry: Dict[str, Any]) -> List[Dict[str, str]]:
    """Sensorenliste aus Konfiguration (Legacy entity_id → ein Sensor mit +)."""
    sensors = entry.get("sensors")
    if isinstance(sensors, list) and sensors:
        result: List[Dict[str, str]] = []
        for item in sensors:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip()
            if not entity_id:
                continue
            sign = str(item.get("sign") or "+").strip()
            if sign not in ("+", "-"):
                sign = "+"
            result.append({"entity_id": entity_id, "sign": sign})
        return result
    entity_id = str(entry.get("entity_id") or "").strip()
    if entity_id:
        return [{"entity_id": entity_id, "sign": "+"}]
    return []


def normalize_field_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    sensors = normalize_sensors(entry)
    return {
        "enabled": bool(entry.get("enabled")),
        "entity_id": sensors[0]["entity_id"] if sensors else "",
        "sensors": sensors,
    }


def _default_fields() -> Dict[str, Dict[str, Any]]:
    defaults = {
        "kelag": {
            "enabled": True,
            "entity_id": "sensor.smart_meter_50ka_3_wirkleistung",
            "sensors": [],
        },
        "pv": {
            "enabled": True,
            "entity_id": "sensor.solarnet_pv_leistung",
            "sensors": [],
        },
        "wasserkraft": {"enabled": False, "entity_id": "", "sensors": []},
        "wohnhaus": {"enabled": False, "entity_id": "", "sensors": []},
        "betrieb": {"enabled": True, "entity_id": "", "sensors": []},
        "bezug": {"enabled": False, "entity_id": "", "sensors": []},
        "lieferung": {"enabled": False, "entity_id": "", "sensors": []},
        "erzeugung_gesamt": {"enabled": False, "entity_id": "", "sensors": []},
        "eigenverbrauch": {"enabled": False, "entity_id": "", "sensors": []},
        "betriebsverbrauch": {"enabled": False, "entity_id": "", "sensors": []},
        "battery_soc": {"enabled": False, "entity_id": "", "sensors": []},
        "battery_power": {"enabled": False, "entity_id": "", "sensors": []},
    }
    return defaults


def default_config() -> Dict[str, Any]:
    return {
        "source": "home_assistant",
        "updated_at": None,
        "fields": _default_fields(),
    }


def config_path() -> Path:
    for path in CONFIG_PATHS:
        if path.is_file():
            return path
    return CONFIG_PATHS[0]


def load_config() -> Dict[str, Any]:
    path = config_path()
    cfg = default_config()
    if not path.is_file():
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return cfg
        cfg.update({k: v for k, v in data.items() if k != "fields"})
        # Gespeicherte Felder aus der Datei (nicht aus default_config)
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        merged_fields: Dict[str, Dict[str, Any]] = {}
        for key, default in _default_fields().items():
            entry = fields.get(key) if isinstance(fields.get(key), dict) else {}
            merged = dict(default)
            merged.update(entry)
            merged_fields[key] = normalize_field_entry(merged)
        cfg["fields"] = merged_fields
    except Exception:
        pass
    return cfg


def save_config(cfg: Dict[str, Any]) -> Path:
    path = CONFIG_PATHS[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    out = deepcopy(cfg)
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def field_is_enabled(cfg: Dict[str, Any], key: str) -> bool:
    fields = cfg.get("fields") or {}
    entry = fields.get(key) or {}
    return bool(entry.get("enabled"))


def get_visibility(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """Dashboard-Sichtbarkeit für Stromfluss und Kreisdiagramme."""
    cfg = cfg or load_config()
    fields = cfg.get("fields") or {}
    battery_on = field_is_enabled(cfg, "battery_soc") or field_is_enabled(
        cfg, "battery_power"
    )
    return {
        "pv": field_is_enabled(cfg, "pv"),
        "hydro": field_is_enabled(cfg, "wasserkraft"),
        "grid": field_is_enabled(cfg, "kelag"),
        "haus": field_is_enabled(cfg, "wohnhaus"),
        "betrieb": field_is_enabled(cfg, "betrieb"),
        "battery": battery_on,
    }


def list_ha_entities(snapshot: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Alle HA-Entitäten für Dropdowns, sortiert nach Gerät."""
    if snapshot is None:
        if not HA_SNAPSHOT_PATH.is_file():
            return []
        try:
            snapshot = json.loads(HA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    items: List[Dict[str, Any]] = []
    devices_map = snapshot.get("devices") or {}
    name_counts: Dict[str, int] = {}
    for device in devices_map.values():
        dn = device.get("name") or ""
        name_counts[dn] = name_counts.get(dn, 0) + 1

    def device_display_name(device: Dict[str, Any]) -> str:
        name = device.get("name") or "—"
        if name_counts.get(name, 0) > 1:
            short_id = (device.get("id") or "")[:8]
            return f"{name} ({short_id})" if short_id else name
        return name

    def add_entity(entity_id: str, entity: Dict[str, Any], device_name: str) -> None:
        state = entity.get("state")
        attrs = entity.get("attributes") or {}
        state_str = "" if state is None else str(state)
        items.append(
            {
                "entity_id": entity_id,
                "name": entity.get("name") or attrs.get("friendly_name") or entity_id,
                "device": device_name,
                "state": state_str,
                "unit": entity.get("unit_of_measurement")
                or attrs.get("unit_of_measurement")
                or "",
                "device_class": entity.get("device_class")
                or attrs.get("device_class"),
                "available": state_str not in ("unavailable", "unknown", ""),
            }
        )

    for device in devices_map.values():
        device_name = device_display_name(device)
        for entity_id, entity in (device.get("entities") or {}).items():
            add_entity(entity_id, entity, device_name)

    for entity_id, entity in (snapshot.get("unassigned_entities") or {}).items():
        add_entity(entity_id, entity, "—")

    items.sort(key=lambda x: (x["device"].lower(), x["name"].lower(), x["entity_id"]))
    return items


CONTROLLABLE_DOMAINS = frozenset(
    {
        "switch",
        "light",
        "input_boolean",
        "fan",
        "input_button",
        "cover",
        "siren",
        "valve",
        "lock",
        "media_player",
    }
)


def is_controllable_entity(entity_id: str, state: str = "") -> bool:
    """Schaltbare Domain – unabhängig vom aktuellen Zustand (wie Dashboard-Liste)."""
    domain = (entity_id or "").split(".", 1)[0]
    return domain in CONTROLLABLE_DOMAINS


def format_flow_entity_label(ent: Dict[str, Any]) -> str:
    """Label wie Stromfluss-Dashboard: Gerät · Name (Zustand Einheit)."""
    device = ent.get("device") or "—"
    name = ent.get("name") or ent.get("entity_id") or "—"
    state = ent.get("state") or "—"
    unit = ent.get("unit") or ""
    return f"{device} · {name} ({state}{(' ' + unit) if unit else ''})"


def format_entity_option_label(ent: Dict[str, Any]) -> str:
    """Anzeige-Label für Dropdowns (eindeutig, analog Stromfluss-Konfiguration)."""
    device = ent.get("device") or "—"
    name = ent.get("name") or ent.get("entity_id") or "—"
    state = ent.get("state") or "—"
    unit = ent.get("unit") or ""
    state_part = f"{state}{(' ' + unit) if unit else ''}"
    entity_id = ent.get("entity_id") or ""
    short_id = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    return f"{device} · {name} · {short_id} ({state_part})"


def list_controllable_entities(snapshot: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Schaltbare HA-Entitäten für Geräte hinzufügen (gleiche Quelle wie Dashboard)."""
    if snapshot is None:
        if not HA_SNAPSHOT_PATH.is_file():
            return []
        try:
            snapshot = json.loads(HA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    seen: set[str] = set()
    result: List[Dict[str, Any]] = []
    for ent in list_ha_entities(snapshot):
        entity_id = ent.get("entity_id", "")
        if not entity_id or entity_id in seen:
            continue
        if not is_controllable_entity(entity_id, ent.get("state", "")):
            continue
        seen.add(entity_id)
        domain = entity_id.split(".", 1)[0]
        item = dict(ent)
        item["domain"] = domain
        item["label"] = format_entity_option_label(item)
        result.append(item)

    result.sort(key=lambda x: (x["device"].lower(), x["name"].lower(), x["entity_id"]))
    return result


def snapshot_updated_at(snapshot: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if snapshot is None:
        if not HA_SNAPSHOT_PATH.is_file():
            return None
        try:
            snapshot = json.loads(HA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return snapshot.get("updated_at")


def _parse_sensors_from_entry(entry: Dict[str, Any]) -> List[Dict[str, str]]:
    sensors_in = entry.get("sensors")
    if isinstance(sensors_in, list):
        return normalize_sensors({"sensors": sensors_in})
    entity_id = str(entry.get("entity_id") or "").strip()
    if entity_id:
        return [{"entity_id": entity_id, "sign": "+"}]
    return []


def parse_form_to_config(form) -> Dict[str, Any]:
    cfg = load_config()
    fields = cfg.setdefault("fields", {})
    for key in FIELD_DEFINITIONS:
        enabled_raw = form.get(f"ha_enabled_{key}", "0")
        if isinstance(enabled_raw, list):
            enabled_raw = enabled_raw[-1]
        entity_raw = form.get(f"ha_entity_{key}", "")
        if isinstance(entity_raw, list):
            entity_raw = entity_raw[-1]
        entity_id = str(entity_raw or "").strip()
        sensors = [{"entity_id": entity_id, "sign": "+"}] if entity_id else []
        fields[key] = normalize_field_entry(
            {"enabled": str(enabled_raw).strip() in ("1", "true", "on", "yes"), "sensors": sensors}
        )
    cfg["source"] = "home_assistant"
    return cfg


def parse_json_to_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Felder aus JSON-Body (API-Speichern)."""
    cfg = load_config()
    fields_in = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    fields = cfg.setdefault("fields", {})
    for key in FIELD_DEFINITIONS:
        entry = fields_in.get(key) if isinstance(fields_in.get(key), dict) else {}
        sensors = _parse_sensors_from_entry(entry)
        fields[key] = normalize_field_entry(
            {"enabled": bool(entry.get("enabled")), "sensors": sensors}
        )
    cfg["source"] = "home_assistant"
    return cfg


def compute_field_value(
    entry: Dict[str, Any],
    meta: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Optional[float]:
    """Summiert alle zugeordneten Sensoren mit +/- Vorzeichen."""
    from energy_monitor.ha_live_reader import (
        _find_entity,
        parse_entity_percent,
        parse_entity_power_kw,
    )

    sensors = normalize_sensors(entry)
    if not sensors:
        return None

    value_type = meta.get("value_type", "power_kw")
    if value_type == "power_kw_signed":
        parser = lambda e: parse_entity_power_kw(e, signed=True)
    elif value_type == "percent":
        parser = parse_entity_percent
    else:
        parser = lambda e: parse_entity_power_kw(e, signed=False)

    total = 0.0
    has_any = False
    for sensor in sensors:
        entity = _find_entity(snapshot, sensor["entity_id"])
        val = parser(entity)
        if val is None:
            continue
        has_any = True
        if sensor["sign"] == "-":
            total -= float(val)
        else:
            total += float(val)

    if not has_any:
        return None
    if value_type == "percent":
        return round(total, 1)
    return round(total, 3)


def live_values_for_mapping(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Aktuelle Live-Werte für die Zuordnungs-UI."""
    from energy_monitor.ha_live_reader import (
        _find_entity,
        load_snapshot,
        parse_entity_percent,
        parse_entity_power_kw,
    )

    cfg = cfg or load_config()
    try:
        snapshot = load_snapshot()
    except Exception:
        snapshot = {}

    type_parsers = {
        "power_kw": lambda e: parse_entity_power_kw(e, signed=False),
        "power_kw_signed": lambda e: parse_entity_power_kw(e, signed=True),
        "percent": parse_entity_percent,
    }

    out: Dict[str, Any] = {}
    for key, meta in FIELD_DEFINITIONS.items():
        entry = normalize_field_entry((cfg.get("fields") or {}).get(key) or {})
        sensors = entry.get("sensors") or []
        parser = type_parsers.get(meta.get("value_type", "power_kw"), parse_entity_power_kw)

        sensor_details: List[Dict[str, Any]] = []
        for sensor in sensors:
            entity = _find_entity(snapshot, sensor["entity_id"]) if snapshot else None
            val = parser(entity) if entity else None
            sensor_details.append(
                {
                    "entity_id": sensor["entity_id"],
                    "sign": sensor["sign"],
                    "live_value": val,
                }
            )

        live_value = None
        if snapshot and entry.get("enabled") and sensors:
            try:
                live_value = compute_field_value(entry, meta, snapshot)
            except Exception:
                live_value = None

        out[key] = {
            "enabled": bool(entry.get("enabled")),
            "entity_id": entry.get("entity_id") or "",
            "sensors": sensor_details,
            "live_value": live_value,
            "label": meta["label"],
            "value_type": meta.get("value_type", "power_kw"),
        }
    return out
