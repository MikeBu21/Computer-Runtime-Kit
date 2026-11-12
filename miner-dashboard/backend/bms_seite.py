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
LOG_FILE = BACKEND_DIR / "bms_test_log.json"
DAUERZUKAUF_TEST_FILE = BACKEND_DIR / "dauerzukauf_test.json"
TZ = ZoneInfo("Europe/Vienna")

# Solarman-Logger-Discovery (MAC/IP/Serial). Verbindung/Modbus läuft über Port 8899.
LOGGER_DISCOVERY_PORT = 48899
LOGGER_DISCOVERY_TIMEOUT = 1.5
LOGGER_DISCOVERY_MESSAGES = (b"WIFIKIT-214028-READ", b"HF-A11ASSISTHREAD")
# Stündliche MAC-Prüfung: IP-Wechsel → Settings + Home Assistant nachziehen.
LOGGER_TRACK_GAP_SEC = 3600
DEFAULT_LOGGER_MAC = "d4:27:87:0a:d7:b4"


def _ha_storage_dir() -> Optional[Path]:
    """Live-Home-Assistant zuerst, dann Repo-Kopie."""
    candidates = (
        Path.home() / "homeassistant" / "config" / ".storage",
        BACKEND_DIR.parents[1] / "homeassistant" / "config" / ".storage",
    )
    for path in candidates:
        if (path / "core.config_entries").is_file():
            return path
    return None


def _ha_config_entries_path() -> Path:
    root = _ha_storage_dir()
    if root is not None:
        return root / "core.config_entries"
    return BACKEND_DIR.parents[1] / "homeassistant" / "config" / ".storage" / "core.config_entries"


def _ha_device_registry_path() -> Path:
    root = _ha_storage_dir()
    if root is not None:
        return root / "core.device_registry"
    return BACKEND_DIR.parents[1] / "homeassistant" / "config" / ".storage" / "core.device_registry"


# Rückwärtskompatibel für bestehenden Code.
HA_CONFIG_ENTRIES = _ha_config_entries_path()
HA_DEVICE_REGISTRY = _ha_device_registry_path()

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
# Register 0x00B2, Bit 0: Microinverter Export cut-off. Aus, sobald kein Zukauf mehr gehalten wird.
MICRO_EXPORT_CUTOFF_BIT = 0
# Schalter Generator/Microinverter, Register 0x0084. 0 = aus.
REG_MICROINVERTER = 0x0084
MAX_DISCHARGE_AMPS = 240
TOU_HOLD_SOC = 20
TOU_HOLD_POWER_W = 12000
# Werte des Elektrikers bei der Installation (am SG04LP3 gelesen). Sie werden beim
# Ausschalten zurückgeschrieben; Ströme und Leistungen sind zugleich Obergrenzen.
INSTALL_DEFAULTS = {
    "install_max_charge_a": 240,
    "install_max_discharge_a": 240,
    "install_max_sell_w": 13180,
    "install_max_import_w": 8000,
    "install_shutdown_soc": 9,
    "install_restart_soc": 10,
    "install_low_soc": 11,
}
INSTALL_RANGES = {
    "install_max_charge_a": (0, 350),
    "install_max_discharge_a": (0, 350),
    "install_max_sell_w": (0, 24000),
    "install_max_import_w": (0, 80000),
    "install_shutdown_soc": (0, 100),
    "install_restart_soc": (0, 100),
    "install_low_soc": (0, 100),
}
REG_MAX_CHARGE = 0x006C
REG_MAX_DISCHARGE = 0x006D
REG_SELL_POWER = 0x008F
REG_MAX_IMPORT = 0x00E9
REG_SHUTDOWN_SOC = 0x0073
REG_RESTART_SOC = 0x0074
REG_LOW_SOC = 0x0075
REG_PROGRAM_TIME = 0x0094
REG_PROGRAM_POWER = 0x009A
REG_PROGRAM_SOC = 0x00A6
REG_PROGRAM_CHARGE = 0x00AC
PROGRAM_SLOTS = 6
# Elektriker-Register: (Adresse, Faktor Rohwert → gespeicherter Wert). 0x00E9 zählt 10-W-Schritte.
INSTALL_REGISTER_SCALE = {
    "install_max_charge_a": (REG_MAX_CHARGE, 1),
    "install_max_discharge_a": (REG_MAX_DISCHARGE, 1),
    "install_max_sell_w": (REG_SELL_POWER, 1),
    "install_max_import_w": (REG_MAX_IMPORT, 10),
    "install_shutdown_soc": (REG_SHUTDOWN_SOC, 1),
    "install_restart_soc": (REG_RESTART_SOC, 1),
    "install_low_soc": (REG_LOW_SOC, 1),
}


def _ui_modbus_defaults(inverter_id: str) -> Dict[str, int]:
    """Vorschläge für Port/Unit im Formular. Deye läuft über Solarman (8899), sonst Modbus TCP 502."""
    family = _family(inverter_id)
    if family == "deye":
        return {"port": 8899, "unit": 1}
    return {"port": 502, "unit": 1}


