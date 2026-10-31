# -*- coding: utf-8 -*-
"""Energiegemeinschaft über Tailscale: Code, Minutenpaket, Auftrag.

Tailscale ist die einzige zentrale Infrastruktur. Die Pakete gehen danach
direkt von Pi zu Pi. Ein Auftrag wird nur angezeigt, nicht an den
Wechselrichter geschrieben.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from energiegemeinschaft import BACKEND_DIR, SETTINGS_FILE, load_settings as load_page_settings

LISTEN_PORT = 8765
TICK_SEC = 60
FRESH_SEC = 90
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_lock = threading.Lock()
_started = False
_sock: Optional[socket.socket] = None


def generate_invite_code() -> str:
    import secrets

    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"EG-{raw[:4]}-{raw[4:]}"


def code_is_valid(code: str) -> bool:
    parts = (code or "").strip().upper().split("-")
    if len(parts) != 3 or parts[0] != "EG":
        return False
    left, right = parts[1], parts[2]
    return (
        len(left) == 4
        and len(right) == 4
        and all(ch in _ALPHABET for ch in left + right)
    )


def _canonical(payload: Dict[str, Any]) -> bytes:
    body = {key: payload[key] for key in sorted(payload) if key != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_packet(payload: Dict[str, Any], code: str) -> Dict[str, Any]:
    out = {key: value for key, value in payload.items() if key != "sig"}
    digest = hmac.new(code.encode("utf-8"), _canonical(out), hashlib.sha256).hexdigest()
    out["sig"] = digest
    return out


def verify_packet(payload: Dict[str, Any], code: str) -> bool:
    if not code or not isinstance(payload, dict):
        return False
    sig = str(payload.get("sig") or "")
    if not sig:
        return False
    expected = sign_packet(payload, code).get("sig") or ""
    return hmac.compare_digest(sig, expected)


def verify_any(payload: Dict[str, Any], codes: List[str]) -> bool:
    return any(verify_packet(payload, code) for code in codes)


def merge_invite_codes(previous: str, extras: Any, new: str) -> List[str]:
    """Den neuen Code vorne, ältere Codes behalten, damit eine zweite Steuerung die erste nicht ausblendet."""
    kept: List[str] = []
    for code in [previous, *list(extras or [])]:
        text = str(code or "").strip().upper()
        if code_is_valid(text) and text != new and text not in kept:
            kept.append(text)
    return kept[-50:]


def feed_packet(
    name: str,
    feed_in_kw: float,
    soc_percent: Optional[float],
    code: str,
    energy_kwh: Optional[float] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "v": 1,
        "kind": "feed",
        "from": name,
        "ts": datetime.now(timezone.utc).isoformat(),
        "feed_in_kw": round(float(feed_in_kw), 3),
    }
    if soc_percent is not None:
        payload["soc_percent"] = round(float(soc_percent), 1)
    if energy_kwh is not None:
        payload["feed_in_kwh"] = round(float(energy_kwh), 3)
    return sign_packet(payload, code)


def command_packet(sender: str, target: str, text: str, code: str) -> Dict[str, Any]:
    payload = {
        "v": 1,
        "kind": "cmd",
        "id": uuid.uuid4().hex,
        "from": sender,
        "to": target,
        "text": text.strip(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return sign_packet(payload, code)


def _read_raw() -> Dict[str, Any]:
    if not SETTINGS_FILE.is_file():
        return {}
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_raw(data: Dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mesh_settings() -> Dict[str, Any]:
    raw = _read_raw()
    return {
        "invite_code": str(raw.get("invite_code") or ""),
        "invite_codes": raw.get("invite_codes") if isinstance(raw.get("invite_codes"), list) else [],
        "display_name": str(raw.get("display_name") or ""),
        "auth_key_set": bool(str(raw.get("tailscale_auth_key") or "").strip()),
        "members": raw.get("members") if isinstance(raw.get("members"), dict) else {},
        "removed_members": raw.get("removed_members") if isinstance(raw.get("removed_members"), list) else [],
        "commands": raw.get("commands") if isinstance(raw.get("commands"), list) else [],
    }


def _update(mutator) -> Dict[str, Any]:
    with _lock:
        data = _read_raw()
        mutator(data)
        _write_raw(data)
        return data


def set_invite_code(code: str) -> str:
    text = code.strip().upper()
    if not code_is_valid(text):
        raise ValueError("Code hat die Form EG-XXXX-XXXX.")

    def mutate(data: Dict[str, Any]) -> None:
        previous = str(data.get("invite_code") or "")
        data["invite_codes"] = merge_invite_codes(previous, data.get("invite_codes"), text)
        data["invite_code"] = text

    _update(mutate)
    return text


def accepted_codes() -> List[str]:
    settings = mesh_settings()
    codes: List[str] = []
    primary = str(settings.get("invite_code") or "").strip().upper()
    if code_is_valid(primary):
        codes.append(primary)
    for extra in settings.get("invite_codes") or []:
        text = str(extra or "").strip().upper()
        if code_is_valid(text) and text not in codes:
            codes.append(text)
    return codes


def generate_and_store_code() -> str:
    code = generate_invite_code()
    set_invite_code(code)
    return code


def set_auth_key(auth_key: str) -> None:
    text = auth_key.strip()

    def mutate(data: Dict[str, Any]) -> None:
        data["tailscale_auth_key"] = text

    _update(mutate)


def _run(args: List[str], timeout: float = 8) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def tailscale_snapshot() -> Dict[str, Any]:
    """Name, eigene Adresse und Gegenstellen. Leer, wenn Tailscale fehlt."""
    try:
        proc = _run(["tailscale", "status", "--json"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "self_ip": "", "self_name": "", "peers": []}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "tailscale status fehlgeschlagen").strip()[:240],
            "self_ip": "",
            "self_name": "",
            "peers": [],
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "tailscale status ist kein JSON", "self_ip": "", "self_name": "", "peers": []}
    self_info = payload.get("Self") or {}
    ips = self_info.get("TailscaleIPs") or []
    self_ip = ""
    for ip in ips:
        if ":" not in str(ip):
            self_ip = str(ip)
            break
    dns = str(self_info.get("DNSName") or "").strip(".")
    self_name = dns.split(".")[0] if dns else ""
    peers = []
    for peer in (payload.get("Peer") or {}).values():
        if not isinstance(peer, dict):
            continue
        pip = ""
        for ip in peer.get("TailscaleIPs") or []:
            if ":" not in str(ip):
                pip = str(ip)
                break
        if not pip:
            continue
        pdns = str(peer.get("DNSName") or "").strip(".")
        peers.append(
            {
                "name": pdns.split(".")[0] if pdns else pip,
                "ip": pip,
                "online": bool(peer.get("Online")),
            }
        )
    return {"ok": True, "error": "", "self_ip": self_ip, "self_name": self_name, "peers": peers}


def connect_tailscale(auth_key: str) -> Tuple[bool, str]:
    """Nimmt diesen Pi mit einem Auth-Key ohne Markierung ins Konto."""
    key = auth_key.strip()
    if len(key) < 12:
        return False, "Auth-Key fehlt."
    set_auth_key(key)
    try:
        proc = _run(["tailscale", "up", "--auth-key", key], timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "tailscale up fehlgeschlagen").strip()
        return False, detail[:240]
    return True, "Tailscale verbunden."


def local_export_kwh() -> Optional[float]:
    """Heutige Netzeinspeisung in kWh, aus der Tagesstatistik (Verkauf)."""
    try:
        from energy_stats_24h import read_stats_24h

        state = read_stats_24h(str(BACKEND_DIR))
        sold = (state.get("consumption_kwh") or {}).get("verkauf")
        if sold is None:
            return None
        return round(float(sold), 3)
    except Exception:
        return None


def local_reading() -> Tuple[float, Optional[float], Optional[float]]:
    try:
        from control_algorithm.live_data import load_live_snapshot

        live = load_live_snapshot()
        grid = float(live.get("grid_kw") or 0)
        feed = round(max(0.0, -grid), 3)
        soc_raw = live.get("soc_percent")
        soc = None if soc_raw is None else round(float(soc_raw), 1)
        return feed, soc, local_export_kwh()
    except Exception:
        return 0.0, None, None


def local_name(snapshot: Optional[Dict[str, Any]] = None) -> str:
    stored = mesh_settings().get("display_name") or ""
    if stored:
        return stored
    snap = snapshot if snapshot is not None else tailscale_snapshot()
    if snap.get("self_name"):
        return str(snap["self_name"])
    return socket.gethostname().split(".")[0]


CONNECTED_GAP_SEC = 150
HISTORY_LIMIT = 360


def append_history(previous: Any, now: float, feed_kw: Optional[float], soc: Optional[float]) -> List[Dict[str, Any]]:
    """Einen Zeitpunkt an den Verlauf dieses Geräts hängen."""
    history = [item for item in (previous or []) if isinstance(item, dict)]
    point: Dict[str, Any] = {"ts": round(float(now), 1)}
    if feed_kw is not None:
        point["feed_in_kw"] = round(float(feed_kw), 3)
    if soc is not None:
        point["soc_percent"] = round(float(soc), 1)
    if history and abs(float(history[-1].get("ts") or 0) - float(now)) < 20:
        history[-1] = point
    else:
        history.append(point)
    return history[-HISTORY_LIMIT:]


def history_for_display(item: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Aktuellen Einspeise- und Speicherwert sofort als ersten Punkt zeigen."""
    stored = [point for point in (item.get("history") or []) if isinstance(point, dict)]
    if stored:
        return stored[-HISTORY_LIMIT:]
    feed = item.get("feed_in_kw")
    soc = item.get("soc_percent")
    feed_v = float(feed) if isinstance(feed, (int, float)) else None
    soc_v = float(soc) if isinstance(soc, (int, float)) else None
    if feed_v is None and soc_v is None:
        return []
    stamp = float(item.get("seen") or now)
    return append_history([], stamp, feed_v, soc_v)


