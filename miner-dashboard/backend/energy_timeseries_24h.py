#!/usr/bin/env python3
"""24h-Zeitreihe für Liniendiagramm – 1 Messpunkt pro Minute, Reset täglich."""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from miner_flow_data import build_flow_response

from energy_stats_24h import (
    _battery_entladung_kw,
    _battery_ladung_kw,
    _flow_battery_power_kw,
    _parse_kw,
    _parse_kw_signed,
)

_lock = threading.Lock()
SAMPLE_INTERVAL_SEC = 60
MINUTES_PER_DAY = 24 * 60
MAX_SAMPLES = MINUTES_PER_DAY + 5
_last_record_monotonic = 0.0


def _path(base_dir: str) -> Path:
    try:
        from ram_cache import timeseries_path

        return timeseries_path(base_dir, "energy_timeseries_24h.json")
    except Exception:
        return Path(base_dir) / "energy_monitor" / "energy_timeseries_24h.json"


def load_timeseries_state(base_dir: str) -> Dict[str, Any]:
    """
    Roh-Zustand der 24h-Zeitreihe für Steuerungsalgorithmus / Verbrauchshistorie.

    Liefert {"date": "YYYY-MM-DD", "samples": [...]} ohne Tages-Reset zu erzwingen,
    damit Aufrufer (Archivierung, Ist-Verbrauch) Datum selbst prüfen können.
    """
    path = _path(base_dir)
    if not path.is_file():
        return {"date": "", "samples": []}
    try:
        with _lock:
            state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return {"date": "", "samples": []}
        samples = state.get("samples") or []
        if not isinstance(samples, list):
            samples = []
        return {
            "date": str(state.get("date") or ""),
            "samples": samples,
        }
    except Exception:
        return {"date": "", "samples": []}


def _empty_state(today: str) -> Dict[str, Any]:
    return {"date": today, "samples": []}


def _minutes_since_midnight(now: datetime) -> float:
    return now.hour * 60.0 + now.minute + now.second / 60.0


def _minute_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _enline_kw_from_files(base_dir: str, json_key: str, txt_names: list) -> float:
    """Direkt aus Enline-*.txt / JSON (ohne berechnete flow-Felder)."""
    try:
        from ram_cache import is_enabled, read_json, read_text

        if is_enabled():
            for name in txt_names:
                raw = read_text(base_dir, name, scope="energy_monitor")
                if raw:
                    return _parse_kw(raw)
            data = read_json(base_dir, "enline_values.json", scope="energy_monitor")
            if isinstance(data, dict):
                values = data.get("values_kw") or data
                v = values.get(json_key)
                if v is not None and v != "":
                    return _parse_kw(str(v))
            return 0.0
    except Exception:
        pass
    em = Path(base_dir) / "energy_monitor"
    for name in txt_names:
        path = em / name
        if path.is_file():
            try:
                return _parse_kw(path.read_text(encoding="utf-8").strip())
            except Exception:
                pass
    json_path = em / "enline_values.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            values = data.get("values_kw") or data
            v = values.get(json_key)
            if v is not None and v != "":
                return _parse_kw(str(v))
        except Exception:
            pass
    return 0.0


def _load_state(path: Path) -> Dict[str, Any]:
    today = date.today().isoformat()
    if not path.is_file():
        return _empty_state(today)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state(today)
    if state.get("date") != today:
        return _empty_state(today)
    state.setdefault("samples", [])
    return state


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=0), encoding="utf-8")


