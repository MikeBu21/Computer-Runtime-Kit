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
import os
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from energiegemeinschaft import BACKEND_DIR, SETTINGS_FILE, load_settings as load_page_settings

LISTEN_PORT = 8765
# Gleicher Takt wie die BMS-Schleife (SAMPLE_GAP_SEC): Einspeisung der Mitglieder
# wird alle 6 s ausgetauscht und im selben Takt ins Register geschrieben.
TICK_SEC = 6
FRESH_SEC = 90
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_lock = threading.Lock()
_started = False
_sock: Optional[socket.socket] = None
_sock_ip = ""
_listen_gen = 0
_last_tick_ok = 0.0
# Letzter vollständig gelesener Stand. Ein halbfertiges JSON darf daraus nicht
# eine leere Gemeinschaft machen.
_last_good_path = ""
_last_good: Dict[str, Any] = {}


def format_stillzeit(age_sec: Optional[float], code_mismatch: bool = False) -> str:
    """Lesbare Stillzeit: Sekunden nur intern, Anzeige in Min./Std./Tagen."""
    if code_mismatch:
        return "im Netz, der Code passt nicht mehr"
    if age_sec is None:
        return "noch keine Meldung"
    if age_sec < FRESH_SEC:
        return "letzte Minute da"
    total = max(0, int(round(float(age_sec))))
    days = total // 86400
    hours = (total % 86400) // 3600
    mins = max(1, (total % 3600) // 60)
    parts: List[str] = []
    if days:
        parts.append("1 Tag" if days == 1 else f"{days} Tagen")
    if hours:
        parts.append("1 Std." if hours == 1 else f"{hours} Std.")
    if not days:
        parts.append("1 Min." if mins == 1 else f"{mins} Min.")
    return "still seit " + " ".join(parts)


def generate_invite_code() -> str:
    """16 Zeichen aus 32 Symbolen = 80 Bit aus dem Kernel-Zufall (secrets/os.urandom).

    Der Code ist zugleich HMAC-Schlüssel der Mesh-Pakete, darf also nicht erratbar sein.
    """
    import secrets

    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return "EG-" + "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def code_is_valid(code: str) -> bool:
    """EG-XXXX-XXXX-XXXX-XXXX; ältere Codes EG-XXXX-XXXX bleiben zum Beitreten gültig."""
    parts = (code or "").strip().upper().split("-")
    if len(parts) not in (3, 5) or parts[0] != "EG":
        return False
    groups = parts[1:]
    return all(len(g) == 4 and all(ch in _ALPHABET for ch in g) for g in groups)


def device_fingerprint() -> str:
    """Kennung der Hardware, nicht des Images: geklonte SD-Karten teilen machine-id
    und Einstellungen, aber nicht die Seriennummer des Raspberry Pi."""
    sources: List[str] = []
    for path in ("/sys/firmware/devicetree/base/serial-number", "/proc/device-tree/serial-number"):
        try:
            text = Path(path).read_bytes().decode("ascii", errors="ignore").strip("\x00 \n")
        except OSError:
            continue
        if text:
            sources.append(text)
            break
    if not sources:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("serial") and ":" in line:
                    serial = line.split(":", 1)[1].strip()
                    if serial.strip("0"):
                        sources.append(serial)
                    break
        except OSError:
            pass
    if not sources:
        sources.append(f"{uuid.getnode():012x}")
    return hashlib.sha256("|".join(sources).encode("utf-8")).hexdigest()[:32]


def ensure_device_code() -> bool:
    """Auf neu geflashter/geklonter Hardware einen eigenen Kopplungscode erzeugen.

    Liegt ein Code ohne ``code_device`` vor (laufendes Gerät von vor diesem Update),
    wird er übernommen, damit eine bestehende Gemeinschaft nicht abreißt.
    Gibt True zurück, wenn ein neuer Code erzeugt wurde.
    """
    fingerprint = device_fingerprint()
    with _lock:
        status, data = _load_settings_document()
        if status == "unreadable":
            return False
        stored = str(data.get("code_device") or "")
        if stored == fingerprint:
            return False
        if not stored and code_is_valid(str(data.get("invite_code") or "")):
            data["code_device"] = fingerprint
            _write_raw(data)
            return False
        # Written directly: _update() would restore the cloned code and old invite_codes.
        data["invite_code"] = generate_invite_code()
        data["invite_codes"] = []
        data["node_name"] = ""
        data["code_device"] = fingerprint
        _write_raw(data)
        return True


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
    balance_kwh: Optional[float] = None,
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
    if balance_kwh is not None:
        payload["balance_kwh"] = round(float(balance_kwh), 3)
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


def _remember_good(data: Dict[str, Any]) -> None:
    global _last_good_path, _last_good
    _last_good_path = str(SETTINGS_FILE)
    _last_good = json.loads(json.dumps(data))


def _copy_last_good() -> Optional[Dict[str, Any]]:
    if _last_good_path != str(SETTINGS_FILE) or not _last_good:
        return None
    return json.loads(json.dumps(_last_good))


def _load_settings_document() -> Tuple[str, Dict[str, Any]]:
    """missing, ok oder unreadable. Ein kaputter Zwischenstand wird nicht als leer gelesen."""
    if not SETTINGS_FILE.is_file():
        return "missing", {}
    for attempt in range(5):
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01 * (attempt + 1))
            continue
        if isinstance(raw, dict):
            _remember_good(raw)
            return "ok", raw
        time.sleep(0.01 * (attempt + 1))
    cached = _copy_last_good()
    if cached is not None:
        return "ok", cached
    return "unreadable", {}


