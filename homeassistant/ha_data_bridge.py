#!/usr/bin/env python3
"""Home Assistant data bridge: snapshot sensor values in RAM every 6s."""

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

INTERVAL_SECONDS = 6
# Wechselrichter bleiben in der Sensorliste: Inverter 2, Solarman und Fronius
# (SolarNet, Symo, GEN24). Oswald/Osswald, Heizung und Fischteich bleiben draußen.
INVERTER_DEVICE_NAMES = {"inverter 2"}
KEEP_NAME_PARTS = ("solarman", "fronius", "solarnet", "symo", "gen24", "wechselrichter")
BANNED_NAME_PARTS = (
    "osswald",
    "oswald",
    "heizkörper sofa",
    "heizkorper sofa",
    "heizkorper_sofa",
    "fischteich",
)
# Gemeinsame Ablage im RAM (tmpfs). Nicht unter /tmp und nicht auf der SSD.
OUTPUT_PATH = Path("/dev/shm/ha_snapshot.json")
LATEST_SNAPSHOT: dict = {}
CREDENTIALS_PATH = Path.home() / ".config/ha-bridge/credentials.json"
# Home Assistant kann im eigenen Ordner oder im Steuerungsordner liegen.
# Es gewinnt die Registry mit den meisten Einträgen, damit ein leeres Zweitverzeichnis
# die Sensorliste nicht ausblendet.
REGISTRY_ROOTS = (
    Path("/home/piminer/homeassistant/config/.storage"),
    Path("/home/piminer/minersteuerung/homeassistant/config/.storage"),
)

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


def load_registry(filename: str, list_key: str) -> dict:
    """Registry-Datei lesen. Fehlt sie oder ist sie kaputt, kommt ein leeres Dokument zurück."""
    best: dict = {}
    best_count = -1
    for root in REGISTRY_ROOTS:
        path = root / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        entries = (data.get("data") or {}).get(list_key) or []
        count = len(entries) if isinstance(entries, list) else 0
        if count > best_count:
            best = data
            best_count = count
    return best


def _banned(*parts: str) -> bool:
    folded = " ".join(str(part or "") for part in parts).casefold()
    return any(needle in folded for needle in BANNED_NAME_PARTS)


def _is_kept_text(text: str) -> bool:
    folded = (text or "").casefold()
    if folded.strip() in INVERTER_DEVICE_NAMES:
        return True
    if "192.168.1.176" in folded or "192_168_1_176" in folded:
        return True
    return any(part in folded for part in KEEP_NAME_PARTS)


def _is_inverter_device(name: str) -> bool:
    return _is_kept_text(name)


def _is_inverter_live(entity_id: str, friendly: str) -> bool:
    return _is_kept_text(f"{entity_id} {friendly}")


def _entity_payload(entity_id: str, state: dict | None, registry: dict | None = None) -> dict:
    registry = registry or {}
    state = state or {}
    attrs = state.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    return {
        "entity_id": entity_id,
        "name": registry.get("name")
        or registry.get("original_name")
        or attrs.get("friendly_name")
        or entity_id,
        "platform": registry.get("platform"),
        "device_class": registry.get("device_class") or attrs.get("device_class"),
        "unit_of_measurement": registry.get("unit_of_measurement")
        or attrs.get("unit_of_measurement"),
        "state": state.get("state", "unknown"),
        "attributes": attrs,
        "last_changed": state.get("last_changed"),
        "last_updated": state.get("last_updated"),
    }


def build_snapshot(client: HomeAssistantClient) -> dict:
    device_registry = load_registry("core.device_registry", "devices")
    entity_registry = load_registry("core.entity_registry", "entities")
    states = client.get_json("/api/states")
    if not isinstance(states, list):
        states = []

    devices_by_id: dict[str, dict] = {}
    for device in (device_registry.get("data") or {}).get("devices") or []:
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

    state_by_entity = {
        item["entity_id"]: item
        for item in states
        if isinstance(item, dict) and item.get("entity_id")
    }
    unassigned_entities: dict[str, dict] = {}

    for entity in (entity_registry.get("data") or {}).get("entities") or []:
        if entity.get("disabled_by"):
            continue

        entity_id = entity["entity_id"]
        device_id = entity.get("device_id")
        device = devices_by_id.get(device_id) if device_id else None
        device_name = (device or {}).get("name") or ""
        if _banned(entity_id, entity.get("name"), entity.get("original_name"), device_name):
            continue
        if not _is_inverter_device(device_name):
            continue
        state = state_by_entity.get(entity_id)
        entity_payload = _entity_payload(entity_id, state, entity)
        devices_by_id[device_id]["entities"][entity_id] = entity_payload

    # Sensoren, die Home Assistant live meldet, aber die lokale Registry-Datei
    # nicht enthält (falscher Ordner, noch nicht geschrieben), trotzdem anzeigen.
    known = set(unassigned_entities)
    for device in devices_by_id.values():
        known.update(device["entities"])
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "")
        attrs = item.get("attributes") or {}
        friendly = attrs.get("friendly_name") if isinstance(attrs, dict) else ""
        if not entity_id or entity_id in known or _banned(entity_id, friendly):
            continue
        # Live gemeldete Sensoren ohne Registry-Eintrag: Wechselrichter oder Solarman.
        if not _is_inverter_live(entity_id, str(friendly or "")):
            continue
        unassigned_entities[entity_id] = _entity_payload(entity_id, item)
        known.add(entity_id)

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


def _ram_dir() -> Path | None:
    """Nur /dev/shm, und nur wenn es wirklich tmpfs ist."""
    ram = OUTPUT_PATH.parent
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ram if ram.is_dir() else None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(ram) and parts[2] == "tmpfs":
            return ram
    log.error("%s ist kein tmpfs, Snapshot wird nicht auf die SSD geschrieben", ram)
    return None


def write_snapshot(snapshot: dict) -> None:
    global LATEST_SNAPSHOT
    LATEST_SNAPSHOT = snapshot
    ram = _ram_dir()
    if ram is None:
        return
    fd, temp_path = tempfile.mkstemp(
        dir=ram,
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


def _credentials_mtime() -> float:
    try:
        return CREDENTIALS_PATH.stat().st_mtime
    except OSError:
        return 0.0


def main() -> None:
    ha_url, refresh_token, client_id = load_credentials()
    client = HomeAssistantClient(ha_url, refresh_token, client_id)
    creds_mtime = _credentials_mtime()
    log.info("Starting bridge -> %s every %ss", OUTPUT_PATH, INTERVAL_SECONDS)

    while True:
        started = time.monotonic()
        # ha_bridge_token.py schreibt beim Home-Assistant-Start einen neuen Token.
        current_mtime = _credentials_mtime()
        if current_mtime != creds_mtime:
            try:
                ha_url, refresh_token, client_id = load_credentials()
                client = HomeAssistantClient(ha_url, refresh_token, client_id)
                creds_mtime = current_mtime
                log.info("Credentials reloaded")
            except Exception:
                log.exception("Credentials unreadable")
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
