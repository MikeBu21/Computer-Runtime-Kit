# -*- coding: utf-8 -*-
"""Öffentliche Standardversion veröffentlichen und herunterladen.

Persönliche Werte bleiben auf diesem Pi. Das öffentliche Repo enthält nur
Beispielgeräte und die Standardeinstellungen aus dem Programm.
Der Upload-Button existiert nur auf diesem Pi und nur, solange keine neuere
öffentliche Version vorliegt und kein Upload läuft.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as _secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent.parent
OWNER = "MikeBu21"
REPO_NAME = "Computer-Runtime-Kit"
REPO_URL = f"https://github.com/{OWNER}/{REPO_NAME}.git"
RAW_RELEASE_URL = (
    f"https://raw.githubusercontent.com/{OWNER}/{REPO_NAME}/main/standard_release.json"
)
BRANCH = "main"
RAW_ADMIN_INDEX_URL = f"https://raw.githubusercontent.com/{OWNER}/{REPO_NAME}/main/admin/index.json"

STATE_DIR = Path(
    os.environ.get(
        "PIMINER_PUBLIC_UPDATE_DIR",
        str(Path.home() / ".local/share/piminer-public-update"),
    )
)
PUBLISHER_FILE = STATE_DIR / "publisher"
STATE_FILE = STATE_DIR / "state.json"
JOB_FILE = STATE_DIR / "job.json"
PUBLISH_DIR = STATE_DIR / "publish"
# Verschlüsselte Admin-Pakete (nur Chiffretext) für den nächsten Push.
ADMIN_STORE_DIR = STATE_DIR / "admin_packages"
# Arbeitskopien nur im RAM: ungeschwärzter Klartext darf nie unverschlüsselt auf die SD-Karte.
RAM_DIR = Path(
    os.environ.get(
        "PIMINER_PUBLIC_UPDATE_RAM",
        "/dev/shm/piminer-public-update" if Path("/dev/shm").is_dir() else str(Path(tempfile.gettempdir()) / "piminer-public-update"),
    )
)
SNAPSHOT_DIR = RAM_DIR / "snapshot"
DOWNLOAD_DIR = RAM_DIR / "download"
PROBE_DIR = RAM_DIR / "probe"
ADMIN_WORK_DIR = RAM_DIR / "admin"
# Liegt im RAM: nach jedem Einschalten ist Auto-Update wieder an.
AUTO_UPDATE_OFF_FLAG = RAM_DIR / "auto_update_off"
LEGACY_WORK_DIRS = (STATE_DIR / "snapshot", STATE_DIR / "download")

# Chip-gebundene Verschlüsselung (gocryptfs). Der Update-Code spricht nur mit der
# eingehängten Klartext-Sicht und fasst die Chiffre-Ordner ~/.NAME.crypt nie an.
CRYPT_FOLDERS = ("minersteuerung", "steuerungsalgorythmus-ranch", "Steuerungsalgorithmus-Range-backup")
CRYPT_READY_FILE = Path(os.environ.get("PIMINER_CRYPT_READY", "/run/miner-chip-crypt.ready"))
CRYPT_SERVICE_FILE = Path(os.environ.get("PIMINER_CRYPT_SERVICE", "/etc/systemd/system/miner-chip-crypt.service"))
CRYPT_WAIT_STEP_SEC = 10
CRYPT_WAIT_MAX_SEC = 3600
ROLLBACK_DIR_NAME = ".update-rollback"
ROLLBACK_KEEP = 3

# Schlüssel liegen im verschlüsselten Ordner und werden nie hochgeladen oder überschrieben.
KEYS_REL = "config/update-keys"
KEYS_DIR = REPO_ROOT / KEYS_REL
NETWORK_KEY_FILE = KEYS_DIR / "network_update.key"
SIGNING_KEY_FILE = KEYS_DIR / "admin_signing.key"
SIGNING_PUB_FILE = KEYS_DIR / "admin_signing.pub"
ROOT_SIGNING_PUB_FILE = Path(os.environ.get("PIMINER_ROOT_SIGNING_PUB", "/etc/piminer/admin_signing.pub"))
RELEASE_SIG_NAME = "standard_release.sig"
RELEASE_FORMAT = 2

SYSTEM_KIT_PREFIX = "miner-dashboard/docs/system/"
SYSTEM_HELPER = Path(os.environ.get("PIMINER_SYSTEM_HELPER", "/usr/local/sbin/piminer-apply-system-files"))
# Systemstartskripte, die per Update verteilt werden. Entschlüsselung und LUKS bleiben gesperrt.
SYSTEM_EXPORT_GLOBS = (
    "/usr/local/bin/piminer-*.sh",
    "/etc/xdg/labwc/autostart",
    "/usr/local/sbin/tailscale-*.sh",
    "/etc/systemd/system/tailscale-*.service",
    "/etc/systemd/system/piminer-*.service",
    "/etc/systemd/system/piminer-*.timer",
)
SYSTEM_DENY_MARKERS = ("miner-chip", "pi-chip-bind", "luks", "gocryptfs", "piminer-apply-system-files", "sudoers")

CLONE_RETRY_SEC = 60
CLONE_RETRY_MAX = 60
HEALTH_WAIT_SEC = 90
_remote_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_remote_refresh_lock = threading.Lock()
_remote_refresh_running = False
REMOTE_CACHE_SEC = 30
AUTO_UPDATE_WAIT_SEC = 60
AUTO_UPDATE_POLL_SEC = 30
AUTO_ADMIN_CHECK_SEC = 300
_auto_update_started = False

EXAMPLE_IP = "192.0.2.10"
_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b|\btskey-[A-Za-z0-9_-]{10,}")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:ghp_|github_pat_|gho_|ghu_|ghs_)[A-Za-z0-9_]{20,}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
# Home-Assistant-Entity-IDs sind Geräte-Zuordnung, keine Secrets für den Download-Scan.
_HA_ENTITY_ID_RE = re.compile(
    r"^(?:sensor|switch|light|binary_sensor|number|input_boolean|input_number|"
    r"input_text|input_select|climate|cover|fan|lock|media_player|person|"
    r"device_tracker|button|select|update|vacuum|water_heater|weather|"
    r"automation|script|scene|group|zone|sun|camera|alarm_control_panel|"
    r"remote|siren|todo|event|image|notify|text|date|time|datetime|"
    r"conversation|stt|tts|assist_satellite)\.[A-Za-z0-9_]+$"
)
_PLACEHOLDER_SECRET_RE = re.compile(r"^(?:DEIN_|YOUR_|EXAMPLE_|BEISPIEL_|TODO_|FIXME_)", re.I)

# Laufzeit und persönliche Historie: nicht veröffentlichen.
# skip_export() entfernt führende Punkte, deshalb beide Schreibweisen.
SKIP_DIR_NAMES = {".storage", "__pycache__", ".git", ROLLBACK_DIR_NAME, ROLLBACK_DIR_NAME.lstrip("."), "update-keys"}
CRYPT_INTERNAL_NAMES = {"gocryptfs.conf", "gocryptfs.diriv", "gocryptfs.longname"}
SKIP_FILE_NAMES = {
    "control_algorithm_learning_store.json",
    "control_algorithm_state.json",
    "control_algorithm_heartbeat.json",
    "control_algorithm_steering_history.json",
    "control_latches.json",
    "curve_budget_audit_last.json",
    "hashrate_timeseries_24h.json",
    "maintenance_watchdog_state.json",
    "miner_status.json",
    "last_actions.json",
    "consumption_history.json",
    "enline_values.json",
    "energy_stats_24h.json",
    "energy_stats_30d.json",
    "energy_stats_year.json",
    "energy_timeseries_24h.json",
    "monthly_peak_15min.json",
    "monthly_peak_15min.peak.json",
    "rule_runtime_status.json",
    "telegram_ladestation_samples.json",
    "telegram_shutdown_snapshot.json",
    "telegram_pcs_latch.json",
    "telegram_shutdown_latch.json",
    "grundverbrauch_rechner.json",
    "boersenhandel_settings.json",
    "energiegemeinschaft_settings.json",
    "bms_steuerung_settings.json",
    "bms_test_settings.json",
    "bms_test_log.json",
    "ip_recovery_notifications.json",
    "daten.json",
    # Nur auf diesem Pi: Einmal-Klon-Endpunkt nicht an andere Pis verteilen.
    "einmal_clone.py",
}
SKIP_PREFIXES = (
    "scripts/tailscale_notify/state/",
    "homeassistant/config/.storage/",
    KEYS_REL + "/",
)

SECRET_KEYS = {
    "password",
    "login_password",
    "telegram_token",
    "token",
    "refresh_token",
    "access_token",
    "api_key",
    "secret",
    "chat_id",
    "chat_ids",
    "user_chat_id",
}
PERSONAL_TEXT_KEYS = {
    "note",
    "label",
    "model",
    "alias",
    "city",
    "city_display",
    "hostname_override",
    "schedule_note",
    "mac",
    "last_seen_ip",
}

_job_lock = threading.Lock()


def upload_allowed(
    *,
    is_publisher: bool,
    uploading: bool,
    downloading: bool,
    local_version: int,
    remote_version: Optional[int],
) -> bool:
    """Upload nur auf dem Veröffentlichungs-Pi und nicht während eines Laufs.

    Ist GitHub gerade nicht prüfbar, bleibt der Button sichtbar; der Push entscheidet.
    """
    return bool(is_publisher) and not uploading and not downloading


def is_publisher() -> bool:
    return PUBLISHER_FILE.is_file()


def _read_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(fallback)


def load_state() -> Dict[str, Any]:
    return _read_json(
        STATE_FILE,
        {
            "local_version": 0,
            "news_pending": False,
            "news_seen_version": 0,
            "last_release_notes": "",
            "last_installed_version": 0,
        },
    )


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _job() -> Dict[str, Any]:
    job = _read_json(
        JOB_FILE,
        {"uploading": False, "downloading": False, "phase": "", "message": "", "pid": 0},
    )
    pid = int(job.get("pid") or 0)
    if (job.get("uploading") or job.get("downloading")) and not _pid_alive(pid):
        job["uploading"] = False
        job["downloading"] = False
        job["phase"] = "error"
        job["message"] = "Vorgang abgebrochen."
        JOB_FILE.write_text(json.dumps(job, indent=2), encoding="utf-8")
    return job


def _write_job(**fields: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    current = _read_json(
        JOB_FILE,
        {"uploading": False, "downloading": False, "phase": "", "message": "", "pid": 0},
    )
    current.update(fields)
    JOB_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")


def _is_crypt_internal_rel(rel: str) -> bool:
    """Pfade der gocryptfs-Chiffre (~/.NAME.crypt, gocryptfs.conf/.diriv) nie lesen oder schreiben."""
    parts = [part for part in str(rel).replace("\\", "/").split("/") if part]
    for part in parts:
        if not part.endswith(".crypt"):
            continue
        if part.startswith(".") or part[: -len(".crypt")] in CRYPT_FOLDERS:
            return True
    return bool(parts) and (parts[-1] in CRYPT_INTERNAL_NAMES or parts[-1].startswith("gocryptfs.longname."))


def crypt_state(repo: Optional[Path] = None) -> Tuple[str, str]:
    """('ready' | 'plain' | 'wait', Meldung) für den Update-Ordner.

    ready: verschlüsselt und eingehängt, Schreiben landet verschlüsselt auf der Karte.
    plain: noch nicht verschlüsselt, der nächste Start verschlüsselt neue Dateien mit.
    wait:  Chiffre vorhanden, aber nicht eingehängt, oder Erstverschlüsselung läuft.
           Schreiben würde Klartext unter den Mount legen, deshalb warten.
    """
    repo = Path(repo or REPO_ROOT)
    cipher_dir = repo.parent / f".{repo.name}.crypt"
    if CRYPT_SERVICE_FILE.is_file() and not CRYPT_READY_FILE.exists():
        return "wait", "Entschlüsselung läuft noch. Das Update wartet und läuft danach automatisch weiter."
    if cipher_dir.exists() or os.path.lexists(cipher_dir):
        if os.path.ismount(repo):
            return "ready", "Verschlüsselte Ordner bereit."
        return "wait", "Verschlüsselter Ordner ist nicht eingehängt. Das Update wartet und läuft danach automatisch weiter."
    return "plain", "Ordner noch nicht verschlüsselt. Der nächste Start verschlüsselt neue Dateien mit."


def wait_for_crypt(job_key: str, repo: Optional[Path] = None, max_sec: Optional[float] = None) -> str:
    """Wartet, bis in den Update-Ordner geschrieben werden darf. Verwirft das Update nicht."""
    limit = CRYPT_WAIT_MAX_SEC if max_sec is None else max_sec
    started = time.time()
    while True:
        state, message = crypt_state(repo)
        if state in {"ready", "plain"}:
            return message
        if time.time() - started >= limit:
            raise RuntimeError(
                "Verschlüsselter Ordner wurde nicht eingehängt. Es wurde nichts geschrieben. "
                "Nach dem Neustart erneut auf Update tippen."
            )
        _write_job(**{job_key: True}, phase="wait_crypt", message=message)
        time.sleep(CRYPT_WAIT_STEP_SEC)


def _assert_repo_target(target: Path, dest_root: Path) -> None:
    """Schreibziel muss in der Klartext-Sicht des Update-Ordners liegen, nie in der Chiffre."""
    root = dest_root.resolve()
    resolved = target.parent.resolve() / target.name
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"Schreibziel außerhalb des Update-Ordners: {target}")
    if _is_crypt_internal_rel(resolved.relative_to(root).as_posix()):
        raise RuntimeError(f"Schreibziel liegt in der Verschlüsselung: {target}")


def _ram_dir() -> Path:
    RAM_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(RAM_DIR, 0o700)
    except OSError:
        pass
    return RAM_DIR


def _cleanup_legacy_workdirs() -> None:
    """Alte Arbeitskopien auf der SD-Karte (vor dieser Version) entfernen."""
    for path in LEGACY_WORK_DIRS:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def _cpu_serial() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("serial"):
                value = line.split(":", 1)[1].strip().lower()
                if value and value.strip("0"):
                    return value
    except Exception:
        pass
    try:
        value = Path("/sys/firmware/devicetree/base/serial-number").read_text(errors="ignore")
        value = value.replace("\x00", "").strip().lower()
        if value and value.strip("0"):
            return value
    except Exception:
        pass
    return ""


def device_id() -> str:
    """Geräte-ID für Admin-Pakete. Eigenes Präfix: nie identisch mit dem Chip-Passwort."""
    serial = _cpu_serial()
    if not serial:
        return ""
    return "pi-" + hashlib.sha256(("piminer-device-id:" + serial).encode("utf-8")).hexdigest()[:12]


def device_names() -> Set[str]:
    names = {socket.gethostname().strip().lower()}
    ident = device_id()
    if ident:
        names.add(ident)
    return {name for name in names if name}


def _fetch_remote_version_now() -> Optional[int]:
    """Netzabruf der öffentlichen Versionsnummer. Timeout 20 s."""
    request = urllib.request.Request(RAW_RELEASE_URL, headers={"User-Agent": "piminer-standard"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return int(payload.get("version") or 0)
    except urllib.error.HTTPError as exc:
        return 0 if exc.code == 404 else None
    except Exception:
        return None


def _store_remote_version(value: Optional[int]) -> Optional[int]:
    _remote_cache["at"] = time.time()
    _remote_cache["value"] = value
    return value


def _schedule_remote_version_refresh() -> None:
    """Hintergrund-Aktualisierung, damit Seitenaufrufe nicht auf GitHub warten."""
    global _remote_refresh_running
    with _remote_refresh_lock:
        if _remote_refresh_running:
            return
        _remote_refresh_running = True

    def worker() -> None:
        global _remote_refresh_running
        try:
            _store_remote_version(_fetch_remote_version_now())
        finally:
            with _remote_refresh_lock:
                _remote_refresh_running = False

    threading.Thread(target=worker, name="public-version-refresh", daemon=True).start()


def fetch_remote_version(use_cache: bool = False, *, allow_network: bool = True) -> Optional[int]:
    """Öffentliche Versionsnummer. 0 = noch kein Release. None = nicht prüfbar.

    allow_network=False: nur Cache, Seitenaufbau blockiert nie (Navigation).
    use_cache=True: frischer Cache sofort, sonst Hintergrund-Refresh und letzter Wert.
    """
    age = time.time() - float(_remote_cache["at"])
    cached = _remote_cache["value"]
    have_cache = float(_remote_cache["at"]) > 0
    if use_cache and have_cache and age < REMOTE_CACHE_SEC:
        return cached
    if not allow_network:
        if not have_cache or age >= REMOTE_CACHE_SEC:
            _schedule_remote_version_refresh()
        return cached if have_cache else None
    if use_cache and have_cache:
        _schedule_remote_version_refresh()
        return cached
    return _store_remote_version(_fetch_remote_version_now())


def next_version(local_version: int, remote_version: Optional[int]) -> int:
    return max(int(remote_version or 0), int(local_version or 0)) + 1


def dashboard_status(*, allow_network: bool = True) -> Dict[str, Any]:
    state = load_state()
    job = _job()
    local_version = int(state.get("local_version") or 0)
    remote_version = fetch_remote_version(use_cache=True, allow_network=allow_network)
    uploading = bool(job.get("uploading"))
    downloading = bool(job.get("downloading"))
    allowed = upload_allowed(
        is_publisher=is_publisher(),
        uploading=uploading,
        downloading=downloading,
        local_version=local_version,
        remote_version=remote_version,
    )
    phase = str(job.get("phase") or "")
    job_message = str(job.get("message") or "").strip()
    if uploading:
        message = job_message or "Upload läuft. Der Upload-Button ist ausgeblendet."
    elif downloading:
        message = job_message or "Update wird geladen."
    elif phase == "error" and job_message:
        # Fehlermeldung nicht durch den Standard-Hinweis überschreiben.
        message = job_message
    elif phase == "done" and job_message and not uploading and not downloading:
        message = job_message
    elif phase == "confirm" and job_message:
        message = job_message
    elif remote_version is None:
        message = f"Lokal Version {local_version}. Öffentliche Version gerade nicht prüfbar."
    elif not is_publisher():
        message = f"Dieser Pi lädt Updates nur herunter. Lokal Version {local_version}, öffentlich Version {remote_version}."
    else:
        message = f"Lokal Version {local_version}. Öffentlich Version {remote_version}."
    progress_map = {
        "start": 8,
        "wait_crypt": 10,
        "export": 28,
        "scan": 48,
        "push": 70,
        "verify": 85,
        "fetch": 30,
        "backup": 45,
        "apply": 60,
        "system": 72,
        "admin": 78,
        "setup": 88,
        "rollback": 95,
        "done": 100,
        "error": 100,
    }
    if uploading or downloading:
        progress = progress_map.get(phase, 15)
    elif phase in {"done", "error"}:
        progress = 100
    else:
        progress = 0
    pending_updates = 0
    if remote_version is not None and int(remote_version) > int(local_version):
        pending_updates = int(remote_version) - int(local_version)
    crypt, crypt_message = crypt_state()
    publisher = is_publisher()
    return {
        "upload_allowed": allowed,
        "uploading": uploading,
        "downloading": downloading,
        "local_version": local_version,
        "remote_version": remote_version,
        "next_version": next_version(local_version, remote_version),
        "pending_updates": pending_updates,
        "phase": phase,
        "progress": progress,
        "message": message,
        "notes": list(job.get("notes") or []),
        "skipped_files": list(job.get("skipped_files") or []),
        "syntax_errors": list(job.get("syntax_errors") or []),
        "confirm_needed": phase == "confirm",
        "role": "publisher" if publisher else "download",
        "auto_update": auto_update_active(),
        "auto_update_available": not publisher,
        "crypt_state": crypt,
        "crypt_ready": crypt != "wait",
        "crypt_message": crypt_message,
        "device_id": device_id(),
        "hostname": socket.gethostname(),
        "signing_available": publisher and SIGNING_KEY_FILE.is_file(),
        "admin_available": publisher and SIGNING_KEY_FILE.is_file() and NETWORK_KEY_FILE.is_file(),
        "repo_url": f"https://github.com/{OWNER}/{REPO_NAME}",
    }


def standard_overlay() -> Dict[str, str]:
    """Dateien, die statt der lokalen Werte ins öffentliche Repo kommen."""
    from control_algorithm.config import default_config as algo_defaults
    from dashboard_config import default_config as dash_defaults
    from ha_entity_mapping import default_config as ha_defaults

    algo = algo_defaults()
    algo["city"] = ""
    algo["city_display"] = ""
    algo["lat"] = None
    algo["lon"] = None
    algo["devices"] = [
        {
            "ip": EXAMPLE_IP,
            "enabled": False,
            "power_kw": 3.0,
            "priority": 5,
        }
    ]
    algo["has_storage"] = False
    algo["storage_kwh"] = 0
    algo["has_pv"] = False
    algo["pv_kwp"] = 0

    example_miner = {
        "ip": EXAMPLE_IP,
        "username": "root",
        "password": "",
        "model": "Beispiel-Miner",
        "note": "Beispielgerät",
        "auto": False,
        "auto_battery": False,
        "pause_kw": "3",
        "resume_kw": "5",
        "lower_kw": "",
        "raise_kw": "",
        "control_source": "computed.erzeugung",
        "battery_control_source": "flow.battery_soc",
        "automation_rules": [],
    }
    example_device = {
        "entity_id": "switch.beispiel_geraet",
        "label": "Beispielgerät",
        "note": "Beispiel",
        "auto": False,
        "auto_battery": False,
        "pause_kw": "",
        "resume_kw": "",
        "lower_kw": "",
        "raise_kw": "",
        "time_pause": False,
        "pause_time": "",
        "resume_time": "",
        "control_source": "field.pv",
        "battery_control_source": "flow.battery_soc",
        "automation_rules": [],
    }
    settings = {
        "avg_minutes": 45,
        "current_kw": "n/a",
        "avg_kw": "n/a",
        "smartmeter_url": "",
        "login_username": "admin",
        "login_password": "",
    }
    telegram = {
        "telegram_token": "",
        "telegram_tokens": [],
        "feed_in_alarm_kw": "-95.0",
        "high_usage_alarm_kw": "50.0",
        "power_outage_enabled": 0,
        "ha_data_monitor_enabled": 0,
    }
    error_cfg = {
        "log_dirs": ["/dev/shm/minersteuerung/logs"],
        "repo": "/home/piminer/minersteuerung",
        "buffer_dir": "/home/piminer/minersteuerung/logs/error_reports/buffer",
        "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
        "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
        "window_days": 14,
        "token_file": "",
        "chats_file": "",
        "telegram_token": "",
        "chat_ids": [],
        "user_chat_id": "",
        "schedule_note": "Standard: keine Empfänger konfiguriert.",
    }
    tailscale = {
        "telegram_token": "",
        "chat_ids": [],
        "token_file": "",
        "chats_file": "",
        "interval_sec": 60,
        "key_warn_days": 14,
        "hostname_override": "",
    }
    tailscale_deploy = {
        "telegram_token": "",
        "chat_ids": [],
        "interval_sec": 60,
        "key_warn_days": 14,
        "hostname_override": "",
    }
    legacy_miners = [
        {
            "name": "Beispiel-Miner",
            "ip": EXAMPLE_IP,
            "user": "root",
            "password": "",
            "type": "beispiel",
            "enabled": False,
        }
    ]
    ha_map = ha_defaults()
    for field in (ha_map.get("fields") or {}).values():
        if isinstance(field, dict):
            field["entity_id"] = ""
            field["sensors"] = []
    credentials = {
        "refresh_token": "",
        "client_id": "http://127.0.0.1:8123/",
        "ha_url": "http://127.0.0.1:8123",
    }
    readme = f"""# Computer-Runtime-Kit

