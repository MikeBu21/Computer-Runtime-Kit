# -*- coding: utf-8 -*-
"""BMS-Netzbezug: Live-Anzeige, Rechnung und optionales Modbus-Schreiben.

Das Häkchen „Ins Register schreiben“ schreibt den berechneten Wert in das
Register des gewählten Typs. Ausgelassen bleibt es eine reine Anzeige.
Register stammen aus den Solarman-Dateien (Deye) und der Fronius-SunSpec-Anleitung.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BACKEND_DIR / "bms_test_settings.json"
HA_CONFIG_ENTRIES = BACKEND_DIR.parents[1] / "homeassistant" / "config" / ".storage" / "core.config_entries"
HA_DEVICE_REGISTRY = BACKEND_DIR.parents[1] / "homeassistant" / "config" / ".storage" / "core.device_registry"
LOG_FILE = BACKEND_DIR / "bms_test_log.json"
DAUERZUKAUF_TEST_FILE = BACKEND_DIR / "dauerzukauf_test.json"
TZ = ZoneInfo("Europe/Vienna")

SOC_EMPTY = 10.0
SOC_FULL = 98.0
BATTERY_LIMIT_KW = 12.0
LOG_LIMIT = 30
SAMPLE_GAP_SEC = 15

# Deye-Adressen aus den Solarman-Definitionen, Fronius aus der GEN24-Anleitung
# (float): WMaxLimPct ist dort Register 40242, Basic Storage Control beginnt bei
# 40353. OutWRte/InWRte sind SunSpec-Modell 124, Offset 12 und 13.
INVERTER_LIST: List[Dict[str, Any]] = [
    {
        "id": "deye_hybrid",
        "label": "Deye Hybrid Niedervolt",
        "detail": "mein DE",
        "register": 0x00CE,
        "register_name": "Zero Export power",
        "writable": True,
        "encoding": "watts",
        "signed": True,
        "minimum": -20,
        "maximum": 12000,
        "note": "Soll-Netzbezug in Watt. Am dreiphasigen Niedervolt gilt Register 0x0068.",
    },
    {
        "id": "deye_p3",
        "label": "Deye P3",
        "detail": "Dreiphasen-Hybrid",
        "register": 0x0068,
        "register_name": "Zero Export power",
        "writable": True,
        "encoding": "watts",
        "signed": True,
        "minimum": -20,
        "maximum": 12000,
        "grid_charge_register": 0x0082,
        "note": "Soll-Netzbezug in Watt. Dasselbe Register wie beim SG04LP3.",
    },
    {
        "id": "deye_micro",
        "label": "Deye Micro",
        "detail": "Mikrowechselrichter",
        "register": 0x0028,
        "register_name": "Active Power Regulation",
        "writable": True,
        "encoding": "pct10",
        "signed": False,
        "minimum": 0,
        "maximum": 1200,
        "note": "",
    },
    {
        "id": "deye_2mppt",
        "label": "Deye 2 MPPT",
        "detail": "String, zwei MPP-Tracker",
        "register": 0x0028,
        "register_name": "Active Power Regulations",
        "writable": True,
        "encoding": "pct10",
        "signed": False,
        "minimum": 0,
        "maximum": 1200,
        "note": "In der Solarman-Datei dasselbe Register 0x0028 wie beim Deye Micro.",
    },
    {
        "id": "deye_4mppt",
        "label": "Deye 4 MPPT",
        "detail": "String, vier MPP-Tracker",
        "register": 0x0028,
        "register_name": "Active Power Regulations",
        "writable": True,
        "encoding": "pct10",
        "signed": False,
        "minimum": 0,
        "maximum": 1200,
        "note": "In der Solarman-Datei dasselbe Register 0x0028 wie beim Deye Micro.",
    },
    {
        "id": "deye_string",
        "label": "Deye String",
        "detail": "Stringwechselrichter",
        "register": None,
        "register_name": "",
        "writable": False,
        "encoding": "",
        "signed": False,
        "minimum": 0,
        "maximum": 0,
        "note": "Die Datei deye_string.yaml hat kein Leistungs-Sollregister.",
    },
    {
        "id": "deye_sg04lp3",
        "label": "Deye SG04LP3",
        "detail": "Niedervolt, dreiphasig",
        "register": 0x0068,
        "register_name": "Zero Export power",
        "writable": True,
        "encoding": "watts",
        "signed": True,
        "minimum": -20,
        "maximum": 12000,
        "grid_charge_register": 0x0082,
        "note": "Soll-Netzbezug in Watt. Die Messwertdatei hat kein eigenes Sollregister, der Niedervolt nutzt 0x0068.",
    },
    {
        "id": "fronius_drehstrom",
        "label": "Fronius Drehwechselrichter",
        "detail": "Symo, Primo, Eco, Galvo, GEN24",
        "register": 40242,
        "register_name": "WMaxLimPct",
        "writable": True,
        "encoding": "pct100",
        "signed": False,
        "minimum": 0,
        "maximum": 10000,
        "note": "Wirkleistungsgrenze in Prozent der Nennleistung.",
    },
    {
        "id": "fronius_hybrid",
        "label": "Fronius Hybrid",
        "detail": "GEN24 Plus, Symo Hybrid",
        "register": 40365,
        "register_name": "OutWRte",
        "writable": True,
        "encoding": "pct100",
        "signed": True,
        "minimum": -10000,
        "maximum": 10000,
        "note": "Basic Storage Control, Start 40353, SunSpec-Offset 12.",
    },
    {
        "id": "fronius_hochvolt",
        "label": "Fronius Hochvolt",
        "detail": "GEN24 Plus Hochvolt",
        "register": 40366,
        "register_name": "InWRte",
        "writable": True,
        "encoding": "pct100",
        "signed": True,
        "minimum": -10000,
        "maximum": 10000,
        "note": "Basic Storage Control, Start 40353, SunSpec-Offset 13.",
    },
    {
        "id": "fronius_tauro",
        "label": "Fronius Tauro",
        "detail": "Tauro",
        "register": None,
        "register_name": "",
        "writable": False,
        "encoding": "",
        "signed": False,
        "minimum": 0,
        "maximum": 0,
        "note": "Die Fronius-Anleitung nennt für Tauro kein festes Sollregister.",
    },
    {
        "id": "fronius_tauro_eco",
        "label": "Fronius Tauro ECO",
        "detail": "Tauro ECO",
        "register": None,
        "register_name": "",
        "writable": False,
        "encoding": "",
        "signed": False,
        "minimum": 0,
        "maximum": 0,
        "note": "Die Fronius-Anleitung nennt für Tauro ECO kein festes Sollregister.",
    },
    {
        "id": "fronius_verto",
        "label": "Fronius Verto",
        "detail": "Verto",
        "register": None,
        "register_name": "",
        "writable": False,
        "encoding": "",
        "signed": False,
        "minimum": 0,
        "maximum": 0,
        "note": "Die Fronius-Anleitung nennt für Verto kein festes Sollregister.",
    },
]

INVERTERS = {item["id"]: item for item in INVERTER_LIST}
DEFAULT_INVERTER = "deye_hybrid"

_lock = threading.Lock()
_last_write: Dict[str, Any] = {"at": 0.0, "signature": None, "result": None}
_writer_started = False


def inverter_choices(selected: str = "") -> List[Dict[str, Any]]:
    link = resolve_ha_link(selected, None) if selected else None
    rows = []
    for item in INVERTER_LIST:
        shown = _control_item(item, link) if item["id"] == selected else item
        rows.append(
            {
                "id": shown["id"],
                "label": shown["label"],
                "detail": shown["detail"],
                "register_text": _register_text(shown),
                "note": shown.get("note") or "",
            }
        )
    return rows


def _register_text(item: Dict[str, Any]) -> str:
    register = item.get("register")
    if register is None:
        return "kein Sollregister"
    return f"Register {int(register)} (0x{int(register):04X}, {item['register_name']})"


def default_settings() -> Dict[str, Any]:
    return {
        "inverter": DEFAULT_INVERTER,
        "real_mode": False,
        "target_grid_kw": 2.0,
        "peak_cap_kw": 2.0,
        "modbus_host": "",
        "modbus_port": 502,
        "modbus_unit": 1,
    }


def _kw(value: Any, fallback: float, lowest: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return round(min(100.0, max(lowest, number)), 2)


def _port(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 502
    return min(65535, max(1, number))


def _unit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return min(247, max(1, number))


def _host(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 253 or any(ch.isspace() for ch in text):
        return ""
    return text


_INVERTER_HINTS = (
    "solarman", "fronius", "deye", "growatt", "huawei", "goodwe", "solaredge",
    "sma", "sofar", "sungrow", "solax", "kostal", "enphase", "foxess", "solis",
    "ginlong", "hoymiles", "alphaess", "saj", "victron",
)


def _family(inverter_id: str) -> str:
    text = (inverter_id or "").lower()
    if text.startswith("deye"):
        return "deye"
    if text.startswith("fronius"):
        return "fronius"
    return text.split("_", 1)[0]


def _entry_blob(entry: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for key in ("data", "options"):
        raw = entry.get(key)
        if isinstance(raw, dict):
            merged.update(raw)
    return merged


def _looks_like_inverter(entry: Dict[str, Any], blob: Dict[str, Any]) -> bool:
    domain = str(entry.get("domain") or "").lower()
    title = str(entry.get("title") or "").lower()
    lookup = str(blob.get("lookup_file") or "").lower()
    haystack = " ".join((domain, title, lookup))
    if any(hint in haystack for hint in _INVERTER_HINTS):
        return True
    return "inverter" in haystack or "wechselrichter" in haystack


def _entry_endpoint(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    blob = _entry_blob(entry)
    host = _host(blob.get("host") or blob.get("ip") or blob.get("inverter_host"))
    if not host or not _looks_like_inverter(entry, blob):
        return None
    port_raw = blob.get("port", blob.get("inverter_port"))
    port = _port(port_raw) if port_raw not in (None, "") else 502
    unit_raw = blob.get("mb_slave_id", blob.get("inverter_mb_slaveid", blob.get("slave", 1)))
    lookup = str(blob.get("lookup_file") or "")
    modified = str(entry.get("modified_at") or entry.get("created_at") or "")
    try:
        serial = int(blob.get("serial") or 0)
    except (TypeError, ValueError):
        serial = 0
    return {
        "entry_id": str(entry.get("entry_id") or host),
        "host": host,
        "port": port,
        "unit": _unit(unit_raw),
        "domain": str(entry.get("domain") or ""),
        "title": str(entry.get("title") or ""),
        "lookup": lookup,
        "modified": modified,
        "serial": serial,
    }


def _ha_endpoints() -> List[Dict[str, Any]]:
    if not HA_CONFIG_ENTRIES.is_file():
        return []
    try:
        raw = json.loads(HA_CONFIG_ENTRIES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("data", {}).get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        endpoint = _entry_endpoint(entry)
        if endpoint:
            found.append(endpoint)
    return found


def _match_score(endpoint: Dict[str, Any], inverter_id: str) -> int:
    family = _family(inverter_id)
    domain = endpoint["domain"].lower()
    title = endpoint["title"].lower()
    lookup = endpoint["lookup"].lower()
    score = 0
    expected = f"{inverter_id}.yaml" if inverter_id.startswith("deye_") else ""
    if expected and expected in lookup:
        score += 100
    if family and family in domain:
        score += 80
    if family == "deye" and "solarman" in domain:
        score += 70
    if family and family in lookup:
        score += 50
    if family and family in title:
        score += 40
    return score


def resolve_ha_link(inverter_id: str, known_ids: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Adresse aus der Home-Assistant-Konfiguration. Neue Einträge gewinnen."""
    endpoints = _ha_endpoints()
    if not endpoints:
        return None
    known = set(str(item) for item in (known_ids or []))
    fresh = [item for item in endpoints if item["entry_id"] not in known]
    pool = fresh or endpoints
    ranked = sorted(
        pool,
        key=lambda item: (_match_score(item, inverter_id), item["modified"]),
        reverse=True,
    )
    # Ein ganz neuer Wechselrichter ohne Typ-Treffer soll trotzdem übernommen werden.
    if fresh:
        chosen = max(fresh, key=lambda item: item["modified"])
    else:
        best = ranked[0]
        if _match_score(best, inverter_id) <= 0:
            chosen = max(endpoints, key=lambda item: item["modified"])
        else:
            chosen = best
    if not chosen.get("serial"):
        for item in endpoints:
            if item["host"] == chosen["host"] and item.get("serial"):
                chosen["serial"] = item["serial"]
                break
    device = _solarman_device(chosen["host"])
    if device:
        try:
            echoed = int(device.get("serial_number") or 0)
        except (TypeError, ValueError):
            echoed = 0
        if echoed:
            chosen["serial"] = echoed
    source = chosen["title"] or chosen["domain"] or "Home Assistant"
    return {
        "host": chosen["host"],
        "port": chosen["port"],
        "unit": chosen["unit"],
        "serial": int(chosen.get("serial") or 0),
        "domain": chosen["domain"],
        "lookup": chosen.get("lookup") or "",
        "model": str((device or {}).get("model") or ""),
        "device_id": str((device or {}).get("id") or ""),
        "source": source,
        "known_entry_ids": [item["entry_id"] for item in endpoints],
    }


