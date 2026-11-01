# -*- coding: utf-8 -*-
"""BMS-Netzbezug: Live-Anzeige, Rechnung und optionales Modbus-Schreiben.

Das Häkchen „Ins Register schreiben“ hält den Netzbezug auf dem Soll:
Hauslast darüber kommt aus der Batterie (Netzspitzenkappung), Hauslast darunter
wird durch Netzladung in die Batterie ausgeglichen. Die Zeitschaltuhr bleibt an.
Ausgelassen bleibt es eine reine Anzeige.
Register stammen aus den Solarman-Dateien (Deye) und der Fronius-SunSpec-Anleitung.
"""
from __future__ import annotations

import json
import random
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
# Unterkante des Niedervolt-Fensters. Der Ladestrom wird so bemessen, dass der
# ganze Zukauf auch dort noch in die Batterie passt. Register 0x0080 zählt Ampere.
GRID_CHARGE_SIZING_VOLT = 40
GRID_CHARGE_CURRENT_MAX = 350
LOG_LIMIT = 30
SAMPLE_GAP_SEC = 6
# Rechnung alle 3 s aus dem RAM. Die schnellen Werte bleiben dort und gehen nicht auf die SD-Karte.
CONTROL_SAMPLE_SEC = 3

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
        "grid_charge_current_register": 0x0080,
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
        "grid_charge_current_register": 0x0080,
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
# Einmal lösen, nachdem wirklich ein Zukauf geschrieben wurde. Steht in den Einstellungen,
# damit ein Neustart den offenen Löschvorgang nicht vergisst.
_needs_release = False
_tou_hold_applied = False
_tou_soc_applied: Optional[tuple] = None
_release_sent = False
# Time of Use 255: alle Wochentage, eingeschaltet. Aus = Batterie steht, Hauslast aus dem Netz.
TOU_WEEK_ENABLED = 0x00FF
# Export First: Hauslast darf vom Netz kommen. Zero-Export-to-CT hält das Netz auf 0.
WORK_MODE_EXPORT_FIRST = 0
WORK_MODE_ZERO_EXPORT_CT = 2
ENERGY_PATTERN_BATTERY_FIRST = 0
ENERGY_PATTERN_LOAD_FIRST = 1
SOLAR_SELL_OFF = 0
# Soll-Netzbezug in Watt ins Zero-Export-Register, nicht nur 20 W Offset.
# Nach dem Ausschalten: externer Zugriff / Zero-Export-Offset 20 W.
ZERO_EXPORT_IDLE_W = 20
GRID_PEAK_ENABLE_BIT = 4
MAX_DISCHARGE_AMPS = 240
TOU_HOLD_SOC = 20
TOU_HOLD_POWER_W = 12000


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
        "idle_grid_w": 20,
        "idle_work_mode": WORK_MODE_ZERO_EXPORT_CT,
        "idle_energy_pattern": ENERGY_PATTERN_LOAD_FIRST,
        "idle_peak_w": 8000,
        "idle_tou_soc": 20,
        "idle_tou_charging": 1,
        "idle_tou_power_w": TOU_HOLD_POWER_W,
        "modbus_host": "",
        "modbus_port": 502,
        "modbus_unit": 1,
        "release_pending": False,
    }