Öffentliche Standardversion der Programmoberfläche und des Laufzeitcodes.

Enthalten sind der aktuelle Programmstand und Beispielwerte
(Beispielgeräte, leere Zugänge, Standardeinstellungen).

Nicht enthalten sind persönliche Daten: Tokens, Passwörter, echte Geräte,
Standorte und Verläufe.

## Ersteinrichtung

```bash
git clone https://github.com/{OWNER}/{REPO_NAME}.git ~/minersteuerung
cd ~/minersteuerung
```

Danach die lokale Einrichtung wie gewohnt ausführen. Dashboard-Anmeldung der
Standardversion: Benutzer `admin`, ohne Passwort. Echte Zugangsdaten nur lokal
eintragen.

## Update auf einem anderen Gerät

1. Programm und Dashboard starten.
2. Im Dashboard unter **Öffentliches Update** auf **Update herunterladen** tippen.
3. Es wird nur Programmcode aktualisiert. Persönliche Einstellungen, Geräte,
   Passwörter, Lernstände und Historie bleiben unverändert.
4. Auto-Start und die skalierbare Anzeige werden mit eingerichtet.
5. Die Software-Dienste starten danach neu (kein Computer-Reboot).
6. Der Fortschrittsbalken zeigt den Stand, bis das Update fertig ist.

Details auch in `UPDATE.md`.

Dieses Repo ist öffentlich. Hochladen neuer Versionen ist nur auf dem
Veröffentlichungsgerät möglich, nicht über den Download-Button.
"""
    update_guide = f"""# Update-Anleitung

So holst du die neueste Version von `{OWNER}/{REPO_NAME}` auf ein Gerät,
auf dem die Software bereits läuft.

## Kurz

1. Dashboard öffnen.
2. Bereich **Öffentliches Update**.
3. **Update herunterladen** tippen.
4. Warten, bis der Fortschrittsbalken bei 100 % ist.

## Was wird aktualisiert?

Nur Programmcode (zum Beispiel `.py`, `.html`, `.css`, `.js`, `.sh`).

Zusätzlich richtet der Download die Auto-Start-Struktur ein (Desktop-Eintrag,
Startreihenfolge, Statusfenster) und startet die Software-Dienste neu
(Scheduler, Batterie-Scraper, Flask-Backend, Energiemonitor,
Steuerungsalgorithmus, Telegram-Bot). Der Computer wird **nicht** neu gebootet.

## Was bleibt erhalten?

- Persönliche Einstellungen
- Geräte und Zuordnungen
- Passwörter und Tokens
- Chat-IDs
- Lernstände / Neuronen
- Historie und Trainingsdaten

## Erster Start ohne vorhandene Installation

```bash
git clone https://github.com/{OWNER}/{REPO_NAME}.git ~/minersteuerung
```