def seed_live_history() -> None:
    """Liegt schon ein Messwert vor, den ersten Verlaufspunkt sofort mitschreiben."""
    members = mesh_settings().get("members") or {}
    needs_seed = False
    for item in members.values():
        if not isinstance(item, dict) or item.get("history"):
            continue
        if isinstance(item.get("feed_in_kw"), (int, float)) or isinstance(item.get("soc_percent"), (int, float)):
            needs_seed = True
            break
    if not needs_seed:
        return

    def mutate(data: Dict[str, Any]) -> None:
        stored = data.get("members")
        if not isinstance(stored, dict):
            return
        now = time.time()
        for item in stored.values():
            if not isinstance(item, dict) or item.get("history"):
                continue
            feed = item.get("feed_in_kw")
            soc = item.get("soc_percent")
            feed_v = float(feed) if isinstance(feed, (int, float)) else None
            soc_v = float(soc) if isinstance(soc, (int, float)) else None
            if feed_v is None and soc_v is None:
                continue
            stamp = float(item.get("seen") or now)
            item["history"] = append_history([], stamp, feed_v, soc_v)

    _update(mutate)


def accrue_connected_kwh(
    previous_kwh: Any,
    previous_seen: Any,
    previous_kw: Any,
    now: float,
    feed_kw: Optional[float],
    gap_sec: float = CONNECTED_GAP_SEC,
) -> float:
    """kWh nur in der Zeit addieren, in der die Steuerung verbunden bleibt."""
    total = float(previous_kwh or 0)
    if previous_seen in (None, 0, "") or feed_kw is None or previous_kw is None:
        return round(total, 3)
    elapsed = float(now) - float(previous_seen)
    if elapsed <= 0 or elapsed > gap_sec:
        return round(total, 3)
    average_kw = (float(previous_kw) + float(feed_kw)) / 2.0
    return round(total + average_kw * (elapsed / 3600.0), 3)


