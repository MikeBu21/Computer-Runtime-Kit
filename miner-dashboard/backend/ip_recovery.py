#!/usr/bin/env python3
"""MAC-basierte IP-Wiederherstellung für Miner."""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
MINERS_PATH = BACKEND_DIR / "miners.json"
CONFIG_PATH = BACKEND_DIR / "control_algorithm_config.json"
STATE_PATH = BACKEND_DIR / "control_algorithm_state.json"
STATUS_PATH = BACKEND_DIR / "miner_status.json"
EM_DIR = BACKEND_DIR / "energy_monitor"
LATCHES_PATH = EM_DIR / "control_latches.json"
LAST_ACTIONS_PATH = EM_DIR / "last_actions.json"
NOTIFY_PATH = BACKEND_DIR / "ip_recovery_notifications.json"
LOCK_PATH = BACKEND_DIR / ".ip_recovery.lock"


def _notify_path() -> Path:
    try:
        from ram_cache import backend_ephemeral_path, is_enabled

        if is_enabled():
            return backend_ephemeral_path(BACKEND_DIR, "ip_recovery_notifications.json")
    except Exception:
        pass
    return NOTIFY_PATH

IMMEDIATE_SCAN_COOLDOWN_SEC = 300  # 5 min
DAILY_SCAN_INTERVAL_SEC = 86400  # 24 h
MAC_CAPTURE_COOLDOWN_SEC = 60  # ARP/SSH MAC-Lernen pro Miner
# Erst nach N Fehlversuchen hintereinander (alle ~5 Min) nachfragen
MISSING_CONFIRM_STREAK = 20
RECOVERY_STATES = {"ok", "searching", "missing", "awaiting_user", "removed"}

_mac_re = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)
_rate_lock = threading.Lock()
_last_immediate_scan: Dict[str, float] = {}
_last_mac_capture: Dict[str, float] = {}


@dataclass
class RecoverResult:
    success: bool
    old_ip: str
    new_ip: Optional[str] = None
    action: str = "none"  # none | migrated | missing | searching | skipped
    message: str = ""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_mac(mac: Any) -> Optional[str]:
    if not mac:
        return None
    text = str(mac).strip().lower().replace("-", ":")
    if not _mac_re.match(text):
        # aa:bb:cc:dd:ee:ff from raw hex
        hex_only = re.sub(r"[^0-9a-f]", "", text)
        if len(hex_only) == 12:
            text = ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
        else:
            return None
    parts = text.split(":")
    return ":".join(p.zfill(2) for p in parts)


@contextmanager
def _recovery_lock() -> Iterator[None]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield


