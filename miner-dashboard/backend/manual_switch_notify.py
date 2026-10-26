# -*- coding: utf-8 -*-
"""Broadcast: manuelle Ein-/Ausschaltung an alle verbundenen Telegram-Chats."""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
CHAT_IDS_PATH = BACKEND_DIR / "telegram_chats.json"
CONFIG_PATH = BACKEND_DIR / "telegram_config.json"

SWITCH_ON = {"on", "resume"}
SWITCH_OFF = {"off", "pause"}
SWITCH_ACTIONS = SWITCH_ON | SWITCH_OFF


def is_manual_switch_action(action: str) -> bool:
    return str(action or "").strip().lower() in SWITCH_ACTIONS


def switch_verb(action: str) -> str:
    key = str(action or "").strip().lower()
    if key in SWITCH_ON:
        return "eingeschaltet"
    if key == "pause":
        return "pausiert"
    return "ausgeschaltet"


def _strip_trailing_number(name: str) -> str:
    """Notiz wie 'Beispiel' → 'Hydro'; die Zahl steht nicht mehr in der Nachricht."""
    cleaned = re.sub(r"\s+\d+$", "", str(name or "").strip())
    return cleaned or str(name or "").strip()


def device_display_name(device: Optional[Dict[str, Any]]) -> str:
    device = device or {}
    label = str(device.get("label") or "").strip()
    if label:
        return _strip_trailing_number(label)
    if str(device.get("type") or "").lower() == "ha":
        entity = str(device.get("entity_id") or device.get("id") or "").strip()
        try:
            from device_registry import load_ha_devices_raw

            for d in load_ha_devices_raw():
                if d.get("entity_id") == entity:
                    return _strip_trailing_number(str(d.get("label") or entity))
        except Exception:
            pass
        return entity or "unbekanntes Gerät"
    ip = str(device.get("ip") or device.get("id") or "").strip()
    try:
        from device_registry import load_miners_raw

        for m in load_miners_raw():
            if m.get("ip") == ip:
                return _strip_trailing_number(str(m.get("note") or m.get("model") or ip))
    except Exception:
        pass
    return ip or "unbekanntes Gerät"


def device_ip(device: Optional[Dict[str, Any]]) -> str:
    device = device or {}
    ip = str(device.get("ip") or "").strip()
    if ip:
        return ip
    if str(device.get("type") or "").lower() == "ha":
        return ""
    ident = str(device.get("id") or "").strip()
    if ident.count(".") == 3:
        return ident
    return ""


def device_identity(device: Optional[Dict[str, Any]]) -> str:
    """Gerätename plus vollständige IP, damit eindeutig ist, was geschaltet wurde."""
    name = device_display_name(device)
    ip = device_ip(device)
    if ip and ip not in name:
        return f"{name} ({ip})"
    if ip:
        return ip
    entity = str((device or {}).get("entity_id") or "").strip()
    if entity and entity not in name:
        return f"{name} ({entity})"
    return name or "unbekanntes Gerät"


def format_actor(actor: Optional[Dict[str, Any]]) -> str:
    actor = actor or {}
    source = str(actor.get("source") or "").lower()
    chat_id = actor.get("chat_id")
    user_id = actor.get("user_id")
    name = str(actor.get("name") or actor.get("username") or "").strip()
    if chat_id is not None and str(chat_id).strip() != "":
        text = f"Nutzer mit Chat-ID {chat_id}"
        if user_id is not None and str(user_id) != str(chat_id):
            text += f" (User-ID {user_id})"
        if name:
            text += f" ({name})"
        return text
    if source == "dashboard" or actor.get("username"):
        user = name or "Dashboard"
        return f"Nutzer mit Dashboard-Konto {user}"
    return "Nutzer"


def format_manual_switch_message(
    actor: Optional[Dict[str, Any]],
    device_name: str,
    action: str,
    *,
    ip: str = "",
) -> str:
    ident = _strip_trailing_number(device_name)
    ip = str(ip or "").strip()
    if ip and ip not in ident:
        ident = f"{ident} ({ip})"
    return (
        f"{format_actor(actor)} hat Gerät {ident} {switch_verb(action)}."
    )


def load_connected_chat_ids(path: Optional[Path] = None) -> List[int]:
    p = path or CHAT_IDS_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return sorted({int(x) for x in data})
    except Exception:
        return []


def _load_bot_token() -> str:
    if not CONFIG_PATH.is_file():
        return ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("telegram_token") or "").strip()
    except Exception:
        return ""


def _post_telegram_message(token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read(256)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Telegram-Broadcast an {chat_id} fehlgeschlagen: {exc}")


def broadcast_text(text: str, *, chat_ids: Optional[Iterable[int]] = None) -> int:
    """Sendet synchron an alle verbundenen Chats. Rückgabe: Anzahl Versuche."""
    token = _load_bot_token()
    if not token or not text:
        return 0
    ids = list(chat_ids) if chat_ids is not None else load_connected_chat_ids()
    for chat_id in ids:
        _post_telegram_message(token, int(chat_id), text)
    return len(ids)


def notify_manual_switch(
    device: Optional[Dict[str, Any]],
    action: str,
    *,
    actor: Optional[Dict[str, Any]] = None,
    background: bool = True,
) -> Optional[str]:
    """Nur Ein/Aus. Läuft im Hintergrund, blockiert die Schaltung nicht."""
    if not is_manual_switch_action(action):
        return None
    text = format_manual_switch_message(
        actor,
        device_display_name(device),
        action,
        ip=device_ip(device),
    )

    def _run() -> None:
        try:
            broadcast_text(text)
        except Exception as exc:
            print(f"Manuelle-Schalt-Nachricht fehlgeschlagen: {exc}")

    if background:
        threading.Thread(target=_run, name="tg-manual-switch", daemon=True).start()
    else:
        _run()
    return text