def _remember_member(
    name: str,
    ip: str,
    feed: Optional[float],
    soc: Optional[float],
    energy_kwh: Optional[float] = None,
) -> None:
    now = time.time()

    def mutate(data: Dict[str, Any]) -> None:
        members = data.get("members")
        if not isinstance(members, dict):
            members = {}
        prev = members.get(name) if isinstance(members.get(name), dict) else {}
        current_kw = feed if feed is not None else prev.get("feed_in_kw")
        members[name] = {
            "name": name,
            "ip": ip or prev.get("ip") or "",
            "feed_in_kw": current_kw,
            "feed_in_kwh": energy_kwh if energy_kwh is not None else prev.get("feed_in_kwh"),
            "connected_kwh": accrue_connected_kwh(
                prev.get("connected_kwh"),
                prev.get("seen"),
                prev.get("feed_in_kw"),
                now,
                None if current_kw is None else float(current_kw),
            ),
            "soc_percent": soc if soc is not None else prev.get("soc_percent"),
            "history": append_history(prev.get("history"), now, None if current_kw is None else float(current_kw), soc if soc is not None else prev.get("soc_percent")),
            "alias": prev.get("alias") or "",
            "ask_ct_kwh": prev.get("ask_ct_kwh"),
            "grid_cost_ct_kwh": prev.get("grid_cost_ct_kwh"),
            "seen": now,
        }
        data["members"] = members

    _update(mutate)


