#!/usr/bin/env python3
"""15-minütige Monatsspitze: rollierender 15-Min-Durchschnitt, Reset monatlich."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
WINDOW_MINUTES = 15
# Bei ~3 s Intervall: 15 Min ≈ 300 Werte – genug Puffer für 15-Min-Fenster
MAX_SAMPLES = 450


def _stats_path(base_dir: str) -> Path:
    try:
        from ram_cache import monthly_peak_state_path

        return monthly_peak_state_path(base_dir)
    except Exception:
        return Path(base_dir) / "energy_monitor" / "monthly_peak_15min.json"


def _peak_backup_path(base_dir: str) -> Path:
    try:
        from ram_cache import monthly_peak_backup_path

        return monthly_peak_backup_path(base_dir)
    except Exception:
        return Path(base_dir) / "energy_monitor" / "monthly_peak_15min.peak.json"


def _kw_to_kwh_15min(kw: float) -> float:
    return round(float(kw) / 4.0, 2)


def _apply_peak(state: Dict[str, Any], peak_kw: float, peak_at: str) -> None:
    state["peak_kw"] = round(float(peak_kw), 2)
    state["peak_kwh"] = _kw_to_kwh_15min(peak_kw)
    state["peak_at"] = peak_at


def _save_state_ram(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _save_peak_backup_durable(backup_path: Path, state: Dict[str, Any]) -> None:
    """Nur Monatsspitze auf SD — fsync bei Peak-Änderung."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    peak_only = {
        "month": state.get("month"),
        "peak_kw": state.get("peak_kw"),
        "peak_kwh": state.get("peak_kwh"),
        "peak_at": state.get("peak_at"),
    }
    backup_path.write_text(json.dumps(peak_only, indent=2), encoding="utf-8")
    with backup_path.open("rb") as fh:
        os.fsync(fh.fileno())


