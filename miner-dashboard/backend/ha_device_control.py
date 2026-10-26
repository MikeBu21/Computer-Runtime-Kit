#!/usr/bin/env python3
"""Home Assistant Service-Calls zum Ein-/Ausschalten von Geräten."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

CREDENTIALS_PATH = Path.home() / ".config/ha-bridge/credentials.json"

# Domains mit turn_on / turn_off (synchron mit ha_entity_mapping.CONTROLLABLE_DOMAINS)
from ha_entity_mapping import CONTROLLABLE_DOMAINS

LOCK_DOMAIN = "lock"
COVER_DOMAIN = "cover"

ON_STATES = frozenset({"on", "open", "opening"})
OFF_STATES = frozenset({"off", "closed", "closing"})


def entity_domain(entity_id: str) -> str:
    return (entity_id or "").split(".", 1)[0]


def is_controllable_domain(domain: str) -> bool:
    return domain in CONTROLLABLE_DOMAINS


class HomeAssistantControlClient:
    def __init__(self, ha_url: str, refresh_token: str, client_id: str) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.refresh_token = refresh_token
        self.client_id = client_id
        self._access_token: Optional[str] = None
        self._access_token_expires = 0.0

    def _refresh_access_token(self) -> None:
        data = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.ha_url}/auth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
        self._access_token = payload["access_token"]
        self._access_token_expires = time.time() + payload.get("expires_in", 1800) - 60

    def _get_access_token(self) -> str:
        if not self._access_token or time.time() >= self._access_token_expires:
            self._refresh_access_token()
        assert self._access_token
        return self._access_token

    def call_service(self, domain: str, service: str, entity_id: str) -> bool:
        token = self._get_access_token()
        body = json.dumps({"entity_id": entity_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.ha_url}/api/services/{domain}/{service}",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._access_token = None
            return False
        except Exception:
            return False

    def get_state(self, entity_id: str) -> Optional[str]:
        """Live-Zustand einer Entity (None bei Fehler)."""
        token = self._get_access_token()
        req = urllib.request.Request(
            f"{self.ha_url}/api/states/{urllib.parse.quote(entity_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.load(response)
            return str(payload.get("state") or "")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._access_token = None
            return None
        except Exception:
            return None


_client: Optional[HomeAssistantControlClient] = None


def _get_client() -> HomeAssistantControlClient:
    global _client
    if _client is None:
        with CREDENTIALS_PATH.open(encoding="utf-8") as handle:
            creds = json.load(handle)
        _client = HomeAssistantControlClient(
            creds["ha_url"],
            creds["refresh_token"],
            creds["client_id"],
        )
    return _client


def ha_action_to_service(action: str, domain: str) -> Optional[str]:
    """Miner pause/resume/on/off → HA service name."""
    if action in ("pause", "off", "turn_off"):
        if domain == COVER_DOMAIN:
            return "close"
        if domain == LOCK_DOMAIN:
            return "lock"
        return "turn_off"
    if action in ("resume", "on", "turn_on"):
        if domain == COVER_DOMAIN:
            return "open"
        if domain == LOCK_DOMAIN:
            return "unlock"
        return "turn_on"
    return None


def read_ha_entity_state(entity_id: str) -> Optional[str]:
    """Live HA-State lesen (ohne Snapshot)."""
    try:
        return _get_client().get_state(entity_id)
    except Exception:
        return None


def execute_ha_command(entity_id: str, action: str) -> bool:
    domain = entity_domain(entity_id)
    service = ha_action_to_service(action, domain)
    if not service or not is_controllable_domain(domain):
        return False
    try:
        ok = _get_client().call_service(domain, service, entity_id)
        if not ok:
            return False
        # HTTP 200 allein reicht nicht – Stecker kann „on“ bleiben.
        time.sleep(0.6)
        live = read_ha_entity_state(entity_id)
        if live is None:
            return False
        status = ha_state_to_device_status(live)
        if action in ("pause", "off", "turn_off"):
            return status == "paused"
        if action in ("resume", "on", "turn_on"):
            return status == "running"
        return True
    except Exception:
        return False


def ha_state_to_device_status(state: str) -> str:
    """HA state → running/paused (Miner-kompatibel)."""
    s = (state or "").strip().lower()
    if s in ON_STATES:
        return "running"
    if s in OFF_STATES:
        return "paused"
    return "unknown"
