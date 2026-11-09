#!/usr/bin/env python3
"""Rollen für Stromfluss-Punkte: Erzeugung, Netz, Speicher, Verbraucher."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

FLOW_ROLES: Tuple[str, ...] = ("erzeugung", "netz", "speicher", "verbraucher")

FLOW_ROLE_LABELS: Dict[str, str] = {
    "erzeugung": "Erzeugung",
    "netz": "Netz",
    "speicher": "Speicher",
    "verbraucher": "Verbraucher",
}

# Bestehende Hardcodes → Defaults, damit alte Configs ohne Aktion weiterlaufen
DEFAULT_BUILTIN_ROLES: Dict[str, str] = {
    "pv": "erzeugung",
    "wasserkraft": "erzeugung",
    "kelag": "netz",
    "battery": "speicher",
    "wohnhaus": "verbraucher",
    "betrieb": "verbraucher",
}

DEFAULT_CUSTOM_ROLE = "verbraucher"

# HA-/Modal-Feldschlüssel → Stromfluss-Punkt
FIELD_KEY_TO_POINT: Dict[str, str] = {
    "pv": "pv",
    "wasserkraft": "wasserkraft",
    "kelag": "kelag",
    "wohnhaus": "wohnhaus",
    "betrieb": "betrieb",
    "battery_soc": "battery",
    "battery_power": "battery",
}


def normalize_role(raw: Any, default: str = DEFAULT_CUSTOM_ROLE) -> str:
    role = str(raw or "").strip().lower()
    if role in FLOW_ROLES:
        return role
    # UI-/Legacy-Aliase
    aliases = {
        "produzent": "erzeugung",
        "generation": "erzeugung",
        "grid": "netz",
        "battery": "speicher",
        "storage": "speicher",
        "verbrauch": "verbraucher",
        "consumption": "verbraucher",
        "haus": "verbraucher",
    }
    return aliases.get(role, default if default in FLOW_ROLES else DEFAULT_CUSTOM_ROLE)


def normalize_flow_point_roles(raw: Any) -> Dict[str, str]:
    out = dict(DEFAULT_BUILTIN_ROLES)
    if not isinstance(raw, dict):
        return out
    for key, default in DEFAULT_BUILTIN_ROLES.items():
        if key in raw:
            out[key] = normalize_role(raw.get(key), default)
    return out


def point_key_for_field(field_key: str) -> Optional[str]:
    key = str(field_key or "").strip()
    if key.startswith("custom:"):
        return key
    return FIELD_KEY_TO_POINT.get(key)


def builtin_role(dash_cfg: Optional[Dict[str, Any]], point_key: str) -> str:
    roles = normalize_flow_point_roles((dash_cfg or {}).get("flow_point_roles"))
    return roles.get(point_key, DEFAULT_BUILTIN_ROLES.get(point_key, DEFAULT_CUSTOM_ROLE))


def custom_field_role(field: Dict[str, Any]) -> str:
    return normalize_role(field.get("role"), DEFAULT_CUSTOM_ROLE)


def role_for_field_key(dash_cfg: Optional[Dict[str, Any]], field_key: str) -> str:
    """Rolle für Modal-Feldschlüssel (pv, battery_soc, custom:… )."""
    key = str(field_key or "").strip()
    if key.startswith("custom:"):
        custom_id = key[7:]
        for field in (dash_cfg or {}).get("custom_flow_fields") or []:
            if field.get("id") == custom_id:
                return custom_field_role(field)
        return DEFAULT_CUSTOM_ROLE
    point = FIELD_KEY_TO_POINT.get(key, key)
    if point in DEFAULT_BUILTIN_ROLES:
        return builtin_role(dash_cfg, point)
    return DEFAULT_CUSTOM_ROLE


def empty_role_sums() -> Dict[str, float]:
    return {role: 0.0 for role in FLOW_ROLES}


def apply_sensor_sign(raw_value: Any, sign: Any = "+") -> Optional[float]:
    """Wendet das Modal-Vorzeichen (+/−) auf einen Sensorwert an.

    Minus bedeutet: Wert wird von der Rollensumme abgezogen (nicht addiert).
    Niemals Betrag/abs — Vorzeichen bleibt erhalten.
    """
    if raw_value is None:
        return None
    try:
        val = float(raw_value)
    except (TypeError, ValueError):
        return None
    s = str(sign or "+").strip()
    if s == "-":
        return -val
    return val


def add_to_role_sum(sums: Dict[str, float], role: str, value: Optional[float]) -> None:
    """Addiert einen bereits vorzeichenbehafteten Beitrag zur Rollensumme (kein abs)."""
    if value is None:
        return
    try:
        kw = float(value)
    except (TypeError, ValueError):
        return
    r = normalize_role(role)
    sums[r] = round(float(sums.get(r, 0.0)) + kw, 3)


def sum_signed_sensor_values(
    sensors: Iterable[Dict[str, Any]],
    value_by_entity: Dict[str, Optional[float]],
) -> Optional[float]:
    """Summe der Sensoren eines Punkts: jeder Sensor mit seinem +/−-Präfix.

    Beispiel: +Haus 2 kW und −External 0.005 kW → 1.995 kW für die Punkt-/Rollensumme.
    """
    total = 0.0
    has_any = False
    for item in sensors or []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        if not entity_id:
            continue
        signed = apply_sensor_sign(value_by_entity.get(entity_id), item.get("sign") or "+")
        if signed is None:
            continue
        has_any = True
        total += signed
    if not has_any:
        return None
    return round(total, 3)


def sum_values_by_role(
    contributions: Iterable[Tuple[str, Optional[float]]],
) -> Dict[str, float]:
    """contributions: Iterable von (role, signed_kw) — Vorzeichen bleiben erhalten."""
    sums = empty_role_sums()
    for role, value in contributions:
        add_to_role_sum(sums, role, value)
    return sums


def role_options_for_ui() -> List[Dict[str, str]]:
    return [{"value": role, "label": FLOW_ROLE_LABELS[role]} for role in FLOW_ROLES]
