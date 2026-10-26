#!/usr/bin/env python3
"""Home Assistant data bridge: snapshot all device values to RAM every 30s."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

INTERVAL_SECONDS = 30
OUTPUT_PATH = Path("/tmp/ha_snapshot.json")
CREDENTIALS_PATH = Path.home() / ".config/ha-bridge/credentials.json"
DEVICE_REGISTRY = Path("/home/piminer/homeassistant/config/.storage/core.device_registry")
ENTITY_REGISTRY = Path("/home/piminer/homeassistant/config/.storage/core.entity_registry")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ha-bridge] %(levelname)s %(message)s",
)
log = logging.getLogger("ha-bridge")


class HomeAssistantClient:
    def __init__(self, ha_url: str, refresh_token: str, client_id: str) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.refresh_token = refresh_token
        self.client_id = client_id
        self._access_token: str | None = None
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

    def get_json(self, path: str) -> list | dict:
        token = self._get_access_token()
        req = urllib.request.Request(
            f"{self.ha_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)


def load_json_file(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def build_snapshot(client: HomeAssistantClient) -> dict:
    device_registry = load_json_file(DEVICE_REGISTRY)
    entity_registry = load_json_file(ENTITY_REGISTRY)
    states = client.get_json("/api/states")

    devices_by_id: dict[str, dict] = {}
    for device in device_registry.get("data", {}).get("devices", []):
        device_id = device["id"]
        devices_by_id[device_id] = {
            "id": device_id,
            "name": device.get("name_by_user") or device.get("name") or device_id,
            "manufacturer": device.get("manufacturer"),
            "model": device.get("model"),
            "area_id": device.get("area_id"),
            "via_device_id": device.get("via_device_id"),
            "disabled_by": device.get("disabled_by"),
            "entities": {},
        }

    state_by_entity = {item["entity_id"]: item for item in states}
    unassigned_entities: dict[str, dict] = {}

    for entity in entity_registry.get("data", {}).get("entities", []):
        if entity.get("disabled_by"):
            continue

        entity_id = entity["entity_id"]
        state = state_by_entity.get(entity_id)
        if not state:
            continue

        entity_payload = {
            "entity_id": entity_id,
            "name": entity.get("name") or entity.get("original_name"),
            "platform": entity.get("platform"),
            "device_class": entity.get("device_class"),
            "unit_of_measurement": entity.get("unit_of_measurement"),
            "state": state.get("state"),
            "attributes": state.get("attributes", {}),
            "last_changed": state.get("last_changed"),
            "last_updated": state.get("last_updated"),
        }

        device_id = entity.get("device_id")
        if device_id and device_id in devices_by_id:
            devices_by_id[device_id]["entities"][entity_id] = entity_payload
        else:
            unassigned_entities[entity_id] = entity_payload

    active_devices: dict[str, dict] = {}
    for device in devices_by_id.values():
        if device["entities"]:
            active_devices[device["id"]] = device

    return {
        "updated_at": datetime.now(UTC).isoformat(),
        "interval_seconds": INTERVAL_SECONDS,
        "device_count": len(active_devices),
        "entity_count": sum(len(device["entities"]) for device in active_devices.values())
        + len(unassigned_entities),
        "devices": active_devices,
        "unassigned_entities": unassigned_entities,
    }


def write_snapshot(snapshot: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=OUTPUT_PATH.parent,
        prefix=".ha_snapshot_",
        suffix=".json.tmp",
    )
    os.close(fd)
    temp = Path(temp_path)
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        temp.replace(OUTPUT_PATH)
        os.chmod(OUTPUT_PATH, 0o600)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def load_credentials() -> tuple[str, str, str]:
    with CREDENTIALS_PATH.open(encoding="utf-8") as handle:
        creds = json.load(handle)
    return creds["ha_url"], creds["refresh_token"], creds["client_id"]


def main() -> None:
    ha_url, refresh_token, client_id = load_credentials()
    client = HomeAssistantClient(ha_url, refresh_token, client_id)
    log.info("Starting bridge -> %s every %ss", OUTPUT_PATH, INTERVAL_SECONDS)

    while True:
        started = time.monotonic()
        try:
            snapshot = build_snapshot(client)
            write_snapshot(snapshot)
            log.info(
                "Snapshot written: %s devices, %s entities",
                snapshot["device_count"],
                snapshot["entity_count"],
            )
        except urllib.error.HTTPError as exc:
            log.error("Home Assistant API error: %s %s", exc.code, exc.read().decode()[:200])
            if exc.code == 401:
                client._access_token = None
        except Exception:
            log.exception("Snapshot failed")

        elapsed = time.monotonic() - started
        time.sleep(max(0, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
