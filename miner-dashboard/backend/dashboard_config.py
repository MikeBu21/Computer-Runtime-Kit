#!/usr/bin/env python3
"""Dashboard-Layout: eigene Stromfluss-Felder, Kreis- und Liniendiagramm-Konfiguration."""
from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
CONFIG_PATHS = (
    BACKEND_DIR.parent.parent / "config" / "dashboard_config.json",
    BACKEND_DIR / "dashboard_config.json",
)

SOURCE_OPTIONS: List[Dict[str, str]] = [
    {"group": "Stromfluss-Felder", "value": "field.pv", "label": "PV Gesamt"},
    {"group": "Stromfluss-Felder", "value": "field.wasserkraft", "label": "Wasserkraft"},
    {"group": "Stromfluss-Felder", "value": "field.kelag", "label": "Netz / Kelag"},
    {"group": "Stromfluss-Felder", "value": "field.wohnhaus", "label": "Wohnhaus"},
    {"group": "Stromfluss-Felder", "value": "field.betrieb", "label": "Betrieb"},
    {"group": "Stromfluss-Felder", "value": "field.bezug", "label": "Bezug"},
    {"group": "Stromfluss-Felder", "value": "field.lieferung", "label": "Lieferung / Einspeisung"},
    {"group": "Stromfluss-Felder", "value": "field.erzeugung_gesamt", "label": "Erzeugung Gesamt"},
    {"group": "Stromfluss-Felder", "value": "field.eigenverbrauch", "label": "Eigenverbrauch"},
    {"group": "Berechnet", "value": "flow.verbrauch_gesamt", "label": "Verbrauch gesamt"},
    {"group": "Berechnet", "value": "flow.betrieb_anzeige", "label": "Betrieb (Anzeige)"},
    {"group": "Berechnet", "value": "computed.erzeugung", "label": "Erzeugung (PV + Wasser)"},
    {"group": "Berechnet", "value": "computed.grid_bezug", "label": "Netzbezug (+)"},
    {"group": "Berechnet", "value": "computed.grid_verkauf", "label": "Einspeisung / Verkauf"},
    {"group": "Batterie", "value": "flow.battery_soc", "label": "Batterie SOC (%)"},
    {"group": "Batterie", "value": "flow.battery_power", "label": "Batterie Leistung (kW)"},
    {"group": "Batterie", "value": "computed.battery_ladung", "label": "Batterie Laden (kW)"},
    {"group": "Batterie", "value": "computed.battery_entladung", "label": "Batterie Entladen (kW)"},
]

FLOW_ICONS = ["⚡", "☀️", "💧", "🔌", "🔋", "🏠", "🏭", "🔥", "❄️", "🌬️", "📊", "🔆", "🌡️", "💡", "🔧"]
MAX_FLOW_SLOTS = 9
# Freie Slots für eigene Felder (Built-ins belegen 0,1,2,5,6,7)
DEFAULT_CUSTOM_SLOTS = (3, 4, 8)
BUILTIN_FLOW_KEYS = ("pv", "wasserkraft", "kelag", "battery", "wohnhaus", "betrieb")
BUILTIN_FLOW_SLOTS: Dict[str, int] = {
    "pv": 0,
    "wasserkraft": 1,
    "kelag": 2,
    "battery": 5,
    "wohnhaus": 6,
    "betrieb": 7,
}
# Anzeigenamen. Der Punkt-Schlüssel bleibt stabil, der Name ist umbenennbar.
DEFAULT_BUILTIN_LABELS: Dict[str, str] = {
    "pv": "PV-Platten",
    "wasserkraft": "Wasserkraftwerk",
    "kelag": "Netz",
    "battery": "Batterie",
    "wohnhaus": "Wohnhaus",
    "betrieb": "Betrieb",
}
_CONSUMER_COLORS = ("#9ccc65", "#ef5350", "#ffb74d", "#26c6da", "#ce93d8", "#ff8a65", "#b39ddb", "#80cbc4")
_TOTAL_LINE_LABELS = {
    "Aktueller Verbrauch",
    "Aktueller Verbrauch (Gesamt)",
    "Wohnhaus",
    "Verbrauch",
    "Verbrauch gesamt",
    "",
}