Danach lokal einrichten. Der Button **Update herunterladen** ist in dieser
Version bereits enthalten und holt künftige Updates aus demselben Repo.
"""
    files = {
        "miner-dashboard/backend/miners.json": json.dumps([example_miner], indent=2, ensure_ascii=False) + "\n",
        "miner-dashboard/backend/ha_devices.json": json.dumps([example_device], indent=2, ensure_ascii=False) + "\n",
        "miner-dashboard/backend/dashboard_settings.json": json.dumps(settings, indent=2) + "\n",
        "miner-dashboard/backend/telegram_config.json": json.dumps(telegram, indent=2) + "\n",
        "miner-dashboard/backend/telegram_chats.json": "[]\n",
        "miner-dashboard/backend/control_algorithm_config.json": json.dumps(algo, indent=2, ensure_ascii=False) + "\n",
        "miner-dashboard/backend/config/energy_chat_ids.json": "[]\n",
        "config/dashboard_config.json": json.dumps(dash_defaults(), indent=2, ensure_ascii=False) + "\n",
        "config/miners.json": json.dumps(legacy_miners, indent=2, ensure_ascii=False) + "\n",
        "config/ha_entity_mapping.json": json.dumps(ha_map, indent=2, ensure_ascii=False) + "\n",
        "scripts/error_log_pipeline/pi/config.json": json.dumps(error_cfg, indent=2, ensure_ascii=False) + "\n",
        "scripts/tailscale_notify/linux/config.json": json.dumps(tailscale, indent=2) + "\n",
        "scripts/tailscale_notify/linux/config.deploy.json": json.dumps(tailscale_deploy, indent=2) + "\n",
        "homeassistant/config/secrets.yaml": "# Standard ohne Zugangsdaten\nsome_password: \"\"\n",
        "homeassistant/config/automations.yaml": "[]\n",
        "homeassistant/ha-bridge/credentials.json": json.dumps(credentials, indent=2) + "\n",
        "README.md": readme,
        "UPDATE.md": update_guide,
    }
    return files


def protected_download_paths() -> Set[str]:
    """Diese Dateien bleiben auf diesem Pi beim Herunterladen unverändert."""
    return set(standard_overlay()) | {
        "homeassistant/config/.storage",
    }


def skip_export(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    parts = rel.split("/")
    if any(part in SKIP_DIR_NAMES for part in parts):
        return True
    if _is_crypt_internal_rel(rel):
        return True
    if rel.startswith(SKIP_PREFIXES) or parts[-1] in SKIP_FILE_NAMES:
        return True
    if "/energy_monitor/" in f"/{rel}" and (
        parts[-1].endswith("_value.txt") or parts[-1].endswith("_avg.txt")
    ):
        return True
    if parts[-1].endswith((".log", ".bak", ".db")):
        return True
    return False


def _add_secret(bucket: Set[str], value: Any) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        if re.fullmatch(r"\d{6,}", text):
            bucket.add(text)
        return
    if not isinstance(value, str):
        return
    text = value.strip()
    if len(text) < 6:
        return
    if text.lower() in {
        "n/a",
        "welcome",
        "example",
        "beispiel",
        "beispielgerät",
        "device",
        "single",
        "switch",
        "sensor",
        "turn_on",
        "turn_off",
        "admin",
    }:
        return
    bucket.add(text)
    for match in _IPV4_RE.findall(text):
        if not match.startswith("192.0.2.") and match not in {"127.0.0.1", "0.0.0.0"}:
            bucket.add(match)
    for match in _MAC_RE.findall(text):
        bucket.add(match)
    for match in _TOKEN_RE.findall(text):
        bucket.add(match)


def _walk_personal(node: Any, bucket: Set[str], key: str = "") -> None:
    if isinstance(node, dict):
        for child_key, child in node.items():
            name = str(child_key)
            if name in SECRET_KEYS or name in PERSONAL_TEXT_KEYS or name in {"ip", "entity_id", "device_id", "lat", "lon"}:
                if isinstance(child, list):
                    for item in child:
                        _add_secret(bucket, item)
                else:
                    _add_secret(bucket, child)
            _walk_personal(child, bucket, name)
        return
    if isinstance(node, list):
        for item in node:
            _walk_personal(item, bucket, key)


def collect_sensitive_values(root: Path) -> Set[str]:
    """Werte aus den lokalen Dateien, die im öffentlichen Baum nicht vorkommen dürfen."""
    found: Set[str] = set()
    candidates = [
        "miner-dashboard/backend/miners.json",
        "miner-dashboard/backend/ha_devices.json",
        "miner-dashboard/backend/dashboard_settings.json",
        "miner-dashboard/backend/telegram_config.json",
        "miner-dashboard/backend/telegram_chats.json",
        "miner-dashboard/backend/control_algorithm_config.json",
        "miner-dashboard/backend/config/energy_chat_ids.json",
        "config/miners.json",
        "config/ha_entity_mapping.json",
        "scripts/error_log_pipeline/pi/config.json",
        "scripts/tailscale_notify/linux/config.json",
        "scripts/tailscale_notify/linux/config.deploy.json",
        "homeassistant/ha-bridge/credentials.json",
        "homeassistant/config/secrets.yaml",
        "homeassistant/config/automations.yaml",
    ]
    for rel in candidates:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _TOKEN_RE.findall(text):
            found.add(match)
        for match in _MAC_RE.findall(text):
            found.add(match)
        for match in _IPV4_RE.findall(text):
            if not match.startswith("192.0.2.") and match not in {"127.0.0.1", "0.0.0.0", "10.255.255.255"}:
                found.add(match)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                for item in parsed:
                    _add_secret(found, item)
                    _walk_personal(item, found)
            else:
                _walk_personal(parsed, found)
        except Exception:
            for match in re.findall(r"device_id:\s*([0-9a-fA-F]{12,})", text):
                found.add(match)
            for match in re.findall(r"alias:\s*(.+)", text):
                _add_secret(found, match.strip().strip("\"'"))
    found.discard(EXAMPLE_IP)
    found.discard("switch.beispiel_geraet")
    found.discard("admin")
    return {item for item in found if len(item) >= 6}


def redact_tree(root: Path, secrets: Iterable[str]) -> int:
    """Ersetzt persönliche Werte in der Kopie. Das laufende Projekt bleibt unverändert."""
    needles = sorted({item for item in secrets if item and item != EXAMPLE_IP and len(item) >= 6}, key=len, reverse=True)
    if not needles:
        return 0
    replaced = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".gz", ".zip", ".db"}:
            continue
        if path.stat().st_size > 8_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        updated = text
        for needle in needles:
            if needle not in updated:
                continue
            if _IPV4_RE.fullmatch(needle):
                placeholder = EXAMPLE_IP
            elif needle.isdigit():
                placeholder = "0"
            elif _MAC_RE.fullmatch(needle):
                placeholder = "02:00:00:00:00:00"
            else:
                placeholder = "Beispiel"
            updated = updated.replace(needle, placeholder)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            replaced += 1
    return replaced


def _scan_needle_usable(needle: str) -> bool:
    """Nur hochvertrauenswürdige Secrets für Download-/Code-Scan (keine HA-Entity-IDs)."""
    if not needle or needle == EXAMPLE_IP:
        return False
    text = str(needle).strip()
    if not text:
        return False
    # Geräte-Zuordnung / Platzhalter ≠ Leak-Nadel gegen Kit-Code.
    if _HA_ENTITY_ID_RE.fullmatch(text):
        return False
    if _PLACEHOLDER_SECRET_RE.match(text) or text.upper() in {"Beispiel", "YOUR_PASSWORD", "CHANGEME"}:
        return False
    # Tokens (Telegram / GitHub)
    if _TOKEN_RE.fullmatch(text) or _GITHUB_TOKEN_RE.fullmatch(text):
        return True
    if _TOKEN_RE.search(text) or _GITHUB_TOKEN_RE.search(text):
        return True
    # Chat-IDs (lange Ziffernfolgen)
    if text.isdigit() and len(text) >= 8:
        return True
    # MAC / echte LAN-IPs (Dokumentations-IP 192.0.2.* ausgenommen)
    if _MAC_RE.fullmatch(text):
        return True
    if _IPV4_RE.fullmatch(text) and not text.startswith("192.0.2."):
        if text not in {"127.0.0.1", "0.0.0.0"}:
            return True
    # Lange Einwort-Secrets/Passwörter (Labels mit Leerzeichen und HA-IDs sind raus).
    if len(text) >= 12 and " " not in text:
        return True
    return False


def scan_tree(root: Path, secrets: Iterable[str]) -> List[str]:
    """Pfade, in denen persönliche Werte oder Token-Muster stehen. Ohne die Werte selbst."""
    needles = [item for item in secrets if _scan_needle_usable(str(item))]
    problems: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        # Skip-Verzeichnisse (.git, __pycache__, …) sind kein Fund — nur auslassen.
        if any(part in SKIP_DIR_NAMES for part in Path(rel).parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".gz", ".zip", ".db"}:
            continue
        if path.stat().st_size > 8_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if _TOKEN_RE.search(text) or _GITHUB_TOKEN_RE.search(text):
            problems.append(rel)
            continue
        for needle in needles:
            if needle in text:
                problems.append(rel)
                break
    return sorted(set(problems))


def project_files(repo: Path) -> List[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(repo),
    )
    return [item.decode("utf-8") for item in output.split(b"\0") if item]


# Allgemeine Projektdateien ohne Code-Endung, die trotzdem öffentlich sein dürfen.
PUBLIC_EXTRA_FILES = {"requirements.txt"}


def is_public_export(rel: str) -> bool:
    """Upload nimmt nur Programmcode und Setup-Dateien mit, keine Einstellungsdateien."""
    rel = rel.replace("\\", "/")
    if rel in PUBLIC_EXTRA_FILES:
        return True
    return is_installable_code(rel) or is_setup_asset(rel)


def _system_export_paths() -> List[Path]:
    import glob

    found: List[Path] = []
    for pattern in SYSTEM_EXPORT_GLOBS:
        for name in sorted(glob.glob(pattern)):
            path = Path(name)
            low = str(path).lower()
            if any(marker in low for marker in SYSTEM_DENY_MARKERS):
                continue
            if path.is_file() and not path.is_symlink() and os.access(path, os.R_OK):
                found.append(path)
    return found


def export_system_files(dest: Path) -> List[str]:
    """Systemstartskripte des Hauptrechners in den Kit-Ordner docs/system/ legen (live gewinnt)."""
    exported: List[str] = []
    for path in _system_export_paths():
        rel = SYSTEM_KIT_PREFIX + str(path).lstrip("/")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        try:
            target.chmod(path.stat().st_mode & 0o777)
        except OSError:
            pass
        exported.append(rel)
    return exported


def export_snapshot(dest: Path, version: int, repo: Optional[Path] = None) -> None:
    repo = repo or REPO_ROOT
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass
    for rel in project_files(repo):
        if skip_export(rel) or not is_public_export(rel):
            continue
        source = repo / rel
        if not source.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if repo == REPO_ROOT:
        export_system_files(dest)
    overlay = standard_overlay()
    for rel, content in overlay.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    release = {
        "version": int(version),
        "channel": "standard",
        "notes": (
            "Download nutzt den Soft-Scan: nur Token im Programmcode, und der Scan wird nach jedem "
            "Update wiederhergestellt. Der Upload-Scan bleibt streng. Standardwerte ohne persönliche Daten."
        ),
    }
    (dest / "standard_release.json").write_text(
        json.dumps(release, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    redact_tree(dest, collect_sensitive_values(repo))


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = "piminer Standard"
    env["GIT_AUTHOR_EMAIL"] = "piminer@users.noreply.github.com"
    env["GIT_COMMITTER_NAME"] = "piminer Standard"
    env["GIT_COMMITTER_EMAIL"] = "piminer@users.noreply.github.com"
    return env


def _run_git(args: List[str], cwd: Path) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"git {' '.join(args)} fehlgeschlagen"
        raise RuntimeError(tail)


def _github_auth() -> Tuple[str, str]:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=_git_env(),
    )
    username = ""
    password = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("username="):
            username = line.split("=", 1)[1]
        elif line.startswith("password="):
            password = line.split("=", 1)[1]
    if not username or not password:
        raise RuntimeError("Hinterlegte GitHub-Anmeldung fehlt. Es wird kein Passwort abgefragt.")
    return username, password


def _github_api(method: str, url: str, payload: Optional[dict] = None) -> tuple[int, dict]:
    username, password = _github_auth()
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "piminer-standard",
            "Authorization": f"Basic {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8")
            body = json.loads(raw) if raw.strip() else {}
            return int(response.status), body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {"message": raw[:200]}
        if not isinstance(body, dict):
            body = {"message": str(body)}
        return int(exc.code), body


def ensure_public_repo() -> None:
    code, body = _github_api(
        "GET",
        f"https://api.github.com/repos/{OWNER}/{REPO_NAME}",
    )
    if code == 200:
        if body.get("private") is True:
            raise RuntimeError("Das Repo existiert, ist aber privat. Es muss öffentlich sein.")
        return
    if code not in (404, 301):
        raise RuntimeError(f"Repo-Status nicht prüfbar ({code}).")

    code, body = _github_api(
        "POST",
        "https://api.github.com/user/repos",
        {
            "name": REPO_NAME,
            "private": False,
            "description": "Shared runtime helpers and default templates.",
            "auto_init": False,
        },
    )
    if code in (200, 201):
        if body.get("private") is True:
            raise RuntimeError("Repo wurde privat angelegt.")
        return
    if code == 422:
        # schon vorhanden
        return
    detail = str(body.get("message") or "").strip() or f"HTTP {code}"
    raise RuntimeError(f"Öffentliches Repo konnte nicht angelegt werden: {detail}")


def _sync_publish_repo() -> None:
    """publish/ auf den GitHub-Stand bringen, damit kein Push abgewiesen wird."""
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    if not (PUBLISH_DIR / ".git").is_dir():
        _run_git(["init", "-b", BRANCH], PUBLISH_DIR)
        _run_git(["remote", "add", "origin", REPO_URL], PUBLISH_DIR)
    try:
        _run_git(["fetch", "--depth", "1", "origin", BRANCH], PUBLISH_DIR)
        _run_git(["reset", "--hard", f"origin/{BRANCH}"], PUBLISH_DIR)
    except RuntimeError:
        pass


def _copy_admin_store_into(root: Path) -> int:
    """Verschlüsselte Admin-Pakete bei jedem Push mitnehmen (nur Chiffretext)."""
    count = 0
    if not ADMIN_STORE_DIR.is_dir():
        return 0
    ids: List[str] = []
    for path in sorted(ADMIN_STORE_DIR.glob("*.enc")):
        target = root / "admin" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        ids.append(path.name[: -len(".enc")])
        count += 1
    if ids:
        (root / "admin" / "index.json").write_text(json.dumps({"ids": ids}, indent=2) + "\n", encoding="utf-8")
    return count


def _mirror_into_publish(snapshot: Path) -> None:
    _sync_publish_repo()
    for child in PUBLISH_DIR.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(snapshot)
        target = PUBLISH_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    _copy_admin_store_into(PUBLISH_DIR)


def _push_publish(message: str = "Standardupdate ohne persönliche Daten") -> None:
    _run_git(["add", "-A"], PUBLISH_DIR)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(PUBLISH_DIR),
        env=_git_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if status.stdout.strip():
        _run_git(["commit", "-m", message], PUBLISH_DIR)
    _run_git(["push", "-u", "origin", f"HEAD:{BRANCH}"], PUBLISH_DIR)


# --- Signatur, Manifest und Prüfung (Upload und Download nutzen dieselbe Funktion) ---


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_signing_key():
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(SIGNING_KEY_FILE.read_bytes(), password=None)


def _load_verify_key():
    from cryptography.hazmat.primitives import serialization

    for path in (ROOT_SIGNING_PUB_FILE, SIGNING_PUB_FILE):
        try:
            if path.is_file():
                return serialization.load_pem_public_key(path.read_bytes())
        except Exception:
            continue
    return None


def sign_bytes(data: bytes) -> str:
    return _b64(_load_signing_key().sign(data))


def verify_bytes(data: bytes, signature_b64: str) -> str:
    """'ok' | 'invalid' | 'missing' | 'no-key'."""
    if not signature_b64:
        return "missing"
    key = _load_verify_key()
    if key is None:
        return "no-key"
    try:
        key.verify(base64.b64decode(signature_b64), data)
        return "ok"
    except Exception:
        return "invalid"


def _release_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[0] in {".git", "admin"} or rel in {"standard_release.json", RELEASE_SIG_NAME}:
            continue
        files.append(path)
    return files


def write_release_manifest(root: Path, version: int) -> bool:
    """standard_release.json mit Format, Hashes und Signatur. True, wenn signiert."""
    release_path = root / "standard_release.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except Exception:
        release = {}
    release.update(
        {
            "version": int(version),
            "channel": "standard",
            "format": RELEASE_FORMAT,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": {path.relative_to(root).as_posix(): _sha256_file(path) for path in _release_files(root)},
        }
    )
    release.setdefault("notes", "Standardwerte ohne persönliche Daten.")
    data = (json.dumps(release, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    release_path.write_bytes(data)
    sig_path = root / RELEASE_SIG_NAME
    if SIGNING_KEY_FILE.is_file():
        try:
            sig_path.write_text(sign_bytes(data) + "\n", encoding="ascii")
            return True
        except Exception:
            pass
    if sig_path.exists():
        sig_path.unlink()
    return False


def _is_shell_script(path: Path, rel: str) -> bool:
    if rel.endswith(".sh"):
        return True
    if not rel.startswith(SYSTEM_KIT_PREFIX):
        return False
    try:
        with open(path, "rb") as handle:
            first = handle.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and (b"bash" in first or b"/sh" in first)


def syntax_problems(root: Path, only_installable: bool = False) -> List[str]:
    """Python- und Shell-Syntax mit dem Python dieses Geräts. Liefert Hinweise, verwirft nichts."""
    problems: List[str] = []
    bash = shutil.which("bash")
    for path in _release_files(root):
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIR_NAMES for part in rel.split("/")):
            continue
        if only_installable and not (is_installable_code(rel) or is_setup_asset(rel)):
            continue
        if rel.endswith(".py"):
            try:
                compile(path.read_bytes(), rel, "exec", dont_inherit=True)
            except SyntaxError as exc:
                problems.append(f"{rel}:{exc.lineno}: {exc.msg}")
            except ValueError as exc:
                problems.append(f"{rel}: {exc}")
        elif bash and _is_shell_script(path, rel):
            try:
                proc = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True, timeout=20, check=False)
            except Exception as exc:
                problems.append(f"{rel}: bash -n nicht prüfbar ({exc})")
                continue
            if proc.returncode != 0:
                detail = (proc.stderr or "").strip().splitlines()
                problems.append(f"{rel}: {(detail[-1] if detail else 'bash -n fehlgeschlagen')[:160]}")
    return problems


def validate_release(root: Path, *, upload: bool, secrets: Iterable[str] = ()) -> Dict[str, Any]:
    """Gemeinsame Prüfung für Upload, Probe-Download und Download.

    Liefert nur einen Bericht. Kein Befund verwirft das Update.
    """
    report: Dict[str, Any] = {
        "version": 0,
        "format": 0,
        "notes": "",
        "release_ok": False,
        "token_files": [],
        "personal_files": [],
        "syntax_errors": [],
        "hash_mismatch": [],
        "signature": "missing",
        "file_count": 0,
    }
    release_path = root / "standard_release.json"
    release_bytes = b""
    release: Dict[str, Any] = {}
    try:
        release_bytes = release_path.read_bytes()
        loaded = json.loads(release_bytes.decode("utf-8"))
        if isinstance(loaded, dict):
            release = loaded
            report["release_ok"] = True
    except Exception:
        release = {}
    try:
        report["version"] = int(release.get("version") or 0)
    except Exception:
        report["version"] = 0
    try:
        report["format"] = int(release.get("format") or 0)
    except Exception:
        report["format"] = 0
    report["notes"] = str(release.get("notes") or "")
    report["file_count"] = len(_release_files(root))
    if upload:
        report["personal_files"] = scan_tree(root, secrets)
    else:
        report["token_files"] = [
            p for p in scan_tree(root, [])
            if is_installable_code(str(p)) or is_setup_asset(str(p))
        ]
    report["syntax_errors"] = syntax_problems(root, only_installable=not upload)
    manifest = release.get("files")
    if isinstance(manifest, dict):
        for rel, digest in manifest.items():
            path = root / str(rel)
            if ".." in str(rel).split("/") or not path.is_file() or _sha256_file(path) != str(digest):
                report["hash_mismatch"].append(str(rel))
    sig_path = root / RELEASE_SIG_NAME
    if release_bytes and sig_path.is_file():
        report["signature"] = verify_bytes(release_bytes, sig_path.read_text(encoding="ascii", errors="ignore").strip())
    return report


def report_notes(report: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if not report.get("release_ok"):
        notes.append("standard_release.json fehlt oder ist beschädigt.")
    if report.get("release_ok") and int(report.get("format") or 0) < RELEASE_FORMAT:
        notes.append("Älteres Release-Format ohne Prüfsummen. Wird trotzdem installiert.")
    token_files = report.get("token_files") or []
    if token_files:
        notes.append(f"Hinweis: Token-Muster in {len(token_files)} Datei(en), z. B. {token_files[0]}.")
    syntax = report.get("syntax_errors") or []
    if syntax:
        notes.append(f"Hinweis: Syntaxfehler in {len(syntax)} Datei(en), z. B. {syntax[0]}.")
    mismatch = report.get("hash_mismatch") or []
    if mismatch:
        notes.append(f"Hinweis: Prüfsumme weicht ab in {len(mismatch)} Datei(en), z. B. {mismatch[0]}.")
    signature = report.get("signature")
    if signature == "invalid":
        notes.append("Release-Signatur ungültig: Systemstartskripte werden nicht installiert, Programmcode schon.")
    elif signature in {"missing", "no-key"}:
        notes.append("Release ohne prüfbare Signatur: Systemstartskripte werden nicht installiert, Programmcode schon.")
    return notes


def _drop_personal_files(snapshot: Path, problems: Iterable[str]) -> List[str]:
    """Dateien mit persönlichen Daten nicht neu veröffentlichen; die vorherige öffentliche Fassung bleibt."""
    dropped: List[str] = []
    for rel in sorted(set(problems)):
        path = snapshot / rel
        if not path.is_file():
            continue
        path.unlink()
        previous = PUBLISH_DIR / rel
        if previous.is_file() and not previous.is_symlink():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous, path)
        dropped.append(rel)
    return dropped


def _clone_release(dest: Path, job_key: str) -> None:
    """Öffentliche Version nach dest (RAM) holen. Netzfehler: alle 60 s neu versuchen."""
    _ram_dir()
    for attempt in range(1, CLONE_RETRY_MAX + 1):
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            _run_git(["clone", "--depth", "1", "--branch", BRANCH, REPO_URL, str(dest)], RAM_DIR)
            break
        except RuntimeError as exc:
            if attempt >= CLONE_RETRY_MAX:
                raise RuntimeError(f"GitHub nicht erreichbar ({exc}). Es wurde nichts geschrieben.")
            _write_job(
                **{job_key: True},
                phase="fetch",
                message=f"GitHub nicht erreichbar, neuer Versuch in {CLONE_RETRY_SEC} s (Versuch {attempt}).",
            )
            time.sleep(CLONE_RETRY_SEC)
    git_meta = dest / ".git"
    if git_meta.exists():
        shutil.rmtree(git_meta, ignore_errors=True)
    if not any(dest.iterdir()):
        raise RuntimeError("Öffentliche Version ist leer. Es wurde nichts geschrieben.")


def probe_download(version: int) -> List[str]:
    """Nach dem Push genau so laden und prüfen wie die anderen Pis. Nur Hinweise."""
    notes: List[str] = []
    raw = fetch_remote_version()
    if raw is None:
        notes.append("Probe: öffentliche Versionsdatei gerade nicht abrufbar.")
    elif int(raw) != int(version):
        notes.append(f"Probe: GitHub zeigt noch Version {raw} (Zwischenspeicher, aktualisiert sich in wenigen Minuten).")
    try:
        _run_probe_clone()
        report = validate_release(PROBE_DIR, upload=False)
        if int(report.get("version") or 0) != int(version):
            notes.append(f"Probe: geladene Version ist {report.get('version')}, erwartet {version}.")
        notes.extend("Probe: " + item for item in report_notes(report))
        if not notes:
            notes.append(f"Probe-Download in Ordnung: Version {version} wird von allen Pis angenommen.")
    except Exception as exc:
        notes.append(f"Probe-Download nicht möglich: {exc}")
    finally:
        shutil.rmtree(PROBE_DIR, ignore_errors=True)
    return notes


def _run_probe_clone() -> None:
    _ram_dir()
    if PROBE_DIR.exists():
        shutil.rmtree(PROBE_DIR, ignore_errors=True)
    _run_git(["clone", "--depth", "1", "--branch", BRANCH, REPO_URL, str(PROBE_DIR)], RAM_DIR)
    shutil.rmtree(PROBE_DIR / ".git", ignore_errors=True)


def _upload_worker(version: int, force: bool = False) -> None:
    snapshot = SNAPSHOT_DIR
    notes: List[str] = []
    skipped: List[str] = []
    syntax: List[str] = []
    try:
        _ram_dir()
        _cleanup_legacy_workdirs()
        crypt_message = wait_for_crypt("uploading")
        if crypt_state()[0] == "plain":
            notes.append(crypt_message)
        _write_job(uploading=True, phase="export", message="Standardversion wird zusammengestellt (nur im RAM).")
        export_snapshot(snapshot, version)
        _write_job(phase="scan", message="Prüfung auf persönliche Daten.")
        report = validate_release(snapshot, upload=True, secrets=collect_sensitive_values(REPO_ROOT))
        skipped = _drop_personal_files(snapshot, report["personal_files"])
        if skipped:
            notes.append(
                f"{len(skipped)} Datei(en) nicht neu veröffentlicht (persönliche Daten), z. B. {skipped[0]}. "
                "Die vorherige öffentliche Fassung bleibt."
            )
        syntax = syntax_problems(snapshot)
        if syntax and not force:
            _write_job(
                uploading=False,
                phase="confirm",
                message=f"Syntaxfehler in {len(syntax)} Datei(en), z. B. {syntax[0]}. Trotzdem veröffentlichen?",
                syntax_errors=syntax,
                skipped_files=skipped,
                notes=notes,
                pending_version=int(version),
            )
            return
        if syntax:
            notes.append(f"Mit {len(syntax)} Syntaxhinweis(en) veröffentlicht, z. B. {syntax[0]}.")
        if not write_release_manifest(snapshot, version):
            notes.append("Ohne Signatur veröffentlicht: Systemstartskripte werden auf den Pis nicht installiert.")
        _write_job(phase="push", message="Öffentliches Repo wird aktualisiert.")
        ensure_public_repo()
        _mirror_into_publish(snapshot)
        _push_publish()
        state = load_state()
        state["local_version"] = int(version)
        save_state(state)
        _write_job(phase="verify", message="Probe-Download wie auf den anderen Pis.")
        notes.extend(probe_download(version))
        _write_job(
            uploading=False,
            phase="done",
            message=(
                f"Version {version} ist öffentlich. Nur Programmcode und Beispielwerte, ohne persönliche Daten. "
                "Steuerungsprozesse werden neu gestartet (kein Anlagen-Neustart)."
            ),
            notes=notes,
            skipped_files=skipped,
            syntax_errors=syntax,
        )
        restart_control_services_soon()
    except Exception as exc:
        _write_job(
            uploading=False,
            phase="error",
            message=str(exc) or "Upload fehlgeschlagen.",
            notes=notes,
            skipped_files=skipped,
            syntax_errors=syntax,
        )
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


def start_upload(force: bool = False) -> Tuple[bool, str]:
    with _job_lock:
        status = dashboard_status()
        if not status["upload_allowed"]:
            return False, status["message"] or "Upload ist nicht verfügbar."
        try:
            _github_auth()
        except RuntimeError as exc:
            return False, str(exc)
        job = _job()
        pending = int(job.get("pending_version") or 0) if job.get("phase") == "confirm" else 0
        version = pending if (force and pending) else next_version(status["local_version"], status["remote_version"])
        _write_job(
            uploading=True,
            downloading=False,
            phase="start",
            message="Upload gestartet.",
            pid=os.getpid(),
            notes=[],
            skipped_files=[],
            syntax_errors=[],
            pending_version=0,
        )
    threading.Thread(target=_upload_worker, args=(version, bool(force)), daemon=True).start()
    return True, "Upload gestartet. Der Upload-Button ist währenddessen ausgeblendet."


# Nur diese Endungen können Programmcode sein. Alles andere wird nie installiert.
CODE_SUFFIXES = {".py", ".html", ".css", ".js", ".mjs", ".sh", ".md"}

# Ordner, die persönliche, historische oder zwischengespeicherte Daten halten.
PERSONAL_DIR_NAMES = {
    "backup",
    "backups",
    "export",
    "exports",
    "logs",
    "state",
    "tmp",
    "temp",
    "cache",
    ".storage",
    "shared_settings",
    "credentials",
    "profiles",
    "profile",
    "private",
    "outbox",
    "sent",
    "buffer",
    "error_reports",
    "snapshots",
    "snapshot",
    "history",
    "training",
    "chats",
    "local-data",
    "enline_debug",
    ".git",
    "__pycache__",
}

# Dateinamen, die trotz Code-Endung benutzerspezifisch sind.
PERSONAL_NAME_MARKERS = (
    "backup",
    "secret",
    "credential",
    "password",
    "profile",
    ".local.",
    ".user.",
    ".override.",
    ".bak",
)


def is_setup_asset(rel: str) -> bool:
    """Mitgelieferte Setup-Dateien (Icons, Desktop, systemd), kein persönlicher Code."""
    rel = rel.replace("\\", "/").lstrip("./")
    if not rel or ".." in rel.split("/"):
        return False
    suffix = Path(rel).suffix.lower()
    if rel.startswith("miner-dashboard/docs/icons/") and suffix == ".png":
        return True
    if rel.startswith("miner-dashboard/docs/autostart/") and suffix == ".desktop":
        return True
    if rel.startswith("miner-dashboard/docs/systemd-user/") and suffix in {".service", ".timer"}:
        return True
    if rel.startswith("Desktop/") and suffix == ".desktop":
        return True
    if rel.startswith(SYSTEM_KIT_PREFIX) and _system_target_for_rel(rel) is not None:
        return True
    return False


def _system_target_for_rel(rel: str) -> Optional[str]:
    """docs/system/<pfad> -> /<pfad>, nur für erlaubte Systemstartskripte."""
    rel = rel.replace("\\", "/")
    if not rel.startswith(SYSTEM_KIT_PREFIX):
        return None
    target = "/" + rel[len(SYSTEM_KIT_PREFIX):]
    if ".." in target.split("/"):
        return None
    if system_target_allowed(target, admin=False):
        return target
    return None


def system_target_allowed(target: str, admin: bool) -> bool:
    """Gleiche Regeln wie im Root-Helfer. Entschlüsselung, LUKS und der Helfer bleiben gesperrt."""
    import fnmatch

    target = os.path.normpath(target)
    if not target.startswith("/") or ".." in target.split("/"):
        return False
    low = target.lower()
    if any(marker in low for marker in ("miner-chip", "pi-chip-bind", "luks", "gocryptfs", "piminer-apply-system-files")):
        return False
    if low.startswith(("/proc/", "/sys/", "/dev/", "/run/", "/boot/")):
        return False
    if re.search(r"/\.[^/]+\.crypt(/|$)", target):
        return False
    if any(target == f"/home/piminer/{name}" or target.startswith(f"/home/piminer/{name}/") for name in CRYPT_FOLDERS):
        return False
    if admin:
        return True
    if "sudoers" in low:
        return False
    return any(fnmatch.fnmatchcase(target, pattern) for pattern in SYSTEM_EXPORT_GLOBS)


def is_installable_code(rel: str) -> bool:
    """True nur für vom Entwickler gelieferten Programmcode."""
    rel = rel.replace("\\", "/").lstrip("./")
    if not rel or rel.endswith("/"):
        return False
    parts = [part for part in rel.split("/") if part]
    if ".." in parts or skip_export(rel) or rel in protected_download_paths():
        return False
    lowered = [part.lower() for part in parts]
    if any(part in PERSONAL_DIR_NAMES for part in lowered[:-1]):
        return False
    # Konfigurationsbäume, auch wenn darin eine Skriptdatei liegt.
    if rel.startswith("config/") or rel.startswith("homeassistant/config/") or "/config/" in rel:
        return False
    name = lowered[-1]
    if any(marker in name for marker in PERSONAL_NAME_MARKERS):
        return False
    return Path(name).suffix.lower() in CODE_SUFFIXES


def _local_code_is_personal(path: Path, secrets: Iterable[str]) -> bool:
    """Lokale Code-Datei enthält schon persönliche Werte und bleibt deshalb liegen."""
    if not path.is_file():
        return False
    try:
        if path.stat().st_size > 2_000_000:
            return True
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True
    if _TOKEN_RE.search(text) or _GITHUB_TOKEN_RE.search(text):
        return True
    for needle in secrets:
        if not needle or not _scan_needle_usable(str(needle)):
            continue
        if needle in text:
            return True
    return False


def _atomic_copy(source: Path, target: Path) -> None:
    """Temp-Datei im selben Ordner (innerhalb des Mounts), fsync, os.replace.

    gocryptfs benennt atomar um: auf der Karte steht immer die alte oder die neue Datei.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = source.stat().st_mode & 0o777
    if target.exists():
        mode |= target.stat().st_mode & 0o111
    tmp = target.parent / f".{target.name}.upd-{os.getpid()}.tmp"
    try:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write_bytes(target: Path, data: bytes, mode: int = 0o664) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        mode = target.stat().st_mode & 0o777
    tmp = target.parent / f".{target.name}.upd-{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _same_content(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return _sha256_file(left) == _sha256_file(right)
    except OSError:
        return False