def _kw(value: Any, fallback: float, lowest: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return round(min(100.0, max(lowest, number)), 2)


def _idle_grid_w(settings: Optional[Dict[str, Any]] = None) -> int:
    """Standeinstellung nach Schreibstopp. Nie 0, Vorgabe 20 W."""
    raw = ZERO_EXPORT_IDLE_W
    if settings is not None and settings.get("idle_grid_w") not in (None, ""):
        raw = settings.get("idle_grid_w")
    try:
        watts = int(round(float(str(raw).replace(",", ".").strip())))
    except (TypeError, ValueError):
        watts = ZERO_EXPORT_IDLE_W
    return max(1, min(500, watts))


def _idle_tou_soc(settings: Optional[Dict[str, Any]] = None) -> int:
    raw = TOU_HOLD_SOC
    if settings is not None and settings.get("idle_tou_soc") not in (None, ""):
        raw = settings.get("idle_tou_soc")
    try:
        value = int(round(float(str(raw).replace(",", ".").strip())))
    except (TypeError, ValueError):
        value = TOU_HOLD_SOC
    return max(TOU_HOLD_SOC, min(100, value))


def _idle_tou_charging(settings: Optional[Dict[str, Any]] = None) -> int:
    raw = 1
    if settings is not None and settings.get("idle_tou_charging") not in (None, ""):
        raw = settings.get("idle_tou_charging")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return value if value in (0, 1, 2, 3) else 1


def _idle_work_mode(settings: Optional[Dict[str, Any]] = None) -> int:
    raw = WORK_MODE_ZERO_EXPORT_CT
    if settings is not None and settings.get("idle_work_mode") not in (None, ""):
        raw = settings.get("idle_work_mode")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = WORK_MODE_ZERO_EXPORT_CT
    return value if value in (WORK_MODE_EXPORT_FIRST, WORK_MODE_ZERO_EXPORT_CT) else WORK_MODE_ZERO_EXPORT_CT


def _idle_energy_pattern(settings: Optional[Dict[str, Any]] = None) -> int:
    raw = ENERGY_PATTERN_LOAD_FIRST
    if settings is not None and settings.get("idle_energy_pattern") not in (None, ""):
        raw = settings.get("idle_energy_pattern")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = ENERGY_PATTERN_LOAD_FIRST
    return value if value in (ENERGY_PATTERN_BATTERY_FIRST, ENERGY_PATTERN_LOAD_FIRST) else ENERGY_PATTERN_LOAD_FIRST


def _idle_peak_w(settings: Optional[Dict[str, Any]] = None) -> int:
    raw = 8000
    if settings is not None and settings.get("idle_peak_w") not in (None, ""):
        raw = settings.get("idle_peak_w")
    try:
        value = int(round(float(str(raw).replace(",", ".").strip())))
    except (TypeError, ValueError):
        value = 8000
    return max(100, min(80000, value))


def _idle_tou_power_w(settings: Optional[Dict[str, Any]] = None) -> int:
    raw = TOU_HOLD_POWER_W
    if settings is not None and settings.get("idle_tou_power_w") not in (None, ""):
        raw = settings.get("idle_tou_power_w")
    try:
        value = int(round(float(str(raw).replace(",", ".").strip())))
    except (TypeError, ValueError):
        value = TOU_HOLD_POWER_W
    return max(100, min(12000, value))


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
        chosen.pop("grid_charge_current_register", None)
        chosen.pop("grid_charge_start_register", None)
        chosen.pop("time_of_use_register", None)
        chosen.pop("work_mode_register", None)
        chosen.pop("energy_pattern_register", None)
        chosen.pop("solar_sell_register", None)
        chosen.pop("grid_peak_enable_register", None)
        chosen.pop("grid_peak_power_register", None)
        chosen.pop("max_discharge_register", None)
        chosen.pop("max_export_register", None)
        chosen.pop("max_import_register", None)
        chosen.pop("sell_power_register", None)
    else:
        chosen["register"] = 0x0068
        chosen["grid_charge_register"] = 0x0082
        chosen["grid_charge_current_register"] = 0x0080
        chosen["grid_charge_start_register"] = 0x007F
        chosen["time_of_use_register"] = 0x0092
        chosen["work_mode_register"] = 0x008E
        chosen["energy_pattern_register"] = 0x008D
        chosen["solar_sell_register"] = 0x0091
        chosen["grid_peak_enable_register"] = 0x00B2
        chosen["grid_peak_power_register"] = 0x00BF
        chosen["max_discharge_register"] = 0x006D
        chosen["max_export_register"] = 0x00E7
        chosen["max_import_register"] = 0x00E9
        chosen["sell_power_register"] = 0x008F
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
        "idle_grid_w": data["idle_grid_w"],
        "idle_work_mode": data["idle_work_mode"],
        "idle_energy_pattern": data["idle_energy_pattern"],
        "idle_peak_w": data["idle_peak_w"],
        "idle_tou_soc": data["idle_tou_soc"],
        "idle_tou_charging": data["idle_tou_charging"],
        "idle_tou_power_w": data["idle_tou_power_w"],
        "modbus_host": data["modbus_host"],
        "modbus_port": data["modbus_port"],
        "modbus_unit": data["modbus_unit"],
        "modbus_source": data.get("modbus_source") or "",
        "known_entry_ids": data.get("known_entry_ids") or [],
        "release_pending": bool(data.get("release_pending")),
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
    data["idle_grid_w"] = _idle_grid_w({"idle_grid_w": data.get("idle_grid_w")})
    data["idle_work_mode"] = _idle_work_mode({"idle_work_mode": data.get("idle_work_mode")})
    data["idle_energy_pattern"] = _idle_energy_pattern({"idle_energy_pattern": data.get("idle_energy_pattern")})
    data["idle_peak_w"] = _idle_peak_w({"idle_peak_w": data.get("idle_peak_w")})
    data["idle_tou_soc"] = _idle_tou_soc({"idle_tou_soc": data.get("idle_tou_soc")})
    data["idle_tou_charging"] = _idle_tou_charging({"idle_tou_charging": data.get("idle_tou_charging")})
    data["idle_tou_power_w"] = _idle_tou_power_w({"idle_tou_power_w": data.get("idle_tou_power_w")})
    data["modbus_host"] = _host(data.get("modbus_host"))
    data["modbus_port"] = _port(data.get("modbus_port"))
    data["modbus_unit"] = _unit(data.get("modbus_unit"))
    if not isinstance(data.get("known_entry_ids"), list):
        data["known_entry_ids"] = []
    data["modbus_source"] = str(data.get("modbus_source") or "")
    data["release_pending"] = bool(data.get("release_pending"))
    return _apply_ha_link(data)


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    global _release_sent
    data = load_settings()
    if "inverter" in updates and updates["inverter"] in INVERTERS:
        data["inverter"] = updates["inverter"]
    if "real_mode" in updates:
        wanted = bool(updates["real_mode"])
        if data["real_mode"] and not wanted:
            data["release_pending"] = True
            _release_sent = False
        data["real_mode"] = wanted
    if "target_grid_kw" in updates:
        data["target_grid_kw"] = _kw(updates["target_grid_kw"], data["target_grid_kw"], lowest=-100.0)
    if "peak_cap_kw" in updates:
        data["peak_cap_kw"] = _kw(updates["peak_cap_kw"], data["peak_cap_kw"])
    if data["target_grid_kw"] > 0:
        data["peak_cap_kw"] = data["target_grid_kw"]
    if "idle_grid_w" in updates:
        data["idle_grid_w"] = _idle_grid_w({"idle_grid_w": updates["idle_grid_w"]})
    if "idle_work_mode" in updates:
        data["idle_work_mode"] = _idle_work_mode({"idle_work_mode": updates["idle_work_mode"]})
    if "idle_energy_pattern" in updates:
        data["idle_energy_pattern"] = _idle_energy_pattern({"idle_energy_pattern": updates["idle_energy_pattern"]})
    if "idle_peak_w" in updates:
        data["idle_peak_w"] = _idle_peak_w({"idle_peak_w": updates["idle_peak_w"]})
    if "idle_tou_soc" in updates:
        data["idle_tou_soc"] = _idle_tou_soc({"idle_tou_soc": updates["idle_tou_soc"]})
    if "idle_tou_charging" in updates:
        data["idle_tou_charging"] = _idle_tou_charging({"idle_tou_charging": updates["idle_tou_charging"]})
    if "idle_tou_power_w" in updates:
        data["idle_tou_power_w"] = _idle_tou_power_w({"idle_tou_power_w": updates["idle_tou_power_w"]})
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


_sample_lock = threading.Lock()
_control_sample: Dict[str, Any] = {}
_sampler_started = False


HA_GRID_POWER = "sensor.192_168_1_176_grid_power"
HA_LOAD_POWER = "sensor.192_168_1_176_load_power"
HA_BATTERY_POWER = "Beispiel"
HA_BATTERY_SOC = (
    "Beispiel",
    "Beispiel_bms_soc",
    "Beispiel_soc",
    "sensor.192_168_1_176_soc",
)


def _ha_number(raw: Any) -> Optional[float]:
    if raw in (None, "", "unavailable", "unknown", "n/a"):
        return None
    try:
        return float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _read_live_now() -> Dict[str, Any]:
    """Wechselrichter-Zähler aus Home Assistant. Nicht daten.json (dort steht oft SOC 0)."""
    soc = None
    battery_kw = None
    grid_kw = None
    load_kw = None
    try:
        from ha_device_control import read_ha_entity_state

        raw_grid = _ha_number(read_ha_entity_state(HA_GRID_POWER))
        if raw_grid is not None:
            grid_kw = round(raw_grid / 1000.0, 2)
        raw_load = _ha_number(read_ha_entity_state(HA_LOAD_POWER))
        if raw_load is not None:
            load_kw = round(raw_load / 1000.0, 2)
        raw_batt = _ha_number(read_ha_entity_state(HA_BATTERY_POWER))
        if raw_batt is not None:
            # Solarman: Plus = Entladen. Hier: Plus = Laden.
            battery_kw = round(-raw_batt / 1000.0, 2)
        for entity in HA_BATTERY_SOC:
            soc = _parse_soc(read_ha_entity_state(entity))
            if soc is not None:
                break
    except Exception:
        pass
    if soc is None or battery_kw is None:
        try:
            from dashboard_battery import load_battery_data

            battery = load_battery_data()
            file_soc = _parse_soc(battery.get("soc"))
            # 0 % aus der Datei ist der bekannte Totwert, nicht der Speicher.
            if soc is None and file_soc not in (None, 0.0):
                soc = file_soc
            if battery_kw is None:
                power = battery.get("power_kw")
                battery_kw = None if power is None else round(float(power), 2)
        except Exception:
            pass
    if grid_kw is None:
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
        "load_kw": load_kw,
        "source": "Home Assistant / Solarman",
    }


