"""Start/status helpers for Miner-Steuerung, Home Assistant and Tailscale."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.error
import urllib.request

TAILSCALE_START = os.path.expanduser("~/bin/tailscale-start.sh")


def _run(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except Exception as exc:
        return False, str(exc)


def get_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _systemd_user_active(unit: str) -> bool:
    ok, out = _run(["systemctl", "--user", "is-active", unit], timeout=10)
    return ok and out.strip() == "active"


def _systemd_system_active(unit: str) -> bool:
    ok, out = _run(["systemctl", "is-active", unit], timeout=10)
    return ok and out.strip() == "active"


def miner_status() -> dict:
    active = _systemd_user_active("miner-dashboard.service")
    reachable = _http_ok("http://127.0.0.1:5000/")
    return {
        "id": "miner",
        "label": "Miner-Steuerung",
        "active": active,
        "reachable": reachable,
        "detail": "Läuft" if reachable else ("Dienst aktiv" if active else "Gestoppt"),
        "url": f"http://{get_lan_ip()}:5000",
    }


def homeassistant_status() -> dict:
    active = _systemd_user_active("home-assistant.service")
    bridge = _systemd_user_active("ha-data-bridge.service")
    reachable = _http_ok("http://127.0.0.1:8123/")
    detail = "Läuft" if reachable else ("Dienst aktiv" if active else "Gestoppt")
    if active and not bridge:
        detail += " (HA-Bridge aus)"
    return {
        "id": "homeassistant",
        "label": "Home Assistant",
        "active": active,
        "reachable": reachable,
        "detail": detail,
        "url": "http://127.0.0.1:8123",
    }


def tailscale_status() -> dict:
    daemon = _systemd_system_active("tailscaled")
    ok, out = _run(["tailscale", "status", "--json"], timeout=15)
    connected = False
    ip = ""
    if ok and out:
        try:
            data = json.loads(out)
            connected = bool(data.get("BackendState") == "Running" and data.get("Self", {}).get("Online"))
            ips = data.get("Self", {}).get("TailscaleIPs") or []
            ip = ips[0] if ips else ""
        except json.JSONDecodeError:
            connected = "100." in out
    detail = "Verbunden" if connected else ("Daemon läuft" if daemon else "Gestoppt")
    if ip:
        detail += f" ({ip})"
    return {
        "id": "tailscale",
        "label": "Tailscale",
        "active": daemon,
        "reachable": connected,
        "detail": detail,
        "url": "https://login.tailscale.com/admin/machines",
    }


def all_status() -> list[dict]:
    return [miner_status(), homeassistant_status(), tailscale_status()]


def start_miner() -> tuple[bool, str]:
    ok, out = _run(["systemctl", "--user", "start", "miner-dashboard.service"])
    if ok:
        return True, "Miner-Steuerung gestartet."
    return False, out or "Miner-Steuerung konnte nicht gestartet werden."


def start_homeassistant() -> tuple[bool, str]:
    ok, out = _run(
        [
            "systemctl",
            "--user",
            "start",
            "home-assistant.service",
            "ha-data-bridge.service",
        ]
    )
    if ok:
        return True, "Home Assistant und HA-Bridge gestartet."
    return False, out or "Home Assistant konnte nicht gestartet werden."


def start_tailscale() -> tuple[bool, str]:
    if os.path.isfile(TAILSCALE_START) and os.access(TAILSCALE_START, os.X_OK):
        try:
            subprocess.Popen(
                [TAILSCALE_START],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, "Tailscale-Start ausgeführt."
        except OSError as exc:
            return False, str(exc)

    ok, out = _run(["systemctl", "start", "tailscaled"])
    if not ok:
        return False, out or "tailscaled konnte nicht gestartet werden."
    ok, out = _run(["tailscale", "up"], timeout=120)
    if ok:
        return True, "Tailscale verbunden."
    return False, out or "tailscale up fehlgeschlagen (ggf. Anmeldung nötig)."


def start_all() -> list[dict]:
    script = os.path.expanduser("~/bin/start-all-services.sh")
    if os.path.isfile(script) and os.access(script, os.X_OK):
        try:
            subprocess.Popen(
                [script, "desktop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return [
                {"id": "miner", "ok": True, "message": "Start-Skript ausgeführt."},
                {"id": "homeassistant", "ok": True, "message": "Start-Skript ausgeführt."},
                {"id": "tailscale", "ok": True, "message": "Start-Skript ausgeführt."},
            ]
        except OSError as exc:
            return [{"id": "all", "ok": False, "message": str(exc)}]

    results = []
    for name, starter in (
        ("miner", start_miner),
        ("homeassistant", start_homeassistant),
        ("tailscale", start_tailscale),
    ):
        ok, message = starter()
        results.append({"id": name, "ok": ok, "message": message})
    return results


def schedule_automation_services_restart(*, include_flask: bool = True, delay_seconds: float = 1.5) -> tuple[bool, str]:
    """
    Startet Energiemonitor (optional Flask) im Hintergrund neu – ohne lxterminal.

    Hinweis: Beim Speichern von Automatik-Regeln nicht nötig (Config wird live gelesen).
    """
    script = os.path.expanduser("~/minersteuerung/miner-dashboard/restart_automation_services.sh")
    if not os.path.isfile(script):
        return False, f"Neustart-Skript fehlt: {script}"
    try:
        subprocess.Popen(
            ["bash", script, str(delay_seconds), "1" if include_flask else "0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        parts = ["Energiemonitor"]
        if include_flask:
            parts.insert(0, "Flask Backend")
        return True, f"{' und '.join(parts)} werden im Hintergrund neu gestartet …"
    except OSError as exc:
        return False, str(exc)


def schedule_miner_desktop_restart(delay_seconds: float = 0.5) -> tuple[bool, str]:
    """Startet alle Miner-Terminals neu (wie Dienste-Seite)."""
    script = os.path.expanduser("~/minersteuerung/miner-dashboard/start_all_desktop.sh")
    if not os.path.isfile(script):
        return False, f"Start-Skript fehlt: {script}"
    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep {delay_seconds}; OPEN_MINER_BROWSER=0 bash {script!r}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, "Miner-Dienste werden neu gestartet …"
    except OSError as exc:
        return False, str(exc)
