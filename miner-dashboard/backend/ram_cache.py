# -*- coding: utf-8 -*-
"""Live-Daten in RAM (/dev/shm) — schont die SD-Karte bei häufigen Schreibvorgängen."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[str, Path]

RAM_ROOT = Path(os.environ.get("MINER_RAM_ROOT", "/dev/shm/minersteuerung"))
FLUSH_INTERVAL_SEC = int(os.environ.get("MINER_RAM_FLUSH_SEC", "3600"))
_raw_log_max = os.environ.get("MINER_LOG_MAX_BYTES", "")
LOG_MAX_BYTES = int(_raw_log_max) if str(_raw_log_max).strip().isdigit() else 2_000_000

_lock = threading.Lock()
_last_flush_mono = 0.0
_last_flush_date: Optional[str] = None


def is_enabled() -> bool:
    return os.environ.get("MINER_RAM_CACHE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def ensure_dirs() -> None:
    if not is_enabled():
        return
    RAM_ROOT.mkdir(parents=True, exist_ok=True)
    logs_dir()


def logs_dir() -> Path:
    """Laufzeit-Logs in RAM (optional per MINER_LOG_DIR überschreibbar)."""
    override = os.environ.get("MINER_LOG_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    if is_enabled():
        path = RAM_ROOT / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path
    root = Path(os.environ.get("MINER_ROOT", "/home/piminer/minersteuerung"))
    path = root / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_runtime_dirs(base_dir: str) -> None:
    if not is_enabled():
        return
    ensure_dirs()
    # Nach Reboot ist /dev/shm leer → force-Hydrate; sonst nur fehlende Dateien.
    force = not (RAM_ROOT / ".hydrated").is_file()
    hydrate_from_sd(base_dir, force=force)


def trim_log_file(path: Path, max_bytes: Optional[int] = None) -> None:
    """Ringpuffer: älteste Log-Zeilen verwerfen wenn Datei zu groß."""
    limit = int(max_bytes or LOG_MAX_BYTES or 0)
    if limit <= 0:
        return
    try:
        if not path.is_file() or path.stat().st_size <= limit:
            return
        with open(path, "rb") as fp:
            fp.seek(-limit, 2)
            data = fp.read()
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1 :]
        path.write_bytes(data)
    except Exception:
        pass


def _sd_energy_dir(base_dir: str) -> Path:
    return Path(base_dir) / "energy_monitor"


def _sd_backend_dir(base_dir: str) -> Path:
    return Path(base_dir)


def live_energy_path(base_dir: str, filename: str) -> Path:
    if is_enabled():
        return RAM_ROOT / "energy_monitor" / filename
    return _sd_energy_dir(base_dir) / filename


def backend_live_path(base_dir: str, filename: str) -> Path:
    if is_enabled():
        return RAM_ROOT / "backend" / filename
    return _sd_backend_dir(base_dir) / filename


def backend_ephemeral_path(base_dir: str, filename: str) -> Path:
    return backend_live_path(base_dir, filename)


def sd_energy_path(base_dir: str, filename: str) -> Path:
    return _sd_energy_dir(base_dir) / filename


def sd_backend_path(base_dir: str, filename: str) -> Path:
    return _sd_backend_dir(base_dir) / filename


def monthly_peak_state_path(base_dir: str) -> Path:
    name = "monthly_peak_15min.json"
    if is_enabled():
        return RAM_ROOT / "energy_monitor" / name
    return _sd_energy_dir(base_dir) / name


def monthly_peak_backup_path(base_dir: str) -> Path:
    return _sd_energy_dir(base_dir) / "monthly_peak_15min.json"


def rule_runtime_path(base_dir: str) -> Path:
    return live_energy_path(base_dir, "rule_runtime_state.json")


def timeseries_path(base_dir: str, filename: str) -> Path:
    if filename == "energy_timeseries_24h.json":
        if is_enabled():
            return RAM_ROOT / "energy_monitor" / filename
        return _sd_energy_dir(base_dir) / filename
    # backend timeseries (z. B. hashrate)
    if is_enabled():
        return RAM_ROOT / "backend" / filename
    return _sd_backend_dir(base_dir) / filename


def _read_candidates(base_dir: str, filename: str, scope: str) -> list:
    if scope == "energy_monitor":
        live = live_energy_path(base_dir, filename)
        sd = _sd_energy_dir(base_dir) / filename
    else:
        live = backend_live_path(base_dir, filename)
        sd = _sd_backend_dir(base_dir) / filename
    return [live, sd] if is_enabled() else [sd]


def read_text(
    base_dir: str,
    filename: str,
    default: str = "",
    *,
    scope: str = "energy_monitor",
) -> str:
    """Neuere Quelle gewinnt: lebende RAM-Datei, sonst die frischere SD-Kopie.

    Eine stehengebliebene RAM-Ansicht (älter als die SD) darf den Live-Wert
    nicht verdecken.
    """
    best: Optional[Path] = None
    best_mtime = -1.0
    for path in _read_candidates(base_dir, filename, scope):
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except Exception:
            continue
        if mtime >= best_mtime:
            best = path
            best_mtime = mtime
    if best is None:
        return default
    try:
        return best.read_text(encoding="utf-8")
    except Exception:
        return default


def read_json(
    base_dir: str,
    filename: str,
    default: Any = None,
    *,
    scope: str = "energy_monitor",
) -> Any:
    raw = read_text(base_dir, filename, default="", scope=scope)
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def write_text(
    base_dir: str,
    filename: str,
    content: str,
    *,
    scope: str = "energy_monitor",
) -> Path:
    ensure_dirs()
    path = (
        live_energy_path(base_dir, filename)
        if scope == "energy_monitor"
        else backend_live_path(base_dir, filename)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_json(
    base_dir: str,
    filename: str,
    data: Any,
    *,
    scope: str = "energy_monitor",
) -> Path:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if not filename.endswith(".json"):
        filename = f"{filename}.json"
    return write_text(base_dir, filename, text + "\n", scope=scope)


def write_json_durable(
    base_dir: str,
    filename: str,
    data: Any,
    *,
    scope: str = "backend",
) -> Path:
    """Kritische Status-Dateien: immer SD + bei RAM-Cache auch Live-Pfad.

    Verhindert nach Reboot/Stromausfall, dass Leser nur leeren RAM sehen,
    während die SD bereits gültige Daten hat (und umgekehrt).
    """
    if not filename.endswith(".json"):
        filename = f"{filename}.json"
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if scope == "energy_monitor":
        sd = _sd_energy_dir(base_dir) / filename
    else:
        sd = _sd_backend_dir(base_dir) / filename
    sd.parent.mkdir(parents=True, exist_ok=True)
    tmp = sd.with_suffix(sd.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(sd)
    if is_enabled():
        return write_text(base_dir, filename, text, scope=scope)
    return sd


# Live-Stromfluss-Dateien: nach Reboot als SD-Fallback, bis HA-Sync wieder RAM füllt.
_LIVE_ENERGY_FILES = (
    "enline_values.json",
    "daten.json",
    "energy_value.txt",
    "pv_value.txt",
    "kelag_value.txt",
    "wohnhaus_value.txt",
    "bezug_value.txt",
    "lieferung_value.txt",
    "eigenverbrauch_value.txt",
    "wasserkraft_value.txt",
    "betrieb_value.txt",
    "betrieb_anzeige_value.txt",
    "verbrauch_gesamt_value.txt",
    "erzeugung_gesamt_value.txt",
)


def flush_timeseries_to_sd(base_dir: str) -> None:
    """Live-RAM-Daten auf SD sichern (Timeseries, Statistik, Stromfluss-Werte)."""
    if not is_enabled():
        return
    pairs = [
        (
            RAM_ROOT / "energy_monitor" / "energy_timeseries_24h.json",
            _sd_energy_dir(base_dir) / "energy_timeseries_24h.json",
        ),
        (
            RAM_ROOT / "backend" / "hashrate_timeseries_24h.json",
            _sd_backend_dir(base_dir) / "hashrate_timeseries_24h.json",
        ),
        (
            RAM_ROOT / "backend" / "miner_status.json",
            _sd_backend_dir(base_dir) / "miner_status.json",
        ),
        (
            RAM_ROOT / "energy_monitor" / "energy_stats_24h.json",
            _sd_energy_dir(base_dir) / "energy_stats_24h.json",
        ),
    ]
    for name in _LIVE_ENERGY_FILES:
        pairs.append(
            (
                RAM_ROOT / "energy_monitor" / name,
                _sd_energy_dir(base_dir) / name,
            )
        )
    for src, dst in pairs:
        try:
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copy2(src, tmp)
            tmp.replace(dst)
        except Exception:
            pass


def maybe_flush_timeseries(base_dir: str) -> None:
    """Stündlich oder bei Tageswechsel Timeseries auf SD sichern."""
    if not is_enabled():
        return
    global _last_flush_mono, _last_flush_date
    now_mono = time.monotonic()
    today = date.today().isoformat()
    with _lock:
        due_day = bool(_last_flush_date) and _last_flush_date != today
        due_time = (now_mono - _last_flush_mono) >= FLUSH_INTERVAL_SEC
        if not due_day and not due_time and _last_flush_mono > 0:
            return
        _last_flush_mono = now_mono
        _last_flush_date = today
    flush_timeseries_to_sd(base_dir)


def hydrate_from_sd(base_dir: str, *, force: bool = False) -> None:
    """Beim Start fehlende RAM-Dateien aus SD übernehmen.

    force=True: nach Stromausfall auch vorhandene (leere/alte) RAM-Dateien
    aus der SD überschreiben, wenn die SD-Kopie neuer oder größer ist.
    """
    if not is_enabled():
        return
    ensure_dirs()
    (RAM_ROOT / "energy_monitor").mkdir(parents=True, exist_ok=True)
    (RAM_ROOT / "backend").mkdir(parents=True, exist_ok=True)
    logs_dir()
    copies = [
        (
            _sd_energy_dir(base_dir) / "energy_timeseries_24h.json",
            RAM_ROOT / "energy_monitor" / "energy_timeseries_24h.json",
        ),
        (
            _sd_backend_dir(base_dir) / "hashrate_timeseries_24h.json",
            RAM_ROOT / "backend" / "hashrate_timeseries_24h.json",
        ),
        (
            _sd_backend_dir(base_dir) / "miner_status.json",
            RAM_ROOT / "backend" / "miner_status.json",
        ),
        (
            _sd_energy_dir(base_dir) / "energy_stats_24h.json",
            RAM_ROOT / "energy_monitor" / "energy_stats_24h.json",
        ),
        (
            _sd_energy_dir(base_dir) / "monthly_peak_15min.json",
            RAM_ROOT / "energy_monitor" / "monthly_peak_15min.json",
        ),
        (
            _sd_energy_dir(base_dir) / "enline_values.json",
            RAM_ROOT / "energy_monitor" / "enline_values.json",
        ),
        (
            _sd_energy_dir(base_dir) / "control_latches.json",
            RAM_ROOT / "energy_monitor" / "control_latches.json",
        ),
    ]
    for name in _LIVE_ENERGY_FILES:
        copies.append(
            (
                _sd_energy_dir(base_dir) / name,
                RAM_ROOT / "energy_monitor" / name,
            )
        )
    copied = 0
    for src, dst in copies:
        try:
            if not src.is_file():
                continue
            if dst.is_file() and not force:
                continue
            if dst.is_file() and force:
                try:
                    if dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
                        continue
                except OSError:
                    pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except Exception:
            pass
    marker = RAM_ROOT / ".hydrated"
    try:
        marker.write_text(
            f"copied={copied}\nforce={int(bool(force))}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def hydrate_after_powerloss(base_dir: str) -> None:
    """Explizit nach Stromausfall/Reboot: RAM aus SD regenerieren."""
    setup_runtime_dirs(base_dir)
    # Wenn shm gerade leer war, force erneut falls Teildateien existieren
    hydrate_from_sd(base_dir, force=True)