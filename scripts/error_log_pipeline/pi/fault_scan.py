#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fehlerbericht-Erfassung: App-Logs, Systemjournal (inkl. Kernel) und Systemzustand.

Arbeitet inkrementell: Der Hintergrunddienst ruft ``--scan`` regelmäßig auf, damit
nichts verloren geht, wenn die RAM-Logs in /dev/shm bei einem Neustart verschwinden.
Gleichartige Meldungen werden unter einem Fingerprint zusammengefasst (Anzahl,
erstes/letztes Auftreten). ``--collect`` macht daraus am Periodenende ein Paket für
die Outbox, aber nur wenn es etwas zu melden gibt.

Nur Standardbibliothek: der Dienst läuft mit /usr/bin/python3.

CLI:
  --scan      nur erfassen
  --collect   erfassen und ggf. Paket in die Outbox legen (Periode beginnt neu)
  --preview   aktuellen Stand als Bericht ausgeben, ohne etwas zurückzusetzen
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

REPORT_VERSION = 2
MAX_FINDINGS = 400
MAX_MESSAGE = 4000
MAX_TRACEBACK_LINES = 60
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_JOURNAL_ENTRIES = 200_000
MAX_DETAILS = 60

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
SEVERITY_LABEL = {"critical": "KRITISCH", "error": "FEHLER", "warning": "WARNUNG", "info": "INFO"}

SOURCES_DESCRIPTION = [
    "Alle Programm-Logs (*.log) in /dev/shm/minersteuerung/logs und ~/minersteuerung/logs",
    "Python-Abstürze (Traceback) mit Ausnahme und Code-Stelle",
    "HTTP-5xx-Antworten des Dashboards",
    "Systemjournal ab Warnung: Dienste, Kernel, Datenträger, Netzwerk",
    "Systemzustand: Unterspannung/Drosselung, Temperatur, Speicherplatz, RAM, "
    "fehlgeschlagene Dienste, Absturzabbilder (Coredumps), Neustarts",
]

DEFAULTS: Dict[str, Any] = {
    "log_dirs": ["/dev/shm/minersteuerung/logs", "/home/piminer/minersteuerung/logs"],
    "log_glob": "*.log",
    "repo": "/home/piminer/minersteuerung",
    "buffer_dir": "/home/piminer/minersteuerung/logs/error_reports/buffer",
    "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
    "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
    "state_file": "/home/piminer/minersteuerung/logs/error_reports/state.json",
    "store_file": "/home/piminer/minersteuerung/logs/error_reports/findings.json",
    "window_days": 14,
    "journal": True,
    "journal_max_priority": 4,
    "min_send_severity": "warning",
    "ignore_patterns": [],
}


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    if path and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


# --- Text helpers -----------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|(?<![\w\]])\[(?:\d{1,3};?)+m")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM = re.compile(r"\d+")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def normalize(text: str) -> str:
    """Variable parts (numbers, ids) → placeholders; IP addresses stay so devices remain distinct."""
    kept: List[str] = []

    def stash(m: "re.Match[str]") -> str:
        kept.append(m.group(0))
        return "\ue000"

    t = _IP.sub(stash, text)
    t = _UUID.sub("<id>", t)
    t = _HEX.sub("0x#", t)
    t = _NUM.sub("#", t)
    return re.sub("\ue000", lambda _m: kept.pop(0), t)


def fingerprint(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:12]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _parse_iso(raw: str) -> Optional[float]:
    raw = raw.replace(",", ".")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt.timestamp()


_TS_BRACKET = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]\s*"
)
_TS_PLAIN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
)
_TS_WERKZEUG = re.compile(r"\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4} \d{2}:\d{2}:\d{2})\]")


def parse_leading_ts(line: str) -> Tuple[Optional[float], str]:
    first = line[:1]
    if not (first.isdigit() or (first == "[" and line[1:2].isdigit())):
        return None, line
    for rx in (_TS_BRACKET, _TS_PLAIN):
        m = rx.match(line)
        if m:
            ts = _parse_iso(m.group("ts"))
            if ts is not None:
                return ts, line[m.end():]
    return None, line


def parse_line_ts(line: str) -> Tuple[Optional[float], str]:
    ts, rest = parse_leading_ts(line)
    if ts is not None:
        return ts, rest
    m = _TS_WERKZEUG.search(line)
    if m:
        try:
            return datetime.strptime(m.group("ts"), "%d/%b/%Y %H:%M:%S").timestamp(), line
        except ValueError:
            pass
    return None, line


# --- Classification rules ---------------------------------------------------

# Bekannte harmlose Meldungen: werden gezählt (Abschnitt "Gefiltert"), aber nicht gemeldet.
BUILTIN_IGNORE: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Flask-Entwicklungsserver-Hinweis", re.compile(r"(?i)WARNING: This is a development server")),
    ("Boot-Reparatur OK", re.compile(r"(?i)\bOK critical \.py modules")),
    ("Audio/Desktop RealtimeKit", re.compile(r"RTKit|RealtimeKit|MaxRealtimePriority|MinNiceLevel")),
    ("udev alsa-restore Regeln", re.compile(r"alsa-restore\.rules")),
    ("Kernel-Option unprivileged_userns_clone", re.compile(r"unprivileged_userns_clone")),
    ("Bluetooth Dummy-Regulator", re.compile(r"supply \w+ not found, using dummy regulator")),
    ("USB-Laufwerk Cache-Hinweis", re.compile(r"No Caching mode page found|Assuming drive cache")),
    ("Bluetooth Verzeichnisrechte", re.compile(r"ConfigurationDirectory '\w+' already exists but the mode is different")),
    ("Netplan Dateirechte", re.compile(r"Netplan configuration should NOT be accessible|Permissions for /\S*netplan\S* are too open")),
    ("udisks GLib-Assertion", re.compile(r"g_variant_store: assertion")),
    ("NFS blocklayout", re.compile(r"rpc_pipefs/nfs/blocklayout")),
    ("Pi-Firmware Abfrage nicht unterstützt", re.compile(r"raspberrypi-firmware .*returned status 0x80000001")),
    ("Desktop-Portal Ersatzauswahl", re.compile(r"Choosing \S+ for \S+ as a last-resort fallback")),
    ("Desktop ohne Akku (UPower)", re.compile(r"Failed to get percentage from UPower")),
    ("Schlüsselbund-Dienst Start", re.compile(r"discover_other_daemon")),
]

