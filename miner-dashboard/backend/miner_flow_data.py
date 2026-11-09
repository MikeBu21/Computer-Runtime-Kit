#!/usr/bin/env python3
"""
Stromfluss-Daten für Dashboard – liest energy_monitor/*.txt und enline_values.json.

Bei MINER_RAM_CACHE=1 kommen Live-Werte aus /dev/shm (wie ha_live_reader schreibt),
sonst von der SD. Sonst bleiben alte SD-Dateien sichtbar (z. B. Wohnhaus 1.789).

Rollen (Erzeugung / Netz / Speicher / Verbraucher) kommen aus dashboard_config;
gleiche Rollen werden intern summiert.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple


def _em_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "energy_monitor")


def _read_em_text(base_dir: str, filename: str) -> Optional[str]:
    """RAM zuerst (falls aktiv), sonst SD-Datei."""
    try:
        from ram_cache import is_enabled, read_text

        if is_enabled():
            raw = (read_text(base_dir, filename, default="", scope="energy_monitor") or "").strip()
            return raw if raw else None
    except Exception:
        pass
    path = os.path.join(_em_dir(base_dir), filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            v = fh.read().strip()
        return v if v else None
    except Exception:
        return None


def _read_first(base_dir: str, names: list) -> Optional[str]:
    for name in names:
        v = _read_em_text(base_dir, name)
        if v is not None:
            return v
    return None


def _read_json_field(base_dir: str, field: str) -> Optional[str]:
    data = None
    try:
        from ram_cache import is_enabled, read_json

        if is_enabled():
            data = read_json(
                base_dir, "enline_values.json", default=None, scope="energy_monitor"
            )
    except Exception:
        data = None
    if data is None:
        path = os.path.join(_em_dir(base_dir), "enline_values.json")
        try:
            import json

            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    values = data.get("values_kw") or data
    if not isinstance(values, dict):
        return None
    v = values.get(field)
    if v is not None and v != "":
        return str(v)
    return None


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "."))
    except ValueError:
        return None


def _format_kw(value: float) -> str:
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return str(rounded)


VERBRAUCH_GESAMT_FILE = "verbrauch_gesamt_value.txt"
BETRIEB_ANZEIGE_FILE = "betrieb_anzeige_value.txt"


def _write_kw_file(base_dir: str, filename: str, kw: Optional[float]) -> None:
    if kw is None:
        return
    content = _format_kw(kw)
    try:
        from ram_cache import is_enabled, write_text

        if is_enabled():
            write_text(base_dir, filename, content, scope="energy_monitor")
            return
    except Exception:
        pass
    path = os.path.join(_em_dir(base_dir), filename)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _total_verbrauch_kw(haus_kw: Optional[float]) -> Optional[float]:
    """Gesamtverbrauch aus Haus-/Verbraucher-Summe (Legacy-Hilfsfunktion)."""
    if haus_kw is None:
        return None
    return float(haus_kw)


def _load_dash_cfg() -> Dict[str, Any]:
    try:
        from dashboard_config import load_config

        return load_config()
    except Exception:
        return {}


def _hidden_builtins(dash_cfg: Dict[str, Any]) -> set:
    try:
        from dashboard_config import hidden_builtins

        return hidden_builtins(dash_cfg)
    except Exception:
        return set(dash_cfg.get("flow_hidden_builtins") or [])


def _signed_ha_field_kw(
    field_key: str,
    ha_cfg: Optional[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]],
) -> Optional[float]:
    """Live-Feldwert inkl. Sensor-Vorzeichen (+/−) aus dem Mapping-Modal."""
    if not ha_cfg or not snapshot:
        return None
    try:
        from ha_entity_mapping import (
            FIELD_DEFINITIONS,
            compute_field_value,
            normalize_field_entry,
        )

        entry = normalize_field_entry((ha_cfg.get("fields") or {}).get(field_key) or {})
        if not entry.get("enabled") or not entry.get("sensors"):
            return None
        meta = FIELD_DEFINITIONS.get(field_key) or {"value_type": "power_kw"}
        # compute_field_value: jeder Sensor mit sign "+" addiert, "-" subtrahiert
        return compute_field_value(entry, meta, snapshot)
    except Exception:
        return None


def _collect_role_contributions(
    base_dir: str,
    *,
    pv_f: Optional[float],
    hydro_f: Optional[float],
    grid_f: Optional[float],
    haus_f: Optional[float],
    betrieb_f: Optional[float],
    battery_power_kw: Optional[float],
    dash_cfg: Dict[str, Any],
) -> Tuple[Dict[str, float], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Summiert sichtbare Punkte nach Rolle (mit Sensor-Vorzeichen +/−).

    Rollensumme = Summe der vorzeichenbehafteten Punktwerte.
    Punktwert = Summe der Sensoren mit ihrem Plus-/Minus-Präfix (wie im Modal).
    Negative Beiträge (Minus-Präfix) werden abgezogen, nie als Betrag addiert.
    """
    from flow_roles import (
        add_to_role_sum,
        builtin_role,
        custom_field_role,
        empty_role_sums,
    )

    hidden = _hidden_builtins(dash_cfg)
    sums = empty_role_sums()
    details: List[Dict[str, Any]] = []

    snapshot = None
    ha_cfg = None
    try:
        from dashboard_sources import _load_ha_snapshot
        from ha_entity_mapping import load_config as load_ha_config

        snapshot = _load_ha_snapshot()
        ha_cfg = load_ha_config()
    except Exception:
        snapshot = None
        ha_cfg = None

    # Built-ins: Live-HA (mit +/−) bevorzugen, sonst Datei-/Fallback-Wert
    builtin_file_fallback = {
        "pv": pv_f,
        "wasserkraft": hydro_f,
        "kelag": grid_f,
        "wohnhaus": haus_f,
        "betrieb": betrieb_f,
        "battery": battery_power_kw,
    }
    builtin_ha_field = {
        "pv": "pv",
        "wasserkraft": "wasserkraft",
        "kelag": "kelag",
        "wohnhaus": "wohnhaus",
        "betrieb": "betrieb",
        "battery": "battery_power",
    }
    for point_key, fallback in builtin_file_fallback.items():
        if point_key in hidden:
            continue
        role = builtin_role(dash_cfg, point_key)
        value = None
        ha_key = builtin_ha_field.get(point_key)
        if ha_key:
            value = _signed_ha_field_kw(ha_key, ha_cfg, snapshot)
        if value is None:
            value = fallback
        # signed value unverändert zur Rollensumme (Minus-Sensor → negativer Beitrag)
        add_to_role_sum(sums, role, value)
        if value is not None:
            details.append({"point": point_key, "role": role, "kw": float(value)})

    custom_fields: Dict[str, Dict[str, Any]] = {}
    try:
        from dashboard_sources import resolve_custom_flow_values

        # resolve_custom_flow_values → compute_field_value (Sensor-Vorzeichen +/−)
        custom_fields = resolve_custom_flow_values(dash_cfg, snapshot)
    except Exception:
        custom_fields = {}

    for field in dash_cfg.get("custom_flow_fields") or []:
        if not field.get("enabled", True):
            continue
        fid = field.get("id")
        if not fid:
            continue
        role = custom_field_role(field)
        entry = custom_fields.get(fid) or {}
        val = entry.get("value_kw")
        add_to_role_sum(sums, role, val)
        if isinstance(entry, dict):
            entry = dict(entry)
            entry["role"] = role
            custom_fields[fid] = entry
        if val is not None:
            details.append({"point": f"custom:{fid}", "role": role, "kw": float(val)})

    return sums, custom_fields, details


