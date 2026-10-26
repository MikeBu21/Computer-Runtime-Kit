#!/usr/bin/env python3
"""
Telegram Home-Assistant-Datenüberwachung.

Prüft ob HA-Snapshot und Dashboard-Daten regelmäßig aktualisiert werden.
Bei veralteten Daten: Alarm „keine stabile Verbindung“.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
EM_DIR = BASE_DIR / "energy_monitor"
HA_SNAPSHOT_PATH = Path("/tmp/ha_snapshot.json")
ENLINE_PATH = EM_DIR / "enline_values.json"
LATCH_FILE = BASE_DIR / "telegram_ha_latch.json"

# Bridge + Dashboard-Sync alle 30 s → 90 s ohne Update = instabil
STALE_SECONDS = 90


def _latch_path() -> Path:
    try:
        from ram_cache import backend_ephemeral_path, is_enabled

        if is_enabled():
            return backend_ephemeral_path(BASE_DIR, "telegram_ha_latch.json")
    except Exception:
        pass
    return LATCH_FILE


def _read_enline_payload() -> Dict[str, Any]:
    try:
        from ram_cache import is_enabled, read_json

        if is_enabled():
            data = read_json(BASE_DIR, "enline_values.json", scope="energy_monitor")
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    return _read_json(ENLINE_PATH)


def _config_bool(config: Dict[str, Any], key: str, default: bool = False) -> bool:
    val = config.get(key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def ha_monitor_enabled(config: Dict[str, Any]) -> bool:
    if "ha_data_monitor_enabled" in config:
        return _config_bool(config, "ha_data_monitor_enabled")
    return _config_bool(config, "speicher_ausfall_enabled")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_latch() -> Dict[str, Any]:
    return _read_json(_latch_path())


def save_latch(data: Dict[str, Any]) -> None:
    _write_json(_latch_path(), data)


def _parse_iso_age_seconds(iso_text: str) -> Optional[float]:
    try:
        text = str(iso_text).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - dt).total_seconds())
    except Exception:
        return None


def _file_age_seconds(path: Path) -> Optional[float]:
    if not path.is_file():
        return None
    try:
        return max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
    except OSError:
        return None


def _data_age(path: Path, payload: Dict[str, Any]) -> Optional[float]:
    if payload.get("updated_at"):
        age = _parse_iso_age_seconds(str(payload["updated_at"]))
        if age is not None:
            return age
    return _file_age_seconds(path)


def read_ha_data_state() -> Dict[str, Any]:
    """Aktueller Verbindungszustand für UI und /status."""
    snap = _read_json(HA_SNAPSHOT_PATH)
    enline = _read_enline_payload()
    enline_path = _latch_path().parent / "energy_monitor" / "enline_values.json"
    try:
        from ram_cache import is_enabled, live_energy_path

        if is_enabled():
            enline_path = live_energy_path(BASE_DIR, "enline_values.json")
    except Exception:
        pass

    snap_age = _data_age(HA_SNAPSHOT_PATH, snap)
    dash_age = _data_age(enline_path, enline)

    snap_fresh = snap_age is not None and snap_age <= STALE_SECONDS
    dash_fresh = dash_age is not None and dash_age <= STALE_SECONDS
    stable = snap_fresh and dash_fresh

    detail_parts: List[str] = []
    if snap_age is not None:
        detail_parts.append(f"Snapshot {int(snap_age)} s")
    else:
        detail_parts.append("Snapshot fehlt")
    if dash_age is not None:
        detail_parts.append(f"Dashboard {int(dash_age)} s")
    else:
        detail_parts.append("Dashboard fehlt")

    return {
        "stable": stable,
        "snapshot_fresh": snap_fresh,
        "dashboard_fresh": dash_fresh,
        "snapshot_age": snap_age,
        "dashboard_age": dash_age,
        "detail": " · ".join(detail_parts),
        "snapshot_exists": HA_SNAPSHOT_PATH.is_file(),
        "dashboard_exists": enline_path.is_file(),
    }


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _status_row(active: bool, label: str, detail: str = "") -> str:
    icon = "✅" if active else "❌"
    extra = f" · <i>{_esc(detail)}</i>" if detail else ""
    state = "aktiv" if active else "aus"
    return f"{icon} <b>{_esc(label)}</b>  <i>({state})</i>{extra}"


def build_dashboard_ha_status_lines(config: Dict[str, Any]) -> List[str]:
    """Zeilen für /status — Datenüberwachung Home Assistant."""
    monitor_on = ha_monitor_enabled(config)
    state = read_ha_data_state()

    lines: List[str] = [
        "",
        "📡 <b>Datenüberwachung / Home Assistant</b>",
        _status_row(monitor_on, "Telegram HA-Alarm"),
    ]

    if state["stable"]:
        lines.append(
            _status_row(True, "Verbindung stabil", state["detail"])
        )
    else:
        lines.append(
            _status_row(False, "Verbindung", "keine stabile Verbindung")
        )
        lines.append(f"⚠️ <i>{_esc(state['detail'])}</i>")

    lines.append("")
    return lines


def build_alarm_message(state: Dict[str, Any]) -> str:
    return (
        "⚠️ <b>Home Assistant: keine stabile Verbindung</b>\n\n"
        f"📡 {_esc(state['detail'])}\n\n"
        "Bitte <code>ha_data_bridge</code> und Dashboard prüfen."
    )


def build_recovery_message() -> str:
    return "✅ <b>Home Assistant: Datenverbindung wieder stabil.</b>"


def evaluate_ha_telegram_alarm(
    config: Dict[str, Any],
    latch: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Returns (alarm_message_html, recovery_message_html, use_html).
    """
    if not ha_monitor_enabled(config):
        lat = latch if latch is not None else load_latch()
        lat["ha_alarm_active"] = False
        save_latch(lat)
        return None, None, True

    state = read_ha_data_state()
    lat = latch if latch is not None else load_latch()
    alarm_active = bool(lat.get("ha_alarm_active"))

    if not state["stable"]:
        if not alarm_active:
            lat["ha_alarm_active"] = True
            lat["ha_alarm_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_latch(lat)
            return build_alarm_message(state), None, True
        return None, None, True

    if alarm_active:
        lat["ha_alarm_active"] = False
        lat["ha_alarm_at"] = None
        save_latch(lat)
        return None, build_recovery_message(), True

    return None, None, True
