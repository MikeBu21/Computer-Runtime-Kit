#!/usr/bin/env python3
"""Login der Datenbrücke an diesen Home Assistant binden.

Läuft vor dem Start von Home Assistant (ExecStartPre). Passt der Token in
~/.config/ha-bridge/credentials.json nicht zum Login-Speicher dieses Home Assistant
(z. B. frisch geflashtes Image), wird für den Besitzer ein neuer Token angelegt und
in beide Dateien geschrieben. Jeder Pi bekommt so seinen eigenen Token.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

CONFIG_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/piminer/homeassistant/config")
AUTH_PATH = CONFIG_DIR / ".storage" / "auth"
VERSION_PATH = CONFIG_DIR / ".HA_VERSION"
CREDENTIALS_PATH = Path.home() / ".config/ha-bridge/credentials.json"
DEFAULT_URL = "http://127.0.0.1:8123"
DEFAULT_CLIENT_ID = "http://127.0.0.1:8123/"
REFRESH_TOKEN_EXPIRATION = 90 * 24 * 3600


def log(message: str) -> None:
    print(f"[ha-bridge-token] {message}", flush=True)


def ha_running(url: str) -> bool:
    host_port = url.split("://", 1)[-1].split("/", 1)[0]
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 8123)), timeout=1):
            return True
    except OSError:
        return False


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def token_is_valid(auth_data: dict, token: str, client_id: str) -> bool:
    users = {user.get("id"): user for user in auth_data.get("users", [])}
    now = time.time()
    for record in auth_data.get("refresh_tokens", []):
        if record.get("token") != token or record.get("client_id") != client_id:
            continue
        user = users.get(record.get("user_id")) or {}
        if not user.get("is_active", True):
            return False
        expire_at = record.get("expire_at")
        return expire_at is None or expire_at > now + 60
    return False


def pick_user(auth_data: dict) -> dict | None:
    candidates = [
        user
        for user in auth_data.get("users", [])
        if user.get("is_active", True) and not user.get("system_generated")
    ]
    candidates.sort(key=lambda user: not user.get("is_owner"))
    return candidates[0] if candidates else None


def main() -> int:
    if not AUTH_PATH.is_file():
        log(f"{AUTH_PATH} fehlt, Home Assistant ist noch nicht eingerichtet")
        return 0

    try:
        creds = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        creds = {}
    ha_url = str(creds.get("ha_url") or DEFAULT_URL)
    client_id = str(creds.get("client_id") or DEFAULT_CLIENT_ID)
    token = str(creds.get("refresh_token") or "")

    auth = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    auth_data = auth.setdefault("data", {})
    if token and token_is_valid(auth_data, token, client_id):
        log("Token passt zu diesem Home Assistant")
        return 0

    if ha_running(ha_url):
        # Home Assistant überschreibt den Login-Speicher beim Beenden.
        log("Home Assistant läuft bereits, Token wird beim nächsten Start angelegt")
        return 0

    user = pick_user(auth_data)
    if user is None:
        log("kein aktiver Benutzer im Login-Speicher, nichts angelegt")
        return 1

    credential_id = next(
        (
            cred.get("id")
            for cred in auth_data.get("credentials", [])
            if cred.get("user_id") == user.get("id")
        ),
        None,
    )
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        version = None

    new_token = secrets.token_hex(64)
    record = {
        "id": uuid.uuid4().hex,
        "user_id": user["id"],
        "client_id": client_id,
        "client_name": None,
        "client_icon": None,
        "token_type": "normal",
        "created_at": datetime.now(UTC).isoformat(),
        "access_token_expiration": 1800.0,
        "token": new_token,
        "jwt_key": secrets.token_hex(64),
        "last_used_at": None,
        "last_used_ip": None,
        "expire_at": time.time() + REFRESH_TOKEN_EXPIRATION,
        "credential_id": credential_id,
        "version": version,
    }
    auth_data.setdefault("refresh_tokens", []).append(record)
    write_json(AUTH_PATH, auth)
    write_json(
        CREDENTIALS_PATH,
        {"ha_url": ha_url, "refresh_token": new_token, "client_id": client_id},
    )
    log(f"neuer Token für Benutzer {user.get('name') or user['id']} angelegt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
