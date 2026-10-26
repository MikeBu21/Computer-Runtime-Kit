#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hintergrund-Daemon für den Steuerungsalgorithmus.

- Alle ~60s: Health-Log (Wetter, Verbrauch, Planer, Steuerung, Prozesse)
- Alle ~120s: Steuerungs-Tick
- Watchdog: hängende Ticks → sofort Hard-Restart (kein Parallel-Tick im Hintergrund)
- Absturzsicher: Exceptions abfangen, weiterlaufen, bei Dauerfehler neu starten

Start: python3 control_algorithm_daemon.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from control_algorithm.health_monitor import (  # noqa: E402
    log,
    print_heartbeat,
    write_heartbeat_file,
)
from control_algorithm.service import run_tick  # noqa: E402

# Steuerung braucht frische Live-Daten (SOC/PV/Miner).
TICK_INTERVAL_SECONDS = int(os.environ.get("CONTROL_TICK_INTERVAL_SECONDS", "120"))  # 2 Minuten
HEARTBEAT_INTERVAL_SECONDS = 60  # jede Minute Status
# Horizon-/Balance-Rechnung oft 1–3 min auf dem Pi; Cache hält Tick typisch <30s
TICK_TIMEOUT_SECONDS = int(os.environ.get("CONTROL_TICK_TIMEOUT_SECONDS", "180"))
MAX_CONSECUTIVE_TICK_FAILURES = 3
# Ein Hang reicht: Zombie-Tick-Threads belasten die CPU und lösen Folgehänger aus.
MAX_CONSECUTIVE_HANGS = 1
RESTART_COOLDOWN_SECONDS = 60

# Verhindert, dass ein neuer Tick startet, während der vorherige noch hängt.
_tick_lock = threading.Lock()
_active_tick_thread: Optional[threading.Thread] = None


class _TickResult:
    def __init__(self) -> None:
        self.ok = False
        self.error: Optional[str] = None
        self.payload: Optional[Dict[str, Any]] = None
        self.elapsed = 0.0


def _run_tick_with_timeout(timeout: float) -> _TickResult:
    """Führt run_tick in einem Thread aus; bei Timeout = Hang (Caller restartet)."""
    global _active_tick_thread
    result = _TickResult()
    started = time.time()

    if _active_tick_thread is not None and _active_tick_thread.is_alive():
        result.error = "SKIP: vorheriger Tick hängt noch – Restart nötig"
        result.elapsed = 0.0
        return result

    def _worker() -> None:
        try:
            with _tick_lock:
                result.payload = run_tick()
                result.ok = True
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()

    t = threading.Thread(target=_worker, name="control-tick", daemon=True)
    _active_tick_thread = t
    t.start()
    t.join(timeout=timeout)
    result.elapsed = time.time() - started
    if t.is_alive():
        result.ok = False
        result.error = f"TIMEOUT nach {timeout:.0f}s (Tick hängt)"
        log(f"[Watchdog] WARN {result.error} – Hard-Restart folgt (kein Parallel-Tick)")
    else:
        _active_tick_thread = None
    return result


def _self_restart(reason: str) -> None:
    """Ersetzt den aktuellen Prozess (Hard-Restart)."""
    log(f"[Watchdog] HARD-RESTART: {reason}")
    log("[Watchdog] Starte Daemon neu …")
    time.sleep(1.0)
    try:
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    except Exception:
        traceback.print_exc()
        log("[Watchdog] execv fehlgeschlagen – beende Prozess (Supervisor soll neu starten)")
        sys.exit(1)