def _load_peak_backup(backup_path: Path, month: str) -> Dict[str, Any]:
    if not backup_path.is_file():
        return {}
    try:
        data = json.loads(backup_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if data.get("month") != month:
        return {}
    return data


def _load_state(path: Path, backup_path: Path, month: str) -> Dict[str, Any]:
    state: Dict[str, Any] = _empty_state(month)
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            state = _empty_state(month)
    if state.get("month") != month:
        return _empty_state(month)

    backup = _load_peak_backup(backup_path, month)
    if backup.get("peak_kw") is not None:
        cur = state.get("peak_kw")
        if cur is None or float(backup["peak_kw"]) > float(cur):
            _apply_peak(
                state,
                float(backup["peak_kw"]),
                str(backup.get("peak_at") or state.get("peak_at") or ""),
            )
            if backup.get("peak_kwh") is not None:
                state["peak_kwh"] = float(backup["peak_kwh"])
    elif state.get("peak_kw") is not None and state.get("peak_kwh") is None:
        state["peak_kwh"] = _kw_to_kwh_15min(float(state["peak_kw"]))

    state.setdefault("samples", [])
    return state


def _parse_kw_text(raw: str) -> Optional[float]:
    text = str(raw).strip().replace(",", ".")
    if not text or text.lower() == "n/a":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_current_kw(base_dir: str) -> Optional[float]:
    try:
        from ram_cache import is_enabled, read_text

        if is_enabled():
            for name in ("energy_value.txt", "betrieb_kw.txt", "betrieb_value.txt"):
                raw = read_text(base_dir, name, scope="energy_monitor")
                if raw:
                    kw = _parse_kw_text(raw)
                    if kw is not None:
                        return abs(kw)
    except Exception:
        pass

    em_dir = Path(base_dir) / "energy_monitor"
    for name in ("energy_value.txt", "betrieb_kw.txt", "betrieb_value.txt"):
        path = em_dir / name
        if path.is_file():
            kw = _parse_kw_text(path.read_text(encoding="utf-8"))
            if kw is not None:
                return abs(kw)

    try:
        from miner_flow_data import build_flow_response

        flow = build_flow_response(base_dir, battery_soc_loader=None)
        for key in ("grid", "betrieb", "erzeugung_gesamt"):
            kw = _parse_kw_text(str(flow.get(key, "")))
            if kw is not None:
                return abs(kw)
    except Exception:
        pass
    return None


def _empty_state(month: str) -> Dict[str, Any]:
    return {
        "month": month,
        "peak_kw": None,
        "peak_at": None,
        "samples": [],
    }


def _trim_samples(samples: List[Dict[str, Any]], cutoff: datetime) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for item in samples:
        try:
            ts = datetime.fromisoformat(str(item.get("t")))
            if ts >= cutoff:
                kept.append({"t": ts.isoformat(timespec="seconds"), "kw": float(item["kw"])})
        except Exception:
            continue
    return kept


def _sample_span_minutes(samples: List[Dict[str, Any]]) -> float:
    if len(samples) < 2:
        return 0.0
    try:
        oldest = datetime.fromisoformat(samples[0]["t"])
        newest = datetime.fromisoformat(samples[-1]["t"])
        return max(0.0, (newest - oldest).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _window_ready(samples: List[Dict[str, Any]]) -> bool:
    """Mindestens ~14 Minuten Messdaten im Fenster (ältester → neuester Punkt)."""
    return _sample_span_minutes(samples) >= (WINDOW_MINUTES - 1.0)


def _downsample(samples: List[Dict[str, Any]], every_seconds: int = 30) -> List[Dict[str, Any]]:
    """Reduziert Dateigröße, behält 15-Min-Spanne."""
    if not samples:
        return []
    out: List[Dict[str, Any]] = []
    last_kept: Optional[datetime] = None
    for item in samples:
        try:
            ts = datetime.fromisoformat(item["t"])
        except Exception:
            continue
        if last_kept is None or (ts - last_kept).total_seconds() >= every_seconds:
            out.append(item)
            last_kept = ts
    if samples and out and out[-1]["t"] != samples[-1]["t"]:
        out.append(samples[-1])
    return out


def update_monthly_peak(base_dir: str) -> Dict[str, Any]:
    path = _stats_path(base_dir)
    backup_path = _peak_backup_path(base_dir)
    now = datetime.now()
    month = now.strftime("%Y-%m")
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)

    with _lock:
        state = _load_state(path, backup_path, month)

        kw = _read_current_kw(base_dir)
        samples = _trim_samples(state.get("samples") or [], cutoff)

        if kw is not None:
            samples.append({"t": now.isoformat(timespec="seconds"), "kw": abs(kw)})
            samples = _trim_samples(samples, cutoff)

        if len(samples) > MAX_SAMPLES:
            samples = _downsample(samples, every_seconds=15)
        if len(samples) > MAX_SAMPLES:
            samples = samples[-MAX_SAMPLES:]

        span_min = _sample_span_minutes(samples)
        current_avg: Optional[float] = None
        if samples:
            current_avg = round(mean(s["kw"] for s in samples), 2)

        peak_changed = False
        if samples and _window_ready(samples):
            avg_15 = round(mean(s["kw"] for s in samples), 2)
            peak = state.get("peak_kw")
            if peak is None or avg_15 > float(peak):
                try:
                    peak_at = datetime.fromisoformat(samples[0]["t"]).isoformat(
                        timespec="seconds"
                    )
                except Exception:
                    peak_at = now.isoformat(timespec="seconds")
                _apply_peak(state, avg_15, peak_at)
                peak_changed = True

        state["month"] = month
        state["samples"] = samples
        state["current_15min_avg"] = current_avg
        state["window_span_minutes"] = round(span_min, 1)

        _save_state_ram(path, state)
        if peak_changed:
            _save_peak_backup_durable(backup_path, state)
        return state


def get_monthly_peak(base_dir: str) -> Dict[str, Any]:
    state = update_monthly_peak(base_dir)
    span = float(state.get("window_span_minutes") or 0)
    peak_kw = state.get("peak_kw")
    peak_kwh = state.get("peak_kwh")
    if peak_kw is not None and peak_kwh is None:
        peak_kwh = _kw_to_kwh_15min(float(peak_kw))
    return {
        "month": state.get("month"),
        "peak_kw": peak_kw,
        "peak_kwh": peak_kwh,
        "peak_at": state.get("peak_at"),
        "current_15min_avg": state.get("current_15min_avg"),
        "window_minutes": WINDOW_MINUTES,
        "window_span_minutes": span,
        "window_ready": span >= (WINDOW_MINUTES - 1.0),
        "progress_minutes": min(span, float(WINDOW_MINUTES)),
    }