# (Muster, Schwere, Kategorie) — höchste zutreffende Schwere gewinnt.
ESCALATE: List[Tuple["re.Pattern[str]", str, str]] = [
    (re.compile(r"(?i)under-?voltage|unterspannung"), "critical", "Stromversorgung"),
    (re.compile(r"(?i)corrupt|message authentication failed|EXT4-fs error|Buffer I/O error|"
                r"I/O error|read-only file system|remounting filesystem read-only|mmc\d+:.*(error|timeout)"),
     "critical", "Datenträger"),
    (re.compile(r"(?i)out of memory|oom-kill|invoked oom-killer"), "critical", "Arbeitsspeicher"),
    (re.compile(r"(?i)segfault|segmentation fault|core dumped|dumped core|general protection"), "error", "Absturz"),
    (re.compile(r"(?i)Failed to start|Failed with result|Failed at step|start request repeated too quickly|"
                r"Main process exited, code=(killed|dumped)"), "error", "Dienst"),
    (re.compile(r"(?i)hung task|blocked for more than \d+ seconds|watchdog: BUG"), "error", "Kernel"),
    (re.compile(r"(?i)thermal|throttl|over-?temperature"), "warning", "Temperatur"),
]

ADVICE: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"(?i)corrupt block|message authentication failed"),
     "Datei im verschlüsselten Speicher beschädigt. Betroffene Datei sichern/umbenennen, "
     "danach SD-Karte prüfen (häufige Ursache: Stromausfall beim Schreiben, alternde Karte)."),
    (re.compile(r"(?i)I/O error|EXT4-fs error|mmc\d+"),
     "Lese-/Schreibfehler am Datenträger. Backup prüfen und SD-Karte/SSD zeitnah tauschen."),
    (re.compile(r"(?i)under-?voltage|unterspannung"),
     "Netzteil oder Kabel liefert zu wenig Spannung (Pi 5: offizielles 27-W-Netzteil, 5 V/5 A)."),
    (re.compile(r"(?i)out of memory|oom-kill"),
     "Arbeitsspeicher voll, der Kernel hat Prozesse beendet. Speicherverbrauch der Dienste prüfen."),
    (re.compile(r"(?i)Failed at step (CHDIR|EXEC).*No such file|requested working directory failed"),
     "Dienst startete, bevor sein Verzeichnis verfügbar war (z. B. verschlüsselter Ordner noch nicht "
     "eingehängt). Startreihenfolge/Abhängigkeit des Dienstes prüfen."),
    (re.compile(r"(?i)address already in use|port \d+ is in use"),
     "Port bereits belegt: vermutlich läuft eine zweite Instanz desselben Programms."),
    (re.compile(r"(?i)timed ?out|zeitüberschreitung|name resolution|unreachable|nicht erreichbar"),
     "Netzwerk-/Internetverbindung kurzzeitig weg. Nur relevant, wenn es häufig auftritt."),
    (re.compile(r"(?i)Status:\s*(error|offline)"),
     "Gerät antwortet nicht (aus, vom Netz getrennt oder IP geändert)."),
    (re.compile(r"(?i)Unterspannung|throttl|Temperaturdrossel|Frequenz begrenzt"),
     "Kühlung und Stromversorgung prüfen."),
]

# Vorfilter auf der kleingeschriebenen Zeile (ohne IGNORECASE deutlich schneller).
_APP_CANDIDATE = re.compile(
    r"traceback|error|exception|fehl|fail|critical|fatal|panic|kritisch|warn|timed ?out|"
    r"zeitüberschreitung|refused|reset by peer|unreachable|erreichbar|no route|name resolution|"
    r"permission denied|no such file|segmentation|address already|in use|status:"
)
_ACCESS = re.compile(r'"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (?P<path>\S+) HTTP/[\d.]+" (?P<status>\d{3})\b')
_LEVEL_TAG = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*?\[?(?P<lvl>CRITICAL|FATAL|ERROR|FEHLER|WARNING|WARN|WARNUNG|INFO|DEBUG|OK)\]?[\s:|-]",
    re.I,
)
_NOT_ERROR = re.compile(r"(?i)\b(?:0|no|kein(?:e|en)?|ohne)\s+(?:errors?|fehler)\b|\berrors?\s*[=:]\s*0\b")
_DEVICE_STATUS = re.compile(r"^\[(?P<dev>[\w.:-]+)\]\s+Status:\s*(?P<st>error|offline|fehler)\b", re.I)
_CRIT_WORD = re.compile(r"(?i)\b(critical|fatal|panic|kritisch)\b")
_ERR_WORD = re.compile(
    r"(?i)\berror\b|\bexception\b|\bfehler\b|fehlgeschlagen|\bfailed\b|address already in use|"
    r"port \d+ is in use|permission denied|keine berechtigung|no such file or directory|segmentation fault"
)
_WARN_WORD = re.compile(
    r"(?i)\bwarn(?:ing)?\b|\bwarnung\b|timed ?out|zeitüberschreitung|connection (?:refused|reset|aborted)|"
    r"reset by peer|nicht erreichbar|unreachable|no route to host|name resolution"
)
_NET_WORD = re.compile(
    r"(?i)timed ?out|timeout|connect(?:ion)?(?:error| refused| reset| aborted)|unreachable|no route|"
    r"name resolution|nicht erreichbar|networkerror|netzwerk"
)
_TB_START = "Traceback (most recent call last)"
_TB_CHAIN = ("The above exception", "During handling of the above exception")
_EXC_LINE = re.compile(r"^[A-Za-z_][\w.]*(?::\s.*)?$")
_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')