def record_sample(base_dir: str, force: bool = False) -> None:
    """Speichert einen Punkt (kW / %), höchstens alle 60 Sekunden (1× pro Minute)."""
    global _last_record_monotonic
    now_mono = time.monotonic()
    if not force and now_mono - _last_record_monotonic < SAMPLE_INTERVAL_SEC - 0.5:
        return

    path = _path(base_dir)
    now = datetime.now()
    today = date.today().isoformat()
    battery_kw = _flow_battery_power_kw()

    flow = build_flow_response(
        base_dir, battery_soc_loader=None, battery_power_kw=battery_kw
    )
    flow["battery_power_kw"] = battery_kw

    soc_val: Optional[float] = None
    try:
        from dashboard_battery import load_battery_soc

        raw_soc = load_battery_soc()
        if raw_soc is not None:
            soc_val = float(str(raw_soc).replace("%", "").replace(",", ".").strip())
    except Exception:
        pass
    if soc_val is not None:
        flow["battery_soc"] = soc_val

    from dashboard_config import enabled_line_series, load_config as load_dash_config
    from dashboard_sources import resolve_source_value, _load_ha_snapshot

    dash_cfg = load_dash_config()
    snapshot = _load_ha_snapshot()
    point: Dict[str, Any] = {"m": _minute_of_day(now)}

    for series in enabled_line_series(dash_cfg):
        val = resolve_source_value(
            series.get("source", ""),
            flow=flow,
            snapshot=snapshot,
            battery_kw=battery_kw,
            battery_soc=soc_val,
        )
        if val is not None:
            point[series["id"]] = round(float(val), 2)

    # Legacy-Felder für Abwärtskompatibilität
    pv = _parse_kw(flow.get("pv"))
    hydro = _parse_kw(flow.get("hydro"))
    point.setdefault("erzeugung", round(pv + hydro, 2))
    point.setdefault("netz", round(_parse_kw_signed(flow.get("grid")), 2))
    verbrauch = _parse_kw(flow.get("verbrauch_gesamt"))
    if verbrauch <= 0:
        verbrauch = _enline_kw_from_files(
            base_dir,
            "verbrauch_gesamt",
            ["verbrauch_gesamt_value.txt"],
        )
    # Fallback: Dashboard-Linie / Wohnhaus, wenn Bilanzformel (ohne Hydro) 0 liefert
    if verbrauch <= 0:
        for key in ("line_verbrauch", "line_last", "line_wohnhaus"):
            try:
                verbrauch = max(verbrauch, float(point.get(key) or 0))
            except (TypeError, ValueError):
                pass
    if verbrauch <= 0:
        verbrauch = _parse_kw(flow.get("haus") or flow.get("wohnhaus"))
    point.setdefault("betrieb", round(verbrauch, 2))
    if soc_val is not None:
        point.setdefault("soc", round(soc_val, 1))
    if battery_kw is not None:
        point["battery_kw"] = round(float(battery_kw), 2)

    # Miner-Leistung für Steuerungsalgorithmus (Grundverbrauch = betrieb − miner_kw)
    try:
        from control_algorithm.config import load_config, load_miners, merge_devices_from_miners
        from control_algorithm.planner import miner_load_kw
        from dashboard_miner_status import load_miner_status_map

        cfg = load_config()
        devices = merge_devices_from_miners(cfg, load_miners())
        enabled = [d for d in devices if d.get("enabled")]
        point["miner_kw"] = round(miner_load_kw(enabled, load_miner_status_map()), 2)
    except Exception:
        point.setdefault("miner_kw", 0.0)

    with _lock:
        state = _load_state(path)
        if state.get("date") != today:
            state = _empty_state(today)
        samples: List[Dict[str, Any]] = state["samples"]
        replaced = False
        for i, s in enumerate(samples):
            if int(float(s.get("m", -1))) == point["m"]:
                samples[i] = point
                replaced = True
                break
        if not replaced:
            samples.append(point)
            samples.sort(key=lambda x: int(float(x.get("m", 0))))
        if len(samples) > MAX_SAMPLES:
            samples = samples[-MAX_SAMPLES:]
        state["samples"] = samples
        state["date"] = today
        _save_state(path, state)

    try:
        from ram_cache import maybe_flush_timeseries

        maybe_flush_timeseries(base_dir)
    except Exception:
        pass

    _last_record_monotonic = now_mono


def maybe_record_sample(base_dir: str) -> None:
    record_sample(base_dir, force=False)


def _bucket_by_minute(samples: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    buckets: Dict[int, Dict[str, Any]] = {}
    for s in samples:
        m = int(float(s.get("m", -1)))
        if 0 <= m < MINUTES_PER_DAY:
            buckets[m] = s
    return buckets


def get_timeseries_24h(base_dir: str) -> Dict[str, Any]:
    """Liefert Tageskurve auf fester 24h-Achse (00:00 … jetzt), 1 Punkt pro Minute."""
    from dashboard_config import enabled_line_series, load_config as load_dash_config

    path = _path(base_dir)
    now = datetime.now()
    now_m = int(_minutes_since_midnight(now))

    with _lock:
        state = _load_state(path)

    dash_cfg = load_dash_config()
    series_cfg = enabled_line_series(dash_cfg)

    buckets = _bucket_by_minute(state.get("samples", []))
    end_m = min(now_m, MINUTES_PER_DAY - 1)

    labels: List[str] = []
    minutes: List[int] = []
    series_data: Dict[str, List[Optional[float]]] = {s["id"]: [] for s in series_cfg}
    netz: List[Optional[float]] = []
    erzeugung: List[Optional[float]] = []
    betrieb: List[Optional[float]] = []
    soc: List[Optional[float]] = []

    for minute in range(0, end_m + 1):
        h = minute // 60
        mi = minute % 60
        labels.append(f"{h:02d}:{mi:02d}")
        minutes.append(minute)
        s = buckets.get(minute)
        if s:
            for ser in series_cfg:
                v = s.get(ser["id"])
                series_data[ser["id"]].append(
                    float(v) if v is not None and v != "" else None
                )
            for key, arr in (
                ("netz", netz),
                ("erzeugung", erzeugung),
                ("betrieb", betrieb),
                ("soc", soc),
            ):
                v = s.get(key)
                arr.append(float(v) if v is not None and v != "" else None)
        else:
            for ser in series_cfg:
                series_data[ser["id"]].append(None)
            netz.append(None)
            erzeugung.append(None)
            betrieb.append(None)
            soc.append(None)

    series_out = []
    for ser in series_cfg:
        series_out.append(
            {
                "id": ser["id"],
                "label": ser.get("label"),
                "color": ser.get("color"),
                "fill": bool(ser.get("fill")),
                "y_axis": ser.get("y_axis", "kw"),
                "data": series_data.get(ser["id"], []),
            }
        )

    return {
        "date": state.get("date"),
        "unit_kw": "kW",
        "unit_soc": "%",
        "now_minutes": now_m,
        "sample_interval_sec": SAMPLE_INTERVAL_SEC,
        "labels": labels,
        "minutes": minutes,
        "series": series_out,
        "netz": netz,
        "erzeugung": erzeugung,
        "betrieb": betrieb,
        "soc": soc,
    }
