# -*- coding: utf-8 -*-
"""Hashrate-Zeitreihe: 1 Punkt / Minute (Live-Gesamt + Soll der laufenden Miner)."""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_lock = threading.Lock()
MAX_SAMPLES = 1445

# "(100 TH/s)" oder "100 TH/s" / "100 TH"
_TH_RE = re.compile(
    r"(?:\(|\b)(\d+(?:[.,]\d+)?)\s*TH(?:/s)?(?:\))?",
    re.IGNORECASE,
)
# "Beispiel", "Beispiel" → Nenn-TH aus Notiz
_HYDRO_NOTE_RE = re.compile(r"(?i)\bhydro\s+(\d+(?:[.,]\d+)?)\b")


def _path(base_dir: str) -> Path:
    try:
        from ram_cache import timeseries_path

        return timeseries_path(base_dir, "hashrate_timeseries_24h.json")
    except Exception:
        return Path(base_dir) / "hashrate_timeseries_24h.json"


def _empty(today: str) -> Dict[str, Any]:
    return {"date": today, "samples": []}


def parse_nominal_ths(*texts: Any) -> Optional[float]:
    """Extrahiert Nenn-Hashrate (TH/s) aus note/model.

    Unterstützt u. a.:
    - ``Antminer S19j Pro (100 TH/s)``
    - ``100 TH/s`` / ``100 TH``
    - ``Beispiel`` / ``Beispiel`` (übliche Notiz ohne „TH“)
    """
    for text in texts:
        if not text:
            continue
        s = str(text).strip()
        if not s:
            continue
        m = _TH_RE.search(s)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                pass
        m = _HYDRO_NOTE_RE.search(s)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                # Plausible Miner-Nennleistung (TH/s)
                if 10.0 <= val <= 1000.0:
                    return val
            except ValueError:
                pass
    return None


def load_miners_nominal(base_dir: str) -> Dict[str, float]:
    """IP -> Nenn-Hashrate TH/s aus note/model."""
    path = Path(base_dir) / "miners.json"
    out: Dict[str, float] = {}
    if not path.is_file():
        return out
    try:
        miners = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(miners, list):
        return out
    for m in miners:
        if not isinstance(m, dict):
            continue
        ip = m.get("ip")
        if not ip:
            continue
        ths = parse_nominal_ths(m.get("note"), m.get("model"))
        if ths is not None and ths > 0:
            out[str(ip)] = float(ths)
    return out


def compute_totals(
    status_entries: List[Dict[str, Any]],
    nominal_by_ip: Dict[str, float],
) -> Tuple[float, float]:
    """(live_ths, target_ths) – Soll = Summe Nennleistung aller running Miner.

    Fehlt die Nennleistung in miners.json, zählt die aktuelle Live-Hashrate
    des Miners als Soll-Proxy (Linie folgt dann dem Ist, statt bei 0 zu kleben).
    """
    live = 0.0
    target = 0.0
    for entry in status_entries:
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("ip") or "")
        status = str(entry.get("status") or "").strip().lower()
        if status not in ("running", "run", "active"):
            continue
        hr_val: Optional[float] = None
        try:
            hr = entry.get("hashrate_ths")
            if hr is not None:
                hr_val = max(0.0, float(hr))
                live += hr_val
        except (TypeError, ValueError):
            pass
        nom = float(nominal_by_ip.get(ip) or 0.0)
        if nom <= 0 and hr_val is not None and hr_val > 0:
            nom = hr_val
        target += nom
    return round(live, 2), round(target, 2)


def _load_state(path: Path) -> Dict[str, Any]:
    today = date.today().isoformat()
    if not path.is_file():
        return _empty(today)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty(today)
    if not isinstance(state, dict):
        return _empty(today)
    if state.get("date") != today:
        return _empty(today)
    state.setdefault("samples", [])
    if not isinstance(state["samples"], list):
        state["samples"] = []
    return state


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_hashrate_sample(
    base_dir: str,
    status_entries: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Speichert höchstens einen Punkt pro Kalenderminute."""
    now = now or datetime.now()
    path = _path(base_dir)
    nominal = load_miners_nominal(base_dir)
    live, target = compute_totals(status_entries, nominal)
    minute_key = now.strftime("%Y-%m-%dT%H:%M")

    with _lock:
        state = _load_state(path)
        samples = list(state.get("samples") or [])
        if samples:
            last_ts = str(samples[-1].get("ts") or "")
            if last_ts[:16] == minute_key:
                samples[-1] = {
                    "ts": now.isoformat(timespec="seconds"),
                    "minute": now.strftime("%H:%M"),
                    "live_ths": live,
                    "target_ths": target,
                }
                state["samples"] = samples
                state["live_ths"] = live
                state["target_ths"] = target
                state["updated_at"] = now.isoformat(timespec="seconds")
                _save_state(path, state)
                try:
                    from ram_cache import maybe_flush_timeseries

                    maybe_flush_timeseries(base_dir)
                except Exception:
                    pass
                return state

        samples.append(
            {
                "ts": now.isoformat(timespec="seconds"),
                "minute": now.strftime("%H:%M"),
                "live_ths": live,
                "target_ths": target,
            }
        )
        if len(samples) > MAX_SAMPLES:
            samples = samples[-MAX_SAMPLES:]
        state["samples"] = samples
        state["live_ths"] = live
        state["target_ths"] = target
        state["updated_at"] = now.isoformat(timespec="seconds")
        _save_state(path, state)
        try:
            from ram_cache import maybe_flush_timeseries

            maybe_flush_timeseries(base_dir)
        except Exception:
            pass
        return state


def get_hashrate_timeseries(base_dir: str) -> Dict[str, Any]:
    path = _path(base_dir)
    with _lock:
        state = _load_state(path)
    samples = state.get("samples") or []
    return {
        "date": state.get("date"),
        "updated_at": state.get("updated_at"),
        "live_ths": state.get("live_ths"),
        "target_ths": state.get("target_ths"),
        "samples": samples,
        "labels": [s.get("minute") or "" for s in samples],
        "live": [s.get("live_ths") for s in samples],
        "target": [s.get("target_ths") for s in samples],
    }
