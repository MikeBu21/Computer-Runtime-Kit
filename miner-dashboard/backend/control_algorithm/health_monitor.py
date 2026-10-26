# -*- coding: utf-8 -*-
"""
Health-/Heartbeat-Logs für das Steuerungsalgorithmus-Terminal.

Zeigt jede Minute mit Zeitstempel den Status von Wetter, Verbrauch,
Planer, Steuerung und begleitenden Prozessen.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Prozessmuster → Anzeigename
MONITORED_PROCS: List[Tuple[str, str]] = [
    ("control_algorithm_daemon.py", "Steuerung"),
    ("auto_control.py", "EnergyMonitor"),
    ("minerbot.py", "Telegram"),
    ("scheduler.py", "Zeitsteuerung"),
    # Batterie/SoC kommt über HA (ha_live_reader) – alter scraper.py wird nicht mehr gestartet.
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(line: str, *, stream=None) -> None:
    """Eine Zeile mit Zeitstempel auf stdout (Terminal) schreiben."""
    out = stream or sys.stdout
    print(f"[{_ts()}] {line}", flush=True, file=out)


def _age_str(iso: Any) -> str:
    if not iso:
        return "nie"
    try:
        dt = datetime.fromisoformat(str(iso))
        sec = max(0, int((datetime.now() - dt).total_seconds()))
    except Exception:
        return "?"
    if sec < 90:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _proc_running(pattern: str) -> bool:
    """True nur bei echtem Prozess – nicht bei bash/lxterminal-Wrappern nach Crash."""
    try:
        r = subprocess.run(
            ["ps", "-eo", "args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return False
        wrapper_bins = {
            "bash",
            "sh",
            "dash",
            "lxterminal",
            "xterm",
            "gnome-terminal",
            "xfce4-terminal",
            "screen",
            "tmux",
        }
        for cmd in (r.stdout or "").splitlines():
            cmd = cmd.strip()
            if not cmd or pattern not in cmd:
                continue
            first = cmd.split(None, 1)[0]
            base = first.rsplit("/", 1)[-1]
            if base in wrapper_bins:
                continue
            return True
        return False
    except Exception:
        return False


def process_health() -> List[Dict[str, Any]]:
    rows = []
    for pattern, name in MONITORED_PROCS:
        ok = _proc_running(pattern)
        rows.append({"name": name, "pattern": pattern, "ok": ok})
    return rows


def collect_subsystem_status(
    config: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Kompakte Statusdaten aller Teil-Algos."""
    from control_algorithm.config import load_config, load_state
    from control_algorithm.consumption_history import profile_sample_count
    from control_algorithm.live_data import load_live_snapshot

    config = config if config is not None else load_config()
    state = state if state is not None else load_state()
    try:
        live = live if live is not None else load_live_snapshot(str(BACKEND_DIR))
    except Exception:
        live = live or {}

    hp = state.get("horizon_projection") or state.get("balance_projection") or {}
    weather_ctx = hp.get("weather_context") or {}
    bad_days = weather_ctx.get("bad_streak_days")
    if bad_days is None:
        bad_days = (state.get("energy_week_plan") or {}).get("bad_streak_days")

    samples = 0
    try:
        samples = profile_sample_count(str(BACKEND_DIR))
    except Exception:
        pass

    miner_status = (live or {}).get("miner_status") or {}
    running = sum(1 for s in miner_status.values() if str(s).lower() == "running")
    paused = sum(
        1 for s in miner_status.values() if str(s).lower() in ("paused", "stopped", "off")
    )

    return {
        "enabled": bool(config.get("enabled")),
        "dry_run": bool(config.get("dry_run")),
        "daemon_status": state.get("daemon_status") or "?",
        "last_tick": state.get("last_tick"),
        "last_weather_fetch": state.get("last_weather_fetch"),
        "pv_today": state.get("pv_yield_today_kwh"),
        "pv_tomorrow": state.get("pv_yield_tomorrow_kwh"),
        "soc": (live or {}).get("soc_percent") if live else state.get("live_soc"),
        "pv_kw": (live or {}).get("pv_kw") if live else state.get("live_pv_kw"),
        "budget_kw": hp.get("miner_budget_kw"),
        "binding": hp.get("binding_constraint"),
        "projected_soc": hp.get("projected_soc"),
        "target_soc": hp.get("target_soc"),
        "export_guard": bool(state.get("export_guard_active")),
        "export_floor_kw": state.get("export_guard_floor_kw"),
        "night_lock": bool(state.get("night_lock_active")),
        "anomaly": bool(state.get("anomaly_active")),
        "consumption_samples": samples,
        "consumption_days": config.get("consumption_history_days"),
        "bad_streak_days": bad_days,
        "miners_running": running,
        "miners_paused": paused,
        "planned_actions": len(state.get("planned_actions") or []),
        "processes": process_health(),
    }