def _read_raw() -> Dict[str, Any]:
    _status, data = _load_settings_document()
    return data


def _write_raw(data: Dict[str, Any]) -> None:
    """Ersetzt die Datei atomar. Leser sehen den alten oder den neuen Stand, nie ein halbes JSON."""
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, SETTINGS_FILE)
    _remember_good(data)


def mesh_settings() -> Dict[str, Any]:
    raw = _read_raw()
    return {
        "invite_code": str(raw.get("invite_code") or ""),
        "invite_codes": raw.get("invite_codes") if isinstance(raw.get("invite_codes"), list) else [],
        "display_name": str(raw.get("display_name") or ""),
        "node_name": str(raw.get("node_name") or ""),
        "auth_key_set": bool(str(raw.get("tailscale_auth_key") or "").strip()),
        "members": raw.get("members") if isinstance(raw.get("members"), dict) else {},
        "member_admin": raw.get("member_admin") if isinstance(raw.get("member_admin"), dict) else {},
        "removed_members": raw.get("removed_members") if isinstance(raw.get("removed_members"), list) else [],
        "commands": raw.get("commands") if isinstance(raw.get("commands"), list) else [],
    }


def _merge_history(older: Any, newer: Any, limit: int = 360) -> List[Dict[str, Any]]:
    """Beide Verläufe behalten. Ein Punkt ohne Messwert löscht den gespeicherten Wert nicht."""
    by_ts: Dict[float, Dict[str, Any]] = {}
    for source in (older, newer):
        if not isinstance(source, list):
            continue
        for point in source:
            if not isinstance(point, dict):
                continue
            try:
                stamp = round(float(point.get("ts")), 1)
            except (TypeError, ValueError):
                continue
            merged = by_ts.get(stamp, {"ts": stamp})
            for field in ("feed_in_kw", "soc_percent"):
                if point.get(field) is not None:
                    merged[field] = point.get(field)
            by_ts[stamp] = merged
    ordered = [by_ts[stamp] for stamp in sorted(by_ts)]
    if limit > 0:
        return ordered[-limit:]
    return ordered


