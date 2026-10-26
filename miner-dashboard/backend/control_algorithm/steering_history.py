# -*- coding: utf-8 -*-
"""Protokoll: Miner Ein/Aus durch den Steuerungsalgorithmus inkl. Dauer und Grund."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None  # type: ignore

MAX_SESSIONS = 200
MAX_EVENTS = 400
# Abgeschlossene Einträge älter als RETENTION_DAYS werden gelöscht.
# 14 Tage: UI bleibt brauchbar, History-JSON bleibt klein (weniger CPU beim Tick).
RETENTION_DAYS = 14

_log = logging.getLogger(__name__)


def history_path(base_dir: str) -> Path:
    return Path(base_dir) / "control_algorithm_steering_history.json"


def _lock_path(base_dir: str) -> Path:
    return Path(base_dir) / "control_algorithm_steering_history.lock"


def _empty_store() -> Dict[str, Any]:
    return {"sessions": [], "open_by_ip": {}, "events": []}


def _normalize_store(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _empty_store()
    data.setdefault("sessions", [])
    data.setdefault("open_by_ip", {})
    data.setdefault("events", [])
    return data


def _store_fingerprint(data: Dict[str, Any]) -> tuple:
    """Leichtgewichtiger Vergleich, ob Speichern nötig ist."""
    sessions = data.get("sessions") or []
    events = data.get("events") or []
    open_map = data.get("open_by_ip") or {}
    last_event_ts = events[-1].get("ts") if events else None
    last_session = sessions[-1] if sessions else {}
    return (
        len(sessions),
        len(events),
        len(open_map),
        last_event_ts,
        last_session.get("resumed_at"),
        last_session.get("paused_at"),
        tuple(sorted(str(k) for k in open_map.keys())),
    )


def load_steering_history(base_dir: str) -> Dict[str, Any]:
    path = history_path(base_dir)
    if not path.is_file():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_store(data)
    except Exception:
        return _empty_store()


def save_steering_history(base_dir: str, data: Dict[str, Any]) -> bool:
    """Atomar speichern + Rechte setzen. True bei Erfolg (mit Verify)."""
    path = history_path(base_dir)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp_name: Optional[str] = None
    last_exc: Optional[BaseException] = None
    for attempt in range(2):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix="steering_history_",
                suffix=".tmp",
                dir=str(path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, path)
                tmp_name = None
                try:
                    os.chmod(path, 0o664)
                except OSError:
                    pass
                # Verify: Datei lesbar und Event-/Session-Zähler passen
                try:
                    disk = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(disk, dict):
                        raise ValueError("Protokoll-Datei ist kein Objekt")
                    if len(disk.get("events") or []) != len(data.get("events") or []):
                        raise ValueError(
                            f"Verify events mismatch disk={len(disk.get('events') or [])} "
                            f"expected={len(data.get('events') or [])}"
                        )
                    if len(disk.get("sessions") or []) != len(data.get("sessions") or []):
                        raise ValueError(
                            f"Verify sessions mismatch disk={len(disk.get('sessions') or [])} "
                            f"expected={len(data.get('sessions') or [])}"
                        )
                except Exception as verify_exc:
                    last_exc = verify_exc
                    _log.error(
                        "Steuerungs-Protokoll Verify fehlgeschlagen (%s): %s",
                        path,
                        verify_exc,
                    )
                    continue
                return True
            finally:
                if tmp_name and os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except OSError as exc:
            last_exc = exc
            _log.error(
                "Steuerungs-Protokoll konnte nicht gespeichert werden (%s) Versuch %s: %s",
                path,
                attempt + 1,
                exc,
            )
    _log.error(
        "Steuerungs-Protokoll endgültig nicht speicherbar (%s): %s",
        path,
        last_exc,
    )
    return False


def _with_history_lock(base_dir: str, fn):
    """Exklusives Lock für Read-Modify-Write (Daemon-Tick vs. Status-API)."""
    lock_path = _lock_path(base_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 'a+' damit die Datei existiert; Lock ist unabhängig vom JSON-Inhalt.
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _rebuild_open_by_ip(sessions: List[Dict[str, Any]]) -> Dict[str, int]:
    """Nur die jeweils letzte offene Session pro IP behalten; ältere Orphans schließen."""
    open_by_ip: Dict[str, int] = {}
    for i, s in enumerate(sessions):
        if s.get("resume_only"):
            continue
        if s.get("resumed_at") is None and s.get("ip") and s.get("paused_at"):
            ip = str(s["ip"])
            prev = open_by_ip.get(ip)
            if prev is not None:
                older = sessions[prev]
                if older.get("resumed_at") is None:
                    older["resumed_at"] = older.get("paused_at")
                    older["resume_reason"] = "ersetzt (Doppel-Protokoll bereinigt)"
                    older["duration_sec"] = 0.0
            open_by_ip[ip] = i
    return open_by_ip


def prune_steering_history(
    store: Dict[str, Any],
    now: Optional[datetime] = None,
    retention_days: int = RETENTION_DAYS,
) -> Dict[str, Any]:
    """
    Löscht abgeschlossene Sessions/Events älter als retention_days.
    Offene Aus-Sessions bleiben erhalten (sonst fehlt später der Ein-Grund).
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    sessions: List[Dict[str, Any]] = list(store.get("sessions") or [])
    kept: List[Dict[str, Any]] = []
    for s in sessions:
        paused = _parse_iso(str(s.get("paused_at") or ""))
        resumed = _parse_iso(str(s.get("resumed_at") or ""))
        # Offene echte Pause: behalten, bis Resume kommt
        if s.get("paused_at") and s.get("resumed_at") is None and not s.get("resume_only"):
            kept.append(s)
            continue
        anchor = resumed or paused
        if anchor is None:
            continue
        if anchor < cutoff:
            continue
        kept.append(s)
    if len(kept) > MAX_SESSIONS:
        # Offene Sessions priorisieren
        open_s = [s for s in kept if s.get("resumed_at") is None and not s.get("resume_only")]
        closed = [s for s in kept if s.get("resumed_at") is not None or s.get("resume_only")]
        closed = closed[-(MAX_SESSIONS - len(open_s)) :] if len(open_s) < MAX_SESSIONS else []
        kept = open_s + closed

    events: List[Dict[str, Any]] = []
    for e in store.get("events") or []:
        ts = _parse_iso(str(e.get("ts") or ""))
        if ts is None or ts < cutoff:
            continue
        events.append(e)
    if len(events) > MAX_EVENTS:
        events = events[-MAX_EVENTS:]

    store["sessions"] = kept
    store["events"] = events
    store["open_by_ip"] = _rebuild_open_by_ip(kept)
    return store


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "—"
    sec = int(round(seconds))
    if sec < 60:
        return f"{sec} s"
    mins = sec // 60
    if mins < 60:
        return f"{mins} min"
    hours = mins // 60
    rem_m = mins % 60
    if rem_m:
        return f"{hours} h {rem_m} min"
    return f"{hours} h"


