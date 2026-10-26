"""Liest Home-Assistant-Snapshot und schreibt energy_monitor/*.txt Dateien."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

FILE_MAP = {
    "bezug": "bezug_value.txt",
    "lieferung": "lieferung_value.txt",
    "betriebsverbrauch": "betriebsverbrauch_value.txt",
    "wohnhaus": "wohnhaus_value.txt",
    "wasserkraft": "wasserkraft_value.txt",
    "pv": "pv_value.txt",
    "betrieb": "betrieb_value.txt",
    "erzeugung_gesamt": "erzeugung_gesamt_value.txt",
    "eigenverbrauch": "eigenverbrauch_value.txt",
    "kelag": "kelag_value.txt",
}

HA_SNAPSHOT_PATH = Path("/tmp/ha_snapshot.json")
OUT_DIR = Path(__file__).resolve().parent
BATTERY_JSON = OUT_DIR / "daten.json"


def _find_entity(snapshot: Dict[str, Any], entity_id: str) -> Optional[Dict[str, Any]]:
    if not entity_id:
        return None
    for device in (snapshot.get("devices") or {}).values():
        entities = device.get("entities") or {}
        if entity_id in entities:
            return entities[entity_id]
    unassigned = snapshot.get("unassigned_entities") or {}
    return unassigned.get(entity_id)


def _unit(entity: Dict[str, Any]) -> str:
    attrs = entity.get("attributes") or {}
    return str(
        entity.get("unit_of_measurement") or attrs.get("unit_of_measurement") or ""
    ).lower()


def parse_entity_power_kw(
    entity: Optional[Dict[str, Any]], signed: bool = False
) -> Optional[float]:
    if not entity:
        return None
    state = entity.get("state")
    if state in (None, "unavailable", "unknown", ""):
        return None
    try:
        val = float(str(state).replace(",", ".").strip())
    except ValueError:
        return None
    unit = _unit(entity)
    # Nur Leistungs-Einheiten – sonst landen z. B. Volt als „kW“ im Stromfluss.
    if unit in ("w", "watt"):
        val /= 1000.0
    elif unit == "mw":
        val *= 1000.0
    elif unit in ("kw", "kilowatt"):
        pass
    elif unit in ("",):
        # Manche HA-Sensoren ohne Unit: Rohwert wie bisher als kW annehmen.
        pass
    else:
        return None
    return round(val, 3)


def parse_entity_percent(entity: Optional[Dict[str, Any]]) -> Optional[float]:
    if not entity:
        return None
    state = entity.get("state")
    if state in (None, "unavailable", "unknown", ""):
        return None
    text = str(state).strip().replace("%", "").replace(",", ".")
    try:
        val = float(text)
    except ValueError:
        return None
    if 0 <= val <= 100:
        return round(val, 1)
    return None


def load_snapshot() -> Dict[str, Any]:
    if not HA_SNAPSHOT_PATH.is_file():
        raise FileNotFoundError(f"HA-Snapshot fehlt: {HA_SNAPSHOT_PATH}")
    return json.loads(HA_SNAPSHOT_PATH.read_text(encoding="utf-8"))


def read_mapped_values(
    cfg: Dict[str, Any], snapshot: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    snap = snapshot or load_snapshot()
    fields = cfg.get("fields") or {}
    values: Dict[str, Any] = {}

    from ha_entity_mapping import (
        FIELD_DEFINITIONS,
        compute_field_value,
        normalize_field_entry,
    )

    for key, meta in FIELD_DEFINITIONS.items():
        entry = normalize_field_entry(fields.get(key) or {})
        if not entry.get("enabled"):
            continue
        if not entry.get("sensors"):
            continue
        val = compute_field_value(entry, meta, snap)
        if val is None:
            continue
        values[key] = val
    return values


def _write_battery_json(
    values: Dict[str, Any], output_dir: Optional[Path] = None
) -> None:
    payload: Dict[str, Any] = {}
    if "battery_soc" in values:
        payload["soc"] = values["battery_soc"]
    if "battery_power" in values:
        payload["power_kw"] = values["battery_power"]
    if not payload:
        return
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    out = Path(output_dir or OUT_DIR)
    try:
        from ram_cache import is_enabled, write_json

        if is_enabled():
            write_json(_BACKEND, "daten.json", payload, scope="energy_monitor")
            return
    except Exception:
        pass
    (out / "daten.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_dashboard_files(
    cfg: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]] = None,
    output_dir: Optional[Path] = None,
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Schreibt Legacy-*.txt und enline_values.json wie früher Enline."""
    values_raw = read_mapped_values(cfg, snapshot)
    out = Path(output_dir or OUT_DIR)
    base_dir = _BACKEND

    values_kw: Dict[str, float] = {}
    for key, val in values_raw.items():
        if key in ("battery_soc",):
            continue
        if isinstance(val, (int, float)):
            values_kw[key] = float(val)

    for key, fname in FILE_MAP.items():
        if key not in values_kw:
            continue
        content = str(values_kw[key])
        try:
            from ram_cache import is_enabled, write_text

            if is_enabled():
                write_text(base_dir, fname, content, scope="energy_monitor")
                continue
        except Exception:
            pass
        (out / fname).write_text(content, encoding="utf-8")

    if "kelag" in values_kw:
        content = str(values_kw["kelag"])
        try:
            from ram_cache import is_enabled, write_text

            if is_enabled():
                write_text(
                    base_dir, "energy_value.txt", content, scope="energy_monitor"
                )
            else:
                (out / "energy_value.txt").write_text(content, encoding="utf-8")
        except Exception:
            (out / "energy_value.txt").write_text(content, encoding="utf-8")

    from ha_entity_mapping import FIELD_DEFINITIONS

    for key, meta in FIELD_DEFINITIONS.items():
        entry = (cfg.get("fields") or {}).get(key) or {}
        if entry.get("enabled"):
            continue
        for fname in meta.get("output_files") or []:
            path = out / fname
            if path.is_file():
                try:
                    path.unlink()
                except Exception:
                    pass

    _write_battery_json(values_raw)

    rows = []
    for key, val in values_kw.items():
        meta = FIELD_DEFINITIONS.get(key, {})
        rows.append(
            {
                "name": meta.get("label", key),
                "slug": re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_"),
                "legacy_key": key,
                "value_kw": val,
                "unit": "kW",
            }
        )

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "home_assistant",
        "row_count": len(rows),
        "values_kw": values_kw,
        "rows": rows,
    }
    try:
        from ram_cache import is_enabled, write_json

        if is_enabled():
            write_json(
                base_dir, "enline_values.json", payload, scope="energy_monitor"
            )
            return values_kw, payload
    except Exception:
        pass
    (out / "enline_values.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return values_kw, payload


def get_kelag_value(cfg: Optional[Dict[str, Any]] = None) -> Optional[float]:
    from ha_entity_mapping import load_config

    cfg = cfg or load_config()
    values = read_mapped_values(cfg)
    kelag = values.get("kelag")
    if kelag is None:
        return None
    return float(kelag)