def _keep_recorded_stats(baseline: Dict[str, Any], data: Dict[str, Any]) -> None:
    """Ein dünner neuer Stand darf Code, Mitglieder und schon geschriebene Statistik nicht löschen."""
    if not isinstance(baseline, dict) or not isinstance(data, dict):
        return
    if str(baseline.get("invite_code") or "") and not str(data.get("invite_code") or ""):
        data["invite_code"] = baseline.get("invite_code")
    old_codes = baseline.get("invite_codes")
    if isinstance(old_codes, list) and old_codes and not data.get("invite_codes"):
        data["invite_codes"] = list(old_codes)
    removed = {
        str(item).strip()
        for item in (data.get("removed_members") or [])
        if str(item).strip()
    }
    old_members = baseline.get("members") if isinstance(baseline.get("members"), dict) else {}
    members = data.get("members") if isinstance(data.get("members"), dict) else None
    if members is None:
        if old_members:
            data["members"] = json.loads(json.dumps(old_members))
        return
    for key, old in old_members.items():
        if not isinstance(old, dict):
            continue
        if key in removed or str(old.get("name") or "") in removed:
            continue
        new = members.get(key)
        if not isinstance(new, dict):
            members[key] = json.loads(json.dumps(old))
            continue
        merged = _merge_history(old.get("history"), new.get("history"))
        if merged:
            new["history"] = merged
        for field in ("feed_in_kwh", "soc_percent", "balance_kwh", "alias", "ask_ct_kwh", "grid_cost_ct_kwh"):
            if new.get(field) is None and old.get(field) is not None:
                new[field] = old.get(field)
        old_connected = old.get("connected_kwh")
        new_connected = new.get("connected_kwh")
        if isinstance(old_connected, (int, float)) and (
            not isinstance(new_connected, (int, float)) or float(new_connected) < float(old_connected)
        ):
            new["connected_kwh"] = old_connected
    data["members"] = members
    old_admin = baseline.get("member_admin") if isinstance(baseline.get("member_admin"), dict) else {}
    admin = data.get("member_admin") if isinstance(data.get("member_admin"), dict) else None
    if admin is None:
        if old_admin:
            data["member_admin"] = json.loads(json.dumps(old_admin))
        return
    for key, value in old_admin.items():
        if key not in admin and key not in removed:
            admin[key] = value
    data["member_admin"] = admin


def _update(mutator) -> Dict[str, Any]:
    with _lock:
        prior = _copy_last_good()
        status, data = _load_settings_document()
        if status == "unreadable":
            # Nicht mit einem leeren Dict überschreiben, sonst sind Code und Mitglieder weg.
            return prior or {}
        if prior:
            _keep_recorded_stats(prior, data)
        baseline = json.loads(json.dumps(data))
        mutator(data)
        _keep_recorded_stats(baseline, data)
        _write_raw(data)
        return data


def set_invite_code(code: str) -> str:
    text = code.strip().upper()
    if not code_is_valid(text):
        raise ValueError("Code hat die Form EG-XXXX-XXXX-XXXX-XXXX.")

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


def night_balance_kwh_from_state(state: Dict[str, Any]) -> Optional[float]:
    """Schon gerechnete Tagesbilanz in kWh.

    Der Steuerungsalgorithmus speichert Produktion, Tagesverbrauch und die
    Differenz in energy_budget_snapshots[0].day_balance.balance_kwh.
    Negativ ist die Fehlmenge, null oder positiv heißt die Bilanz reicht.
    """
    if not isinstance(state, dict):
        return None
    snaps = state.get("energy_budget_snapshots")
    if not isinstance(snaps, list) or not snaps or not isinstance(snaps[0], dict):
        return None
    day = snaps[0].get("day_balance")
    if not isinstance(day, dict):
        return None
    raw = day.get("balance_kwh")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return round(float(raw), 3)


def local_night_balance_kwh() -> Optional[float]:
    """Liest nur das gespeicherte Ergebnis. Die Bilanz wird hier nicht neu gerechnet."""
    try:
        from control_algorithm.config import STATE_PATH

        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return night_balance_kwh_from_state(raw if isinstance(raw, dict) else {})


