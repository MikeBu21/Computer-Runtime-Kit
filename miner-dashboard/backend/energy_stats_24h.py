#!/usr/bin/env python3
"""24-Stunden-Energiestatistik: kW-Messwerte → kWh, Reset täglich um 0:00."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from miner_flow_data import build_flow_response


def _flow_battery_power_kw() -> Optional[float]:
    try:
        from dashboard_battery import load_battery_power_kw

        return load_battery_power_kw()
    except Exception:
        return None

_lock = threading.Lock()
SAMPLE_SECONDS = 6  # gleich Enline/auto_control-Intervall
BATTERY_IDLE_KW = 0.1
# Nur bei neuer Enline-Messung (Datei-mtime) integrieren – kein 5-s-Doppelzählen.
_FLOW_MTIME_FILES = (
    "pv_value.txt",
    "wasserkraft_value.txt",
    "erzeugung_gesamt_value.txt",
    "energy_value.txt",
    "betrieb_value.txt",
    "wohnhaus_value.txt",
    "kelag_value.txt",
    "bezug_value.txt",
    "lieferung_value.txt",
    "enline_values.json",
    "verbrauch_gesamt_value.txt",
    "betrieb_anzeige_value.txt",
)
_MAX_CATCHUP_SECONDS = 900  # max. 15 Min pro Lücke (Neustart), nicht ganzer Tag


def _stats_path(base_dir: str) -> Path:
    try:
        from ram_cache import is_enabled, live_energy_path

        if is_enabled():
            return live_energy_path(base_dir, "energy_stats_24h.json")
    except Exception:
        pass
    return Path(base_dir) / "energy_monitor" / "energy_stats_24h.json"


def _empty_state(today: str) -> Dict[str, Any]:
    return {
        "date": today,
        "last_sample": None,
        "bootstrapped": False,
        "verbrauch_gesamt_kwh": 0.0,
        "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
        "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
        "chart_kwh": {},
    }


_LEGACY_GEN_MAP = {
    "gen_pv": "pv",
    "gen_hydro": "hydro",
    "gen_ladung": "ladung",
    "gen_entladung": "entladung",
}
_LEGACY_CON_MAP = {
    "con_betrieb": "betrieb",
    "con_haus": "haus",
    "con_netz": "netz",
    "con_verkauf": "verkauf",
}


def _parse_kw(raw: Any) -> float:
    if raw is None or raw == "" or raw == "n/a":
        return 0.0
    try:
        return max(0.0, float(str(raw).replace(",", ".").strip()))
    except ValueError:
        return 0.0


def _parse_kw_signed(raw: Any) -> float:
    if raw is None or raw == "" or raw == "n/a":
        return 0.0
    try:
        return float(str(raw).replace(",", ".").strip())
    except ValueError:
        return 0.0


def _grid_bezug_kw(raw: Any) -> float:
    """Netzbezug (positiver Netz-/Kelag-Wert)."""
    return max(0.0, _parse_kw_signed(raw))


def _grid_verkauf_kw(raw: Any) -> float:
    """Einspeisung ins Netz (negativer Netz-Wert als positive kW)."""
    return max(0.0, -_parse_kw_signed(raw))


def _generation_defaults() -> Dict[str, float]:
    return {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0}


def _battery_ladung_kw(power_kw: Optional[float]) -> float:
    """API +kW = Laden in die Batterie."""
    if power_kw is None or power_kw <= BATTERY_IDLE_KW:
        return 0.0
    return float(power_kw)


def _battery_entladung_kw(power_kw: Optional[float]) -> float:
    """API -kW = Entladen aus der Batterie."""
    if power_kw is None or power_kw >= -BATTERY_IDLE_KW:
        return 0.0
    return abs(float(power_kw))


def _consumption_defaults() -> Dict[str, float]:
    return {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0}


def _format_generation(gen: Dict[str, Any]) -> Dict[str, float]:
    g = _generation_defaults()
    for key in g:
        g[key] = round(float(gen.get(key, 0.0)), 2)
    return g


def _format_consumption(con: Dict[str, Any]) -> Dict[str, float]:
    c = _consumption_defaults()
    for key in c:
        c[key] = round(float(con.get(key, 0.0)), 2)
    return c


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
    state.setdefault("generation_kwh", _generation_defaults())
    state.setdefault("consumption_kwh", _consumption_defaults())
    state.setdefault("verbrauch_gesamt_kwh", 0.0)
    state.setdefault("chart_kwh", {})
    state.setdefault("bootstrapped", bool(state.get("last_sample")))
    return state


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _flow_measurement_mtime(base_dir: str) -> float:
    """Neuester Zeitstempel der Enline-/Batterie-Messdateien."""
    em = Path(base_dir) / "energy_monitor"
    latest = 0.0
    try:
        from ram_cache import is_enabled, live_energy_path

        if is_enabled():
            for name in _FLOW_MTIME_FILES:
                path = live_energy_path(base_dir, name)
                if path.is_file():
                    latest = max(latest, path.stat().st_mtime)
    except Exception:
        pass
    for name in _FLOW_MTIME_FILES:
        path = em / name
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    try:
        from dashboard_battery import _battery_data_paths

        for path in _battery_data_paths():
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    except Exception:
        pass
    return latest


def _delta_hours_for_measurement(state: Dict[str, Any], measurement_mtime: float) -> float:
    """kWh-Intervall = Zeit zwischen zwei echten Enline-Aktualisierungen."""
    last_m = state.get("last_flow_mtime")
    if last_m is None:
        return SAMPLE_SECONDS / 3600.0
    elapsed = max(0.0, measurement_mtime - float(last_m))
    if elapsed <= 0.001:
        return 0.0
    return min(elapsed / 3600.0, _MAX_CATCHUP_SECONDS / 3600.0)


def _integrate_timeseries_kw(samples: list, key: str) -> float:
    if len(samples) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(samples)):
        dm = float(samples[i].get("m", 0)) - float(samples[i - 1].get("m", 0))
        if dm <= 0:
            continue
        dt_h = dm / 60.0
        v0 = float(samples[i - 1].get(key) or 0)
        v1 = float(samples[i].get(key) or 0)
        total += (v0 + v1) / 2.0 * dt_h
    return round(total, 4)


def _maybe_backfill_from_timeseries(
    state: Dict[str, Any], base_dir: str, flow: Dict[str, Any]
) -> None:
    """Stellt Tages-kWh aus Liniendiagramm-Daten wieder her (nach Reset/Neustart)."""
    if state.get("timeseries_backfill_date") == state.get("date"):
        return
    ts_path = Path(base_dir) / "energy_monitor" / "energy_timeseries_24h.json"
    if not ts_path.is_file():
        return
    try:
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if ts.get("date") != state.get("date"):
        return
    samples = ts.get("samples") or []
    if len(samples) < 3:
        return

    erzeug_kwh = _integrate_timeseries_kw(samples, "erzeugung")
    pv_kw = _parse_kw(flow.get("pv"))
    hydro_kw = _parse_kw(flow.get("hydro"))
    denom = pv_kw + hydro_kw
    pv_frac = (pv_kw / denom) if denom > 0.5 else 0.88

    gen = state["generation_kwh"]
    cur_erzeug = float(gen.get("pv", 0)) + float(gen.get("hydro", 0))
    if erzeug_kwh > cur_erzeug * 1.08:
        gen["pv"] = round(erzeug_kwh * pv_frac, 4)
        gen["hydro"] = round(erzeug_kwh * (1.0 - pv_frac), 4)

    ladung_kwh = 0.0
    entladung_kwh = 0.0
    for i in range(1, len(samples)):
        dm = float(samples[i].get("m", 0)) - float(samples[i - 1].get("m", 0))
        if dm <= 0:
            continue
        dt_h = dm / 60.0
        bkw = samples[i - 1].get("battery_kw")
        if bkw is None:
            continue
        try:
            p = float(bkw)
        except (TypeError, ValueError):
            continue
        ladung_kwh += _battery_ladung_kw(p) * dt_h
        entladung_kwh += _battery_entladung_kw(p) * dt_h
    ladung_kwh = round(ladung_kwh, 4)
    entladung_kwh = round(entladung_kwh, 4)
    if ladung_kwh > float(gen.get("ladung", 0)) * 1.08:
        gen["ladung"] = ladung_kwh
    if entladung_kwh > float(gen.get("entladung", 0)) * 1.08:
        gen["entladung"] = entladung_kwh

    verbrauch_kwh = _integrate_timeseries_kw(samples, "betrieb")
    if verbrauch_kwh > float(state.get("verbrauch_gesamt_kwh", 0)) * 1.08:
        state["verbrauch_gesamt_kwh"] = verbrauch_kwh

    state["timeseries_backfill_date"] = state.get("date")


def _maybe_rollover_previous_day(path: Path, base_dir: str, today: str) -> None:
    if not path.is_file():
        return
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    prev_date = prev.get("date")
    if not prev_date or prev_date == today:
        return
    from energy_stats_rollover import rollover_daily_totals

    rollover_daily_totals(base_dir, prev)
    try:
        from ram_cache import flush_timeseries_to_sd

        flush_timeseries_to_sd(base_dir)
    except Exception:
        pass


def accumulate_from_flow(base_dir: str) -> Dict[str, Any]:
    """Liest aktuelle kW-Werte und addiert kWh zur Tagesstatistik."""
    path = _stats_path(base_dir)
    now = datetime.now()
    today = date.today().isoformat()

    with _lock:
        _maybe_rollover_previous_day(path, base_dir, today)
        state = _load_state(path)
        if state.get("date") != today:
            state = _empty_state(today)

        measurement_mtime = _flow_measurement_mtime(base_dir)
        hours = _delta_hours_for_measurement(state, measurement_mtime)
        if hours <= 0:
            state["last_sample"] = now.isoformat(timespec="seconds")
            _save_state(path, state)
            return state

        battery_kw = _flow_battery_power_kw()
        flow = build_flow_response(
            base_dir, battery_soc_loader=None, battery_power_kw=battery_kw
        )
        _maybe_backfill_from_timeseries(state, base_dir, flow)

        if hours > 0:
            from dashboard_config import enabled_pie_slices, load_config as load_dash_config
            from dashboard_sources import integration_kw_for_pie

            gen = state["generation_kwh"]
            con = state["consumption_kwh"]
            chart_kwh: Dict[str, float] = state.setdefault("chart_kwh", {})
            dash_cfg = load_dash_config()

            for sl in enabled_pie_slices(dash_cfg, "generation"):
                kw = integration_kw_for_pie(sl.get("source", ""), flow, battery_kw)
                sid = sl["id"]
                chart_kwh[sid] = round(float(chart_kwh.get(sid, 0.0)) + kw * hours, 4)
                legacy = _LEGACY_GEN_MAP.get(sid)
                if legacy:
                    gen[legacy] = round(float(gen.get(legacy, 0.0)) + kw * hours, 4)

            for sl in enabled_pie_slices(dash_cfg, "consumption"):
                kw = integration_kw_for_pie(sl.get("source", ""), flow, battery_kw)
                sid = sl["id"]
                chart_kwh[sid] = round(float(chart_kwh.get(sid, 0.0)) + kw * hours, 4)
                legacy = _LEGACY_CON_MAP.get(sid)
                if legacy:
                    con[legacy] = round(float(con.get(legacy, 0.0)) + kw * hours, 4)

            if not enabled_pie_slices(dash_cfg, "generation"):
                gen["pv"] = round(gen.get("pv", 0.0) + _parse_kw(flow.get("pv")) * hours, 4)
                gen["hydro"] = round(gen.get("hydro", 0.0) + _parse_kw(flow.get("hydro")) * hours, 4)
                gen["ladung"] = round(
                    gen.get("ladung", 0.0) + _battery_ladung_kw(battery_kw) * hours, 4
                )
                gen["entladung"] = round(
                    gen.get("entladung", 0.0) + _battery_entladung_kw(battery_kw) * hours, 4
                )

            if not enabled_pie_slices(dash_cfg, "consumption"):
                grid_raw = flow.get("grid")
                betrieb_kw = _parse_kw(flow.get("betrieb_anzeige"))
                con["betrieb"] = round(con.get("betrieb", 0.0) + betrieb_kw * hours, 4)
                con["haus"] = round(con.get("haus", 0.0) + _parse_kw(flow.get("haus")) * hours, 4)
                con["netz"] = round(con.get("netz", 0.0) + _grid_bezug_kw(grid_raw) * hours, 4)
                con["verkauf"] = round(
                    con.get("verkauf", 0.0) + _grid_verkauf_kw(grid_raw) * hours, 4
                )

            verbrauch_kw = _parse_kw(flow.get("verbrauch_gesamt"))
            state["verbrauch_gesamt_kwh"] = round(
                float(state.get("verbrauch_gesamt_kwh", 0.0)) + verbrauch_kw * hours,
                4,
            )

        state["date"] = today
        state["bootstrapped"] = True
        state["last_flow_mtime"] = measurement_mtime
        state["last_sample"] = now.isoformat(timespec="seconds")
        state.pop("repaired_hydro", None)
        _save_state(path, state)
        try:
            from ram_cache import maybe_flush_timeseries

            maybe_flush_timeseries(base_dir)
        except Exception:
            pass
        return state


def read_stats_24h(base_dir: str) -> Dict[str, Any]:
    """Nur lesen – Akkumulation läuft im Hintergrund-Thread / auto_control."""
    path = _stats_path(base_dir)
    today = date.today().isoformat()
    with _lock:
        state = _load_state(path)
        if state.get("date") != today:
            state = _empty_state(today)
    return state


def _sync_chart_kwh_from_legacy(state: Dict[str, Any], dash_cfg: Dict[str, Any]) -> None:
    """Übernimmt Legacy-kWh in chart_kwh wenn Slice neu zugeordnet wurde."""
    from dashboard_config import enabled_pie_slices

    chart_kwh: Dict[str, float] = state.setdefault("chart_kwh", {})
    gen = state.get("generation_kwh") or {}
    con = state.get("consumption_kwh") or {}
    for sl in enabled_pie_slices(dash_cfg, "generation"):
        sid = sl["id"]
        legacy = _LEGACY_GEN_MAP.get(sid)
        if legacy and float(chart_kwh.get(sid, 0.0)) <= 0 and float(gen.get(legacy, 0.0)) > 0:
            chart_kwh[sid] = float(gen[legacy])
    for sl in enabled_pie_slices(dash_cfg, "consumption"):
        sid = sl["id"]
        legacy = _LEGACY_CON_MAP.get(sid)
        if legacy and float(chart_kwh.get(sid, 0.0)) <= 0 and float(con.get(legacy, 0.0)) > 0:
            chart_kwh[sid] = float(con[legacy])


def _live_slice_kwh_estimate(base_dir: str, source: str) -> float:
    """Schätzt Tages-kWh aus aktuellem kW × vergangene Stunden (Anzeige bis Akkumulation nachzieht)."""
    try:
        from dashboard_sources import integration_kw_for_pie
        from miner_flow_data import build_flow_response

        battery_kw = _flow_battery_power_kw()
        flow = build_flow_response(base_dir, battery_soc_loader=None, battery_power_kw=battery_kw)
        kw = integration_kw_for_pie(source, flow, battery_kw)
        if kw <= 0:
            return 0.0
        now = datetime.now()
        hours = (now.hour * 60 + now.minute + now.second / 60.0) / 60.0
        return round(kw * hours, 2)
    except Exception:
        return 0.0


def get_stats_24h(base_dir: str) -> Dict[str, Any]:
    from dashboard_config import enabled_pie_slices, load_config as load_dash_config
    from dashboard_sources import build_pie_response
    from energy_stats_totals import format_totals

    state = read_stats_24h(base_dir)
    gen = _format_generation(state["generation_kwh"])
    dash_cfg = load_dash_config()
    _sync_chart_kwh_from_legacy(state, dash_cfg)
    chart_kwh = dict(state.get("chart_kwh") or {})
    for key in ("pie_generation", "pie_consumption"):
        for sl in enabled_pie_slices(dash_cfg, key.replace("pie_", "")):
            if not sl.get("enabled"):
                continue
            sid = sl["id"]
            stored = float(chart_kwh.get(sid, 0.0))
            if stored <= 0:
                est = _live_slice_kwh_estimate(base_dir, sl.get("source", ""))
                if est > stored:
                    chart_kwh[sid] = est
    pie_gen = build_pie_response(chart_kwh, dash_cfg.get("pie_generation") or [])
    pie_con = build_pie_response(chart_kwh, dash_cfg.get("pie_consumption") or [])
    return {
        "date": state.get("date"),
        "unit": "kWh",
        "generation": gen,
        "consumption": _format_consumption(state["consumption_kwh"]),
        "pie_generation": pie_gen,
        "pie_consumption": pie_con,
        "totals": format_totals(gen, float(state.get("verbrauch_gesamt_kwh", 0))),
    }
