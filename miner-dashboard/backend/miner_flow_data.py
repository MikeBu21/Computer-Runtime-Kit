#!/usr/bin/env python3
"""
Stromfluss-Daten für Dashboard – liest energy_monitor/*.txt und enline_values.json.

Bei MINER_RAM_CACHE=1 kommen Live-Werte aus /dev/shm (wie ha_live_reader schreibt),
sonst von der SD. Sonst bleiben alte SD-Dateien sichtbar (z. B. Wohnhaus 1.789).
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional


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


def _total_verbrauch_kw(
    pv_f: Optional[float],
    hydro_f: Optional[float],
    grid_f: Optional[float],
    battery_power_kw: Optional[float] = None,
) -> Optional[float]:
    """Gesamtverbrauch = PV + Wasserkraft + Kelag/Netz − Batterie.

    Fehlende Wasserkraft zählt als 0 kW (Anlagen ohne Hydro), sonst bleibt
    verbrauch_gesamt/betrieb dauerhaft n/a und Last-Ist im Diagramm bei 0.
    """
    if grid_f is None or pv_f is None:
        return None
    hydro = 0.0 if hydro_f is None else float(hydro_f)
    total = float(pv_f) + hydro + float(grid_f)
    if battery_power_kw is not None:
        total -= float(battery_power_kw)
    return total


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

    if hydro is None:
        hydro = _read_json_field(base_dir, "wasserkraft")
    if gesamt is None:
        gesamt = _read_json_field(base_dir, "erzeugung_gesamt")
    if pv is None:
        pv = _read_json_field(base_dir, "pv")
    if haus is None:
        haus = _read_json_field(base_dir, "wohnhaus")

    pv_f = _to_float(pv if pv not in (None, "n/a") else None)
    hydro_f = _to_float(hydro if hydro not in (None, "n/a") else None)
    gesamt_f = _to_float(gesamt if gesamt not in (None, "n/a") else None)
    grid_f = _to_float(grid if grid not in (None, "n/a") else None)
    haus_f = _to_float(haus if haus not in (None, "n/a") else None)
    total = _total_verbrauch_kw(pv_f, hydro_f, grid_f, battery_power_kw)

    verbrauch_gesamt = _format_kw(total) if total is not None else "n/a"
    betrieb_net: Optional[float] = None
    if total is not None and haus_f is not None:
        betrieb_net = max(0.0, total - haus_f)
    betrieb_anzeige = _format_kw(betrieb_net) if betrieb_net is not None else "n/a"

    _write_kw_file(base_dir, VERBRAUCH_GESAMT_FILE, total)
    _write_kw_file(base_dir, BETRIEB_ANZEIGE_FILE, betrieb_net)

    soc = battery_soc_loader() if battery_soc_loader else None

    return {
        "grid": grid,
        "hydro": hydro or "n/a",
        "pv": pv or "n/a",
        "haus": haus or "n/a",
        "verbrauch_gesamt": verbrauch_gesamt,
        "betrieb_anzeige": betrieb_anzeige,
        "erzeugung_gesamt": gesamt or "n/a",
        "battery_soc": soc,
    }