def _parse_ct(raw: str) -> Optional[float]:
    text = (raw or "").strip().replace(",", ".")
    if not text:
        return None
    value = float(text)
    if value < 0 or value > 10000:
        raise ValueError("Betrag liegt außerhalb.")
    return round(value, 3)


def community_price_key(row: Dict[str, Any]) -> Tuple[int, float, str]:
    """Günstigster vereinbarter Preis zuerst. Ohne Preis nach unten."""
    price = row.get("ask_ct_kwh")
    name = str(row.get("alias") or row.get("name") or "")
    if isinstance(price, (int, float)):
        return (0, float(price), name)
    return (1, 0.0, name)


def price_comparison(price: Any, grid_cost: Any) -> Dict[str, Any]:
    """Preis und Netzkosten gegenüberstellen und zur Summe zusammenführen."""
    if not isinstance(price, (int, float)) or not isinstance(grid_cost, (int, float)):
        return {"ready": False, "total_ct": None, "delta_ct": None}
    return {
        "ready": True,
        "total_ct": round(float(price) + float(grid_cost), 3),
        "delta_ct": round(float(grid_cost) - float(price), 3),
    }


def is_community_counterpart(item: Dict[str, Any], self_name: str, self_ip: str) -> bool:
    """Nur ein Gerät, das den Einladungscode geschickt hat. Nicht man selbst, nicht das ganze Konto."""
    if not isinstance(item, dict) or not item.get("seen"):
        return False
    if self_name and item.get("name") == self_name:
        return False
    if self_ip and item.get("ip") == self_ip:
        return False
    return True


def member_is_feeding(feed: Any, fresh: bool) -> bool:
    return bool(fresh and isinstance(feed, (int, float)) and float(feed) > 0)


def update_member_field(name: str, field: str, raw: str) -> None:
    if field not in {"alias", "ask_ct_kwh", "grid_cost_ct_kwh"}:
        raise ValueError("Unbekanntes Feld.")
    peer = name.strip()
    if not peer:
        raise ValueError("Steuerung fehlt.")
    if field in {"ask_ct_kwh", "grid_cost_ct_kwh"} and peer == local_name():
        raise ValueError("Preis und Netzkosten gelten für die anderen Geräte.")
    if field == "alias":
        value: Any = raw.strip()[:80]
    else:
        try:
            value = _parse_ct(raw)
        except ValueError as exc:
            if "außerhalb" in str(exc):
                raise
            raise ValueError("Bitte eine Zahl in ct/kWh eintragen.") from None

    def mutate(data: Dict[str, Any]) -> None:
        members = data.get("members")
        if not isinstance(members, dict):
            members = {}
        current = members.get(peer)
        if not isinstance(current, dict):
            current = {"name": peer, "ip": "", "seen": 0}
        current["name"] = peer
        current[field] = value
        members[peer] = current
        data["members"] = members

    _update(mutate)


def remove_member(name: str) -> None:
    """Mitglied aus der Gemeinschaft nehmen. Es kommt mit dem nächsten Paket nicht zurück."""
    peer = name.strip()
    if not peer:
        raise ValueError("Steuerung fehlt.")
    if peer == local_name():
        raise ValueError("Die eigene Steuerung bleibt.")

    def mutate(data: Dict[str, Any]) -> None:
        members = data.get("members")
        if not isinstance(members, dict) or not isinstance(members.get(peer), dict):
            raise ValueError("Mitglied ist nicht in der Gemeinschaft.")
        members.pop(peer, None)
        data["members"] = members
        removed = data.get("removed_members")
        if not isinstance(removed, list):
            removed = []
        if peer not in removed:
            removed.append(peer)
        data["removed_members"] = removed[-50:]

    _update(mutate)


def _removed_names() -> set:
    names = mesh_settings().get("removed_members")
    if not isinstance(names, list):
        return set()
    return {str(item).strip() for item in names if str(item).strip()}


def _remember_command(entry: Dict[str, Any]) -> None:
    def mutate(data: Dict[str, Any]) -> None:
        commands = data.get("commands")
        if not isinstance(commands, list):
            commands = []
        commands.append(entry)
        data["commands"] = commands[-40:]

    _update(mutate)


