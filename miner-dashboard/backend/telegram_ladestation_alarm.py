#!/usr/bin/env python3
"""Alarm Ladestation: Ø-Verbrauch über Zeitfenster vs. kW-Grenzwert."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MINUTES = 15
DEFAULT_THRESHOLD_KW = 50.0
SAMPLE_INTERVAL_SEC = 30


def _samples_path(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor" / "telegram_ladestation_samples.json"


def load_settings(config: Dict[str, Any]) -> Tuple[bool, float, int]:
    enabled = config.get("ev_charging_alarm_enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
    minutes = config.get("ev_charging_alarm_minutes", DEFAULT_MINUTES)
    threshold = config.get("ev_charging_alarm_kw", DEFAULT_THRESHOLD_KW)
    try:
        minutes_i = max(1, min(180, int(float(str(minutes).replace(",", ".")))))
    except (TypeError, ValueError):
        minutes_i = DEFAULT_MINUTES
    try:
        threshold_f = float(str(threshold).replace(",", "."))
    except (TypeError, ValueError):
        threshold_f = DEFAULT_THRESHOLD_KW
    return bool(enabled), threshold_f, minutes_i


def _load_samples(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("samples") or [])
    except Exception:
        return []


def _save_samples(path: Path, samples: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"samples": samples}, indent=0),
        encoding="utf-8",
    )


def record_consumption_sample(base_dir: str, kw: float) -> None:
    path = _samples_path(base_dir)
    now = datetime.now(timezone.utc)
    samples = _load_samples(path)
    samples.append({"t": now.isoformat(), "kw": round(float(kw), 3)})
    cutoff = now - timedelta(hours=3)
    kept: List[Dict[str, Any]] = []
    for item in samples:
        try:
            ts = datetime.fromisoformat(str(item["t"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                kept.append(item)
        except Exception:
            continue
    if len(kept) > 500:
        kept = kept[-500:]
    _save_samples(path, kept)


def average_consumption_kw(base_dir: str, minutes: int) -> Optional[float]:
    path = _samples_path(base_dir)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    values: List[float] = []
    for item in _load_samples(path):
        try:
            ts = datetime.fromisoformat(str(item["t"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                values.append(float(item["kw"]))
        except Exception:
            continue
    if len(values) < 2:
        return None
    return round(mean(values), 2)


def check_ev_charging_alarm(
    base_dir: str, config: Dict[str, Any]
) -> Tuple[bool, Optional[float], float, int]:
    """
    Returns: (should_alert, avg_kw, threshold_kw, minutes)
    should_alert only when avg > threshold and enough samples.
    """
    enabled, threshold, minutes = load_settings(config)
    if not enabled:
        return False, None, threshold, minutes
    avg = average_consumption_kw(base_dir, minutes)
    if avg is None:
        return False, None, threshold, minutes
    return avg > threshold, avg, threshold, minutes