def compute_aktueller_verbrauch_kw(
    base_dir: str,
    battery_power_kw: Optional[float] = None,
) -> Optional[str]:
    flow = build_flow_response(
        base_dir, battery_soc_loader=None, battery_power_kw=battery_power_kw
    )
    v = flow.get("verbrauch_gesamt")
    return v if v and v != "n/a" else None


def build_flow_response(
    base_dir: str,
    battery_soc_loader: Optional[Callable[[], Optional[str]]] = None,
    battery_power_kw: Optional[float] = None,
) -> Dict[str, Any]:
    grid = _read_first(base_dir, ["energy_value.txt"]) or "n/a"

    hydro = _read_first(
        base_dir,
        ["wasserkraft_value.txt", "wasserkraftwerk_value.txt", "hydro_value.txt"],
    )
    pv = _read_first(base_dir, ["pv_value.txt", "pv_kw.txt", "solar_kw.txt"])
    gesamt = _read_first(base_dir, ["erzeugung_gesamt_value.txt"])
    haus = _read_first(base_dir, ["wohnhaus_value.txt", "haus_value.txt"])
    betrieb = _read_first(
        base_dir, ["betrieb_value.txt", "betrieb_anzeige_value.txt", "betrieb_verbrauch_value.txt"]
    )

    if hydro is None:
        hydro = _read_json_field(base_dir, "wasserkraft")
    if gesamt is None:
        gesamt = _read_json_field(base_dir, "erzeugung_gesamt")
    if pv is None:
        pv = _read_json_field(base_dir, "pv")
    if haus is None:
        haus = _read_json_field(base_dir, "wohnhaus")
    if betrieb is None:
        betrieb = _read_json_field(base_dir, "betrieb")

    pv_f = _to_float(pv if pv not in (None, "n/a") else None)
    hydro_f = _to_float(hydro if hydro not in (None, "n/a") else None)
    grid_f = _to_float(grid if grid not in (None, "n/a") else None)
    haus_f = _to_float(haus if haus not in (None, "n/a") else None)
    betrieb_f = _to_float(betrieb if betrieb not in (None, "n/a") else None)

    dash_cfg = _load_dash_cfg()
    role_sums, custom_fields, role_details = _collect_role_contributions(
        base_dir,
        pv_f=pv_f,
        hydro_f=hydro_f,
        grid_f=grid_f,
        haus_f=haus_f,
        betrieb_f=betrieb_f,
        battery_power_kw=battery_power_kw,
        dash_cfg=dash_cfg,
    )

    # Verbraucher-Summe ersetzt den früheren reinen Wohnhaus-Wert
    verbrauch_f = role_sums.get("verbraucher")
    has_verbraucher = any(d.get("role") == "verbraucher" for d in role_details)
    if not has_verbraucher:
        verbrauch_f = haus_f

    if verbrauch_f is None:
        verbrauch_gesamt = "n/a"
    else:
        verbrauch_gesamt = _format_kw(float(verbrauch_f))

    erzeugung_f = role_sums.get("erzeugung")
    has_erzeugung = any(d.get("role") == "erzeugung" for d in role_details)
    if has_erzeugung:
        erzeugung_gesamt = _format_kw(float(erzeugung_f or 0.0))
    else:
        erzeugung_gesamt = gesamt or "n/a"

    betrieb_anzeige = "n/a" if betrieb_f is None else (
        str(betrieb).strip() if betrieb not in (None, "", "n/a") else _format_kw(betrieb_f)
    )

    if verbrauch_gesamt != "n/a":
        try:
            from ram_cache import is_enabled, write_text

            if is_enabled():
                write_text(
                    base_dir, VERBRAUCH_GESAMT_FILE, verbrauch_gesamt, scope="energy_monitor"
                )
            else:
                raise OSError
        except Exception:
            path = os.path.join(_em_dir(base_dir), VERBRAUCH_GESAMT_FILE)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(verbrauch_gesamt)
            except Exception:
                pass

    soc = battery_soc_loader() if battery_soc_loader else None

    try:
        from flow_roles import normalize_flow_point_roles

        flow_point_roles = normalize_flow_point_roles(dash_cfg.get("flow_point_roles"))
    except Exception:
        flow_point_roles = {}

    return {
        "grid": grid,
        "hydro": hydro or "n/a",
        "pv": pv or "n/a",
        "haus": haus or "n/a",
        "verbrauch_gesamt": verbrauch_gesamt,
        "betrieb_anzeige": betrieb_anzeige,
        "erzeugung_gesamt": erzeugung_gesamt,
        "battery_soc": soc,
        "custom_fields": custom_fields,
        "role_sums": role_sums,
        "role_details": role_details,
        "flow_point_roles": flow_point_roles,
    }