def _mark_command(command_id: str) -> None:
    def mutate(data: Dict[str, Any]) -> None:
        for entry in data.get("commands") or []:
            if isinstance(entry, dict) and entry.get("id") == command_id:
                entry["acked"] = True

    _update(mutate)


def known_member_keeps_link(name: str, source_ip: str, members: Any) -> bool:
    """Gerät, das den Code schon einmal gültig geschickt hat, bleibt an derselben Adresse online."""
    if not name or not source_ip or not isinstance(members, dict):
        return False
    item = members.get(name)
    if not isinstance(item, dict) or not item.get("seen"):
        return False
    return str(item.get("ip") or "") == source_ip


def _note_code_mismatch(name: str, source_ip: str) -> None:
    """Paket ist angekommen, der Code passt nicht. Den letzten gültigen Wert nicht überschreiben."""
    if not name:
        return
    current = _read_raw().get("members")
    if not isinstance(current, dict) or not isinstance(current.get(name), dict):
        return

    def mutate(data: Dict[str, Any]) -> None:
        members = data.get("members")
        if not isinstance(members, dict):
            return
        item = members.get(name)
        if not isinstance(item, dict):
            return
        item["code_mismatch"] = True
        item["rejected_seen"] = time.time()

    _update(mutate)


def handle_datagram(raw: bytes, source_ip: str) -> None:
    code = mesh_settings().get("invite_code") or ""
    if not code:
        return
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    sender = str(payload.get("from") or "") if isinstance(payload, dict) else ""
    if sender and sender in _removed_names():
        return
    verified = isinstance(payload, dict) and verify_any(payload, accepted_codes() or [code])
    if not verified:
        sender = str(payload.get("from") or "") if isinstance(payload, dict) else ""
        if (
            isinstance(payload, dict)
            and payload.get("kind") == "feed"
            and known_member_keeps_link(sender, source_ip, mesh_settings().get("members"))
        ):
            verified = True
        else:
            if isinstance(payload, dict) and payload.get("kind") == "feed":
                _note_code_mismatch(sender, source_ip)
            return
    kind = payload.get("kind")
    if kind == "feed":
        name = str(payload.get("from") or source_ip)
        feed = payload.get("feed_in_kw")
        soc = payload.get("soc_percent")
        energy = payload.get("feed_in_kwh")
        _remember_member(
            name,
            source_ip,
            None if feed is None else float(feed),
            None if soc is None else float(soc),
            None if energy is None else float(energy),
        )
        return
    if kind == "cmd" and str(payload.get("to") or "") == local_name():
        _remember_command(
            {
                "id": payload.get("id"),
                "from": payload.get("from"),
                "to": payload.get("to"),
                "text": payload.get("text"),
                "ts": payload.get("ts"),
                "incoming": True,
                "acked": False,
            }
        )
        _send_ack(payload, source_ip, code)
        return
    if kind == "ack":
        _mark_command(str(payload.get("id") or ""))