def _de_kwh(value: float) -> str:
    text = f"{abs(float(value)):.3f}".rstrip("0").rstrip(".")
    if text in ("", "-"):
        text = "0"
    return text.replace(".", ",")


def outlook_line(balance_kwh: Any) -> str:
    """Statuszeile zur gespeicherten Tagesbilanz. Ohne Zahl kein erfundener Bedarf."""
    if isinstance(balance_kwh, bool) or not isinstance(balance_kwh, (int, float)):
        return "Nachbar hat noch keinen Wert gemeldet."
    value = round(float(balance_kwh), 3)
    shown = _de_kwh(value)
    if value < 0:
        return (
            "Nachbar wird nicht über die Nacht kommen, er würde "
            f"{shown} kWh benötigen."
        )
    sign = "+" if value > 0 else ""
    return f"Nachbar kommt über die Nacht, die Bilanz ist {sign}{shown} kWh."


def balance_lamp(balance_kwh: Any) -> Optional[Dict[str, str]]:
    """Ampel zur gespeicherten Tagesbilanz in kWh.

    Über +1 eine grüne Lampe, von −1 bis +1 eine hellgrüne Lampe.
    Unter −1 füllt das Statuskästchen hellrot, ohne rote Lampe.
    """
    if isinstance(balance_kwh, bool) or not isinstance(balance_kwh, (int, float)):
        return None
    value = float(balance_kwh)
    if value > 1:
        return {"level": "green", "fill": False}
    if value >= -1:
        return {"level": "light-green", "fill": False}
    return {"level": "short", "fill": True}


_REPORTED_CHOICES = {
    "neighbor_trade": ("buy", "sell", "both"),
    "neighbor_storage": ("yes", "no"),
    "neighbor_pv": ("yes", "no"),
}
_REPORTED_KW = ("max_zukauf_kw", "max_verkauf_kw")
_REPORTED_FLAGS = ("share_without_production", "only_if_enough_tomorrow")


def reported_member_settings(item: Dict[str, Any]) -> Dict[str, Any]:
    """Nur das, was dieses Mitglied selbst mitgeschickt hat. Sonst leer."""
    out: Dict[str, Any] = {}
    for key, allowed in _REPORTED_CHOICES.items():
        if key not in item:
            out[key] = None
            continue
        text = str(item.get(key) or "").strip()
        out[key] = text if text in allowed else None
    for key in _REPORTED_KW:
        raw = item.get(key) if key in item else None
        out[key] = None if isinstance(raw, bool) or not isinstance(raw, (int, float)) else round(float(raw), 3)
    for key in _REPORTED_FLAGS:
        raw = item.get(key) if key in item else None
        out[key] = raw if isinstance(raw, bool) else None
    raw_balance = item.get("balance_kwh") if "balance_kwh" in item else None
    if isinstance(raw_balance, bool) or not isinstance(raw_balance, (int, float)):
        out["balance_kwh"] = None
    else:
        out["balance_kwh"] = round(float(raw_balance), 3)
    return out


def _clean_choice(raw: Any, allowed: set) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text not in allowed:
        raise ValueError("Unbekannte Auswahl.")
    return text