def _default_pie_generation() -> List[Dict[str, Any]]:
    return [
        {"id": "gen_pv", "label": "PV-Platten", "source": "field.pv", "color": "#ffc107", "enabled": True},
        {"id": "gen_hydro", "label": "Wasserkraft", "source": "field.wasserkraft", "color": "#42a5f5", "enabled": True},
        {"id": "gen_ladung", "label": "Ladung Batterie", "source": "computed.battery_ladung", "color": "#66bb6a", "enabled": True},
        {"id": "gen_entladung", "label": "Entladung", "source": "computed.battery_entladung", "color": "#ff7043", "enabled": True},
    ]


def _default_pie_consumption() -> List[Dict[str, Any]]:
    return [
        {"id": "con_betrieb", "label": "Betrieb", "source": "flow.betrieb_anzeige", "color": "#ef5350", "enabled": True},
        {"id": "con_haus", "label": "Wohnhaus", "source": "field.wohnhaus", "color": "#9ccc65", "enabled": True},
        {"id": "con_netz", "label": "Netz", "source": "computed.grid_bezug", "color": "#8b9cb3", "enabled": True},
        {"id": "con_verkauf", "label": "Verkauf", "source": "computed.grid_verkauf", "color": "#26c6da", "enabled": True},
    ]


def _default_line_series() -> List[Dict[str, Any]]:
    return [
        {
            "id": "line_erzeugung",
            "label": "Erzeugung",
            "source": "computed.erzeugung",
            "color": "#00ff41",
            "fill": True,
            "y_axis": "kw",
            "enabled": True,
        },
        {
            "id": "line_verbrauch",
            "label": "Aktueller Verbrauch",
            "source": "flow.verbrauch_gesamt",
            "color": "#ffea00",
            "fill": False,
            "y_axis": "kw",
            "enabled": True,
        },
        {
            "id": "line_netz",
            "label": "Netz",
            "source": "field.kelag",
            "color": "#ce93d8",
            "fill": True,
            "y_axis": "kw",
            "enabled": True,
        },
        {
            "id": "line_soc",
            "label": "Batterie %",
            "source": "flow.battery_soc",
            "color": "#42a5f5",
            "fill": False,
            "y_axis": "percent",
            "enabled": True,
        },
    ]


def default_config() -> Dict[str, Any]:
    from flow_roles import DEFAULT_BUILTIN_ROLES

    return {
        "updated_at": None,
        "flow_hidden_builtins": [],
        "flow_builtin_slots": {},
        "flow_builtin_labels": {},
        "flow_point_roles": dict(DEFAULT_BUILTIN_ROLES),
        "custom_flow_fields": [],
        "pie_generation": _default_pie_generation(),
        "pie_consumption": _default_pie_consumption(),
        "line_series": _default_line_series(),
    }


def _normalize_hidden_builtins(keys: Any) -> List[str]:
    if not isinstance(keys, list):
        return []
    out: List[str] = []
    for key in keys:
        k = str(key or "").strip()
        if k in BUILTIN_FLOW_KEYS and k not in out:
            out.append(k)
    return out


