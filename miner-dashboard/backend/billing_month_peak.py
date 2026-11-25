#!/usr/bin/env python3
"""Monatsspitze nach der österreichischen Netzverrechnung.

SNE-V 2018, Begriff „Viertelstundenmaximumzählung“: die höchste
einviertelstündliche Durchschnittsbelastung innerhalb eines Kalendermonats.
SNE-G-V, BGBl. II Nr. 299/2026, § 2 und § 3 Abs. 1: der Viertelstundenleistungswert
ist die über ein 15-Minuten-Marktintervall gemessene mittlere Wirkleistung.
Verrechnet wird am Hauptzählpunkt der höchste monatliche Wert in Entnahmerichtung.

P_15 = Summe der minütlichen Bezugsleistung (kW, Export zählt 0) / 15
     = importierte kWh der Viertelstunde / 0,25 h.

Nur volle, uhrzeitgleiche Intervalle (:00, :15, :30, :45, Europe/Vienna).
Eine fehlende Minute macht das Intervall ungültig. Es wird kein Ersatzwert gebildet.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

QUARTER_MINUTES = 15
MINUTES_PER_DAY = 24 * 60


def _archive_dir(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / "energy_timeseries_days"


def _live_path(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / "energy_timeseries_24h.json"


def _kw(raw: Any) -> Optional[Decimal]:
    if raw is None or raw == "" or raw == "n/a":
        return None
    try:
        return Decimal(str(raw).replace(",", ".").strip())
    except Exception:
        return None


def _round_kw(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def quarter_import_kw(minute_kw: Dict[int, Decimal]) -> Optional[Decimal]:
    """Mittlere Entnahmeleistung einer vollen Viertelstunde, in kW.

    minute_kw bildet den Minutenversatz 0..14 auf die vorzeichenbehaftete
    Netzleistung ab. Positiv ist Bezug, negativ ist Einspeisung.
    """
    if any(offset not in minute_kw for offset in range(QUARTER_MINUTES)):
        return None
    total = Decimal(0)
    for offset in range(QUARTER_MINUTES):
        sample = minute_kw[offset]
        if sample > 0:
            total += sample
    return total / Decimal(QUARTER_MINUTES)


def quarters_from_samples(samples: List[Dict[str, Any]]) -> List[Tuple[int, Decimal]]:
    """Uhrzeitgleiche Viertelstunden eines Tages. Index 0 ist 00:00–00:15."""
    buckets: Dict[int, Dict[int, Decimal]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        try:
            minute = int(sample.get("m"))
        except (TypeError, ValueError):
            continue
        if minute < 0 or minute >= MINUTES_PER_DAY:
            continue
        power = _kw(sample.get("netz"))
        if power is None:
            continue
        quarter, offset = divmod(minute, QUARTER_MINUTES)
        buckets.setdefault(quarter, {})[offset] = power
    found: List[Tuple[int, Decimal]] = []
    for quarter, minutes in buckets.items():
        power = quarter_import_kw(minutes)
        if power is not None:
            found.append((quarter, power))
    return found


def _sample_kw(sample: Dict[str, Any], *keys: str) -> Optional[Decimal]:
    for key in keys:
        value = _kw(sample.get(key))
        if value is not None:
            return value
    return None


def uncovered_import_kw(sample: Dict[str, Any]) -> Optional[Decimal]:
    """Bezug, den die Last ohne Batterie-Gegenregelung gebraucht hätte.

    Verbrauch minus Erzeugung. Liegt die Erzeugung darüber, ist der Bezug 0,
    genau wie Einspeisung bei der verrechneten Monatsspitze nicht zählt.
    """
    if not isinstance(sample, dict):
        return None
    load = _sample_kw(sample, "betrieb", "line_verbrauch")
    generation = _sample_kw(sample, "erzeugung", "line_erzeugung")
    if load is None or generation is None:
        return None
    gap = load - generation
    return gap if gap > 0 else Decimal(0)


def quarters_uncovered(samples: List[Dict[str, Any]]) -> List[Tuple[int, Decimal]]:
    """Volle Viertelstunden von Verbrauch minus Erzeugung."""
    buckets: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        try:
            minute = int(sample.get("m"))
        except (TypeError, ValueError):
            continue
        if minute < 0 or minute >= MINUTES_PER_DAY:
            continue
        quarter, offset = divmod(minute, QUARTER_MINUTES)
        buckets.setdefault(quarter, {})[offset] = sample
    found: List[Tuple[int, Decimal]] = []
    for quarter, minutes in buckets.items():
        if any(offset not in minutes for offset in range(QUARTER_MINUTES)):
            continue
        total = Decimal(0)
        complete = True
        for offset in range(QUARTER_MINUTES):
            power = uncovered_import_kw(minutes[offset])
            if power is None:
                complete = False
                break
            total += power
        if complete:
            found.append((quarter, total / Decimal(QUARTER_MINUTES)))
    return found


def _load_day(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _day_sources(base_dir: str, month: str) -> List[Tuple[str, Path]]:
    """Abgeschlossene Tage plus die laufende Tagesdatei, nur dieser Kalendermonat."""
    sources: Dict[str, Path] = {}
    folder = _archive_dir(base_dir)
    if folder.is_dir():
        for path in sorted(folder.glob(f"{month}-*.json")):
            day = path.stem
            if len(day) == 10:
                sources[day] = path
    live = _live_path(base_dir)
    loaded = _load_day(live) if live.is_file() else None
    live_day = str((loaded or {}).get("date") or "")[:10]
    if live_day.startswith(month) and len(live_day) == 10:
        sources[live_day] = live
    return sorted(sources.items())


def _quarter_label(day: str, quarter: int) -> str:
    start = quarter * QUARTER_MINUTES
    end = start + QUARTER_MINUTES
    try:
        stamp = datetime.strptime(day, "%Y-%m-%d")
        when = stamp.strftime("%d.%m.%Y")
    except ValueError:
        when = day
    return (
        f"{when}, {start // 60:02d}:{start % 60:02d}"
        f"–{end // 60 % 24:02d}:{end % 60:02d}"
    )


_caches: Dict[str, Tuple[Tuple[Any, ...], Dict[str, Any]]] = {}


def _highest_quarter(
    base_dir: str,
    today: Optional[date],
    name: str,
    extractor,
) -> Dict[str, Any]:
    """Höchste volle Viertelstunde des Kalendermonats für eine Minutenrechnung."""
    month_day = today or date.today()
    month = month_day.strftime("%Y-%m")
    sources = _day_sources(base_dir, month)
    key_parts: List[Any] = [month, name]
    for day, path in sources:
        try:
            key_parts.append((day, path.stat().st_mtime_ns, path.stat().st_size))
        except OSError:
            key_parts.append((day, 0, 0))
    key = tuple(key_parts)
    cached = _caches.get(name)
    if cached is not None and cached[0] == key:
        return dict(cached[1])

    best_kw: Optional[Decimal] = None
    best_day = ""
    best_quarter = 0
    complete = 0
    for day, path in sources:
        loaded = _load_day(path)
        if not loaded or str(loaded.get("date") or day)[:7] != month:
            continue
        samples = loaded.get("samples")
        if not isinstance(samples, list):
            continue
        for quarter, power in extractor(samples):
            complete += 1
            if best_kw is None or power > best_kw:
                best_kw = power
                best_day = str(loaded.get("date") or day)[:10]
                best_quarter = quarter

    first_day = sources[0][0] if sources else None
    covered = len(sources)
    if best_kw is None:
        value: Dict[str, Any] = {
            "month": month,
            "peak_kw": None,
            "peak_at": None,
            "window": None,
            "quarters": 0,
            "days": covered,
            "first_day": first_day,
            "label": "Noch keine volle Viertelstunde",
        }
    else:
        shown = _round_kw(best_kw)
        window = _quarter_label(best_day, best_quarter)
        start = best_quarter * QUARTER_MINUTES
        label = f"Viertelstunde {window}"
        if first_day and not first_day.endswith("-01"):
            try:
                start_day = datetime.strptime(first_day, "%Y-%m-%d").strftime("%d.%m.")
            except ValueError:
                start_day = first_day
            label = f"{label}. Gemessen ab {start_day}"
        value = {
            "month": month,
            "peak_kw": shown,
            "peak_at": f"{best_day}T{start // 60:02d}:{start % 60:02d}:00",
            "window": window,
            "quarters": complete,
            "days": covered,
            "first_day": first_day,
            "label": label,
        }
    _caches[name] = (key, value)
    return dict(value)


def billing_month_peak(base_dir: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Höchste volle Viertelstunden-Entnahmeleistung des Kalendermonats."""
    return _highest_quarter(base_dir, today, "grid", quarters_from_samples)


