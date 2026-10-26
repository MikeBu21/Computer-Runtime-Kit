#!/usr/bin/env python3
"""Überträgt abgeschlossene Tageswerte (24h) in Monats- und Jahresstatistik."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict

MIGRATION_FLAG = ".stats_rollover_v2"


def _add_totals(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key, val in (source or {}).items():
        if isinstance(val, (int, float)):
            target[key] = round(float(target.get(key, 0.0)) + float(val), 4)


def _period_path(base_dir: str, name: str) -> Path:
    return Path(base_dir) / "energy_monitor" / name


def _load_period(path: Path, empty: Dict[str, Any]) -> Dict[str, Any]:
    if not path.is_file():
        return dict(empty)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(empty)
    state.setdefault(
        "generation_kwh", {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0}
    )
    state.setdefault(
        "consumption_kwh", {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0}
    )
    state.setdefault("verbrauch_gesamt_kwh", 0.0)
    return state


def _save_period(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def rollover_daily_totals(base_dir: str, day_state: Dict[str, Any]) -> None:
    """Addiert einen abgeschlossenen Tag in Monats- und Jahres-JSON."""
    day = day_state.get("date")
    if not day:
        return
    try:
        day_date = date.fromisoformat(str(day))
    except ValueError:
        return

    gen = day_state.get("generation_kwh") or {}
    con = day_state.get("consumption_kwh") or {}
    day_verbrauch = float(day_state.get("verbrauch_gesamt_kwh", 0) or 0)

    month_path = _period_path(base_dir, "energy_stats_30d.json")
    month = _load_period(
        month_path,
        {
            "period_start": day_date.replace(day=1).isoformat(),
            "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
            "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
            "verbrauch_gesamt_kwh": 0.0,
        },
    )
    month.setdefault("verbrauch_gesamt_kwh", 0.0)
    month_start = day_date.replace(day=1).isoformat()
    if month.get("period_start") != month_start:
        month = {
            "period_start": month_start,
            "last_sample": None,
            "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
            "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
            "verbrauch_gesamt_kwh": 0.0,
        }
    _add_totals(month["generation_kwh"], gen)
    _add_totals(month["consumption_kwh"], con)
    month.setdefault("chart_kwh", {})
    _add_totals(month["chart_kwh"], day_state.get("chart_kwh") or {})
    month["verbrauch_gesamt_kwh"] = round(
        float(month.get("verbrauch_gesamt_kwh", 0.0)) + day_verbrauch, 4
    )
    month["period_start"] = month_start
    _save_period(month_path, month)

    year_path = _period_path(base_dir, "energy_stats_year.json")
    year = _load_period(
        year_path,
        {
            "period_year": day_date.year,
            "period_start": date(day_date.year, 1, 1).isoformat(),
            "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
            "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
            "verbrauch_gesamt_kwh": 0.0,
        },
    )
    year.setdefault("verbrauch_gesamt_kwh", 0.0)
    if int(year.get("period_year", 0)) != day_date.year:
        year = {
            "period_year": day_date.year,
            "period_start": date(day_date.year, 1, 1).isoformat(),
            "last_sample": None,
            "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
            "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
            "verbrauch_gesamt_kwh": 0.0,
        }
    _add_totals(year["generation_kwh"], gen)
    _add_totals(year["consumption_kwh"], con)
    year.setdefault("chart_kwh", {})
    _add_totals(year["chart_kwh"], day_state.get("chart_kwh") or {})
    year["verbrauch_gesamt_kwh"] = round(
        float(year.get("verbrauch_gesamt_kwh", 0.0)) + day_verbrauch, 4
    )
    year["period_year"] = day_date.year
    year["period_start"] = date(day_date.year, 1, 1).isoformat()
    _save_period(year_path, year)


def migrate_to_rollover_model(base_dir: str) -> None:
    """Entfernt fehlerhafte Live-Akkumulation aus Monat/Jahr (einmalig)."""
    flag = Path(base_dir) / "energy_monitor" / MIGRATION_FLAG
    if flag.is_file():
        return
    for name in ("energy_stats_30d.json", "energy_stats_year.json"):
        path = _period_path(base_dir, name)
        if not path.is_file():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state["generation_kwh"] = {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0}
        state["consumption_kwh"] = {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0}
        state["verbrauch_gesamt_kwh"] = 0.0
        state["last_sample"] = None
        _save_period(path, state)
    flag.touch()


def load_today_24h(base_dir: str) -> Dict[str, Any]:
    path = _period_path(base_dir, "energy_stats_24h.json")
    today = date.today().isoformat()
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if state.get("date") != today:
        return {}
    return state


def merge_period_with_today(stored: Dict[str, Any], today_24h: Dict[str, Any]) -> Dict[str, Any]:
    """Gespeicherte Periodenwerte + laufender Tagesstand."""
    gen = dict(
        stored.get("generation_kwh")
        or {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0}
    )
    con = dict(
        stored.get("consumption_kwh")
        or {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0}
    )
    tgen = today_24h.get("generation_kwh") or {}
    tcon = today_24h.get("consumption_kwh") or {}
    out_gen: Dict[str, float] = {}
    out_con: Dict[str, float] = {}
    for k in ("pv", "hydro", "ladung", "entladung"):
        out_gen[k] = round(float(gen.get(k, 0.0)) + float(tgen.get(k, 0.0)), 4)
    for k in ("betrieb", "haus", "netz", "verkauf"):
        out_con[k] = round(float(con.get(k, 0.0)) + float(tcon.get(k, 0.0)), 4)
    from energy_stats_totals import merge_verbrauch_totals

    chart_stored = dict(stored.get("chart_kwh") or {})
    chart_today = today_24h.get("chart_kwh") or {}
    out_chart: Dict[str, float] = {}
    for key in set(chart_stored) | set(chart_today):
        out_chart[key] = round(
            float(chart_stored.get(key, 0.0)) + float(chart_today.get(key, 0.0)), 4
        )

    return {
        "generation_kwh": out_gen,
        "consumption_kwh": out_con,
        "chart_kwh": out_chart,
        "verbrauch_gesamt_kwh": merge_verbrauch_totals(stored, today_24h),
    }
