# -*- coding: utf-8 -*-
"""Öffentliche Standardversion veröffentlichen und herunterladen.

Persönliche Werte bleiben auf diesem Pi. Das öffentliche Repo enthält nur
Beispielgeräte und die Standardeinstellungen aus dem Programm.
Der Upload-Button existiert nur auf diesem Pi und nur, solange keine neuere
öffentliche Version vorliegt und kein Upload läuft.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent.parent
OWNER = "MikeBu21"
REPO_NAME = "Computer-Runtime-Kit"
REPO_URL = f"https://github.com/{OWNER}/{REPO_NAME}.git"
RAW_RELEASE_URL = (
    f"https://raw.githubusercontent.com/{OWNER}/{REPO_NAME}/main/standard_release.json"
)
BRANCH = "main"

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
DOWNLOAD_DIR = STATE_DIR / "download"

EXAMPLE_IP = "192.0.2.10"
_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
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
SKIP_DIR_NAMES = {".storage", "__pycache__", ".git"}
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
    "ip_recovery_notifications.json",
    "daten.json",
}
SKIP_PREFIXES = (
    "scripts/tailscale_notify/state/",
    "homeassistant/config/.storage/",
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
    """Button darf nicht existieren, wenn eine neuere Version da ist oder ein Lauf aktiv ist."""
    if not is_publisher or uploading or downloading:
        return False
    if remote_version is None:
        return False
    return int(remote_version) <= int(local_version)


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


def fetch_remote_version() -> Optional[int]:
    """Öffentliche Versionsnummer. 0 = noch kein Release. None = nicht prüfbar."""
    request = urllib.request.Request(RAW_RELEASE_URL, headers={"User-Agent": "piminer-standard"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return int(payload.get("version") or 0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 0
        return None
    except Exception:
        return None


def dashboard_status() -> Dict[str, Any]:
    state = load_state()
    job = _job()
    local_version = int(state.get("local_version") or 0)
    remote_version = fetch_remote_version()
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
    elif remote_version is None:
        message = "Öffentliche Version gerade nicht prüfbar. Upload ist ausgeblendet."
    elif not is_publisher():
        message = "Dieser Pi veröffentlicht keine Updates."
    elif remote_version > local_version:
        message = (
            f"Öffentlich ist Version {remote_version} verfügbar. "
            "Der Upload-Button ist ausgeblendet, bis diese Version geladen ist."
        )
    else:
        message = f"Lokal Version {local_version}. Öffentlich Version {remote_version}."
    progress_map = {
        "start": 8,
        "export": 28,
        "scan": 48,
        "push": 78,
        "fetch": 35,
        "apply": 78,
        "setup": 88,
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
    return {
        "upload_allowed": allowed,
        "uploading": uploading,
        "downloading": downloading,
        "local_version": local_version,
        "remote_version": remote_version,
        "pending_updates": pending_updates,
        "news_count": news_badge_count(state),
        "phase": phase,
        "progress": progress,
        "message": message,
        "repo_url": f"https://github.com/{OWNER}/{REPO_NAME}",
    }


def news_badge_count(state: Optional[Dict[str, Any]] = None) -> int:
    """1 = ungesehene Neuigkeiten nach einer Installation, sonst 0."""
    state = state if state is not None else load_state()
    return 1 if bool(state.get("news_pending")) else 0


def mark_news_seen() -> Dict[str, Any]:
    state = load_state()
    state["news_pending"] = False
    state["news_seen_version"] = int(state.get("local_version") or 0)
    save_state(state)
    return state


def news_payload() -> Dict[str, Any]:
    state = load_state()
    return {
        "news_count": news_badge_count(state),
        "news_pending": bool(state.get("news_pending")),
        "local_version": int(state.get("local_version") or 0),
        "last_installed_version": int(state.get("last_installed_version") or state.get("local_version") or 0),
        "last_release_notes": str(state.get("last_release_notes") or ""),
        "items": _default_news_items(state),
    }


def _default_news_items(state: Dict[str, Any]) -> List[Dict[str, str]]:
    version = int(state.get("last_installed_version") or state.get("local_version") or 0)
    notes = str(state.get("last_release_notes") or "").strip()
    items = [
        {
            "title": f"Version {version} installiert" if version else "Willkommen",
            "body": notes
            or "Die Programmoberfläche wurde aktualisiert. Neue Funktionen findest du unter Einstellungen.",
        },
        {
            "title": "Update-Hinweis",
            "body": "Wenn ein neues Update bereitsteht, erscheint ein grüner Zähler bei Updates.",
        },
    ]
    return items


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


def export_snapshot(dest: Path, version: int, repo: Optional[Path] = None) -> None:
    repo = repo or REPO_ROOT
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel in project_files(repo):
        if skip_export(rel):
            continue
        source = repo / rel
        if not source.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    overlay = standard_overlay()
    for rel, content in overlay.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    release = {
        "version": int(version),
        "channel": "standard",
        "notes": (
            "Update-Badge mit Zähler, Neuigkeiten-Badge nach Installation, "
            "Tesla-ähnliche Seitenleiste. Standardwerte ohne persönliche Daten."
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
    env["GIT_AUTHOR_NAME"] = "piMiner Standard"
    env["GIT_AUTHOR_EMAIL"] = "piminer@users.noreply.github.com"
    env["GIT_COMMITTER_NAME"] = "piMiner Standard"
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


def _mirror_into_publish(snapshot: Path) -> None:
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    if not (PUBLISH_DIR / ".git").is_dir():
        _run_git(["init", "-b", BRANCH], PUBLISH_DIR)
        _run_git(["remote", "add", "origin", REPO_URL], PUBLISH_DIR)
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


def _push_publish() -> None:
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
        _run_git(["commit", "-m", "Standardupdate ohne persönliche Daten"], PUBLISH_DIR)
    _run_git(["push", "-u", "origin", f"HEAD:{BRANCH}"], PUBLISH_DIR)


def _upload_worker(version: int) -> None:
    snapshot = STATE_DIR / "snapshot"
    try:
        _write_job(uploading=True, phase="export", message="Standardversion wird zusammengestellt.")
        export_snapshot(snapshot, version)
        _write_job(phase="scan", message="Prüfung auf persönliche Daten.")
        problems = scan_tree(snapshot, collect_sensitive_values(REPO_ROOT))
        if problems:
            raise RuntimeError("Upload abgebrochen: persönliche Daten wären enthalten.")
        _write_job(phase="push", message="Öffentliches Repo wird aktualisiert.")
        ensure_public_repo()
        _mirror_into_publish(snapshot)
        _push_publish()
        state = load_state()
        state["local_version"] = int(version)
        save_state(state)
        _write_job(
            uploading=False,
            phase="done",
            message=f"Version {version} ist öffentlich. Nur Standardwerte, ohne persönliche Daten.",
        )
    except Exception as exc:
        _write_job(uploading=False, phase="error", message=str(exc) or "Upload fehlgeschlagen.")
    finally:
        if snapshot.exists():
            shutil.rmtree(snapshot, ignore_errors=True)


def start_upload() -> Tuple[bool, str]:
    with _job_lock:
        status = dashboard_status()
        if not status["upload_allowed"]:
            return False, status["message"] or "Upload ist nicht verfügbar."
        remote = status["remote_version"]
        if remote is None:
            return False, "Öffentliche Version nicht prüfbar."
        version = max(int(remote), int(status["local_version"])) + 1
        _write_job(
            uploading=True,
            downloading=False,
            phase="start",
            message="Upload gestartet.",
            pid=os.getpid(),
        )
    threading.Thread(target=_upload_worker, args=(version,), daemon=True).start()
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
    return False


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


def _copy_download(snapshot: Path, dest_root: Optional[Path] = None) -> int:
    """Kopiert nur Entwickler-Code. Legt keine Daten an und löscht nichts."""
    dest_root = dest_root or REPO_ROOT
    secrets = collect_sensitive_values(dest_root) if dest_root.is_dir() else set()
    copied = 0
    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(snapshot).as_posix()
        if not (is_installable_code(rel) or is_setup_asset(rel)):
            continue
        target = dest_root / rel
        if is_installable_code(rel) and _local_code_is_personal(target, secrets):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


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
    desktop_body = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Alles starten (Autostart)\n"
        "Comment=Startet Dienste in Reihenfolge und öffnet das Statusfenster\n"
        f"Exec={boot}\n"
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
            subprocess.Popen(
                ["bash", str(script)],
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

    threading.Timer(2.0, _restart).start()


def _download_worker() -> None:
    try:
        _write_job(downloading=True, phase="fetch", message="Öffentliche Version wird geladen.")
        if DOWNLOAD_DIR.exists():
            shutil.rmtree(DOWNLOAD_DIR)
        _run_git(["clone", "--depth", "1", "--branch", BRANCH, REPO_URL, str(DOWNLOAD_DIR)], STATE_DIR)
        # git clone legt .git an — gehört nicht zur öffentlichen Version und darf den Scan nicht stören.
        git_meta = DOWNLOAD_DIR / ".git"
        if git_meta.exists():
            shutil.rmtree(git_meta, ignore_errors=True)
        release_path = DOWNLOAD_DIR / "standard_release.json"
        if not release_path.is_file():
            raise RuntimeError("Öffentliche Version hat keine standard_release.json.")
        release = json.loads(release_path.read_text(encoding="utf-8"))
        version = int(release.get("version") or 0)
        problems = scan_tree(DOWNLOAD_DIR, collect_sensitive_values(REPO_ROOT))
        if problems:
            raise RuntimeError(
                "Download abgebrochen: die öffentliche Version enthält persönliche Daten "
                f"({len(problems)} Fundstelle(n), z. B. {problems[0]})."
            )
        _write_job(phase="apply", message=f"Version {version}: nur Programmcode wird übernommen.")
        _copy_download(DOWNLOAD_DIR)
        _write_job(phase="setup", message="Auto-Start und Anzeige werden eingerichtet.")
        setup_notes = apply_runtime_setup(REPO_ROOT)
        state = load_state()
        state["local_version"] = version
        state["last_installed_version"] = version
        state["last_release_notes"] = str(release.get("notes") or "")
        state["news_pending"] = True
        save_state(state)
        note = (", ".join(setup_notes) if setup_notes else "Setup ok")
        _write_job(
            downloading=False,
            phase="done",
            message=(
                f"Version {version} geladen. Code aktualisiert, Auto-Start eingerichtet ({note}). "
                "Software-Dienste werden neu gestartet (kein Reboot). "
                "Persönliche Daten unverändert."
            ),
        )
        restart_control_services_soon()
    except Exception as exc:
        _write_job(downloading=False, phase="error", message=str(exc) or "Download fehlgeschlagen.")


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
        )
    threading.Thread(target=_download_worker, daemon=True).start()
    return True, "Download gestartet."
