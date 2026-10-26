#!/usr/bin/env python3
"""Empfohlener Dauer-Netzbezug (kWh/h) aus Wochenhochrechnung.

Ziel: gleichmäßiger Netzbezug über den Monat, damit keine stark schwankenden
Stundenwerte (z. B. 20 vs. 100 kWh) die Monatsspitze treiben.

- Letzte ~7 Tage als typische Woche → Hochrechnung aufs Monat
- Saisonwechsel (PV sinkt / Verbrauch steigt) innerhalb ~2 Wochen erkennen
- Wöchentlich neu, mit Glättung gegen wilde Sprünge
"""
from __future__ import annotations

import calendar
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_lock = threading.Lock()

LOOKBACK_DAYS = 7
COMPARE_DAYS = 7  # vorherige Woche für Saison-Erkennung
MIN_DAYS = 3
MAX_DAY_ARCHIVE = 90
SMOOTH_ALPHA_NORMAL = 0.45
SMOOTH_ALPHA_SHIFT = 0.70
MAX_STEP_NORMAL = 0.25  # max. relative Änderung/Woche
MAX_STEP_SHIFT = 0.50
PV_DROP_FACTOR = 0.70  # PV < 70 % der Vorwoche
VERBRAUCH_RISE_FACTOR = 1.20  # Verbrauch > +20 %