def _store_control_sample(sample: Dict[str, Any]) -> None:
    """Ein Rechenwert. Der vorige wird verworfen, nichts wird auf die SD-Karte geschrieben."""
    stored = dict(sample)
    stored["sampled_at"] = time.time()
    with _sample_lock:
        _control_sample.clear()
        _control_sample.update(stored)


def _sampler_loop() -> None:
    while True:
        try:
            _store_control_sample(_read_live_now())
        except Exception:
            pass
        time.sleep(CONTROL_SAMPLE_SEC)


def _ensure_sampler() -> None:
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True
    threading.Thread(target=_sampler_loop, name="bms-sample", daemon=True).start()


def read_live() -> Dict[str, Any]:
    """Ladezustand, Batterieleistung und Netzbezug. Die Rechnung nimmt den 3-Sekunden-Wert aus dem RAM."""
    _ensure_sampler()
    with _sample_lock:
        if _control_sample:
            return dict(_control_sample)
    sample = _read_live_now()
    _store_control_sample(sample)
    return dict(sample)


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
    # Beim festen Zukauf ist die Spitzenkappung der Soll selbst. Ein kleinerer Kappungswert
    # würde den Soll sonst still auf sich herunterziehen (6 kW Soll, 2 kW Kappung = 2 kW).
    soll = round(target_grid_kw, 2)
    cap = target_grid_kw if target_grid_kw > 0 else peak_cap_kw
    shaved = None if grid_kw is None else round(max(0.0, grid_kw - cap), 2)
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


def _solarman_exchange(host: str, port: int, serial: int, modbus: bytes, accept) -> bytes:
    """Ein Modbus-Rahmen über den Solarman-Logger (Port 8899).

    Der Logger schickt die Antworten an alle offenen Verbindungen, also auch die
    Antworten auf die Abfragen von Home Assistant. Gezählt wird nur ein Rahmen mit
    unserer Folgenummer, für den accept() zustimmt. Sonst ginge ein verlorener
    Schreibbefehl als bestätigt durch.
    """
    if not serial:
        raise OSError("Solarman-Seriennummer fehlt")
    sequence = random.randint(1, 255)
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
    deadline = time.monotonic() + 4.0
    with socket.create_connection((host, int(port)), timeout=4.0) as sock:
        sock.sendall(frame)
        data = b""
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = sock.recv(512)
            if not chunk:
                break
            data += chunk
            while True:
                begin = data.find(b"\xA5")
                if begin < 0:
                    data = b""
                    break
                data = data[begin:]
                if len(data) < 3:
                    break
                total = 13 + struct.unpack("<H", data[1:3])[0]
                if len(data) < total:
                    break
                reply, data = data[:total], data[total:]
                if reply[-1:] != b"\x15" or reply[4] != 0x15 or reply[5] != sequence:
                    continue
                if reply[-2] != _solarman_checksum(reply[:-2]):
                    continue
                answer = reply[25:-2]
                if len(answer) >= 3 and answer[0] == modbus[0] and answer[1] == (modbus[1] | 0x80):
                    raise OSError(f"Modbus-Fehler {answer[2]}")
                if accept(answer):
                    return answer
    raise OSError("keine passende Logger-Antwort")


def write_solarman_register(host: str, port: int, unit: int, serial: int, address: int, value: int) -> None:
    """Funktionscode 6 über den Solarman-Logger."""
    modbus = struct.pack(">BBHH", int(unit) & 0xFF, 6, int(address) & 0xFFFF, _u16(value))
    modbus += _modbus_crc(modbus)
    _solarman_exchange(host, port, serial, modbus, lambda answer: answer[:6] == modbus[:6])