def _format_display_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "—"
    dt = _parse_iso(str(iso_ts))
    if not dt:
        return str(iso_ts)
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def _session_row(session: Dict[str, Any], *, active: bool = False) -> Dict[str, Any]:
    paused_at = session.get("paused_at")
    resumed_at = session.get("resumed_at")
    duration_sec = session.get("duration_sec")
    resume_only = bool(session.get("resume_only"))
    if active and paused_at and duration_sec is None:
        start = _parse_iso(str(paused_at))
        if start:
            duration_sec = (datetime.now() - start).total_seconds()

    if active:
        resumed_display = "noch aus"
    elif resumed_at:
        resumed_display = _format_display_time(resumed_at)
    else:
        resumed_display = "—"

    return {
        "ip": session.get("ip"),
        "name": session.get("name") or session.get("ip"),
        "paused_at": paused_at,
        "paused_at_display": _format_display_time(paused_at) if paused_at else "—",
        "resumed_at": resumed_at,
        "resumed_at_display": resumed_display,
        "duration_sec": round(float(duration_sec), 1) if duration_sec is not None else None,
        "duration_display": format_duration(duration_sec) if duration_sec is not None else "—",
        "pause_reason": session.get("pause_reason") or "—",
        "resume_reason": session.get("resume_reason") or "—",
        "dry_run": bool(session.get("dry_run")),
        "force": bool(session.get("force")),
        "active": active,
        "resume_only": resume_only,
        "kind": "resume_only" if resume_only else ("active_pause" if active else "cycle"),
    }