def _normalize_builtin_slots(raw: Any) -> Dict[str, int]:
    """Nur visuelle Slot-Positionen der Built-in-Knoten (keine Rollen/Sensoren)."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    used: set = set()
    for key, val in raw.items():
        k = str(key or "").strip()
        if k not in BUILTIN_FLOW_KEYS:
            continue
        try:
            slot = int(val)
        except (TypeError, ValueError):
            continue
        if not (0 <= slot < MAX_FLOW_SLOTS) or slot in used:
            continue
        out[k] = slot
        used.add(slot)
    return out


def effective_builtin_slots(cfg: Dict[str, Any]) -> Dict[str, int]:
    """Effektive Grid-Slots sichtbarer Built-ins (Overrides + Defaults, ohne Kollision)."""
    hidden = hidden_builtins(cfg)
    overrides = _normalize_builtin_slots(cfg.get("flow_builtin_slots"))
    result: Dict[str, int] = {}
    used: set = set()
    for key in BUILTIN_FLOW_KEYS:
        if key in hidden:
            continue
        if key in overrides and overrides[key] not in used:
            result[key] = overrides[key]
            used.add(overrides[key])
    for key in BUILTIN_FLOW_KEYS:
        if key in hidden or key in result:
            continue
        default = BUILTIN_FLOW_SLOTS[key]
        if default not in used:
            result[key] = default
            used.add(default)
            continue
        for slot in range(MAX_FLOW_SLOTS):
            if slot not in used:
                result[key] = slot
                used.add(slot)
                break
    return result


def config_path() -> Path:
    for path in CONFIG_PATHS:
        if path.is_file():
            return path
    return CONFIG_PATHS[0]


def _normalize_custom_field(entry: Dict[str, Any]) -> Dict[str, Any]:
    field_id = str(entry.get("id") or "").strip() or str(uuid.uuid4())[:8]
    sensors_in = entry.get("sensors") if isinstance(entry.get("sensors"), list) else []
    sensors: List[Dict[str, str]] = []
    for item in sensors_in:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        if not entity_id:
            continue
        sign = str(item.get("sign") or "+").strip()
        if sign not in ("+", "-"):
            sign = "+"
        sensors.append({"entity_id": entity_id, "sign": sign})
    icon = str(entry.get("icon") or "⚡").strip() or "⚡"
    slot_raw = entry.get("slot")
    slot = int(slot_raw) if slot_raw is not None and str(slot_raw).isdigit() else None
    if slot is not None and not (0 <= slot < MAX_FLOW_SLOTS):
        slot = None
    from flow_roles import DEFAULT_CUSTOM_ROLE, normalize_role

    return {
        "id": field_id,
        "name": str(entry.get("name") or "Neues Feld").strip() or "Neues Feld",
        "icon": icon if icon in FLOW_ICONS else "⚡",
        "enabled": bool(entry.get("enabled", True)),
        "sensors": sensors,
        "slot": slot,
        "role": normalize_role(entry.get("role"), DEFAULT_CUSTOM_ROLE),
    }


def hidden_builtins(cfg: Dict[str, Any]) -> set:
    return set(_normalize_hidden_builtins(cfg.get("flow_hidden_builtins")))


def visible_builtin_count(cfg: Dict[str, Any]) -> int:
    return len(BUILTIN_FLOW_KEYS) - len(hidden_builtins(cfg))


def used_flow_slots(cfg: Dict[str, Any]) -> set:
    used = set(effective_builtin_slots(cfg).values())
    for field in cfg.get("custom_flow_fields") or []:
        if field.get("enabled", True) and field.get("slot") is not None:
            used.add(int(field["slot"]))
    return used


def next_free_flow_slot(cfg: Dict[str, Any]) -> Optional[int]:
    used = used_flow_slots(cfg)
    for preferred in DEFAULT_CUSTOM_SLOTS:
        if preferred not in used:
            return preferred
    for slot in range(MAX_FLOW_SLOTS):
        if slot not in used:
            return slot
    return None


def count_enabled_custom_fields(cfg: Dict[str, Any]) -> int:
    return sum(1 for f in (cfg.get("custom_flow_fields") or []) if f.get("enabled", True))


def count_visible_flow_nodes(cfg: Dict[str, Any]) -> int:
    return visible_builtin_count(cfg) + count_enabled_custom_fields(cfg)


def can_add_custom_field(cfg: Dict[str, Any]) -> bool:
    return count_visible_flow_nodes(cfg) < MAX_FLOW_SLOTS


def count_enabled_flow_nodes(cfg: Dict[str, Any]) -> int:
    return count_visible_flow_nodes(cfg)


def assign_slots(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Vergibt fehlende Slot-Nummern für eigene Felder."""
    out = deepcopy(cfg)
    fields = out.get("custom_flow_fields") or []
    used = used_flow_slots(out)
    for field in fields:
        if not field.get("enabled", True):
            continue
        if field.get("slot") is not None:
            used.add(int(field["slot"]))
            continue
        for preferred in list(DEFAULT_CUSTOM_SLOTS) + list(range(MAX_FLOW_SLOTS)):
            if preferred not in used:
                field["slot"] = preferred
                used.add(preferred)
                break
    out["custom_flow_fields"] = fields
    return out