def write_solarman_multiple(host: str, port: int, unit: int, serial: int, address: int, value: int) -> None:
    """Funktionscode 16. Einzelne Schreibbefehle bleiben am SG04LP3 nicht stehen."""
    payload = struct.pack(">H", _u16(value))
    modbus = struct.pack(">BBHHB", int(unit) & 0xFF, 16, int(address) & 0xFFFF, 1, len(payload)) + payload
    modbus += _modbus_crc(modbus)
    _solarman_exchange(host, port, serial, modbus, lambda answer: answer[:6] == modbus[:6])


def read_solarman_holding(host: str, port: int, unit: int, serial: int, address: int) -> int:
    """Ein Holding-Register über den Solarman-Logger lesen (Funktionscode 3)."""
    modbus = struct.pack(">BBHH", int(unit) & 0xFF, 3, int(address) & 0xFFFF, 1)
    modbus += _modbus_crc(modbus)
    answer = _solarman_exchange(
        host, port, serial, modbus,
        lambda reply: len(reply) >= 5 and reply[0] == modbus[0] and reply[1] == 3 and reply[2] == 2,
    )
    return (answer[3] << 8) | answer[4]


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


def _grid_charge_amps(watts: int) -> int:
    """Ampere für Register 0x0080. Deckt den vollen Zukauf bei GRID_CHARGE_SIZING_VOLT ab."""
    if watts <= 0:
        return 0
    amps = (int(watts) + GRID_CHARGE_SIZING_VOLT - 1) // GRID_CHARGE_SIZING_VOLT
    return max(1, min(GRID_CHARGE_CURRENT_MAX, amps))


# Gemessene Packspannung. Die Ströme sind nur Stellgrößen, gehalten wird am Netzzähler.
PACK_VOLT = 53
CHARGE_CURRENT_CAP = 240
# Am SG04LP3 deckelt die Spitzenkappung den ganzen Netzbezug, auch die Netzladung
# (gemessen: Kappung 1000/2000 W bei 60 A Netzladung ergibt 945/1937 W). Der Ladestrom
# bekommt Reserve über den Bedarf, den Soll hält die Kappung im Wechselrichter selbst.
CHARGE_HEADROOM_W = 800
AMP_STEP_DOWN = 10
# Die Kappung landet rund 60 W unter dem Registerwert. Der Rest wird langsam nachgeführt.
PEAK_TRIM_MIN_W = -300
PEAK_TRIM_MAX_W = 400
PEAK_TRIM_GAIN = 0.5
# Gleiche Rolle für die Einspeisung: Export Surplus Power deckelt, der Entladestrom hat Reserve.
DISCHARGE_HEADROOM_W = 800
DISCHARGE_GAIN = 0.5
MODE_SWITCH_W = 150
GRID_DEADBAND_W = 250
SELL_POWER_MAX_W = 12000
# Stand vor den Einspeise-Tests (Home Assistant: Export Surplus Power 13180 W).
SELL_POWER_IDLE_W = 13180
_commanded_amps = 0
_discharge_amps = MAX_DISCHARGE_AMPS
# "charge": Netz deckt Haus plus Netzladung, Kappung hält den Soll.
# "discharge": Haus über dem Soll, Entladestrom begrenzt, damit trotzdem der Soll vom Netz kommt.
# "feed": Einspeisung aus Batterie und PV.
_hold_mode: Optional[str] = None
_hold_target_w: Optional[int] = None
_peak_trim_w = 0


def _hold_deadband_w(soll_kw: float) -> int:
    """Totzone folgt dem Soll. 250 W machen 200 W-Halt unmöglich."""
    target_w = abs(int(round(float(soll_kw) * 1000)))
    return max(60, min(GRID_DEADBAND_W, int(round(target_w * 0.25)) or 60))


def _reset_hold(target_w: Optional[int]) -> None:
    global _commanded_amps, _discharge_amps, _hold_mode, _hold_target_w, _peak_trim_w, _tou_hold_applied, _tou_soc_applied
    _commanded_amps = 0
    _discharge_amps = MAX_DISCHARGE_AMPS
    _hold_mode = None
    _hold_target_w = target_w
    _peak_trim_w = 0
    _tou_hold_applied = False
    _tou_soc_applied = None


def _grid_w(live: Optional[Dict[str, Any]]) -> Optional[int]:
    grid = None if not live else live.get("grid_kw")
    return None if grid is None else int(round(float(grid) * 1000))


def _battery_need_w(target_w: int, live: Optional[Dict[str, Any]]) -> Optional[int]:
    """Batterieleistung (Laden plus), bei der das Netz genau auf dem Soll stünde.

    Rechnet aus Netz und Batterie, nicht aus der Hauslast. PV und Mikrowechselrichter
    stecken so automatisch drin.
    """
    grid = _grid_w(live)
    battery = None if not live else live.get("battery_kw")
    if grid is None or battery is None:
        return None
    return int(round(float(battery) * 1000)) + target_w - grid