def _send_ack(command: Dict[str, Any], source_ip: str, code: str) -> None:
    if _sock is None or not source_ip:
        return
    packet = sign_packet(
        {
            "v": 1,
            "kind": "ack",
            "id": command.get("id"),
            "from": local_name(),
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        code,
    )
    try:
        _sock.sendto(json.dumps(packet).encode("utf-8"), (source_ip, LISTEN_PORT))
    except OSError:
        pass


def _send(packet: Dict[str, Any], ip: str) -> None:
    if _sock is None or not ip:
        return
    try:
        _sock.sendto(json.dumps(packet, ensure_ascii=False).encode("utf-8"), (ip, LISTEN_PORT))
    except OSError:
        pass


def send_command(target_name: str, text: str) -> Dict[str, Any]:
    code = mesh_settings().get("invite_code") or ""
    if not code:
        raise ValueError("Zuerst einen Einladungscode eintragen.")
    text = text.strip()
    if not text:
        raise ValueError("Auftragstext fehlt.")
    members = mesh_settings().get("members") or {}
    member = members.get(target_name)
    if not isinstance(member, dict) or not member.get("ip"):
        raise ValueError("Dieser Pi ist nicht in der Gemeinschaft erreichbar.")
    packet = command_packet(local_name(), target_name, text, code)
    _send(packet, str(member["ip"]))
    entry = {
        "id": packet["id"],
        "from": packet["from"],
        "to": target_name,
        "text": text,
        "ts": packet["ts"],
        "incoming": False,
        "acked": False,
    }
    _remember_command(entry)
    return entry


def _ensure_socket() -> None:
    global _sock
    if _sock is not None:
        return
    snap = tailscale_snapshot()
    bind_ip = snap.get("self_ip") or ""
    if not bind_ip:
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((bind_ip, LISTEN_PORT))
    except OSError:
        sock.close()
        return
    _sock = sock
    threading.Thread(target=_listen_loop, args=(sock,), name="eg-mesh-listen", daemon=True).start()


def cycle_once() -> None:
    _ensure_socket()
    code = mesh_settings().get("invite_code") or ""
    if not code or _sock is None:
        return
    snap = tailscale_snapshot()
    name = local_name(snap)
    feed, soc, energy = local_reading()
    _remember_member(name, snap.get("self_ip") or "", feed, soc, energy)
    packet = feed_packet(name, feed, soc, code, energy)
    known = {item.get("ip") for item in (mesh_settings().get("members") or {}).values() if isinstance(item, dict)}
    self_ip = snap.get("self_ip") or ""
    for peer in snap.get("peers") or []:
        ip = peer.get("ip") or ""
        if not ip or ip == self_ip:
            continue
        if ip in known or peer.get("online"):
            _send(packet, ip)


def _listen_loop(sock: socket.socket) -> None:
    while True:
        try:
            raw, addr = sock.recvfrom(4096)
        except OSError:
            time.sleep(1)
            continue
        handle_datagram(raw, addr[0])


def _tick_loop() -> None:
    while True:
        try:
            cycle_once()
        except Exception:
            pass
        time.sleep(TICK_SEC)


def start_mesh() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_tick_loop, name="eg-mesh-tick", daemon=True).start()


def public_status() -> Dict[str, Any]:
    seed_live_history()
    snap = tailscale_snapshot()
    settings = mesh_settings()
    now = time.time()
    members = []
    feed_sum = 0.0
    fresh_count = 0
    self_name = local_name(snap)
    self_ip = snap.get("self_ip") or ""
    for item in (settings.get("members") or {}).values():
        if not is_community_counterpart(item, self_name, self_ip):
            continue
        seen = float(item.get("seen") or 0)
        age = None if not seen else round(now - seen, 1)
        fresh = age is not None and age <= FRESH_SEC
        rejected = float(item.get("rejected_seen") or 0)
        code_mismatch = bool(item.get("code_mismatch")) and rejected > 0 and (now - rejected) <= FRESH_SEC
        if fresh:
            fresh_count += 1
        feed = item.get("feed_in_kw")
        if fresh and isinstance(feed, (int, float)):
            feed_sum += float(feed)
        is_self = False
        members.append(
            {
                "name": item.get("name"),
                "alias": item.get("alias") or "",
                "ask_ct_kwh": item.get("ask_ct_kwh"),
                "grid_cost_ct_kwh": item.get("grid_cost_ct_kwh"),
                "feed_in_kw": feed,
                "feed_in_kwh": item.get("feed_in_kwh"),
                "connected_kwh": item.get("connected_kwh") or 0,
                "soc_percent": item.get("soc_percent"),
                "age_sec": age,
                "fresh": fresh,
                "code_mismatch": code_mismatch,
                "feeding": member_is_feeding(feed, fresh),
                "self": is_self,
                "comparison": price_comparison(item.get("ask_ct_kwh"), item.get("grid_cost_ct_kwh")),
                "history": history_for_display(item, now),
            }
        )
    members.sort(key=community_price_key)
    page = load_page_settings()
    return {
        "ok": True,
        "tailscale_ok": bool(snap.get("ok")),
        "tailscale_error": snap.get("error") or "",
        "self_name": self_name,
        "self_ip": self_ip,
        "invite_set": bool(settings.get("invite_code")),
        "invite_code": settings.get("invite_code") or "",
        "auth_key_set": settings.get("auth_key_set"),
        "peer_count": len(snap.get("peers") or []),
        "fresh_count": fresh_count,
        "connection_count": sum(1 for row in members if not row["self"]),
        "feed_sum_kw": round(feed_sum, 3),
        "members": members,
        "commands": list(reversed(settings.get("commands") or []))[:20],
        "community": page.get("community"),
        "inverter_write": False,
    }
