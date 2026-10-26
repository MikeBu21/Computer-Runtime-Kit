# -*- coding: utf-8 -*-
"""Verbrauchshistorie: Tagesarchiv, Wochentagsprofile, FIFO."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOURS_PER_DAY = 24
ARCHIVE_SCHEDULE_HOUR = 23
ARCHIVE_SCHEDULE_MINUTE = 50
# Default für Wochentags-Durchschnitt (Mo/Di/…), wenn Config fehlt.
# Konfigurierbar über consumption_profile_lookback_days.
# Das Tagesarchiv selbst bleibt bei consumption_history_days.
WEEKDAY_PROFILE_LOOKBACK_DAYS = 62


def resolve_profile_lookback_days(lookback_days: Optional[int] = None) -> int:
    """Effektives Fenster für Verbrauchs-Wochentagsmittel (7–365 Tage)."""
    if lookback_days is not None:
        try:
            n = int(lookback_days)
        except (TypeError, ValueError):
            n = WEEKDAY_PROFILE_LOOKBACK_DAYS
    else:
        try:
            from control_algorithm.config import load_config

            n = int(
                load_config().get("consumption_profile_lookback_days")
                or WEEKDAY_PROFILE_LOOKBACK_DAYS
            )
        except Exception:
            n = WEEKDAY_PROFILE_LOOKBACK_DAYS
    return max(7, min(365, n))


def _parse_day_date(day: Dict[str, Any]) -> Optional[date]:
    raw = str(day.get("date") or "")[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def days_for_weekday_profiles(
    days: List[Dict[str, Any]],
    *,
    reference: Optional[date] = None,
    lookback_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Archiv-Tage für Wochentagsmittel: nur innerhalb des Lookback-Fensters."""
    lookback = resolve_profile_lookback_days(lookback_days)
    ref = reference or date.today()
    cutoff = ref - timedelta(days=lookback)
    out: List[Dict[str, Any]] = []
    for d in days or []:
        dd = _parse_day_date(d)
        if dd is not None and dd >= cutoff:
            out.append(d)
    return out


def history_path(base_dir: str) -> Path:
    from control_algorithm.paths import energy_file

    return energy_file(base_dir, "consumption_history.json")


def empty_history() -> Dict[str, Any]:
    return {
        "days": [],
        "weekday_profiles": {str(w): _empty_weekday_profile() for w in range(7)},
    }


def _empty_weekday_profile() -> Dict[str, Any]:
    return {
        "hourly_base_kwh_avg": [0.0] * HOURS_PER_DAY,
        "hourly_hydro_kwh_avg": [0.0] * HOURS_PER_DAY,
        "hourly_verbrauch_pct_avg": [1.0 / HOURS_PER_DAY] * HOURS_PER_DAY,
        "hourly_base_pct_avg": [1.0 / HOURS_PER_DAY] * HOURS_PER_DAY,
        "sample_count": 0,
    }


def _hourly_fractions(values: List[float]) -> List[float]:
    """kWh pro Stunde → Anteil am Tageswert (0–1), Summe = 1."""
    total = sum(float(v) for v in values)
    if total <= 0:
        return [round(1.0 / HOURS_PER_DAY, 6)] * HOURS_PER_DAY
    return [round(float(v) / total, 6) for v in values]


def normalize_day_record(record: Dict[str, Any]) -> bool:
    """Ergänzt fehlende Grundverbrauch-Prozente in archivierten Tagen."""
    changed = False
    base = list(record.get("hourly_base_kwh") or [0.0] * HOURS_PER_DAY)
    if "total_base_kwh" not in record:
        record["total_base_kwh"] = round(sum(float(x) for x in base), 4)
        changed = True
    if "hourly_base_pct" not in record:
        record["hourly_base_pct"] = _hourly_fractions(base)
        changed = True
    return changed


def migrate_history_fields(history: Dict[str, Any]) -> bool:
    """Normalisiert ältere Archive (z. B. nur kWh ohne Stunden-%)."""
    changed = False
    for day in history.get("days") or []:
        if normalize_day_record(day):
            changed = True
    if changed:
        update_weekday_profiles(history)
    return changed


_history_cache: Dict[str, Any] = {
    "path": None,
    "mtime_ns": None,
    "size": None,
    "data": None,
}


def load_history(base_dir: str) -> Dict[str, Any]:
    """Lädt Verbrauchsarchiv – einmal cachen (pro Tick sonst 10k× JSON-Parse)."""
    path = history_path(base_dir)
    if not path.is_file():
        return empty_history()
    try:
        st = path.stat()
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        size = int(st.st_size)
        cache_path = str(path.resolve())
        if (
            _history_cache.get("path") == cache_path
            and _history_cache.get("mtime_ns") == mtime_ns
            and _history_cache.get("size") == size
            and isinstance(_history_cache.get("data"), dict)
        ):
            # Kopie vermeiden – Aufrufer mutieren selten; save_history invalidiert.
            return _history_cache["data"]
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty_history()
    data.setdefault("days", [])
    data.setdefault("weekday_profiles", {})
    for w in range(7):
        key = str(w)
        if key not in data["weekday_profiles"]:
            data["weekday_profiles"][key] = _empty_weekday_profile()
    if migrate_history_fields(data):
        save_history(base_dir, data)
        # save_history aktualisiert Cache
        return _history_cache.get("data") or data
    try:
        st = path.stat()
        _history_cache["path"] = str(path.resolve())
        _history_cache["mtime_ns"] = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        _history_cache["size"] = int(st.st_size)
        _history_cache["data"] = data
    except Exception:
        _history_cache["data"] = data
    return data


def save_history(base_dir: str, history: Dict[str, Any]) -> None:
    path = history_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        st = path.stat()
        _history_cache["path"] = str(path.resolve())
        _history_cache["mtime_ns"] = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        _history_cache["size"] = int(st.st_size)
        _history_cache["data"] = history
    except Exception:
        _history_cache["path"] = None
        _history_cache["data"] = None


