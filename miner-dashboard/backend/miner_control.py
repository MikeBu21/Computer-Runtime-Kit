"""Miner pause/resume über Braiins API (mit Fallback auf CGMiner-Port 4028 und SSH)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import paramiko
import requests

logger = logging.getLogger(__name__)

GRPC_PATHS = {
    "pause": "/brains.boser.web.ActionsService/PauseMining",
    "resume": "/brains.boser.web.ActionsService/ResumeMining",
}

# Circuit-Breaker: unerreichbare Miner nicht jede Sekunde per SSH hämmern (CPU/Logs).
# Persistiert – sonst löscht jeder Daemon-Hard-Restart den Schutz und Ticks hängen erneut.
_SSH_FAIL_UNTIL: Dict[str, float] = {}
_SSH_FAIL_STREAK: Dict[str, int] = {}
_SSH_COOLDOWN_SEC = 300.0
_SSH_FAIL_THRESHOLD = 1
_CIRCUIT_FILE = Path(
    os.environ.get(
        "MINER_SSH_CIRCUIT_FILE",
        "/dev/shm/minersteuerung/backend/ssh_circuit.json",
    )
)
_circuit_loaded = False


def _circuit_path() -> Path:
    try:
        _CIRCUIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        return _CIRCUIT_FILE
    except Exception:
        return Path("/tmp/miner_ssh_circuit.json")


def _load_circuit() -> None:
    global _circuit_loaded
    if _circuit_loaded:
        return
    _circuit_loaded = True
    path = _circuit_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        until = data.get("fail_until") or {}
        streak = data.get("fail_streak") or {}
        now = time.time()
        for ip, ts in until.items():
            try:
                t = float(ts)
            except (TypeError, ValueError):
                continue
            if t > now:
                _SSH_FAIL_UNTIL[str(ip)] = t
        for ip, n in streak.items():
            try:
                _SSH_FAIL_STREAK[str(ip)] = int(n)
            except (TypeError, ValueError):
                pass
    except Exception as exc:
        logger.debug("SSH-Circuit laden fehlgeschlagen: %s", exc)


def _save_circuit() -> None:
    path = _circuit_path()
    try:
        now = time.time()
        payload = {
            "fail_until": {ip: ts for ip, ts in _SSH_FAIL_UNTIL.items() if ts > now},
            "fail_streak": dict(_SSH_FAIL_STREAK),
            "saved_at": now,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        logger.debug("SSH-Circuit speichern fehlgeschlagen: %s", exc)


def _host_reachable(ip: str, port: int = 22, timeout: float = 1.2) -> bool:
    """Schneller TCP-Check – verhindert lange SSH/gRPC-Warteschlangen bei toten Minern."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _miner_marked_missing(ip: str) -> bool:
    """miner.json recovery_state=missing → ohne User-Entscheidung nicht blockierend scannen."""
    try:
        path = Path(__file__).resolve().parent / "miners.json"
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return False
        for m in data:
            if str((m or {}).get("ip") or "") != str(ip):
                continue
            state = str((m or {}).get("recovery_state") or "").lower()
            streak = int((m or {}).get("recovery_miss_streak") or 0)
            return state == "missing" or streak >= 5
    except Exception:
        return False
    return False


def _should_skip_unreachable(ip: str) -> bool:
    """Einheitlicher Guard: Circuit, Missing-Flag oder kein TCP → Tick läuft weiter."""
    if _ssh_circuit_open(ip):
        return True
    if _miner_marked_missing(ip):
        if not _ssh_circuit_open(ip):
            _ssh_circuit_trip(ip)
        return True
    if not _host_reachable(ip, port=22, timeout=1.2):
        _ssh_circuit_trip(ip)
        return True
    return False


def _ssh_circuit_open(ip: str) -> bool:
    _load_circuit()
    until = _SSH_FAIL_UNTIL.get(ip) or 0.0
    return time.time() < until


def _ssh_circuit_trip(ip: str) -> None:
    _load_circuit()
    streak = int(_SSH_FAIL_STREAK.get(ip) or 0) + 1
    _SSH_FAIL_STREAK[ip] = streak
    if streak >= _SSH_FAIL_THRESHOLD:
        _SSH_FAIL_UNTIL[ip] = time.time() + _SSH_COOLDOWN_SEC
        logger.warning(
            "SSH-Circuit offen für %s (%ds) nach %d Fehlern",
            ip,
            int(_SSH_COOLDOWN_SEC),
            streak,
        )
        _save_circuit()


def _ssh_circuit_clear(ip: str) -> None:
    _load_circuit()
    _SSH_FAIL_STREAK.pop(ip, None)
    _SSH_FAIL_UNTIL.pop(ip, None)
    _save_circuit()


