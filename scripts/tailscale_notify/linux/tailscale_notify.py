#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""piMiner-only: Tailscale change → Telegram (all known chats, stdlib only)."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_INTERVAL = 60
KEY_WARN_DAYS = 14


def _now_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _run(cmd: List[str], timeout: float = 8.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (p.stdout or "").strip()
        if not out and p.stderr:
            out = (p.stderr or "").strip()
        return p.returncode, out
    except FileNotFoundError:
        return 127, "tailscale not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 1, str(exc)


def _read_chat_ids_file(path: Path) -> List[int]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items: List[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("chat_ids"), list):
        items = raw["chat_ids"]
    else:
        return []
    out: List[int] = []
    for x in items:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def load_config(path: Path) -> Dict[str, Any]:
    """Load notifier config. Chat IDs are resolved separately each cycle."""
    cfg: Dict[str, Any] = {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_ids": [],
        "interval_sec": DEFAULT_INTERVAL,
        "key_warn_days": KEY_WARN_DAYS,
        "hostname_override": "",
        "token_file": "",
        "chats_file": "",
    }
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"config read failed: {exc}", file=sys.stderr)

    token_file = str(cfg.get("token_file") or "").strip()
    if not cfg.get("telegram_token") and token_file:
        try:
            data = json.loads(Path(token_file).read_text(encoding="utf-8"))
            cfg["telegram_token"] = str(data.get("telegram_token") or "").strip()
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    return cfg


def resolve_chat_ids(cfg: Dict[str, Any]) -> List[int]:
    """Union of config chat_ids, chats_file (minerbot), and TELEGRAM_CHAT_IDS.

    Reloaded every cycle so newly registered minerbot chats are included
    without restarting this service. Does not call getUpdates (minerbot owns
    that long-poll); minerbot writes telegram_chats.json on /start and commands.
    """
    ids: Set[int] = set()
    for x in cfg.get("chat_ids") or []:
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            pass

    chats_file = str(cfg.get("chats_file") or "").strip()
    if chats_file:
        ids.update(_read_chat_ids_file(Path(chats_file)))

    env_chats = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
    if env_chats:
        for part in env_chats.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                pass

    return sorted(ids)