def load_miners() -> List[Dict[str, Any]]:
    if not MINERS_PATH.is_file():
        return []
    try:
        data = json.loads(MINERS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_miners(miners: List[Dict[str, Any]]) -> None:
    MINERS_PATH.write_text(
        json.dumps(miners, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _find_miner(miners: List[Dict[str, Any]], ip: str) -> Optional[Dict[str, Any]]:
    ip = str(ip or "").strip()
    for m in miners:
        if str(m.get("ip") or "").strip() == ip:
            return m
    return None


def _find_miner_by_mac(miners: List[Dict[str, Any]], mac: str) -> Optional[Dict[str, Any]]:
    mac_n = normalize_mac(mac)
    if not mac_n:
        return None
    for m in miners:
        if normalize_mac(m.get("mac")) == mac_n:
            return m
    return None


def detect_local_subnets() -> List[str]:
    """IPv4-CIDRs aller aktiven Interfaces (router-unabhängig)."""
    subnets: List[str] = []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global", "up"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            for token in parts:
                if "/" not in token or token.count(".") != 3:
                    continue
                try:
                    iface = ipaddress.IPv4Interface(token)
                except ValueError:
                    continue
                net = iface.network
                if net.is_loopback or net.is_link_local:
                    continue
                cidr = str(net)
                if cidr not in subnets:
                    subnets.append(cidr)
    except Exception as exc:
        logger.warning("Subnet-Erkennung fehlgeschlagen: %s", exc)
    return subnets


def read_mac_via_ssh(ip: str, username: str, password: str) -> Optional[str]:
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=username, password=password, timeout=8)
        try:
            cmd = (
                "for i in eth0 wlan0 enp0s3 eno1; do "
                "[ -f /sys/class/net/$i/address ] && cat /sys/class/net/$i/address && break; "
                "done"
            )
            _, stdout, _ = ssh.exec_command(cmd, timeout=8)
            mac = normalize_mac(stdout.read().decode().strip())
            return mac
        finally:
            ssh.close()
    except Exception as exc:
        logger.debug("MAC per SSH von %s nicht lesbar: %s", ip, exc)
        return None


def _parse_neigh_line(line: str) -> Tuple[Optional[str], Optional[str]]:
    parts = line.split()
    if len(parts) < 5:
        return None, None
    ip = parts[0]
    lladdr_idx = parts.index("lladdr") if "lladdr" in parts else -1
    if lladdr_idx < 0 or lladdr_idx + 1 >= len(parts):
        return None, None
    mac = normalize_mac(parts[lladdr_idx + 1])
    if not mac or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return ip, None
    return ip, mac


def _parse_ip_neigh() -> Dict[str, str]:
    """MAC -> IP aus ARP-Nachbartabelle."""
    table: Dict[str, str] = {}
    try:
        out = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        for line in (out.stdout or "").splitlines():
            ip, mac = _parse_neigh_line(line)
            if mac and ip:
                table[mac] = ip
    except Exception as exc:
        logger.debug("ip neigh fehlgeschlagen: %s", exc)
    return table


def read_mac_via_arp(ip: str) -> Optional[str]:
    """MAC zu einer IP aus der ARP-Tabelle (Ping triggert Eintrag)."""
    ip = str(ip or "").strip()
    if not ip:
        return None
    try:
        subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ip", "neigh", "show", ip],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (out.stdout or "").splitlines():
            if not line.startswith(ip):
                continue
            _, mac = _parse_neigh_line(line)
            if mac:
                return mac
    except Exception as exc:
        logger.debug("ARP-MAC für %s fehlgeschlagen: %s", ip, exc)
    # Fallback: globale Nachbartabelle
    for mac, mapped_ip in _parse_ip_neigh().items():
        if mapped_ip == ip:
            return mac
    return None


def discover_mac_for_ip(
    ip: str,
    username: str = "",
    password: str = "",
) -> Optional[Tuple[str, str]]:
    """MAC zu IP ermitteln: zuerst ARP (ohne Login), sonst SSH."""
    mac = read_mac_via_arp(ip)
    if mac:
        return mac, "arp"
    if username and password:
        mac = read_mac_via_ssh(ip, username, password)
        if mac:
            return mac, "ssh"
    return None


def _ping_sweep_subnet(cidr: str, *, timeout_per_host: float = 0.15) -> None:
    """Best-effort Ping-Sweep um ARP-Einträge zu füllen."""
    try:
        net = ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return
    hosts = list(net.hosts())
    if len(hosts) > 512:
        hosts = hosts[:512]
    for addr in hosts:
        ip = str(addr)
        try:
            subprocess.run(
                ["ping", "-c", "1", "-W", "1", ip],
                capture_output=True,
                timeout=timeout_per_host + 1,
                check=False,
            )
        except Exception:
            pass


def build_arp_table(subnets: Optional[List[str]] = None) -> Dict[str, str]:
    subnets = subnets or detect_local_subnets()
    for cidr in subnets:
        _ping_sweep_subnet(cidr)
    return _parse_ip_neigh()


def resolve_ip_by_mac(mac: str, subnets: Optional[List[str]] = None) -> Optional[str]:
    mac_n = normalize_mac(mac)
    if not mac_n:
        return None
    table = build_arp_table(subnets)
    return table.get(mac_n)


def _rekey_mapping(data: Any, old_key: str, new_key: str) -> Any:
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            nk = new_key if str(k) == old_key else k
            out[nk] = _rekey_mapping(v, old_key, new_key)
        return out
    if isinstance(data, list):
        return [_rekey_mapping(item, old_key, new_key) for item in data]
    if isinstance(data, str) and data == old_key:
        return new_key
    return data


def _rekey_json_file(path: Path, old_ip: str, new_ip: str) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    updated = _rekey_mapping(data, old_ip, new_ip)
    if updated != data:
        path.write_text(
            json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def migrate_device_ip(old_ip: str, new_ip: str) -> bool:
    """Atomisch IP in allen relevanten Stores aktualisieren."""
    old_ip = str(old_ip or "").strip()
    new_ip = str(new_ip or "").strip()
    if not old_ip or not new_ip or old_ip == new_ip:
        return False

    with _recovery_lock():
        miners = load_miners()
        miner = _find_miner(miners, old_ip)
        if not miner:
            return False

        miner["ip"] = new_ip
        miner["last_seen_ip"] = new_ip
        miner["recovery_state"] = "ok"
        miner["recovery_updated_at"] = _now_iso()
        miner["recovery_miss_streak"] = 0
        miner.pop("user_confirmed_exists", None)
        save_miners(miners)

        if CONFIG_PATH.is_file():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for dev in cfg.get("devices") or []:
                    if str(dev.get("ip") or "").strip() == old_ip:
                        dev["ip"] = new_ip
                CONFIG_PATH.write_text(
                    json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("Algo-Config IP-Migration fehlgeschlagen: %s", exc)

        if STATE_PATH.is_file():
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                state = _rekey_mapping(state, old_ip, new_ip)
                STATE_PATH.write_text(
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("Algo-State IP-Migration fehlgeschlagen: %s", exc)

        if STATUS_PATH.is_file():
            try:
                entries = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict) and str(entry.get("ip")) == old_ip:
                            entry["ip"] = new_ip
                    STATUS_PATH.write_text(
                        json.dumps(entries, indent=4, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass

        _rekey_json_file(LATCHES_PATH, old_ip, new_ip)
        _rekey_json_file(LAST_ACTIONS_PATH, old_ip, new_ip)

    label = miner.get("note") or miner.get("model") or old_ip
    mac = miner.get("mac") or "—"
    queue_notification(
        "ip_changed",
        {
            "label": label,
            "old_ip": old_ip,
            "new_ip": new_ip,
            "mac": mac,
            "message": (
                f"Miner {label}: IP geändert {old_ip} → {new_ip} (MAC {mac})"
            ),
        },
    )
    logger.info("IP migriert: %s → %s (%s)", old_ip, new_ip, label)
    return True


def queue_notification(kind: str, payload: Dict[str, Any]) -> None:
    """Benachrichtigungen für Telegram-Bot (async) zwischenspeichern."""
    entry = {"kind": kind, "payload": payload, "created_at": _now_iso()}
    items: List[Dict[str, Any]] = []
    notify_path = _notify_path()
    if notify_path.is_file():
        try:
            raw = json.loads(notify_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = raw
        except Exception:
            items = []
    items.append(entry)
    items = items[-50:]
    notify_path.write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def pop_notifications() -> List[Dict[str, Any]]:
    notify_path = _notify_path()
    if not notify_path.is_file():
        return []
    try:
        items = json.loads(notify_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    notify_path.write_text("[]\n", encoding="utf-8")
    return items if isinstance(items, list) else []


def store_mac_on_miner(ip: str, mac: str, source: str = "ssh") -> None:
    mac_n = normalize_mac(mac)
    if not mac_n:
        return
    with _recovery_lock():
        miners = load_miners()
        miner = _find_miner(miners, ip)
        if not miner:
            return
        if normalize_mac(miner.get("mac")) == mac_n:
            return
        miner["mac"] = mac_n
        miner["mac_source"] = source
        miner.setdefault("recovery_state", "ok")
        miner.setdefault("last_seen_ip", ip)
        miner["recovery_updated_at"] = _now_iso()
        save_miners(miners)


def _should_mac_capture(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        last = _last_mac_capture.get(ip, 0)
        if now - last < MAC_CAPTURE_COOLDOWN_SEC:
            return False
        _last_mac_capture[ip] = now
        return True


def discover_mac_with_retries(
    ip: str,
    username: str = "",
    password: str = "",
    *,
    retries: int = 4,
    delay_sec: float = 0.75,
) -> Optional[Tuple[str, str]]:
    """MAC zu IP mit mehreren Versuchen (ARP, dann SSH)."""
    for attempt in range(max(1, retries)):
        found = discover_mac_for_ip(ip, username, password)
        if found:
            return found
        if attempt + 1 < retries:
            time.sleep(delay_sec)
    return None


def onboard_miner_entry(ip: str) -> Optional[str]:
    """MAC für einen bestehenden Miner-Eintrag ermitteln und speichern."""
    ip = str(ip or "").strip()
    if not ip:
        return None
    miner = _find_miner(load_miners(), ip)
    if not miner:
        return None
    if miner.get("mac"):
        return normalize_mac(miner.get("mac"))
    return capture_mac_if_missing(
        ip,
        miner.get("username") or "root",
        miner.get("password") or "",
        force=True,
    )


def register_miner(miner: Dict[str, Any]) -> Dict[str, Any]:
    """Miner speichern und MAC sofort automatisch aus IP ermitteln (ARP → SSH)."""
    ip = str(miner.get("ip") or "").strip()
    if not ip:
        raise ValueError("Miner-IP fehlt")

    entry = dict(miner)
    entry.setdefault("recovery_state", "ok")
    entry.setdefault("last_seen_ip", ip)
    entry.setdefault("auto", False)
    entry.setdefault("note", entry.get("note") or "")

    with _recovery_lock():
        miners = load_miners()
        existing = _find_miner(miners, ip)
        if existing:
            for key, val in entry.items():
                if val is not None and val != "":
                    existing[key] = val
            entry = existing
        else:
            miners.append(entry)
        save_miners(miners)

    if not entry.get("mac"):
        found = discover_mac_with_retries(
            ip,
            entry.get("username") or "root",
            entry.get("password") or "",
        )
        if found:
            mac, source = found
            store_mac_on_miner(ip, mac, source)
            entry["mac"] = mac
            entry["mac_source"] = source
        else:
            logger.warning(
                "MAC für neuen Miner %s konnte beim Hinzufügen nicht ermittelt werden",
                ip,
            )

    return entry


def capture_mac_if_missing(
    ip: str,
    username: str = "",
    password: str = "",
    *,
    force: bool = False,
) -> Optional[str]:
    """MAC automatisch lernen (ARP zuerst, dann SSH). Gibt MAC zurück wenn neu gespeichert."""
    ip = str(ip or "").strip()
    if not ip:
        return None
    miners = load_miners()
    miner = _find_miner(miners, ip)
    if not miner:
        return None
    if miner.get("mac"):
        return normalize_mac(miner.get("mac"))
    if not force and not _should_mac_capture(ip):
        return None
    found = discover_mac_with_retries(ip, username, password, retries=1 if force else 2)
    if not found:
        return None
    mac, source = found
    store_mac_on_miner(ip, mac, source)
    logger.info("MAC für %s ermittelt: %s (via %s)", ip, mac, source)
    return mac


def ensure_all_miner_macs() -> int:
    """Für alle Miner ohne MAC: ARP/SSH-Versuch. Returns: Anzahl neu gespeicherter MACs."""
    count = 0
    for miner in load_miners():
        ip = str(miner.get("ip") or "").strip()
        if not ip or miner.get("mac"):
            continue
        mac = capture_mac_if_missing(
            ip,
            miner.get("username") or "root",
            miner.get("password") or "",
        )
        if mac:
            count += 1
    return count


def _should_immediate_scan(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        last = _last_immediate_scan.get(ip, 0)
        if now - last < IMMEDIATE_SCAN_COOLDOWN_SEC:
            return False
        _last_immediate_scan[ip] = now
        return True


def _should_daily_scan(miner: Dict[str, Any]) -> bool:
    raw = miner.get("last_recovery_scan_at") or miner.get("recovery_updated_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    return datetime.now() - last >= timedelta(seconds=DAILY_SCAN_INTERVAL_SEC)


def _miss_streak(miner: Dict[str, Any]) -> int:
    try:
        return max(0, int(miner.get("recovery_miss_streak") or 0))
    except (TypeError, ValueError):
        return 0


def clear_recovery_miss(ip: str) -> None:
    """Miss-Streak zurücksetzen wenn Gerät wieder gefunden / erreichbar."""
    ip = str(ip or "").strip()
    if not ip:
        return
    with _recovery_lock():
        miners = load_miners()
        m = _find_miner(miners, ip)
        if not m:
            return
        changed = False
        if _miss_streak(m) != 0:
            m["recovery_miss_streak"] = 0
            changed = True
        if m.get("recovery_state") == "searching":
            m["recovery_state"] = "ok"
            m["recovery_updated_at"] = _now_iso()
            changed = True
        if changed:
            save_miners(miners)


def mark_miner_reachable(ip: str) -> None:
    """SSH/ARP wieder ok → Recovery vollständig zurücksetzen."""
    ip = str(ip or "").strip()
    if not ip:
        return
    with _recovery_lock():
        miners = load_miners()
        m = _find_miner(miners, ip)
        if not m:
            return
        state = str(m.get("recovery_state") or "ok")
        needs = (
            state != "ok"
            or _miss_streak(m) != 0
            or "user_confirmed_exists" in m
        )
        if not needs:
            return
        m["recovery_state"] = "ok"
        m["recovery_miss_streak"] = 0
        m.pop("user_confirmed_exists", None)
        m["last_seen_ip"] = ip
        m["recovery_updated_at"] = _now_iso()
        save_miners(miners)


def mark_miner_missing(miner: Dict[str, Any], *, notify: bool = True) -> None:
    ip = str(miner.get("ip") or "").strip()
    if not ip:
        return
    with _recovery_lock():
        miners = load_miners()
        m = _find_miner(miners, ip)
        if not m:
            return
        prev = m.get("recovery_state")
        m["recovery_state"] = "missing"
        m["recovery_updated_at"] = _now_iso()
        m["last_recovery_scan_at"] = _now_iso()
        m["recovery_miss_streak"] = max(
            _miss_streak(m), MISSING_CONFIRM_STREAK
        )
        save_miners(miners)

    if notify and prev != "missing":
        label = miner.get("note") or miner.get("model") or ip
        mac = miner.get("mac") or "unbekannt"
        queue_notification(
            "device_missing",
            {
                "ip": ip,
                "mac": mac,
                "label": label,
                "message": (
                    f"Miner {label} ({ip}, MAC {mac}) nach "
                    f"{MISSING_CONFIRM_STREAK}× Prüfung nicht erreichbar.\n"
                    "Existiert das Gerät noch?"
                ),
            },
        )


def record_recovery_miss(
    miner: Dict[str, Any],
    *,
    daily: bool = False,
) -> RecoverResult:
    """Fehlversuch zählen; Nachfrage erst ab MISSING_CONFIRM_STREAK.

    - Nach „Ja, existiert“ (awaiting_user): still suchen, nie erneut fragen.
    - Sofort-Scans (~5 Min): Streak hochzählen, ab 20× → missing + Frage.
    - Daily-Scan: kein erneutes Eskalieren / keine Telegram-Frage.
    """
    ip = str(miner.get("ip") or "").strip()
    label = miner.get("note") or miner.get("model") or ip
    mac = miner.get("mac") or "unbekannt"
    if not ip:
        return RecoverResult(False, ip, action="skipped", message="Keine IP")

    with _recovery_lock():
        miners = load_miners()
        m = _find_miner(miners, ip)
        if not m:
            return RecoverResult(False, ip, action="skipped", message="Nicht gefunden")

        state = str(m.get("recovery_state") or "ok")
        confirmed = bool(m.get("user_confirmed_exists"))
        m["last_recovery_scan_at"] = _now_iso()
        m["recovery_updated_at"] = _now_iso()

        # User hat bestätigt: nur still weitersuchen, State behalten
        if state == "awaiting_user" and confirmed:
            save_miners(miners)
            return RecoverResult(
                False,
                ip,
                action="missing",
                message=(
                    f"{label}: MAC {mac} nicht gefunden "
                    "(bestätigt – tägliche Suche ohne Nachfrage)"
                ),
            )

        # Schon missing: kein Re-Notify, Timestamps aktualisieren
        if state == "missing":
            save_miners(miners)
            return RecoverResult(
                False,
                ip,
                action="missing",
                message=f"{label}: weiterhin nicht gefunden",
            )

        # Daily ohne Bestätigung: nicht eskalieren (nur Sofort-Streak zählt)
        if daily:
            if state == "ok":
                m["recovery_state"] = "searching"
            save_miners(miners)
            return RecoverResult(
                False,
                ip,
                action="missing",
                message=f"{label}: Daily-Scan ohne Treffer",
            )

        streak = _miss_streak(m) + 1
        m["recovery_miss_streak"] = streak

        if streak < MISSING_CONFIRM_STREAK:
            m["recovery_state"] = "searching"
            save_miners(miners)
            return RecoverResult(
                False,
                ip,
                action="searching",
                message=(
                    f"{label}: nicht gefunden "
                    f"({streak}/{MISSING_CONFIRM_STREAK} vor Nachfrage)"
                ),
            )

        save_miners(miners)

    mark_miner_missing(miner, notify=True)
    return RecoverResult(
        False,
        ip,
        action="missing",
        message=(
            f"{label}: MAC {mac} nach {MISSING_CONFIRM_STREAK}× "
            "nicht gefunden – Nachfrage"
        ),
    )


def confirm_miner_exists(ip_or_mac: str, *, exists: bool) -> Tuple[bool, str]:
    miners = load_miners()
    miner = _find_miner(miners, ip_or_mac) or _find_miner_by_mac(ip_or_mac)
    if not miner:
        return False, "Miner nicht gefunden"

    ip = str(miner.get("ip") or "").strip()
    if not exists:
        remove_miner_completely(ip)
        return True, f"Miner {ip} entfernt"

    with _recovery_lock():
        miners = load_miners()
        m = _find_miner(miners, ip)
        if not m:
            return False, "Miner nicht gefunden"
        m["recovery_state"] = "awaiting_user"
        m["user_confirmed_exists"] = True
        m["recovery_miss_streak"] = 0
        m["recovery_updated_at"] = _now_iso()
        m["last_recovery_scan_at"] = _now_iso()
        save_miners(miners)
    return True, f"Suche für {ip} wird täglich fortgesetzt"


def remove_miner_completely(ip: str) -> bool:
    ip = str(ip or "").strip()
    if not ip:
        return False
    with _recovery_lock():
        miners = load_miners()
        before = len(miners)
        miners = [m for m in miners if str(m.get("ip") or "").strip() != ip]
        if len(miners) == before:
            return False
        save_miners(miners)

        try:
            from control_algorithm.config import remove_device_from_config

            remove_device_from_config(ip)
        except Exception:
            pass

        if STATUS_PATH.is_file():
            try:
                entries = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
                if isinstance(entries, list):
                    entries = [
                        e for e in entries if str(e.get("ip") or "") != ip
                    ]
                    STATUS_PATH.write_text(
                        json.dumps(entries, indent=4, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass

        for path in (LATCHES_PATH, LAST_ACTIONS_PATH):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and ip in data:
                    del data[ip]
                    path.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            except Exception:
                pass

        if STATE_PATH.is_file():
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                state = _rekey_mapping(state, ip, "")
                # leere Keys entfernen
                if isinstance(state, dict):

                    def _prune(obj: Any) -> Any:
                        if isinstance(obj, dict):
                            return {
                                k: _prune(v)
                                for k, v in obj.items()
                                if str(k) != ""
                            }
                        if isinstance(obj, list):
                            return [_prune(x) for x in obj]
                        return obj

                    state = _prune(state)
                STATE_PATH.write_text(
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
            except Exception:
                pass
    return True


def list_pending_recovery() -> List[Dict[str, Any]]:
    """Miner, die eine User-Entscheidung brauchen (Ja existiert / Nein entfernen).

    Nach „Ja, existiert“ bleibt recovery_state=awaiting_user für den täglichen Scan,
    der Banner soll aber nicht weiter fragen. Auch bei State=missing nach Bestätigung
    nicht erneut anzeigen.
    """
    out = []
    for m in load_miners():
        state = str(m.get("recovery_state") or "ok")
        if state not in ("missing", "awaiting_user"):
            continue
        if bool(m.get("user_confirmed_exists")):
            continue
        out.append(
            {
                "ip": m.get("ip"),
                "mac": m.get("mac"),
                "label": m.get("note") or m.get("model") or m.get("ip"),
                "recovery_state": state,
                "user_confirmed_exists": False,
                "recovery_miss_streak": _miss_streak(m),
            }
        )
    return out


def try_recover_miner(
    miner: Dict[str, Any],
    *,
    force: bool = False,
    daily: bool = False,
) -> RecoverResult:
    ip = str(miner.get("ip") or "").strip()
    username = miner.get("username") or miner.get("user") or "root"
    password = miner.get("password") or ""
    label = miner.get("note") or miner.get("model") or ip

    if not ip:
        return RecoverResult(False, ip, action="skipped", message="Keine IP")

    state = str(miner.get("recovery_state") or "ok")
    if state == "removed":
        return RecoverResult(False, ip, action="skipped", message="Entfernt")

    if daily:
        if not _should_daily_scan(miner):
            return RecoverResult(False, ip, action="skipped", message="Daily cooldown")
    elif not force and not _should_immediate_scan(ip):
        return RecoverResult(False, ip, action="skipped", message="Rate limit")

    mac = normalize_mac(miner.get("mac"))
    if not mac:
        found = discover_mac_for_ip(ip, username, password)
        if found:
            learned, source = found
            store_mac_on_miner(ip, learned, source)
            mac = learned
            # Gerät an bekannter IP gefunden → Miss-Streak sofort löschen
            mark_miner_reachable(ip)
        else:
            return record_recovery_miss(miner, daily=daily)

    new_ip = resolve_ip_by_mac(mac)
    if new_ip and new_ip != ip:
        if migrate_device_ip(ip, new_ip):
            miner["ip"] = new_ip
            return RecoverResult(
                True,
                ip,
                new_ip=new_ip,
                action="migrated",
                message=f"{label}: {ip} → {new_ip}",
            )
        return RecoverResult(False, ip, action="none", message="Migration fehlgeschlagen")

    if new_ip == ip:
        # Erreichbar laut ARP, Verbindungsproblem woanders
        mark_miner_reachable(ip)
        return RecoverResult(False, ip, new_ip=ip, action="none", message="IP unverändert")

    # MAC kurz nicht in der Nachbartabelle – wenn die aktuelle IP trotzdem
    # antwortet, zählt das als Fund (Streak zurück), kein Miss.
    probed = discover_mac_for_ip(ip, username, password)
    if probed:
        learned, source = probed
        store_mac_on_miner(ip, learned, source)
        mark_miner_reachable(ip)
        return RecoverResult(
            False,
            ip,
            new_ip=ip,
            action="none",
            message="IP unverändert (Gerät erneut gefunden)",
        )

    return record_recovery_miss(miner, daily=daily)


def run_daily_missing_scan() -> List[RecoverResult]:
    results: List[RecoverResult] = []
    for miner in load_miners():
        state = str(miner.get("recovery_state") or "ok")
        if state not in ("missing", "awaiting_user"):
            continue
        if not _should_daily_scan(miner):
            continue
        results.append(try_recover_miner(miner, force=True, daily=True))
    return results


def recovery_state_label(state: Optional[str]) -> str:
    mapping = {
        "ok": "OK",
        "searching": "Suche…",
        "missing": "Nicht gefunden",
        "awaiting_user": "Tägliche Suche",
        "removed": "Entfernt",
    }
    return mapping.get(str(state or "ok"), str(state or "ok"))