def advice_for(text: str) -> str:
    for rx, hint in ADVICE:
        if rx.search(text):
            return hint
    return ""


def _user_ignore(cfg: Dict[str, Any]) -> List[Tuple[str, "re.Pattern[str]"]]:
    out = []
    for raw in cfg.get("ignore_patterns") or []:
        try:
            out.append((f"Eigene Regel: {raw}", re.compile(str(raw))))
        except re.error:
            continue
    return out


def ignored_by(text: str, rules: List[Tuple[str, "re.Pattern[str]"]]) -> Optional[str]:
    for label, rx in rules:
        if rx.search(text):
            return label
    return None


def escalate(text: str, severity: str, category: str) -> Tuple[str, str]:
    for rx, sev, cat in ESCALATE:
        if rx.search(text) and SEVERITY_RANK[sev] >= SEVERITY_RANK[severity]:
            return sev, cat
    return severity, category


# --- Store ------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _initial_period_start(cfg: Dict[str, Any], now: float) -> float:
    days = max(1, int(cfg.get("window_days") or 14))
    fallback = now - days * 86400
    state = _load_json(Path(str(cfg.get("state_file") or "")))
    if isinstance(state, dict):
        try:
            last = float(state.get("last_collect") or 0)
        except (TypeError, ValueError):
            last = 0
        if fallback - days * 86400 < last < now:
            return last
    return fallback