def _nc_output(ip: str, action: str) -> str:
    if action not in GRPC_PATHS or not shutil.which("nc"):
        return ""
    cmd = json.dumps({"command": action})
    try:
        result = subprocess.run(
            ["nc", "-w", "3", ip, "4028"],
            input=cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (result.stdout or "") + (result.stderr or "")
    except Exception as exc:
        logger.error("nc %s für %s fehlgeschlagen: %s", action, ip, exc)
        return ""


def _nc_succeeded(output: str) -> bool:
    if not output:
        return False
    if "Access denied" in output:
        return False
    if '"STATUS":"E"' in output or '"STATUS": [{"STATUS": "E"' in output:
        return False
    return True


def _grpc_action(ip: str, username: str, password: str, action: str) -> bool:
    if action not in GRPC_PATHS:
        return False
    url = f"http://{ip}{GRPC_PATHS[action]}"
    try:
        # Kein HTTP_PROXY aus der Umgebung (Cursor-Sandbox sonst → 403/ProxyError).
        session = requests.Session()
        session.trust_env = False
        response = session.post(
            url,
            auth=requests.auth.HTTPDigestAuth(username, password),
            headers={"Content-Type": "application/grpc-web+proto"},
            timeout=10,
            proxies={"http": None, "https": None},
        )
        if response.status_code == 200:
            return True
        logger.warning(
            "gRPC %s für %s: HTTP %s", action, ip, response.status_code
        )
        return False
    except Exception as exc:
        logger.warning("gRPC %s für %s fehlgeschlagen: %s", action, ip, exc)
        return False


def _expected_status(action: str) -> str:
    return "paused" if action == "pause" else "running"


def _ssh_connect(ip: str, username: str, password: str):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # Kurzer Timeout: bei nicht erreichbaren Minern soll der Tick nicht hängen
    ssh.connect(
        ip,
        username=username,
        password=password,
        timeout=4,
        banner_timeout=4,
        auth_timeout=4,
    )
    return ssh


def _parse_pause_status(out: str) -> Optional[str]:
    """Datei-Marker und letzte true/false-Zeile aus bosminer-Ausgabe."""
    # ANSI weg
    cleaned = ""
    i = 0
    while i < len(out):
        if out[i] == "\x1b":
            while i < len(out) and out[i] != "m":
                i += 1
            i += 1
            continue
        cleaned += out[i]
        i += 1
    upper = cleaned.upper()
    if "FILE_PAUSED" in upper:
        return "paused"
    if "FILE_RUNNING" in upper:
        return "running"
    for line in reversed(cleaned.splitlines()):
        token = line.strip().lower()
        if token == "true":
            return "paused"
        if token == "false":
            return "running"
    return None


def _ssh_miner_status(ip: str, username: str, password: str) -> Optional[str]:
    if _ssh_circuit_open(ip):
        return None
    if not _host_reachable(ip):
        _ssh_circuit_trip(ip)
        return None
    last_err: Optional[BaseException] = None
    for attempt in range(2):
        try:
            ssh = _ssh_connect(ip, username, password)
            try:
                # RUST_LOG=off: sonst INFO-Spam; Datei-Marker als harte Wahrheit.
                stdin, stdout, stderr = ssh.exec_command(
                    "RUST_LOG=off bosminer api is-paused 2>/dev/null; "
                    "test -f /etc/bosminer_paused && echo FILE_PAUSED || echo FILE_RUNNING"
                )
                out = stdout.read().decode()
            finally:
                ssh.close()
            parsed = _parse_pause_status(out)
            if parsed:
                _ssh_circuit_clear(ip)
                return parsed
        except Exception as exc:
            last_err = exc
            if attempt + 1 < 2:
                time.sleep(0.4 * (attempt + 1))
    if last_err is not None:
        _ssh_circuit_trip(ip)
        # Nur einmal pro Trip loggen (nicht jeden Retry-Sturm)
        if int(_SSH_FAIL_STREAK.get(ip) or 0) <= _SSH_FAIL_THRESHOLD:
            logger.warning(
                "SSH-Status %s fehlgeschlagen nach Retries: %s", ip, last_err
            )
    return None


def _ssh_bosminer_api(ip: str, username: str, password: str, action: str) -> bool:
    if action not in GRPC_PATHS:
        return False
    if _ssh_circuit_open(ip):
        return False
    if not _host_reachable(ip):
        _ssh_circuit_trip(ip)
        logger.warning("SSH bosminer api %s @ %s: Host nicht erreichbar – übersprungen", action, ip)
        return False
    last_err: Optional[BaseException] = None
    for attempt in range(2):
        try:
            ssh = _ssh_connect(ip, username, password)
            try:
                stdin, stdout, stderr = ssh.exec_command(
                    f"bosminer api {action} 2>&1",
                    timeout=15,
                )
                output = (stdout.read() + stderr.read()).decode()
            finally:
                ssh.close()
            lowered = output.lower()
            if "unrecognized subcommand" in lowered:
                logger.warning("SSH bosminer api %s @ %s: unrecognized", action, ip)
                return False
            if "error:" in lowered and action not in lowered:
                logger.warning(
                    "SSH bosminer api %s @ %s Fehler: %s",
                    action,
                    ip,
                    output.strip()[:200],
                )
                if attempt + 1 < 3:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                return False
            return True
        except Exception as exc:
            last_err = exc
            logger.warning(
                "SSH bosminer api %s @ %s Versuch %s: %s",
                action,
                ip,
                attempt + 1,
                exc,
            )
            if attempt + 1 < 2:
                time.sleep(0.4 * (attempt + 1))
    if last_err is not None:
        _ssh_circuit_trip(ip)
        logger.error("SSH bosminer api %s @ %s endgültig fehlgeschlagen", action, ip)
    return False


def _verify_status_with_retry(
    ip: str,
    username: str,
    password: str,
    expected: str,
    *,
    attempts: int = 3,
    delay: float = 1.0,
) -> Optional[bool]:
    """True=ok, False=falscher Status, None=Status unlesbar."""
    last: Optional[str] = None
    for attempt in range(attempts):
        last = _ssh_miner_status(ip, username, password)
        if last == expected:
            return True
        if last is not None and last != expected:
            # Noch kurz warten — Pause braucht manchmal 1–2s
            if attempt + 1 < attempts:
                time.sleep(delay)
                continue
            return False
        if attempt + 1 < attempts:
            time.sleep(delay)
    return None if last is None else False


def execute_miner_pause_resume(
    ip: str,
    action: str,
    username: str = "root",
    password: str = "",
) -> bool:
    """Pause oder Resume – SSH bosminer api (Retry), dann gRPC, zuletzt CGMiner nc.

    Erfolg nur bei verifiziertem Status (oder wenn Status gar nicht lesbar,
    aber SSH-Befehl ok war — dann tentative). Bei lesbarem Falsch-Status: Fail.
    """
    if action not in GRPC_PATHS:
        return False

    expected = _expected_status(action)

    # Tot / Circuit / missing: sofort abbrechen – sonst hängt der Steuerungs-Tick.
    # Unabhängig von UI-Häkchen (enabled/auto/Regeln): System bleibt bugfrei lauffähig.
    if _should_skip_unreachable(ip):
        logger.warning(
            "%s %s übersprungen (Miner nicht erreichbar / missing / SSH-Circuit)",
            ip,
            action,
        )
        return False

    if password:
        current = _ssh_miner_status(ip, username, password)
        if current == expected:
            return True

        if _ssh_bosminer_api(ip, username, password, action):
            verified = _verify_status_with_retry(ip, username, password, expected)
            if verified is True:
                return True
            if verified is False:
                logger.error(
                    "%s %s per SSH gesendet, Status aber nicht %s",
                    ip,
                    action,
                    expected,
                )
                # kein Success — Caller muss retryen
            else:
                logger.warning(
                    "%s %s per SSH gesendet, Status unlesbar — tentative OK",
                    ip,
                    action,
                )
                return True

        if _grpc_action(ip, username, password, action):
            verified = _verify_status_with_retry(ip, username, password, expected)
            if verified is True:
                return True
            if verified is False:
                logger.error(
                    "gRPC %s für %s ohne Statuswechsel auf %s",
                    action,
                    ip,
                    expected,
                )
            else:
                # gRPC 200, SSH-Verify unmöglich → akzeptieren
                logger.warning(
                    "gRPC %s für %s OK, Status-Verify unlesbar", action, ip
                )
                return True

    # nc nur, wenn der Status danach geprüft werden kann. Ohne Passwort
    # wäre eine Textantwort ein blinder Erfolg (16. September).
    if not password:
        logger.error(
            "nc %s für %s nicht gesendet – ohne Passwort kein Statusnachweis",
            action,
            ip,
        )
        return False

    nc_out = _nc_output(ip, action)
    if not _nc_succeeded(nc_out):
        logger.error(
            "nc %s für %s ohne gültige Antwort – nicht als Erfolg gewertet",
            action,
            ip,
        )
        return False
    verified = _verify_status_with_retry(
        ip, username, password, expected, attempts=3
    )
    if verified is True:
        return True
    logger.error("nc %s für %s ohne Statuswechsel auf %s", action, ip, expected)
    return False


def read_miner_status(ip: str, username: str, password: str) -> Optional[str]:
    """Aktuellen Pause/Running-Status per SSH lesen."""
    return _ssh_miner_status(ip, username, password)
