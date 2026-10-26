#!/usr/bin/env python3
"""
Enline-Tabelle -> Dateien für Dashboard (Stromfluss).
Liest alle Zeilen von cloud.enline.at/power.
Dashboard/Statistik-Feld „pv“ = Enline „06 Erzeugung Photovoltaik Gesamt“.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Page

# Legacy-Schlüssel für Dashboard / *.txt (Rückwärtskompatibilität)
LEGACY_PATTERNS: Dict[str, List[str]] = {
    "bezug": [r"00\s*bezug"],
    "lieferung": [r"01\s*lieferung"],
    "betriebsverbrauch": [r"02\s*betriebsverbrauch"],
    "wohnhaus": [r"03\s*wohnhaus", r"\bwohnhaus\b"],
    "erzeugung_gesamt": [r"04\s*erzeugung gesamt", r"^erzeugung gesamt$"],
    "wasserkraft": [r"05\s*erzeugung wasserkraft", r"erzeugung wasserkraft"],
    "pv": [
        r"06\s*erzeugung photovoltaik gesamt",
        r"^06\s+erzeugung photovoltaik gesamt",
        r"erzeugung photovoltaik gesamt",
    ],
    "eigenverbrauch": [r"07\s*eigenverbrauch"],
    "betrieb": [r"99\s*betrieb", r"betrieb.*kontrollmessung"],
    "kelag": [r"99\s*kelag", r"kelag.*kontrollmessung"],
}

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


def _out_dir() -> Path:
    return Path(__file__).resolve().parent


def parse_kw(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = str(text).replace("\xa0", " ").strip()
    m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(kW|kw|W|w)?", cleaned, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "kw").lower()
    if unit == "w":
        return round(val / 1000.0, 3)
    return val


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _slug_from_name(name: str) -> str:
    """Stabiler Schlüssel aus Anzeigenamen (z. B. '06.1 PV-ECO1' -> 'pv_eco1')."""
    n = (name or "").strip()
    n = re.sub(r"^[\d]+(?:\.\d+)?\s*", "", n)
    n = n.lower()
    for src, dst in (
        ("ä", "ae"),
        ("ö", "oe"),
        ("ü", "ue"),
        ("ß", "ss"),
    ):
        n = n.replace(src, dst)
    n = re.sub(r"[^a-z0-9]+", "_", n)
    return n.strip("_") or "unknown"


def _match_legacy_key(name: str) -> Optional[str]:
    n = _normalize_name(name)
    for key, patterns in LEGACY_PATTERNS.items():
        for pat in patterns:
            if not re.search(pat, n, re.I):
                continue
            if key == "erzeugung_gesamt" and (
                "wasserkraft" in n or "photovoltaik" in n
            ):
                continue
            if key == "pv" and re.search(r"06\.\d", n):
                continue
            if key == "wasserkraft" and "photovoltaik" in n:
                continue
            if key == "betrieb" and "kelag" in n:
                continue
            if key == "kelag" and "betrieb" in n and "kelag" not in n:
                continue
            return key
    return None


def apply_dashboard_aliases(values_kw: Dict[str, float]) -> None:
    """
    pv / pv_value.txt = Zeile 06 Photovoltaik Gesamt (niemals 04 Erzeugung Gesamt).
    """
    if values_kw.get("pv") is not None:
        return
    for key in ("erzeugung_photovoltaik_gesamt",):
        if key in values_kw:
            values_kw["pv"] = values_kw[key]
            return


def values_from_table_data(
    data: Dict[str, Any],
) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Alle Tabellenzeilen -> values_kw (Slug + Legacy-Aliase)."""
    values_kw: Dict[str, float] = {}
    rows_out: List[Dict[str, Any]] = []

    for row in data.get("rows", []):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        raw = f"{row.get('value', '')} {row.get('unit', '')}".strip()
        kw = parse_kw(raw)
        if kw is None:
            continue

        slug = _slug_from_name(name)
        values_kw[slug] = kw

        legacy = _match_legacy_key(name)
        if legacy:
            values_kw[legacy] = kw

        rows_out.append(
            {
                "name": name,
                "slug": slug,
                "legacy_key": legacy,
                "value_kw": kw,
                "unit": (row.get("unit") or "").strip(),
            }
        )

    apply_dashboard_aliases(values_kw)
    return values_kw, rows_out


def scrape_extra_values(page: Page) -> Dict[str, float]:
    from enline_reader import _read_table

    data = _read_table(page)
    values_kw, _ = values_from_table_data(data)
    return values_kw


def save_dashboard_values(page: Page, output_dir: Optional[Path] = None) -> Dict[str, float]:
    """Schreibt alle Enline-Werte nach enline_values.json + Legacy-*.txt."""
    from enline_reader import _read_table

    data = _read_table(page)
    values_kw, rows_detail = values_from_table_data(data)
    if not values_kw:
        return values_kw

    out = Path(output_dir or _out_dir())
    for key, fname in FILE_MAP.items():
        if key in values_kw:
            (out / fname).write_text(str(values_kw[key]), encoding="utf-8")

    json_path = out / "enline_values.json"
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows_detail),
        "values_kw": values_kw,
        "rows": rows_detail,
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return values_kw


# Alias
save_extra_values = save_dashboard_values
