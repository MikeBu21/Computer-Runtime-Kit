# -*- coding: utf-8 -*-
"""BMS-Unterseite: Live-Anzeige und Testrechnung.

Es werden keine Befehle an den Wechselrichter geschrieben. Der Testmodus
rechnet Soll-Netzbezug, Batterie-Sollleistung und Spitzenkappung nur zur Anzeige.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BACKEND_DIR / "bms_test_settings.json"
LOG_FILE = BACKEND_DIR / "bms_test_log.json"
TZ = ZoneInfo("Europe/Vienna")

SOC_EMPTY = 10.0
SOC_FULL = 98.0
BATTERY_LIMIT_KW = 12.0
LOG_LIMIT = 30
SAMPLE_GAP_SEC = 15


def default_settings() -> Dict[str, Any]:
    return {
        "test_mode": False,
        "target_grid_kw": 2.0,
        "peak_cap_kw": 2.0,
    }


def _kw(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return round(min(100.0, max(0.0, number)), 2)


def load_settings() -> Dict[str, Any]:
    data = default_settings()
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    data["test_mode"] = bool(data.get("test_mode"))
    data["target_grid_kw"] = _kw(data.get("target_grid_kw"), 2.0)
    data["peak_cap_kw"] = _kw(data.get("peak_cap_kw"), 2.0)
    return data


def save_settings(test_mode: bool, target_grid_kw: float, peak_cap_kw: float) -> Dict[str, Any]:
    payload = {
        "test_mode": bool(test_mode),
        "target_grid_kw": _kw(target_grid_kw, 2.0),
        "peak_cap_kw": _kw(peak_cap_kw, 2.0),
    }
    SETTINGS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


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
        if soc >= SOC_FULL:
            can_buy = False
            warnings.append(
                f"Batterie voll ({soc:.0f} %). Ein Zukauf von {target_kw:.1f} kW kann nicht mehr eingelagert werden."
            )
    if not warnings and soc is not None:
        warnings.append(
            f"Zukauf von {target_kw:.1f} kW und Spitzenkappung ab {peak_cap_kw:.1f} kW sind vom Ladezustand her möglich."
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
    """Reine Rechnung. Schickt nichts an den Wechselrichter."""
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
        action = "würde laden"
    elif setpoint < -0.05:
        action = "würde entladen"
    else:
        action = "würde halten"
    return {
        "target_grid_kw": soll,
        "peak_shave_kw": shaved,
        "battery_setpoint_kw": setpoint,
        "action": action,
        "limited": limited,
        "command_sent": False,
    }


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


def _log_line(live: Dict[str, Any], plan: Dict[str, Any]) -> str:
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
    return f"{grid_text}, Soll-Netzbezug {soll:.1f} kW, {peak_text}, {battery_text}. Nur Simulation."


def status(record: bool = True) -> Dict[str, Any]:
    settings = load_settings()
    live = read_live()
    judgement = assess(live.get("soc_percent"), settings["target_grid_kw"], settings["peak_cap_kw"])
    plan = None
    log = _load_log()
    if settings["test_mode"]:
        plan = simulate(
            live.get("grid_kw"),
            live.get("battery_kw"),
            settings["target_grid_kw"],
            settings["peak_cap_kw"],
        )
        if record:
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
                        "text": _log_line(live, plan),
                        "command_sent": False,
                    },
                )
                _save_log(log)
    return {
        "settings": settings,
        "live": live,
        "assessment": judgement,
        "plan": plan,
        "log": log[:LOG_LIMIT],
        "command_sent": False,
    }