def _state_path(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / "recommended_grid_import.json"


def _empty_state() -> Dict[str, Any]:
    return {
        "days": [],
        "recommendation": {
            "kwh_per_hour": None,
            "raw_kwh_per_hour": None,
            "month_projected_kwh": None,
            "week_id": None,
            "updated_at": None,
            "season_shift": False,
            "days_used": 0,
            "basis": None,
        },
    }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "" or value == "n/a":
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _iso_week_id(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()
    data.setdefault("days", [])
    data.setdefault("recommendation", _empty_state()["recommendation"])
    return data


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _day_index(days: List[Dict[str, Any]], day: str) -> int:
    for i, row in enumerate(days):
        if row.get("date") == day:
            return i
    return -1


def _upsert_day(days: List[Dict[str, Any]], row: Dict[str, Any]) -> None:
    day = str(row.get("date") or "")[:10]
    if not day:
        return
    row = dict(row)
    row["date"] = day
    idx = _day_index(days, day)
    if idx >= 0:
        # Abgeschlossene Tage nicht mit Partial-Werten überschreiben
        if days[idx].get("complete") and not row.get("complete"):
            return
        days[idx].update(row)
    else:
        days.append(row)
    days.sort(key=lambda r: str(r.get("date") or ""))
    while len(days) > MAX_DAY_ARCHIVE:
        days.pop(0)


def _load_today_24h(base_dir: str) -> Dict[str, Any]:
    try:
        from ram_cache import is_enabled, read_json

        if is_enabled():
            state = read_json(base_dir, "energy_stats_24h.json", scope="energy_monitor")
            return state if isinstance(state, dict) else {}
    except Exception:
        pass
    path = Path(base_dir) / "energy_monitor" / "energy_stats_24h.json"
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _pv_from_yield_history(base_dir: str) -> Dict[str, float]:
    """PV-Ist aus Steuerungsalgorithmus-Kalibrierung (falls vorhanden)."""
    path = Path(base_dir) / "control_algorithm_state.json"
    out: Dict[str, float] = {}
    if not path.is_file():
        return out
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    for row in state.get("yield_history") or []:
        day = str(row.get("date") or "")[:10]
        actual = row.get("actual")
        if day and actual is not None:
            out[day] = _f(actual)
    today = date.today().isoformat()
    live = state.get("live_pv_kwh_actual")
    if live is not None:
        out[today] = max(out.get(today, 0.0), _f(live))
    return out


def _month_avg_pv_kwh(base_dir: str) -> Optional[float]:
    """Ø PV kWh/Tag aus Monatsstatistik (+ heutiger Hochrechnung)."""
    path = Path(base_dir) / "energy_monitor" / "energy_stats_30d.json"
    today_stats = _load_today_24h(base_dir)
    pv = 0.0
    days = 0
    if path.is_file():
        try:
            month = json.loads(path.read_text(encoding="utf-8"))
            start = str(month.get("period_start") or "")[:10]
            if start:
                start_d = date.fromisoformat(start)
                elapsed = max(0, (date.today() - start_d).days)
                pv += _f((month.get("generation_kwh") or {}).get("pv"))
                days += elapsed
        except Exception:
            pass
    if today_stats.get("date") == date.today().isoformat():
        hours = max(1.0, datetime.now().hour + datetime.now().minute / 60.0)
        frac = min(1.0, hours / 24.0)
        if frac >= 0.2:
            today_pv = _f((today_stats.get("generation_kwh") or {}).get("pv"))
            pv += today_pv / frac
            days += 1
    if days <= 0:
        vals = list(_pv_from_yield_history(base_dir).values())
        if not vals:
            return None
        return sum(vals) / len(vals)
    return pv / days


def _resolve_day_pv(
    day: str,
    pv_map: Dict[str, float],
    month_avg_pv: Optional[float],
) -> Tuple[float, str]:
    if day in pv_map and pv_map[day] > 0:
        return pv_map[day], "yield_or_live"
    if month_avg_pv is not None and month_avg_pv > 0:
        return month_avg_pv, "month_avg"
    return 0.0, "none"


def _load_always_on_settings(base_dir: str) -> Tuple[int, float, List[Dict[str, Any]]]:
    """Anzahl, Summen-kW und Liste der 24h-Geräte (höchste Priority zuerst)."""
    try:
        from control_algorithm.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    try:
        count = int(cfg.get("dauer_zukauf_always_on_count") or 0)
    except (TypeError, ValueError):
        count = 0
    devices = [
        d
        for d in (cfg.get("devices") or [])
        if d.get("enabled") and _f(d.get("power_kw")) > 0
    ]
    devices.sort(key=lambda d: _f(d.get("priority")), reverse=True)
    count = max(0, min(count, len(devices)))
    selected = devices[:count]
    total_kw = sum(_f(d.get("power_kw")) for d in selected)
    return count, total_kw, selected


def _today_base_kwh(base_dir: str) -> float:
    """Grundverbrauch heute (betrieb − miner) seit Mitternacht."""
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        from control_algorithm.consumption_history import actual_base_consumption_kwh

        return _f(actual_base_consumption_kwh(base_dir, start, now))
    except Exception:
        return 0.0


def _verbrauch_for_always_on(
    base_kwh: float, always_on_kw: float, *, hours: float = 24.0
) -> float:
    """Verbrauch für Dauer-Zukauf: Grundlast + N Geräte × Stunden."""
    return max(0.0, _f(base_kwh) + _f(always_on_kw) * hours)


def _refresh_days_always_on(days: List[Dict[str, Any]], base_dir: str) -> None:
    """Aktualisiert verbrauch/need aller Tage nach aktueller 24h-Geräteanzahl."""
    _, always_on_kw, _ = _load_always_on_settings(base_dir)
    today = date.today()
    for row in days:
        base = row.get("base_kwh")
        if base is None:
            continue
        base_f = _f(base)
        try:
            d = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        if not row.get("complete") and d == today:
            hours = max(1.0, datetime.now().hour + datetime.now().minute / 60.0)
            verbrauch = _verbrauch_for_always_on(base_f, always_on_kw, hours=hours)
        else:
            verbrauch = _verbrauch_for_always_on(base_f, always_on_kw, hours=24.0)
        pv = _f(row.get("pv_kwh"))
        hydro = _f(row.get("hydro_kwh"))
        netz = row.get("netz_kwh")
        row["verbrauch_kwh"] = round(verbrauch, 4)
        row["deficit_kwh"] = round(max(0.0, verbrauch - pv - hydro), 4)
        row["need_kwh"] = round(_day_need(verbrauch, pv, hydro, netz), 4)


def _history_base_kwh(row: Dict[str, Any]) -> float:
    """Grundlast aus Verbrauchshistorie (ohne intermittierende Miner)."""
    base = _f(row.get("total_base_kwh"))
    if base > 0:
        return base
    total = _f(row.get("total_verbrauch_kwh"))
    if total <= 0:
        return 0.0
    hourly_base = row.get("hourly_base_kwh") or []
    if hourly_base:
        return sum(_f(x) for x in hourly_base)
    return total


def _day_need(
    verbrauch: float, pv: float, hydro: float, netz: Optional[float] = None
) -> float:
    """Benötigter Netzbezug: Energiebilanz, optional mit gemessenem Netz."""
    deficit = max(0.0, verbrauch - pv - hydro)
    if netz is None:
        return deficit
    return max(_f(netz), deficit)


def _repair_day_row(
    row: Dict[str, Any],
    pv_map: Dict[str, float],
    month_avg_pv: Optional[float],
) -> None:
    """Korrigiert Tage ohne PV (sonst wird der Zukauf massiv überschätzt)."""
    day = str(row.get("date") or "")[:10]
    pv = _f(row.get("pv_kwh"))
    source = str(row.get("source") or "")
    if source == "energy_stats_24h" and pv > 0:
        verbrauch = _f(row.get("verbrauch_kwh"))
        hydro = _f(row.get("hydro_kwh"))
        netz = row.get("netz_kwh")
        row["deficit_kwh"] = round(max(0.0, verbrauch - pv - hydro), 4)
        row["need_kwh"] = round(_day_need(verbrauch, pv, hydro, netz), 4)
        return
    if pv <= 0 or source == "consumption_history":
        est_pv, pv_src = _resolve_day_pv(day, pv_map, month_avg_pv)
        if est_pv > 0:
            row["pv_kwh"] = round(est_pv, 4)
            row["pv_source"] = pv_src
            pv = est_pv
        verbrauch = _f(row.get("verbrauch_kwh"))
        hydro = _f(row.get("hydro_kwh"))
        deficit = max(0.0, verbrauch - pv - hydro)
        row["deficit_kwh"] = round(deficit, 4)
        row["need_kwh"] = round(deficit, 4)
        if source == "consumption_history":
            row["netz_kwh"] = round(deficit, 4)


def _bootstrap_from_consumption_history(base_dir: str, days: List[Dict[str, Any]]) -> None:
    """Füllt fehlende Tage aus Verbrauchshistorie + PV-Schätzung."""
    hist_path = Path(base_dir) / "energy_monitor" / "consumption_history.json"
    if not hist_path.is_file():
        return
    try:
        hist = json.loads(hist_path.read_text(encoding="utf-8"))
    except Exception:
        return
    pv_map = _pv_from_yield_history(base_dir)
    month_avg_pv = _month_avg_pv_kwh(base_dir)
    today = date.today().isoformat()

    for existing in days:
        _repair_day_row(existing, pv_map, month_avg_pv)

    for row in hist.get("days") or []:
        day = str(row.get("date") or "")[:10]
        if not day or day == today:
            continue
        if _day_index(days, day) >= 0:
            continue
        verbrauch = _f(row.get("total_verbrauch_kwh"))
        hydro = sum(_f(x) for x in (row.get("hourly_hydro_kwh") or []))
        if hydro <= 0:
            hydro = _f(row.get("total_hydro_kwh"))
        pv, pv_src = _resolve_day_pv(day, pv_map, month_avg_pv)
        base_kwh = _history_base_kwh(row)
        _, always_on_kw, _ = _load_always_on_settings(base_dir)
        verbrauch = _verbrauch_for_always_on(base_kwh, always_on_kw, hours=24.0)
        need = _day_need(verbrauch, pv, hydro)
        _upsert_day(
            days,
            {
                "date": day,
                "base_kwh": round(base_kwh, 4),
                "verbrauch_kwh": round(verbrauch, 4),
                "pv_kwh": round(pv, 4),
                "pv_source": pv_src,
                "hydro_kwh": round(hydro, 4),
                "netz_kwh": round(need, 4),
                "deficit_kwh": round(need, 4),
                "need_kwh": round(need, 4),
                "complete": True,
                "source": "consumption_history",
            },
        )


def _backfill_base_from_history(
    days: List[Dict[str, Any]], base_dir: str
) -> None:
    """Ergänzt base_kwh in älteren Tageszeilen (Migration)."""
    hist_path = Path(base_dir) / "energy_monitor" / "consumption_history.json"
    hist_by_day: Dict[str, Dict[str, Any]] = {}
    if hist_path.is_file():
        try:
            hist = json.loads(hist_path.read_text(encoding="utf-8"))
            for row in hist.get("days") or []:
                day = str(row.get("date") or "")[:10]
                if day:
                    hist_by_day[day] = row
        except Exception:
            pass
    today_s = date.today().isoformat()
    for row in days:
        if row.get("base_kwh") is not None:
            continue
        day = str(row.get("date") or "")[:10]
        if not day:
            continue
        if day == today_s:
            row["base_kwh"] = round(_today_base_kwh(base_dir), 4)
        elif day in hist_by_day:
            row["base_kwh"] = round(_history_base_kwh(hist_by_day[day]), 4)


def sync_today_from_stats(base_dir: str) -> Dict[str, Any]:
    """Übernimmt aktuelle 24h-Zähler in das Tagesarchiv (für Live + Tageswechsel)."""
    path = _state_path(base_dir)
    today = date.today()
    today_s = today.isoformat()
    stats = _load_today_24h(base_dir)

    with _lock:
        state = _load_state(path)
        days: List[Dict[str, Any]] = list(state.get("days") or [])
        _bootstrap_from_consumption_history(base_dir, days)
        _backfill_base_from_history(days, base_dir)

        # Gestern als complete markieren, sobald wir einen neuen Tag sehen
        yesterday = (today - timedelta(days=1)).isoformat()
        y_idx = _day_index(days, yesterday)
        if y_idx >= 0 and not days[y_idx].get("complete"):
            days[y_idx]["complete"] = True

        if stats.get("date") == today_s:
            gen = stats.get("generation_kwh") or {}
            con = stats.get("consumption_kwh") or {}
            pv = _f(gen.get("pv"))
            hydro = _f(gen.get("hydro"))
            netz = _f(con.get("netz"))
            base_kwh = _today_base_kwh(base_dir)
            _, always_on_kw, _ = _load_always_on_settings(base_dir)
            hours = max(1.0, datetime.now().hour + datetime.now().minute / 60.0)
            verbrauch = _verbrauch_for_always_on(base_kwh, always_on_kw, hours=hours)
            need = _day_need(verbrauch, pv, hydro, netz)
            _upsert_day(
                days,
                {
                    "date": today_s,
                    "base_kwh": round(base_kwh, 4),
                    "verbrauch_kwh": round(verbrauch, 4),
                    "pv_kwh": round(pv, 4),
                    "pv_source": "energy_stats_24h",
                    "hydro_kwh": round(hydro, 4),
                    "netz_kwh": round(netz, 4),
                    "deficit_kwh": round(max(0.0, verbrauch - pv - hydro), 4),
                    "need_kwh": round(need, 4),
                    "complete": False,
                    "source": "energy_stats_24h",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )

        state["days"] = days
        _refresh_days_always_on(days, base_dir)
        _save_state(path, state)
        return state


def _window_sums(
    days: List[Dict[str, Any]], start: date, end: date
) -> Tuple[float, float, float, float, float, int]:
    """Summen in [start, end] inklusiv. Gibt (need, netz, pv, hydro, verbrauch, n) zurück."""
    need = netz = pv = hydro = verbrauch = 0.0
    n = 0
    for row in days:
        try:
            d = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        if d < start or d > end:
            continue
        # Unvollständiger Heuttag: bisherige kWh auf 24 h hochrechnen
        scale = 1.0
        if not row.get("complete") and d == date.today():
            hours = max(1.0, datetime.now().hour + datetime.now().minute / 60.0)
            frac = min(1.0, hours / 24.0)
            if frac < 0.15:
                continue
            scale = 1.0 / frac
        n += 1
        day_need = row.get("need_kwh")
        if day_need is None:
            day_need = max(_f(row.get("netz_kwh")), _f(row.get("deficit_kwh")))
        need += _f(day_need) * scale
        netz += _f(row.get("netz_kwh")) * scale
        pv += _f(row.get("pv_kwh")) * scale
        hydro += _f(row.get("hydro_kwh")) * scale
        verbrauch += _f(row.get("verbrauch_kwh")) * scale
    return need, netz, pv, hydro, verbrauch, n


def _detect_season_shift(
    days: List[Dict[str, Any]], today: date
) -> Tuple[bool, Dict[str, Any]]:
    recent_start = today - timedelta(days=LOOKBACK_DAYS - 1)
    prev_end = recent_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=COMPARE_DAYS - 1)
    r_need, _, r_pv, _, r_verbr, r_n = _window_sums(days, recent_start, today)
    p_need, _, p_pv, _, p_verbr, p_n = _window_sums(days, prev_start, prev_end)
    info = {
        "recent_days": r_n,
        "prev_days": p_n,
        "recent_pv_kwh": round(r_pv, 2),
        "prev_pv_kwh": round(p_pv, 2),
        "recent_verbrauch_kwh": round(r_verbr, 2),
        "prev_verbrauch_kwh": round(p_verbr, 2),
        "recent_need_kwh": round(r_need, 2),
        "prev_need_kwh": round(p_need, 2),
    }
    if r_n < MIN_DAYS or p_n < MIN_DAYS:
        return False, info
    pv_drop = p_pv > 50 and r_pv < p_pv * PV_DROP_FACTOR
    verbrauch_up = p_verbr > 50 and r_verbr > p_verbr * VERBRAUCH_RISE_FACTOR
    # Saison = PV bricht ein, oder Verbrauch steigt bei gleichzeitig schwächerer PV
    season = bool(pv_drop or (verbrauch_up and r_pv < p_pv * 0.9))
    return season, info


def _mtd_netz_kwh_per_hour(base_dir: str) -> Optional[float]:
    """Monats-Netzbezug bisher → kWh/h (Anker gegen überschätzte Historie)."""
    path = Path(base_dir) / "energy_monitor" / "energy_stats_30d.json"
    today_stats = _load_today_24h(base_dir)
    netz = 0.0
    hours = 0.0
    if path.is_file():
        try:
            month = json.loads(path.read_text(encoding="utf-8"))
            start = str(month.get("period_start") or "")[:10]
            if start:
                start_d = date.fromisoformat(start)
                elapsed_days = max(0, (date.today() - start_d).days)
                netz += _f((month.get("consumption_kwh") or {}).get("netz"))
                hours += elapsed_days * 24.0
        except Exception:
            pass
    if today_stats.get("date") == date.today().isoformat():
        hours_today = max(1.0, datetime.now().hour + datetime.now().minute / 60.0)
        netz += _f((today_stats.get("consumption_kwh") or {}).get("netz"))
        hours += hours_today
    if hours < 24:
        return None
    return max(0.0, netz / hours)


def _estimated_pv_share(days: List[Dict[str, Any]], start: date, end: date) -> float:
    total = 0
    estimated = 0
    for row in days:
        try:
            d = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        if d < start or d > end:
            continue
        total += 1
        src = str(row.get("pv_source") or "")
        if src in ("month_avg", "none") or (
            row.get("source") == "consumption_history" and src != "yield_or_live"
        ):
            estimated += 1
    if total <= 0:
        return 1.0
    return estimated / total


def _smooth(prev: Optional[float], raw: float, season_shift: bool) -> float:
    if prev is None or prev <= 0:
        return raw
    alpha = SMOOTH_ALPHA_SHIFT if season_shift else SMOOTH_ALPHA_NORMAL
    mixed = alpha * raw + (1.0 - alpha) * prev
    max_step = MAX_STEP_SHIFT if season_shift else MAX_STEP_NORMAL
    lo = prev * (1.0 - max_step)
    hi = prev * (1.0 + max_step)
    # Bei sehr kleinem prev absoluten Boden erlauben
    if prev < 0.5:
        lo = 0.0
        hi = max(hi, raw, 0.5)
    return max(lo, min(hi, mixed))


def compute_recommendation(base_dir: str, *, force: bool = False) -> Dict[str, Any]:
    """Berechnet/aktualisiert den empfohlenen Dauer-Zukauf in kWh/h."""
    path = _state_path(base_dir)
    today = date.today()

    with _lock:
        state = _load_state(path)
        days: List[Dict[str, Any]] = list(state.get("days") or [])
        _bootstrap_from_consumption_history(base_dir, days)
        _backfill_base_from_history(days, base_dir)
        _refresh_days_always_on(days, base_dir)

        # Heute ggf. aus Stats nachziehen (ohne zweite Datei-Runde außerhalb)
        stats = _load_today_24h(base_dir)
        if stats.get("date") == today.isoformat():
            gen = stats.get("generation_kwh") or {}
            con = stats.get("consumption_kwh") or {}
            pv = _f(gen.get("pv"))
            hydro = _f(gen.get("hydro"))
            netz = _f(con.get("netz"))
            base_kwh = _today_base_kwh(base_dir)
            _, always_on_kw, _ = _load_always_on_settings(base_dir)
            hours = max(1.0, datetime.now().hour + datetime.now().minute / 60.0)
            verbrauch = _verbrauch_for_always_on(base_kwh, always_on_kw, hours=hours)
            need = _day_need(verbrauch, pv, hydro, netz)
            _upsert_day(
                days,
                {
                    "date": today.isoformat(),
                    "base_kwh": round(base_kwh, 4),
                    "verbrauch_kwh": round(verbrauch, 4),
                    "pv_kwh": round(pv, 4),
                    "pv_source": "energy_stats_24h",
                    "hydro_kwh": round(hydro, 4),
                    "netz_kwh": round(netz, 4),
                    "deficit_kwh": round(max(0.0, verbrauch - pv - hydro), 4),
                    "need_kwh": round(need, 4),
                    "complete": False,
                    "source": "energy_stats_24h",
                },
            )

        season_shift, season_info = _detect_season_shift(days, today)
        win_start = today - timedelta(days=LOOKBACK_DAYS - 1)
        need, netz, pv, hydro, verbrauch, n = _window_sums(days, win_start, today)

        rec = dict(state.get("recommendation") or {})
        week_id = _iso_week_id(today)
        prev_week = rec.get("week_id")
        week_changed = prev_week != week_id

        if n < MIN_DAYS:
            always_on_count, always_on_kw, always_on_devices = _load_always_on_settings(
                base_dir
            )
            rec.update(
                {
                    "kwh_per_hour": rec.get("kwh_per_hour"),
                    "raw_kwh_per_hour": None,
                    "month_projected_kwh": None,
                    "week_id": week_id,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "season_shift": season_shift,
                    "days_used": n,
                    "basis": "zu_wenig_daten",
                    "season_info": season_info,
                    "always_on_count": always_on_count,
                    "always_on_kw": round(always_on_kw, 2),
                    "always_on_devices": [
                        {
                            "ip": d.get("ip"),
                            "power_kw": _f(d.get("power_kw")),
                            "priority": _f(d.get("priority")),
                        }
                        for d in always_on_devices
                    ],
                }
            )
            state["days"] = days
            state["recommendation"] = rec
            _save_state(path, state)
            return _public_payload(state)

        hours = float(n * 24)
        raw = need / hours if hours > 0 else 0.0
        # Wenn viele Tage nur geschätztes PV haben: mit echtem Monats-Netzbezug mischen
        est_share = _estimated_pv_share(days, win_start, today)
        mtd_rate = _mtd_netz_kwh_per_hour(base_dir)
        if mtd_rate is not None and est_share >= 0.4:
            # Je mehr Schätzung, desto stärker der Monatsanker
            w = min(0.75, 0.35 + 0.5 * est_share)
            raw = (1.0 - w) * raw + w * mtd_rate
        raw = max(0.0, round(raw, 4))

        # Nur einmal pro ISO-Woche den geglätteten Wert festschreiben,
        # außer Saisonwechsel oder force → schneller nachziehen
        should_update = force or week_changed or season_shift or rec.get("kwh_per_hour") is None
        prev = rec.get("kwh_per_hour")
        try:
            prev_f = float(prev) if prev is not None else None
        except (TypeError, ValueError):
            prev_f = None

        if should_update:
            smoothed = round(_smooth(prev_f, raw, season_shift), 3)
        else:
            # Innerhalb der Woche: raw live halten, Anzeige-Wert stabil lassen
            smoothed = round(prev_f, 3) if prev_f is not None else round(raw, 3)

        days_in_month = calendar.monthrange(today.year, today.month)[1]
        month_projected = round(smoothed * days_in_month * 24, 1)
        always_on_count, always_on_kw, always_on_devices = _load_always_on_settings(base_dir)

        rec.update(
            {
                "kwh_per_hour": smoothed,
                "raw_kwh_per_hour": raw,
                "month_projected_kwh": month_projected,
                "week_id": week_id,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "season_shift": season_shift,
                "days_used": n,
                "basis": "wochenhochrechnung",
                "week_need_kwh": round(need, 2),
                "week_netz_kwh": round(netz, 2),
                "week_pv_kwh": round(pv, 2),
                "week_hydro_kwh": round(hydro, 2),
                "week_verbrauch_kwh": round(verbrauch, 2),
                "days_in_month": days_in_month,
                "season_info": season_info,
                "week_changed": week_changed,
                "always_on_count": always_on_count,
                "always_on_kw": round(always_on_kw, 2),
                "always_on_devices": [
                    {
                        "ip": d.get("ip"),
                        "power_kw": _f(d.get("power_kw")),
                        "priority": _f(d.get("priority")),
                    }
                    for d in always_on_devices
                ],
            }
        )
        state["days"] = days
        state["recommendation"] = rec
        _save_state(path, state)
        return _public_payload(state)


def _public_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    rec = state.get("recommendation") or {}
    kwh_h = rec.get("kwh_per_hour")
    always_on_count = rec.get("always_on_count")
    always_on_kw = rec.get("always_on_kw")
    return {
        "kwh_per_hour": kwh_h,
        "raw_kwh_per_hour": rec.get("raw_kwh_per_hour"),
        "month_projected_kwh": rec.get("month_projected_kwh"),
        "unit": "kWh/h",
        "week_id": rec.get("week_id"),
        "updated_at": rec.get("updated_at"),
        "season_shift": bool(rec.get("season_shift")),
        "days_used": rec.get("days_used") or 0,
        "basis": rec.get("basis"),
        "week_need_kwh": rec.get("week_need_kwh"),
        "week_pv_kwh": rec.get("week_pv_kwh"),
        "week_hydro_kwh": rec.get("week_hydro_kwh"),
        "week_verbrauch_kwh": rec.get("week_verbrauch_kwh"),
        "always_on_count": always_on_count,
        "always_on_kw": always_on_kw,
        "always_on_devices": rec.get("always_on_devices") or [],
        "label": "Empfohlener Dauer-Zukauf",
        "hint": _hint_text(rec),
    }


def _hint_text(rec: Dict[str, Any]) -> str:
    if rec.get("basis") == "zu_wenig_daten":
        return "Noch zu wenig Tagesdaten (mind. 3 Tage)"
    parts = []
    count = rec.get("always_on_count")
    kw = rec.get("always_on_kw")
    if count is not None and kw is not None:
        parts.append(f"{count}×24h · {kw:g} kW")
    if rec.get("week_id"):
        parts.append(f"KW {str(rec['week_id']).split('-W')[-1]}")
    if rec.get("season_shift"):
        parts.append("Saisonwechsel erkannt – schneller angepasst")
    elif rec.get("days_used"):
        parts.append(f"Basis {rec['days_used']} Tage")
    proj = rec.get("month_projected_kwh")
    if proj is not None:
        parts.append(f"≈ {proj:g} kWh/Monat")
    return " · ".join(parts) if parts else "Wochenhochrechnung"


def get_recommended_grid_import(base_dir: str) -> Dict[str, Any]:
    """Für API/Dashboard: sync + berechnen."""
    try:
        sync_today_from_stats(base_dir)
    except Exception:
        pass
    try:
        return compute_recommendation(base_dir)
    except Exception as exc:
        return {
            "kwh_per_hour": None,
            "unit": "kWh/h",
            "label": "Empfohlener Dauer-Zukauf",
            "hint": "Berechnung fehlgeschlagen",
            "error": str(exc),
        }


def tick_recommended_grid_import(base_dir: str) -> None:
    """Periodischer Tick aus dem Energy-Stats-Loop."""
    try:
        sync_today_from_stats(base_dir)
        compute_recommendation(base_dir)
    except Exception:
        pass
