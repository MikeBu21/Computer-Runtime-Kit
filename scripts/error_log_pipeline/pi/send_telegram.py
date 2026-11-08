#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fehlerberichte per Telegram.

Modi:
  (ohne)     Outbox einmal abarbeiten
  --test     Testnachricht: erst Fehlerbericht-Chat(s), danach eigene Chat-ID
  --daemon   still im Hintergrund: alle 14 Tage sammeln, nur Berichte mit
             Einträgen senden, bei Telegram-Fehlern pausieren und später erneut

Jede Zustellung öffnet eine eigene kurze HTTPS-Verbindung (Connection: close)
und schließt sie sofort wieder. Es wird nie per getUpdates gepollt, deshalb
können beliebig viele Pis/Bots denselben Fehlerbericht-Chat nutzen, ohne sich
gegenseitig zu stören oder voneinander zu wissen.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent


def load_config(path: Path) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "buffer_dir": "/home/piminer/minersteuerung/logs/error_reports/buffer",
        "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
        "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
        "state_file": "/home/piminer/minersteuerung/logs/error_reports/state.json",
        "token_file": "/home/piminer/minersteuerung/miner-dashboard/backend/telegram_config.json",
        "chats_file": "/home/piminer/minersteuerung/miner-dashboard/backend/telegram_chats.json",
        "telegram_token": "",
        "chat_ids": [],
        "user_chat_id": "",
        "user_token_file": "/home/piminer/minersteuerung/miner-dashboard/backend/telegram_config.json",
        "window_days": 14,
        "poll_sec": 20,
        "pause_min_sec": 60,
        "pause_max_sec": 1800,
    }
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"config: {exc}", file=sys.stderr)
    return cfg


def _token_from_file(path_str: str) -> str:
    p = Path(path_str) if path_str else None
    if not p or not p.is_file():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("telegram_token") or "").strip()
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return ""


def resolve_token(cfg: Dict[str, Any]) -> str:
    token = str(cfg.get("telegram_token") or "").strip()
    if token:
        return token
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        return env
    return _token_from_file(str(cfg.get("token_file") or "").strip())


