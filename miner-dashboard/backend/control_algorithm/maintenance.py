# -*- coding: utf-8 -*-
"""Health-Checks und Recovery-Hilfen für den Wartungs-Watchdog."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.health_monitor import _proc_running

BACKEND_DIR = Path(__file__).resolve().parent.parent


def is_calibration_due(
    now: datetime,
    calibration_hour: int,
    calibration_minute: int,
    last_calibration_run_key: Any,
) -> bool:
    """True ab Kalibrierungszeit bis Tagesende, wenn heute noch nicht gelaufen."""
    cal_start = now.replace(
        hour=int(calibration_hour),
        minute=int(calibration_minute),
        second=0,
        microsecond=0,
    )
    if now < cal_start:
        return False
    key = f"cal_{now.date().isoformat()}"
    return str(last_calibration_run_key or "") != key

ROOT_DIR = BACKEND_DIR.parent.parent


@dataclass(frozen=True)
class ServiceSpec:
    script: str
    label: str
    critical: bool = True
    optional: bool = False


# Kritische Hintergrunddienste (ohne Desktop-Terminals).
MANAGED_SERVICES: Tuple[ServiceSpec, ...] = (
    ServiceSpec("app.py", "Flask Backend"),
    ServiceSpec("scheduler.py", "Scheduler"),
    ServiceSpec("control_algorithm_daemon.py", "Steuerung"),
    ServiceSpec("energy_monitor/auto_control.py", "Energiemonitor"),
    ServiceSpec("status.py", "Miner Status"),
    # critical, wenn Token gesetzt – sonst bleibt optional und wird nicht als Defekt gezählt
    ServiceSpec("minerbot.py", "Telegram", critical=True, optional=True),
)

# Quellmodule, ohne die Live-Betrieb still bricht (.py weg, .pyc übrig).
CRITICAL_SOURCE_MODULES: Tuple[str, ...] = (
    "app.py",
    "hashrate_timeseries.py",
    "ram_cache.py",
    "ha_entity_mapping.py",
    "maintenance_watchdog.py",
    "control_algorithm_web.py",
    "control_algorithm_daemon.py",
    "scheduler.py",
    "status.py",
    "control_algorithm/daemon_control.py",
    "control_algorithm/maintenance.py",
    "control_algorithm/config.py",
    "energy_monitor/ha_live_reader.py",
    "miner_control.py",
    "energy_monitor/dashboard_login_miners.py",
    "energy_monitor/auto_control.py",
)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def read_heartbeat(base_dir: Path = BACKEND_DIR) -> Dict[str, Any]:
    """Heartbeat aus RAM-Cache oder SD-Fallback lesen."""
    try:
        from ram_cache import is_enabled, read_json

        if is_enabled():
            data = read_json(base_dir, "control_algorithm_heartbeat.json", default={}, scope="backend")
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    path = base_dir / "control_algorithm_heartbeat.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_algo_state(base_dir: Path = BACKEND_DIR) -> Dict[str, Any]:
    try:
        from control_algorithm.config import load_state

        state = load_state()
        return state if isinstance(state, dict) else {}
    except Exception:
        path = base_dir / "control_algorithm_state.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def check_processes(
    services: Tuple[ServiceSpec, ...] = MANAGED_SERVICES,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    tg_ok = telegram_configured()
    for spec in services:
        running = _proc_running(spec.script)
        critical = spec.critical
        # Ohne Token: Telegram nicht als kritischen Ausfall melden
        if spec.optional and spec.script == "minerbot.py" and not tg_ok:
            critical = False
            running = True
        rows.append(
            {
                "script": spec.script,
                "label": spec.label,
                "critical": critical,
                "optional": spec.optional,
                "running": running,
            }
        )
    return rows


def check_critical_sources(
    base_dir: Path = BACKEND_DIR,
    modules: Tuple[str, ...] = CRITICAL_SOURCE_MODULES,
) -> Dict[str, Any]:
    """Fehlende .py-Dateien erkennen (Rest-.pyc täuscht sonst Import-OK vor)."""
    missing: List[str] = []
    for rel in modules:
        if not (base_dir / rel).is_file():
            missing.append(rel)
    return {
        "ok": not missing,
        "missing": missing,
        "checked": len(modules),
    }


def check_ha_sync_freshness(
    *,
    max_age_sec: int = 180,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """HA→Dashboard Sync tot? (stille except:pass im Flask-Thread)."""
    now = now or datetime.now()
    candidates: List[Path] = []
    try:
        from ram_cache import live_energy_path

        candidates.append(Path(live_energy_path(str(BACKEND_DIR), "pv_value.txt")))
    except Exception:
        pass
    candidates.append(BACKEND_DIR / "energy_monitor" / "pv_value.txt")
    age_sec: Optional[int] = None
    path_used: Optional[str] = None
    for path in candidates:
        try:
            if path.is_file():
                age_sec = int(now.timestamp() - path.stat().st_mtime)
                path_used = str(path)
                break
        except OSError:
            continue
    issues: List[str] = []
    if age_sec is None:
        issues.append("ha_sync_file_missing")
    elif age_sec > max_age_sec:
        issues.append(f"ha_sync_stale_{age_sec}s")
    return {
        "ok": not issues,
        "issues": issues,
        "age_sec": age_sec,
        "path": path_used,
    }


def check_heartbeat(
    heartbeat: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_age_sec: int = 420,
    max_tick_age_sec: int = 1200,
    process_running: Optional[bool] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    issues: List[str] = []
    hb_ts = _parse_iso(heartbeat.get("ts"))
    tick_ts = _parse_iso(heartbeat.get("last_tick"))
    hb_age = int((now - hb_ts).total_seconds()) if hb_ts else None
    tick_age = int((now - tick_ts).total_seconds()) if tick_ts else None
    if hb_age is None:
        issues.append("heartbeat_missing")
    elif hb_age > max_age_sec:
        tick_ok_enough = tick_age is not None and tick_age <= max_tick_age_sec
        if not (process_running and tick_ok_enough):
            issues.append(f"heartbeat_stale_{hb_age}s")
    if tick_age is None:
        issues.append("last_tick_missing")
    elif tick_age > max_tick_age_sec:
        issues.append(f"last_tick_stale_{tick_age}s")
    return {
        "ok": not issues,
        "issues": issues,
        "heartbeat_age_sec": hb_age,
        "tick_age_sec": tick_age,
        "daemon_status": heartbeat.get("daemon_status"),
    }


def check_calibration(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    verify_after_hour: int = 23,
    verify_after_minute: int = 35,
) -> Dict[str, Any]:
    now = now or datetime.now()
    ch = int(config.get("calibration_hour", 23))
    cm = int(config.get("calibration_minute", 0))
    key = f"cal_{now.date().isoformat()}"
    last_key = str(state.get("last_calibration_run_key") or "")
    verify_at = now.replace(hour=verify_after_hour, minute=verify_after_minute, second=0, microsecond=0)
    due = is_calibration_due(now, ch, cm, last_key)
    missed = now >= verify_at and last_key != key
    return {
        "ok": not missed,
        "due_now": due,
        "missed_today": missed,
        "expected_key": key,
        "last_key": last_key or None,
        "last_calibration": state.get("last_calibration"),
    }


def collect_health_report(
    config: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now()
    if config is None:
        from control_algorithm.config import load_config

        config = load_config()
    heartbeat = read_heartbeat()
    state = read_algo_state()
    processes = check_processes()
    steering_running = any(
        p.get("script") == "control_algorithm_daemon.py" and p.get("running")
        for p in processes
    )
    hb = check_heartbeat(heartbeat, now=now, process_running=steering_running)
    cal = check_calibration(config, state, now=now)
    sources = check_critical_sources()
    ha_sync = check_ha_sync_freshness(now=now)
    missing_critical = [
        p["label"] for p in processes if p["critical"] and not p["running"]
    ]
    issues: List[str] = []
    issues.extend(hb.get("issues") or [])
    if missing_critical:
        issues.append("missing_critical:" + ",".join(missing_critical))
    if cal.get("missed_today"):
        issues.append("calibration_missed_today")
    if not sources.get("ok"):
        issues.append("missing_source:" + ",".join(sources.get("missing") or []))
    issues.extend(ha_sync.get("issues") or [])
    return {
        "ts": now.isoformat(),
        "ok": not issues,
        "issues": issues,
        "processes": processes,
        "heartbeat": hb,
        "calibration": cal,
        "missing_critical": missing_critical,
        "critical_sources": sources,
        "ha_sync": ha_sync,
    }


def should_run_nightly_recovery(
    report: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    hour: int = 0,
    minute: int = 5,
    window_minutes: int = 10,
) -> bool:
    """Einmal pro Nacht (00:05–00:15): Voll-Recovery wenn Probleme bestehen."""
    now = now or datetime.now()
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=window_minutes)
    if not (start <= now < end):
        return False
    return not bool(report.get("ok"))


def telegram_configured(base_dir: Path = BACKEND_DIR) -> bool:
    path = base_dir / "telegram_config.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = str((data or {}).get("telegram_token") or "").strip()
        return bool(token and token != '""')
    except Exception:
        return False