def _event_row(event: Dict[str, Any]) -> Dict[str, Any]:
    action = event.get("action")
    return {
        "ip": event.get("ip"),
        "name": event.get("name") or event.get("ip"),
        "ts": event.get("ts"),
        "ts_display": _format_display_time(event.get("ts")),
        "action": action,
        "action_label": "Einschalten" if action == "resume" else "Ausschalten",
        "reason": event.get("reason") or "—",
        "dry_run": bool(event.get("dry_run")),
        "force": bool(event.get("force")),
    }


def _append_event(
    events: List[Dict[str, Any]],
    *,
    now_iso: str,
    ip: str,
    name: str,
    action: str,
    reason: str,
    dry_run: bool,
    force: bool,
) -> None:
    events.append(
        {
            "ts": now_iso,
            "ip": ip,
            "name": name,
            "action": action,
            "reason": reason,
            "dry_run": dry_run,
            "force": force,
        }
    )


def open_pause_ips(base_dir: str) -> set:
    """Aktuell offene Aus-IPs (für Abgleich Live ↔ Protokoll)."""
    store = load_steering_history(base_dir)
    return {str(ip) for ip in (store.get("open_by_ip") or {}).keys()}


def _event_key(ip: Any, action: Any, ts: Any) -> str:
    return f"{ip}|{action}|{ts}"


def backfill_events_from_sessions(store: Dict[str, Any]) -> bool:
    """
    Sessions ohne passendes Event (Altbestand / Save-Lücken) → Events nachziehen.
    Returns True wenn etwas ergänzt wurde.
    """
    sessions: List[Dict[str, Any]] = list(store.get("sessions") or [])
    events: List[Dict[str, Any]] = list(store.get("events") or [])
    existing = {
        _event_key(e.get("ip"), e.get("action"), e.get("ts"))
        for e in events
    }
    changed = False
    for s in sessions:
        ip = s.get("ip")
        name = s.get("name") or ip
        dry_run = bool(s.get("dry_run"))
        force = bool(s.get("force"))
        paused_at = s.get("paused_at")
        if paused_at and not s.get("resume_only"):
            key = _event_key(ip, "pause", paused_at)
            if key not in existing:
                _append_event(
                    events,
                    now_iso=str(paused_at),
                    ip=str(ip),
                    name=str(name),
                    action="pause",
                    reason=str(s.get("pause_reason") or "Steuerungsalgorithmus"),
                    dry_run=dry_run,
                    force=force,
                )
                existing.add(key)
                changed = True
        resumed_at = s.get("resumed_at")
        if resumed_at and not s.get("silent_sync"):
            key = _event_key(ip, "resume", resumed_at)
            if key not in existing:
                _append_event(
                    events,
                    now_iso=str(resumed_at),
                    ip=str(ip),
                    name=str(name),
                    action="resume",
                    reason=str(s.get("resume_reason") or "Steuerungsalgorithmus"),
                    dry_run=dry_run,
                    force=force,
                )
                existing.add(key)
                changed = True
    if changed:
        events.sort(key=lambda e: str(e.get("ts") or ""))
        if len(events) > MAX_EVENTS:
            events = events[-MAX_EVENTS:]
        store["events"] = events
    return changed