def _normalize_pie_slice(entry: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    slice_id = str(entry.get("id") or "").strip() or f"{prefix}_{uuid.uuid4().hex[:6]}"
    out = {
        "id": slice_id,
        "label": str(entry.get("label") or "Segment").strip() or "Segment",
        "source": str(entry.get("source") or "field.pv").strip(),
        "color": str(entry.get("color") or "#ffc107").strip(),
        "enabled": bool(entry.get("enabled", True)),
    }
    managed = str(entry.get("managed") or "").strip()
    if managed in ("consumer", "verbrauch_gesamt"):
        out["managed"] = managed
    return out


def _normalize_line_series(entry: Dict[str, Any]) -> Dict[str, Any]:
    series_id = str(entry.get("id") or "").strip() or f"line_{uuid.uuid4().hex[:6]}"
    y_axis = str(entry.get("y_axis") or "kw").strip().lower()
    if y_axis not in ("kw", "percent"):
        y_axis = "kw"
    source = str(entry.get("source") or "field.pv").strip()
    if y_axis == "percent" and source not in ("flow.battery_soc", "field.battery_soc"):
        source = "flow.battery_soc"
    out = {
        "id": series_id,
        "label": str(entry.get("label") or "Linie").strip() or "Linie",
        "source": source,
        "color": str(entry.get("color") or "#00ff41").strip(),
        "fill": bool(entry.get("fill")),
        "y_axis": y_axis,
        "enabled": bool(entry.get("enabled", True)),
    }
    managed = str(entry.get("managed") or "").strip()
    if managed in ("consumer", "verbrauch_gesamt"):
        out["managed"] = managed
    return out


def _normalize_builtin_labels(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key in BUILTIN_FLOW_KEYS:
        if key not in raw:
            continue
        name = str(raw.get(key) or "").strip()
        if name:
            out[key] = name[:80]
    return out


def builtin_label(cfg: Optional[Dict[str, Any]], key: str) -> str:
    """Anzeigename eines Stromfluss-Felds. Ohne Umbenennung bleibt der Standardname."""
    labels = (cfg or {}).get("flow_builtin_labels") or {}
    name = ""
    if isinstance(labels, dict):
        name = str(labels.get(key) or "").strip()
    if name:
        return name
    return DEFAULT_BUILTIN_LABELS.get(key, key)


def consumer_points(cfg: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Sichtbare Felder mit Rolle Verbraucher, jedes mit dem vom Nutzer gesetzten Namen."""
    from flow_roles import builtin_role, custom_field_role

    cfg = cfg or {}
    hidden = hidden_builtins(cfg)
    points: List[Dict[str, str]] = []
    for key in BUILTIN_FLOW_KEYS:
        if key in hidden:
            continue
        if builtin_role(cfg, key) != "verbraucher":
            continue
        source = "flow.battery_power" if key == "battery" else f"field.{key}"
        points.append({"point": key, "source": source, "label": builtin_label(cfg, key)})
    for field in cfg.get("custom_flow_fields") or []:
        if not isinstance(field, dict) or not field.get("enabled", True):
            continue
        if custom_field_role(field) != "verbraucher":
            continue
        fid = str(field.get("id") or "").strip()
        if not fid:
            continue
        name = str(field.get("name") or fid).strip() or fid
        points.append({"point": f"custom:{fid}", "source": f"custom.{fid}", "label": name})
    return points


def _consumer_point_kw(flow: Dict[str, Any], point: str) -> float:
    details = flow.get("role_details") if isinstance(flow.get("role_details"), list) else []
    for item in details:
        if not isinstance(item, dict):
            continue
        if item.get("point") == point and item.get("role") == "verbraucher":
            try:
                return float(item.get("kw") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    if point.startswith("custom:"):
        fid = point.split(":", 1)[1]
        custom = flow.get("custom_fields") if isinstance(flow.get("custom_fields"), dict) else {}
        entry = custom.get(fid) if isinstance(custom.get(fid), dict) else {}
        try:
            val = entry.get("value_kw")
            return float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    if point == "wohnhaus":
        raw = flow.get("haus")
        if raw not in (None, "", "n/a"):
            try:
                return float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def build_consumer_statistics(
    cfg: Optional[Dict[str, Any]], flow: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Verbrauch gesamt plus jedes Verbraucherfeld. Die Einzelwerte ergeben die Summe."""
    flow = flow or {}
    items: List[Dict[str, Any]] = []
    for point in consumer_points(cfg):
        items.append(
            {
                "point": point["point"],
                "label": point["label"],
                "source": point["source"],
                "kw": round(_consumer_point_kw(flow, point["point"]), 3),
            }
        )
    total_kw = round(sum(float(item["kw"]) for item in items), 3)
    return {
        "total": {
            "label": "Verbrauch gesamt",
            "source": "flow.verbrauch_gesamt",
            "kw": total_kw,
        },
        "items": items,
    }


def _stat_slug(prefix: str, point: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(point or "").lower()).strip("_") or "feld"
    return f"{prefix}_{slug}"[:48]


_LEGACY_CONSUMER_SLICE_IDS = {
    "wohnhaus": ("con_haus", "con_wohnhaus"),
    "betrieb": ("con_betrieb",),
}


def _fresh_stat_id(rows: List[Dict[str, Any]], preferred: str) -> str:
    used = {str(row.get("id") or "") for row in rows}
    if preferred not in used:
        return preferred
    n = 2
    while f"{preferred}_{n}" in used:
        n += 1
    return f"{preferred}_{n}"


def sync_consumer_statistics(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Statistik führt die Verbrauchersumme und jedes Verbraucherfeld unter seinem Namen."""
    points = consumer_points(cfg)
    sources = {p["source"] for p in points}
    pie = [dict(sl) for sl in (cfg.get("pie_consumption") or []) if isinstance(sl, dict)]

    def find_consumer_slice(point: Dict[str, str]) -> Optional[Dict[str, Any]]:
        for sl in pie:
            if sl.get("managed") == "verbrauch_gesamt":
                continue
            if sl.get("source") == point["source"]:
                return sl
        legacy_ids = _LEGACY_CONSUMER_SLICE_IDS.get(point["point"], ())
        aliases = {
            point["source"],
            f"field.{point['point']}",
            f"flow.{point['point']}",
            "flow.betrieb_anzeige" if point["point"] == "betrieb" else "",
        }
        for sl in pie:
            if sl.get("managed") == "verbrauch_gesamt":
                continue
            if sl.get("id") in legacy_ids and sl.get("source") in aliases:
                return sl
        return None

    for idx, point in enumerate(points):
        match = find_consumer_slice(point)
        if match:
            match["label"] = point["label"]
            match["source"] = point["source"]
            match["enabled"] = True
            match["managed"] = "consumer"
            continue
        pie.append(
            {
                "id": _fresh_stat_id(pie, _stat_slug("con", point["point"])),
                "label": point["label"],
                "source": point["source"],
                "color": _CONSUMER_COLORS[idx % len(_CONSUMER_COLORS)],
                "enabled": True,
                "managed": "consumer",
            }
        )

    for sl in pie:
        if sl.get("managed") == "consumer" and sl.get("source") not in sources:
            sl["enabled"] = False

    total = next(
        (
            sl
            for sl in pie
            if sl.get("managed") == "verbrauch_gesamt" or sl.get("id") == "con_verbrauch_gesamt"
        ),
        None,
    )
    if total is None:
        pie.insert(
            0,
            {
                "id": "con_verbrauch_gesamt",
                "label": "Verbrauch gesamt",
                "source": "flow.verbrauch_gesamt",
                "color": "#ffea00",
                "enabled": True,
                "managed": "verbrauch_gesamt",
            },
        )
    else:
        total["label"] = "Verbrauch gesamt"
        total["source"] = "flow.verbrauch_gesamt"
        total["enabled"] = True
        total["managed"] = "verbrauch_gesamt"
        total.setdefault("color", "#ffea00")
    cfg["pie_consumption"] = pie

    lines = [dict(ser) for ser in (cfg.get("line_series") or []) if isinstance(ser, dict)]
    for ser in lines:
        if ser.get("id") == "line_verbrauch" and ser.get("source") in (
            "field.wohnhaus",
            "flow.verbrauch_gesamt",
        ):
            ser["source"] = "flow.verbrauch_gesamt"
            if str(ser.get("label") or "") in _TOTAL_LINE_LABELS:
                ser["label"] = "Verbrauch gesamt"
            ser["managed"] = "verbrauch_gesamt"
            ser["enabled"] = True
            ser["y_axis"] = "kw"
    if not any(
        ser.get("source") == "flow.verbrauch_gesamt" and ser.get("enabled", True) for ser in lines
    ):
        lines.append(
            {
                "id": "line_verbrauch",
                "label": "Verbrauch gesamt",
                "source": "flow.verbrauch_gesamt",
                "color": "#ffea00",
                "fill": False,
                "y_axis": "kw",
                "enabled": True,
                "managed": "verbrauch_gesamt",
            }
        )
    for idx, point in enumerate(points):
        match = next(
            (
                ser
                for ser in lines
                if ser.get("managed") != "verbrauch_gesamt" and ser.get("source") == point["source"]
            ),
            None,
        )
        if match:
            match["label"] = point["label"]
            match["source"] = point["source"]
            match["enabled"] = True
            match["managed"] = "consumer"
            match["y_axis"] = "kw"
            continue
        lines.append(
            {
                "id": _fresh_stat_id(lines, _stat_slug("line", point["point"])),
                "label": point["label"],
                "source": point["source"],
                "color": _CONSUMER_COLORS[idx % len(_CONSUMER_COLORS)],
                "fill": False,
                "y_axis": "kw",
                "enabled": True,
                "managed": "consumer",
            }
        )
    for ser in lines:
        if ser.get("managed") == "consumer" and ser.get("source") not in sources:
            ser["enabled"] = False
    cfg["line_series"] = lines
    return cfg


def normalize_config(data: Dict[str, Any]) -> Dict[str, Any]:
    from flow_roles import normalize_flow_point_roles

    out = default_config()
    out["updated_at"] = data.get("updated_at")
    out["flow_hidden_builtins"] = _normalize_hidden_builtins(data.get("flow_hidden_builtins"))
    out["flow_builtin_slots"] = _normalize_builtin_slots(data.get("flow_builtin_slots"))
    out["flow_builtin_labels"] = _normalize_builtin_labels(data.get("flow_builtin_labels"))
    out["flow_point_roles"] = normalize_flow_point_roles(data.get("flow_point_roles"))
    custom = data.get("custom_flow_fields")
    if isinstance(custom, list):
        out["custom_flow_fields"] = [_normalize_custom_field(x) for x in custom if isinstance(x, dict)]
    pie_gen = data.get("pie_generation")
    if isinstance(pie_gen, list):
        out["pie_generation"] = [_normalize_pie_slice(x, "gen") for x in pie_gen if isinstance(x, dict)]
    pie_con = data.get("pie_consumption")
    if isinstance(pie_con, list):
        out["pie_consumption"] = [_normalize_pie_slice(x, "con") for x in pie_con if isinstance(x, dict)]
    line = data.get("line_series")
    if isinstance(line, list):
        out["line_series"] = [_normalize_line_series(x) for x in line if isinstance(x, dict)]
    return sync_consumer_statistics(out)


def load_config() -> Dict[str, Any]:
    path = config_path()
    cfg = default_config()
    if not path.is_file():
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return normalize_config(data)
    except Exception:
        pass
    return cfg


def save_config(cfg: Dict[str, Any]) -> Path:
    path = CONFIG_PATHS[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    out = sanitize_chart_sources(assign_slots(normalize_config(deepcopy(cfg))))
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def parse_json_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    from flow_roles import normalize_flow_point_roles

    cfg = load_config()
    if "flow_hidden_builtins" in payload:
        cfg["flow_hidden_builtins"] = _normalize_hidden_builtins(payload.get("flow_hidden_builtins"))
    if "flow_builtin_slots" in payload:
        cfg["flow_builtin_slots"] = _normalize_builtin_slots(payload.get("flow_builtin_slots"))
    if "flow_builtin_labels" in payload:
        cfg["flow_builtin_labels"] = _normalize_builtin_labels(payload.get("flow_builtin_labels"))
    if "flow_point_roles" in payload:
        cfg["flow_point_roles"] = normalize_flow_point_roles(payload.get("flow_point_roles"))
    if "custom_flow_fields" in payload:
        fields = payload.get("custom_flow_fields")
        if isinstance(fields, list):
            cfg["custom_flow_fields"] = [_normalize_custom_field(x) for x in fields if isinstance(x, dict)]
    if "pie_generation" in payload:
        items = payload.get("pie_generation")
        if isinstance(items, list):
            cfg["pie_generation"] = [_normalize_pie_slice(x, "gen") for x in items if isinstance(x, dict)]
    if "pie_consumption" in payload:
        items = payload.get("pie_consumption")
        if isinstance(items, list):
            cfg["pie_consumption"] = [_normalize_pie_slice(x, "con") for x in items if isinstance(x, dict)]
    if "line_series" in payload:
        items = payload.get("line_series")
        if isinstance(items, list):
            cfg["line_series"] = [_normalize_line_series(x) for x in items if isinstance(x, dict)]
    return cfg


_SOURCE_LABELS: Dict[str, str] = {opt["value"]: opt["label"] for opt in SOURCE_OPTIONS}


def _source_opt(group: str, value: str, label: Optional[str] = None) -> Dict[str, str]:
    return {
        "group": group,
        "value": value,
        "label": label or _SOURCE_LABELS.get(value, value),
    }


def source_options_for_flow(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Nur Datenquellen, die im aktuellen Stromfluss-Diagramm existieren."""
    cfg = cfg or load_config()
    hidden = hidden_builtins(cfg)
    options: List[Dict[str, str]] = []

    for key in ("pv", "wasserkraft", "kelag", "wohnhaus", "betrieb"):
        if key not in hidden:
            options.append(_source_opt("Stromfluss-Felder", f"field.{key}", builtin_label(cfg, key)))

    for field in cfg.get("custom_flow_fields") or []:
        if not field.get("enabled", True):
            continue
        options.append(
            _source_opt(
                "Stromfluss-Felder",
                f"custom.{field['id']}",
                field.get("name") or field["id"],
            )
        )

    if "pv" not in hidden or "wasserkraft" not in hidden:
        if not ("pv" in hidden and "wasserkraft" in hidden):
            options.append(_source_opt("Berechnet", "computed.erzeugung"))

    if "kelag" not in hidden:
        options.append(_source_opt("Berechnet", "computed.grid_bezug"))
        options.append(_source_opt("Berechnet", "computed.grid_verkauf"))

    if "betrieb" not in hidden:
        options.append(_source_opt("Berechnet", "flow.betrieb_anzeige"))

    if count_visible_flow_nodes(cfg) > 0:
        options.append(_source_opt("Berechnet", "flow.verbrauch_gesamt"))

    if "battery" not in hidden:
        for value in (
            "flow.battery_soc",
            "flow.battery_power",
            "computed.battery_ladung",
            "computed.battery_entladung",
        ):
            options.append(_source_opt("Batterie", value))

    return options


def valid_source_values(cfg: Optional[Dict[str, Any]] = None) -> set:
    return {opt["value"] for opt in source_options_for_flow(cfg)}


def sanitize_chart_sources(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Deaktiviert Chart-Zeilen deren Quelle im Stromfluss nicht mehr existiert."""
    valid = valid_source_values(cfg)
    for key in ("pie_generation", "pie_consumption", "line_series"):
        for item in cfg.get(key) or []:
            src = str(item.get("source") or "").strip()
            if src and src not in valid:
                # Wohnhaus-Fallback wenn sichtbar
                if key == "pie_consumption" and "wohnhaus" not in hidden_builtins(cfg):
                    if src in ("field.eigenverbrauch", "field.bezug", "field.lieferung"):
                        item["source"] = "field.wohnhaus"
                        item["enabled"] = True
                        continue
                item["enabled"] = False
    return cfg


def source_options_with_custom(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    return source_options_for_flow(cfg)


DEFAULT_AUTOMATION_KW_SOURCE = "field.kelag"
# SOC-% hat in der kW-Automatik nichts zu suchen
_AUTOMATION_KW_EXCLUDE = frozenset({"flow.battery_soc"})


def automation_all_source_options(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Alle Quellen für flexible Automatik-Regeln (kW, %, berechnet)."""
    return source_options_for_flow(cfg)


def valid_automation_all_sources(cfg: Optional[Dict[str, Any]] = None) -> set:
    return {opt["value"] for opt in automation_all_source_options(cfg)}


def normalize_automation_source(
    source: Optional[str], cfg: Optional[Dict[str, Any]] = None
) -> str:
    src = str(source or DEFAULT_AUTOMATION_KW_SOURCE).strip() or DEFAULT_AUTOMATION_KW_SOURCE
    valid = valid_automation_all_sources(cfg)
    if src in valid:
        return src
    if DEFAULT_AUTOMATION_KW_SOURCE in valid:
        return DEFAULT_AUTOMATION_KW_SOURCE
    return next(iter(valid), DEFAULT_AUTOMATION_KW_SOURCE)


def automation_kw_source_options(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """kW-Quellen für Automatikmodus — gleicher Pool wie Kreisdiagramm (ohne SOC %)."""
    return [
        opt
        for opt in source_options_for_flow(cfg)
        if opt["value"] not in _AUTOMATION_KW_EXCLUDE
    ]


def valid_automation_kw_sources(cfg: Optional[Dict[str, Any]] = None) -> set:
    return {opt["value"] for opt in automation_kw_source_options(cfg)}


def source_label(source: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    for opt in source_options_for_flow(cfg):
        if opt["value"] == source:
            return opt["label"]
    return _SOURCE_LABELS.get(source, source)


def normalize_automation_kw_source(
    source: Optional[str], cfg: Optional[Dict[str, Any]] = None
) -> str:
    src = str(source or DEFAULT_AUTOMATION_KW_SOURCE).strip() or DEFAULT_AUTOMATION_KW_SOURCE
    valid = valid_automation_kw_sources(cfg)
    if src in valid:
        return src
    if DEFAULT_AUTOMATION_KW_SOURCE in valid:
        return DEFAULT_AUTOMATION_KW_SOURCE
    return next(iter(valid), DEFAULT_AUTOMATION_KW_SOURCE)


DEFAULT_AUTOMATION_SOC_SOURCE = "flow.battery_soc"


def automation_soc_source_options(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """SOC-% Quellen für Batterie-Automatik (Stromfluss / HA-Mapping)."""
    from ha_entity_mapping import FIELD_DEFINITIONS, field_is_enabled, load_config as load_ha_config

    cfg = cfg or load_config()
    hidden = hidden_builtins(cfg)
    ha_cfg = load_ha_config()
    options: List[Dict[str, str]] = []

    if "battery" not in hidden:
        options.append(_source_opt("Batterie", "flow.battery_soc"))

    for key, meta in FIELD_DEFINITIONS.items():
        if meta.get("value_type") != "percent":
            continue
        if not field_is_enabled(ha_cfg, key):
            continue
        options.append(_source_opt("Stromfluss-Felder", f"field.{key}", meta.get("label")))

    if not options:
        options.append(_source_opt("Batterie", "flow.battery_soc"))
    return options


def valid_automation_soc_sources(cfg: Optional[Dict[str, Any]] = None) -> set:
    return {opt["value"] for opt in automation_soc_source_options(cfg)}


def normalize_automation_soc_source(
    source: Optional[str], cfg: Optional[Dict[str, Any]] = None
) -> str:
    src = str(source or DEFAULT_AUTOMATION_SOC_SOURCE).strip() or DEFAULT_AUTOMATION_SOC_SOURCE
    valid = valid_automation_soc_sources(cfg)
    if src in valid:
        return src
    if DEFAULT_AUTOMATION_SOC_SOURCE in valid:
        return DEFAULT_AUTOMATION_SOC_SOURCE
    return next(iter(valid), DEFAULT_AUTOMATION_SOC_SOURCE)


def enabled_pie_slices(cfg: Dict[str, Any], chart: str) -> List[Dict[str, Any]]:
    key = "pie_generation" if chart == "generation" else "pie_consumption"
    return [s for s in (cfg.get(key) or []) if s.get("enabled")]


def enabled_line_series(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in (cfg.get("line_series") or []) if s.get("enabled")]


def new_custom_field_id(name: str = "Neues Feld") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:20]
    return slug or str(uuid.uuid4())[:8]
