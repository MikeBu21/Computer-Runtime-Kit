#!/usr/bin/env python3
"""Einheitliche Geräte-Abstraktion: Miner + Home-Assistant-Schaltgeräte."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
MINERS_PATH = BACKEND_DIR / "miners.json"
HA_DEVICES_PATH = BACKEND_DIR / "ha_devices.json"
GPIO_PINS_PATH = BACKEND_DIR / "gpio_pins.json"
HA_SNAPSHOT_PATH = Path("/tmp/ha_snapshot.json")


def load_miners_raw() -> List[Dict[str, Any]]:
    if not MINERS_PATH.is_file():
        return []
    try:
        return json.loads(MINERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_miners_raw(miners: List[Dict[str, Any]]) -> None:
    MINERS_PATH.write_text(
        json.dumps(miners, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_ha_devices_raw() -> List[Dict[str, Any]]:
    if not HA_DEVICES_PATH.is_file():
        return []
    try:
        return json.loads(HA_DEVICES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_ha_devices(devices: List[Dict[str, Any]]) -> None:
    HA_DEVICES_PATH.write_text(
        json.dumps(devices, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _miner_as_device(miner: Dict[str, Any]) -> Dict[str, Any]:
    ip = miner.get("ip", "")
    return {
        "id": ip,
        "type": "miner",
        "label": miner.get("note") or miner.get("model") or ip,
        "ip": ip,
        "username": miner.get("username", "root"),
        "password": miner.get("password", ""),
        "auto": bool(miner.get("auto")),
        "auto_battery": bool(miner.get("auto_battery")),
        "pause_kw": miner.get("pause_kw", ""),
        "resume_kw": miner.get("resume_kw", ""),
        "lower_kw": miner.get("lower_kw", ""),
        "raise_kw": miner.get("raise_kw", ""),
        "time_pause": bool(miner.get("time_pause")),
        "time_ths": bool(miner.get("time_ths")),
        "pause_time": miner.get("pause_time", ""),
        "resume_time": miner.get("resume_time", ""),
        "low_time": miner.get("low_time", ""),
        "high_time": miner.get("high_time", ""),
        "note": miner.get("note", ""),
        "model": miner.get("model", ""),
        "control_source": miner.get("control_source", "field.kelag"),
        "battery_control_source": miner.get(
            "battery_control_source", "flow.battery_soc"
        ),
        "automation_rules": miner.get("automation_rules") or [],
        "mac": miner.get("mac", ""),
        "recovery_state": miner.get("recovery_state", "ok"),
    }


def _ha_as_device(dev: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = dev.get("entity_id", "")
    domain = entity_id.split(".", 1)[0] if entity_id else ""
    return {
        "id": entity_id,
        "type": "ha",
        "entity_id": entity_id,
        "domain": domain,
        "label": dev.get("label") or entity_id,
        "note": dev.get("note", ""),
        "auto": bool(dev.get("auto")),
        "auto_battery": bool(dev.get("auto_battery")),
        "pause_kw": dev.get("pause_kw", ""),
        "resume_kw": dev.get("resume_kw", ""),
        "lower_kw": dev.get("lower_kw", ""),
        "raise_kw": dev.get("raise_kw", ""),
        "time_pause": bool(dev.get("time_pause")),
        "pause_time": dev.get("pause_time", ""),
        "resume_time": dev.get("resume_time", ""),
        "control_source": dev.get("control_source", "field.kelag"),
        "battery_control_source": dev.get(
            "battery_control_source", "flow.battery_soc"
        ),
        "automation_rules": dev.get("automation_rules") or [],
    }


def _gpio_as_device(dev: Dict[str, Any]) -> Dict[str, Any]:
    from gpio_pins import pin_id, parse_pin_id

    pin = parse_pin_id(dev.get("pin") if dev.get("pin") is not None else dev.get("id"))
    gid = pin_id(pin) if pin is not None else str(dev.get("id") or "")
    return {
        "id": gid,
        "type": "gpio",
        "pin": pin,
        "ip": gid,
        "label": dev.get("label") or gid,
        "note": dev.get("note", ""),
        "power_kw": dev.get("power_kw", ""),
        "priority": dev.get("priority", 5),
        "auto": False,
        "auto_battery": False,
    }


def load_gpio_pins_raw() -> List[Dict[str, Any]]:
    from gpio_pins import load_gpio_pins_raw as _load

    return _load()


def save_gpio_pins(pins: List[Dict[str, Any]]) -> None:
    from gpio_pins import save_gpio_pins as _save

    _save(pins)


def load_all_controllable() -> List[Dict[str, Any]]:
    devices = [_miner_as_device(m) for m in load_miners_raw() if m.get("ip")]
    devices.extend(_ha_as_device(d) for d in load_ha_devices_raw() if d.get("entity_id"))
    devices.extend(_gpio_as_device(d) for d in load_gpio_pins_raw() if d.get("pin") is not None or d.get("id"))
    return devices


def _load_snapshot() -> Dict[str, Any]:
    if not HA_SNAPSHOT_PATH.is_file():
        return {}
    try:
        return json.loads(HA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _entity_state_from_snapshot(entity_id: str, snapshot: Optional[Dict[str, Any]] = None) -> str:
    snapshot = snapshot if snapshot is not None else _load_snapshot()
    for device in (snapshot.get("devices") or {}).values():
        ent = (device.get("entities") or {}).get(entity_id)
        if ent:
            return str(ent.get("state") or "")
    ent = (snapshot.get("unassigned_entities") or {}).get(entity_id)
    if ent:
        return str(ent.get("state") or "")
    return ""


def get_ha_device_status_map(snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    from ha_device_control import ha_state_to_device_status, read_ha_entity_state

    result: Dict[str, str] = {}
    for dev in load_ha_devices_raw():
        eid = dev.get("entity_id")
        if not eid:
            continue
        # Live bevorzugen – Snapshot bleibt oft „on“ obwohl Pause gesendet wurde
        # und triggert sonst Dauer-Prioritätskorrekturen gegen die Miner.
        live = None
        try:
            live = read_ha_entity_state(eid)
        except Exception:
            live = None
        state = live if live else _entity_state_from_snapshot(eid, snapshot)
        result[eid] = ha_state_to_device_status(state)
    return result


def get_gpio_status_map() -> Dict[str, str]:
    from gpio_pins import get_gpio_status_map as _map

    return _map()


def get_device_status_map() -> Dict[str, str]:
    """Kombinierter Status: Miner (SSH) + HA + Gibbo-Pins."""
    try:
        from dashboard_miner_status import load_miner_status_map

        status = dict(load_miner_status_map())
    except Exception:
        status = {}
    status.update(get_ha_device_status_map())
    try:
        status.update(get_gpio_status_map())
    except Exception:
        pass
    return status


def execute_miner_command(ip: str, action: str, username: str = "root", password: str = "") -> bool:
    # Explizit Backend-Modul laden (nicht energy_monitor/miner_control Schatten).
    import sys
    from pathlib import Path

    backend_dir = str(Path(__file__).resolve().parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    # Falls ein falsches miner_control schon geladen wurde: neu aus Backend-Datei.
    existing = sys.modules.get("miner_control")
    if existing is not None:
        mod_file = getattr(existing, "__file__", "") or ""
        if "energy_monitor" in mod_file.replace("\\", "/"):
            del sys.modules["miner_control"]
    from miner_control import execute_miner_pause_resume

    miners = load_miners_raw()
    miner = next((m for m in miners if str(m.get("ip") or "") == ip), None)
    if miner:
        try:
            from miner_control import _host_reachable, _ssh_circuit_open

            if not (_ssh_circuit_open(ip) or not _host_reachable(ip, timeout=1.0)):
                from ip_recovery import capture_mac_if_missing

                capture_mac_if_missing(ip, username, password)
        except Exception:
            pass

    if action in ("pause", "resume"):
        ok = execute_miner_pause_resume(ip, action, username, password)
        if ok:
            try:
                from ip_recovery import mark_miner_reachable

                mark_miner_reachable(ip)
            except Exception:
                pass
        if not ok and miner:
            # Kein Recovery-Scan für bekannte tote Miner (Circuit / kein Ping) –
            # sonst hängt der Steuerungs-Tick Minuten in IP-Suchen.
            try:
                from miner_control import _should_skip_unreachable

                if _should_skip_unreachable(ip):
                    return False
            except Exception:
                try:
                    from miner_control import _host_reachable, _ssh_circuit_open

                    if _ssh_circuit_open(ip) or not _host_reachable(ip, timeout=1.0):
                        return False
                except Exception:
                    pass
            try:
                from ip_recovery import try_recover_miner, mark_miner_reachable

                result = try_recover_miner(miner)
                if result.success and result.new_ip:
                    ok2 = execute_miner_pause_resume(
                        result.new_ip, action, username, password
                    )
                    if ok2:
                        try:
                            mark_miner_reachable(result.new_ip)
                        except Exception:
                            pass
                    return ok2
            except Exception:
                pass
        return ok

    if action in ("low", "high"):
        level = "60" if action == "low" else "100"
        result = subprocess.run(
            [str(BACKEND_DIR / "control.sh"), ip, username, password, level],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(BACKEND_DIR),
        )
        if result.returncode != 0:
            logger.error(
                "control.sh %s für %s fehlgeschlagen (Code %s): %s",
                action,
                ip,
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:200],
            )
            return False
        return True
    return False


def execute_device_action(
    device: Dict[str, Any],
    action: str,
    *,
    source: str = "auto",
    actor: Optional[Dict[str, Any]] = None,
) -> bool:
    """Zentrale Schaltfunktion für Miner und HA-Geräte.

    source='manual': Dashboard / Telegram / andere Hand-Schaltungen.
    source='auto' (Standard): Steueralgorithmus, Zeitsteuerung, Automatik
    — dann keine Nutzer-Broadcast-Nachricht.
    """
    ok = False
    dtype = str(device.get("type") or "").lower()
    if dtype == "ha":
        from ha_device_control import execute_ha_command

        entity_id = device.get("entity_id") or device.get("id")
        if not entity_id:
            return False
        ok = execute_ha_command(entity_id, action)
    elif dtype == "gpio" or device.get("pin") is not None:
        from gpio_pins import execute_gpio_action, is_gpio_device_key

        key = device.get("id") or device.get("ip") or device.get("pin")
        if not is_gpio_device_key(key) and device.get("pin") is None:
            return False
        ok = execute_gpio_action(device, action)
    else:
        ip = device.get("ip") or device.get("id")
        if not ip:
            return False
        # gpio:N ohne type-Feld
        try:
            from gpio_pins import execute_gpio_action, is_gpio_device_key

            if is_gpio_device_key(ip):
                ok = execute_gpio_action(ip, action)
            else:
                ok = execute_miner_command(
                    ip,
                    action,
                    device.get("username", "root"),
                    device.get("password", ""),
                )
        except Exception:
            ok = execute_miner_command(
                ip,
                action,
                device.get("username", "root"),
                device.get("password", ""),
            )
    if ok and str(source).strip().lower() == "manual":
        try:
            from manual_switch_notify import notify_manual_switch

            notify_manual_switch(device, action, actor=actor)
        except Exception:
            logger.exception("Manuelle-Schalt-Nachricht nicht gesendet")
    return ok


def default_ha_device(entity_id: str, label: str = "", note: str = "") -> Dict[str, Any]:
    return {
        "entity_id": entity_id,
        "label": label or entity_id,
        "note": note,
        "auto": False,
        "auto_battery": False,
        "pause_kw": "",
        "resume_kw": "",
        "lower_kw": "",
        "raise_kw": "",
        "time_pause": False,
        "pause_time": "",
        "resume_time": "",
        "control_source": "field.kelag",
        "battery_control_source": "flow.battery_soc",
    }