def close_open_pauses_silently(
    base_dir: str,
    running_ips: Any,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Schließt offene Aus-Sessions ohne Event, wenn Live-Status wieder running ist.

    Kein Schein-„Einschalten“ im Protokoll – nur Buchhaltung (hängende Aus-Einträge).
    """
    now = now or datetime.now()
    ips = {str(ip) for ip in (running_ips or []) if ip}

    def _mutate() -> Dict[str, Any]:
        before = load_steering_history(base_dir)
        before_fp = _store_fingerprint(before)
        store = prune_steering_history(before, now=now)
        sessions: List[Dict[str, Any]] = list(store.get("sessions") or [])
        open_by_ip: Dict[str, int] = dict(store.get("open_by_ip") or {})
        now_iso = now.isoformat()
        changed = False
        for ip in list(open_by_ip.keys()):
            if ip not in ips:
                continue
            idx = open_by_ip.get(ip)
            if idx is None or not (0 <= idx < len(sessions)):
                open_by_ip.pop(ip, None)
                changed = True
                continue
            session = sessions[idx]
            if session.get("resumed_at") is not None or session.get("resume_only"):
                open_by_ip.pop(ip, None)
                changed = True
                continue
            session["resumed_at"] = now_iso
            session["resume_reason"] = "Status-Sync (kein Steuerungsbefehl)"
            start = _parse_iso(str(session.get("paused_at") or ""))
            if start:
                session["duration_sec"] = round((now - start).total_seconds(), 1)
            session["silent_sync"] = True
            open_by_ip.pop(ip, None)
            changed = True
        if changed:
            store["sessions"] = sessions
            store["open_by_ip"] = open_by_ip
            if not save_steering_history(base_dir, store):
                _log.error("Steuerungs-Protokoll: stiller Sync nicht speicherbar")
        elif _store_fingerprint(store) != before_fp:
            if not save_steering_history(base_dir, store):
                _log.error("Steuerungs-Protokoll: Prune nach Sync nicht speicherbar")
        return store

    return _with_history_lock(base_dir, _mutate)


def record_steering_actions(
    base_dir: str,
    applied_actions: List[Dict[str, Any]],
    miner_names: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Protokolliert angewendete pause/resume-Aktionen (auch dry_run). Jede Aktion = Event."""
    now = now or datetime.now()
    miner_names = miner_names or {}

    def _mutate() -> Dict[str, Any]:
        # Unter Lock erneut laden – sonst überschreibt parallele Status-API den Tick.
        before = load_steering_history(base_dir)
        before_fp = _store_fingerprint(before)
        store = prune_steering_history(before, now=now)
        backfilled = backfill_events_from_sessions(store)

        if not applied_actions:
            if backfilled or _store_fingerprint(store) != before_fp:
                if not save_steering_history(base_dir, store):
                    _log.error("Steuerungs-Protokoll: Backfill/Prune nicht speicherbar")
            return store

        now_iso = now.isoformat()
        sessions: List[Dict[str, Any]] = list(store.get("sessions") or [])
        events: List[Dict[str, Any]] = list(store.get("events") or [])
        open_by_ip: Dict[str, int] = dict(store.get("open_by_ip") or {})
        events_before = len(events)

        for act in applied_actions:
            ip = act.get("ip")
            action = act.get("action")
            if not ip or action not in ("pause", "resume"):
                continue
            ip = str(ip)

            name = miner_names.get(ip) or ip
            reason = str(act.get("reason") or "Steuerungsalgorithmus")
            dry_run = bool(act.get("dry_run"))
            force = bool(act.get("force"))

            if action == "pause":
                prev_idx = open_by_ip.get(ip)
                if prev_idx is not None and 0 <= prev_idx < len(sessions):
                    prev = sessions[prev_idx]
                    if prev.get("resumed_at") is None and not prev.get("resume_only"):
                        # Session schon offen = Gerät gilt bereits als aus.
                        # Nur Grund aktualisieren, kein weiteres Aus-Event (kein Spam).
                        prev["pause_reason"] = reason
                        prev["name"] = name
                        prev["dry_run"] = dry_run
                        prev["force"] = force
                        continue
                _append_event(
                    events,
                    now_iso=now_iso,
                    ip=ip,
                    name=name,
                    action="pause",
                    reason=reason,
                    dry_run=dry_run,
                    force=force,
                )
                session = {
                    "ip": ip,
                    "name": name,
                    "paused_at": now_iso,
                    "pause_reason": reason,
                    "resumed_at": None,
                    "resume_reason": None,
                    "duration_sec": None,
                    "dry_run": dry_run,
                    "force": force,
                }
                sessions.append(session)
                open_by_ip[ip] = len(sessions) - 1

            elif action == "resume":
                # Jede Einschaltung immer als Event dokumentieren
                _append_event(
                    events,
                    now_iso=now_iso,
                    ip=ip,
                    name=name,
                    action="resume",
                    reason=reason,
                    dry_run=dry_run,
                    force=force,
                )
                idx = open_by_ip.pop(ip, None)
                closed = False
                if idx is not None and 0 <= idx < len(sessions):
                    session = sessions[idx]
                    if session.get("resumed_at") is None and not session.get("resume_only"):
                        session["resumed_at"] = now_iso
                        session["resume_reason"] = reason
                        start = _parse_iso(str(session.get("paused_at") or ""))
                        if start:
                            session["duration_sec"] = round(
                                (now - start).total_seconds(), 1
                            )
                        closed = True
                if not closed:
                    # Keine offene Pause (gelöscht/Neustart) → Einschaltung trotzdem speichern
                    sessions.append(
                        {
                            "ip": ip,
                            "name": name,
                            "paused_at": None,
                            "pause_reason": "— (kein offener Aus-Eintrag)",
                            "resumed_at": now_iso,
                            "resume_reason": reason,
                            "duration_sec": None,
                            "dry_run": dry_run,
                            "force": force,
                            "resume_only": True,
                        }
                    )

        if len(sessions) > MAX_SESSIONS:
            open_s = [
                s
                for s in sessions
                if s.get("resumed_at") is None and not s.get("resume_only")
            ]
            closed = [
                s
                for s in sessions
                if s.get("resumed_at") is not None or s.get("resume_only")
            ]
            budget = max(0, MAX_SESSIONS - len(open_s))
            sessions = open_s + closed[-budget:]
            open_by_ip = _rebuild_open_by_ip(sessions)
        if len(events) > MAX_EVENTS:
            events = events[-MAX_EVENTS:]

        store["sessions"] = sessions
        store["events"] = events
        store["open_by_ip"] = open_by_ip
        if not save_steering_history(base_dir, store):
            _log.error(
                "Steuerungs-Protokoll: %s Aktionen nicht persistent "
                "(events %s→%s) – nächster Tick versucht erneut",
                len(applied_actions),
                events_before,
                len(events),
            )
        return store

    return _with_history_lock(base_dir, _mutate)


def build_steering_stats_payload(base_dir: str) -> Dict[str, Any]:
    def _build() -> Dict[str, Any]:
        before = load_steering_history(base_dir)
        before_fp = _store_fingerprint(before)
        store = prune_steering_history(before)
        backfilled = backfill_events_from_sessions(store)
        # Nur speichern wenn Prune/Backfill etwas geändert hat – sonst überschreibt die
        # Status-API parallele Tick-Schreibvorgänge mit einem veralteten Stand.
        if backfilled or _store_fingerprint(store) != before_fp:
            save_steering_history(base_dir, store)
        return store

    store = _with_history_lock(base_dir, _build)
    sessions: List[Dict[str, Any]] = list(store.get("sessions") or [])
    events: List[Dict[str, Any]] = list(store.get("events") or [])
    open_by_ip: Dict[str, int] = dict(store.get("open_by_ip") or {})

    active_rows: List[Dict[str, Any]] = []
    for ip, idx in open_by_ip.items():
        if 0 <= idx < len(sessions):
            active_rows.append(_session_row(sessions[idx], active=True))

    completed = [
        _session_row(s)
        for s in reversed(sessions)
        if s.get("resumed_at") is not None and not s.get("silent_sync")
    ][:80]

    active_rows.sort(key=lambda r: r.get("paused_at") or "", reverse=True)

    recent_events = [_event_row(e) for e in reversed(events)][:100]

    total_pauses = sum(1 for e in events if e.get("action") == "pause")
    total_resumes = sum(1 for e in events if e.get("action") == "resume")
    # Fallback falls Events noch leer (alte Dateien)
    if not events:
        total_pauses = sum(1 for s in sessions if s.get("paused_at"))
        total_resumes = sum(1 for s in sessions if s.get("resumed_at"))

    durations = [
        float(s["duration_sec"])
        for s in sessions
        if s.get("duration_sec") is not None
        and not s.get("resume_only")
        and not s.get("silent_sync")
    ]
    avg_duration_sec = round(sum(durations) / len(durations), 1) if durations else None

    return {
        "active_pauses": active_rows,
        "recent_sessions": completed,
        "recent_events": recent_events,
        "summary": {
            "total_pause_events": total_pauses,
            "total_resume_events": total_resumes,
            "completed_sessions": sum(
                1
                for s in sessions
                if s.get("resumed_at")
                and not s.get("resume_only")
                and not s.get("silent_sync")
            ),
            "currently_paused": len(active_rows),
            "avg_off_duration_display": format_duration(avg_duration_sec),
            "avg_off_duration_sec": avg_duration_sec,
        },
    }