def load_store(cfg: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    data = _load_json(Path(str(cfg["store_file"])))
    if not isinstance(data, dict) or data.get("version") != REPORT_VERSION:
        data = {"version": REPORT_VERSION, "period_start": _initial_period_start(cfg, now)}
    data.setdefault("period_start", now)
    for key in ("files", "findings", "suppressed", "health"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def save_store(cfg: Dict[str, Any], store: Dict[str, Any]) -> None:
    _write_json(Path(str(cfg["store_file"])), store)


@contextmanager
def store_lock(cfg: Dict[str, Any]) -> Iterator[None]:
    lock_path = Path(str(cfg["store_file"])).with_name(".findings.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def add_finding(
    store: Dict[str, Any],
    *,
    fp: str,
    ts: float,
    severity: str,
    category: str,
    source: str,
    origin: str,
    title: str,
    message: str,
    unit: str = "",
    location: str = "",
    counted: bool = True,
    merge_message: bool = False,
) -> Optional[Dict[str, Any]]:
    """Returns the finding when it was newly created (caller may enrich it).

    ``counted=False`` attaches a line to a grouped finding without raising its count;
    ``merge_message`` appends lines that are new for that group to its message.
    """
    findings = store["findings"]
    row = findings.get(fp)
    stamp = iso(ts)
    if row:
        if counted:
            row["count"] = int(row.get("count") or 0) + 1
        if merge_message:
            current = str(row.get("message") or "")
            if normalize(message) not in normalize(current) and len(current) + len(message) < MAX_MESSAGE:
                row["message"] = f"{current}\n{message}"
        if stamp > str(row.get("last_seen") or ""):
            row["last_seen"] = stamp
        if stamp < str(row.get("first_seen") or stamp):
            row["first_seen"] = stamp
        if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(row.get("severity"), 0):
            row["severity"] = severity
        return None
    if len(findings) >= MAX_FINDINGS:
        sup = store["suppressed"]
        sup["Limit verschiedener Meldungen erreicht"] = int(sup.get("Limit verschiedener Meldungen erreicht") or 0) + 1
        return None
    row = {
        "fingerprint": fp,
        "severity": severity,
        "category": category,
        "source": source,
        "origin": origin,
        "title": title[:300],
        "message": message[:MAX_MESSAGE],
        "unit": unit,
        "location": location,
        "first_seen": stamp,
        "last_seen": stamp,
        "count": 1 if counted else 0,
    }
    findings[fp] = row
    return row


def _suppress(store: Dict[str, Any], label: str) -> None:
    sup = store["suppressed"]
    sup[label] = int(sup.get(label) or 0) + 1


# --- App logs ---------------------------------------------------------------

def iter_log_files(cfg: Dict[str, Any]) -> List[Path]:
    seen = set()
    files: List[Path] = []
    pattern = str(cfg.get("log_glob") or "*.log")
    for d in cfg.get("log_dirs") or []:
        root = Path(d)
        if not root.is_dir():
            continue
        for p in sorted(root.glob(pattern)):
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            files.append(p)
    return files


def _trim_traceback(block: List[str]) -> str:
    if len(block) > MAX_TRACEBACK_LINES:
        block = block[:5] + ["  …"] + block[-(MAX_TRACEBACK_LINES - 6):]
    return "\n".join(block)


def _read_traceback(lines: List[str], i: int) -> Tuple[List[str], int]:
    """Collect a (possibly chained) Python traceback starting at lines[i]."""
    block = [lines[i]]
    j = i + 1
    n = len(lines)
    while j < n:
        ln = strip_ansi(lines[j])
        if not ln.strip() or ln.startswith((" ", "\t")) or ln.startswith(_TB_START) or ln.startswith(_TB_CHAIN):
            block.append(ln)
            j += 1
            continue
        if _EXC_LINE.match(ln):
            block.append(ln)
            j += 1
            k = j
            while k < n and not lines[k].strip():
                k += 1
            if k < n and lines[k].startswith(_TB_CHAIN):
                j = k
                continue
        break
    while block and not block[-1].strip():
        block.pop()
    return block, j


def _traceback_finding(block: List[str], source: str) -> Tuple[str, str, str, str]:
    """→ (fingerprint, title, location, category)"""
    exc_line = ""
    for ln in reversed(block):
        if _EXC_LINE.match(ln) and not ln.startswith(_TB_START):
            exc_line = ln.strip()
            break
    frames = [m for m in (_FRAME.search(ln) for ln in block) if m]
    own = [f for f in frames if "/minersteuerung/" in f.group("file") and "/venv/" not in f.group("file")]
    frame = (own or frames or [None])[-1]
    location = ""
    loc_key = ""
    if frame is not None:
        location = f"{frame.group('file')}:{frame.group('line')} in {frame.group('func')}"
        loc_key = f"{Path(frame.group('file')).name}:{frame.group('func')}"
    exc_type = exc_line.split(":", 1)[0] if exc_line else "Traceback"
    title = exc_line or "Python-Traceback"
    category = "Netzwerk" if _NET_WORD.search(exc_type + " " + title) else "Software"
    return fingerprint("tb", source, exc_type, loc_key), title, location, category


def classify_app_line(rest: str) -> Optional[Tuple[str, str]]:
    """→ (severity, category) or None if the line is not a problem."""
    m = _LEVEL_TAG.match(rest)
    lvl = (m.group("lvl").upper() if m else "")
    if lvl in ("INFO", "DEBUG", "OK"):
        return None
    if _NOT_ERROR.search(rest):
        return None
    category = "Netzwerk" if _NET_WORD.search(rest) else "Software"
    if lvl in ("CRITICAL", "FATAL"):
        return "critical", category
    if lvl in ("ERROR", "FEHLER"):
        return "error", category
    if lvl in ("WARNING", "WARN", "WARNUNG"):
        return "warning", category
    if _CRIT_WORD.search(rest):
        return "critical", category
    if _ERR_WORD.search(rest):
        return "error", category
    if _WARN_WORD.search(rest):
        return "warning", category
    return None


def scan_text(
    store: Dict[str, Any],
    text: str,
    source: str,
    *,
    since: float,
    last_ts: Optional[float],
    fallback_ts: float,
    ignore: List[Tuple[str, "re.Pattern[str]"]],
) -> Optional[float]:
    """Process complete log lines; returns the last seen timestamp (for the next chunk)."""
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        ts, rest = parse_leading_ts(raw)
        if ts is not None:
            last_ts = ts
        i += 1
        access = "HTTP/" in raw and '" 5' in raw
        if not access and not _APP_CANDIDATE.search(raw.lower()):
            continue
        if ts is None:
            ts, rest = parse_line_ts(raw)
        use_ts = ts or last_ts or fallback_ts
        if use_ts < since:
            continue
        rest = strip_ansi(rest).strip()
        if not rest:
            continue
        if "HTTP/" in rest:
            m = _ACCESS.search(rest)
            if m:
                status = int(m.group("status"))
                if status >= 500:
                    path = m.group("path").split("?", 1)[0]
                    add_finding(
                        store, fp=fingerprint("http", source, str(status), normalize(path)), ts=use_ts,
                        severity="error", category="Dashboard", source=source, origin="app",
                        title=f"HTTP {status} bei {path}", message=rest,
                    )
                continue
        label = ignored_by(rest, ignore)
        if label:
            _suppress(store, label)
            continue
        if rest.startswith(_TB_START):
            block, nxt = _read_traceback(lines, i - 1)
            i = max(i, nxt)
            fp, title, location, category = _traceback_finding(block, source)
            add_finding(
                store, fp=fp, ts=use_ts, severity="error", category=category, source=source,
                origin="app", title=title, message=_trim_traceback(block), location=location,
            )
            continue
        if rest.startswith(_TB_CHAIN):
            continue
        dm = _DEVICE_STATUS.match(rest)
        if dm:
            add_finding(
                store, fp=fingerprint("dev", source, dm.group("dev"), dm.group("st").lower()), ts=use_ts,
                severity="warning", category="Gerät", source=source, origin="app",
                title=f"Gerät {dm.group('dev')} meldet Status {dm.group('st')}", message=rest,
            )
            continue
        cls = classify_app_line(rest)
        if not cls:
            continue
        severity, category = escalate(rest, *cls)
        add_finding(
            store, fp=fingerprint("app", source, normalize(rest)[:300]), ts=use_ts, severity=severity,
            category=category, source=source, origin="app", title=rest[:300], message=rest,
        )
    return last_ts


def scan_app_logs(store: Dict[str, Any], cfg: Dict[str, Any], now: float) -> int:
    ignore = BUILTIN_IGNORE + _user_ignore(cfg)
    since = float(store["period_start"])
    files_state = store["files"]
    alive = set()
    scanned = 0
    for path in iter_log_files(cfg):
        key = str(path)
        alive.add(key)
        try:
            st = path.stat()
        except OSError:
            continue
        prev = files_state.get(key) or {}
        offset = int(prev.get("offset") or 0)
        if prev.get("ino") != st.st_ino or prev.get("dev") != st.st_dev or st.st_size < offset:
            offset = 0
            prev = {}
        entry = {"ino": st.st_ino, "dev": st.st_dev, "offset": offset, "last_ts": prev.get("last_ts")}
        if st.st_size > offset and st.st_mtime >= since:
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(MAX_READ_BYTES)
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                text = chunk[: cut + 1].decode("utf-8", errors="replace")
                entry["last_ts"] = scan_text(
                    store, text, path.name, since=since, last_ts=prev.get("last_ts"),
                    fallback_ts=st.st_mtime, ignore=ignore,
                )
                entry["offset"] = offset + cut + 1
                scanned += 1
        elif st.st_size > offset:
            entry["offset"] = st.st_size
        files_state[key] = entry
    for key in list(files_state):
        if key not in alive:
            del files_state[key]
    return scanned


# --- Journal ----------------------------------------------------------------

def _journal_message(entry: Dict[str, Any]) -> str:
    msg = entry.get("MESSAGE")
    if isinstance(msg, list):
        try:
            return bytes(msg).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            return ""
    return str(msg or "")


_UNIT_FAIL = re.compile(
    r"Failed to start|Failed with result|Failed at step|requested working directory failed|"
    r"start request repeated too quickly|Main process exited, code=(?:killed|dumped)"
)
_GOCRYPT_INO = re.compile(r"(?:doRead |ino)(\d+)")


def _gocryptfs_mounts() -> List[str]:
    out = []
    try:
        for ln in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
            parts = ln.split()
            if len(parts) >= 3 and parts[2] == "fuse.gocryptfs":
                out.append(parts[1].replace("\\040", " "))
    except OSError:
        pass
    return out


def _mount_of_pid(pid: str, mounts: List[str]) -> List[str]:
    try:
        args = [a for a in Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0") if a]
    except (OSError, ValueError):
        return mounts
    for a in reversed(args):
        p = a.decode("utf-8", errors="replace")
        if p in mounts:
            return [p]
    return mounts


def resolve_inode(ino: int, roots: List[str], max_entries: int = 300_000, max_sec: float = 15.0) -> str:
    deadline = time.monotonic() + max_sec
    seen = 0
    stack = list(roots)
    while stack:
        top = stack.pop()
        try:
            it = os.scandir(top)
        except OSError:
            continue
        with it:
            for de in it:
                seen += 1
                if seen > max_entries or time.monotonic() > deadline:
                    return ""
                try:
                    if de.inode() == ino:
                        return de.path
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                except OSError:
                    continue
    return ""


def _enrich_gocryptfs(row: Dict[str, Any], entry: Dict[str, Any]) -> None:
    m = re.search(r"(?:doRead |ino)(\d+)", row.get("message") or "")
    if not m:
        return
    mounts = _gocryptfs_mounts()
    if not mounts:
        return
    path = resolve_inode(int(m.group(1)), _mount_of_pid(str(entry.get("_PID") or ""), mounts))
    if path:
        row["hint"] = f"Betroffene Datei: {path}"


def _run_journal(args: List[str], status: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    try:
        proc = subprocess.Popen(
            ["journalctl", "--no-pager", "-o", "json", *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
        )
    except OSError:
        status["rc"] = -1
        return
    assert proc.stdout is not None
    count = 0
    try:
        for line in proc.stdout:
            count += 1
            if count > MAX_JOURNAL_ENTRIES:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
    finally:
        if proc.poll() is None:
            proc.kill()
        status["rc"] = proc.wait()


def scan_journal(store: Dict[str, Any], cfg: Dict[str, Any], now: float) -> int:
    if not cfg.get("journal", True):
        return 0
    prio = max(0, min(7, int(cfg.get("journal_max_priority") or 4)))
    ignore = BUILTIN_IGNORE + _user_ignore(cfg)
    cursor = str(store.get("journal_cursor") or "")
    since = float(store.get("journal_since") or store["period_start"])
    base = ["-p", f"0..{prio}"]
    args = base + (["--after-cursor", cursor] if cursor else ["--since", f"@{int(since)}"])
    processed = 0
    last_cursor = cursor
    status: Dict[str, Any] = {}
    for entry in _run_journal(args, status):
        processed += 1
        last_cursor = str(entry.get("__CURSOR") or last_cursor)
        try:
            ts = int(entry.get("__REALTIME_TIMESTAMP") or 0) / 1_000_000
        except (TypeError, ValueError):
            ts = now
        if ts < float(store["period_start"]):
            continue
        msg = strip_ansi(_journal_message(entry)).strip()
        if not msg:
            continue
        label = ignored_by(msg, ignore)
        if label:
            _suppress(store, label)
            continue
        try:
            p = int(entry.get("PRIORITY") or 6)
        except (TypeError, ValueError):
            p = 6
        severity = "critical" if p <= 2 else "error" if p == 3 else "warning"
        kernel = entry.get("_TRANSPORT") == "kernel"
        ident = str(entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or ("kernel" if kernel else "system"))
        unit = str(
            entry.get("UNIT") or entry.get("USER_UNIT") or entry.get("_SYSTEMD_USER_UNIT")
            or entry.get("_SYSTEMD_UNIT") or ""
        )
        source = f"Journal/{ident}"
        if unit and _UNIT_FAIL.search(msg):
            add_finding(
                store, fp=fingerprint("unitfail", unit), ts=ts, severity="error", category="Dienst",
                source="Journal/systemd", origin="journal", title=f"Dienst {unit} ist fehlgeschlagen",
                message=msg, unit=unit, counted="Failed with result" in msg, merge_message=True,
            )
            continue
        ino = _GOCRYPT_INO.search(msg) if ident == "gocryptfs" else None
        if ino:
            row = add_finding(
                store, fp=fingerprint("gocryptfs", ino.group(1)), ts=ts, severity="critical",
                category="Datenträger", source=source, origin="journal",
                title=f"Beschädigte Datei im verschlüsselten Speicher (Inode {ino.group(1)})",
                message=msg, unit=unit, counted="corrupt block" in msg, merge_message=True,
            )
            if row is not None:
                _enrich_gocryptfs(row, entry)
            continue
        category = "Kernel" if kernel else ("Netzwerk" if _NET_WORD.search(msg) else "System")
        severity, category = escalate(msg, severity, category)
        add_finding(
            store, fp=fingerprint("journal", ident, normalize(msg)[:300]), ts=ts, severity=severity,
            category=category, source=source, origin="journal", title=f"{ident}: {msg}"[:300],
            message=msg, unit=unit if unit and unit not in ("init.scope",) else "",
        )
    if cursor and processed == 0 and status.get("rc"):
        # Cursor nach Journal-Rotation ungültig → nächstes Mal ab letztem Scan.
        store.pop("journal_cursor", None)
        store["journal_since"] = float(store.get("last_scan") or store["period_start"])
    elif last_cursor:
        store["journal_cursor"] = last_cursor
        store.pop("journal_since", None)
    return processed


# --- System health ----------------------------------------------------------

THROTTLE_BITS = {
    0: ("critical", "Stromversorgung", "Unterspannung bei einer Prüfung aktiv"),
    1: ("warning", "Temperatur", "CPU-Frequenz bei einer Prüfung begrenzt"),
    2: ("warning", "Temperatur", "CPU bei einer Prüfung gedrosselt"),
    3: ("warning", "Temperatur", "Temperaturgrenze bei einer Prüfung aktiv"),
    16: ("critical", "Stromversorgung", "Unterspannung seit dem letzten Start aufgetreten"),
    17: ("warning", "Temperatur", "CPU-Frequenz seit dem letzten Start begrenzt worden"),
    18: ("warning", "Temperatur", "CPU seit dem letzten Start gedrosselt worden"),
    19: ("warning", "Temperatur", "Temperaturgrenze seit dem letzten Start erreicht"),
}


def _cmd(args: List[str], timeout: float = 10) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout or ""


def _throttled() -> Optional[int]:
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", _cmd(["vcgencmd", "get_throttled"]))
    return int(m.group(1), 16) if m else None


def _cpu_temp() -> Optional[float]:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = ln.partition(":")
            out[k] = int(v.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def _failed_units() -> List[str]:
    units = []
    for scope in ([], ["--user"]):
        for ln in _cmd(["systemctl", *scope, "--failed", "--plain", "--no-legend", "--no-pager"]).splitlines():
            parts = ln.split()
            if parts:
                units.append(("Benutzer: " if scope else "") + parts[0])
    return units


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    except OSError:
        return ""


def _boots_since(since: float) -> List[str]:
    raw = _cmd(["journalctl", "--list-boots", "-o", "json", "--no-pager"])
    out = []
    try:
        for b in json.loads(raw or "[]"):
            first = float(b.get("first_entry") or 0) / 1_000_000
            if first >= since:
                out.append(iso(first))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        pass
    return out


def _root_readonly() -> bool:
    try:
        for ln in Path("/proc/mounts").read_text().splitlines():
            parts = ln.split()
            if len(parts) >= 4 and parts[1] == "/":
                return "ro" in parts[3].split(",")
    except OSError:
        pass
    return False


def snapshot_health(store: Dict[str, Any], cfg: Dict[str, Any], now: float) -> None:
    h = store["health"]
    bits = _throttled()
    if bits is not None:
        h["throttled_bits"] = int(h.get("throttled_bits") or 0) | bits
        h["throttled_now"] = bits
        h["boot_id"] = _boot_id()
    temp = _cpu_temp()
    if temp is not None:
        h["temp_now"] = temp
        h["temp_max"] = max(float(h.get("temp_max") or 0), temp)
    disks = h.setdefault("disk_max_pct", {})
    for mount in ("/", "/dev/shm"):
        try:
            sv = os.statvfs(mount)
        except OSError:
            continue
        if sv.f_blocks:
            pct = round(100.0 * (sv.f_blocks - sv.f_bavail) / sv.f_blocks, 1)
            disks[mount] = max(float(disks.get(mount) or 0), pct)
    mem = _meminfo()
    if mem.get("MemAvailable") is not None:
        prev = h.get("mem_avail_min_mb")
        h["mem_avail_min_mb"] = mem["MemAvailable"] if prev is None else min(int(prev), mem["MemAvailable"])
        h["mem_total_mb"] = mem.get("MemTotal")
    if mem.get("SwapTotal"):
        used = mem["SwapTotal"] - mem.get("SwapFree", 0)
        h["swap_used_max_mb"] = max(int(h.get("swap_used_max_mb") or 0), used)
    failed = set(h.get("failed_units") or []) | set(_failed_units())
    h["failed_units"] = sorted(failed)
    h["root_readonly"] = bool(h.get("root_readonly")) or _root_readonly()
    try:
        h["uptime_h"] = round(float(Path("/proc/uptime").read_text().split()[0]) / 3600, 1)
    except (OSError, ValueError, IndexError):
        pass
    h["boots"] = _boots_since(float(store["period_start"]))
    cores = []
    core_dir = Path("/var/lib/systemd/coredump")
    if core_dir.is_dir():
        for p in core_dir.iterdir():
            try:
                if p.stat().st_mtime >= float(store["period_start"]):
                    cores.append(p.name)
            except OSError:
                continue
    h["coredumps"] = sorted(set(h.get("coredumps") or []) | set(cores))
    h["checked_at"] = iso(now)


def health_findings(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    h = store.get("health") or {}
    stamp = h.get("checked_at") or iso(time.time())
    out: List[Dict[str, Any]] = []

    def add(key: str, severity: str, category: str, title: str, message: str = "") -> None:
        out.append({
            "fingerprint": fingerprint("health", key), "severity": severity, "category": category,
            "source": "Systemzustand", "origin": "health", "title": title, "message": message or title,
            "unit": "", "location": "", "first_seen": stamp, "last_seen": stamp, "count": 1,
        })

    bits = int(h.get("throttled_bits") or 0)
    already = int((store.get("throttle_reported") or {}).get(h.get("boot_id") or "", 0))
    for bit, (sev, cat, text) in THROTTLE_BITS.items():
        if bits & (1 << bit) and not (bit >= 16 and already & (1 << bit)):
            add(f"throttle{bit}", sev, cat, text, f"vcgencmd get_throttled = {hex(bits)}")
    temp = h.get("temp_max")
    if isinstance(temp, (int, float)) and temp >= 80:
        add("temp", "critical" if temp >= 85 else "warning", "Temperatur", f"CPU-Temperatur bis {temp:.1f} °C")
    for mount, pct in (h.get("disk_max_pct") or {}).items():
        if pct >= 90:
            add(f"disk{mount}", "critical" if pct >= 97 else "warning", "Speicherplatz",
                f"Speicher {mount} zu {pct:.0f} % voll")
    mem_min = h.get("mem_avail_min_mb")
    if isinstance(mem_min, int) and mem_min < 200:
        add("mem", "warning", "Arbeitsspeicher", f"Freier Arbeitsspeicher fiel auf {mem_min} MB")
    for unit in h.get("failed_units") or []:
        add(f"unit{unit}", "error", "Dienst", f"Dienst im Zustand 'failed': {unit}")
    for core in h.get("coredumps") or []:
        add(f"core{core}", "error", "Absturz", f"Absturzabbild (Coredump): {core}")
    if h.get("root_readonly"):
        add("rootro", "critical", "Datenträger", "Systempartition ist schreibgeschützt eingehängt (read-only)")
    return out


# --- Scan / report ----------------------------------------------------------

def scan(cfg: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    with store_lock(cfg):
        store = load_store(cfg, now)
        scan_app_logs(store, cfg, now)
        scan_journal(store, cfg, now)
        snapshot_health(store, cfg, now)
        store["last_scan"] = now
        save_store(cfg, store)
    return store


def software_version(repo: Path) -> Dict[str, str]:
    out: Dict[str, str] = {"hostname": socket.gethostname()}
    sha = _cmd(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], 5).strip()
    if sha:
        out["git_sha"] = sha
    date = _cmd(["git", "-C", str(repo), "log", "-1", "--format=%ci"], 5).strip()
    if date:
        out["git_date"] = date
    return out


def _count(f: Dict[str, Any]) -> int:
    return max(1, int(f.get("count") or 0))


def _sort_key(f: Dict[str, Any]) -> Tuple[int, int, str]:
    return (-SEVERITY_RANK.get(f.get("severity"), 0), -_count(f), str(f.get("first_seen") or ""))


def report_findings(store: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    min_rank = SEVERITY_RANK.get(str(cfg.get("min_send_severity") or "warning").lower(), 1)
    rows = list(store["findings"].values()) + health_findings(store)
    rows = [r for r in rows if SEVERITY_RANK.get(r.get("severity"), 0) >= min_rank]
    for r in rows:
        if not r.get("advice"):
            r["advice"] = advice_for(f"{r.get('title')}\n{r.get('message')}")
    return sorted(rows, key=_sort_key)


def build_meta(store: Dict[str, Any], cfg: Dict[str, Any], findings: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    start = float(store["period_start"])
    version = software_version(Path(str(cfg.get("repo") or ".")))
    sev_counts = {k: 0 for k in ("critical", "error", "warning")}
    for f in findings:
        if f.get("severity") in sev_counts:
            sev_counts[f["severity"]] += 1
    return {
        "report_version": REPORT_VERSION,
        "host": version.get("hostname") or socket.gethostname(),
        "period_label": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        "period_days": max(1, round((now - start) / 86400)),
        "window_start": iso(start),
        "window_end": iso(now),
        "generated_at": iso(now),
        "event_count": sum(_count(f) for f in findings),
        "finding_count": len(findings),
        "severity_counts": sev_counts,
        "software": version,
        "health": store.get("health") or {},
        "suppressed": store.get("suppressed") or {},
        "sources": SOURCES_DESCRIPTION,
        "top_errors": [
            {"fingerprint": f["fingerprint"], "count": f.get("count"), "severity": f.get("severity"),
             "sample": str(f.get("title") or "")[:240], "sources": [f.get("source") or ""]}
            for f in findings[:20]
        ],
    }


def _fmt_ts(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value or "—")


def summary_line(sev_counts: Dict[str, Any]) -> str:
    parts = [
        f"{int(sev_counts.get('critical') or 0)} kritisch",
        f"{int(sev_counts.get('error') or 0)} Fehler",
        f"{int(sev_counts.get('warning') or 0)} Warnungen",
    ]
    return " · ".join(parts)


def health_rows(h: Dict[str, Any]) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    bits = h.get("throttled_bits")
    if bits is not None:
        issues = [THROTTLE_BITS[b][2] for b in THROTTLE_BITS if int(bits) & (1 << b)]
        rows.append(("Strom/Drosselung", "in Ordnung" if not issues else "; ".join(issues)))
    if h.get("temp_max") is not None:
        rows.append(("CPU-Temperatur", f"max. {float(h['temp_max']):.1f} °C (zuletzt {float(h.get('temp_now') or 0):.1f} °C)"))
    disks = h.get("disk_max_pct") or {}
    if disks:
        rows.append(("Speicher belegt (max.)", " · ".join(f"{m} {p:.0f} %" for m, p in sorted(disks.items()))))
    if h.get("mem_avail_min_mb") is not None:
        swap = f", Swap max. {h['swap_used_max_mb']} MB" if h.get("swap_used_max_mb") else ""
        rows.append(("RAM frei (min.)", f"{h['mem_avail_min_mb']} MB von {h.get('mem_total_mb') or '?'} MB{swap}"))
    boots = h.get("boots") or []
    rows.append(("Neustarts im Zeitraum", ", ".join(_fmt_ts(b) for b in boots) if boots else "keine"))
    if h.get("uptime_h") is not None:
        rows.append(("Laufzeit seit Start", f"{float(h['uptime_h']):.1f} h"))
    rows.append(("Fehlgeschlagene Dienste", ", ".join(h.get("failed_units") or []) or "keine"))
    rows.append(("Absturzabbilder", ", ".join(h.get("coredumps") or []) or "keine"))
    return rows


def render_text(meta: Dict[str, Any], findings: List[Dict[str, Any]]) -> str:
    soft = meta.get("software") or {}
    rule = "-" * 72
    git = soft.get("git_sha") or "—"
    if soft.get("git_date"):
        git += f" ({soft['git_date']})"
    lines: List[str] = [
        "piminer Fehlerbericht",
        "=" * 72,
        f"Gerät:      {meta.get('host') or soft.get('hostname') or '—'}",
        f"Software:   Git {git}",
        f"Zeitraum:   {_fmt_ts(meta.get('window_start'))} bis {_fmt_ts(meta.get('window_end'))} "
        f"({meta.get('period_days', '?')} Tage)",
        f"Erstellt:   {_fmt_ts(meta.get('generated_at'))}",
        f"Ergebnis:   {summary_line(meta.get('severity_counts') or {})} "
        f"({meta.get('event_count', 0)} Meldungen insgesamt)",
        "",
        "SYSTEMZUSTAND",
        rule,
    ]
    for label, value in health_rows(meta.get("health") or {}):
        lines.append(f"  {label:<26}{value}")
    lines += ["", "ÜBERSICHT (nach Schwere, dann Häufigkeit)", rule]
    if not findings:
        lines.append("  Keine Auffälligkeiten.")
    for i, f in enumerate(findings, 1):
        title = str(f.get("title") or "").replace("\n", " ")
        lines.append(
            f"  [{i:>2}] {SEVERITY_LABEL.get(f.get('severity'), '?'):<8} {_count(f):>6}×  "
            f"{str(f.get('category') or ''):<15} {title[:110]}"
        )
    if findings:
        lines += ["", "DETAILS", rule]
    for i, f in enumerate(findings[:MAX_DETAILS], 1):
        lines.append(f"[{i}] {SEVERITY_LABEL.get(f.get('severity'), '?')} · {f.get('category')} · {f.get('source')}")
        lines.append(f"    {str(f.get('title') or '').splitlines()[0] if f.get('title') else ''}")
        lines.append(
            f"    Anzahl {_count(f)} · erstmals {_fmt_ts(f.get('first_seen'))} · zuletzt {_fmt_ts(f.get('last_seen'))}"
        )
        if f.get("unit"):
            lines.append(f"    Dienst:     {f['unit']}")
        if f.get("location"):
            lines.append(f"    Code-Stelle: {f['location']}")
        if f.get("hint"):
            lines.append(f"    {f['hint']}")
        if f.get("advice"):
            lines.append(f"    Empfehlung: {f['advice']}")
        lines.append(f"    Kennung:    {f.get('fingerprint')}")
        message = str(f.get("message") or "")
        if message and message.strip() != str(f.get("title") or "").strip():
            lines.append("    Meldung:")
            lines.extend("      " + ml for ml in message.splitlines()[:MAX_TRACEBACK_LINES])
        lines.append("")
    if len(findings) > MAX_DETAILS:
        lines.append(f"… {len(findings) - MAX_DETAILS} weitere Einträge nur in der Übersicht.")
        lines.append("")
    sup = meta.get("suppressed") or {}
    if sup:
        lines += ["GEFILTERT (bekannte harmlose Meldungen, oben nicht aufgeführt)", rule]
        for label, n in sorted(sup.items(), key=lambda kv: -int(kv[1])):
            lines.append(f"  {int(n):>6}×  {label}")
        lines.append("")
    lines += ["ERFASSTE QUELLEN", rule]
    lines.extend(f"  - {s}" for s in meta.get("sources") or SOURCES_DESCRIPTION)
    return "\n".join(lines).rstrip() + "\n"


def _reset_period(store: Dict[str, Any], now: float) -> None:
    h = store.get("health") or {}
    if h.get("boot_id"):
        prev = store.get("throttle_reported") or {}
        bits = int(prev.get(h["boot_id"], 0)) | int(h.get("throttled_bits") or 0)
        store["throttle_reported"] = {h["boot_id"]: bits}
    store["period_start"] = now
    store["findings"] = {}
    store["suppressed"] = {}
    store["health"] = {}


def write_package(cfg: Dict[str, Any], meta: Dict[str, Any], findings: List[Dict[str, Any]]) -> Tuple[Path, Path]:
    outbox = Path(str(cfg["outbox_dir"]))
    buffer = Path(str(cfg["buffer_dir"]))
    outbox.mkdir(parents=True, exist_ok=True)
    buffer.mkdir(parents=True, exist_ok=True)
    label = str(meta["period_label"])
    if any((d / f"piminer-errors-{label}.meta.json").exists() for d in (outbox, buffer, Path(str(cfg["sent_dir"])))):
        label = f"{label}-{datetime.now().strftime('%H%M')}"
        meta["period_label"] = label
    base = f"piminer-errors-{label}"
    payload = "".join(
        json.dumps({**f, "ts": f.get("last_seen")}, ensure_ascii=False) + "\n" for f in findings
    ).encode("utf-8")
    (buffer / f"{base}.jsonl").write_bytes(payload)
    meta_text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    (buffer / f"{base}.meta.json").write_text(meta_text, encoding="utf-8")
    gz_path = outbox / f"{base}.jsonl.gz"
    gz_path.write_bytes(gzip.compress(payload))
    meta_path = outbox / f"{base}.meta.json"
    meta_path.write_text(meta_text, encoding="utf-8")
    return gz_path, meta_path


def collect(cfg: Dict[str, Any], now: Optional[float] = None) -> Optional[Tuple[Path, Path]]:
    """Scan, then package the period if anything is reportable. The period restarts either way."""
    now = now or time.time()
    scan(cfg, now)
    with store_lock(cfg):
        store = load_store(cfg, now)
        findings = report_findings(store, cfg)
        result = None
        if findings:
            meta = build_meta(store, cfg, findings, now)
            result = write_package(cfg, meta, findings)
        _reset_period(store, now)
        save_store(cfg, store)
    return result


def preview(cfg: Dict[str, Any], now: Optional[float] = None) -> str:
    now = now or time.time()
    store = scan(cfg, now)
    findings = report_findings(store, cfg)
    meta = build_meta(store, cfg, findings, now)
    text = render_text(meta, findings)
    if not findings:
        text = "Derzeit nichts zu melden – es würde kein Bericht gesendet.\n\n" + text
    return text


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get("ERROR_LOG_PIPELINE_CONFIG", str(Path(__file__).resolve().parent / "config.json")),
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--preview", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(Path(args.config))
    if args.preview:
        sys.stdout.write(preview(cfg))
        return 0
    if args.collect:
        result = collect(cfg)
        if result is None:
            print("nothing to report — no package queued")
        else:
            print(f"wrote {result[0]}")
            print(f"meta {result[1]}")
        return 0
    store = scan(cfg)
    print(f"scan ok: {len(store['findings'])} distinct findings since {iso(float(store['period_start']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