def collect_snapshot(hostname: str) -> Dict[str, Any]:
    ipv4 = ""
    ipv6 = ""
    rc4, out4 = _run(["tailscale", "ip", "-4"])
    if rc4 == 0 and out4 and " " not in out4.split("\n")[0]:
        ipv4 = out4.splitlines()[0].strip()
    rc6, out6 = _run(["tailscale", "ip", "-6"])
    if rc6 == 0 and out6:
        first = out6.splitlines()[0].strip()
        if first and not first.lower().startswith("error"):
            ipv6 = first

    backend = "unknown"
    online: Optional[bool] = None
    dns_name = ""
    key_expiry = ""
    health: List[Any] = []
    ips: List[str] = []

    rc, raw = _run(["tailscale", "status", "--json"])
    if rc == 0 and raw.startswith("{"):
        try:
            data = json.loads(raw)
            backend = str(data.get("BackendState") or backend)
            health = list(data.get("Health") or [])
            self_info = data.get("Self") or {}
            online = self_info.get("Online")
            dns_name = str(self_info.get("DNSName") or "").rstrip(".")
            key_expiry = str(self_info.get("KeyExpiry") or "")
            ips = list(self_info.get("TailscaleIPs") or [])
            if not ipv4:
                for ip in ips:
                    if ":" not in ip:
                        ipv4 = ip
                        break
            if not ipv6:
                for ip in ips:
                    if ":" in ip:
                        ipv6 = ip
                        break
            if not hostname:
                hostname = str(self_info.get("HostName") or hostname)
        except json.JSONDecodeError:
            backend = "json_error"
    elif rc != 0:
        backend = f"error:{out4 or raw or rc}"

    return {
        "hostname": hostname or socket.gethostname(),
        "ipv4": ipv4,
        "ipv6": ipv6,
        "dns_name": dns_name,
        "backend": backend,
        "online": online,
        "key_expiry": key_expiry,
        "health": health,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def days_until_expiry(iso_ts: str) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        ts = iso_ts.replace("Z", "+00:00")
        exp = datetime.fromisoformat(ts)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return (exp - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except ValueError:
        return None


def diff_changes(
    prev: Optional[Dict[str, Any]],
    cur: Dict[str, Any],
    *,
    key_warn_days: float,
) -> List[str]:
    if not prev:
        return ["initial"]
    changes: List[str] = []
    for key, label in (
        ("ipv4", "IPv4"),
        ("ipv6", "IPv6"),
        ("backend", "Backend"),
        ("online", "Online"),
        ("dns_name", "DNS"),
        ("key_expiry", "KeyExpiry"),
    ):
        if prev.get(key) != cur.get(key):
            changes.append(label)
    prev_h = json.dumps(prev.get("health") or [], sort_keys=True)
    cur_h = json.dumps(cur.get("health") or [], sort_keys=True)
    if prev_h != cur_h:
        changes.append("Health")

    days = days_until_expiry(str(cur.get("key_expiry") or ""))
    prev_days = days_until_expiry(str(prev.get("key_expiry") or ""))
    if days is not None and days <= key_warn_days:
        if prev_days is None or prev_days > key_warn_days or "KeyExpiry" in changes:
            if "KeyWarn" not in changes:
                changes.append("KeyWarn")
    return changes


def format_message(snap: Dict[str, Any], changes: List[str]) -> str:
    lines = [
        "Tailscale Status",
        f"Maschine: {snap.get('hostname')}",
    ]
    if snap.get("dns_name"):
        lines.append(f"DNS: {snap.get('dns_name')}")
    lines.append(f"IPv4: {snap.get('ipv4') or '—'}")
    lines.append(f"IPv6: {snap.get('ipv6') or '—'}")
    lines.append(f"Backend: {snap.get('backend')}")
    if snap.get("online") is not None:
        lines.append(f"Online: {snap.get('online')}")
    if snap.get("key_expiry"):
        days = days_until_expiry(str(snap["key_expiry"]))
        extra = f" ({days:.0f} Tage)" if days is not None else ""
        lines.append(f"KeyExpiry: {snap['key_expiry']}{extra}")
    health = snap.get("health") or []
    if health:
        lines.append(f"Health: {', '.join(str(h) for h in health)}")
    lines.append(f"Änderung: {', '.join(changes)}")
    lines.append(f"Zeit: {_now_local()}")
    return "\n".join(lines)


def send_telegram(token: str, chat_ids: List[int], text: str) -> int:
    if not token or not chat_ids or not text:
        return 0
    ok = 0
    for chat_id in chat_ids:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode(
            {"chat_id": str(chat_id), "text": text}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read(256)
            ok += 1
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"telegram {chat_id} failed: {exc}", file=sys.stderr)
    return ok


def load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"snapshot": None, "known_chat_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"snapshot": None, "known_chat_ids": []}
    if not isinstance(data, dict):
        return {"snapshot": None, "known_chat_ids": []}
    # Migrate flat snapshot from first install
    if "snapshot" not in data and "ipv4" in data:
        known = data.pop("known_chat_ids", []) if "known_chat_ids" in data else []
        return {"snapshot": data, "known_chat_ids": list(known or [])}
    return {
        "snapshot": data.get("snapshot"),
        "known_chat_ids": list(data.get("known_chat_ids") or []),
    }


def save_state(
    path: Path,
    snap: Dict[str, Any],
    known_chat_ids: List[int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot": snap,
        "known_chat_ids": sorted({int(x) for x in known_chat_ids}),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def one_cycle(cfg: Dict[str, Any], state_path: Path, *, force: bool = False) -> int:
    hostname = str(cfg.get("hostname_override") or "").strip() or socket.gethostname()
    snap = collect_snapshot(hostname)
    state = load_state(state_path)
    prev = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else None
    warn_days = float(cfg.get("key_warn_days") or KEY_WARN_DAYS)
    changes = diff_changes(prev, snap, key_warn_days=warn_days)
    if force and not changes:
        changes = ["manual"]

    token = str(cfg.get("telegram_token") or "")
    chats = resolve_chat_ids(cfg)
    if not chats:
        print(f"{_now_local()} no chat_ids yet — waiting for minerbot / config", file=sys.stderr)
        save_state(state_path, snap, [])
        return 0

    known_prev = {int(x) for x in (state.get("known_chat_ids") or [])}
    chats_set = set(chats)
    new_chats = sorted(chats_set - known_prev)
    sent_total = 0

    # New chat IDs: welcome with current status (even if Tailscale unchanged)
    if new_chats and not force:
        welcome = format_message(snap, ["new_chat"])
        sent_total += send_telegram(token, new_chats, welcome)
        print(f"{_now_local()} new_chats={new_chats} sent={sent_total}")

    if changes:
        msg = format_message(snap, changes)
        sent = send_telegram(token, chats, msg)
        sent_total += sent
        print(f"{_now_local()} change={changes} sent={sent}/{len(chats)}")

    save_state(state_path, snap, chats)
    return sent_total


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="piMiner Tailscale → Telegram notifier")
    ap.add_argument(
        "--config",
        default=os.environ.get(
            "TAILSCALE_NOTIFY_CONFIG",
            str(Path(__file__).resolve().parent / "config.json"),
        ),
    )
    ap.add_argument(
        "--state",
        default=os.environ.get(
            "TAILSCALE_NOTIFY_STATE",
            str(Path(__file__).resolve().parent / "state.json"),
        ),
    )
    ap.add_argument("--once", action="store_true", help="Single check then exit")
    ap.add_argument("--force", action="store_true", help="Send even without change")
    ap.add_argument("--interval", type=int, default=0, help="Override poll interval")
    args = ap.parse_args(argv)

    config_path = Path(args.config)
    state_path = Path(args.state)
    cfg0 = load_config(config_path)
    if not cfg0.get("telegram_token"):
        print("missing telegram_token (config or TELEGRAM_BOT_TOKEN)", file=sys.stderr)
        return 2

    if args.once or args.force:
        # Reload chats at send time
        cfg = load_config(config_path)
        one_cycle(cfg, state_path, force=bool(args.force))
        return 0

    interval = int(args.interval or cfg0.get("interval_sec") or DEFAULT_INTERVAL)
    interval = max(15, interval)
    print(f"tailscale_notify (piMiner) every {interval}s host={socket.gethostname()}")
    while True:
        try:
            cfg = load_config(config_path)  # pick up token/path edits
            one_cycle(cfg, state_path, force=False)
        except Exception as exc:  # keep service alive
            print(f"cycle error: {exc}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