def _amps_for(watts: float, cap: int) -> int:
    if watts <= 0:
        return 0
    return max(1, min(cap, int((watts + PACK_VOLT - 1) // PACK_VOLT)))


def _needed_charge_amps(target_w: int, live: Optional[Dict[str, Any]]) -> int:
    """Netzladestrom mit Reserve. Steigt sofort, fällt in kleinen Schritten."""
    global _commanded_amps
    soc = None if not live else live.get("soc_percent")
    if soc is not None and float(soc) >= SOC_FULL:
        _commanded_amps = 0
        return 0
    need = _battery_need_w(target_w, live)
    if need is None:
        goal = _grid_charge_amps(target_w)
    else:
        goal = _amps_for(need + CHARGE_HEADROOM_W, CHARGE_CURRENT_CAP)
    if goal >= _commanded_amps:
        _commanded_amps = goal
    else:
        _commanded_amps = max(goal, _commanded_amps - AMP_STEP_DOWN)
    return _commanded_amps


def _track_peak_trim(target_w: int, live: Optional[Dict[str, Any]]) -> None:
    global _peak_trim_w
    grid = _grid_w(live)
    if grid is None:
        return
    error = target_w - grid
    if abs(error) > 400 or abs(error) <= 25:
        return
    _peak_trim_w = max(PEAK_TRIM_MIN_W, min(PEAK_TRIM_MAX_W, _peak_trim_w + int(round(error * PEAK_TRIM_GAIN))))


def _discharge_limit_amps(target_w: int, live: Optional[Dict[str, Any]]) -> int:
    """Entladestrom so begrenzen, dass die Hauslast über dem Soll aus der Batterie kommt."""
    global _discharge_amps
    grid = _grid_w(live)
    if grid is None:
        return _discharge_amps
    error = grid - target_w
    if abs(error) > _hold_deadband_w(target_w / 1000.0):
        step = int(round(error * DISCHARGE_GAIN / PACK_VOLT)) or (1 if error > 0 else -1)
        _discharge_amps = max(0, min(MAX_DISCHARGE_AMPS, _discharge_amps + step))
    return _discharge_amps


def _choose_import_mode(target_w: int, live: Optional[Dict[str, Any]]) -> str:
    global _hold_mode, _discharge_amps
    need = _battery_need_w(target_w, live)
    soc = None if not live else live.get("soc_percent")
    can_discharge = soc is None or float(soc) > SOC_EMPTY
    if _hold_mode not in ("charge", "discharge"):
        _hold_mode = "charge"
    if need is None:
        return _hold_mode
    if _hold_mode == "charge" and need < -MODE_SWITCH_W and can_discharge:
        _hold_mode = "discharge"
        _discharge_amps = _amps_for(-need, MAX_DISCHARGE_AMPS)
    elif _hold_mode == "discharge" and (need > MODE_SWITCH_W or not can_discharge):
        _hold_mode = "charge"
    return _hold_mode


def _house_load_kw(live: Optional[Dict[str, Any]]) -> Optional[float]:
    """Hauslast: HA-Lastzähler, sonst Netz minus Batterie (Laden plus, Entladen minus)."""
    if not live:
        return None
    if live.get("load_kw") is not None:
        return round(float(live["load_kw"]), 3)
    grid = live.get("grid_kw")
    battery = live.get("battery_kw")
    if grid is None or battery is None:
        return None
    return round(float(grid) - float(battery), 3)


def _peak_enable_row(item: Dict[str, Any], peak_flags: Optional[int]) -> List[tuple]:
    """0x00B2 ist ein 16-Bit-Feld mit weiteren Schaltern. Nur Bit 4 dazunehmen, nie überschreiben."""
    register = item.get("grid_peak_enable_register")
    if register is None or peak_flags is None or (int(peak_flags) >> GRID_PEAK_ENABLE_BIT) & 1:
        return []
    return [(int(register), "Grid Peak shaving enable", int(peak_flags) | (1 << GRID_PEAK_ENABLE_BIT))]


def _import_writes(
    item: Dict[str, Any],
    target_w: int,
    live: Optional[Dict[str, Any]],
    peak_flags: Optional[int],
) -> tuple:
    """Fester Netzbezug. Liefert (Registerzeilen, TOU-SOC, TOU-Laden)."""
    enable = item.get("grid_charge_register")
    current = item.get("grid_charge_current_register")
    if enable is None or current is None:
        return [], TOU_HOLD_SOC, 1
    mode = _choose_import_mode(target_w, live)
    if mode == "charge":
        _track_peak_trim(target_w, live)
    peak_w = max(0, target_w + _peak_trim_w)
    rows: List[tuple] = []
    if item.get("work_mode_register") is not None:
        rows.append((int(item["work_mode_register"]), "Work Mode", WORK_MODE_ZERO_EXPORT_CT))
    if item.get("energy_pattern_register") is not None:
        rows.append((int(item["energy_pattern_register"]), "Energy Pattern", ENERGY_PATTERN_LOAD_FIRST))
    if item.get("solar_sell_register") is not None:
        rows.append((int(item["solar_sell_register"]), "Solar Sell", SOLAR_SELL_OFF))
    if item.get("max_import_register") is not None:
        # Scale 10, nur lose Obergrenze. Den Soll hält die Spitzenkappung.
        loose = min(800, (target_w + PEAK_TRIM_MAX_W + 500 + 9) // 10)
        rows.append((int(item["max_import_register"]), "Grid Max Import power", loose))
    if item.get("time_of_use_register") is not None:
        rows.append((int(item["time_of_use_register"]), "Time of Use", TOU_WEEK_ENABLED))
    if item.get("grid_peak_power_register") is not None:
        rows.append((int(item["grid_peak_power_register"]), "Grid Peak shaving", peak_w))
    rows.extend(_peak_enable_row(item, peak_flags))
    if mode == "charge":
        amps = _needed_charge_amps(target_w, live)
        if item.get("max_discharge_register") is not None:
            rows.append((int(item["max_discharge_register"]), "Battery Max Discharging Current", MAX_DISCHARGE_AMPS))
        rows.append((int(current), "Battery Grid Charging Current", amps))
        rows.append((int(enable), "Battery Grid Charging", 1 if amps > 0 else 0))
        return rows, 100, 1
    amps = _discharge_limit_amps(target_w, live)
    if item.get("max_discharge_register") is not None:
        rows.append((int(item["max_discharge_register"]), "Battery Max Discharging Current", amps))
    rows.append((int(current), "Battery Grid Charging Current", 0))
    rows.append((int(enable), "Battery Grid Charging", 0))
    return rows, TOU_HOLD_SOC, 1


def _needed_discharge_amps(export_w: int, live: Optional[Dict[str, Any]]) -> int:
    """Entladestrom für die Einspeisung, mit Reserve. Steigt sofort, fällt in kleinen Schritten."""
    global _discharge_amps
    soc = None if not live else live.get("soc_percent")
    if soc is not None and float(soc) <= SOC_EMPTY:
        _discharge_amps = 0
        return 0
    need = _battery_need_w(-export_w, live)
    if need is None:
        goal = _amps_for(export_w + DISCHARGE_HEADROOM_W, MAX_DISCHARGE_AMPS)
    else:
        goal = _amps_for(-need + DISCHARGE_HEADROOM_W, MAX_DISCHARGE_AMPS)
    if goal >= _discharge_amps:
        _discharge_amps = goal
    else:
        _discharge_amps = max(goal, _discharge_amps - AMP_STEP_DOWN)
    return _discharge_amps


def _track_sell_trim(export_w: int, live: Optional[Dict[str, Any]]) -> None:
    global _peak_trim_w
    grid = _grid_w(live)
    if grid is None:
        return
    error = -grid - export_w
    if abs(error) > 400 or abs(error) <= 25:
        return
    _peak_trim_w = max(PEAK_TRIM_MIN_W, min(PEAK_TRIM_MAX_W, _peak_trim_w - int(round(error * PEAK_TRIM_GAIN))))


def _feed_writes(
    item: Dict[str, Any],
    export_w: int,
    live: Optional[Dict[str, Any]],
    settings: Dict[str, Any],
) -> tuple:
    """Feste Einspeisung: Export First, Batterie entlädt über die Zeitschaltuhr,
    Export Surplus Power deckelt auf den Soll. Liefert (Registerzeilen, TOU-SOC, TOU-Laden)."""
    global _hold_mode
    _hold_mode = "feed"
    rows: List[tuple] = []
    current = item.get("grid_charge_current_register")
    enable = item.get("grid_charge_register")
    if item.get("sell_power_register") is None or item.get("work_mode_register") is None:
        return rows, TOU_HOLD_SOC, 0
    _track_sell_trim(export_w, live)
    sell_w = max(0, min(SELL_POWER_MAX_W, export_w + _peak_trim_w))
    if current is not None:
        rows.append((int(current), "Battery Grid Charging Current", 0))
    if enable is not None:
        rows.append((int(enable), "Battery Grid Charging", 0))
    rows.append((int(item["work_mode_register"]), "Work Mode", WORK_MODE_EXPORT_FIRST))
    if item.get("energy_pattern_register") is not None:
        rows.append((int(item["energy_pattern_register"]), "Energy Pattern", ENERGY_PATTERN_LOAD_FIRST))
    if item.get("solar_sell_register") is not None:
        rows.append((int(item["solar_sell_register"]), "Solar Sell", 1))
    rows.append((int(item["sell_power_register"]), "Export Surplus Power", sell_w))
    if item.get("time_of_use_register") is not None:
        rows.append((int(item["time_of_use_register"]), "Time of Use", TOU_WEEK_ENABLED))
    if item.get("grid_peak_power_register") is not None:
        rows.append((int(item["grid_peak_power_register"]), "Grid Peak shaving", _idle_peak_w(settings)))
    if item.get("max_import_register") is not None:
        rows.append((int(item["max_import_register"]), "Grid Max Import power", (_idle_peak_w(settings) + 9) // 10))
    if item.get("max_discharge_register") is not None:
        rows.append(
            (
                int(item["max_discharge_register"]),
                "Battery Max Discharging Current",
                _needed_discharge_amps(export_w, live),
            )
        )
    return rows, TOU_HOLD_SOC, 0


def _tou_program_writes(item: Dict[str, Any], tou_soc: int, charging: int, with_power: bool) -> List[tuple]:
    if item.get("time_of_use_register") is None:
        return []
    value = max(TOU_HOLD_SOC, min(100, int(tou_soc)))
    rows: List[tuple] = []
    for index in range(6):
        if with_power:
            rows.append((0x009A + index, f"Program {index + 1} Power", TOU_HOLD_POWER_W))
        rows.append((0x00A6 + index, f"Program {index + 1} SOC", value))
        rows.append((0x00AC + index, f"Program {index + 1} Charging", int(charging)))
    return rows


def _phrase(address: int, name: str, value: int) -> str:
    return f"Register {int(address)} (0x{int(address):04X}, {name}), Wert {int(value)}"


def _send_holding(host: str, port: int, unit: int, serial: int, address: int, value: int) -> None:
    """Ein Holding-Register. Am Solarman-Logger Funktionscode 16, sonst Funktionscode 6.

    Der Logger beantwortet parallel die Abfragen von Home Assistant und verwirft dabei
    gelegentlich einen Rahmen. Darum bis zu drei Versuche.
    """
    for attempt in range(3):
        try:
            if int(port) == 8899 or serial:
                if not serial:
                    raise OSError("Solarman-Seriennummer fehlt")
                write_solarman_multiple(host, port, unit, serial, address, value)
            else:
                write_register(host, port, unit, address, value)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5)


def _mark_release_pending(settings: Dict[str, Any]) -> None:
    if settings.get("release_pending"):
        return
    settings["release_pending"] = True
    _persist_settings(settings)


def _clear_release_pending(settings: Dict[str, Any]) -> None:
    if not settings.get("release_pending"):
        return
    settings["release_pending"] = False
    _persist_settings(settings)


def _release_due(settings: Dict[str, Any]) -> bool:
    return bool(_needs_release or settings.get("release_pending"))


def _release_writes(item: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> List[tuple]:
    """Haltung einmal lösen: konfigurierte Standeinstellung, nicht fest verdrahtet."""
    if item.get("register") is None:
        return []
    idle_w = _idle_grid_w(settings)
    work_mode = _idle_work_mode(settings)
    pattern = _idle_energy_pattern(settings)
    peak_w = _idle_peak_w(settings)
    tou_soc = _idle_tou_soc(settings)
    tou_charge = _idle_tou_charging(settings)
    tou_power = _idle_tou_power_w(settings)
    rows: List[tuple] = []
    if item.get("work_mode_register") is not None:
        rows.append((int(item["work_mode_register"]), "Work Mode", work_mode))
    if item.get("energy_pattern_register") is not None:
        rows.append((int(item["energy_pattern_register"]), "Energy Pattern", pattern))
    rows.append((int(item["register"]), item.get("register_name") or "Soll", idle_w))
    current = item.get("grid_charge_current_register")
    enable = item.get("grid_charge_register")
    if current is not None:
        rows.append((int(current), "Battery Grid Charging Current", 0))
    if enable is not None:
        rows.append((int(enable), "Battery Grid Charging", 0))
    if item.get("max_discharge_register") is not None:
        rows.append((int(item["max_discharge_register"]), "Battery Max Discharging Current", MAX_DISCHARGE_AMPS))
    if item.get("grid_peak_power_register") is not None:
        rows.append((int(item["grid_peak_power_register"]), "Grid Peak shaving", peak_w))
    if item.get("max_import_register") is not None:
        rows.append((int(item["max_import_register"]), "Grid Max Import power", min(800, (peak_w + 9) // 10)))
    if item.get("solar_sell_register") is not None:
        rows.append((int(item["solar_sell_register"]), "Solar Sell", SOLAR_SELL_OFF))
    if item.get("sell_power_register") is not None:
        rows.append((int(item["sell_power_register"]), "Export Surplus Power", SELL_POWER_IDLE_W))
    schedule = item.get("time_of_use_register")
    if schedule is not None:
        rows.append((int(schedule), "Time of Use", TOU_WEEK_ENABLED))
        for index in range(6):
            rows.append((0x009A + index, f"Program {index + 1} Power", tou_power))
            rows.append((0x00A6 + index, f"Program {index + 1} SOC", tou_soc))
            rows.append((0x00AC + index, f"Program {index + 1} Charging", tou_charge))
    return rows


def _send_rows(settings: Dict[str, Any], serial: int, writes: List[tuple], raw: Optional[int]) -> Dict[str, Any]:
    done: List[str] = []
    try:
        for address, name, value in writes:
            _send_holding(
                settings["modbus_host"],
                settings["modbus_port"],
                settings["modbus_unit"],
                serial,
                address,
                value,
            )
            done.append(_phrase(address, name, value))
    except OSError as exc:
        written = "; ".join(done) if done else "Schreiben"
        return _write_result(False, f"{written}. Schreiben fehlgeschlagen: {exc}.", raw)
    return _write_result(True, "Geschrieben: " + "; ".join(done) + ".", raw)


def apply_write(
    settings: Dict[str, Any],
    plan: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    global _needs_release, _tou_hold_applied, _tou_soc_applied, _release_sent
    base = INVERTERS.get(settings.get("inverter"), INVERTERS[DEFAULT_INVERTER])
    link = resolve_ha_link(str(settings.get("inverter") or ""), settings.get("known_entry_ids") or [])
    item = _control_item(base, link)
    register_text = _register_text(item)
    if not settings.get("real_mode"):
        if settings.get("release_pending"):
            _release_sent = False
        if not _release_due(settings):
            return _write_result(False, "Aus. Es wird nichts geschrieben.")
        if _release_sent:
            _needs_release = False
            _clear_release_pending(settings)
            return _write_result(False, "Aus. Es wird nichts geschrieben.")
        host = settings.get("modbus_host") or ""
        writes = _release_writes(item, settings)
        if not host or not writes:
            _needs_release = False
            _release_sent = True
            _clear_release_pending(settings)
            return _write_result(False, "Aus. Es wird nichts geschrieben.")
        serial = int(link.get("serial") or 0) if link else 0
        with _lock:
            if _release_sent or not _release_due(settings):
                return _write_result(False, "Aus. Es wird nichts geschrieben.")
            result = _send_rows(settings, serial, writes, _idle_grid_w(settings))
            _reset_hold(None)
            _last_write["at"] = time.monotonic()
            _last_write["signature"] = ("release",)
            _last_write["result"] = result
            if result.get("command_sent"):
                _release_sent = True
                _needs_release = False
                _clear_release_pending(settings)
                _sync_ha_release(settings)
            else:
                _release_sent = False
            return result
    if not item.get("writable") or item.get("register") is None:
        return _write_result(False, item.get("note") or "Für diesen Typ wird nichts geschrieben.")
    target_w = int(round(float(plan.get("target_grid_kw") or 0) * 1000))
    # 0x0068 bleibt auf der Standeinstellung. Ein hoher Zero-Export-Wert öffnet nur die
    # Einspeisung, er hält keinen Zukauf. Den Halt machen Kappung, Netzladung und Entladestrom.
    raw = _idle_grid_w(settings)
    host = settings.get("modbus_host") or ""
    if not host:
        return _write_result(False, f"{register_text}. Modbus-Host fehlt, es wurde nichts geschrieben.", raw)
    serial = int(link.get("serial") or 0) if link else 0
    if target_w != _hold_target_w:
        _reset_hold(target_w)
    writes = [(int(item["register"]), item.get("register_name") or "Soll", int(raw))]
    if target_w < 0:
        rows, tou_soc, tou_charging = _feed_writes(item, -target_w, live, settings)
    else:
        peak_flags = None
        if item.get("grid_peak_enable_register") is not None and serial:
            try:
                peak_flags = read_solarman_holding(
                    host,
                    settings["modbus_port"],
                    settings["modbus_unit"],
                    serial,
                    int(item["grid_peak_enable_register"]),
                )
            except OSError:
                peak_flags = None
        rows, tou_soc, tou_charging = _import_writes(item, target_w, live, peak_flags)
    writes.extend(rows)
    tou_state = (tou_soc, tou_charging)
    if not _tou_hold_applied or tou_state != _tou_soc_applied:
        writes.extend(_tou_program_writes(item, tou_soc, tou_charging, with_power=not _tou_hold_applied))
    signature = (
        item["id"],
        tuple((address, value) for address, _name, value in writes),
        host,
        settings["modbus_port"],
        settings["modbus_unit"],
    )
    now = time.monotonic()
    with _lock:
        _needs_release = True
        _release_sent = False
        _mark_release_pending(settings)
        if signature == _last_write["signature"] and now - float(_last_write["at"]) < SAMPLE_GAP_SEC:
            previous = _last_write["result"] or _write_result(False, "unverändert")
            return previous
        result = _send_rows(settings, serial, writes, raw)
        _last_write["at"] = now
        _last_write["signature"] = signature
        _last_write["result"] = result
        if result.get("command_sent"):
            _tou_hold_applied = True
            _tou_soc_applied = tou_state
        else:
            # Ein abgebrochener Durchlauf kann die Programme halb geschrieben haben.
            _tou_hold_applied = False
        return result


def _sync_ha_release(settings: Optional[Dict[str, Any]] = None) -> None:
    """Nach dem Ausschalten HA auf die konfigurierte Standeinstellung setzen."""
    try:
        from ha_device_control import ha_service, read_ha_entity_state
    except Exception:
        return
    idle_w = _idle_grid_w(settings)
    work_name = "Export First" if _idle_work_mode(settings) == WORK_MODE_EXPORT_FIRST else "Zero Export To CT"
    pattern_name = "Battery First" if _idle_energy_pattern(settings) == ENERGY_PATTERN_BATTERY_FIRST else "Load First"
    charge_name = {0: "Disabled", 1: "Grid", 2: "Generator", 3: "Both"}[_idle_tou_charging(settings)]
    prefix = "number.192_168_1_176"
    select = "select.192_168_1_176"
    wanted = [
        ("number", "set_value", f"{prefix}_zero_export_power", {"value": idle_w}),
        ("number", "set_value", f"{prefix}_grid_peak_shaving", {"value": _idle_peak_w(settings)}),
        ("number", "set_value", f"{prefix}_battery_grid_charging_current", {"value": 0}),
        ("select", "select_option", f"{select}_work_mode", {"option": work_name}),
        ("select", "select_option", f"{select}_energy_pattern", {"option": pattern_name}),
        ("switch", "turn_off", "switch.192_168_1_176_battery_grid_charging", {}),
    ]
    for index in range(1, 7):
        wanted.append(("number", "set_value", f"{prefix}_program_{index}_soc", {"value": _idle_tou_soc(settings)}))
        wanted.append(("number", "set_value", f"{prefix}_program_{index}_power", {"value": _idle_tou_power_w(settings)}))
        wanted.append(("select", "select_option", f"{select}_program_{index}_charging", {"option": charge_name}))
    for domain, service, entity, extra in wanted:
        current = (read_ha_entity_state(entity) or "").strip()
        target = str(extra.get("value") if extra.get("value") is not None else extra.get("option") or "")
        if service == "turn_off" and current in ("off", "0"):
            continue
        if target and current == target:
            continue
        try:
            data = {"entity_id": entity}
            data.update(extra)
            ha_service(domain, service, data)
        except Exception:
            pass


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
                settings = load_settings()
                if settings.get("real_mode"):
                    reapply_dauerzukauf_test()
                live = read_live()
                plan = simulate(
                    live.get("grid_kw"),
                    live.get("battery_kw"),
                    settings["target_grid_kw"],
                    settings["peak_cap_kw"],
                )
                item = INVERTERS[settings["inverter"]]
                write = apply_write(settings, plan, live)
                if settings.get("real_mode") or write.get("command_sent"):
                    _remember(live, plan, write, item["label"])
            except Exception:
                pass
            time.sleep(SAMPLE_GAP_SEC)

    threading.Thread(target=loop, name="bms-modbus", daemon=True).start()


def read_register_state() -> Dict[str, Any]:
    """Aktuelle Soll-Register aus Home Assistant, für die Netzbezug-Seite."""
    try:
        from ha_device_control import read_ha_entity_state
    except Exception:
        return {}
    return {
        "zero_export_w": read_ha_entity_state("number.192_168_1_176_zero_export_power"),
        "work_mode": read_ha_entity_state("select.192_168_1_176_work_mode"),
        "energy_pattern": read_ha_entity_state("select.192_168_1_176_energy_pattern"),
        "peak_w": read_ha_entity_state("number.192_168_1_176_grid_peak_shaving"),
        "grid_charge": read_ha_entity_state("switch.192_168_1_176_battery_grid_charging"),
        "grid_charge_a": read_ha_entity_state("number.192_168_1_176_battery_grid_charging_current"),
        "max_discharge_a": read_ha_entity_state("number.192_168_1_176_battery_max_discharging_current"),
        "program_soc": read_ha_entity_state("number.192_168_1_176_program_5_soc"),
        "program_charging": read_ha_entity_state("select.192_168_1_176_program_5_charging"),
        "program_power": read_ha_entity_state("number.192_168_1_176_program_5_power"),
        "max_export_w": read_ha_entity_state("number.192_168_1_176_grid_max_export_power"),
        "solar_sell": read_ha_entity_state("switch.192_168_1_176_export_surplus"),
    }


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
    # Nur der Hintergrundlauf schreibt. Ein Seitenabruf alle 5 s würde sonst
    # denselben Strom noch einmal nachziehen und die Schwingung verstärken.
    write = _last_write["result"] or _write_result(False, "Aus. Es wird nichts geschrieben.")
    plan["command_sent"] = bool(write.get("command_sent"))
    plan["write_text"] = write.get("write_text") or ""
    plan["raw"] = write.get("raw")
    log = _load_log()
    del record
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
        "registers": read_register_state(),
        "assessment": judgement,
        "plan": plan,
        "log": log[:LOG_LIMIT],
        "command_sent": bool(write["command_sent"]),
    }
