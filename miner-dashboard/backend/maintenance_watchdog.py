#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wartungs-Watchdog für die Miner-Steuerung.

Prüft regelmäßig Prozesse, Heartbeat und Abend-Kalibrierung.
Behebt fehlende Dienste gezielt; bei anhaltenden Problemen um Mitternacht
Voll-Recovery über start_all.sh (headless, mit Logs).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent.parent
START_ALL = BACKEND_DIR.parent / "start_all.sh"
VENV_PYTHON = ROOT_DIR / "venv" / "bin" / "python"

CHECK_INTERVAL_SEC = int(os.environ.get("MAINTENANCE_CHECK_INTERVAL_SEC", "300"))
STATE_FILE = BACKEND_DIR / "maintenance_watchdog_state.json"

from control_algorithm.maintenance import (  # noqa: E402
    MANAGED_SERVICES,
    ServiceSpec,
    collect_health_report,
    should_run_nightly_recovery,
    telegram_configured,
)
from control_algorithm.health_monitor import _proc_running, log  # noqa: E402


def _python() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.is_file() else sys.executable


def _ram_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("MINER_RAM_CACHE", "1")
    env.setdefault("MINER_RAM_ROOT", "/dev/shm/minersteuerung")
    env.setdefault("MINER_RAM_FLUSH_SEC", "3600")
    env.setdefault("MINER_LOG_DIR", "/dev/shm/minersteuerung/logs")
    env.setdefault("MINER_ROOT", str(ROOT_DIR))
    return env