def format_heartbeat(status: Dict[str, Any], *, tick_ok: Optional[bool] = None) -> List[str]:
    """Mehrzeiliger Heartbeat fürs Terminal."""
    lines: List[str] = []
    lines.append("──────── Health-Check (alle Algos) ────────")

    tick_flag = "OK" if tick_ok is None else ("OK" if tick_ok else "FEHLER")
    en = "AN" if status.get("enabled") else "AUS"
    dry = "DRY" if status.get("dry_run") else "LIVE"
    lines.append(
        f"[Steuerung]  {tick_flag} | {en}/{dry} | tick vor {_age_str(status.get('last_tick'))} "
        f"| status={status.get('daemon_status')}"
    )

    pv_t = status.get("pv_today")
    pv_m = status.get("pv_tomorrow")
    pv_t_s = f"{float(pv_t):.0f} kWh" if pv_t is not None else "?"
    pv_m_s = f"{float(pv_m):.0f} kWh" if pv_m is not None else "?"
    lines.append(
        f"[Wetteralgo] OK | fetch vor {_age_str(status.get('last_weather_fetch'))} "
        f"| heute {pv_t_s} | morgen {pv_m_s}"
        + (
            f" | Schlechtwetter-Streak {status.get('bad_streak_days')}d"
            if status.get("bad_streak_days")
            else ""
        )
    )

    lines.append(
        f"[Verbrauch]  OK | Historie-Tage cfg={status.get('consumption_days')} "
        f"| Profil-Samples {status.get('consumption_samples')}"
    )

    soc = status.get("soc")
    soc_s = f"{float(soc):.0f}%" if soc is not None else "?"
    bud = status.get("budget_kw")
    bud_s = f"{float(bud):.1f} kW" if bud is not None else "?"
    proj = status.get("projected_soc")
    proj_s = f"{float(proj):.0f}%" if proj is not None else "?"
    tgt = status.get("target_soc")
    tgt_s = f"{float(tgt):.0f}%" if tgt is not None else "?"
    guard = "AN" if status.get("export_guard") else "aus"
    lines.append(
        f"[Planer]     OK | SOC {soc_s} → Ziel {tgt_s} (Prog. {proj_s}) "
        f"| Budget {bud_s} | Binding {status.get('binding') or '-'} "
        f"| Export-Guard {guard}"
    )

    pv_kw = status.get("pv_kw")
    pv_kw_s = f"{float(pv_kw):.1f} kW" if pv_kw is not None else "?"
    flags = []
    if status.get("night_lock"):
        flags.append("Nacht-Sperre")
    if status.get("anomaly"):
        flags.append("Anomalie")
    flag_s = (" | " + ", ".join(flags)) if flags else ""
    lines.append(
        f"[Last/Geräte] OK | PV {pv_kw_s} | Miner an={status.get('miners_running')} "
        f"aus={status.get('miners_paused')} | geplante Aktionen {status.get('planned_actions')}"
        f"{flag_s}"
    )

    procs = status.get("processes") or []
    parts = []
    for p in procs:
        mark = "✓" if p.get("ok") else "✗"
        parts.append(f"{p.get('name')}{mark}")
    lines.append("[Prozesse]   " + " | ".join(parts))
    lines.append("──────────────────────────────────────────")
    return lines


def print_heartbeat(
    config: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
    *,
    tick_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    status = collect_subsystem_status(config, state, live)
    for line in format_heartbeat(status, tick_ok=tick_ok):
        log(line)
    return status


def write_heartbeat_file(status: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Maschinenlesbarer Heartbeat für externe Watchdogs."""
    import json

    path = path or (BACKEND_DIR / "control_algorithm_heartbeat.json")
    payload = {
        "ts": datetime.now().isoformat(),
        "pid": os.getpid(),
        **{k: v for k, v in status.items() if k != "processes"},
        "processes": status.get("processes") or [],
    }
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