def inverter_choices(selected: str = "") -> List[Dict[str, Any]]:
    # UI zeigt vorerst nur den aktiven Typ; andere Inverter sind noch nicht freigegeben.
    active_id = selected if selected in INVERTERS else DEFAULT_INVERTER
    link = resolve_ha_link(active_id, None) if active_id else None
    rows = []
    for item in INVERTER_LIST:
        if item["id"] != active_id:
            continue
        shown = _control_item(item, link) if item["id"] == active_id else item
        defaults = _ui_modbus_defaults(shown["id"])
        rows.append(
            {
                "id": shown["id"],
                "label": shown["label"],
                "detail": shown["detail"],
                "register_text": _register_text(shown),
                "note": shown.get("note") or "",
                "default_port": defaults["port"],
                "default_unit": defaults["unit"],
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
        "install_max_charge_a": INSTALL_DEFAULTS["install_max_charge_a"],
        "install_max_discharge_a": INSTALL_DEFAULTS["install_max_discharge_a"],
        "install_max_sell_w": INSTALL_DEFAULTS["install_max_sell_w"],
        "install_max_import_w": INSTALL_DEFAULTS["install_max_import_w"],
        "install_shutdown_soc": INSTALL_DEFAULTS["install_shutdown_soc"],
        "install_restart_soc": INSTALL_DEFAULTS["install_restart_soc"],
        "install_low_soc": INSTALL_DEFAULTS["install_low_soc"],
        "modbus_host": "",
        "modbus_port": 502,
        "modbus_unit": 1,
        "logger_ip": "",
        "logger_mac": "",
        "logger_serial": 0,
        "logger_tracked_at": "",
        "tracked_inverters": [],
        "control_tracking_removed": False,
        "release_pending": False,
        "planning_hold": False,
        "community_hold": False,
        "exchange_hold": False,
        "install_captured": False,
        "install_captured_serial": 0,
        "program_soc": [],
        "program_power": [],
        "program_charging": [],
        "program_time": [],
        "soc_stop": False,
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


def _install_value(settings: Optional[Dict[str, Any]], key: str) -> int:
    raw = INSTALL_DEFAULTS[key]
    if settings is not None and settings.get(key) not in (None, ""):
        raw = settings.get(key)
    try:
        value = int(round(float(str(raw).replace(",", ".").strip())))
    except (TypeError, ValueError):
        value = INSTALL_DEFAULTS[key]
    lowest, highest = INSTALL_RANGES[key]
    return max(lowest, min(highest, value))


def _coerce_program_list(raw: Any, highest: int = 65535) -> Optional[List[int]]:
    """Sechs gespeicherte Programmwerte. Unvollständig zählt als nicht gelesen."""
    if not isinstance(raw, (list, tuple)) or len(raw) != PROGRAM_SLOTS:
        return None
    values: List[int] = []
    for item in raw:
        try:
            number = int(round(float(str(item).replace(",", ".").strip())))
        except (TypeError, ValueError):
            return None
        values.append(max(0, min(highest, number)))
    return values


def install_caps_kw(settings: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Aktuelle Elektriker-Obergrenzen in kW, jedes Mal aus der Netzbezug-Einstellung."""
    if settings is None:
        settings = load_settings()
    return {
        "import_kw": _install_value(settings, "install_max_import_w") / 1000.0,
        "sell_kw": _install_value(settings, "install_max_sell_w") / 1000.0,
    }


def _install_caps(settings: Optional[Dict[str, Any]]) -> Dict[int, int]:
    """Obergrenzen je Register aus den Elektriker-Werten. Die Steuerung schreibt nie darüber."""
    charge_a = _install_value(settings, "install_max_charge_a")
    import_w = _install_value(settings, "install_max_import_w")
    return {
        REG_MAX_CHARGE: charge_a,
        0x0080: charge_a,
        0x006D: _install_value(settings, "install_max_discharge_a"),
        0x008F: _install_value(settings, "install_max_sell_w"),
        0x00BF: import_w,
        0x00E9: import_w // 10,
    }


def _cap_rows(rows: List[tuple], settings: Optional[Dict[str, Any]]) -> List[tuple]:
    caps = _install_caps(settings)
    return [
        (address, name, min(int(value), caps[address]) if address in caps else value)
        for address, name, value in rows
    ]


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


def _format_mac(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "").replace(":", "").replace(".", "")
    if len(text) != 12 or any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return ":".join(text[i : i + 2] for i in range(0, 12, 2))


def _parse_discovery_payload(payload: bytes) -> Optional[Dict[str, Any]]:
    parts = payload.decode("utf-8", errors="ignore").strip().split(",")
    if len(parts) != 3:
        return None
    ip = _host(parts[0])
    mac = _format_mac(parts[1])
    try:
        serial = int(parts[2])
    except (TypeError, ValueError):
        serial = 0
    if not ip or not mac or not serial:
        return None
    return {"ip": ip, "mac": mac, "serial": serial, "port": 8899}


def _discovery_targets(preferred_host: str = "") -> List[str]:
    """Unicast- und Broadcast-Ziele für die Logger-Suche."""
    targets: List[str] = []
    preferred = _host(preferred_host)
    if preferred:
        targets.append(preferred)
    targets.append("255.255.255.255")
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = _host(info[4][0])
            if not ip or ip.startswith("127."):
                continue
            parts = ip.split(".")
            if len(parts) == 4:
                targets.append(".".join(parts[:3] + ["255"]))
    except OSError:
        pass
    out: List[str] = []
    for item in targets:
        if item and item not in out:
            out.append(item)
    return out


def _collect_discovery(
    host: str = "",
    timeout: float = LOGGER_DISCOVERY_TIMEOUT,
    stop_mac: str = "",
) -> List[Dict[str, Any]]:
    """Alle Solarman-Logger einsammeln, die im Zeitfenster antworten. Eine MAC pro Gerät."""
    want_mac = _format_mac(stop_mac)
    found: Dict[str, Dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(max(0.2, float(timeout) / 4.0))
    try:
        for target in _discovery_targets(host):
            for message in LOGGER_DISCOVERY_MESSAGES:
                try:
                    sock.sendto(message, (target, LOGGER_DISCOVERY_PORT))
                except OSError:
                    continue
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            try:
                payload, _addr = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            item = _parse_discovery_payload(payload)
            if not item:
                continue
            found[item["mac"]] = item
            if want_mac and item["mac"] == want_mac:
                break
    finally:
        sock.close()
    return list(found.values())


def discover_loggers(timeout: float = LOGGER_DISCOVERY_TIMEOUT) -> List[Dict[str, Any]]:
    """Alle Logger im lokalen Netz (Broadcast), nicht nur die bekannte MAC."""
    return _collect_discovery("", timeout=timeout)


def discover_logger(
    host: str = "",
    timeout: float = LOGGER_DISCOVERY_TIMEOUT,
    mac: str = "",
) -> Optional[Dict[str, Any]]:
    """Einen Logger per Solarman-UDP finden (IP und/oder Broadcast), optional nach MAC filtern."""
    want_mac = _format_mac(mac)
    items = _collect_discovery(host, timeout=timeout, stop_mac=want_mac)
    preferred = _host(host)
    for found in items:
        if want_mac and found["mac"] != want_mac:
            continue
        if preferred and preferred not in ("255.255.255.255", "<broadcast>"):
            if found["ip"] != preferred and not want_mac:
                continue
        return found
    if want_mac:
        return _discover_logger_via_arp(want_mac, timeout=timeout)
    return None


def _unicast_discover(host: str, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """Nur Unicast an eine bekannte IP — ohne Broadcast/ARP-Rekursion."""
    target = _host(host)
    if not target:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(max(0.2, float(timeout) / 2.0))
    try:
        for message in LOGGER_DISCOVERY_MESSAGES:
            try:
                sock.sendto(message, (target, LOGGER_DISCOVERY_PORT))
            except OSError:
                continue
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            try:
                payload, _addr = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            found = _parse_discovery_payload(payload)
            if found and found["ip"] == target:
                return found
    finally:
        sock.close()
    return None


def _discover_logger_via_arp(mac: str, timeout: float = LOGGER_DISCOVERY_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Fallback: Nachbarschaftstabelle nach MAC durchsuchen und per Unicast bestätigen."""
    want = _format_mac(mac)
    if not want:
        return None
    try:
        text = Path("/proc/net/arp").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, hw = _host(parts[0]), _format_mac(parts[3])
        if ip and hw == want:
            confirmed = _unicast_discover(ip, timeout=min(1.0, timeout))
            if confirmed and _format_mac(confirmed.get("mac")) == want:
                return confirmed
            return {"ip": ip, "mac": want, "serial": 0, "port": 8899}
    return None


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ha_entry_ids_for_mac(mac: str) -> List[str]:
    """Config-Entry-IDs aus der Geräte-Registry, die zu dieser Logger-MAC gehören."""
    mac_text = _format_mac(mac)
    if not mac_text:
        return []
    path = _ha_device_registry_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    devices = raw.get("data", {}).get("devices") if isinstance(raw, dict) else None
    if not isinstance(devices, list):
        return []
    out: List[str] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        connections = device.get("connections") or []
        has_mac = any(
            isinstance(item, list) and len(item) > 1 and item[0] == "mac" and _format_mac(item[1]) == mac_text
            for item in connections
        )
        if not has_mac:
            continue
        for entry_id in device.get("config_entries") or []:
            text = str(entry_id or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def sync_logger_to_ha(ip: str, mac: str, serial: int = 0) -> Dict[str, Any]:
    """Logger-IP und MAC in Home-Assistant Config/Device-Registry nachziehen."""
    host = _host(ip)
    mac_text = _format_mac(mac)
    result = {
        "ok": False,
        "config_updated": False,
        "device_updated": False,
        "ha_restarted": False,
        "message": "",
    }
    if not host or not mac_text:
        result["message"] = "IP oder MAC fehlt."
        return result
    try:
        serial_num = int(serial or 0)
    except (TypeError, ValueError):
        serial_num = 0

    config_path = _ha_config_entries_path()
    device_path = _ha_device_registry_path()
    mac_entry_ids = set(_ha_entry_ids_for_mac(mac_text))
    touched_entries: List[str] = []

    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        entries = raw.get("data", {}).get("entries") if isinstance(raw, dict) else None
        if isinstance(entries, list):
            changed = False
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("domain") != "solarman":
                    continue
                entry_id = str(entry.get("entry_id") or "")
                blob = _entry_blob(entry)
                old_host = _host(blob.get("host") or blob.get("ip") or blob.get("inverter_host") or entry.get("title"))
                try:
                    old_serial = int(blob.get("serial") or 0)
                except (TypeError, ValueError):
                    old_serial = 0
                match = (
                    entry_id in mac_entry_ids
                    or (old_host and old_host == host)
                    or (serial_num and old_serial and old_serial == serial_num)
                )
                # Fallback: genau ein Solarman-Eintrag mit Host → dem zuordnen
                if not match and not mac_entry_ids and old_host and not touched_entries:
                    solarman_with_host = [
                        item for item in entries
                        if isinstance(item, dict)
                        and item.get("domain") == "solarman"
                        and _host(_entry_blob(item).get("host") or item.get("title"))
                    ]
                    if len(solarman_with_host) == 1 and solarman_with_host[0] is entry:
                        match = True
                if not match:
                    continue
                data = entry.setdefault("data", {})
                options = entry.setdefault("options", {})
                if not isinstance(data, dict):
                    entry["data"] = {}
                    data = entry["data"]
                if not isinstance(options, dict):
                    entry["options"] = {}
                    options = entry["options"]
                if data.get("host") != host:
                    data["host"] = host
                    changed = True
                if options.get("host") != host:
                    options["host"] = host
                    changed = True
                if entry.get("title") in ("", None, old_host) or str(entry.get("title") or "").startswith("192.168."):
                    if entry.get("title") != host:
                        entry["title"] = host
                        changed = True
                if serial_num and data.get("serial") != serial_num:
                    data["serial"] = serial_num
                    changed = True
                if data.get("port") in (None, "", 0):
                    data["port"] = 8899
                    changed = True
                touched_entries.append(entry_id)
            if changed:
                _atomic_json_write(config_path, raw)
                result["config_updated"] = True

    if device_path.is_file():
        try:
            raw = json.loads(device_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        devices = raw.get("data", {}).get("devices") if isinstance(raw, dict) else None
        if isinstance(devices, list):
            changed = False
            for device in devices:
                if not isinstance(device, dict):
                    continue
                identifiers = device.get("identifiers") or []
                solarman = any(
                    isinstance(item, list) and item and item[0] == "solarman" for item in identifiers
                )
                if not solarman:
                    continue
                config_ids = [str(item) for item in (device.get("config_entries") or [])]
                connections = device.get("connections") if isinstance(device.get("connections"), list) else []
                has_mac = any(
                    isinstance(item, list) and len(item) > 1 and item[0] == "mac" and _format_mac(item[1]) == mac_text
                    for item in connections
                )
                text = " ".join(
                    str(device.get(key) or "")
                    for key in ("name", "name_by_user", "configuration_url", "serial_number")
                )
                linked = any(item and item in config_ids for item in touched_entries)
                serial_hit = serial_num and (
                    str(serial_num) in text
                    or any(
                        isinstance(item, list) and len(item) > 1 and str(item[1]) == str(serial_num)
                        for item in identifiers
                    )
                )
                host_hit = host and host in text
                if not (has_mac or linked or serial_hit or host_hit):
                    continue
                if not isinstance(device.get("connections"), list):
                    device["connections"] = []
                    connections = device["connections"]
                mac_conn = ["mac", mac_text]
                if mac_conn not in connections:
                    connections[:] = [
                        item for item in connections
                        if not (isinstance(item, list) and item and item[0] == "mac")
                    ]
                    connections.append(mac_conn)
                    changed = True
                if serial_num and str(device.get("serial_number") or "") != str(serial_num):
                    device["serial_number"] = str(serial_num)
                    changed = True
                if serial_num:
                    serial_id = ["solarman", str(serial_num)]
                    if isinstance(identifiers, list) and serial_id not in identifiers:
                        identifiers.append(serial_id)
                        device["identifiers"] = identifiers
                        changed = True
                new_url = f"http://{host}/config_hide.html"
                if device.get("configuration_url") != new_url:
                    device["configuration_url"] = new_url
                    changed = True
                if str(device.get("name") or "").startswith("192.168.") and device.get("name") != host:
                    device["name"] = host
                    changed = True
            if changed:
                _atomic_json_write(device_path, raw)
                result["device_updated"] = True

    if result["config_updated"]:
        result["ha_restarted"] = _restart_home_assistant()

    result["ok"] = True
    result["ip"] = host
    result["mac"] = mac_text
    result["serial"] = serial_num
    parts = []
    if result["config_updated"]:
        parts.append("HA-Config")
    if result["device_updated"]:
        parts.append("Geräte-Registry")
    if result["ha_restarted"]:
        parts.append("HA neu gestartet")
    result["message"] = (
        "Home Assistant aktualisiert (" + ", ".join(parts) + ")."
        if parts
        else "Home Assistant war schon aktuell."
    )
    return result


def _restart_home_assistant() -> bool:
    """HA neu laden, damit geänderte Logger-IP aus der Config greift."""
    try:
        import subprocess

        env = dict(**{k: v for k, v in __import__("os").environ.items()})
        uid = __import__("os").getuid()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
        proc = subprocess.run(
            ["systemctl", "--user", "restart", "home-assistant.service"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def track_logger(host: Optional[str] = None, persist: bool = True) -> Dict[str, Any]:
    """Logger per MAC/Broadcast finden, speichern und bei IP-Wechsel nach HA schreiben."""
    # load_settings ohne frischen HA-Overwrite der getrackten IP: Rohdaten + Defaults
    settings = load_settings()
    mac = _format_mac(settings.get("logger_mac")) or DEFAULT_LOGGER_MAC
    target = _host(host) or _host(settings.get("logger_ip")) or _host(settings.get("modbus_host"))
    found = discover_logger(target, mac=mac)
    if not found and target:
        found = discover_logger(target, mac="")
    if not found:
        return {
            "ok": False,
            "message": (
                f"Kein Logger mit MAC {mac} gefunden "
                f"(Discovery Port {LOGGER_DISCOVERY_PORT}, stündliche Prüfung)."
            ),
            "mac": mac,
            "ip": target or "",
        }
    previous = {
        "ip": _host(settings.get("logger_ip") or settings.get("modbus_host")),
        "mac": _format_mac(settings.get("logger_mac")),
        "serial": int(settings.get("logger_serial") or 0),
    }
    changed = (
        previous["ip"] != found["ip"]
        or previous["mac"] != found["mac"]
        or (int(found["serial"]) and previous["serial"] != int(found["serial"]))
    )
    settings["logger_ip"] = found["ip"]
    settings["logger_mac"] = found["mac"]
    if int(found["serial"]):
        settings["logger_serial"] = int(found["serial"])
    settings["logger_tracked_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    # Getrackte IP ist führend für Modbus
    settings["modbus_host"] = found["ip"]
    if int(settings.get("modbus_port") or 0) in (0, 502) and _family(str(settings.get("inverter") or "")) == "deye":
        settings["modbus_port"] = 8899
    ha = {"ok": True, "message": "Unverändert — kein HA-Sync nötig."}
    if changed or not previous["ip"]:
        ha = sync_logger_to_ha(found["ip"], found["mac"], int(settings.get("logger_serial") or 0))
    if persist:
        _persist_settings(settings)
    read_note = ""
    if _should_autocapture(previous["serial"], int(settings.get("logger_serial") or 0), settings):
        read = capture_install_from_inverter()
        read_note = " " + str(read.get("message") or "")
    return {
        "ok": True,
        "changed": changed,
        "ip": found["ip"],
        "mac": found["mac"],
        "serial": int(settings.get("logger_serial") or 0),
        "port": 8899,
        "tracked_at": settings["logger_tracked_at"],
        "interval_sec": LOGGER_TRACK_GAP_SEC,
        "ha": ha,
        "message": (
            f"Logger {found['ip']} · MAC {found['mac']}"
            + (" · aktualisiert" if changed else " · unverändert")
            + f". {ha.get('message') or ''}"
            + read_note
        ).strip(),
    }


# Nicht für die Anzeige. Ladestand, Batterie, Netz und PV kommen nur aus Home Assistant.
# 0x00BA–0x00BD sind Sollwerte, keine Messung, und werden nicht gelesen.
REG_BATTERY_SOC = 0x00B8
REG_BATTERY_POWER = 0x00BE
REG_GRID_POWER = 0x00A9
_last_discovery: List[Dict[str, Any]] = []


def _normalize_tracked_inverter(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    mac = _format_mac(raw.get("mac"))
    if not mac:
        return None
    try:
        serial = int(raw.get("serial") or 0)
    except (TypeError, ValueError):
        serial = 0
    try:
        pv_kw = float(raw["pv_kw"]) if raw.get("pv_kw") not in (None, "") else None
    except (TypeError, ValueError):
        pv_kw = None
    return {
        "id": str(raw.get("id") or ("wr" + mac.replace(":", ""))),
        "name": str(raw.get("name") or "Wechselrichter").strip() or "Wechselrichter",
        "mac": mac,
        "serial": serial,
        "ip": _host(raw.get("ip")),
        "port": 8899,
        "unit": _unit(raw.get("unit") or 1),
        "role": "erzeugung",
        "control": bool(raw.get("control")),
        "flow_field_id": str(raw.get("flow_field_id") or ""),
        "tracked_at": str(raw.get("tracked_at") or ""),
        "pv_kw": pv_kw,
    }


def _seed_control_inverter(items: List[Dict[str, Any]], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Bekannten Steuer-Logger in die Liste nehmen, ohne einen zweiten Erzeugungs-Punkt."""
    if settings.get("control_tracking_removed"):
        return items
    mac = _format_mac(settings.get("logger_mac"))
    if not mac:
        return items
    if any(item["mac"] == mac for item in items):
        for item in items:
            if item["mac"] == mac:
                item["control"] = True
                if not item.get("ip"):
                    item["ip"] = _host(settings.get("logger_ip") or settings.get("modbus_host"))
                if not item.get("serial"):
                    item["serial"] = int(settings.get("logger_serial") or 0)
                if not item.get("tracked_at"):
                    item["tracked_at"] = str(settings.get("logger_tracked_at") or "")
        return items
    items.insert(0, {
        "id": "control",
        "name": "Steuer-Logger",
        "mac": mac,
        "serial": int(settings.get("logger_serial") or 0),
        "ip": _host(settings.get("logger_ip") or settings.get("modbus_host")),
        "port": 8899,
        "unit": _unit(settings.get("modbus_unit") or 1),
        "role": "erzeugung",
        "control": True,
        "flow_field_id": "",
        "tracked_at": str(settings.get("logger_tracked_at") or ""),
        "pv_kw": None,
    })
    return items


def _normalize_tracked_inverters(raw: Any, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen: set = set()
    if isinstance(raw, list):
        for entry in raw:
            item = _normalize_tracked_inverter(entry)
            if not item or item["mac"] in seen:
                continue
            seen.add(item["mac"])
            items.append(item)
    return _seed_control_inverter(items, settings)


def annotate_discovery(
    found: List[Dict[str, Any]],
    tracked: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Scan-Ergebnis mit Markierung, ob die MAC schon gespeichert ist."""
    known = {_format_mac(item.get("mac")) for item in tracked}
    out: List[Dict[str, Any]] = []
    for item in found:
        mac = _format_mac(item.get("mac"))
        if not mac:
            continue
        out.append({
            "ip": _host(item.get("ip")),
            "mac": mac,
            "serial": int(item.get("serial") or 0),
            "port": 8899,
            "tracked": mac in known,
        })
    return out


def _attach_flow_field(name: str, field_id: str) -> bool:
    """Eigenen Stromfluss-Punkt mit Rolle Erzeugung anlegen. False, wenn kein Platz frei ist."""
    from dashboard_config import load_config, next_free_flow_slot, save_config

    cfg = load_config()
    fields = list(cfg.get("custom_flow_fields") or [])
    if any(str(field.get("id")) == field_id for field in fields):
        return True
    slot = next_free_flow_slot(cfg)
    if slot is None:
        return False
    fields.append({
        "id": field_id,
        "name": name,
        "icon": "☀️",
        "enabled": True,
        "sensors": [],
        "slot": slot,
        "role": "erzeugung",
    })
    cfg["custom_flow_fields"] = fields
    save_config(cfg)
    return True


def _detach_flow_field(field_id: str) -> None:
    if not field_id:
        return
    from dashboard_config import load_config, save_config

    cfg = load_config()
    fields = [
        field for field in (cfg.get("custom_flow_fields") or [])
        if str(field.get("id")) != field_id
    ]
    if len(fields) == len(cfg.get("custom_flow_fields") or []):
        return
    cfg["custom_flow_fields"] = fields
    save_config(cfg)


def scan_inverters() -> Dict[str, Any]:
    """LAN-Scan aller Solarman-Logger. Legt nichts an und schreibt nicht nach Home Assistant."""
    global _last_discovery
    found = discover_loggers()
    settings = load_settings()
    annotated = annotate_discovery(found, settings.get("tracked_inverters") or [])
    _last_discovery = annotated
    return {
        "ok": True,
        "found": annotated,
        "message": (
            f"{len(annotated)} Logger gefunden."
            if annotated
            else "Kein Solarman-Logger im Netz gefunden."
        ),
    }


def add_tracked_inverter(mac: str, name: str = "", serial: int = 0, host: str = "") -> Dict[str, Any]:
    """Wechselrichter nur im Dashboard speichern. MAC bleibt, bis der Eintrag gelöscht wird.

    Ohne MAC reicht eine IP: dann wird nur dieser Logger gefragt, nicht das ganze Netz.
    """
    settings = load_settings()
    previous_serial = int(settings.get("logger_serial") or 0)
    mac_text = _format_mac(mac)
    ip = _host(host)
    if not mac_text and ip:
        found = _unicast_discover(ip, timeout=2.0)
        if not found:
            return {"ok": False, "message": "An dieser IP antwortet kein Solarman-Logger."}
        mac_text = found["mac"]
        ip = found["ip"] or ip
        if not serial:
            try:
                serial = int(found.get("serial") or 0)
            except (TypeError, ValueError):
                serial = 0
    if not mac_text:
        return {"ok": False, "message": "MAC-Adresse fehlt."}
    for item in settings.get("tracked_inverters") or []:
        if item["mac"] == mac_text:
            return {"ok": True, "already": True, "message": "Dieser Wechselrichter ist schon gespeichert.", "inverter": item}
    try:
        serial_num = int(serial or 0)
    except (TypeError, ValueError):
        serial_num = 0
    if not ip:
        for item in _last_discovery:
            if _format_mac(item.get("mac")) == mac_text:
                ip = _host(item.get("ip"))
                if not serial_num:
                    serial_num = int(item.get("serial") or 0)
    if (not serial_num or not ip) and not _host(host):
        found = discover_logger(ip, mac=mac_text)
        if found:
            ip = ip or found["ip"]
            serial_num = serial_num or int(found.get("serial") or 0)
    if not serial_num:
        return {
            "ok": False,
            "need_serial": True,
            "message": "Seriennummer vom Scan fehlt. Bitte die Logger-Seriennummer eingeben.",
        }
    label = str(name or "").strip() or f"Wechselrichter {ip or mac_text}"
    control = bool(settings.get("logger_mac")) and mac_text == _format_mac(settings.get("logger_mac"))
    if not settings.get("logger_mac") and not any(item.get("control") for item in settings.get("tracked_inverters") or []):
        control = mac_text == DEFAULT_LOGGER_MAC or not settings.get("tracked_inverters")
    flow_field_id = ""
    if not control:
        flow_field_id = "wr" + mac_text.replace(":", "")
        if not _attach_flow_field(label, flow_field_id):
            return {
                "ok": False,
                "message": "Kein freier Platz im Stromfluss. Bitte zuerst ein Feld entfernen.",
            }
    entry = {
        "id": "control" if control else flow_field_id,
        "name": "Steuer-Logger" if control else label,
        "mac": mac_text,
        "serial": serial_num,
        "ip": ip,
        "port": 8899,
        "unit": 1,
        "role": "erzeugung",
        "control": control,
        "flow_field_id": flow_field_id,
        "tracked_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "pv_kw": None,
    }
    settings["tracked_inverters"] = list(settings.get("tracked_inverters") or []) + [entry]
    if control:
        settings["logger_mac"] = mac_text
        settings["logger_serial"] = serial_num
        settings["logger_ip"] = ip or settings.get("logger_ip")
        settings["logger_tracked_at"] = entry["tracked_at"]
        settings["control_tracking_removed"] = False
        if ip:
            settings["modbus_host"] = ip
    _persist_settings(settings)
    message = (
        "Steuer-Logger gespeichert. Die Erzeugung bleibt am bestehenden PV-Punkt, BMS-Schreiben ebenfalls."
        if control
        else f"{label} hinzugefügt. MAC wird stündlich weiterverfolgt, Rolle Erzeugung."
    )
    if control and _should_autocapture(previous_serial, serial_num, settings):
        read = capture_install_from_inverter()
        note = str(read.get("message") or "").strip()
        if note:
            message = f"{message} {note}"
    return {"ok": True, "inverter": entry, "message": message}


def delete_tracked_inverter(mac: str, confirm_control: bool = False) -> Dict[str, Any]:
    """Tracking beenden. Der Steuer-Logger braucht eine ausdrückliche Bestätigung."""
    settings = load_settings()
    mac_text = _format_mac(mac)
    items = list(settings.get("tracked_inverters") or [])
    target = next((item for item in items if item["mac"] == mac_text), None)
    if not target:
        return {"ok": False, "message": "Wechselrichter nicht in der Liste."}
    if target.get("control") and not confirm_control:
        return {
            "ok": False,
            "needs_confirm": True,
            "message": (
                "Das ist der Steuer-Logger fürs BMS-Schreiben. "
                "Löschen stoppt nur das MAC-Tracking. Die letzte IP bleibt fürs Schreiben erhalten."
            ),
        }
    settings["tracked_inverters"] = [item for item in items if item["mac"] != mac_text]
    if target.get("flow_field_id"):
        _detach_flow_field(str(target["flow_field_id"]))
    if target.get("control"):
        settings["control_tracking_removed"] = True
    _persist_settings(settings)
    return {"ok": True, "message": f"{target.get('name') or mac_text} entfernt. Tracking gestoppt."}


def _signed16(raw: int) -> int:
    value = int(raw) & 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _empty_logger_powers() -> Dict[str, Optional[float]]:
    return {"soc_percent": None, "battery_kw": None, "grid_kw": None, "pv_kw": None}


# Anzeige-Felder. Nur die zugeordneten Home-Assistant-Sensoren, nie Modbus.
_HA_DISPLAY_FIELDS = (
    ("soc_percent", "battery_soc"),
    ("battery_kw", "battery_power"),
    ("grid_kw", "kelag"),
    ("pv_kw", "pv"),
)


def read_control_logger_powers() -> Dict[str, Optional[float]]:
    """Ladestand, Batterie, Netz und PV ausschließlich aus Home Assistant.

    Die Zuordnung steht in ha_entity_mapping (Felder battery_soc, battery_power,
    kelag, pv) inklusive Vorzeichen. Modbus-Register werden dafür nicht gelesen.
    """
    try:
        from ha_entity_mapping import (
            FIELD_DEFINITIONS,
            compute_field_value,
            load_config,
            normalize_field_entry,
        )
        from energy_monitor.ha_live_reader import load_snapshot

        cfg = load_config()
        snapshot = load_snapshot()
        fields = cfg.get("fields") or {}
    except Exception:
        return _empty_logger_powers()
    out = _empty_logger_powers()
    for key, field_name in _HA_DISPLAY_FIELDS:
        entry = normalize_field_entry(fields.get(field_name) or {})
        if not entry.get("enabled") or not entry.get("sensors"):
            continue
        try:
            out[key] = compute_field_value(entry, FIELD_DEFINITIONS[field_name], snapshot)
        except Exception:
            out[key] = None
    return out


def extra_inverter_flow_values() -> List[Dict[str, Any]]:
    """Keine Modbus-PV mehr im Stromfluss.

    Ein weiterer Wechselrichter erscheint nur, wenn sein HA-Sensor am
    zugehörigen Stromfluss-Feld hängt. Solarman-Register werden hier nicht gelesen.
    """
    return []


def refresh_tracked_inverters() -> Dict[str, Any]:
    """Jede gespeicherte MAC neu suchen und die IP nachziehen. Keine neuen HA-Einträge."""
    settings = load_settings()
    found = {item["mac"]: item for item in discover_loggers(timeout=2.0)}
    changed_control = False
    now = datetime.now(TZ).isoformat(timespec="seconds")
    for item in settings.get("tracked_inverters") or []:
        match = found.get(item["mac"])
        if not match:
            match = discover_logger(item.get("ip") or "", mac=item["mac"], timeout=1.0)
        if match:
            if match["ip"] and match["ip"] != item.get("ip"):
                item["ip"] = match["ip"]
                if item.get("control"):
                    changed_control = True
            if int(match.get("serial") or 0):
                item["serial"] = int(match["serial"])
        item["tracked_at"] = now
        if item.get("control"):
            settings["logger_ip"] = item.get("ip") or settings.get("logger_ip")
            settings["logger_mac"] = item["mac"]
            settings["logger_serial"] = int(item.get("serial") or settings.get("logger_serial") or 0)
            settings["logger_tracked_at"] = now
            if item.get("ip"):
                settings["modbus_host"] = item["ip"]
    ha = {"ok": True, "message": "Kein neuer Home-Assistant-Eintrag."}
    if changed_control:
        control = next((item for item in settings["tracked_inverters"] if item.get("control")), None)
        if control:
            ha = sync_logger_to_ha(control["ip"], control["mac"], int(control.get("serial") or 0))
    _persist_settings(settings)
    return {"ok": True, "changed_control": changed_control, "ha": ha, "count": len(settings.get("tracked_inverters") or [])}


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
    path = _ha_config_entries_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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
    path = _ha_device_registry_path()
    if not host or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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
        chosen["microinverter_register"] = REG_MICROINVERTER
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
        **{key: data[key] for key in INSTALL_DEFAULTS},
        "modbus_host": data["modbus_host"],
        "modbus_port": data["modbus_port"],
        "modbus_unit": data["modbus_unit"],
        "modbus_source": data.get("modbus_source") or "",
        "known_entry_ids": data.get("known_entry_ids") or [],
        "logger_ip": data.get("logger_ip") or "",
        "logger_mac": data.get("logger_mac") or "",
        "logger_serial": int(data.get("logger_serial") or 0),
        "logger_tracked_at": data.get("logger_tracked_at") or "",
        "tracked_inverters": data.get("tracked_inverters") or [],
        "control_tracking_removed": bool(data.get("control_tracking_removed")),
        "release_pending": bool(data.get("release_pending")),
        "planning_hold": bool(data.get("planning_hold")),
        "community_hold": bool(data.get("community_hold")),
        "exchange_hold": bool(data.get("exchange_hold")),
        "install_captured": bool(data.get("install_captured")),
        "install_captured_serial": int(data.get("install_captured_serial") or 0),
        "program_soc": list(data.get("program_soc") or []),
        "program_power": list(data.get("program_power") or []),
        "program_charging": list(data.get("program_charging") or []),
        "program_time": list(data.get("program_time") or []),
        "soc_stop": bool(data.get("soc_stop")),
    }
    SETTINGS_FILE.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")


def _apply_ha_link(data: Dict[str, Any]) -> Dict[str, Any]:
    link = resolve_ha_link(str(data.get("inverter") or DEFAULT_INVERTER), data.get("known_entry_ids") or [])
    tracked_ip = _host(data.get("logger_ip"))
    if not link:
        data["modbus_source"] = data.get("modbus_source") or ""
        data["known_entry_ids"] = data.get("known_entry_ids") or []
        if tracked_ip:
            data["modbus_host"] = tracked_ip
        return data
    # MAC-Tracker hat Vorrang vor der HA-Host-Adresse (IP kann sich geändert haben)
    host = tracked_ip or link["host"]
    changed = (
        data.get("modbus_host") != host
        or data.get("modbus_port") != link["port"]
        or data.get("modbus_unit") != link["unit"]
        or data.get("modbus_source") != link["source"]
        or list(data.get("known_entry_ids") or []) != link["known_entry_ids"]
    )
    data["modbus_host"] = host
    data["modbus_port"] = link["port"]
    data["modbus_unit"] = link["unit"]
    data["modbus_source"] = link["source"]
    data["known_entry_ids"] = link["known_entry_ids"]
    if changed:
        _persist_settings(data)
    return data


def _auto_peak_kw(data: Dict[str, Any]) -> float:
    """Spitzenkappung stellt die Steuerung selbst: beim Zukauf auf den Soll, sonst Standwert."""
    if float(data.get("target_grid_kw") or 0) > 0:
        return round(float(data["target_grid_kw"]), 2)
    return round(_idle_peak_w(data) / 1000.0, 2)


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
    for key in INSTALL_DEFAULTS:
        data[key] = _install_value(data, key)
    data["peak_cap_kw"] = _auto_peak_kw(data)
    data["modbus_host"] = _host(data.get("modbus_host"))
    data["modbus_port"] = _port(data.get("modbus_port"))
    data["modbus_unit"] = _unit(data.get("modbus_unit"))
    if not isinstance(data.get("known_entry_ids"), list):
        data["known_entry_ids"] = []
    data["modbus_source"] = str(data.get("modbus_source") or "")
    data["logger_ip"] = _host(data.get("logger_ip"))
    data["logger_mac"] = _format_mac(data.get("logger_mac"))
    try:
        data["logger_serial"] = int(data.get("logger_serial") or 0)
    except (TypeError, ValueError):
        data["logger_serial"] = 0
    data["logger_tracked_at"] = str(data.get("logger_tracked_at") or "")
    data["control_tracking_removed"] = bool(data.get("control_tracking_removed"))
    data["tracked_inverters"] = _normalize_tracked_inverters(data.get("tracked_inverters"), data)
    data["release_pending"] = bool(data.get("release_pending"))
    data["planning_hold"] = bool(data.get("planning_hold"))
    data["community_hold"] = bool(data.get("community_hold"))
    data["exchange_hold"] = bool(data.get("exchange_hold"))
    data["install_captured"] = bool(data.get("install_captured"))
    try:
        data["install_captured_serial"] = int(data.get("install_captured_serial") or 0)
    except (TypeError, ValueError):
        data["install_captured_serial"] = 0
    data["soc_stop"] = bool(data.get("soc_stop"))
    for key, highest in (
        ("program_soc", 100),
        ("program_power", 65535),
        ("program_charging", 65535),
        ("program_time", 65535),
    ):
        coerced = _coerce_program_list(data.get(key), highest=highest)
        data[key] = coerced if coerced is not None else []
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
        if wanted and not data["real_mode"] and data.get("soc_stop"):
            # Hand einschalten hebt den Stopp nur über der gerade geltenden Elektrikergrenze auf.
            soc = _live_soc(None)
            floor = _electrician_soc_limit(data)
            if floor is not None and soc is not None and soc > float(floor):
                data["soc_stop"] = False
        data["real_mode"] = wanted
    if "soc_stop" in updates:
        data["soc_stop"] = bool(updates["soc_stop"])
    if "install_captured" in updates:
        data["install_captured"] = bool(updates["install_captured"])
    if "install_captured_serial" in updates:
        try:
            data["install_captured_serial"] = int(updates["install_captured_serial"] or 0)
        except (TypeError, ValueError):
            data["install_captured_serial"] = 0
    for key, highest in (
        ("program_soc", 100),
        ("program_power", 65535),
        ("program_charging", 65535),
        ("program_time", 65535),
    ):
        if key in updates:
            coerced = _coerce_program_list(updates.get(key), highest=highest)
            data[key] = coerced if coerced is not None else []
    if "target_grid_kw" in updates:
        data["target_grid_kw"] = _kw(updates["target_grid_kw"], data["target_grid_kw"], lowest=-100.0)
    if "planning_hold" in updates:
        data["planning_hold"] = bool(updates["planning_hold"])
    if "community_hold" in updates:
        data["community_hold"] = bool(updates["community_hold"])
    if "exchange_hold" in updates:
        data["exchange_hold"] = bool(updates["exchange_hold"])
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
    for key in INSTALL_DEFAULTS:
        if key in updates:
            data[key] = _install_value(updates, key)
    data["peak_cap_kw"] = _auto_peak_kw(data)
    if "modbus_host" in updates and not data.get("modbus_host"):
        data["modbus_host"] = _host(updates["modbus_host"])
    if "modbus_port" in updates and not data.get("modbus_port"):
        data["modbus_port"] = _port(updates["modbus_port"])
    if "modbus_unit" in updates and not data.get("modbus_unit"):
        data["modbus_unit"] = _unit(updates["modbus_unit"])
    data = _apply_ha_link(data)
    _persist_settings(data)
    # Nach dem Verknüpfen Logger-MAC/IP erfassen und in Home Assistant nachziehen.
    if data.get("modbus_host") and (
        "inverter" in updates or "modbus_host" in updates or not data.get("logger_mac")
    ):
        try:
            tracked = track_logger(str(data.get("modbus_host") or ""), persist=True)
            if tracked.get("ok"):
                data = load_settings()
        except Exception:
            pass
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


def _read_live_now() -> Dict[str, Any]:
    """Ladestand, Batterie, PV und Netz nur aus den Home-Assistant-Sensoren.

    Die Hauslast ist „Verbrauch gesamt“: alle Stromfluss-Punkte mit Rolle Verbraucher.
    Modbus wird dafür nicht gelesen.
    """
    powers = read_control_logger_powers()
    return {
        "soc_percent": powers.get("soc_percent"),
        "battery_kw": powers.get("battery_kw"),
        "grid_kw": powers.get("grid_kw"),
        "pv_kw": powers.get("pv_kw"),
        "load_kw": _configured_house_kw(),
        "source": "Home Assistant",
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


# Bytes 7–10 jedes Rahmens von 192.168.1.176, little-endian. Die
# Home-Assistant-Konfiguration trägt eine andere Nummer, und auf die antwortet
# dieser Logger nicht. Nur diese Nummer kommt in den Schreib- und Leserahmen.
LOGGER_FRAME_SERIAL = 2965822000


def _solarman_exchange(host: str, port: int, serial: int, modbus: bytes, accept) -> bytes:
    """Ein Modbus-Rahmen über den Solarman-Logger (Port 8899).

    Der Logger schickt die Antworten an alle offenen Verbindungen, also auch die
    Antworten auf die Abfragen von Home Assistant. Gezählt wird nur ein Rahmen mit
    unserer Folgenummer, für den accept() zustimmt. Sonst ginge ein verlorener
    Schreibbefehl als bestätigt durch.
    """
    if not serial:
        raise OSError("Solarman-Seriennummer fehlt")
    if str(host) == "192.168.1.176":
        serial = LOGGER_FRAME_SERIAL
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


def readback_diff(address: int, name: str, wanted: int, actual: int) -> Optional[str]:
    """Soll gegen Rücklesen. None, wenn der Wechselrichter den Wert übernommen hat."""
    if _u16(actual) == _u16(wanted):
        return None
    return _phrase(address, name, wanted) + f", gelesen {_u16(actual)}"


def read_solarman_holding(host: str, port: int, unit: int, serial: int, address: int) -> int:
    """Ein Holding-Register über den Solarman-Logger lesen (Funktionscode 3)."""
    modbus = struct.pack(">BBHH", int(unit) & 0xFF, 3, int(address) & 0xFFFF, 1)
    modbus += _modbus_crc(modbus)
    answer = _solarman_exchange(
        host, port, serial, modbus,
        lambda reply: len(reply) >= 5 and reply[0] == modbus[0] and reply[1] == 3 and reply[2] == 2,
    )
    return (answer[3] << 8) | answer[4]


def read_holding(host: str, port: int, unit: int, serial: int, address: int) -> int:
    """Holding-Register zurücklesen. Port 8899 über Solarman, sonst Modbus-TCP Funktionscode 3."""
    if int(port) == 8899 or serial:
        if not serial:
            raise OSError("Solarman-Seriennummer fehlt")
        return read_solarman_holding(host, port, unit, serial, address)
    pdu = struct.pack(">BHH", 3, int(address) & 0xFFFF, 1)
    message = struct.pack(">HHHB", 1, 0, len(pdu) + 1, int(unit) & 0xFF) + pdu
    with socket.create_connection((host, int(port)), timeout=3.0) as sock:
        sock.settimeout(3.0)
        sock.sendall(message)
        data = sock.recv(32)
    if len(data) < 11 or data[7] != 3 or data[8] != 2:
        raise OSError("Rücklesen ohne Nutzdaten")
    return (data[9] << 8) | data[10]


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
# Home Assistant liefert den Netzzähler nur alle 5–30 s. Öfter nachgeführt, zählt die
# Nachführung denselben alten Wert mehrfach und schwingt über.
PEAK_TRIM_EVERY_SEC = 30
DISCHARGE_GAIN = 0.5
MODE_SWITCH_W = 150
GRID_DEADBAND_W = 250
SELL_POWER_MAX_W = 12000
# Programm-Modus 0x00AC–0x00B1: Bit 0–1 Laden (Netz/Generator), Bit 5 Verkaufen.
# Ohne Bit 5 entlädt Export First die Batterie nur ins Haus, nie ins Netz.
TOU_SELL_BIT = 1 << 5
_commanded_amps = 0
_discharge_amps = MAX_DISCHARGE_AMPS
# "charge": Netz deckt Haus plus Netzladung, Kappung hält den Soll.
# "discharge": Haus über dem Soll, Entladestrom begrenzt, damit trotzdem der Soll vom Netz kommt.
# "feed": Einspeisung aus Batterie und PV.
_hold_mode: Optional[str] = None
_hold_target_w: Optional[int] = None
_peak_trim_w = 0
_peak_trim_at = 0.0


def _hold_deadband_w(soll_kw: float) -> int:
    """Totzone folgt dem Soll. 250 W machen 200 W-Halt unmöglich."""
    target_w = abs(int(round(float(soll_kw) * 1000)))
    return max(60, min(GRID_DEADBAND_W, int(round(target_w * 0.25)) or 60))


def _reset_hold(target_w: Optional[int]) -> None:
    global _commanded_amps, _discharge_amps, _hold_mode, _hold_target_w, _peak_trim_w, _peak_trim_at, _tou_hold_applied, _tou_soc_applied
    _commanded_amps = 0
    _discharge_amps = MAX_DISCHARGE_AMPS
    _hold_mode = None
    _hold_target_w = target_w
    _peak_trim_w = 0
    _peak_trim_at = time.monotonic()
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


def _trim_due() -> bool:
    global _peak_trim_at
    now = time.monotonic()
    if now - _peak_trim_at < PEAK_TRIM_EVERY_SEC:
        return False
    _peak_trim_at = now
    return True


def _track_peak_trim(target_w: int, live: Optional[Dict[str, Any]]) -> None:
    global _peak_trim_w
    grid = _grid_w(live)
    if grid is None:
        return
    error = target_w - grid
    if abs(error) > 400 or abs(error) <= 25 or not _trim_due():
        return
    _peak_trim_w = max(PEAK_TRIM_MIN_W, min(PEAK_TRIM_MAX_W, _peak_trim_w + int(round(error * PEAK_TRIM_GAIN))))


def _discharge_limit_amps(target_w: int, live: Optional[Dict[str, Any]]) -> int:
    """Entladestrom so begrenzen, dass die Hauslast über dem Soll aus der Batterie kommt."""
    global _discharge_amps
    grid = _grid_w(live)
    if grid is None:
        return _discharge_amps
    error = grid - target_w
    if abs(error) > max(30, min(GRID_DEADBAND_W, abs(target_w) // 10)):
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


def _configured_house_kw() -> Optional[float]:
    """Hauslast oben: derselbe Wert wie „Verbrauch gesamt“ im Dashboard.

    Summe aller Stromfluss-Punkte mit Rolle Verbraucher, nicht ein einzelnes Sensorfeld.
    """
    try:
        from miner_flow_data import build_flow_response

        flow = build_flow_response(str(BACKEND_DIR))
        raw = flow.get("verbrauch_gesamt")
        if raw in (None, "", "n/a"):
            return None
        return round(float(str(raw).replace(",", ".").strip()), 3)
    except Exception:
        return None


def _house_load_kw(live: Optional[Dict[str, Any]]) -> Optional[float]:
    """Hauslast ist der Gesamtwert „Verbrauch gesamt“."""
    if not live or live.get("load_kw") is None:
        return None
    return round(float(live["load_kw"]), 3)


def _peak_enable_row(item: Dict[str, Any], peak_flags: Optional[int], enabled: bool = True) -> List[tuple]:
    """0x00B2 ist ein 16-Bit-Feld mit weiteren Schaltern. Nur Bit 4 ändern, nie überschreiben.

    Bei der Einspeisung muss die Spitzenkappung aus: mit gesetztem Bit 4 verkauft der
    SG04LP3 in Export First keine Batterieleistung (Firmware-Fehler, am Gerät bestätigt:
    Bit 4 gelöscht, sofort 1 kW Einspeisung ohne PV).
    """
    register = item.get("grid_peak_enable_register")
    if register is None or peak_flags is None:
        return []
    mask = 1 << GRID_PEAK_ENABLE_BIT
    current = bool(int(peak_flags) & mask)
    if current == enabled:
        return []
    value = int(peak_flags) | mask if enabled else int(peak_flags) & ~mask
    return [(int(register), "Grid Peak shaving enable", value)]


def _release_flag_row(item: Dict[str, Any], peak_flags: Optional[int]) -> List[tuple]:
    """Lösen: Spitzenkappung an (Bit 4), Export-Abschaltung der Mikros aus (Bit 0).

    Nur das gelesene Wort wird verändert. Ohne Rücklesen des Wortes wird nichts geschrieben.
    """
    register = item.get("grid_peak_enable_register")
    if register is None or peak_flags is None:
        return []
    value = int(peak_flags) | (1 << GRID_PEAK_ENABLE_BIT)
    value &= ~(1 << MICRO_EXPORT_CUTOFF_BIT)
    value &= 0xFFFF
    if value == (int(peak_flags) & 0xFFFF):
        return []
    return [(int(register), "System work switch", value)]


def _import_writes(
    item: Dict[str, Any],
    target_w: int,
    live: Optional[Dict[str, Any]],
    peak_flags: Optional[int],
    immediate: bool = False,
) -> tuple:
    """Fester Netzbezug. Liefert (Registerzeilen, TOU-SOC, TOU-Laden).

    immediate: Börsentarif. Der eingegebene Buy-Betrag geht sofort als Netzladung
    in die Batterie, ohne Nachführung. Die Spitzenkappung lässt dafür Haus plus
    diesen Betrag zu und wird später auf das Max. Netzbezug begrenzt.
    """
    global _commanded_amps, _hold_mode, _peak_trim_w
    enable = item.get("grid_charge_register")
    current = item.get("grid_charge_current_register")
    if enable is None or current is None:
        return [], TOU_HOLD_SOC, 1
    if immediate:
        house = _house_load_kw(live)
        house_w = 0 if house is None else max(0, int(round(house * 1000)))
        charge_w = max(0, int(target_w))
        peak_w = charge_w + house_w
        amps = _grid_charge_amps(charge_w)
        _commanded_amps = amps
        _hold_mode = "charge"
        _peak_trim_w = 0
    else:
        mode = _choose_import_mode(target_w, live)
        if mode == "charge":
            _track_peak_trim(target_w, live)
        peak_w = max(0, target_w + _peak_trim_w)
        amps = None
    rows: List[tuple] = []
    if item.get("work_mode_register") is not None:
        rows.append((int(item["work_mode_register"]), "Work Mode", WORK_MODE_ZERO_EXPORT_CT))
    if item.get("energy_pattern_register") is not None:
        rows.append((int(item["energy_pattern_register"]), "Energy Pattern", ENERGY_PATTERN_LOAD_FIRST))
    if item.get("solar_sell_register") is not None:
        rows.append((int(item["solar_sell_register"]), "Solar Sell", SOLAR_SELL_OFF))
    if item.get("max_import_register") is not None:
        # Scale 10, nur lose Obergrenze. Den Soll hält die Spitzenkappung.
        basis = peak_w if immediate else target_w + PEAK_TRIM_MAX_W + 500
        loose = min(800, max(1, (basis + 9) // 10))
        rows.append((int(item["max_import_register"]), "Grid Max Import power", loose))
    if item.get("time_of_use_register") is not None:
        rows.append((int(item["time_of_use_register"]), "Time of Use", TOU_WEEK_ENABLED))
    if item.get("grid_peak_power_register") is not None:
        rows.append((int(item["grid_peak_power_register"]), "Grid Peak shaving", peak_w))
    rows.extend(_peak_enable_row(item, peak_flags))
    if immediate or mode == "charge":
        if not immediate:
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
    """Entladestrom für die Einspeisung: voll offen, die Exportgrenze 0x008F regelt.

    Ein knapper Entladestrom bindet, sobald im Haus ein Verbraucher anläuft, und die
    Einspeisung bricht ein, bis der 6-s-Takt mit den trägen HA-Werten nachgezogen hat.
    """
    global _discharge_amps
    soc = None if not live else live.get("soc_percent")
    if soc is not None and float(soc) <= SOC_EMPTY:
        _discharge_amps = 0
        return 0
    _discharge_amps = MAX_DISCHARGE_AMPS
    return _discharge_amps


def _feed_writes(
    item: Dict[str, Any],
    export_w: int,
    live: Optional[Dict[str, Any]],
    settings: Dict[str, Any],
    peak_flags: Optional[int] = None,
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
    # Export Surplus Power hält am SG04LP3 auf ±30 W genau. Jede Absenkung des Registers
    # lässt den Export für 20 s bis an den Entladestrom hochlaufen (gemessen: 3045 -> 3030 W
    # ergab 5,5 kW Einspeisung). Darum fest auf den Soll, keine Nachführung.
    sell_w = max(0, min(SELL_POWER_MAX_W, export_w))
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
    rows.extend(_peak_enable_row(item, peak_flags, enabled=False))
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
    return rows, TOU_HOLD_SOC, TOU_SELL_BIT


def _tou_program_writes(item: Dict[str, Any], tou_soc: int, charging: int, with_power: bool) -> List[tuple]:
    if item.get("time_of_use_register") is None:
        return []
    value = max(TOU_HOLD_SOC, min(100, int(tou_soc)))
    rows: List[tuple] = []
    for index in range(6):
        if with_power:
            rows.append((0x009A + index, f"Program {index + 1} Power", TOU_HOLD_POWER_W))
        rows.append((0x00A6 + index, f"Program {index + 1} SOC", value))
        rows.append((0x00AC + index, f"Program {index + 1} Mode", int(charging)))
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


def _live_soc(live: Optional[Dict[str, Any]]) -> Optional[float]:
    """Ladestand aus dem übergebenen Live-Wert, sonst aus Home Assistant."""
    if live is None:
        live = read_live()
    if not live:
        return None
    return _parse_soc(live.get("soc_percent"))


def _program_soc_floor(settings: Optional[Dict[str, Any]]) -> Optional[int]:
    """Minimum der sechs gespeicherten Programm-SOCs. Live-Register sind dafür unbrauchbar."""
    socs = _coerce_program_list((settings or {}).get("program_soc"), highest=100)
    if not socs:
        return None
    return min(socs)


def _hhmm_minutes(raw: Any) -> Optional[int]:
    """Deye-Startzeit: Stunde mal 100 plus Minute, zum Beispiel 830 = 08:30."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    hour, minute = divmod(value, 100)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _active_program_index(settings: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[int]:
    """Slot, dessen Startzeit gerade gilt. Zeiten kommen aus dem Snapshot, Uhr aus Europe/Vienna."""
    times = _coerce_program_list((settings or {}).get("program_time"))
    if not times:
        return None
    moment = now or datetime.now(TZ)
    now_m = moment.hour * 60 + moment.minute
    best_index: Optional[int] = None
    best_start: Optional[int] = None
    latest_index: Optional[int] = None
    latest_start: Optional[int] = None
    for index, raw in enumerate(times):
        start = _hhmm_minutes(raw)
        if start is None:
            continue
        if latest_start is None or start > latest_start or (start == latest_start and index > (latest_index or 0)):
            latest_start = start
            latest_index = index
        if start <= now_m and (
            best_start is None or start > best_start or (start == best_start and index > (best_index or 0))
        ):
            best_start = start
            best_index = index
    if best_index is not None:
        return best_index
    return latest_index


def _active_program_soc(settings: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[int]:
    socs = _coerce_program_list((settings or {}).get("program_soc"), highest=100)
    index = _active_program_index(settings, now)
    if socs is None or index is None:
        return None
    return socs[index]


def _electrician_soc_limit(
    settings: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Optional[int]:
    """SoC-Grenze des Elektriker-Fensters, das gerade gilt. Nicht das Minimum aller sechs."""
    return _active_program_soc(settings, now)


def _soc_floor_hit(
    settings: Optional[Dict[str, Any]],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Schreiben stoppen: kein Snapshot, kein Ladestand, oder Ladestand auf/unter der Elektrikergrenze."""
    floor = _electrician_soc_limit(settings, now)
    soc = _live_soc(live)
    if floor is None or soc is None:
        return True
    return soc <= float(floor)


def _planning_enabled() -> bool:
    try:
        from energie_steuerung import load_settings as energy_settings

        return bool(energy_settings().get("algorithmische_planung"))
    except Exception:
        return False


def _resume_ready(
    settings: Optional[Dict[str, Any]],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Wieder an, sobald der Ladestand über der geltenden Elektrikergrenze liegt und die Planung an ist.

    Der Stopp bleibt, solange der SOC auf oder unter dem Fenster liegt, das der Elektriker
    für diese Uhrzeit gesetzt hat, oder der Schalter „Planung, keine Netzspitzen“ aus ist.
    """
    if not settings or not settings.get("soc_stop"):
        return False
    if not _planning_enabled():
        return False
    floor = _electrician_soc_limit(settings, now)
    soc = _live_soc(live)
    if floor is None or soc is None:
        return False
    return soc > float(floor)


def _should_autocapture(previous_serial: int, new_serial: int, settings: Optional[Dict[str, Any]]) -> bool:
    """Einmal lesen, wenn der Steuer-Logger neu gebunden wird. Nicht bei bekanntem, schon gepaartem Gerät."""
    try:
        previous = int(previous_serial or 0)
        new = int(new_serial or 0)
    except (TypeError, ValueError):
        return False
    if not new:
        return False
    data = settings or {}
    if data.get("install_captured") and int(data.get("install_captured_serial") or 0) == new:
        return False
    if previous == new and previous != 0 and not data.get("install_captured"):
        return False
    return previous != new


def _writer_serial(settings: Dict[str, Any]) -> int:
    link = resolve_ha_link(str(settings.get("inverter") or ""), settings.get("known_entry_ids") or [])
    serial = int(link.get("serial") or 0) if link else 0
    if serial:
        return serial
    try:
        return int(settings.get("logger_serial") or 0)
    except (TypeError, ValueError):
        return 0


def _read_holding_retry(settings: Dict[str, Any], serial: int, address: int) -> int:
    host = settings.get("modbus_host") or ""
    port = int(settings.get("modbus_port") or 0)
    unit = int(settings.get("modbus_unit") or 1)
    last: Optional[BaseException] = None
    for _attempt in range(3):
        try:
            return read_holding(host, port, unit, serial, address)
        except OSError as exc:
            last = exc
            time.sleep(0.3)
    raise OSError(str(last or "Lesen fehlgeschlagen"))


def _program_restore_rows(settings: Optional[Dict[str, Any]]) -> List[tuple]:
    """Gespeicherte Programmziele. Ohne Snapshot nichts schreiben, insbesondere nicht SOC 20."""
    if not settings:
        return []
    socs = _coerce_program_list(settings.get("program_soc"), highest=100)
    powers = _coerce_program_list(settings.get("program_power"))
    charges = _coerce_program_list(settings.get("program_charging"))
    if not socs or not powers or not charges:
        return []
    rows: List[tuple] = []
    for index in range(PROGRAM_SLOTS):
        rows.append((REG_PROGRAM_POWER + index, f"Program {index + 1} Power", powers[index]))
        rows.append((REG_PROGRAM_SOC + index, f"Program {index + 1} SOC", socs[index]))
        rows.append((REG_PROGRAM_CHARGE + index, f"Program {index + 1} Charging", charges[index]))
    return rows


def capture_install_from_inverter() -> Dict[str, Any]:
    """Elektrikerwerte und die sechs Programme einmal vom Wechselrichter lesen.

    Nur wenn BMS nicht schreibt. Sonst stünden in den Registern die Stellwerte der Schleife.
    """
    settings = load_settings()
    if settings.get("real_mode") or settings.get("release_pending"):
        return {
            "ok": False,
            "message": (
                "BMS schreibt noch oder die Elektrikerwerte werden gerade zurückgeschrieben. "
                "Bitte warten, bis das aus ist."
            ),
        }
    host = settings.get("modbus_host") or ""
    serial = _writer_serial(settings)
    if not host or not serial:
        return {
            "ok": False,
            "message": "Kein Steuer-Logger mit Seriennummer. Zuerst den Wechselrichter hinzufügen.",
        }
    try:
        updates: Dict[str, Any] = {}
        for key, (address, scale) in INSTALL_REGISTER_SCALE.items():
            raw = _read_holding_retry(settings, serial, address)
            updates[key] = int(raw) * int(scale)
        socs: List[int] = []
        powers: List[int] = []
        charges: List[int] = []
        times: List[int] = []
        for index in range(PROGRAM_SLOTS):
            socs.append(_read_holding_retry(settings, serial, REG_PROGRAM_SOC + index))
            powers.append(_read_holding_retry(settings, serial, REG_PROGRAM_POWER + index))
            charges.append(_read_holding_retry(settings, serial, REG_PROGRAM_CHARGE + index))
            times.append(_read_holding_retry(settings, serial, REG_PROGRAM_TIME + index))
        updates["program_soc"] = socs
        updates["program_power"] = powers
        updates["program_charging"] = charges
        updates["program_time"] = times
        updates["install_captured"] = True
        updates["install_captured_serial"] = serial
    except OSError as exc:
        return {"ok": False, "message": f"Elektrikerwerte konnten nicht gelesen werden: {exc}"}
    saved = save_settings(updates)
    return {"ok": True, "message": "Elektrikerwerte vom Wechselrichter gelesen.", "settings": saved}


def _stopped_for_soc(
    settings: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Settings, wenn der Hold nicht schreiben darf. BMS aus, solange die Elektrikergrenze gilt."""
    hit = _soc_floor_hit(settings, live, now)
    if not hit and not settings.get("soc_stop"):
        return None
    if settings.get("real_mode") or (hit and not settings.get("soc_stop")):
        return save_settings({"real_mode": False, "soc_stop": True})
    return settings


def apply_soc_gate(
    settings: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Vor den Holds: Stopp merken oder den automatischen Wiederanlauf freigeben."""
    if settings.get("soc_stop"):
        if settings.get("release_pending"):
            if settings.get("real_mode"):
                return save_settings({"real_mode": False, "soc_stop": True})
            return settings
        if _resume_ready(settings, live, now):
            return save_settings({"soc_stop": False})
        if settings.get("real_mode"):
            return save_settings({"real_mode": False, "soc_stop": True})
        return settings
    if settings.get("real_mode") and _soc_floor_hit(settings, live, now):
        return save_settings({"real_mode": False, "soc_stop": True})
    return settings


def _release_writes(item: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> List[tuple]:
    """Haltung einmal lösen: Elektrikerwerte, und die Programmziele nur aus dem Snapshot."""
    if item.get("register") is None:
        return []
    idle_w = _idle_grid_w(settings)
    work_mode = _idle_work_mode(settings)
    pattern = _idle_energy_pattern(settings)
    peak_w = _idle_peak_w(settings)
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
        rows.append((REG_MAX_CHARGE, "Battery Max Charging Current", _install_value(settings, "install_max_charge_a")))
        rows.append(
            (
                int(item["max_discharge_register"]),
                "Battery Max Discharging Current",
                _install_value(settings, "install_max_discharge_a"),
            )
        )
        rows.append((REG_SHUTDOWN_SOC, "Battery Shutdown SOC", _install_value(settings, "install_shutdown_soc")))
        rows.append((REG_RESTART_SOC, "Battery Restart SOC", _install_value(settings, "install_restart_soc")))
        rows.append((REG_LOW_SOC, "Battery Low SOC", _install_value(settings, "install_low_soc")))
    if item.get("grid_peak_power_register") is not None:
        rows.append((int(item["grid_peak_power_register"]), "Grid Peak shaving", peak_w))
    if item.get("max_import_register") is not None:
        rows.append(
            (
                int(item["max_import_register"]),
                "Grid Max Import power",
                _install_value(settings, "install_max_import_w") // 10,
            )
        )
    if item.get("solar_sell_register") is not None:
        rows.append((int(item["solar_sell_register"]), "Solar Sell", SOLAR_SELL_OFF))
    if item.get("microinverter_register") is not None:
        rows.append((int(item["microinverter_register"]), "Microinverter", 0))
    if item.get("sell_power_register") is not None:
        rows.append(
            (int(item["sell_power_register"]), "Export Surplus Power", _install_value(settings, "install_max_sell_w"))
        )
    schedule = item.get("time_of_use_register")
    if schedule is not None:
        rows.append((int(schedule), "Time of Use", TOU_WEEK_ENABLED))
        rows.extend(_program_restore_rows(settings))
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


def _read_peak_flags(item: Dict[str, Any], settings: Dict[str, Any], serial: int) -> Optional[int]:
    register = item.get("grid_peak_enable_register")
    if register is None or not serial:
        return None
    for _attempt in range(3):
        try:
            return read_solarman_holding(
                settings["modbus_host"], settings["modbus_port"], settings["modbus_unit"], serial, int(register)
            )
        except OSError:
            time.sleep(0.3)
    return None


# Vom Wechselrichter zurückgelesene Haltewerte. Der SG04LP3 bestätigt Schreibbefehle auch,
# wenn er sie bei vielen Befehlen hintereinander verwirft (gesehen bei den TOU-Programmen).
# Darum wird jeder Wert zurückgelesen und nur Geändertes neu geschrieben.
CONFIRM_REFRESH_SEC = 60
_confirmed: Dict[int, int] = {}
_confirmed_at = 0.0


def _forget_confirmed() -> None:
    global _confirmed_at
    _confirmed.clear()
    _confirmed_at = 0.0


def _send_verified(settings: Dict[str, Any], serial: int, writes: List[tuple], raw: Optional[int]) -> Dict[str, Any]:
    global _confirmed_at
    now = time.monotonic()
    if now - _confirmed_at > CONFIRM_REFRESH_SEC:
        _confirmed.clear()
        _confirmed_at = now
    host, port, unit = settings["modbus_host"], settings["modbus_port"], settings["modbus_unit"]
    writes = _cap_rows(writes, settings)
    pending = [row for row in writes if _confirmed.get(int(row[0])) != _u16(row[2])]
    if not pending:
        return _write_result(True, "Alle Haltewerte bestätigt, nichts neu geschrieben.", raw)
    done: List[str] = []
    missed: List[str] = []
    for address, name, value in pending:
        wanted = _u16(value)
        diff = ""
        ok = False
        for _attempt in range(2):
            try:
                _send_holding(host, port, unit, serial, address, value)
                actual = read_holding(host, port, unit, serial, address)
                diff = readback_diff(address, name, wanted, actual) or ""
                ok = not diff
            except OSError:
                ok = False
                diff = _phrase(address, name, value) + ", Rücklesen fehlgeschlagen"
            if ok:
                break
        if ok:
            _confirmed[int(address)] = wanted
            done.append(_phrase(address, name, value))
        else:
            _confirmed.pop(int(address), None)
            missed.append(diff or _phrase(address, name, value))
    text = "Geschrieben und zurückgelesen: " + ("; ".join(done) if done else "nichts") + "."
    if missed:
        text += " Abweichung nach Rücklesen: " + "; ".join(missed) + "."
        return _write_result(False, text, raw)
    return _write_result(True, text, raw)


def apply_write(
    settings: Dict[str, Any],
    plan: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    global _needs_release, _tou_hold_applied, _tou_soc_applied, _release_sent
    if settings.get("real_mode") and _soc_floor_hit(settings, live):
        settings = save_settings({"real_mode": False, "soc_stop": True})
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
        peak_flags = _read_peak_flags(item, settings, serial)
        writes = writes + _release_flag_row(item, peak_flags)
        flags_unread = item.get("grid_peak_enable_register") is not None and peak_flags is None
        with _lock:
            if _release_sent or not _release_due(settings):
                return _write_result(False, "Aus. Es wird nichts geschrieben.")
            _forget_confirmed()
            result = _send_verified(settings, serial, writes, _idle_grid_w(settings))
            _reset_hold(None)
            _forget_confirmed()
            _last_write["at"] = time.monotonic()
            _last_write["signature"] = ("release",)
            _last_write["result"] = result
            verified = bool(result.get("command_sent")) and "Abweichung" not in (result.get("write_text") or "")
            if flags_unread:
                result = _write_result(
                    False,
                    (result.get("write_text") or "")
                    + " Abweichung nach Rücklesen: System work switch nicht gelesen, Export-Abschaltung ungeprüft.",
                    result.get("raw"),
                )
                verified = False
            if verified:
                ha_errors = _sync_ha_release(settings)
                if ha_errors:
                    result = _write_result(
                        False,
                        (result.get("write_text") or "") + " Abweichung nach Rücklesen: " + "; ".join(ha_errors) + ".",
                        result.get("raw"),
                    )
                    verified = False
            if verified:
                _release_sent = True
                _needs_release = False
                _clear_release_pending(settings)
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
    peak_flags = _read_peak_flags(item, settings, serial)
    if target_w < 0:
        rows, tou_soc, tou_charging = _feed_writes(item, -target_w, live, settings, peak_flags)
    else:
        rows, tou_soc, tou_charging = _import_writes(
            item, target_w, live, peak_flags, immediate=bool(settings.get("exchange_hold"))
        )
    writes.extend(rows)
    tou_state = (tou_soc, tou_charging)
    writes.extend(_tou_program_writes(item, tou_soc, tou_charging, with_power=True))
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
        result = _send_verified(settings, serial, writes, raw)
        _last_write["at"] = now
        _last_write["signature"] = signature
        _last_write["result"] = result
        _tou_hold_applied = True
        _tou_soc_applied = tou_state
        return result


def _ha_matches(entity: str, expected: str, current: str) -> bool:
    if expected == "off":
        return current in ("off", "0")
    if current == expected:
        return True
    try:
        return float(current) == float(expected)
    except (TypeError, ValueError):
        return False


def _sync_ha_release(settings: Optional[Dict[str, Any]] = None) -> List[str]:
    """Nach dem Ausschalten HA auf die Standeinstellung setzen und zurücklesen.

    Solar Sell, Mikrowechselrichter und die Export-Abschaltung müssen aus sein.
    Weicht der gelesene Zustand ab, kommt die Stelle in der Fehlerliste zurück.
    """
    try:
        from ha_device_control import ha_service, read_ha_entity_state
    except Exception as exc:
        return [f"Home Assistant nicht erreichbar: {exc}"]
    idle_w = _idle_grid_w(settings)
    work_name = "Export First" if _idle_work_mode(settings) == WORK_MODE_EXPORT_FIRST else "Zero Export To CT"
    pattern_name = "Battery First" if _idle_energy_pattern(settings) == ENERGY_PATTERN_BATTERY_FIRST else "Load First"
    prefix = "number.192_168_1_176"
    select = "select.192_168_1_176"
    wanted = [
        ("number", "set_value", f"{prefix}_zero_export_power", {"value": idle_w}),
        ("number", "set_value", f"{prefix}_grid_peak_shaving", {"value": _idle_peak_w(settings)}),
        ("number", "set_value", f"{prefix}_battery_grid_charging_current", {"value": 0}),
        ("select", "select_option", f"{select}_work_mode", {"option": work_name}),
        ("select", "select_option", f"{select}_energy_pattern", {"option": pattern_name}),
        ("switch", "turn_off", "switch.192_168_1_176_battery_grid_charging", {}),
        ("switch", "turn_off", "switch.192_168_1_176_export_surplus", {}),
        ("switch", "turn_off", "switch.192_168_1_176_microinverter", {}),
        ("switch", "turn_off", "switch.192_168_1_176_microinverter_export_cut_off", {}),
    ]
    socs = _coerce_program_list((settings or {}).get("program_soc"), highest=100)
    powers = _coerce_program_list((settings or {}).get("program_power"))
    charges = _coerce_program_list((settings or {}).get("program_charging"))
    if socs and powers and charges:
        charge_names = {0: "Disabled", 1: "Grid", 2: "Generator", 3: "Both"}
        for index in range(PROGRAM_SLOTS):
            slot = index + 1
            charge_name = charge_names.get(int(charges[index]) & 0x3, "Grid")
            wanted.append(("number", "set_value", f"{prefix}_program_{slot}_soc", {"value": socs[index]}))
            wanted.append(("number", "set_value", f"{prefix}_program_{slot}_power", {"value": powers[index]}))
            wanted.append(("select", "select_option", f"{select}_program_{slot}_charging", {"option": charge_name}))
    errors: List[str] = []
    for domain, service, entity, extra in wanted:
        current = (read_ha_entity_state(entity) or "").strip()
        target = "off" if service == "turn_off" else str(
            extra.get("value") if extra.get("value") is not None else extra.get("option") or ""
        )
        if target and _ha_matches(entity, target, current):
            continue
        try:
            data = {"entity_id": entity}
            data.update(extra)
            ha_service(domain, service, data)
        except Exception as exc:
            errors.append(f"{entity}: Schreiben fehlgeschlagen ({exc})")
            continue
        seen = (read_ha_entity_state(entity) or "").strip()
        if not _ha_matches(entity, target, seen):
            errors.append(f"{entity}: Soll {target}, gelesen {seen or 'unbekannt'}")
    return errors


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


# Unter diesem Zukauf schreibt die Planung nichts ins Register. 0,02 kW wäre nur Rauschen.
PLANNING_MIN_KW = 0.2


def planning_write_kw(raw: Optional[float]) -> Optional[float]:
    """Zukauf, der ins Register darf. Darunter None, es wird nichts geschrieben."""
    try:
        kw = round(float(raw), 2)
    except (TypeError, ValueError):
        return None
    if kw < PLANNING_MIN_KW:
        return None
    return kw


def apply_planning_hold(
    settings: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Planung, keine Netzspitzen: angezeigter Zukauf ab 0,2 kW ins BMS-Register.

    Nur bei eingeschaltetem Schalter. Aus schreibt keinen Zukauf. Hielt die Planung
    vorher einen Wert, wird er freigegeben. Der Rest der letzten Tage (0,02 kW) zählt nicht.
    Nach einem SOC-Stopp bleibt der Schalter an; geschrieben wird erst wieder, wenn
    der Ladestand über der SoC-Grenze des gerade geltenden Elektriker-Fensters liegt.
    """
    if settings is None:
        settings = load_settings()
    if settings.get("community_hold") or settings.get("exchange_hold"):
        return settings
    try:
        from energie_steuerung import load_settings as energy_settings

        planning_on = bool(energy_settings().get("algorithmische_planung"))
    except Exception:
        return settings
    holding = bool(settings.get("planning_hold"))
    if not planning_on:
        if holding:
            return save_settings({"real_mode": False, "planning_hold": False})
        return settings
    try:
        from grundverbrauch_rechner import recommended_import_kw

        kw = planning_write_kw(recommended_import_kw(str(BACKEND_DIR)))
    except Exception:
        kw = None
    if kw is None:
        if holding:
            return save_settings({"real_mode": False, "planning_hold": False})
        return settings
    stopped = _stopped_for_soc(settings, live, now)
    if stopped is not None:
        return stopped
    if (
        holding
        and settings.get("real_mode")
        and abs(float(settings.get("target_grid_kw") or 0) - kw) < 0.01
    ):
        return settings
    return save_settings({"real_mode": True, "target_grid_kw": kw, "planning_hold": True})


def apply_community_hold(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Energiegemeinschaft ohne PV: frische Einspeisung der Mitglieder als Zukauf.

    Alle Mitglieder der Gemeinschaft werden addiert. Unter 0,2 kW wird
    nichts geschrieben. Mit PV-Anlage ist der Schalter aus, dann passiert hier nichts.
    """
    if settings is None:
        settings = load_settings()
    try:
        from energie_steuerung import load_settings as energy_settings

        use_community = bool(energy_settings().get("gemeinschaft_nutzen"))
    except Exception:
        return settings
    holding = bool(settings.get("community_hold"))
    if not use_community:
        if holding:
            updates: Dict[str, Any] = {"community_hold": False}
            if not settings.get("planning_hold") and not settings.get("exchange_hold"):
                updates["real_mode"] = False
            return save_settings(updates)
        return settings
    try:
        from energiegemeinschaft_mesh import peer_surplus_kw

        kw = planning_write_kw(peer_surplus_kw())
    except Exception:
        kw = None
    if kw is None:
        if holding:
            updates = {"community_hold": False}
            if not settings.get("planning_hold") and not settings.get("exchange_hold"):
                updates["real_mode"] = False
            return save_settings(updates)
        return settings
    stopped = _stopped_for_soc(settings)
    if stopped is not None:
        return stopped
    if (
        holding
        and settings.get("real_mode")
        and abs(float(settings.get("target_grid_kw") or 0) - kw) < 0.01
    ):
        return settings
    return save_settings({"real_mode": True, "target_grid_kw": kw, "community_hold": True})


def apply_exchange_hold(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Börsentarif: Buy nur unter der Preislinie, Sell nur darüber.

    Die kW kommen jedes Mal von den Schiebern. Der Zukauf wird auf das aktuelle
    Max. Netzbezug begrenzt. Zwischen den Linien wird nichts erzwungen.
    """
    if settings is None:
        settings = load_settings()
    if settings.get("community_hold"):
        return settings
    try:
        from energie_steuerung import load_settings as energy_settings

        use_tariff = bool(energy_settings().get("boersentarif_nutzen"))
    except Exception:
        return settings
    holding = bool(settings.get("exchange_hold"))
    if not use_tariff:
        if holding:
            updates: Dict[str, Any] = {"exchange_hold": False}
            if not settings.get("planning_hold"):
                updates["real_mode"] = False
            return save_settings(updates)
        return settings
    try:
        from boersenhandel import load_settings as trade_settings
        from boersenhandel import price_view, tariff_grid_kw

        trade = trade_settings()
        price = price_view(trade["exchange"], "24h", trade).get("current_ct_kwh")
    except Exception:
        return settings
    if price is None:
        return settings
    caps = install_caps_kw(settings)
    kw = tariff_grid_kw(
        price,
        trade["buy_ct_kwh"],
        trade["sell_ct_kwh"],
        trade.get("buy_kw"),
        trade.get("sell_kw"),
        caps["import_kw"],
        caps["sell_kw"],
    )
    if kw is None:
        if holding:
            updates = {"exchange_hold": False}
            if not settings.get("planning_hold"):
                updates["real_mode"] = False
            return save_settings(updates)
        return settings
    stopped = _stopped_for_soc(settings)
    if stopped is not None:
        return stopped
    if (
        holding
        and settings.get("real_mode")
        and abs(float(settings.get("target_grid_kw") or 0) - kw) < 0.01
    ):
        return settings
    return save_settings({"real_mode": True, "target_grid_kw": kw, "exchange_hold": True})


_mac_tracker_started = False
_mac_tracker_last = 0.0


def _ensure_mac_tracker() -> None:
    """Periodisch Logger-MAC/IP prüfen und Änderungen nach Home Assistant schreiben."""
    global _mac_tracker_started, _mac_tracker_last
    if _mac_tracker_started:
        return
    _mac_tracker_started = True

    def loop() -> None:
        global _mac_tracker_last
        # Sofort einmal, danach alle LOGGER_TRACK_GAP_SEC (3600 = stündlich) für jede gespeicherte MAC.
        # Kein PV-Poller: die Anzeige liest nur Home Assistant, nicht die Solarman-Register.
        _mac_tracker_last = time.monotonic() - LOGGER_TRACK_GAP_SEC
        while True:
            try:
                now = time.monotonic()
                if now - _mac_tracker_last >= LOGGER_TRACK_GAP_SEC:
                    refresh_tracked_inverters()
                    _mac_tracker_last = time.monotonic()
            except Exception:
                pass
            time.sleep(15)

    threading.Thread(target=loop, name="bms-mac-tracker", daemon=True).start()


def _ensure_writer() -> None:
    global _writer_started
    _ensure_mac_tracker()
    if _writer_started:
        return
    _writer_started = True

    def loop() -> None:
        while True:
            try:
                settings = load_settings()
                live = read_live()
                settings = apply_soc_gate(settings, live)
                settings = apply_community_hold(settings)
                if not settings.get("community_hold"):
                    settings = apply_exchange_hold(settings)
                if not settings.get("community_hold") and not settings.get("exchange_hold"):
                    settings = apply_planning_hold(settings, live)
                if settings.get("real_mode"):
                    reapply_dauerzukauf_test()
                plan = simulate(
                    live.get("grid_kw"),
                    live.get("battery_kw"),
                    settings["target_grid_kw"],
                    settings["peak_cap_kw"],
                )
                item = INVERTERS[settings["inverter"]]
                write = apply_write(settings, plan, live)
                text = write.get("write_text") or ""
                if settings.get("real_mode") or write.get("command_sent") or "Abweichung" in text:
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
        "max_export_w": read_ha_entity_state("number.192_168_1_176_export_surplus_power"),
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
        "inverters": inverter_choices(str(settings.get("inverter") or "")),
        "logger": {
            "ip": settings.get("logger_ip") or settings.get("modbus_host") or "",
            "mac": settings.get("logger_mac") or "",
            "serial": int(settings.get("logger_serial") or 0),
            "tracked_at": settings.get("logger_tracked_at") or "",
            "port": 8899,
            "interval_sec": LOGGER_TRACK_GAP_SEC,
        },
        "tracked_inverters": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "mac": item.get("mac"),
                "serial": item.get("serial"),
                "ip": item.get("ip"),
                "control": bool(item.get("control")),
                "role": "erzeugung",
                "tracked_at": item.get("tracked_at") or "",
                "pv_kw": None,
            }
            for item in (settings.get("tracked_inverters") or [])
        ],
        "discovered": _last_discovery,
        "live": live,
        "registers": read_register_state(),
        "assessment": judgement,
        "plan": plan,
        "log": log[:LOG_LIMIT],
        "command_sent": bool(write["command_sent"]),
    }