def resolve_chat_ids(cfg: Dict[str, Any]) -> List[int]:
    """Resolve Fehlerberichte recipients.

    If config ``chat_ids`` is non-empty, those IDs alone are used (owner-only /
    explicit list). Unlike Tailscale notify, we do **not** merge minerbot's
    ``telegram_chats.json`` in that case — other chats stay for bot/alarms only.
    When ``chat_ids`` is empty, fall back to ``chats_file`` + TELEGRAM_CHAT_IDS.
    """
    ids: Set[int] = set()
    for x in cfg.get("chat_ids") or []:
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            pass
    if ids:
        return sorted(ids)

    cf = str(cfg.get("chats_file") or "").strip()
    if cf and Path(cf).is_file():
        try:
            raw = json.loads(Path(cf).read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else (raw.get("chat_ids") or [])
            for x in items:
                try:
                    ids.add(int(x))
                except (TypeError, ValueError):
                    pass
        except (OSError, json.JSONDecodeError):
            pass
    env = os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
    if env:
        for part in env.split(","):
            part = part.strip()
            if part:
                try:
                    ids.add(int(part))
                except ValueError:
                    pass
    return sorted(ids)


def resolve_user_chat_id(cfg: Dict[str, Any]) -> Optional[int]:
    raw = str(cfg.get("user_chat_id") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"user_chat_id ungültig: {raw!r}", file=sys.stderr)
        return None


@dataclass(frozen=True)
class Recipient:
    token: str
    chat_id: int
    kind: str  # "fehlerberichte" | "eigene"


def resolve_recipients(cfg: Dict[str, Any]) -> List[Recipient]:
    """Fehlerbericht-Chat(s) zuerst, danach die eigene Chat-ID (falls eingetragen)."""
    out: List[Recipient] = []
    token = resolve_token(cfg)
    if token:
        out.extend(Recipient(token, cid, "fehlerberichte") for cid in resolve_chat_ids(cfg))
    user_chat = resolve_user_chat_id(cfg)
    if user_chat is not None and all(r.chat_id != user_chat for r in out):
        user_token = _token_from_file(str(cfg.get("user_token_file") or "").strip()) or token
        if user_token:
            out.append(Recipient(user_token, user_chat, "eigene"))
    return out


class TelegramError(Exception):
    def __init__(self, message: str, retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _call(token: str, method: str, body: bytes, content_type: str, timeout: float) -> None:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Connection": "close",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(512)
    except urllib.error.HTTPError as exc:
        retry_after = None
        detail = ""
        try:
            data = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            detail = str(data.get("description") or "")
            retry_after = (data.get("parameters") or {}).get("retry_after")
        except (OSError, ValueError, AttributeError):
            pass
        raise TelegramError(
            f"{method} HTTP {exc.code} {detail}".strip(),
            int(retry_after) if retry_after else None,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"{method}: {exc}") from exc


def send_text(r: Recipient, text: str) -> None:
    body = urllib.parse.urlencode({"chat_id": str(r.chat_id), "text": text}).encode("utf-8")
    _call(r.token, "sendMessage", body, "application/x-www-form-urlencoded", 30)


def send_document(r: Recipient, path: Path, caption: str, filename: str) -> None:
    """multipart sendDocument — prefer openable .txt for regular PCs."""
    boundary = "----TgErrLog7f3a"
    parts: List[bytes] = []

    def field(field_name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode()
        )
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    field("chat_id", str(r.chat_id))
    if caption:
        field("caption", caption[:1024])
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    )
    parts.append(b"Content-Type: text/plain; charset=utf-8\r\n\r\n")
    parts.append(path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    _call(
        r.token,
        "sendDocument",
        b"".join(parts),
        f"multipart/form-data; boundary={boundary}",
        60,
    )


def pending_packages(outbox: Path) -> List[Tuple[Path, Path]]:
    pairs = []
    for meta in sorted(outbox.glob("piminer-errors-*.meta.json")):
        label = meta.name.replace(".meta.json", "")
        gz = outbox / f"{label}.jsonl.gz"
        if gz.is_file():
            pairs.append((gz, meta))
    return pairs


def format_caption(meta: Dict[str, Any]) -> str:
    soft = meta.get("software") or {}
    top = meta.get("top_errors") or []
    lines = [
        f"piminer Fehlerbericht ({meta.get('period_days', 14)} Tage)",
        f"Host: {meta.get('host') or soft.get('hostname') or '—'}",
        f"Zeitraum bis: {meta.get('period_label')}",
        f"Einträge: {meta.get('event_count', 0)}",
        f"Git: {soft.get('git_sha') or '—'}",
    ]
    if top:
        sample = str(top[0].get("sample") or "").replace("\n", " ")[:120]
        lines.append(f"Top: {top[0].get('count')}× {sample}")
    return "\n".join(lines)


def _read_jsonl_gz(gz: Path) -> List[str]:
    try:
        raw = gzip.decompress(gz.read_bytes())
    except (OSError, gzip.BadGzipFile, EOFError) as exc:
        return [f"(gzip lesen fehlgeschlagen: {exc})"]
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    return [ln for ln in text.splitlines() if ln.strip()]


def build_plaintext_report(gz: Path, meta: Dict[str, Any]) -> str:
    """Human-readable .txt — opens on any PC (unlike .jsonl.gz)."""
    soft = meta.get("software") or {}
    lines: List[str] = [
        "piminer Fehlerbericht",
        "=" * 40,
        f"Host: {meta.get('host') or soft.get('hostname') or '—'}",
        f"Zeitraum: {meta.get('window_start') or '—'} → {meta.get('window_end') or '—'}",
        f"Label: {meta.get('period_label') or '—'}",
        f"Tage: {meta.get('period_days', 14)}",
        f"Erzeugt: {meta.get('generated_at') or '—'}",
        f"Git: {soft.get('git_sha') or '—'}",
        f"Quellen: {', '.join(meta.get('sources') or []) or '—'}",
        f"Einträge: {meta.get('event_count', 0)}",
        "",
    ]
    top = meta.get("top_errors") or []
    if top:
        lines.append("Häufigste Fehler:")
        for row in top[:10]:
            sample = str(row.get("sample") or "").replace("\n", " ")[:200]
            lines.append(f"  {row.get('count')}× {sample}")
        lines.append("")
    events = _read_jsonl_gz(gz)
    lines.append("Ereignisse (JSONL-Zeilen):")
    lines.append("-" * 40)
    # Cap very large reports for Telegram (50 MB limit; keep readable)
    max_lines = 2000
    for ln in events[:max_lines]:
        try:
            obj = json.loads(ln)
            ts = obj.get("ts") or obj.get("time") or ""
            src = obj.get("source") or obj.get("file") or ""
            msg = obj.get("line") or obj.get("message") or obj.get("sample") or ln
            lines.append(f"{ts} [{src}] {msg}".strip())
        except json.JSONDecodeError:
            lines.append(ln)
    if len(events) > max_lines:
        lines.append(f"… ({len(events) - max_lines} weitere Zeilen weggelassen)")
    return "\n".join(lines) + "\n"


def _read_meta(meta_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def deliver_package(recipients: List[Recipient], gz: Path, meta_path: Path) -> None:
    """One short connection per message; progress is stored per chat in the meta file
    so a retry after a pause only contacts chats that are still missing."""
    meta = _read_meta(meta_path)
    done = {str(x) for x in meta.get("delivered_to") or []}
    caption = format_caption(meta)
    txt_name = gz.name.replace(".jsonl.gz", ".txt")
    with tempfile.TemporaryDirectory(prefix="errlog-txt-") as tmp:
        txt_path = Path(tmp) / txt_name
        txt_path.write_text(build_plaintext_report(gz, meta), encoding="utf-8")
        for r in recipients:
            if str(r.chat_id) in done:
                continue
            send_text(r, caption)
            send_document(r, txt_path, f"Datei: {txt_name} (öffnet als Text)", txt_name)
            done.add(str(r.chat_id))
            meta["delivered_to"] = sorted(done)
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"telegram sent {gz.name} → {r.chat_id} ({r.kind})")


def mark_sent(gz: Path, meta: Path, sent_dir: Path) -> None:
    sent_dir.mkdir(parents=True, exist_ok=True)
    for p in (gz, meta):
        dest = sent_dir / p.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(p), str(dest))


def process_outbox(cfg: Dict[str, Any]) -> Tuple[int, int]:
    """Send every non-empty package; empty ones are archived without contacting Telegram.

    Raises TelegramError on the first failure (caller pauses and retries later).
    """
    outbox = Path(cfg["outbox_dir"])
    sent = Path(cfg["sent_dir"])
    pairs = pending_packages(outbox)
    recipients = resolve_recipients(cfg)
    ok_n = 0
    for gz, meta in pairs:
        if int(_read_meta(meta).get("event_count") or 0) <= 0:
            mark_sent(gz, meta, sent)
            print(f"no entries in {gz.name} — archived without sending")
            ok_n += 1
            continue
        if not recipients:
            continue
        deliver_package(recipients, gz, meta)
        mark_sent(gz, meta, sent)
        ok_n += 1
    return ok_n, len(pairs)


def run_test(cfg: Dict[str, Any]) -> int:
    recipients = resolve_recipients(cfg)
    if not recipients:
        print("keine Empfänger konfiguriert (Token/chat_ids fehlen)", file=sys.stderr)
        return 1
    host = socket.gethostname()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    failed = 0
    for r in recipients:
        label = "Fehlerbericht-Chat" if r.kind == "fehlerberichte" else "eigene Chat-ID"
        try:
            send_text(r, f"✅ Testnachricht Fehlerberichte\nGerät: {host}\nZeit: {stamp}\nEmpfänger: {label}")
            print(f"test ok → {r.chat_id} ({r.kind})")
        except TelegramError as exc:
            failed += 1
            print(f"test failed → {r.chat_id} ({r.kind}): {exc}", file=sys.stderr)
    return 0 if failed == 0 else 1


# --- state / lock -----------------------------------------------------------

def load_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    p = Path(str(cfg["state_file"]))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    p = Path(str(cfg["state_file"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


@contextmanager
def send_lock(cfg: Dict[str, Any]) -> Iterator[None]:
    """Local lock only (daemon vs. dashboard button on this Pi)."""
    lock_path = Path(str(cfg["state_file"])).with_name(".send.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _latest_package_time(cfg: Dict[str, Any]) -> Optional[float]:
    newest: Optional[float] = None
    for key in ("outbox_dir", "sent_dir", "buffer_dir"):
        folder = Path(str(cfg.get(key) or ""))
        if not folder.is_dir():
            continue
        for meta in folder.glob("piminer-errors-*.meta.json"):
            if "-TEST-" in meta.name:
                continue
            gen = _read_meta(meta).get("generated_at")
            try:
                ts = datetime.fromisoformat(str(gen)).timestamp()
            except (TypeError, ValueError):
                continue
            newest = ts if newest is None else max(newest, ts)
    return newest


def _run_collect(cfg_path: Path) -> None:
    script = HERE / "collect_errors.py"
    try:
        r = subprocess.run(
            [sys.executable, str(script), "--config", str(cfg_path)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        print((r.stdout or "").strip() or f"collect exit {r.returncode}")
        if r.returncode != 0 and r.stderr:
            print(r.stderr.strip(), file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"collect failed: {exc}", file=sys.stderr)


def _watch_signature(cfg_path: Path, cfg: Dict[str, Any]) -> Tuple[Any, ...]:
    """Changes here wake the daemon: config (Token/Chat-IDs) and outbox contents."""
    sig: List[Any] = []
    user_token_file = Path(str(cfg.get("user_token_file") or ""))
    for p in (cfg_path, user_token_file):
        try:
            sig.append(p.stat().st_mtime_ns)
        except OSError:
            sig.append(None)
    outbox = Path(str(cfg["outbox_dir"]))
    sig.append(tuple(sorted(p.name for p in outbox.glob("*.meta.json"))) if outbox.is_dir() else ())
    return tuple(sig)


def run_daemon(cfg_path: Path) -> int:
    cfg = load_config(cfg_path)
    state = load_state(cfg)
    if not state.get("last_collect"):
        state["last_collect"] = _latest_package_time(cfg) or time.time()
        save_state(cfg, state)
    print("error-log daemon gestartet", flush=True)
    last_sig: Optional[Tuple[Any, ...]] = None

    while True:
        cfg = load_config(cfg_path)
        sig = _watch_signature(cfg_path, cfg)
        if last_sig is not None and sig != last_sig and state.get("pause_until"):
            # User/admin changed recipients or new reports arrived → retry now
            state["pause_until"] = 0
            save_state(cfg, state)
        last_sig = sig

        now = time.time()
        window = max(1, int(cfg.get("window_days") or 14)) * 86400
        if now - float(state.get("last_collect") or 0) >= window:
            _run_collect(cfg_path)
            state["last_collect"] = now
            save_state(cfg, state)
            sig = last_sig = _watch_signature(cfg_path, cfg)

        if now >= float(state.get("pause_until") or 0) and pending_packages(Path(cfg["outbox_dir"])):
            try:
                with send_lock(cfg):
                    sent_n, total = process_outbox(cfg)
                if sent_n:
                    print(f"done sent={sent_n}/{total}", flush=True)
                    state["last_send"] = datetime.now().isoformat(timespec="seconds")
                state.update(pause_until=0, backoff_sec=0, last_error="")
            except TelegramError as exc:
                lo = int(cfg.get("pause_min_sec") or 60)
                hi = int(cfg.get("pause_max_sec") or 1800)
                backoff = min(max(lo, int(state.get("backoff_sec") or 0) * 2), hi)
                pause = exc.retry_after or backoff
                state.update(
                    pause_until=time.time() + pause,
                    backoff_sec=backoff,
                    last_error=str(exc),
                )
                print(f"Telegram-Fehler: {exc} — Pause {pause}s", file=sys.stderr, flush=True)
            save_state(cfg, state)
            last_sig = _watch_signature(cfg_path, cfg)

        # Idle quietly; wake early when config or outbox changes
        deadline = time.time() + max(5, int(cfg.get("poll_sec") or 20))
        while time.time() < deadline:
            time.sleep(2)
            if _watch_signature(cfg_path, cfg) != last_sig:
                break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get(
            "ERROR_LOG_PIPELINE_CONFIG",
            str(HERE / "config.json"),
        ),
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg_path = Path(args.config)

    if args.daemon:
        return run_daemon(cfg_path)

    cfg = load_config(cfg_path)
    if args.test:
        with send_lock(cfg):
            return run_test(cfg)

    if not pending_packages(Path(cfg["outbox_dir"])):
        print("outbox empty")
        return 0
    if not resolve_recipients(cfg):
        print("no recipients configured (token/chat_ids)", file=sys.stderr)
        return 1
    try:
        with send_lock(cfg):
            ok_n, total = process_outbox(cfg)
    except TelegramError as exc:
        print(f"Telegram-Fehler: {exc} — bleibt in der Outbox", file=sys.stderr)
        return 1
    print(f"done sent={ok_n}/{total}")
    return 0 if ok_n == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