def integrate_kw_by_hour(samples: List[Dict[str, Any]], key: str, default: float = 0.0) -> List[float]:
    """Integriert kW-Zeitreihe → kWh pro Stunde 0–23."""
    result = [0.0] * HOURS_PER_DAY
    if len(samples) < 2:
        return result
    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if m1 <= m0:
            continue
        dt_h = (m1 - m0) / 60.0
        if dt_h <= 0:
            continue
        kw0 = float(s0.get(key, default) or default)
        kw1 = float(s1.get(key, default) or default)
        avg_kw = (kw0 + kw1) / 2.0
        mid_m = (m0 + m1) / 2.0
        hour = int(mid_m // 60)
        if 0 <= hour < HOURS_PER_DAY:
            result[hour] += avg_kw * dt_h
    return [round(v, 4) for v in result]


def build_day_record(day_str: str, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Erzeugt Tagesdatensatz aus Minuten-Samples."""
    try:
        weekday = date.fromisoformat(day_str).weekday()
    except ValueError:
        weekday = 0
    hourly_verbrauch = integrate_kw_by_hour(samples, "betrieb")
    hourly_total, hourly_miner, hourly_base = integrate_base_miner_kwh_by_hour(samples)
    hourly_hydro = integrate_kw_by_hour(samples, "hydro_kw")
    total = round(sum(hourly_verbrauch), 4)
    total_base = round(sum(hourly_base), 4)
    hourly_pct = _hourly_fractions(hourly_verbrauch)
    hourly_base_pct = _hourly_fractions(hourly_base)
    return {
        "date": day_str,
        "weekday": weekday,
        "total_verbrauch_kwh": total,
        "total_base_kwh": total_base,
        "hourly_verbrauch_kwh": hourly_verbrauch,
        "hourly_verbrauch_pct": hourly_pct,
        "hourly_base_kwh": hourly_base,
        "hourly_base_pct": hourly_base_pct,
        "hourly_hydro_kwh": hourly_hydro,
    }


def update_weekday_profiles(
    history: Dict[str, Any],
    *,
    reference: Optional[date] = None,
    lookback_days: Optional[int] = None,
) -> None:
    """Berechnet Wochentags-Mittel aus dem konfigurierbaren Lookback-Fenster."""
    days = days_for_weekday_profiles(
        list(history.get("days") or []),
        reference=reference,
        lookback_days=lookback_days,
    )
    profiles: Dict[str, Dict[str, Any]] = {}
    for w in range(7):
        matching = [d for d in days if int(d.get("weekday", -1)) == w]
        if not matching:
            profiles[str(w)] = _empty_weekday_profile()
            continue
        n = len(matching)
        base_sum = [0.0] * HOURS_PER_DAY
        hydro_sum = [0.0] * HOURS_PER_DAY
        pct_sum = [0.0] * HOURS_PER_DAY
        base_pct_sum = [0.0] * HOURS_PER_DAY
        for d in matching:
            normalize_day_record(d)
            for h in range(HOURS_PER_DAY):
                base_sum[h] += float((d.get("hourly_base_kwh") or [0] * HOURS_PER_DAY)[h])
                hydro_sum[h] += float((d.get("hourly_hydro_kwh") or [0] * HOURS_PER_DAY)[h])
                pct_sum[h] += float((d.get("hourly_verbrauch_pct") or [0] * HOURS_PER_DAY)[h])
                base_pct_sum[h] += float((d.get("hourly_base_pct") or [0] * HOURS_PER_DAY)[h])
        lookback = resolve_profile_lookback_days(lookback_days)
        profiles[str(w)] = {
            "hourly_base_kwh_avg": [round(v / n, 4) for v in base_sum],
            "hourly_hydro_kwh_avg": [round(v / n, 4) for v in hydro_sum],
            "hourly_verbrauch_pct_avg": [round(v / n, 6) for v in pct_sum],
            "hourly_base_pct_avg": [round(v / n, 6) for v in base_pct_sum],
            "sample_count": n,
            "lookback_days": lookback,
        }
    history["weekday_profiles"] = profiles

def rebuild_weekday_profiles(
    base_dir: str,
    *,
    lookback_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Wochentagsprofile neu berechnen (z. B. nach Config-Änderung) und speichern."""
    history = load_history(base_dir)
    lookback = resolve_profile_lookback_days(lookback_days)
    update_weekday_profiles(history, lookback_days=lookback)
    save_history(base_dir, history)
    return {
        "ok": True,
        "lookback_days": lookback,
        "archived_days": len(history.get("days") or []),
        "profiles": {
            k: int((v or {}).get("sample_count") or 0)
            for k, v in (history.get("weekday_profiles") or {}).items()
        },
    }


def trim_fifo(history: Dict[str, Any], max_days: int) -> None:
    """Entfernt älteste Tage über dem Limit."""
    days: List[Dict[str, Any]] = list(history.get("days") or [])
    if max_days < 1:
        max_days = 1
    if len(days) > max_days:
        days.sort(key=lambda d: str(d.get("date", "")))
        history["days"] = days[-max_days:]


def archive_day_from_timeseries(
    base_dir: str,
    day_str: str,
    samples: List[Dict[str, Any]],
    max_days: Optional[int] = None,
) -> bool:
    """Archiviert einen Tag (überspringt Duplikate)."""
    if not day_str or not samples:
        return False
    history = load_history(base_dir)
    existing = {str(d.get("date")) for d in history.get("days") or []}
    if day_str in existing:
        return False
    record = build_day_record(day_str, samples)
    if record["total_verbrauch_kwh"] <= 0:
        return False
    history.setdefault("days", []).append(record)
    history["days"].sort(key=lambda d: str(d.get("date", "")))
    if max_days is None:
        try:
            from control_algorithm.config import load_config

            max_days = int(load_config().get("consumption_history_days") or 60)
        except Exception:
            max_days = 60
    trim_fifo(history, max_days)
    update_weekday_profiles(history)
    save_history(base_dir, history)
    return True


def maybe_archive_timeseries_on_rollover(base_dir: str, prev_date: str) -> None:
    """Fallback-Archivierung beim Stats-Rollover."""
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
        if ts.get("date") == prev_date and ts.get("samples"):
            maybe_archive_daily_consumption(
                base_dir,
                samples=ts.get("samples") or [],
                day_str=str(prev_date),
            )
    except Exception:
        pass


def maybe_archive_daily_consumption(
    base_dir: str,
    now: Optional[datetime] = None,
    *,
    samples: Optional[List[Dict[str, Any]]] = None,
    day_str: Optional[str] = None,
) -> bool:
    """
    Archiviert Tages-Verbrauch in consumption_history.json:
    - ab 23:50 Uhr am laufenden Tag (vor Mitternacht-Reset der Zeitreihe)
    - sofort wenn das Zeitreihen-Datum älter als heute ist (Nachhol-Archiv)
    """
    now = now or datetime.now()
    today = now.date().isoformat()

    if day_str is None or samples is None:
        try:
            from energy_timeseries_24h import load_timeseries_state

            ts = load_timeseries_state(base_dir)
            day_str = ts.get("date")
            samples = ts.get("samples") or []
        except Exception:
            return False

    if not day_str or len(samples) < 2:
        return False

    history = load_history(base_dir)
    if day_str in {str(d.get("date")) for d in history.get("days") or []}:
        return False

    stale = day_str < today
    scheduled = day_str == today and (
        now.hour > ARCHIVE_SCHEDULE_HOUR
        or (now.hour == ARCHIVE_SCHEDULE_HOUR and now.minute >= ARCHIVE_SCHEDULE_MINUTE)
    )
    if not (stale or scheduled):
        return False

    return archive_day_from_timeseries(base_dir, day_str, samples)


def _extrapolate_hourly_full_day(
    hourly: List[float],
    *,
    now_hour: Optional[int] = None,
) -> List[float]:
    """
    Füllt fehlende Stunden (noch nicht gemessen) mit dem Mittel der
    bisherigen Stunden > 0 – damit Live-Profile als Ganztagsschätzung taugen.
    """
    out = [float(x or 0) for x in (hourly or [0.0] * HOURS_PER_DAY)]
    while len(out) < HOURS_PER_DAY:
        out.append(0.0)
    out = out[:HOURS_PER_DAY]
    cutoff = HOURS_PER_DAY if now_hour is None else max(0, min(HOURS_PER_DAY - 1, int(now_hour)))
    past = [out[h] for h in range(0, cutoff + 1) if out[h] > 0.01]
    if not past:
        return out
    avg = sum(past) / len(past)
    for h in range(cutoff + 1, HOURS_PER_DAY):
        if out[h] <= 0.01:
            out[h] = round(avg, 4)
    return out


def _today_live_profile(base_dir: str) -> Optional[Dict[str, Any]]:
    """Heutige Tageskurve als Profil-Fallback wenn Archiv leer (auf 24 h extrapoliert)."""
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
        if ts.get("date") != date.today().isoformat():
            return None
        samples = ts.get("samples") or []
        if len(samples) < 2:
            return None
        record = build_day_record(date.today().isoformat(), samples)
        if record["total_verbrauch_kwh"] <= 0:
            return None
        now_hour = datetime.now().hour
        hourly_base = _extrapolate_hourly_full_day(
            list(record["hourly_base_kwh"]), now_hour=now_hour
        )
        hourly_hydro = _extrapolate_hourly_full_day(
            list(record["hourly_hydro_kwh"]), now_hour=now_hour
        )
        hourly_verbr = _extrapolate_hourly_full_day(
            list(record["hourly_verbrauch_kwh"]), now_hour=now_hour
        )
        return {
            "hourly_base_kwh_avg": hourly_base,
            "hourly_hydro_kwh_avg": hourly_hydro,
            "hourly_verbrauch_pct_avg": _hourly_fractions(hourly_verbr),
            "hourly_base_pct_avg": _hourly_fractions(hourly_base),
            "sample_count": 1,
        }
    except Exception:
        return None


def seed_today_consumption_history(base_dir: str) -> Dict[str, Any]:
    """
    Schreibt/aktualisiert den heutigen Tag in consumption_history.json
    (extrapolierte 24h-Kurve), damit ab sofort Wochentagsprofile und Planung greifen.
    """
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    day_str = str(ts.get("date") or "")
    samples = list(ts.get("samples") or [])
    if day_str != date.today().isoformat() or len(samples) < 2:
        return {"ok": False, "error": "Keine ausreichende Tages-Zeitreihe für heute."}

    record = build_day_record(day_str, samples)
    now_hour = datetime.now().hour
    record["hourly_base_kwh"] = _extrapolate_hourly_full_day(
        list(record["hourly_base_kwh"]), now_hour=now_hour
    )
    record["hourly_hydro_kwh"] = _extrapolate_hourly_full_day(
        list(record["hourly_hydro_kwh"]), now_hour=now_hour
    )
    record["hourly_verbrauch_kwh"] = _extrapolate_hourly_full_day(
        list(record["hourly_verbrauch_kwh"]), now_hour=now_hour
    )
    record["total_base_kwh"] = round(sum(record["hourly_base_kwh"]), 4)
    record["total_verbrauch_kwh"] = round(sum(record["hourly_verbrauch_kwh"]), 4)
    record["hourly_verbrauch_pct"] = _hourly_fractions(record["hourly_verbrauch_kwh"])
    record["hourly_base_pct"] = _hourly_fractions(record["hourly_base_kwh"])
    record["seeded"] = True
    record["seeded_at"] = datetime.now().isoformat(timespec="seconds")

    history = load_history(base_dir)
    days = [d for d in (history.get("days") or []) if str(d.get("date")) != day_str]
    days.append(record)
    days.sort(key=lambda d: str(d.get("date", "")))
    history["days"] = days
    try:
        from control_algorithm.config import load_config

        max_days = int(load_config().get("consumption_history_days") or 60)
    except Exception:
        max_days = 60
    trim_fifo(history, max_days)
    update_weekday_profiles(history)
    save_history(base_dir, history)
    return {
        "ok": True,
        "date": day_str,
        "total_base_kwh": record["total_base_kwh"],
        "total_verbrauch_kwh": record["total_verbrauch_kwh"],
        "archived_days": len(history.get("days") or []),
    }


def get_weekday_profile(
    history: Dict[str, Any],
    weekday: int,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Profil für Wochentag; Fallback auf Lookback-Fenster wenn sample_count=0."""
    profiles = history.get("weekday_profiles") or {}
    prof = dict(profiles.get(str(weekday)) or _empty_weekday_profile())
    if int(prof.get("sample_count") or 0) > 0:
        return prof
    days = days_for_weekday_profiles(history.get("days") or [])
    if not days:
        if base_dir:
            live = _today_live_profile(base_dir)
            if live:
                return live
        return prof
    base_sum = [0.0] * HOURS_PER_DAY
    hydro_sum = [0.0] * HOURS_PER_DAY
    pct_sum = [0.0] * HOURS_PER_DAY
    base_pct_sum = [0.0] * HOURS_PER_DAY
    for d in days:
        normalize_day_record(d)
        for h in range(HOURS_PER_DAY):
            base_sum[h] += float((d.get("hourly_base_kwh") or [0] * HOURS_PER_DAY)[h])
            hydro_sum[h] += float((d.get("hourly_hydro_kwh") or [0] * HOURS_PER_DAY)[h])
            pct_sum[h] += float((d.get("hourly_verbrauch_pct") or [0] * HOURS_PER_DAY)[h])
            base_pct_sum[h] += float((d.get("hourly_base_pct") or [0] * HOURS_PER_DAY)[h])
    n = len(days)
    return {
        "hourly_base_kwh_avg": [round(v / n, 4) for v in base_sum],
        "hourly_hydro_kwh_avg": [round(v / n, 4) for v in hydro_sum],
        "hourly_verbrauch_pct_avg": [round(v / n, 6) for v in pct_sum],
        "hourly_base_pct_avg": [round(v / n, 6) for v in base_pct_sum],
        "sample_count": n,
        "lookback_days": resolve_profile_lookback_days(),
    }


def _hour_fraction_in_range(hour: int, start_minute: float, end_minute: float) -> float:
    """Anteil der Stunde [hour] im Intervall [start_minute, end_minute) (Minuten seit Mitternacht)."""
    h_start = hour * 60.0
    h_end = h_start + 60.0
    overlap_start = max(h_start, start_minute)
    overlap_end = min(h_end, end_minute)
    if overlap_end <= overlap_start:
        return 0.0
    return (overlap_end - overlap_start) / 60.0


def expected_base_consumption_kwh(
    base_dir: str,
    start: datetime,
    end: datetime,
) -> float:
    """Erwarteter Grundverbrauch (ohne Miner) zwischen start und end."""
    if end <= start:
        return 0.0
    history = load_history(base_dir)
    start_m = start.hour * 60 + start.minute + start.second / 60.0
    end_m = end.hour * 60 + end.minute + end.second / 60.0
    total = 0.0

    # Teil 1: Rest des Starttages
    if start.date() == end.date():
        prof = get_weekday_profile(history, start.weekday(), base_dir)
        hourly = prof.get("hourly_base_kwh_avg") or [0.0] * HOURS_PER_DAY
        for h in range(HOURS_PER_DAY):
            frac = _hour_fraction_in_range(h, start_m, end_m)
            if frac > 0:
                total += float(hourly[h]) * frac
        return round(total, 4)

    for h in range(HOURS_PER_DAY):
        if h > start.hour or (h == start.hour and start.minute > 0):
            frac = _hour_fraction_in_range(h, start_m, 24 * 60.0)
            if frac > 0:
                prof = get_weekday_profile(history, start.weekday(), base_dir)
                hourly = prof.get("hourly_base_kwh_avg") or [0.0] * HOURS_PER_DAY
                total += float(hourly[h]) * frac

    # Volle Zwischentage
    d = start.date() + timedelta(days=1)
    while d < end.date():
        prof = get_weekday_profile(history, d.weekday(), base_dir)
        hourly = prof.get("hourly_base_kwh_avg") or [0.0] * HOURS_PER_DAY
        total += sum(float(x) for x in hourly)
        d += timedelta(days=1)

    # Anfang des Endtages
    prof = get_weekday_profile(history, end.weekday(), base_dir)
    hourly = prof.get("hourly_base_kwh_avg") or [0.0] * HOURS_PER_DAY
    for h in range(HOURS_PER_DAY):
        frac = _hour_fraction_in_range(h, 0.0, end_m)
        if frac > 0:
            total += float(hourly[h]) * frac
    return round(total, 4)


def expected_hydro_production_kwh(
    base_dir: str,
    start: datetime,
    end: datetime,
) -> float:
    """Erwartete Wasserkraft-Erzeugung aus historischem Wochentagsprofil."""
    if end <= start:
        return 0.0
    history = load_history(base_dir)
    start_m = start.hour * 60 + start.minute + start.second / 60.0
    end_m = end.hour * 60 + end.minute + end.second / 60.0
    total = 0.0

    if start.date() == end.date():
        prof = get_weekday_profile(history, start.weekday(), base_dir)
        hourly = prof.get("hourly_hydro_kwh_avg") or [0.0] * HOURS_PER_DAY
        for h in range(HOURS_PER_DAY):
            frac = _hour_fraction_in_range(h, start_m, end_m)
            if frac > 0:
                total += float(hourly[h]) * frac
        return round(total, 4)

    for h in range(HOURS_PER_DAY):
        frac = _hour_fraction_in_range(h, start_m, 24 * 60.0)
        if frac > 0:
            prof = get_weekday_profile(history, start.weekday(), base_dir)
            hourly = prof.get("hourly_hydro_kwh_avg") or [0.0] * HOURS_PER_DAY
            total += float(hourly[h]) * frac

    d = start.date() + timedelta(days=1)
    while d < end.date():
        prof = get_weekday_profile(history, d.weekday(), base_dir)
        hourly = prof.get("hourly_hydro_kwh_avg") or [0.0] * HOURS_PER_DAY
        total += sum(float(x) for x in hourly)
        d += timedelta(days=1)

    prof = get_weekday_profile(history, end.weekday(), base_dir)
    hourly = prof.get("hourly_hydro_kwh_avg") or [0.0] * HOURS_PER_DAY
    for h in range(HOURS_PER_DAY):
        frac = _hour_fraction_in_range(h, 0.0, end_m)
        if frac > 0:
            total += float(hourly[h]) * frac
    return round(total, 4)


def expected_hydro_full_day_kwh(base_dir: str, target_date: date) -> float:
    """Erwartete Wasserkraft-Erzeugung für einen vollen Kalendertag (00:00–24:00)."""
    start = datetime.combine(target_date, datetime.min.time())
    end = start + timedelta(days=1)
    return expected_hydro_production_kwh(base_dir, start, end)


def expected_base_full_day_kwh(base_dir: str, target_date: date) -> float:
    """Erwarteter Grundverbrauch (ohne Miner) für einen vollen Kalendertag."""
    start = datetime.combine(target_date, datetime.min.time())
    end = start + timedelta(days=1)
    return expected_base_consumption_kwh(base_dir, start, end)


def weekday_base_consumption_detail(base_dir: str, target_date: date) -> Dict[str, Any]:
    """Grundverbrauch-Profil für einen Wochentag inkl. Archiv-Quellen für die UI."""
    history = load_history(base_dir)
    weekday = target_date.weekday()
    prof = get_weekday_profile(history, weekday, base_dir)
    recent_days = days_for_weekday_profiles(
        history.get("days") or [], reference=target_date
    )
    matching = sorted(
        [d for d in recent_days if int(d.get("weekday", -1)) == weekday],
        key=lambda d: str(d.get("date", "")),
        reverse=True,
    )
    sample_dates = [str(d.get("date")) for d in matching if d.get("date")]
    sample_count = int(prof.get("sample_count") or 0)
    total = round(expected_base_full_day_kwh(base_dir, target_date), 2)
    lookback = int(prof.get("lookback_days") or WEEKDAY_PROFILE_LOOKBACK_DAYS)

    if sample_dates:
        formatted = []
        for ds in sample_dates[:6]:
            parts = ds.split("-")
            if len(parts) == 3:
                formatted.append(f"{parts[2]}.{parts[1]}.")
            else:
                formatted.append(ds)
        extra = f" (+{len(sample_dates) - 6} weitere)" if len(sample_dates) > 6 else ""
        source_note = (
            f"Durchschnitt aus {sample_count} {WEEKDAY_NAMES[weekday]}-Tag(en) "
            f"der letzten ~{lookback} Tage: "
            f"{', '.join(formatted)}{extra}"
        )
    elif sample_count > 0:
        source_note = (
            f"Durchschnitt aus {sample_count} Tagen der letzten ~{lookback} Tage "
            f"(Fallback, kein reines {WEEKDAY_NAMES[weekday]}-Profil)"
        )
    else:
        source_note = f"Keine Archiv-Tage – Schätzung aus Live-Profil ({WEEKDAY_NAMES[weekday]})"

    return {
        "weekday": weekday,
        "weekday_name": WEEKDAY_NAMES[weekday],
        "sample_count": sample_count,
        "sample_dates": sample_dates,
        "lookback_days": lookback,
        "total_kwh": total,
        "source_note": source_note,
    }


def _integrate_actual_base_kwh(
    samples: List[Dict[str, Any]],
    start_m: float,
    end_m: float,
) -> float:
    """Ist-Grundverbrauch (betrieb − miner) zwischen start_m und end_m (Minuten seit Mitternacht)."""
    actual = 0.0
    if len(samples) < 2:
        return actual
    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if m1 <= m0 or m1 < start_m:
            continue
        clip_m0 = max(m0, start_m)
        clip_m1 = min(m1, end_m)
        if clip_m1 <= clip_m0:
            continue
        dt_h = (clip_m1 - clip_m0) / 60.0
        betrieb0 = _sample_betrieb_kw(s0)
        betrieb1 = _sample_betrieb_kw(s1)
        miner0 = float(s0.get("miner_kw") or 0)
        miner1 = float(s1.get("miner_kw") or 0)
        base0 = max(0.0, betrieb0 - miner0)
        base1 = max(0.0, betrieb1 - miner1)
        actual += (base0 + base1) / 2.0 * dt_h
    return actual


def _integrate_actual_miner_kwh(
    samples: List[Dict[str, Any]],
    start_m: float,
    end_m: float,
) -> float:
    """Ist-Miner-Verbrauch (miner_kw) zwischen start_m und end_m (Minuten seit Mitternacht)."""
    actual = 0.0
    if len(samples) < 2:
        return actual
    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if m1 <= m0 or m1 < start_m:
            continue
        clip_m0 = max(m0, start_m)
        clip_m1 = min(m1, end_m)
        if clip_m1 <= clip_m0:
            continue
        dt_h = (clip_m1 - clip_m0) / 60.0
        miner0 = float(s0.get("miner_kw") or 0)
        miner1 = float(s1.get("miner_kw") or 0)
        actual += (miner0 + miner1) / 2.0 * dt_h
    return actual


def _window_samples_and_bounds(
    base_dir: str,
    window_hours: float,
    now: datetime,
) -> Tuple[List[Dict[str, Any]], float, float]:
    """Lädt Tages-Samples und Minuten-Grenzen für ein rollierendes Fenster."""
    start = now - timedelta(hours=window_hours)
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
        samples = list(ts.get("samples") or [])
        if ts.get("date") != now.date().isoformat():
            samples = []
    except Exception:
        samples = []

    start_m = start.hour * 60 + start.minute + start.second / 60.0
    if start.date() < now.date():
        start_m = 0.0
    now_m = now.hour * 60 + now.minute + now.second / 60.0
    return samples, start_m, now_m


def _integrate_actual_betrieb_kwh(
    samples: List[Dict[str, Any]],
    start_m: float,
    end_m: float,
) -> float:
    """Ist-Gesamtverbrauch (betrieb) zwischen start_m und end_m (Minuten seit Mitternacht)."""
    actual = 0.0
    if len(samples) < 2:
        return actual
    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if m1 <= m0 or m1 < start_m:
            continue
        clip_m0 = max(m0, start_m)
        clip_m1 = min(m1, end_m)
        if clip_m1 <= clip_m0:
            continue
        dt_h = (clip_m1 - clip_m0) / 60.0
        betrieb0 = _sample_betrieb_kw(s0)
        betrieb1 = _sample_betrieb_kw(s1)
        actual += (betrieb0 + betrieb1) / 2.0 * dt_h
    return actual


def actual_base_consumption_kwh(
    base_dir: str,
    start: datetime,
    end: datetime,
) -> float:
    """Ist-Grundverbrauch (betrieb − miner) zwischen start und end."""
    if end <= start:
        return 0.0
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
        samples = list(ts.get("samples") or [])
        if ts.get("date") != end.date().isoformat():
            samples = []
    except Exception:
        samples = []

    start_m = start.hour * 60 + start.minute + start.second / 60.0
    if start.date() < end.date():
        start_m = 0.0
    end_m = end.hour * 60 + end.minute + end.second / 60.0
    return round(_integrate_actual_base_kwh(samples, start_m, end_m), 4)


def actual_total_consumption_kwh(
    base_dir: str,
    start: datetime,
    end: datetime,
) -> float:
    """Ist-Gesamtverbrauch (betrieb, inkl. Miner) zwischen start und end."""
    if end <= start:
        return 0.0
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
        samples = list(ts.get("samples") or [])
        if ts.get("date") != end.date().isoformat():
            samples = []
    except Exception:
        samples = []

    start_m = start.hour * 60 + start.minute + start.second / 60.0
    if start.date() < end.date():
        start_m = 0.0
    end_m = end.hour * 60 + end.minute + end.second / 60.0
    return round(_integrate_actual_betrieb_kwh(samples, start_m, end_m), 4)


def integrate_base_consumption_window(
    base_dir: str,
    window_hours: float,
    now: Optional[datetime] = None,
) -> Tuple[float, float]:
    """
    Integriert Ist-Grundverbrauch (betrieb − miner) der letzten window_hours.
    Returns (actual_kwh, expected_kwh).

    Nach Mitternacht schrumpft das Fenster auf „seit Tagesbeginn“, weil die
    Live-Zeitreihe keine Vortags-Samples enthält.
    """
    now = now or datetime.now()
    start = now - timedelta(hours=window_hours)
    if start.date() < now.date():
        compare_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        compare_start = start
    actual = actual_base_consumption_kwh(base_dir, compare_start, now)
    expected = expected_base_consumption_kwh(base_dir, compare_start, now)
    return round(actual, 4), round(expected, 4)


def integrate_miner_consumption_window(
    base_dir: str,
    window_hours: float,
    now: Optional[datetime] = None,
) -> float:
    """Integriert Ist-Miner-Verbrauch (miner_kw) der letzten window_hours."""
    now = now or datetime.now()
    samples, start_m, now_m = _window_samples_and_bounds(base_dir, window_hours, now)
    return round(_integrate_actual_miner_kwh(samples, start_m, now_m), 4)


def _parse_iso_naive(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _miner_run_start_in_window(
    ip: str,
    window_start: datetime,
    device_runtime: Dict[str, Any],
    steering_store: Optional[Dict[str, Any]],
) -> datetime:
    """Frühester bekannter Laufbeginn eines Miners innerhalb des Anomalie-Fensters."""
    run_start = window_start
    rt = device_runtime.get(ip) or {}
    resumed_at = _parse_iso_naive(rt.get("resumed_at"))
    if resumed_at is not None:
        return max(window_start, resumed_at)

    if steering_store:
        open_by_ip: Dict[str, int] = dict(steering_store.get("open_by_ip") or {})
        sessions: List[Dict[str, Any]] = list(steering_store.get("sessions") or [])
        idx = open_by_ip.get(ip)
        if idx is not None and 0 <= idx < len(sessions):
            session_resumed = _parse_iso_naive(sessions[idx].get("resumed_at"))
            if session_resumed is not None:
                return max(window_start, session_resumed)
    return run_start


def _device_power_kw_for_anomaly(
    device: Dict[str, Any],
    state: Optional[Dict[str, Any]],
) -> float:
    """Gemessene Leistung bevorzugen, sonst Config power_kw."""
    ip = str(device.get("ip") or "")
    if state:
        dpm = ((state.get("device_power_measure") or {}).get("by_ip") or {}).get(ip) or {}
        for key in ("measured_kw", "measured_power_kw", "last_raw_kw"):
            try:
                mv = float(dpm.get(key))
                if mv > 0:
                    return mv
            except (TypeError, ValueError):
                pass
    for key in ("measured_power_kw", "power_kw"):
        try:
            mv = float(device.get(key) or 0)
            if mv > 0:
                return mv
        except (TypeError, ValueError):
            pass
    return 0.0


def _merge_time_intervals(
    intervals: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda x: x[0])
    merged: List[Tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _pause_intervals_in_window(
    ip: str,
    window_start: datetime,
    now: datetime,
    device_runtime: Dict[str, Any],
    steering_store: Optional[Dict[str, Any]],
) -> List[Tuple[datetime, datetime]]:
    """Pause-Intervalle (Aus) des Geräts, geschnitten auf [window_start, now]."""
    intervals: List[Tuple[datetime, datetime]] = []
    sessions: List[Dict[str, Any]] = []
    if steering_store:
        sessions = list(steering_store.get("sessions") or [])

    for s in sessions:
        if str(s.get("ip") or "") != str(ip):
            continue
        if s.get("resume_only"):
            continue
        paused_at = _parse_iso_naive(s.get("paused_at"))
        if paused_at is None:
            continue
        resumed_at = _parse_iso_naive(s.get("resumed_at"))
        pause_end = resumed_at if resumed_at is not None else now
        if pause_end <= window_start or paused_at >= now:
            continue
        intervals.append((max(paused_at, window_start), min(pause_end, now)))

    rt = device_runtime.get(str(ip)) or {}
    rt_paused = _parse_iso_naive(rt.get("paused_at"))
    rt_resumed = _parse_iso_naive(rt.get("resumed_at"))
    # Offene Runtime-Pause (noch kein Resume), falls History sie noch nicht hat
    if rt_paused is not None and rt_resumed is None:
        if rt_paused < now and (not intervals or intervals[-1][1] < now):
            if rt_paused < now and now > window_start:
                start = max(rt_paused, window_start)
                if start < now:
                    intervals.append((start, now))

    return _merge_time_intervals(intervals)


def _on_hours_in_anomaly_window(
    ip: str,
    window_start: datetime,
    now: datetime,
    *,
    miner_status: Dict[str, str],
    device_runtime: Dict[str, Any],
    steering_store: Optional[Dict[str, Any]],
) -> float:
    """
    Laufstunden eines Algo-Geräts im Fenster.

    Pausierte Geräte zählen mit, solange sie im Fenster eingeschaltet waren.
    Pause-Sessions aus dem Steuerungsprotokoll werden als Aus-Zeiten abgezogen.
    """
    if now <= window_start:
        return 0.0

    pauses = _pause_intervals_in_window(
        ip, window_start, now, device_runtime, steering_store
    )
    status = str(miner_status.get(ip) or "").lower()
    rt = device_runtime.get(str(ip)) or {}
    resumed_at = _parse_iso_naive(rt.get("resumed_at"))
    paused_at = _parse_iso_naive(rt.get("paused_at"))

    # Keine Pause-Evidenz: nur aktuelle/letzte Laufphase
    if not pauses:
        if status == "running":
            start = max(window_start, resumed_at or window_start)
            return max(0.0, (now - start).total_seconds() / 3600.0)
        if paused_at is not None and paused_at > window_start:
            start = window_start
            if resumed_at is not None and resumed_at < paused_at:
                start = max(window_start, resumed_at)
            end = min(paused_at, now)
            return max(0.0, (end - start).total_seconds() / 3600.0)
        return 0.0

    # Mit Pause-Historie: Fenster abzüglich Pause-Intervalle = ON
    # (Algo-gesteuerte Geräte sind außerhalb expliziter Pausen als laufend zu werten)
    window_h = (now - window_start).total_seconds() / 3600.0
    off_h = sum((e - s).total_seconds() / 3600.0 for s, e in pauses)
    on_h = max(0.0, window_h - off_h)

    # Wenn am Fensterbeginn bereits eine offene Pause lag und Gerät nie resumed wurde:
    # on_h ist bereits korrekt (nur Lücken zwischen Pausen).
    # Wenn aktuell running und resumed_at nach letzter Pause: ebenfalls in on_h enthalten.
    if status == "running" and resumed_at is not None and resumed_at > window_start:
        # Untergrenze: mindestens die aktuelle Laufphase
        min_on = max(0.0, (now - max(window_start, resumed_at)).total_seconds() / 3600.0)
        on_h = max(on_h, min_on)
    return on_h


def algo_attributed_miner_kwh(
    config: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    window_hours: float,
    now: Optional[datetime] = None,
    state: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Algo-Miner-Verbrauch im Anomalie-Fenster (Leistung × ON-Stunden).

    Zählt jedes enabled Steuerungsgerät – auch wenn es inzwischen pausiert ist –
    über alle ON-Intervalle im Fenster. Eigene Einschaltungen dürfen die
    Grundverbrauchs-Anomalie nicht auslösen.
    """
    from control_algorithm.planner import enabled_devices

    now = now or datetime.now()
    window_start = now - timedelta(hours=window_hours)
    if window_start.date() < now.date():
        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    devices = enabled_devices(config)
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    device_runtime: Dict[str, Any] = dict((state or {}).get("device_runtime") or {})

    steering_store: Optional[Dict[str, Any]] = None
    try:
        from control_algorithm.steering_history import load_steering_history

        steering_store = load_steering_history(base_dir)
    except Exception:
        steering_store = None

    total = 0.0
    for d in devices:
        ip = str(d.get("ip") or "")
        pwr = _device_power_kw_for_anomaly(d, state)
        if not ip or pwr <= 0:
            continue
        hours = _on_hours_in_anomaly_window(
            ip,
            window_start,
            now,
            miner_status=miner_status,
            device_runtime=device_runtime,
            steering_store=steering_store,
        )
        if hours <= 0:
            continue
        total += pwr * hours
    return round(total, 4)


def corrected_base_consumption_for_anomaly(
    config: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    window_hours: float,
    now: Optional[datetime] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float]:
    """
    Grundverbrauch für Anomalie-Prüfung.

    Miner-Anteil wird als max(gemessener miner_kw, Algo-Zuschreibung Leistung×ON-Stunden)
    vom Gesamtverbrauch abgezogen. Alle vom Steuerungsalgo geschalteten Geräte
    (auch inzwischen pausierte) zählen nicht zum Grundverbrauch.

    Returns (actual_kwh, expected_kwh).
    """
    now = now or datetime.now()
    _, expected = integrate_base_consumption_window(base_dir, window_hours, now)
    if expected <= 0.1:
        actual, _ = integrate_base_consumption_window(base_dir, window_hours, now)
        return actual, expected

    samples, start_m, now_m = _window_samples_and_bounds(base_dir, window_hours, now)
    total_betrieb = _integrate_actual_betrieb_kwh(samples, start_m, now_m)
    recorded_miner = _integrate_actual_miner_kwh(samples, start_m, now_m)
    algo_miner = algo_attributed_miner_kwh(
        config, live, base_dir, window_hours, now, state
    )
    attributed_miner = max(recorded_miner, algo_miner)
    actual = max(0.0, total_betrieb - attributed_miner)
    return round(actual, 4), expected


def profile_sample_count(base_dir: str, weekday: Optional[int] = None) -> int:
    history = load_history(base_dir)
    if weekday is None:
        return len(history.get("days") or [])
    prof = get_weekday_profile(history, weekday)
    return int(prof.get("sample_count") or 0)


def archived_history_days(base_dir: str) -> int:
    """Anzahl vollständig archivierter Tage (ohne Live-Fallback)."""
    return len(load_history(base_dir).get("days") or [])


WEEKDAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def _sample_betrieb_kw(sample: Dict[str, Any]) -> float:
    """Betrieb/Last kW aus Timeseries-Punkt (Fallback auf Dashboard-Linie)."""
    try:
        betrieb = float(sample.get("betrieb") or 0)
    except (TypeError, ValueError):
        betrieb = 0.0
    if betrieb > 0.05:
        return betrieb
    for key in ("line_verbrauch", "line_last", "line_wohnhaus"):
        try:
            val = float(sample.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0.05:
            return val
    return max(0.0, betrieb)


def _today_timeseries_samples(base_dir: str) -> List[Dict[str, Any]]:
    try:
        from energy_timeseries_24h import load_timeseries_state

        ts = load_timeseries_state(base_dir)
        if ts.get("date") != date.today().isoformat():
            return []
        return list(ts.get("samples") or [])
    except Exception:
        return []


def integrate_base_miner_kwh_by_hour(
    samples: List[Dict[str, Any]],
) -> Tuple[List[float], List[float], List[float]]:
    """
    Segmentweise Integration → kWh pro Stunde für Gesamt, Miner und Grundverbrauch.
    Grundverbrauch = max(0, betrieb − miner_kw) je Messintervall (nicht nachträglich abziehen).
    """
    total = [0.0] * HOURS_PER_DAY
    miner = [0.0] * HOURS_PER_DAY
    base = [0.0] * HOURS_PER_DAY
    if len(samples) < 2:
        return (
            [round(v, 4) for v in total],
            [round(v, 4) for v in miner],
            [round(v, 4) for v in base],
        )
    for i in range(1, len(samples)):
        s0, s1 = samples[i - 1], samples[i]
        m0 = float(s0.get("m") or 0)
        m1 = float(s1.get("m") or 0)
        if m1 <= m0:
            continue
        dt_h = (m1 - m0) / 60.0
        if dt_h <= 0:
            continue
        betrieb0 = _sample_betrieb_kw(s0)
        betrieb1 = _sample_betrieb_kw(s1)
        miner0 = float(s0.get("miner_kw") or 0)
        miner1 = float(s1.get("miner_kw") or 0)
        base0 = max(0.0, betrieb0 - miner0)
        base1 = max(0.0, betrieb1 - miner1)
        avg_total = (betrieb0 + betrieb1) / 2.0
        avg_miner = (miner0 + miner1) / 2.0
        avg_base = (base0 + base1) / 2.0
        mid_m = (m0 + m1) / 2.0
        hour = int(mid_m // 60)
        if 0 <= hour < HOURS_PER_DAY:
            total[hour] += avg_total * dt_h
            miner[hour] += avg_miner * dt_h
            base[hour] += avg_base * dt_h
    return (
        [round(v, 4) for v in total],
        [round(v, 4) for v in miner],
        [round(v, 4) for v in base],
    )


def get_hourly_consumption_breakdown_today(base_dir: str) -> Dict[str, List[float]]:
    """Gesamt-, Miner- und Grundverbrauch kWh pro Stunde (heute)."""
    samples = _today_timeseries_samples(base_dir)
    total, miner, base = integrate_base_miner_kwh_by_hour(samples)
    return {
        "total_kwh": [round(v, 2) for v in total],
        "miner_kwh": [round(v, 2) for v in miner],
        "base_kwh": [round(v, 2) for v in base],
    }


def get_hourly_actual_base_today(base_dir: str) -> List[float]:
    """Ist-Grundverbrauch kWh pro Stunde 0–23 (heute, segmentweise betrieb − miner)."""
    return get_hourly_consumption_breakdown_today(base_dir)["base_kwh"]


def get_hourly_actual_soc_today(base_dir: str) -> List[Optional[float]]:
    """
    Ist-SOC % pro Stunde 0–23 (heute) aus der 24h-Timeseries.
    Pro Stunde der letzte gültige Messwert; Stunden ohne Messung → None.
    """
    out: List[Optional[float]] = [None] * HOURS_PER_DAY
    for sample in _today_timeseries_samples(base_dir):
        soc = sample.get("soc")
        if soc is None or soc == "":
            continue
        try:
            m = float(sample.get("m") or 0)
            soc_f = float(soc)
        except (TypeError, ValueError):
            continue
        hour = int(m) // 60
        if 0 <= hour < HOURS_PER_DAY:
            out[hour] = round(soc_f, 1)
    return out


def build_consumption_forecast_display(base_dir: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Stündliche Grundverbrauch-Prognose (Wochentagsprofil) vs. Ist heute – für Web-UI.
    """
    now = now or datetime.now()
    weekday = now.weekday()
    history = load_history(base_dir)
    prof = get_weekday_profile(history, weekday, base_dir)
    expected = list(prof.get("hourly_base_kwh_avg") or [0.0] * HOURS_PER_DAY)
    expected_pct_profile = list(prof.get("hourly_base_pct_avg") or [])
    breakdown = get_hourly_consumption_breakdown_today(base_dir)
    actual = breakdown["base_kwh"]
    actual_total = breakdown["total_kwh"]
    actual_miner = breakdown["miner_kwh"]
    sample_count = int(prof.get("sample_count") or 0)

    total_expected = round(sum(float(x) for x in expected), 2)
    total_actual = round(sum(float(x) for x in actual), 2)
    total_actual_miner = round(sum(float(x) for x in actual_miner), 2)
    total_actual_gross = round(sum(float(x) for x in actual_total), 2)
    now_hour = now.hour
    has_expected_pct = bool(expected_pct_profile) and sum(float(x) for x in expected_pct_profile) > 0

    bar_scale_kwh = 1.0
    for h in range(HOURS_PER_DAY):
        if h <= now_hour:
            bar_scale_kwh = max(
                bar_scale_kwh,
                float(expected[h]),
                float(actual[h]),
                float(actual_total[h]),
            )

    hours: List[Dict[str, Any]] = []
    for h in range(HOURS_PER_DAY):
        exp = float(expected[h])
        act = float(actual[h])
        gross = float(actual_total[h])
        miner_h = float(actual_miner[h])
        if has_expected_pct:
            pct_of_day = round(float(expected_pct_profile[h]) * 100.0, 1)
        else:
            pct_of_day = round(exp / total_expected * 100.0, 1) if total_expected > 0 else 0.0
        act_pct = round(act / total_actual * 100.0, 1) if total_actual > 0 and h <= now_hour else None
        diff_pct = None
        if h <= now_hour and exp > 0.05:
            diff_pct = round((act - exp) / exp * 100.0, 1)
        hours.append(
            {
                "hour": h,
                "label": f"{h:02d}:00",
                "expected_kwh": round(exp, 2),
                "actual_kwh": round(act, 2) if h <= now_hour else None,
                "actual_total_kwh": round(gross, 2) if h <= now_hour else None,
                "actual_miner_kwh": round(miner_h, 2) if h <= now_hour else None,
                "pct_of_day_expected": pct_of_day,
                "pct_of_day_actual": act_pct,
                "diff_pct": diff_pct,
                "is_past": h <= now_hour,
                "bar_scale_kwh": round(bar_scale_kwh, 2),
            }
        )

    total_diff_pct = None
    if total_expected > 0 and total_actual > 0:
        total_diff_pct = round((total_actual - total_expected) / total_expected * 100.0, 1)

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    so_far_expected = expected_base_consumption_kwh(base_dir, midnight, now)
    so_far_actual = actual_base_consumption_kwh(base_dir, midnight, now)
    so_far_diff_pct = None
    if so_far_expected > 0.05:
        so_far_diff_pct = round((so_far_actual - so_far_expected) / so_far_expected * 100.0, 1)

    return {
        "weekday": weekday,
        "weekday_name": WEEKDAY_NAMES[weekday],
        "profile_sample_count": sample_count,
        "hours": hours,
        "total_expected_kwh": total_expected,
        "total_actual_kwh": total_actual,
        "total_actual_miner_kwh": total_actual_miner,
        "total_actual_gross_kwh": total_actual_gross,
        "total_diff_pct": total_diff_pct,
        "so_far_expected_kwh": round(so_far_expected, 2),
        "so_far_actual_kwh": round(so_far_actual, 2),
        "so_far_diff_pct": so_far_diff_pct,
        "so_far_until": now.strftime("%H:%M"),
        "note": _consumption_forecast_note(base_dir, sample_count, WEEKDAY_NAMES[weekday]),
    }


def _consumption_forecast_note(base_dir: str, weekday_samples: int, weekday_name: str) -> str:
    archived = archived_history_days(base_dir)
    base = (
        "Grundverbrauch (grün) = Gesamtverbrauch minus Miner-Leistung je Minute "
        "(konfigurierte kW × Laufzeit). Graue Balken = abgezogener Miner-Anteil. "
    )
    if archived > 0:
        return (
            f"{base}Prognose aus Wochentagsprofil der letzten ~{resolve_profile_lookback_days()} Tage "
            f"(Archiv gesamt: {archived} Tage). "
            f"Wochentag: {weekday_name} ({weekday_samples} Tage im Profil)."
        )
    return (
        f"{base}Prognose aus heutiger Tageskurve "
        f"(noch keine archivierte Historie — Archiv täglich ab 23:50 Uhr)."
    )
