#!/usr/bin/env python3
"""Jahres-Energiestatistik: kW → kWh, Reset jedes Neujahr."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict

from energy_stats_24h import _format_consumption, _format_generation
from energy_stats_rollover import (
    load_today_24h,
    merge_period_with_today,
    migrate_to_rollover_model,
)

_lock = threading.Lock()


def _stats_path(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / "energy_stats_year.json"


def _year_start(year: int) -> date:
    return date(year, 1, 1)


def _year_end(year: int) -> date:
    return date(year, 12, 31)


def _empty_state(today: date) -> Dict[str, Any]:
    return {
        "period_year": today.year,
        "period_start": _year_start(today.year).isoformat(),
        "last_sample": None,
        "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
        "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
        "verbrauch_gesamt_kwh": 0.0,
    }


def _period_expired(period_year: Any, today: date) -> bool:
    try:
        return int(period_year) != today.year
    except Exception:
        return True


def _load_state(path: Path, today: date) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_state(today)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state(today)

    if _period_expired(state.get("period_year"), today):
        return _empty_state(today)

    state.setdefault(
        "generation_kwh", {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0}
    )
    state.setdefault(
        "consumption_kwh", {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0}
    )
    state.setdefault("verbrauch_gesamt_kwh", 0.0)
    state["period_year"] = today.year
    state["period_start"] = _year_start(today.year).isoformat()
    return state


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def accumulate_from_flow(base_dir: str) -> Dict[str, Any]:
    migrate_to_rollover_model(base_dir)
    path = _stats_path(base_dir)
    today = date.today()
    with _lock:
        state = _load_state(path, today)
        return state


def get_stats_year(base_dir: str) -> Dict[str, Any]:
    migrate_to_rollover_model(base_dir)
    stored = accumulate_from_flow(base_dir)
    merged = merge_period_with_today(stored, load_today_24h(base_dir))
    today = date.today()
    year = today.year
    start = _year_start(year)
    end = _year_end(year)
    days_elapsed = (today - start).days + 1
    period_days = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    gen = merged["generation_kwh"]
    con = merged["consumption_kwh"]
    from energy_stats_totals import format_totals

    from dashboard_config import load_config as load_dash_config
    from dashboard_sources import build_pie_response

    gen_fmt = _format_generation(gen)
    dash_cfg = load_dash_config()
    chart_kwh = merged.get("chart_kwh") or {}
    return {
        "period_year": year,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "days_elapsed": days_elapsed,
        "period_days": period_days,
        "unit": "kWh",
        "generation": gen_fmt,
        "consumption": _format_consumption(con),
        "pie_generation": build_pie_response(chart_kwh, dash_cfg.get("pie_generation") or []),
        "pie_consumption": build_pie_response(chart_kwh, dash_cfg.get("pie_consumption") or []),
        "totals": format_totals(gen_fmt, float(merged.get("verbrauch_gesamt_kwh", 0))),
    }
