#!/usr/bin/env python3
"""Schaltbare Gibbo-/GPIO-Pins (Raspberry Pi Board-Pins, wie heizstap)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
GPIO_PINS_PATH = BACKEND_DIR / "gpio_pins.json"
COMMANDS_PATH = BACKEND_DIR / "commands.json"
PIN_STATUS_PATH = BACKEND_DIR / "gpio_pin_status.json"

# Board-Pin → BCM (gleicher Satz wie heizstap.py)
PIN_MAP: Dict[int, int] = {
    3: 2,
    5: 3,
    7: 4,
    11: 17,
    13: 27,
    15: 22,
    19: 10,
    21: 9,
}
AVAILABLE_PINS: List[int] = list(PIN_MAP.keys())


def pin_id(pin: int) -> str:
    return f"gpio:{int(pin)}"


def parse_pin_id(raw: Any) -> Optional[int]:
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if s.startswith("gpio:"):
        s = s.split(":", 1)[1]
    elif s.startswith("gpio-"):
        s = s.split("-", 1)[1]
    try:
        pin = int(s)
    except ValueError:
        return None
    return pin if pin in PIN_MAP else None


def is_gpio_device_key(key: Any) -> bool:
    return parse_pin_id(key) is not None


def load_gpio_pins_raw() -> List[Dict[str, Any]]:
    if not GPIO_PINS_PATH.is_file():
        return []
    try:
        data = json.loads(GPIO_PINS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_gpio_pins(pins: List[Dict[str, Any]]) -> None:
    GPIO_PINS_PATH.write_text(
        json.dumps(pins, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def default_gpio_pin(
    pin: int,
    *,
    label: str = "",
    note: str = "",
    power_kw: float = 0.0,
    priority: float = 5.0,
) -> Dict[str, Any]:
    pin = int(pin)
    return {
        "pin": pin,
        "id": pin_id(pin),
        "label": label or f"Gibbo-Pin {pin}",
        "note": note or "",
        "power_kw": float(power_kw or 0),
        "priority": float(priority if priority is not None else 5),
    }


def available_pins_for_select(existing: Optional[List[Dict[str, Any]]] = None) -> List[int]:
    used = {int(d["pin"]) for d in (existing or load_gpio_pins_raw()) if d.get("pin") is not None}
    return [p for p in AVAILABLE_PINS if p not in used]


def _load_commands() -> Dict[str, Any]:
    if not COMMANDS_PATH.is_file():
        return {}
    try:
        data = json.loads(COMMANDS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_commands(data: Dict[str, Any]) -> None:
    tmp = COMMANDS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(COMMANDS_PATH)


def write_pin_command(pin: int, action: str) -> bool:
    """Schreibt on/off in commands.json — heizstap.py schaltet den Pin (kein zweiter GPIO-Owner)."""
    pin = int(pin)
    if pin not in PIN_MAP:
        return False
    act = "on" if str(action).lower() in ("on", "resume", "ein", "1", "true") else "off"
    data = _load_commands()
    data[str(pin)] = {"action": act, "timestamp": datetime.now().isoformat()}
    try:
        _save_commands(data)
        return True
    except Exception:
        logger.exception("GPIO-Befehl Pin %s fehlgeschlagen", pin)
        return False


def _normalize_pin_status(raw: Any) -> Optional[str]:
    text = str(raw or "").strip().lower()
    if text in ("running", "run", "on", "ein", "active"):
        return "running"
    if text in ("paused", "pause", "off", "aus", "stopped", "stop"):
        return "paused"
    return None


def read_pin_status(pin: int) -> str:
    """running | paused | unknown — bevorzugt heizstap gpio_pin_status.json."""
    pin = int(pin)
    if pin not in PIN_MAP:
        return "unknown"
    # 1) Live-Status von heizstap (einziger GPIO-Owner)
    if PIN_STATUS_PATH.is_file():
        try:
            data = json.loads(PIN_STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    if int(entry.get("pin") or -1) != pin:
                        continue
                    norm = _normalize_pin_status(entry.get("status"))
                    if norm:
                        return norm
        except Exception:
            pass
    # 2) Letzter Befehl
    cmd = _load_commands().get(str(pin)) or {}
    norm = _normalize_pin_status(cmd.get("action"))
    if norm:
        return norm
    return "unknown"


def get_gpio_status_map() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for dev in load_gpio_pins_raw():
        pin = parse_pin_id(dev.get("pin") if dev.get("pin") is not None else dev.get("id"))
        if pin is None:
            continue
        result[pin_id(pin)] = read_pin_status(pin)
    return result


def execute_gpio_action(device_or_pin: Any, action: str) -> bool:
    pin = None
    if isinstance(device_or_pin, dict):
        pin = parse_pin_id(
            device_or_pin.get("pin")
            or device_or_pin.get("id")
            or device_or_pin.get("ip")
            or device_or_pin.get("entity_id")
        )
    else:
        pin = parse_pin_id(device_or_pin)
    if pin is None:
        return False
    mapped = "on" if str(action).lower() in ("on", "resume", "ein", "1", "true") else "off"
    return write_pin_command(pin, mapped)