def hypothetical_month_peak(base_dir: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Höchste Viertelstunde von Verbrauch minus Erzeugung.

    Das ist die Spitze, die der Netzbezug ohne die Batterie-Gegenregelung
    des PMS gehabt hätte. Die Minutenwerte stehen in der Tagesreihe.
    """
    peak = _highest_quarter(base_dir, today, "uncovered", quarters_uncovered)
    if peak.get("label") and peak.get("peak_kw") is not None:
        label = str(peak["label"]).rstrip(".")
        peak["label"] = f"{label}. Verbrauch minus Erzeugung"
    return peak


def peak_money(
    peak_kw: Optional[float],
    hypothetical_kw: Optional[float],
    tariff_eur_per_kw: Optional[float],
) -> Dict[str, Optional[float]]:
    """Kosten beider Spitzen und die Ersparnis durch die Gegenregelung."""

    def cost(kw: Optional[float]) -> Optional[float]:
        if kw is None or tariff_eur_per_kw is None:
            return None
        return round(float(kw) * float(tariff_eur_per_kw), 2)

    real = cost(peak_kw)
    hypo = cost(hypothetical_kw)
    saved = None if real is None or hypo is None else round(hypo - real, 2)
    return {
        "cost_eur": real,
        "hypothetical_cost_eur": hypo,
        "saved_eur": saved,
    }


def _entry_path(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / "monatsspitze_eingabe.json"


def load_peak_entry(base_dir: str) -> Dict[str, Optional[float]]:
    """Gespeicherte Eingaben. None heißt: den gerechneten Wert anzeigen."""
    empty: Dict[str, Optional[float]] = {
        "peak_kw": None,
        "hypothetical_kw": None,
        "tariff_eur_per_kw": None,
    }
    path = _entry_path(base_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(empty)
    if not isinstance(raw, dict):
        return dict(empty)
    for key in empty:
        empty[key] = _optional_entry(raw.get(key))
    return empty


def _optional_entry(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        raw = raw.replace(",", ".").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 100000:
        return None
    return round(value, 2)


def save_peak_entry(base_dir: str, updates: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Schreibt nur die mitgeschickten Felder. Leerer Wert löscht die Eingabe."""
    entry = load_peak_entry(base_dir)
    for key in ("peak_kw", "hypothetical_kw", "tariff_eur_per_kw"):
        if key not in updates:
            continue
        raw = updates.get(key)
        if raw is None or raw == "":
            entry[key] = None
            continue
        parsed = _optional_entry(raw)
        if parsed is None:
            raise ValueError(f"Ungültiger Wert für {key}")
        entry[key] = parsed
    path = _entry_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return entry