def _solarman_device(host: str) -> Optional[Dict[str, Any]]:
    """Aktives Solarman-Gerät zum Host. Die Seriennummer stammt aus dem Geräteverzeichnis."""
    if not host or not HA_DEVICE_REGISTRY.is_file():
        return None
    try:
        raw = json.loads(HA_DEVICE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    devices = raw.get("data", {}).get("devices") if isinstance(raw, dict) else None
    if not isinstance(devices, list):
        return None
    found: Optional[Dict[str, Any]] = None
    for device in devices:
        if not isinstance(device, dict) or device.get("disabled_by"):
            continue
        identifiers = device.get("identifiers") or []
        solarman = any(
            isinstance(item, list) and item and item[0] == "solarman" for item in identifiers
        )
        if not solarman:
            continue
        text = " ".join(
            str(device.get(key) or "")
            for key in ("name", "name_by_user", "configuration_url", "model")
        )
        if host not in text:
            continue
        if found is None or (device.get("serial_number") and not found.get("serial_number")):
            found = device
    return found


def _three_phase(link: Optional[Dict[str, Any]]) -> bool:
    text = f"{(link or {}).get('lookup', '')} {(link or {}).get('model', '')}".lower()
    return any(token in text for token in ("sg04", "lp3", "deye_p3", "sg0"))


def _control_item(item: Dict[str, Any], link: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Sollregister für den Netzbezug. Der angeschlossene Drehstrom-Niedervolt nutzt 0x0068."""
    chosen = dict(item)
    if chosen.get("id") not in {"deye_hybrid", "deye_p3", "deye_sg04lp3"}:
        return chosen
    if chosen["id"] == "deye_hybrid" and not _three_phase(link):
        chosen["register"] = 0x00CE
        chosen.pop("grid_charge_register", None)
    else:
        chosen["register"] = 0x0068
        chosen["grid_charge_register"] = 0x0082
    chosen["register_name"] = "Zero Export power"
    chosen["writable"] = True
    chosen["encoding"] = "watts"
    chosen["signed"] = True
    chosen["minimum"] = -20
    chosen["maximum"] = 12000
    chosen["note"] = "Soll-Netzbezug in Watt. Plus ist Zukauf."
    return chosen


def _persist_settings(data: Dict[str, Any]) -> None:
    stored = {
        "inverter": data["inverter"],
        "real_mode": bool(data["real_mode"]),
        "target_grid_kw": data["target_grid_kw"],
        "peak_cap_kw": data["peak_cap_kw"],
        "modbus_host": data["modbus_host"],
        "modbus_port": data["modbus_port"],
        "modbus_unit": data["modbus_unit"],
        "modbus_source": data.get("modbus_source") or "",
        "known_entry_ids": data.get("known_entry_ids") or [],
    }
    SETTINGS_FILE.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")


def _apply_ha_link(data: Dict[str, Any]) -> Dict[str, Any]:
    link = resolve_ha_link(str(data.get("inverter") or DEFAULT_INVERTER), data.get("known_entry_ids") or [])
    if not link:
        data["modbus_source"] = data.get("modbus_source") or ""
        data["known_entry_ids"] = data.get("known_entry_ids") or []
        return data
    changed = (
        data.get("modbus_host") != link["host"]
        or data.get("modbus_port") != link["port"]
        or data.get("modbus_unit") != link["unit"]
        or data.get("modbus_source") != link["source"]
        or list(data.get("known_entry_ids") or []) != link["known_entry_ids"]
    )
    data["modbus_host"] = link["host"]
    data["modbus_port"] = link["port"]
    data["modbus_unit"] = link["unit"]
    data["modbus_source"] = link["source"]
    data["known_entry_ids"] = link["known_entry_ids"]
    if changed:
        _persist_settings(data)
    return data


def load_settings() -> Dict[str, Any]:
    data = default_settings()
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    if data.get("inverter") not in INVERTERS:
        data["inverter"] = DEFAULT_INVERTER
    data["real_mode"] = bool(data.get("real_mode"))
    data["target_grid_kw"] = _kw(data.get("target_grid_kw"), 2.0, lowest=-100.0)
    data["peak_cap_kw"] = _kw(data.get("peak_cap_kw"), 2.0)
    data["modbus_host"] = _host(data.get("modbus_host"))
    data["modbus_port"] = _port(data.get("modbus_port"))
    data["modbus_unit"] = _unit(data.get("modbus_unit"))
    if not isinstance(data.get("known_entry_ids"), list):
        data["known_entry_ids"] = []
    data["modbus_source"] = str(data.get("modbus_source") or "")
    return _apply_ha_link(data)


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    data = load_settings()
    if "inverter" in updates and updates["inverter"] in INVERTERS:
        data["inverter"] = updates["inverter"]
    if "real_mode" in updates:
        data["real_mode"] = bool(updates["real_mode"])
    if "target_grid_kw" in updates:
        data["target_grid_kw"] = _kw(updates["target_grid_kw"], data["target_grid_kw"], lowest=-100.0)
    if "peak_cap_kw" in updates:
        data["peak_cap_kw"] = _kw(updates["peak_cap_kw"], data["peak_cap_kw"])
    if "modbus_host" in updates and not data.get("modbus_host"):
        data["modbus_host"] = _host(updates["modbus_host"])
    if "modbus_port" in updates and not data.get("modbus_port"):
        data["modbus_port"] = _port(updates["modbus_port"])
    if "modbus_unit" in updates and not data.get("modbus_unit"):
        data["modbus_unit"] = _unit(updates["modbus_unit"])
    data = _apply_ha_link(data)
    _persist_settings(data)
    return data


def _parse_soc(raw: Any) -> Optional[float]:
    if raw in (None, "", "n/a"):
        return None
    try:
        return float(str(raw).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def read_live() -> Dict[str, Any]:
    """Ladezustand, Batterieleistung und Netzbezug aus den Home-Assistant-Werten."""
    soc = None
    battery_kw = None
    grid_kw = None
    try:
        from dashboard_battery import load_battery_data

        battery = load_battery_data()
        soc = _parse_soc(battery.get("soc"))
        power = battery.get("power_kw")
        battery_kw = None if power is None else round(float(power), 2)
    except Exception:
        pass
    try:
        from dashboard_control import get_grid_kw_for_control

        grid = get_grid_kw_for_control(str(BACKEND_DIR))
        grid_kw = None if grid is None else round(float(grid), 2)
    except Exception:
        pass
    return {
        "soc_percent": None if soc is None else round(soc, 1),
        "battery_kw": battery_kw,
        "grid_kw": grid_kw,
        "source": "Home Assistant / Solarman",
    }


def assess(soc: Optional[float], target_kw: float, peak_cap_kw: float) -> Dict[str, Any]:
    """Ob Zukauf und Spitzenkappung bei diesem Ladezustand gehen."""
    warnings: List[str] = []
    can_buy = True
    can_shave = True
    if soc is None:
        can_buy = False
        can_shave = False
        warnings.append("Ladezustand nicht lesbar. Zukauf und Spitzenkappung sind nicht beurteilbar.")
    else:
        if soc <= SOC_EMPTY:
            can_shave = False
            warnings.append(
                f"Batterie fast leer ({soc:.0f} %). Spitzenkappung ist nicht möglich, der Mehrbedarf käme aus dem Netz."
            )
        if soc >= SOC_FULL and target_kw > 0:
            can_buy = False
            warnings.append(
                f"Batterie voll ({soc:.0f} %). Ein Zukauf von {target_kw:.1f} kW kann nicht mehr eingelagert werden."
            )
    if not warnings and soc is not None:
        direction = f"Einspeisung von {abs(target_kw):.1f} kW" if target_kw < 0 else f"Zukauf von {target_kw:.1f} kW"
        warnings.append(
            f"{direction} und Spitzenkappung ab {peak_cap_kw:.1f} kW sind vom Ladezustand her möglich."
        )
    return {
        "can_buy": can_buy,
        "can_shave": can_shave,
        "warnings": warnings,
    }


def simulate(
    grid_kw: Optional[float],
    battery_kw: Optional[float],
    target_grid_kw: float,
    peak_cap_kw: float,
) -> Dict[str, Any]:
    soll = round(min(target_grid_kw, peak_cap_kw), 2)
    shaved = None if grid_kw is None else round(max(0.0, grid_kw - peak_cap_kw), 2)
    setpoint = None
    limited = False
    if grid_kw is not None and battery_kw is not None:
        setpoint = battery_kw - (grid_kw - soll)
        if setpoint > BATTERY_LIMIT_KW:
            setpoint = BATTERY_LIMIT_KW
            limited = True
        elif setpoint < -BATTERY_LIMIT_KW:
            setpoint = -BATTERY_LIMIT_KW
            limited = True
        setpoint = round(setpoint, 2)
    if setpoint is None:
        action = "nicht rechenbar"
    elif setpoint > 0.05:
        action = "laden"
    elif setpoint < -0.05:
        action = "entladen"
    else:
        action = "halten"
    return {
        "target_grid_kw": soll,
        "peak_shave_kw": shaved,
        "battery_setpoint_kw": setpoint,
        "action": action,
        "limited": limited,
    }


def _raw_value(item: Dict[str, Any], setpoint_kw: Optional[float], target_kw: Optional[float] = None) -> Optional[int]:
    if not item.get("writable"):
        return None
    encoding = item["encoding"]
    if encoding == "watts":
        source = target_kw if target_kw is not None else setpoint_kw
        if source is None:
            return None
        raw = int(round(float(source) * 1000))
        raw = max(int(item["minimum"]), min(int(item["maximum"]), raw))
        if raw < 0:
            raw &= 0xFFFF
        return raw
    if setpoint_kw is None:
        return None
    ratio = max(-1.0, min(1.0, float(setpoint_kw) / BATTERY_LIMIT_KW))
    if encoding == "pct100":
        raw = int(round(ratio * 10000))
        if not item["signed"]:
            raw = abs(raw)
    elif encoding == "pct10":
        raw = int(round(abs(ratio) * 1000))
    else:
        return None
    return max(int(item["minimum"]), min(int(item["maximum"]), raw))


def _u16(value: int) -> int:
    return int(value) & 0xFFFF


def _modbus_crc(payload: bytes) -> bytes:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def _solarman_checksum(frame: bytes) -> int:
    return sum(frame[1:]) & 0xFF


def write_solarman_register(host: str, port: int, unit: int, serial: int, address: int, value: int) -> None:
    """Schreiben über den Solarman-Logger (Port 8899), nicht über nacktes Modbus-TCP."""
    sequence = 1
    modbus = struct.pack(">BBHH", int(unit) & 0xFF, 6, int(address) & 0xFFFF, _u16(value))
    modbus += _modbus_crc(modbus)
    header = (
        b"\xA5"
        + struct.pack("<H", 15 + len(modbus))
        + b"\x10"
        + struct.pack("<B", 0x45)
        + struct.pack("<H", sequence)
        + struct.pack("<I", int(serial) & 0xFFFFFFFF)
        + b"\x02\x00\x00"
        + (b"\x00" * 12)
        + modbus
    )
    frame = header + struct.pack("<B", _solarman_checksum(header)) + b"\x15"
    with socket.create_connection((host, int(port)), timeout=4.0) as sock:
        sock.settimeout(4.0)
        sock.sendall(frame)
        data = b""
        while len(data) < 29 or data[-1:] != b"\x15":
            chunk = sock.recv(256)
            if not chunk:
                break
            data += chunk
            if len(data) > 512:
                break
    if len(data) < 29 or data[0:1] != b"\xA5" or data[-1:] != b"\x15":
        raise OSError("unerwartete Logger-Antwort")
    if data[4] != 0x15:
        raise OSError("Logger hat den Schreibbefehl abgelehnt")
    if data[-2] != _solarman_checksum(data[:-2]):
        raise OSError("Prüfsumme der Antwort stimmt nicht")
    modbus_reply = data[25:-2]
    if len(modbus_reply) >= 2 and modbus_reply[1] & 0x80:
        code = modbus_reply[2] if len(modbus_reply) > 2 else 0
        raise OSError(f"Modbus-Fehler {code}")


def write_solarman_multiple(host: str, port: int, unit: int, serial: int, address: int, value: int) -> None:
    """Funktionscode 16. Einzelne Schreibbefehle bleiben am SG04LP3 nicht stehen."""
    payload = struct.pack(">H", _u16(value))
    modbus = struct.pack(">BBHHB", int(unit) & 0xFF, 16, int(address) & 0xFFFF, 1, len(payload)) + payload
    modbus += _modbus_crc(modbus)
    sequence = 1
    header = (
        b"\xA5"
        + struct.pack("<H", 15 + len(modbus))
        + b"\x10"
        + struct.pack("<B", 0x45)
        + struct.pack("<H", sequence)
        + struct.pack("<I", int(serial) & 0xFFFFFFFF)
        + b"\x02\x00\x00"
        + (b"\x00" * 12)
        + modbus
    )
    frame = header + struct.pack("<B", _solarman_checksum(header)) + b"\x15"
    with socket.create_connection((host, int(port)), timeout=4.0) as sock:
        sock.settimeout(4.0)
        sock.sendall(frame)
        data = b""
        while len(data) < 29 or data[-1:] != b"\x15":
            chunk = sock.recv(256)
            if not chunk:
                break
            data += chunk
            if len(data) > 512:
                break
    if len(data) < 29 or data[0:1] != b"\xA5" or data[-1:] != b"\x15" or data[4] != 0x15:
        raise OSError("Logger hat den Mehrfachschreibbefehl abgelehnt")
    reply = data[25:-2]
    if len(reply) >= 2 and reply[1] & 0x80:
        code = reply[2] if len(reply) > 2 else 0
        raise OSError(f"Modbus-Fehler {code}")


def reapply_dauerzukauf_test() -> None:
    """Testgrenze über 92 % und 1,2 kW Netzbezug halten, bis der Test abgebrochen wird."""
    if not DAUERZUKAUF_TEST_FILE.is_file():
        return
    try:
        stored = json.loads(DAUERZUKAUF_TEST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(stored, dict) or not stored.get("active"):
        return
    applied = stored.get("applied")
    if not isinstance(applied, dict):
        return
    settings = load_settings()
    host = settings.get("modbus_host") or ""
    if not host:
        return
    link = resolve_ha_link(str(settings.get("inverter") or ""), settings.get("known_entry_ids") or [])
    serial = int(link.get("serial") or 0) if link else 0
    if not serial:
        return
    for key, value in applied.items():
        write_solarman_multiple(host, settings["modbus_port"], settings["modbus_unit"], serial, int(key), int(value))


def write_register(host: str, port: int, unit: int, address: int, value: int, serial: int = 0) -> None:
    """Holding-Register schreiben. Port 8899 läuft über das Solarman-Protokoll."""
    if int(port) == 8899 or serial:
        if not serial:
            raise OSError("Solarman-Seriennummer fehlt")
        write_solarman_register(host, port, unit, serial, address, value)
        return
    pdu = struct.pack(">BHH", 6, int(address) & 0xFFFF, _u16(value))
    message = struct.pack(">HHHB", 1, 0, len(pdu) + 1, int(unit) & 0xFF) + pdu
    with socket.create_connection((host, int(port)), timeout=3.0) as sock:
        sock.settimeout(3.0)
        sock.sendall(message)
        data = sock.recv(32)
    if len(data) < 9:
        raise OSError("keine vollständige Antwort")
    function = data[7]
    if function & 0x80:
        code = data[8] if len(data) > 8 else 0
        raise OSError(f"Modbus-Fehler {code}")
    if function != 6:
        raise OSError("unerwartete Antwort")


def _write_result(sent: bool, text: str, raw: Optional[int] = None) -> Dict[str, Any]:
    return {"command_sent": sent, "write_text": text, "raw": raw}


def apply_write(settings: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    base = INVERTERS.get(settings.get("inverter"), INVERTERS[DEFAULT_INVERTER])
    link = resolve_ha_link(str(settings.get("inverter") or ""), settings.get("known_entry_ids") or [])
    item = _control_item(base, link)
    register_text = _register_text(item)
    if not settings.get("real_mode"):
        return _write_result(False, "Aus. Es wird nichts geschrieben.")
    if not item.get("writable") or item.get("register") is None:
        return _write_result(False, item.get("note") or "Für diesen Typ wird nichts geschrieben.")
    raw = _raw_value(item, plan.get("battery_setpoint_kw"), plan.get("target_grid_kw"))
    if raw is None:
        return _write_result(False, "Sollwert nicht rechenbar. Es wurde nichts geschrieben.")
    host = settings.get("modbus_host") or ""
    if not host:
        return _write_result(False, f"{register_text}. Modbus-Host fehlt, es wurde nichts geschrieben.", raw)
    serial = int(link.get("serial") or 0) if link else 0
    signature = (item["id"], int(item["register"]), raw, host, settings["modbus_port"], settings["modbus_unit"])
    now = time.monotonic()
    with _lock:
        if signature == _last_write["signature"] and now - float(_last_write["at"]) < SAMPLE_GAP_SEC:
            previous = _last_write["result"] or _write_result(False, "unverändert")
            return previous
        try:
            write_register(
                host,
                settings["modbus_port"],
                settings["modbus_unit"],
                int(item["register"]),
                raw,
                serial=serial,
            )
        except OSError as exc:
            result = _write_result(False, f"{register_text}, Wert {raw}. Schreiben fehlgeschlagen: {exc}.", raw)
        else:
            result = _write_result(True, f"Geschrieben: {register_text}, Wert {raw}.", raw)
        _last_write["at"] = now
        _last_write["signature"] = signature
        _last_write["result"] = result
        return result


def _load_log() -> List[Dict[str, Any]]:
    if not LOG_FILE.is_file():
        return []
    try:
        raw = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def _save_log(rows: List[Dict[str, Any]]) -> None:
    LOG_FILE.write_text(json.dumps(rows[:LOG_LIMIT], indent=2) + "\n", encoding="utf-8")


def _log_line(live: Dict[str, Any], plan: Dict[str, Any], write: Dict[str, Any], label: str) -> str:
    grid = live.get("grid_kw")
    grid_text = "Netz unbekannt" if grid is None else f"Netz {grid:.1f} kW"
    soll = plan.get("target_grid_kw")
    peak = plan.get("peak_shave_kw")
    peak_text = "Spitze unbekannt" if peak is None else f"Spitze {peak:.1f} kW"
    battery = plan.get("battery_setpoint_kw")
    if battery is None:
        battery_text = "Batterie-Soll unbekannt"
    else:
        battery_text = f"Batterie-Soll {battery:+.1f} kW ({plan.get('action')})"
    return f"{label}: {grid_text}, Zukauf/Einspeisung {soll:+.1f} kW, {peak_text}, {battery_text}. {write.get('write_text')}"


def _remember(live: Dict[str, Any], plan: Dict[str, Any], write: Dict[str, Any], label: str) -> List[Dict[str, Any]]:
    log = _load_log()
    now = datetime.now(TZ)
    fresh = True
    if log:
        try:
            previous = datetime.fromisoformat(log[0]["at"])
            fresh = (now - previous).total_seconds() >= SAMPLE_GAP_SEC
        except (KeyError, TypeError, ValueError):
            fresh = True
    if fresh:
        log.insert(
            0,
            {
                "at": now.isoformat(timespec="seconds"),
                "text": _log_line(live, plan, write, label),
                "command_sent": bool(write.get("command_sent")),
            },
        )
        _save_log(log)
    return log


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    _writer_started = True

    def loop() -> None:
        while True:
            try:
                reapply_dauerzukauf_test()
                settings = load_settings()
                if settings.get("real_mode"):
                    live = read_live()
                    plan = simulate(
                        live.get("grid_kw"),
                        live.get("battery_kw"),
                        settings["target_grid_kw"],
                        settings["peak_cap_kw"],
                    )
                    item = INVERTERS[settings["inverter"]]
                    write = apply_write(settings, plan)
                    _remember(live, plan, write, item["label"])
            except Exception:
                pass
            time.sleep(SAMPLE_GAP_SEC)

    threading.Thread(target=loop, name="bms-modbus", daemon=True).start()


def status(record: bool = True) -> Dict[str, Any]:
    _ensure_writer()
    settings = load_settings()
    item = INVERTERS[settings["inverter"]]
    live = read_live()
    judgement = assess(live.get("soc_percent"), settings["target_grid_kw"], settings["peak_cap_kw"])
    plan = simulate(
        live.get("grid_kw"),
        live.get("battery_kw"),
        settings["target_grid_kw"],
        settings["peak_cap_kw"],
    )
    write = apply_write(settings, plan)
    plan["command_sent"] = bool(write["command_sent"])
    plan["write_text"] = write["write_text"]
    plan["raw"] = write.get("raw")
    log = _remember(live, plan, write, item["label"]) if record and settings.get("real_mode") else _load_log()
    return {
        "settings": settings,
        "inverter": {
            "id": item["id"],
            "label": item["label"],
            "detail": item["detail"],
            "register_text": _register_text(item),
            "note": item.get("note") or "",
            "writable": bool(item.get("writable")),
        },
        "inverters": inverter_choices(),
        "live": live,
        "assessment": judgement,
        "plan": plan,
        "log": log[:LOG_LIMIT],
        "command_sent": bool(write["command_sent"]),
    }
