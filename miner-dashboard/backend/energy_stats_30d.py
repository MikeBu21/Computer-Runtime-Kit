#!/usr/bin/env python3
"""Monats-Energiestatistik (30-Tage-Feld): kW → kWh, Reset jeden Monat."""
from __future__ import annotations

import calendar
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
    return Path(base_dir) / "energy_monitor" / "energy_stats_30d.json"


def _month_start(today: date) -> date:
    return today.replace(day=1)


def _month_end(today: date) -> date:
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=last_day)


def _empty_state(today: date) -> Dict[str, Any]:
    start = _month_start(today)
    return {
        "period_start": start.isoformat(),
        "last_sample": None,
        "generation_kwh": {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0},
        "consumption_kwh": {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0},
        "verbrauch_gesamt_kwh": 0.0,
    }


def _period_expired(period_start: str, today: date) -> bool:
    try:
        start = date.fromisoformat(period_start)
        return start.year != today.year or start.month != today.month
    except Exception:
        return True


def _load_state(path: Path, today: date) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_state(today)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state(today)

    period_start = state.get("period_start") or _month_start(today).isoformat()
    if _period_expired(period_start, today):
        return _empty_state(today)

    state.setdefault(
        "generation_kwh", {"pv": 0.0, "hydro": 0.0, "ladung": 0.0, "entladung": 0.0}
    )
    state.setdefault(
        "consumption_kwh", {"betrieb": 0.0, "haus": 0.0, "netz": 0.0, "verkauf": 0.0}
    )
    state.setdefault("verbrauch_gesamt_kwh", 0.0)
    state["period_start"] = _month_start(today).isoformat()
    return state


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def accumulate_from_flow(base_dir: str) -> Dict[str, Any]:
    """Monat = abgeschlossene Tage (Rollover); kein paralleles Live-Addieren."""
    migrate_to_rollover_model(base_dir)
    path = _stats_path(base_dir)
    today = date.today()
    with _lock:
        state = _load_state(path, today)
        return state


def get_stats_30d(base_dir: str) -> Dict[str, Any]:
    migrate_to_rollover_model(base_dir)
    stored = accumulate_from_flow(base_dir)
    merged = merge_period_with_today(stored, load_today_24h(base_dir))
    today = date.today()
    start = _month_start(today)
    end = _month_end(today)
    period_days = calendar.monthrange(today.year, today.month)[1]
    days_elapsed = today.day
    gen = merged["generation_kwh"]
    con = merged["consumption_kwh"]
    from energy_stats_totals import format_totals

    from dashboard_config import load_config as load_dash_config
    from dashboard_sources import build_pie_response

    gen_fmt = _format_generation(gen)
    dash_cfg = load_dash_config()
    chart_kwh = merged.get("chart_kwh") or {}
    return {
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