def _new_rollback_dir(label: str, dest_root: Optional[Path] = None) -> Path:
    dest_root = dest_root or REPO_ROOT
    base = dest_root / ROLLBACK_DIR_NAME
    stamp = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    path = base / stamp
    counter = 1
    while path.exists():
        counter += 1
        path = base / f"{stamp}-{counter}"
    _assert_repo_target(path, dest_root)
    path.mkdir(parents=True)
    return path


def _prune_rollbacks(dest_root: Optional[Path] = None, keep: int = ROLLBACK_KEEP) -> None:
    base = (dest_root or REPO_ROOT) / ROLLBACK_DIR_NAME
    if not base.is_dir():
        return
    dirs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def _copy_download(snapshot: Path, dest_root: Optional[Path] = None, backup_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Kopiert nur Entwickler-Code atomar in die Klartext-Sicht. Löscht nichts.

    Vorher wird jede ersetzte Datei in backup_dir gesichert (im verschlüsselten Ordner).
    Lokale Code-Dateien mit persönlichen Werten werden ebenfalls aktualisiert und gemeldet.
    """
    dest_root = dest_root or REPO_ROOT
    secrets = collect_sensitive_values(dest_root) if dest_root.is_dir() else set()
    result: Dict[str, Any] = {"copied": 0, "unchanged": 0, "replaced": [], "created": [], "personal_overwritten": []}
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(snapshot).as_posix()
        if not (is_installable_code(rel) or is_setup_asset(rel)):
            continue
        target = dest_root / rel
        _assert_repo_target(target, dest_root)
        if target.is_file() and _same_content(path, target):
            result["unchanged"] += 1
            continue
        if crypt_state(dest_root)[0] == "wait":
            wait_for_crypt("downloading", dest_root)
        if is_installable_code(rel) and _local_code_is_personal(target, secrets):
            result["personal_overwritten"].append(rel)
        if target.exists():
            if backup_dir is not None:
                saved = backup_dir / "files" / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            result["replaced"].append(rel)
        else:
            result["created"].append(rel)
        _atomic_copy(path, target)
        result["copied"] += 1
    if backup_dir is not None:
        (backup_dir / "rollback.json").write_text(
            json.dumps(
                {"replaced": result["replaced"], "created": result["created"], "created_at": time.time()},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return result


def _clear_chromium_fixed_zoom() -> None:
    """Feste Chromium-Host-Zooms entfernen, damit die UI-Skalierung greift."""
    prefs = Path.home() / ".config" / "chromium" / "Default" / "Preferences"
    if not prefs.is_file():
        return
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except Exception:
        return
    partition = data.setdefault("partition", {})
    zoom = partition.get("per_host_zoom_levels")
    if not isinstance(zoom, dict):
        return
    # Leere Host-Overrides → Browser nutzt 100 %, Skalierung macht die Website.
    partition["per_host_zoom_levels"] = {"x": {}}
    try:
        prefs.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def apply_runtime_setup(dest_root: Optional[Path] = None) -> List[str]:
    """Richtet Auto-Start und Hilfsskripte auf dem Ziel-Pi ein (kein Reboot)."""
    dest_root = dest_root or REPO_ROOT
    home = Path.home()
    md = dest_root / "miner-dashboard"
    notes: List[str] = []

    scripts = (
        "start-all-services.sh",
        "open_service_status_terminal.sh",
        "restart_control_services.sh",
        "pi5-boot.sh",
        "start_all.sh",
        "start_all_desktop.sh",
    )
    for name in scripts:
        path = md / name
        if path.is_file():
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except Exception:
                pass

    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    src_start = md / "start-all-services.sh"
    if src_start.is_file():
        dest = bin_dir / "start-all-services.sh"
        shutil.copy2(src_start, dest)
        dest.chmod(dest.stat().st_mode | 0o111)
        notes.append("start-all-services.sh → ~/bin")

    autostart_dir = home / ".config" / "autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    boot = md / "pi5-boot.sh"
    launcher = Path("/usr/local/bin/piminer-boot-launch.sh")
    if launcher.is_file():
        boot = launcher
    if boot != launcher and CRYPT_SERVICE_FILE.is_file():
        # Ohne Launcher würde der Autostart in den verschlüsselten Ordner zeigen, der beim
        # Login noch nicht eingehängt ist. Dann den bestehenden Eintrag lassen.
        notes.append("Autostart unverändert (Launcher fehlt)")
    else:
        desktop_body = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Alles starten (Autostart)\n"
            "Comment=Startet Dienste in Reihenfolge und öffnet das Statusfenster\n"
            f"Exec={boot}\n"
            f"Path={home}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-GNOME-Autostart-Delay=8\n"
        )
        (autostart_dir / "piminer-boot.desktop").write_text(desktop_body, encoding="utf-8")
        notes.append("Autostart-Desktop aktualisiert")

    # Ein Desktop-Start-Button mit einheitlichem Logo (keine Doppel-Icons)
    icon_src = md / "docs" / "icons" / "start-all.png"
    icon_dest_dir = home / ".local" / "share" / "icons"
    icon_dest_dir.mkdir(parents=True, exist_ok=True)
    icon_path = icon_dest_dir / "piminer-start-all.png"
    if icon_src.is_file():
        shutil.copy2(icon_src, icon_path)
    desktop_dir = home / "Desktop"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    alt_dir = desktop_dir / "Alt"
    alt_dir.mkdir(parents=True, exist_ok=True)
    for old in desktop_dir.glob("*.desktop"):
        name = old.name
        if name in {"Alles starten.desktop", "piminer-start-all.desktop"}:
            continue
        try:
            shutil.move(str(old), str(alt_dir / name))
        except Exception:
            pass
    start_desktop = (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=Alles starten\n"
        "Comment=Startet alle Programme in Reihenfolge (ein Button, ein Logo)\n"
        f"Exec={md / 'start-all-services.sh'} desktop\n"
        f"Icon={icon_path if icon_path.is_file() else icon_src}\n"
        "Terminal=false\n"
        "Categories=Network;Settings;\n"
        "StartupNotify=true\n"
    )
    start_path = desktop_dir / "Alles starten.desktop"
    start_path.write_text(start_desktop, encoding="utf-8")
    try:
        start_path.chmod(start_path.stat().st_mode | 0o111)
    except Exception:
        pass
    notes.append("einheitlicher Desktop-Start-Button")

    unit_src = md / "docs" / "systemd-user"
    unit_dest = home / ".config" / "systemd" / "user"
    if unit_src.is_dir():
        unit_dest.mkdir(parents=True, exist_ok=True)
        for pattern in ("*.service", "*.timer"):
            for path in unit_src.glob(pattern):
                shutil.copy2(path, unit_dest / path.name)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        notes.append("systemd-User-Units synchronisiert")

    _clear_chromium_fixed_zoom()
    notes.append("feste Browser-Zooms zurückgesetzt")
    try:
        from bms_seite import load_settings as sync_bms_modbus

        synced = sync_bms_modbus()
        if synced.get("modbus_host"):
            notes.append("Modbus-Host aus Home Assistant übernommen")
    except Exception:
        pass

    return notes


def restart_control_services_soon() -> None:
    """Startet nur die Software-Dienste neu – kein System-Reboot."""

    def _restart() -> None:
        script = REPO_ROOT / "miner-dashboard" / "restart_control_services.sh"
        log_dir = Path(os.environ.get("MINER_LOG_DIR", "/dev/shm/minersteuerung/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "restart_control_services.log"
        try:
            log_file = open(log_path, "a", encoding="utf-8")
        except Exception:
            log_file = subprocess.DEVNULL
        if script.is_file():
            try:
                script.chmod(script.stat().st_mode | 0o111)
            except Exception:
                pass
            # Eigene Sitzung: überlebt das Ende des Upload-Prozesses und startet erst,
            # wenn die Erfolgsmeldung geschrieben ist.
            subprocess.Popen(
                ["bash", "-c", "sleep 2; exec bash \"$1\"", "restart-control", str(script)],
                cwd=str(REPO_ROOT / "miner-dashboard"),
                env={
                    **os.environ,
                    "MINER_RAM_CACHE": "1",
                    "MINER_RAM_ROOT": "/dev/shm/minersteuerung",
                    "MINER_RAM_FLUSH_SEC": "3600",
                    "MINER_LOG_DIR": str(log_dir),
                    "MINER_ROOT": str(REPO_ROOT),
                },
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
            return
        # Fallback: nur Flask, falls Hilfsskript fehlt
        _restart_flask_soon()

    _restart()


def _ensure_soft_download_scan_file(path: Optional[Path] = None) -> None:
    """Nach dem Kit-Copy den Soft-Scan wiederherstellen, falls Upstream den Hard-Scan mitliefert."""
    import inspect

    target = Path(path) if path is not None else Path(__file__)
    if not target.is_file():
        return
    soft = (
        "        # DOWNLOAD_SCAN_SOFT_V1\n"
        "        token_problems = [\n"
        "            p for p in scan_tree(DOWNLOAD_DIR, [])\n"
        "            if is_installable_code(str(p)) or is_setup_asset(str(p))\n"
        "        ]\n"
        "        if token_problems:\n"
        "            raise RuntimeError(\n"
        '                "Download abgebrochen: die öffentliche Version enthält Token "\n'
        '                f"({len(token_problems)} Fundstelle(n), z. B. {token_problems[0]})."\n'
        "            )\n"
    )
    text = target.read_text(encoding="utf-8")
    original = text
    if "DOWNLOAD_SCAN_SOFT_V1" not in text:
        hard = re.compile(
            r"[ \t]*problems = scan_tree\(\s*DOWNLOAD_DIR\s*,\s*collect_sensitive_values\(\s*REPO_ROOT\s*\)\s*\)\s*\n"
            r"[ \t]*if problems:\s*\n"
            r"[ \t]*raise RuntimeError\(\s*\n"
            r"(?:[ \t]*f?\"[^\n]*\"\s*\n)+"
            r"[ \t]*\)\s*\n"
        )
        text, _count = hard.subn(soft, text, count=1)
    call = "_copy_download(DOWNLOAD_DIR)\n        _ensure_soft_download_scan_file()\n"
    if "_copy_download(DOWNLOAD_DIR)\n        _ensure_soft_download_scan_file()" not in text:
        text = text.replace("_copy_download(DOWNLOAD_DIR)\n", call, 1)
    if "def _ensure_soft_download_scan_file(" not in text:
        source = inspect.getsource(_ensure_soft_download_scan_file).rstrip() + "\n\n\n"
        anchor = "def _download_worker("
        if anchor in text:
            text = text.replace(anchor, source + anchor, 1)
    if text != original:
        target.write_text(text, encoding="utf-8")


class _HaLayout(NamedTuple):
    root: Path
    venv_dir: Path
    hass_bin: Path
    config_dir: Path
    tz: str


HA_DOCKER_IMAGE = "ghcr.io/home-assistant/home-assistant"
HA_DOCKER_CONTAINER = "homeassistant"
HA_POLL_URL = "http://127.0.0.1:8123/"
_HA_VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")
_PY_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DOCKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION_OPS = (">=", "<=", "==", "!=", ">", "<")


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _parse_version_tuple(text: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for piece in text.strip().split("."):
        number = ""
        for char in piece:
            if char.isdigit():
                number += char
            else:
                break
        if not number:
            break
        parts.append(int(number))
    return tuple(parts)


def _cmp_version(left: str, right: str) -> int:
    a = _parse_version_tuple(left)
    b = _parse_version_tuple(right)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _python_satisfies(py_version: str, requires: str) -> bool:
    """True only when py_version matches a known Requires-Python specifier."""
    requires = (requires or "").strip()
    if not requires:
        return False
    for raw in requires.split(","):
        part = raw.strip()
        if not part:
            continue
        op = next((item for item in _VERSION_OPS if part.startswith(item)), "")
        if not op:
            return False
        cmp = _cmp_version(py_version, part[len(op):].strip())
        ok = (
            (op == ">=" and cmp >= 0)
            or (op == "<=" and cmp <= 0)
            or (op == ">" and cmp > 0)
            or (op == "<" and cmp < 0)
            or (op == "==" and cmp == 0)
            or (op == "!=" and cmp != 0)
        )
        if not ok:
            return False
    return True


def _hass_ready(path: Path) -> bool:
    """Ausführbares, nicht leeres venv/bin/hass. Fehlende Datei wird nicht angelegt."""
    try:
        return path.is_file() and path.stat().st_size > 0 and os.access(path, os.X_OK)
    except OSError:
        return False


def _resolve_ha_layout() -> _HaLayout:
    """Pfade aus der bestehenden User-Unit, sonst ~/homeassistant."""
    candidates = (
        Path.home() / ".config/systemd/user/home-assistant.service",
        REPO_ROOT / "miner-dashboard/docs/systemd-user/home-assistant.service",
    )
    text = ""
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            break
    work: Optional[Path] = None
    hass: Optional[Path] = None
    config: Optional[Path] = None
    tz = "Europe/Vienna"
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("WorkingDirectory="):
            work = Path(line.split("=", 1)[1].strip())
        elif line.startswith("Environment=") and line.split("=", 1)[1].strip().startswith("TZ="):
            tz = line.split("=", 1)[1].strip().split("=", 1)[1].strip() or tz
        elif line.startswith("ExecStart="):
            parts = line.split("=", 1)[1].strip().split()
            if parts:
                hass = Path(parts[0])
            if "-c" in parts:
                index = parts.index("-c")
                if index + 1 < len(parts):
                    config = Path(parts[index + 1])
    if hass is None:
        root = work or (Path.home() / "homeassistant")
        hass = root / "venv" / "bin" / "hass"
    root = work or hass.parent.parent.parent
    if config is None:
        config = root / "config"
    return _HaLayout(
        root=root,
        venv_dir=hass.parent.parent,
        hass_bin=hass,
        config_dir=config,
        tz=tz,
    )


def _read_ha_version(config_dir: Path) -> str:
    text = (config_dir / ".HA_VERSION").read_text(encoding="utf-8").strip()
    if not _HA_VERSION_RE.fullmatch(text):
        raise RuntimeError("config/.HA_VERSION ist keine Home-Assistant-Version.")
    return text


def _venv_python(venv_dir: Path) -> Optional[Path]:
    for name in ("python3", "python"):
        candidate = venv_dir / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_cmd(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _check_cmd(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    proc = _run_cmd(args, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "Exit " + str(proc.returncode)
        raise RuntimeError(tail[:200])
    return proc


def _venv_python_version(python: Path) -> Optional[str]:
    proc = _run_cmd(
        [str(python), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    if not _PY_VERSION_RE.fullmatch(text):
        return None
    return text


def _requires_python(version: str) -> str:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/homeassistant/{version}/json",
        headers={"User-Agent": "piminer-standard"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    requires = str((payload.get("info") or {}).get("requires_python") or "").strip()
    if not requires:
        raise RuntimeError("Requires-Python fehlt.")
    return requires


def _docker_image_present(image: str) -> bool:
    proc = _run_cmd(["docker", "image", "inspect", "--type", "image", image], timeout=30)
    return proc.returncode == 0


def _container_using_config(config_dir: Path) -> Optional[Tuple[str, bool]]:
    """Name und ob er läuft. Gleicher Config-Mount zählt, damit kein zweiter Container entsteht."""
    proc = _run_cmd(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"], timeout=30)
    if proc.returncode != 0:
        return None
    needle = str(config_dir)
    for line in (proc.stdout or "").splitlines():
        if "\t" not in line:
            continue
        name, status = line.split("\t", 1)
        name = name.strip()
        if not _DOCKER_NAME_RE.fullmatch(name):
            continue
        inspected = _run_cmd(
            ["docker", "inspect", "--format", "{{range .Mounts}}{{.Source}}\n{{end}}", name],
            timeout=30,
        )
        sources = [item.strip() for item in (inspected.stdout or "").splitlines() if item.strip()]
        if needle in sources:
            return name, status.lower().startswith("up")
    return None


def _write_docker_hass_wrapper(layout: _HaLayout, image: str, container: str) -> None:
    """Wrapper, den home-assistant.service als `hass -c CONFIG` ausführt."""
    hass = layout.hass_bin
    config_dir = layout.config_dir.resolve()
    hass.parent.mkdir(parents=True, exist_ok=True)
    destination = hass.resolve() if hass.exists() else hass.parent.resolve() / hass.name
    if destination == config_dir or config_dir in destination.parents:
        raise RuntimeError("hass-Wrapper darf nicht in der HA-Config liegen.")
    lines = [
        "#!/bin/sh",
        "# home-assistant.service startet: hass -c CONFIG",
        "# Nutzt den Container dieser Config und legt keinen zweiten an.",
        "set -eu",
        "DEFAULT_CONFIG=" + _sh_quote(str(layout.config_dir)),
        "NAME=" + _sh_quote(container),
        "IMAGE=" + _sh_quote(image),
        "TZ=" + _sh_quote(layout.tz or "Europe/Vienna"),
        'CONFIG="$DEFAULT_CONFIG"',
        'prev=""',
        'for arg in "$@"; do',
        '  if [ "$prev" = "-c" ] || [ "$prev" = "--config" ]; then',
        '    CONFIG="$arg"',
        "  fi",
        '  prev="$arg"',
        "done",
        'if docker ps -q --filter "name=^/${NAME}$" | grep -q .; then',
        '  exec docker logs -f "$NAME"',
        "fi",
        'if docker ps -aq --filter "name=^/${NAME}$" | grep -q .; then',
        '  docker start "$NAME" >/dev/null',
        '  exec docker logs -f "$NAME"',
        "fi",
        'exec docker run --name "$NAME" --network=host -e "TZ=$TZ" -v "$CONFIG:/config" "$IMAGE"',
        "",
    ]
    hass.write_text("\n".join(lines), encoding="utf-8")
    hass.chmod(hass.stat().st_mode | 0o111)


def _restart_ha_services() -> str:
    proc = _run_cmd(
        ["systemctl", "--user", "restart", "home-assistant.service", "ha-data-bridge.service"],
        timeout=120,
    )
    if proc.returncode == 0:
        return ""
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = detail[-1][:180] if detail else "Exit " + str(proc.returncode)
    return " Dienst-Neustart fehlgeschlagen (" + tail + ")."


def _poll_home_assistant(url: str = HA_POLL_URL, attempts: int = 8) -> bool:
    for index in range(attempts):
        status = 0
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "piminer-standard"})
            with urllib.request.urlopen(request, timeout=3) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except Exception:
            status = 0
        if 200 <= status < 600:
            return True
        if index + 1 < attempts:
            time.sleep(2)
    return False


def _finish_ha_repair() -> str:
    restart_note = _restart_ha_services()
    try:
        up = _poll_home_assistant()
    except Exception:
        up = False
    if up:
        return restart_note + " Port 8123 antwortet."
    return restart_note + " Port 8123 antwortet nicht. Das Update wird nicht abgebrochen."


def _repair_home_assistant() -> str:
    layout = _resolve_ha_layout()
    if layout.hass_bin.is_file() and not os.access(layout.hass_bin, os.X_OK):
        try:
            if layout.hass_bin.stat().st_size > 0:
                layout.hass_bin.chmod(layout.hass_bin.stat().st_mode | 0o111)
        except OSError:
            pass
        if _hass_ready(layout.hass_bin):
            return (
                "Warnung: venv/bin/hass war nicht ausführbar und wurde ausführbar gesetzt."
                + _finish_ha_repair()
            )
    if _hass_ready(layout.hass_bin):
        return "Home Assistant: venv/bin/hass ist ausführbar."
    version = _read_ha_version(layout.config_dir)
    image = HA_DOCKER_IMAGE + ":" + version
    python = _venv_python(layout.venv_dir)
    py_version = _venv_python_version(python) if python is not None else None
    requires = ""
    if py_version:
        try:
            requires = _requires_python(version)
        except Exception:
            requires = ""
    pip_note = ""
    if python is not None and py_version and requires and _python_satisfies(py_version, requires):
        try:
            _check_cmd(
                [str(python), "-m", "pip", "install", "homeassistant==" + version],
                timeout=900,
            )
        except Exception as exc:
            detail = str(exc).strip().splitlines()
            pip_note = (detail[-1] if detail else "pip fehlgeschlagen")[:180]
        if _hass_ready(layout.hass_bin):
            return (
                "Warnung: Home Assistant-Paket fehlte und wurde mit pip installiert (homeassistant=="
                + version
                + ")."
                + _finish_ha_repair()
            )
        if not pip_note:
            pip_note = "hass fehlt nach pip install"
    present = _docker_image_present(image)
    if not present:
        _check_cmd(["docker", "pull", image], timeout=900)
    existing = _container_using_config(layout.config_dir)
    if existing is not None:
        container, running = existing
        if not running:
            _check_cmd(["docker", "start", container], timeout=120)
        image_note = "bestehender Container " + container
    else:
        container = HA_DOCKER_CONTAINER
        _check_cmd(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network=host",
                "-e",
                "TZ=" + (layout.tz or "Europe/Vienna"),
                "-v",
                str(layout.config_dir) + ":/config",
                image,
            ],
            timeout=180,
        )
        if present:
            image_note = "lokales Image " + image
        else:
            image_note = "Image " + image + " wurde gezogen"
    _write_docker_hass_wrapper(layout, image, container)
    if not _hass_ready(layout.hass_bin):
        raise RuntimeError("venv/bin/hass ist nach der Reparatur nicht ausführbar.")
    if pip_note:
        reason = "pip install fehlgeschlagen (" + pip_note + ")"
    elif requires and py_version:
        reason = "Python " + py_version + " erfüllt Requires-Python (" + requires + ") nicht"
    else:
        reason = "Python " + (py_version or "unbekannt") + " erfüllt Requires-Python nicht oder es ist nicht prüfbar"
    return (
        "Warnung: Home Assistant-venv war leer (hass fehlte). "
        + reason
        + ". Docker ("
        + image_note
        + ")."
        + _finish_ha_repair()
    )


def ensure_home_assistant() -> str:
    """Prüft Home Assistant nach dem Code-Apply.

    Update-Button und automatisches Update laufen beide über `_download_worker`.
    Eine fehlende oder leere venv wird nachinstalliert. Die Funktion bricht nie ab;
    der Text wird an job.message angehängt.
    """
    try:
        return _repair_home_assistant()
    except Exception as exc:
        detail = str(exc).strip().splitlines()
        text = (detail[-1] if detail else "unbekannter Fehler").replace("\n", " ")[:200]
        return (
            "Warnung: Home Assistant konnte nicht repariert werden ("
            + text
            + "). Das Update wird nicht abgebrochen."
        )


# --- Systemstartskripte über den Root-Helfer ---


def _system_kit_files(root: Path) -> List[str]:
    base = root / SYSTEM_KIT_PREFIX
    if not base.is_dir():
        return []
    rels: List[str] = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            if _system_target_for_rel(rel):
                rels.append(rel)
    return rels


def _run_system_helper(args: List[str], timeout: int = 600) -> Dict[str, Any]:
    if not SYSTEM_HELPER.is_file():
        return {"error": "Root-Helfer fehlt auf diesem Pi."}
    try:
        proc = subprocess.run(
            ["sudo", "-n", str(SYSTEM_HELPER), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {"error": f"Root-Helfer nicht ausführbar ({exc})."}
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1])
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return {"error": (detail[-1] if detail else f"Exit {proc.returncode}")[:200]}


def apply_system_files(root: Path) -> List[str]:
    """Systemstartskripte aus docs/system/ installieren (Signatur + Prüfsumme prüft der Helfer)."""
    if not _system_kit_files(root):
        return []
    result = _run_system_helper(["release", str(root)])
    notes: List[str] = []
    if result.get("error"):
        notes.append("Systemstartskripte nicht installiert: " + str(result["error"]))
        return notes
    installed = list(result.get("installed") or [])
    kept = list(result.get("kept_old") or [])
    rejected = list(result.get("rejected") or [])
    if installed:
        notes.append(f"Systemstartskripte aktualisiert: {', '.join(installed)}.")
    if kept:
        notes.append(f"Systemstartskripte mit Syntaxfehler, alte Fassung bleibt: {', '.join(kept)}.")
    if rejected:
        notes.append(f"Systemstartskripte abgelehnt (nicht erlaubt oder Prüfsumme falsch): {', '.join(rejected)}.")
    return notes


# --- Admin-Pakete: Tokens, Einstellungen, Systemdateien, Root-Skript ---


def _network_key() -> bytes:
    raw = NETWORK_KEY_FILE.read_text(encoding="ascii").strip()
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("Netzwerkschlüssel hat nicht 32 Bytes.")
    return key


def encrypt_admin_package(inner: bytes, package_id: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = _secrets.token_bytes(12)
    ct = AESGCM(_network_key()).encrypt(nonce, inner, ("piminer-admin:" + package_id).encode("utf-8"))
    envelope = {"v": 1, "alg": "AES-256-GCM", "id": package_id, "nonce": _b64(nonce), "ct": _b64(ct)}
    return (json.dumps(envelope, indent=2) + "\n").encode("utf-8")


def decrypt_admin_package(data: bytes, package_id: str) -> Dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    envelope = json.loads(data.decode("utf-8"))
    nonce = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    inner = AESGCM(_network_key()).decrypt(nonce, ct, ("piminer-admin:" + package_id).encode("utf-8"))
    loaded = json.loads(inner.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Admin-Paket ist kein Objekt.")
    return loaded


def targets_match(targets: Any) -> bool:
    if targets in (None, "", "all", "alle", ["all"]):
        return True
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list):
        return False
    wanted = {str(item).strip().lower() for item in targets if str(item).strip()}
    return bool(wanted & device_names())


def _valid_config_rel(rel: str) -> bool:
    rel = str(rel).replace("\\", "/").strip()
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False
    if not rel.endswith(".json"):
        return False
    if rel.startswith(KEYS_REL + "/") or _is_crypt_internal_rel(rel):
        return False
    if any(part in SKIP_DIR_NAMES for part in rel.split("/")):
        return False
    return True


def _merge_config(current: Any, change: Any) -> Any:
    if isinstance(change, dict) and "__replace__" in change:
        return change["__replace__"]
    if isinstance(current, dict) and isinstance(change, dict):
        merged = dict(current)
        for key, value in change.items():
            merged[key] = _merge_config(current.get(key), value)
        return merged
    return change


def apply_set_config(changes: Dict[str, Any], backup_dir: Optional[Path]) -> List[str]:
    """Werte in JSON-Dateien des Update-Ordners setzen (atomar, landet verschlüsselt auf der Karte)."""
    changed: List[str] = []
    for rel, change in (changes or {}).items():
        if not _valid_config_rel(rel):
            raise RuntimeError(f"Einstellungsdatei nicht erlaubt: {rel}")
        target = REPO_ROOT / rel
        _assert_repo_target(target, REPO_ROOT)
        current: Any = {}
        if target.is_file():
            current = json.loads(target.read_text(encoding="utf-8"))
            if backup_dir is not None:
                saved = backup_dir / "files" / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
        merged = _merge_config(current, change)
        _atomic_write_bytes(target, (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        changed.append(rel)
    return changed


def apply_admin_packages(root: Path) -> List[str]:
    """Neue, signierte Admin-Pakete für diesen Pi anwenden. Nur eine falsche Signatur wird abgelehnt."""
    admin_dir = root / "admin"
    packages = sorted(admin_dir.glob("*.enc")) if admin_dir.is_dir() else []
    if not packages:
        return []
    notes: List[str] = []
    if not NETWORK_KEY_FILE.is_file():
        return ["Admin-Pakete übersprungen: Netzwerkschlüssel fehlt auf diesem Pi."]
    state = load_state()
    applied = list(state.get("applied_admin_ids") or [])
    seen = list(state.get("seen_admin_ids") or [])
    for enc in packages:
        package_id = enc.name[: -len(".enc")]
        if package_id not in seen:
            seen.append(package_id)
            state = load_state()
            state["seen_admin_ids"] = seen[-1000:]
            save_state(state)
        if package_id in applied:
            continue
        try:
            inner = decrypt_admin_package(enc.read_bytes(), package_id)
            payload_bytes = base64.b64decode(inner["payload"])
        except Exception:
            notes.append(f"Admin-Paket {package_id}: nicht entschlüsselbar, übersprungen.")
            continue
        verdict = verify_bytes(payload_bytes, str(inner.get("sig") or ""))
        if verdict != "ok":
            notes.append(f"Admin-Paket {package_id}: Signatur {verdict}, abgelehnt.")
            continue
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            notes.append(f"Admin-Paket {package_id}: Inhalt unlesbar, übersprungen.")
            continue
        if str(payload.get("id") or "") != package_id:
            notes.append(f"Admin-Paket {package_id}: ID passt nicht, abgelehnt.")
            continue
        if not targets_match(payload.get("targets")):
            continue
        done: List[str] = []
        try:
            if payload.get("set_config"):
                wait_for_crypt("downloading")
                backup_dir = _new_rollback_dir("admin-" + package_id)
                changed = apply_set_config(payload["set_config"], backup_dir)
                done.append(f"{len(changed)} Einstellungsdatei(en)")
            if payload.get("files") or payload.get("run_as_root"):
                _ram_dir()
                ADMIN_WORK_DIR.mkdir(parents=True, exist_ok=True)
                os.chmod(ADMIN_WORK_DIR, 0o700)
                work_file = ADMIN_WORK_DIR / f"{package_id}.json"
                fd = os.open(work_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"payload": inner["payload"], "sig": inner["sig"]}, handle)
                try:
                    result = _run_system_helper(["admin", str(work_file)], timeout=900)
                finally:
                    work_file.unlink(missing_ok=True)
                if result.get("error"):
                    done.append("Systemteil nicht ausgeführt: " + str(result["error"]))
                else:
                    if result.get("installed"):
                        done.append(f"Systemdateien: {', '.join(result['installed'])}")
                    if result.get("rejected"):
                        done.append(f"abgelehnt: {', '.join(result['rejected'])}")
                    if "script_exit" in result:
                        done.append(f"Root-Skript Exit {result['script_exit']}")
        except Exception as exc:
            notes.append(f"Admin-Paket {package_id}: Fehler ({exc}).")
            continue
        applied.append(package_id)
        state = load_state()
        state["applied_admin_ids"] = applied[-500:]
        save_state(state)
        notes.append(f"Admin-Paket {package_id} angewendet" + (": " + "; ".join(done) if done else "."))
    return notes


def build_admin_package(
    targets: Any,
    set_config: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Dict[str, Any]]] = None,
    run_as_root: str = "",
    note: str = "",
) -> Tuple[str, bytes]:
    """Paket im RAM bauen, signieren und mit dem Netzwerkschlüssel verschlüsseln."""
    if isinstance(targets, str):
        targets = "all" if targets.strip().lower() in {"", "all", "alle"} else [
            item.strip() for item in re.split(r"[,\s]+", targets) if item.strip()
        ]
    for rel in (set_config or {}):
        if not _valid_config_rel(rel):
            raise RuntimeError(f"Einstellungsdatei nicht erlaubt: {rel}")
    for path, spec in (files or {}).items():
        if not system_target_allowed(path, admin=True):
            raise RuntimeError(f"Systemdatei nicht erlaubt: {path}")
        if not isinstance(spec, dict) or "content_b64" not in spec:
            raise RuntimeError(f"Systemdatei ohne Inhalt: {path}")
    package_id = time.strftime("%Y%m%d-%H%M%S") + "-" + _secrets.token_hex(3)
    payload = {
        "id": package_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "targets": targets,
        "set_config": set_config or {},
        "files": files or {},
        "run_as_root": run_as_root or "",
        "note": note or "",
    }
    payload_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    inner = json.dumps({"payload": _b64(payload_bytes), "sig": sign_bytes(payload_bytes)}).encode("utf-8")
    return package_id, encrypt_admin_package(inner, package_id)


def _admin_push_worker(package_id: str) -> None:
    try:
        _write_job(uploading=True, phase="push", message=f"Admin-Paket {package_id} wird veröffentlicht.")
        ensure_public_repo()
        _sync_publish_repo()
        _copy_admin_store_into(PUBLISH_DIR)
        _push_publish("Verschlüsseltes Admin-Paket")
        _write_job(
            uploading=False,
            phase="done",
            message=(
                f"Admin-Paket {package_id} veröffentlicht. Die Pis übernehmen es beim nächsten "
                "„Update herunterladen“."
            ),
        )
    except Exception as exc:
        _write_job(uploading=False, phase="error", message=f"Admin-Paket nicht veröffentlicht: {exc}")


def start_admin_publish(
    targets: Any,
    set_config: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Dict[str, Any]]] = None,
    run_as_root: str = "",
    note: str = "",
) -> Tuple[bool, str]:
    if not is_publisher():
        return False, "Nur der Pi mit Upload-Button veröffentlicht Admin-Pakete."
    if not (SIGNING_KEY_FILE.is_file() and NETWORK_KEY_FILE.is_file()):
        return False, "Signatur- oder Netzwerkschlüssel fehlt auf diesem Pi."
    if not (PUBLISH_DIR / ".git").is_dir():
        return False, "Erst einmal ein Update hochladen, dann Admin-Pakete senden."
    if not (set_config or files or (run_as_root or "").strip()):
        return False, "Das Paket ist leer."
    with _job_lock:
        job = _job()
        if job.get("uploading") or job.get("downloading"):
            return False, "Es läuft bereits ein Vorgang."
        try:
            _github_auth()
            package_id, data = build_admin_package(targets, set_config, files, run_as_root, note)
        except Exception as exc:
            return False, str(exc)
        ADMIN_STORE_DIR.mkdir(parents=True, exist_ok=True)
        (ADMIN_STORE_DIR / f"{package_id}.enc").write_bytes(data)
        _write_job(
            uploading=True,
            downloading=False,
            phase="start",
            message="Admin-Paket wird veröffentlicht.",
            pid=os.getpid(),
            notes=[],
            skipped_files=[],
            syntax_errors=[],
        )
    threading.Thread(target=_admin_push_worker, args=(package_id,), daemon=True).start()
    return True, f"Admin-Paket {package_id} wird veröffentlicht."


# --- Nachprüfung nach dem Neustart: nur zurückholen, wenn das Dashboard nicht startet ---

_HEALTH_WATCH_SCRIPT = r'''
import json, os, shutil, subprocess, sys, time
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repo = Path(plan["repo_root"])
backup = Path(plan["backup_dir"])
backend = plan["backend_dir"]


def running():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = [a.decode("utf-8", "ignore") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
        except Exception:
            continue
        if len(args) < 2 or "python" not in os.path.basename(args[0]):
            continue
        if args[-1] != "app.py" and not args[-1].endswith("/backend/app.py"):
            continue
        try:
            if os.readlink(f"/proc/{pid}/cwd") != backend and not args[-1].startswith(backend):
                continue
        except Exception:
            pass
        return True
    return False


def crypt_ok():
    cipher = repo.parent / f".{repo.name}.crypt"
    return not cipher.exists() or os.path.ismount(repo)


def write_job(**fields):
    path = Path(plan["job_file"])
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        job = {}
    job.update(fields)
    path.write_text(json.dumps(job, indent=2), encoding="utf-8")


time.sleep(float(plan.get("initial_sleep", 20)))
deadline = time.time() + float(plan.get("wait_sec", 90))
stable = 0
while time.time() < deadline:
    stable = stable + 1 if running() else 0
    if stable >= 3:
        sys.exit(0)
    time.sleep(5)

for _ in range(120):
    if crypt_ok():
        break
    time.sleep(5)
else:
    sys.exit(1)

meta = json.loads((backup / "rollback.json").read_text(encoding="utf-8"))
for rel in meta.get("replaced", []):
    src = backup / "files" / rel
    dst = repo / rel
    if not src.is_file():
        continue
    tmp = dst.parent / f".{dst.name}.rollback.tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
for rel in meta.get("created", []):
    try:
        (repo / rel).unlink()
    except OSError:
        pass
try:
    state_path = Path(plan["state_file"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["local_version"] = int(plan.get("previous_version") or 0)
    state["rolled_back_version"] = int(plan.get("version") or 0)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
except Exception:
    pass
write_job(
    downloading=False,
    uploading=False,
    phase="error",
    message=(
        "Update Version %s zurückgenommen: Das Dashboard ist danach nicht gestartet. "
        "Die vorherige Fassung ist wiederhergestellt." % plan.get("version")
    ),
)
env = dict(os.environ)
env.update(plan.get("env", {}))
subprocess.Popen(["bash", plan["restart_script"]], cwd=str(repo / "miner-dashboard"), env=env,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
'''


def _start_health_watch(backup_dir: Path, version: int, previous_version: int) -> None:
    _ram_dir()
    script = RAM_DIR / "health_watch.py"
    plan_path = RAM_DIR / "health_watch.json"
    script.write_text(_HEALTH_WATCH_SCRIPT, encoding="utf-8")
    log_dir = Path(os.environ.get("MINER_LOG_DIR", "/dev/shm/minersteuerung/logs"))
    plan = {
        "repo_root": str(REPO_ROOT),
        "backend_dir": str(BACKEND_DIR),
        "backup_dir": str(backup_dir),
        "job_file": str(JOB_FILE),
        "state_file": str(STATE_FILE),
        "restart_script": str(REPO_ROOT / "miner-dashboard" / "restart_control_services.sh"),
        "wait_sec": HEALTH_WAIT_SEC,
        "version": int(version),
        "previous_version": int(previous_version),
        "env": {
            "MINER_RAM_CACHE": "1",
            "MINER_RAM_ROOT": "/dev/shm/minersteuerung",
            "MINER_RAM_FLUSH_SEC": "3600",
            "MINER_LOG_DIR": str(log_dir),
            "MINER_ROOT": str(REPO_ROOT),
        },
    }
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    subprocess.Popen(
        [python, str(script), str(plan_path)],
        cwd=str(RAM_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _download_worker() -> None:
    notes: List[str] = []
    try:
        _ram_dir()
        _cleanup_legacy_workdirs()
        _write_job(downloading=True, phase="fetch", message="Öffentliche Version wird geladen (nur im RAM).")
        _clone_release(DOWNLOAD_DIR, "downloading")
        report = validate_release(DOWNLOAD_DIR, upload=False)
        if report["hash_mismatch"]:
            _clone_release(DOWNLOAD_DIR, "downloading")
            report = validate_release(DOWNLOAD_DIR, upload=False)
        state = load_state()
        previous_version = int(state.get("local_version") or 0)
        version = int(report.get("version") or 0)
        if version <= 0:
            remote = fetch_remote_version()
            version = int(remote) if remote else previous_version + 1
        # DOWNLOAD_SCAN_SOFT_V1: Token-Funde, Syntax, Prüfsummen und Format sind nur Hinweise.
        notes.extend(report_notes(report))
        crypt_message = wait_for_crypt("downloading")
        if crypt_state()[0] == "plain":
            notes.append(crypt_message)
        _write_job(phase="backup", message=f"Version {version}: Sicherung im verschlüsselten Ordner.")
        backup_dir = _new_rollback_dir(f"v{version}")
        _write_job(phase="apply", message=f"Version {version}: Programmcode wird übernommen.")
        result = _copy_download(DOWNLOAD_DIR, backup_dir=backup_dir)
        if result["personal_overwritten"]:
            notes.append(
                f"{len(result['personal_overwritten'])} lokale Code-Datei(en) mit persönlichen Werten aktualisiert, "
                f"alte Fassung gesichert: {', '.join(result['personal_overwritten'][:5])}."
            )
        if report.get("signature") == "ok":
            _write_job(phase="system", message="Systemstartskripte werden geprüft.")
            notes.extend(apply_system_files(DOWNLOAD_DIR))
        _write_job(phase="admin", message="Admin-Pakete werden geprüft.")
        notes.extend(apply_admin_packages(DOWNLOAD_DIR))
        _write_job(phase="setup", message="Home Assistant wird geprüft.")
        ha_note = ensure_home_assistant()
        _write_job(phase="setup", message="Auto-Start und Anzeige werden eingerichtet.")
        setup_notes = apply_runtime_setup(REPO_ROOT)
        state = load_state()
        state["local_version"] = version
        state["last_installed_version"] = version
        state["last_release_notes"] = str(report.get("notes") or "")
        state["news_pending"] = False
        state["last_rollback_dir"] = str(backup_dir)
        state.pop("rolled_back_version", None)
        save_state(state)
        _prune_rollbacks()
        note = (", ".join(setup_notes) if setup_notes else "Setup ok")
        _write_job(
            downloading=False,
            phase="done",
            message=(
                f"Version {version} geladen: {result['copied']} Datei(en) aktualisiert, "
                f"{result['unchanged']} unverändert. Auto-Start eingerichtet ({note}). "
                "Software-Dienste werden neu gestartet (kein Reboot). "
                "Persönliche Daten unverändert. "
            )
            + ha_note,
            notes=notes,
        )
        if result["copied"]:
            _start_health_watch(backup_dir, version, previous_version)
        restart_control_services_soon()
    except Exception as exc:
        _write_job(downloading=False, phase="error", message=str(exc) or "Download fehlgeschlagen.", notes=notes)
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        shutil.rmtree(ADMIN_WORK_DIR, ignore_errors=True)


def _restart_flask_soon() -> None:
    def _restart() -> None:
        python = REPO_ROOT / "venv" / "bin" / "python"
        if not python.is_file():
            python = Path("python3")
        subprocess.run(["pkill", "-f", f"{python} app.py"], check=False)
        log_dir = Path(os.environ.get("MINER_LOG_DIR", "/dev/shm/minersteuerung/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "Flask_Backend.log", "a", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "MINER_RAM_CACHE": "1",
                "MINER_RAM_ROOT": "/dev/shm/minersteuerung",
                "MINER_RAM_FLUSH_SEC": "3600",
                "MINER_LOG_DIR": str(log_dir),
                "MINER_ROOT": str(REPO_ROOT),
            }
        )
        subprocess.Popen(
            [str(python), "app.py"],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    threading.Timer(1.5, _restart).start()


def auto_update_active() -> bool:
    """Auto-Update ist auf Download-Pis nach jedem Einschalten an. Aus gilt nur bis zum Neustart."""
    return not is_publisher() and not AUTO_UPDATE_OFF_FLAG.exists()


def set_auto_update(enabled: bool) -> Tuple[bool, str]:
    if is_publisher():
        return False, "Der Pi mit Upload-Button lädt keine Updates automatisch."
    _ram_dir()
    if enabled:
        AUTO_UPDATE_OFF_FLAG.unlink(missing_ok=True)
        return True, "Auto-Update ist an."
    AUTO_UPDATE_OFF_FLAG.write_text("off\n", encoding="utf-8")
    return True, "Auto-Update ist bis zum nächsten Einschalten aus."


def fetch_admin_index() -> Optional[List[str]]:
    request = urllib.request.Request(RAW_ADMIN_INDEX_URL, headers={"User-Agent": "piminer-standard"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [str(item) for item in (payload.get("ids") or [])]
    except urllib.error.HTTPError as exc:
        return [] if exc.code == 404 else None
    except Exception:
        return None


def has_new_admin_packages(ids: Optional[List[str]]) -> bool:
    if not ids:
        return False
    seen = set(load_state().get("seen_admin_ids") or [])
    return any(item not in seen for item in ids)


def pull_if_newer() -> bool:
    """Nur laden, wenn im Web eine höhere Version liegt. Kein erneutes Laden derselben Version."""
    if not auto_update_active():
        return False
    job = _job()
    if job.get("uploading") or job.get("downloading"):
        return False
    remote = fetch_remote_version()
    local = int(load_state().get("local_version") or 0)
    if remote is None or int(remote) <= local:
        return False
    ok, _message = start_download()
    return bool(ok)


def auto_update_step(
    remote: Optional[int],
    local: int,
    busy: bool,
    seen_version: Optional[int],
    seen_at: Optional[float],
    now: float,
    wait_sec: float = AUTO_UPDATE_WAIT_SEC,
) -> Tuple[str, Optional[int], Optional[float]]:
    """Neue Web-Version merken, etwa eine Minute warten, dann laden."""
    if busy or remote is None or int(remote) <= int(local):
        return "idle", None, None
    version = int(remote)
    if seen_version != version or seen_at is None:
        return "wait", version, now
    if now - float(seen_at) >= float(wait_sec):
        return "download", version, seen_at
    return "wait", version, seen_at


def _auto_update_loop() -> None:
    seen_version: Optional[int] = None
    seen_at: Optional[float] = None
    last_admin_check = 0.0
    while True:
        try:
            if auto_update_active():
                job = _job()
                busy = bool(job.get("uploading") or job.get("downloading"))
                remote = fetch_remote_version()
                if remote is not None and int(remote) == int(load_state().get("rolled_back_version") or 0):
                    remote = None
                action, seen_version, seen_at = auto_update_step(
                    remote,
                    int(load_state().get("local_version") or 0),
                    busy,
                    seen_version,
                    seen_at,
                    time.time(),
                )
                if action == "download":
                    pull_if_newer()
                    seen_at = time.time()
                elif action == "idle" and not busy and time.time() - last_admin_check >= AUTO_ADMIN_CHECK_SEC:
                    last_admin_check = time.time()
                    if has_new_admin_packages(fetch_admin_index()):
                        start_download()
        except Exception:
            pass
        time.sleep(AUTO_UPDATE_POLL_SEC)


def start_auto_update() -> None:
    """Startet die Überwachung einmal pro Prozess; ob geladen wird, entscheidet auto_update_active()."""
    global _auto_update_started
    if _auto_update_started:
        return
    _auto_update_started = True
    threading.Thread(target=_auto_update_loop, name="public-update-watch", daemon=True).start()
    print("Automatische Update-Erkennung läuft.", flush=True)


def start_download() -> Tuple[bool, str]:
    with _job_lock:
        job = _job()
        if job.get("uploading") or job.get("downloading"):
            return False, "Es läuft bereits ein Update."
        _write_job(
            downloading=True,
            uploading=False,
            phase="start",
            message="Download gestartet.",
            pid=os.getpid(),
            notes=[],
            skipped_files=[],
            syntax_errors=[],
        )
    threading.Thread(target=_download_worker, daemon=True).start()
    return True, "Download gestartet."