def _log_dir() -> Path:
    path = Path(os.environ.get("MINER_LOG_DIR", "/dev/shm/minersteuerung/logs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_state() -> Dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _logfile_for(label: str) -> Path:
    # Telegram: gleiches Log wie Desktop/start_all, damit Alarme/Fehler sichtbar bleiben
    if label == "Telegram":
        return _log_dir() / "Telegram_Bot.log"
    safe = label.replace(" ", "_").replace("/", "_")
    return _log_dir() / f"maintenance_{safe}.log"


def stop_service(pattern: str) -> None:
    py = _python()
    try:
        subprocess.run(
            ["pkill", "-f", f"{py} {pattern}"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def start_service(spec: ServiceSpec) -> bool:
    if spec.optional and spec.script == "minerbot.py" and not telegram_configured():
        return False
    if _proc_running(spec.script):
        return False
    script_path = BACKEND_DIR / spec.script
    if not script_path.is_file():
        log(f"[Wartung] Script fehlt: {spec.script}")
        return False
    logfile = _logfile_for(spec.label)
    try:
        with open(logfile, "a", encoding="utf-8") as log_fp:
            log_fp.write(f"\n[{datetime.now().isoformat()}] Start {spec.label}\n")
            subprocess.Popen(
                [_python(), str(script_path)],
                cwd=str(BACKEND_DIR),
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=_ram_env(),
            )
        time.sleep(0.8)
        ok = _proc_running(spec.script)
        log(f"[Wartung] {'OK' if ok else 'FEHLER'} Start {spec.label}")
        return ok
    except Exception as exc:
        log(f"[Wartung] Start {spec.label} fehlgeschlagen: {exc}")
        return False


def restart_control_daemon() -> bool:
    try:
        from control_algorithm.daemon_control import ensure_control_daemon

        ok = ensure_control_daemon(restart=True)
        log(f"[Wartung] Steuerungs-Daemon Neustart: {'OK' if ok else 'FEHLER'}")
        return ok
    except Exception as exc:
        log(f"[Wartung] Steuerungs-Daemon Neustart fehlgeschlagen: {exc}")
        return False


def run_missed_calibration() -> bool:
    try:
        from control_algorithm.config import load_config, load_state, save_state
        from control_algorithm.service import maybe_run_calibration

        config = load_config()
        state = load_state()
        before = state.get("last_calibration_run_key")
        state = maybe_run_calibration(config, state)
        after = state.get("last_calibration_run_key")
        save_state(state)
        ok = after != before and after is not None
        log(
            f"[Wartung] Kalibrierung nachgezogen: {'OK' if ok else 'kein Lauf'} "
            f"({before} -> {after})"
        )
        return ok
    except Exception as exc:
        log(f"[Wartung] Kalibrierung fehlgeschlagen: {exc}")
        return False


def stop_all_managed() -> None:
    py = _python()
    patterns = [spec.script for spec in MANAGED_SERVICES]
    for pattern in patterns:
        stop_service(pattern)
    try:
        subprocess.run(["fuser", "-k", "5000/tcp"], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(1.0)


def full_stack_restart() -> bool:
    if not START_ALL.is_file():
        log("[Wartung] start_all.sh fehlt")
        return False
    log("[Wartung] Voll-Recovery: stoppe Dienste, starte headless …")
    stop_all_managed()
    try:
        result = subprocess.run(
            ["bash", str(START_ALL)],
            cwd=str(START_ALL.parent),
            env=_ram_env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines()[-8:]:
                log(f"[Wartung] {line}")
        ok = result.returncode == 0
        log(f"[Wartung] Voll-Recovery: {'OK' if ok else 'FEHLER'}")
        return ok
    except Exception as exc:
        log(f"[Wartung] Voll-Recovery fehlgeschlagen: {exc}")
        return False


def dedupe_managed_processes() -> List[str]:
    """Mehrfach gestartete Managed-Dienste auf je einen Prozess reduzieren."""
    actions: List[str] = []
    py = _python()
    for spec in MANAGED_SERVICES:
        try:
            r = subprocess.run(
                ["pgrep", "-f", f"{py}.*{spec.script}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            continue
        pids = [p for p in (r.stdout or "").split() if p.isdigit()]
        if len(pids) <= 1:
            continue
        # Ältesten behalten (niedrigste PID typisch zuerst gestartet)
        keep, *extras = sorted(pids, key=int)
        for pid in extras:
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
        actions.append(f"dedupe:{spec.label}:{len(extras)}")
        log(
            f"[Wartung] Duplikat {spec.label}: behalte PID {keep}, "
            f"beende {', '.join(extras)}"
        )
    if actions:
        time.sleep(0.5)
    return actions


def apply_recovery(report: Dict[str, Any], *, nightly: bool = False) -> List[str]:
    actions: List[str] = []
    actions.extend(dedupe_managed_processes())
    issues = list(report.get("issues") or [])
    if not issues and not actions:
        return actions

    hb_issues = [i for i in issues if i.startswith(("heartbeat_", "last_tick_"))]
    if hb_issues:
        proc_alive = _proc_running("control_algorithm_daemon.py")
        hard_fail = (not proc_alive) or any(
            i.startswith("last_tick_stale_")
            or i in ("last_tick_missing", "heartbeat_missing")
            for i in hb_issues
        )
        if hard_fail:
            if restart_control_daemon():
                actions.append("restart_control_daemon")
            time.sleep(2.0)
        else:
            log(
                "[Wartung] Heartbeat veraltet, Prozess lebt – kein Kill "
                "(langer Tick, kein Absturz)"
            )

    for proc in report.get("processes") or []:
        # Kritische Dienste immer; Telegram auch wenn optional aber konfiguriert
        should_start = proc.get("critical") and not proc.get("running")
        if (
            not should_start
            and not proc.get("running")
            and proc.get("script") == "minerbot.py"
            and telegram_configured()
        ):
            should_start = True
        if should_start:
            spec = next((s for s in MANAGED_SERVICES if s.script == proc.get("script")), None)
            if spec and start_service(spec):
                actions.append(f"start:{spec.label}")

    cal = report.get("calibration") or {}
    if cal.get("due_now") or cal.get("missed_today"):
        if run_missed_calibration():
            actions.append("run_calibration")

    if nightly and issues:
        if full_stack_restart():
            actions.append("full_stack_restart")

    return actions


def run_check_cycle(*, allow_nightly: bool = True) -> Dict[str, Any]:
    from control_algorithm.config import load_config

    now = datetime.now()
    config = load_config()
    report = collect_health_report(config, now=now)
    wd_state = _load_state()
    nightly = False
    if allow_nightly and should_run_nightly_recovery(report, now=now):
        day_key = now.date().isoformat()
        if wd_state.get("last_nightly_recovery") != day_key:
            nightly = True
            wd_state["last_nightly_recovery"] = day_key

    actions: List[str] = []
    cal = report.get("calibration") or {}
    # Lernen hat Vorrang: ab Kalibrierfenster nachziehen, auch wenn sonst alles OK
    # (exakter Minuten-Treffer entfällt; 10‑Min-Ticks reichen).
    if cal.get("due_now") or cal.get("missed_today"):
        if run_missed_calibration():
            actions.append("run_calibration")

    if not report.get("ok"):
        log(
            "[Wartung] Probleme: "
            + ", ".join(report.get("issues") or ["unbekannt"])
        )
        actions.extend(
            a for a in apply_recovery(report, nightly=nightly) if a not in actions
        )
    elif not actions:
        log("[Wartung] OK – alle Checks bestanden")
    else:
        log("[Wartung] OK – Abend-Lernen nachgezogen")

    entry = {
        "ts": now.isoformat(),
        "ok": report.get("ok"),
        "issues": report.get("issues"),
        "actions": actions,
        "nightly": nightly,
    }
    history = list(wd_state.get("history") or [])
    history.append(entry)
    wd_state["history"] = history[-96:]
    wd_state["last_check"] = now.isoformat()
    wd_state["last_ok"] = bool(report.get("ok"))
    _save_state(wd_state)
    return {"report": report, "actions": actions, "nightly": nightly}


def main() -> None:
    log("[Wartung] Watchdog gestartet")
    log(f"[Wartung] Intervall {CHECK_INTERVAL_SEC}s | Logs {_log_dir()}")
    # Grace: start_all (miner-dashboard) zuerst fertig werden lassen
    boot_grace = int(os.environ.get("MAINTENANCE_BOOT_GRACE_SEC", "90") or 90)
    if boot_grace > 0:
        log(f"[Wartung] Boot-Grace {boot_grace}s – kein Parallel-Start mit start_all")
        time.sleep(boot_grace)
    while True:
        try:
            dedupe_managed_processes()
            run_check_cycle()
        except Exception as exc:
            log(f"[Wartung] Check-Zyklus Fehler: {exc}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        result = run_check_cycle()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0 if result["report"].get("ok") else 1)
    main()