def _clean_kw(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError("Bitte eine Zahl in kW eintragen.") from None
    if value < 0 or value > 1000:
        raise ValueError("Leistung liegt außerhalb.")
    return round(value, 3)


def _clean_flag(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "on", "yes"}


def clean_member_admin(raw: Any) -> Dict[str, Any]:
    """Admin-Werte für eine Person. Leer bleibt leer, nichts wird aus einer Vorlage gefüllt."""
    getter = getattr(raw, "get", None)

    def pick(key: str) -> Any:
        if not callable(getter):
            return None
        return getter(key)

    return {
        "neighbor_trade": _clean_choice(pick("neighbor_trade"), {"buy", "sell", "both"}),
        "max_zukauf_kw": _clean_kw(pick("max_zukauf_kw")),
        "max_verkauf_kw": _clean_kw(pick("max_verkauf_kw")),
        "share_without_production": _clean_flag(pick("share_without_production")),
        "neighbor_night_kw": _clean_kw(pick("neighbor_night_kw")),
        "neighbor_storage": _clean_choice(pick("neighbor_storage"), {"yes", "no"}),
        "neighbor_pv": _clean_choice(pick("neighbor_pv"), {"yes", "no"}),
        "only_if_enough_tomorrow": _clean_flag(pick("only_if_enough_tomorrow")),
    }


def stored_member_admin(store: Any, name: str) -> Dict[str, Any]:
    raw = store.get(name) if isinstance(store, dict) else None
    try:
        return clean_member_admin(raw if isinstance(raw, dict) else {})
    except ValueError:
        return clean_member_admin({})


def local_name(snapshot: Optional[Dict[str, Any]] = None) -> str:
    settings = mesh_settings()
    stored = settings.get("display_name") or ""
    if stored:
        return stored
    node = settings.get("node_name") or ""
    if node:
        return node
    snap = snapshot if snapshot is not None else tailscale_snapshot()
    if snap.get("self_name"):
        return str(snap["self_name"])
    return socket.gethostname().split(".")[0]


def set_display_name(name: str) -> str:
    """Anzeigename dieser Steuerung. Leer setzt zurück auf den Geräte-/Tailscale-Namen."""
    text = str(name or "").strip()
    if len(text) > 64:
        raise ValueError("Name darf höchstens 64 Zeichen haben.")

    def mutate(data: Dict[str, Any]) -> None:
        data["display_name"] = text

    _update(mutate)
    return text


def ensure_node_name(tailscale_name: str) -> None:
    """Tailscale-Name einmal festhalten. Ein Neustart ohne Tailscale ändert ihn nicht."""
    text = str(tailscale_name or "").strip()
    if not text:
        return
    if str(mesh_settings().get("node_name") or "").strip():
        return

    def mutate(data: Dict[str, Any]) -> None:
        if not str(data.get("node_name") or "").strip():
            data["node_name"] = text

    _update(mutate)


CONNECTED_GAP_SEC = 150
HISTORY_LIMIT = 360


def append_history(previous: Any, now: float, feed_kw: Optional[float], soc: Optional[float]) -> List[Dict[str, Any]]:
    """Einen Zeitpunkt an den Verlauf dieses Geräts hängen."""
    history = [item for item in (previous or []) if isinstance(item, dict)]
    if history and abs(float(history[-1].get("ts") or 0) - float(now)) < 20:
        point = dict(history[-1])
        point["ts"] = round(float(now), 1)
        if feed_kw is not None:
            point["feed_in_kw"] = round(float(feed_kw), 3)
        if soc is not None:
            point["soc_percent"] = round(float(soc), 1)
        history[-1] = point
    else:
        point = {"ts": round(float(now), 1)}
        if feed_kw is not None:
            point["feed_in_kw"] = round(float(feed_kw), 3)
        if soc is not None:
            point["soc_percent"] = round(float(soc), 1)
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


def _same_device(item: Dict[str, Any], key: str, name: str, ip: str) -> bool:
    """Gleicher Name, auch bei anderer Großschreibung, oder dieselbe Tailscale-Adresse."""
    folded = name.casefold()
    if str(key).casefold() == folded or str(item.get("name") or "").casefold() == folded:
        return True
    return bool(ip) and str(item.get("ip") or "") == ip


def _absorb_member(keep: Dict[str, Any], extra: Dict[str, Any]) -> None:
    if not keep.get("alias") and extra.get("alias"):
        keep["alias"] = extra["alias"]
    if keep.get("ask_ct_kwh") is None and extra.get("ask_ct_kwh") is not None:
        keep["ask_ct_kwh"] = extra["ask_ct_kwh"]
    if keep.get("grid_cost_ct_kwh") is None and extra.get("grid_cost_ct_kwh") is not None:
        keep["grid_cost_ct_kwh"] = extra["grid_cost_ct_kwh"]
    if not keep.get("ip") and extra.get("ip"):
        keep["ip"] = extra["ip"]
    if keep.get("feed_in_kwh") is None and extra.get("feed_in_kwh") is not None:
        keep["feed_in_kwh"] = extra["feed_in_kwh"]
    if keep.get("balance_kwh") is None and extra.get("balance_kwh") is not None:
        keep["balance_kwh"] = extra["balance_kwh"]
    old_connected = keep.get("connected_kwh")
    extra_connected = extra.get("connected_kwh")
    if isinstance(extra_connected, (int, float)) and (
        not isinstance(old_connected, (int, float)) or float(extra_connected) > float(old_connected)
    ):
        keep["connected_kwh"] = extra_connected
    keep["history"] = _merge_history(keep.get("history"), extra.get("history"))
    if float(extra.get("seen") or 0) > float(keep.get("seen") or 0):
        keep["seen"] = extra.get("seen")
        if extra.get("feed_in_kw") is not None:
            keep["feed_in_kw"] = extra.get("feed_in_kw")
        if extra.get("soc_percent") is not None:
            keep["soc_percent"] = extra.get("soc_percent")
        if extra.get("balance_kwh") is not None:
            keep["balance_kwh"] = extra.get("balance_kwh")


def _collapse_into(members: Dict[str, Any], name: str, ip: str) -> str:
    """Einen Schlüssel behalten. Der Neustart unter anderem Namen legt keinen zweiten an."""
    matches = [
        key
        for key, item in members.items()
        if isinstance(item, dict) and _same_device(item, str(key), name, ip)
    ]
    if not matches:
        return name

    def rank(key: str) -> Tuple[int, int, int, float, int]:
        item = members.get(key) or {}
        return (
            0 if str(key).casefold() == name.casefold() else 1,
            0 if item.get("ip") else 1,
            0 if item.get("alias") else 1,
            -float(item.get("seen") or 0),
            0 if key == name else 1,
        )

    keep = sorted(matches, key=rank)[0]
    for extra_key in matches:
        if extra_key == keep:
            continue
        extra = members.get(extra_key)
        if isinstance(extra, dict):
            _absorb_member(members[keep], extra)
        members.pop(extra_key, None)
    members[keep]["name"] = keep
    return str(keep)


def collapse_stored_duplicates() -> None:
    with _lock:
        data = _read_raw()
        members = data.get("members")
        if not isinstance(members, dict) or not members:
            return
        before = list(members.keys())
        for key in before:
            item = members.get(key)
            if not isinstance(item, dict):
                continue
            _collapse_into(members, str(item.get("name") or key), str(item.get("ip") or ""))
        if list(members.keys()) == before:
            return
        data["members"] = members
        _write_raw(data)


def _remember_member(
    name: str,
    ip: str,
    feed: Optional[float],
    soc: Optional[float],
    energy_kwh: Optional[float] = None,
    balance_kwh: Optional[float] = None,
) -> None:
    now = time.time()

    def mutate(data: Dict[str, Any]) -> None:
        members = data.get("members")
        if not isinstance(members, dict):
            members = {}
        key = _collapse_into(members, name, ip)
        prev = members.get(key) if isinstance(members.get(key), dict) else {}
        current_kw = feed if feed is not None else prev.get("feed_in_kw")
        members[key] = {
            "name": key,
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
            "balance_kwh": balance_kwh if balance_kwh is not None else prev.get("balance_kwh"),
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
    if self_name and str(item.get("name") or "").casefold() == self_name.casefold():
        return False
    if self_ip and item.get("ip") == self_ip:
        return False
    return True


def member_is_feeding(feed: Any, fresh: bool) -> bool:
    return bool(fresh and isinstance(feed, (int, float)) and float(feed) > 0)


def peer_feed_rows() -> List[Dict[str, Any]]:
    """Einspeisung der anderen Mitglieder. Das eigene Haus zählt nicht mit."""
    data = mesh_settings()
    now = time.time()
    self_names = {socket.gethostname().split(".")[0].casefold()}
    for stored in (data.get("display_name"), data.get("node_name")):
        if stored:
            self_names.add(str(stored).casefold())
    rows: List[Dict[str, Any]] = []
    for item in (data.get("members") or {}).values():
        if not isinstance(item, dict) or not item.get("seen"):
            continue
        if str(item.get("name") or "").casefold() in self_names:
            continue
        seen = float(item.get("seen") or 0)
        rows.append(
            {
                "name": item.get("name") or "",
                "alias": item.get("alias") or "",
                "feed_in_kw": item.get("feed_in_kw"),
                "fresh": (now - seen) <= FRESH_SEC,
            }
        )
    return rows


def peer_surplus_kw(rows: Optional[List[Dict[str, Any]]] = None) -> float:
    """Summe der frischen Einspeisung, in kW. Veraltete Meldungen zählen nicht."""
    total = 0.0
    for row in rows if rows is not None else peer_feed_rows():
        feed = row.get("feed_in_kw")
        if row.get("fresh") and isinstance(feed, (int, float)) and float(feed) > 0:
            total += float(feed)
    return round(total, 3)


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


def save_member_admin(name: str, raw: Any) -> Dict[str, Any]:
    """Einstellungen des Admins für genau dieses Mitglied. Nicht die Meldung des Nachbarn."""
    peer = name.strip()
    if not peer:
        raise ValueError("Steuerung fehlt.")
    if peer == local_name():
        raise ValueError("Einstellungen gelten für die anderen Geräte.")
    cleaned = clean_member_admin(raw)

    def mutate(data: Dict[str, Any]) -> None:
        members = data.get("members")
        if not isinstance(members, dict) or not isinstance(members.get(peer), dict):
            raise ValueError("Mitglied ist nicht in der Gemeinschaft.")
        store = data.get("member_admin")
        if not isinstance(store, dict):
            store = {}
        store[peer] = cleaned
        data["member_admin"] = store

    _update(mutate)
    return cleaned


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
        store = data.get("member_admin")
        if isinstance(store, dict):
            store.pop(peer, None)
            data["member_admin"] = store

    _update(mutate)


def _removed_names() -> set:
    names = mesh_settings().get("removed_members")
    if not isinstance(names, list):
        return set()
    return {str(item).strip().casefold() for item in names if str(item).strip()}


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
    folded = name.casefold()
    for key, item in members.items():
        if not isinstance(item, dict) or not item.get("seen"):
            continue
        same = str(key).casefold() == folded or str(item.get("name") or "").casefold() == folded
        if same or str(item.get("ip") or "") == source_ip:
            return str(item.get("ip") or "") == source_ip
    return False


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
    if sender and sender.casefold() in _removed_names():
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
        balance = payload.get("balance_kwh")
        _remember_member(
            name,
            source_ip,
            None if feed is None else float(feed),
            None if soc is None else float(soc),
            None if energy is None else float(energy),
            None if balance is None else float(balance),
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


def _close_socket() -> None:
    """Alten Listener freigeben, damit nach IP-Wechsel neu gebunden werden kann."""
    global _sock, _sock_ip, _listen_gen
    sock = _sock
    _sock = None
    _sock_ip = ""
    _listen_gen += 1
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _ensure_socket() -> None:
    """UDP-Socket an die aktuelle Tailscale-Adresse binden; bei IP-Wechsel neu aufbauen."""
    global _sock, _sock_ip, _listen_gen
    snap = tailscale_snapshot()
    bind_ip = str(snap.get("self_ip") or "")
    if not bind_ip:
        # Kurzer Tailscale-Ausfall: bestehenden Socket behalten.
        return
    if _sock is not None and _sock_ip == bind_ip:
        return
    _close_socket()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_ip, LISTEN_PORT))
    except OSError as exc:
        sock.close()
        print(f"[Energiegemeinschaft] Socket-Bind fehlgeschlagen ({bind_ip}:{LISTEN_PORT}): {exc}")
        return
    _sock = sock
    _sock_ip = bind_ip
    gen = _listen_gen
    threading.Thread(target=_listen_loop, args=(sock, gen), name="eg-mesh-listen", daemon=True).start()


def cycle_once() -> None:
    global _last_tick_ok
    _ensure_socket()
    code = mesh_settings().get("invite_code") or ""
    if not code or _sock is None:
        return
    snap = tailscale_snapshot()
    ensure_node_name(str(snap.get("self_name") or ""))
    name = local_name(snap)
    feed, soc, energy = local_reading()
    balance = local_night_balance_kwh()
    self_ip = str(snap.get("self_ip") or "")
    _remember_member(name, self_ip, feed, soc, energy, balance)
    packet = feed_packet(name, feed, soc, code, energy, balance)
    settings = mesh_settings()
    sent: set[str] = set()
    # Bekannte Mitglieder immer unter der gespeicherten Adresse anstoßen –
    # unabhängig davon, ob Tailscale sie gerade als online meldet.
    for item in (settings.get("members") or {}).values():
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "")
        if not ip or ip == self_ip or ip in sent:
            continue
        _send(packet, ip)
        sent.add(ip)
    # Online-Peers zusätzlich: erster Kontakt oder geänderte Tailscale-IP.
    for peer in snap.get("peers") or []:
        ip = str(peer.get("ip") or "")
        if not ip or ip == self_ip or ip in sent:
            continue
        if peer.get("online"):
            _send(packet, ip)
            sent.add(ip)
    _last_tick_ok = time.time()