def _acquire_singleton_lock() -> Any:
    """Nur eine Daemon-Instanz – sonst überschreiben sich Ticks/State gegenseitig."""
    import fcntl

    lock_path = BACKEND / "control_algorithm_daemon.lock"
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        other = (fh.read() or "").strip() or "?"
        log(
            f"[Steuerung] ABBRUCH: Daemon läuft bereits (PID {other}). "
            "Doppelstart beendet – sonst sterben Termine/Schaltungen."
        )
        fh.close()
        sys.exit(0)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> None:
    _singleton_fh = _acquire_singleton_lock()
    log(
        f"[Steuerung] Daemon gestartet | Tick={TICK_INTERVAL_SECONDS}s "
        f"| Heartbeat={HEARTBEAT_INTERVAL_SECONDS}s "
        f"| Tick-Timeout={TICK_TIMEOUT_SECONDS}s | PID={os.getpid()}"
    )
    log(
        "[Steuerung] Überwacht: Wetteralgo, Verbrauchsberechner, Planer, "
        "Laststeuerung, EnergyMonitor, Telegram, Zeitsteuerung"
    )
    try:
        from control_algorithm.hard_soc_guard import (
            HardSocRulesMissing,
            enforce_hard_soc_rules,
        )

        enforce_hard_soc_rules(log)
    except HardSocRulesMissing:
        sys.exit(2)
    except Exception:
        log("[HART-FEHLER] SoC-Regelprüfung abgestürzt – Daemon startet nicht.")
        traceback.print_exc()
        sys.exit(2)

    last_tick_at = 0.0
    last_heartbeat_at = 0.0
    consecutive_failures = 0
    consecutive_hangs = 0
    last_restart_at = 0.0
    last_tick_ok: Optional[bool] = None

    # Sofort erster Heartbeat
    try:
        status = print_heartbeat(tick_ok=None)
        write_heartbeat_file(status)
        last_heartbeat_at = time.time()
    except Exception:
        traceback.print_exc()

    while True:
        now = time.time()
        try:
            # --- Heartbeat jede Minute ---
            if now - last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS:
                try:
                    status = print_heartbeat(tick_ok=last_tick_ok)
                    write_heartbeat_file(status)
                    for p in status.get("processes") or []:
                        if p.get("name") == "Steuerung":
                            continue
                        if not p.get("ok"):
                            log(
                                f"[Watchdog] Prozess fehlt: {p.get('name')} "
                                f"({p.get('pattern')}) – bitte start_all.sh prüfen"
                            )
                except Exception as exc:
                    log(f"[Health] Fehler beim Heartbeat: {exc}")
                    traceback.print_exc()
                last_heartbeat_at = time.time()

            # --- Steuerungs-Tick ---
            if now - last_tick_at >= TICK_INTERVAL_SECONDS:
                log("[Steuerung] Tick startet …")
                result = _run_tick_with_timeout(TICK_TIMEOUT_SECONDS)
                last_tick_at = time.time()

                if result.ok:
                    consecutive_failures = 0
                    consecutive_hangs = 0
                    last_tick_ok = True
                    log(f"[Steuerung] Tick OK in {result.elapsed:.1f}s")
                    try:
                        from control_algorithm.config import load_state

                        st = load_state()
                        msgs = (st.get("info_messages") or [])[:3]
                        for m in msgs:
                            log(f"[Planer] {m}")
                        acts = st.get("planned_actions") or []
                        if acts:
                            for a in acts[:5]:
                                log(
                                    f"[Steuerung] Aktion: {a.get('action')} "
                                    f"{a.get('ip')} – {a.get('reason', '')[:80]}"
                                )
                        else:
                            log("[Steuerung] Keine Geräte-Aktion diesen Tick")
                    except Exception:
                        pass
                else:
                    last_tick_ok = False
                    consecutive_failures += 1
                    is_hang = bool(result.error) and (
                        "TIMEOUT" in str(result.error) or "hängt" in str(result.error)
                    )
                    if is_hang:
                        consecutive_hangs += 1
                    log(
                        f"[Watchdog] Tick FEHLER ({consecutive_failures}/"
                        f"{MAX_CONSECUTIVE_TICK_FAILURES}): {result.error}"
                    )

                    if consecutive_failures >= 2:
                        try:
                            from control_algorithm.config import load_state, save_state

                            st = load_state()
                            st["force_weather_refresh"] = True
                            st["daemon_status"] = f"recovering: {result.error}"
                            save_state(st)
                            log("[Watchdog] Soft-Recovery: force_weather_refresh gesetzt")
                        except Exception:
                            traceback.print_exc()

                    need_restart = (
                        consecutive_failures >= MAX_CONSECUTIVE_TICK_FAILURES
                        or consecutive_hangs >= MAX_CONSECUTIVE_HANGS
                        or is_hang
                    )
                    if need_restart and (time.time() - last_restart_at) >= RESTART_COOLDOWN_SECONDS:
                        last_restart_at = time.time()
                        _self_restart(
                            f"{consecutive_failures} Tick-Fehler / {consecutive_hangs} Hangs"
                        )

            time.sleep(1.0)

        except KeyboardInterrupt:
            log("[Steuerung] Beendet (KeyboardInterrupt).")
            break
        except Exception:
            consecutive_failures += 1
            last_tick_ok = False
            log("[Watchdog] Unerwarteter Fehler in der Hauptschleife:")
            traceback.print_exc()
            if consecutive_failures >= MAX_CONSECUTIVE_TICK_FAILURES:
                if (time.time() - last_restart_at) >= RESTART_COOLDOWN_SECONDS:
                    last_restart_at = time.time()
                    _self_restart("Hauptschleifen-Abstürze")
            time.sleep(5.0)


if __name__ == "__main__":
    main()
