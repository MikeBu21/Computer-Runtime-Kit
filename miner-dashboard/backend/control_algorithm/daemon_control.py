# -*- coding: utf-8 -*-
"""Start/Stop des Steuerungsalgorithmus-Hintergrund-Daemons."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
# Repo-Root = minersteuerung/ (venv liegt dort, nicht unter backend/)
ROOT = BACKEND.parent.parent
DAEMON_SCRIPT = BACKEND / "control_algorithm_daemon.py"
LOG_DIR = BACKEND.parent / "logs"
LOG_FILE = LOG_DIR / "Steuerungsalgorithmus.log"


def _python() -> str:
    """Gleicher Interpreter wie start_all.sh / maintenance_watchdog."""
    candidates = (
        ROOT / "venv" / "bin" / "python",
        BACKEND.parent / "venv" / "bin" / "python",
        BACKEND / "venv" / "bin" / "python",
    )
    for venv in candidates:
        if venv.is_file():
            return str(venv)
    return sys.executable


def is_daemon_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "control_algorithm_daemon.py"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def stop_daemon() -> None:
    try:
        subprocess.run(
            ["pkill", "-f", "control_algorithm_daemon.py"],
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.4)
    except Exception:
        pass


def start_daemon() -> bool:
    if is_daemon_running():
        return True
    if not DAEMON_SCRIPT.is_file():
        return False
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fp = open(LOG_FILE, "a", encoding="utf-8")
    try:
        subprocess.Popen(
            [_python(), str(DAEMON_SCRIPT)],
            cwd=str(BACKEND),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(0.6)
        return is_daemon_running()
    except Exception:
        return False


def ensure_control_daemon(*, restart: bool = False) -> bool:
    """
    Daemon sicherstellen.

    Standard (restart=False): laufenden Prozess nicht beenden – wichtig wenn der
    Daemon in einem lxterminal-Fenster läuft (start_all.sh). Nur starten, falls
    noch keiner aktiv ist.

    restart=True: expliziter Neustart (stop + start), z. B. für Wartung.
    """
    if restart and is_daemon_running():
        stop_daemon()
    if is_daemon_running():
        return True
    return start_daemon()