def _listen_loop(sock: socket.socket, gen: int) -> None:
    while gen == _listen_gen:
        try:
            raw, addr = sock.recvfrom(4096)
        except OSError:
            if gen != _listen_gen:
                return
            # Socket tot (z. B. nach close/IP-Wechsel): freigeben, Takt baut neu auf.
            if _sock is sock:
                _close_socket()
            return
        try:
            handle_datagram(raw, addr[0])
        except Exception as exc:
            print(f"[Energiegemeinschaft] Paketfehler: {exc}")


def _tick_loop() -> None:
    while True:
        try:
            cycle_once()
        except Exception as exc:
            print(f"[Energiegemeinschaft] Taktfehler: {exc}")
        time.sleep(TICK_SEC)


def start_mesh() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    try:
        if ensure_device_code():
            print("[Energiegemeinschaft] neue Hardware erkannt – eigener Kopplungscode erzeugt")
    except OSError as exc:
        print(f"[Energiegemeinschaft] Kopplungscode-Prüfung: {exc}")
    collapse_stored_duplicates()
    threading.Thread(target=_tick_loop, name="eg-mesh-tick", daemon=True).start()


def public_status() -> Dict[str, Any]:
    collapse_stored_duplicates()
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
                "balance_kwh": item.get("balance_kwh"),
                "outlook_text": outlook_line(item.get("balance_kwh")),
                "outlook_lamp": balance_lamp(item.get("balance_kwh")),
                "reported": reported_member_settings(item if isinstance(item, dict) else {}),
                "admin": stored_member_admin(settings.get("member_admin"), str(item.get("name") or "")),
                "connected_kwh": item.get("connected_kwh") or 0,
                "soc_percent": item.get("soc_percent"),
                "age_sec": age,
                "age_text": format_stillzeit(age, code_mismatch),
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
        "display_name": str(settings.get("display_name") or ""),
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
