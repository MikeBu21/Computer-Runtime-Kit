#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send outbox error packages via Telegram; retry leaves files in outbox."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def load_config(path: Path) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
        "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
        "token_file": "/home/piminer/minersteuerung/miner-dashboard/backend/telegram_config.json",
        "chats_file": "/home/piminer/minersteuerung/miner-dashboard/backend/telegram_chats.json",
        "telegram_token": "",
        "chat_ids": [],
    }
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"config: {exc}", file=sys.stderr)
    return cfg


def resolve_token(cfg: Dict[str, Any]) -> str:
    token = str(cfg.get("telegram_token") or "").strip()
    if token:
        return token
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        return env
    tf = str(cfg.get("token_file") or "").strip()
    if tf and Path(tf).is_file():
        try:
            data = json.loads(Path(tf).read_text(encoding="utf-8"))
            return str(data.get("telegram_token") or "").strip()
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return ""


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


def pending_packages(outbox: Path) -> List[Tuple[Path, Path]]:
    pairs = []
    for meta in sorted(outbox.glob("piMiner-errors-*.meta.json")):
        label = meta.name.replace(".meta.json", "")
        gz = outbox / f"{label}.jsonl.gz"
        if gz.is_file():
            pairs.append((gz, meta))
    return pairs


def _post(url: str, data: Dict[str, str], timeout: float = 30) -> bool:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(512)
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"telegram post failed: {exc}", file=sys.stderr)
        return False


def _post_document(
    token: str,
    chat_id: int,
    path: Path,
    caption: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
    filename: str | None = None,
) -> bool:
    """multipart sendDocument — prefer openable .txt for regular PCs."""
    boundary = "----TgErrLog7f3a"
    file_bytes = path.read_bytes()
    name = filename or path.name
    parts: List[bytes] = []

    def field(field_name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode()
        )
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption[:1024])
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read(512)
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"sendDocument {chat_id} failed: {exc}", file=sys.stderr)
        return False


def format_caption(meta: Dict[str, Any]) -> str:
    soft = meta.get("software") or {}
    top = meta.get("top_errors") or []
    lines = [
        f"piMiner Fehlerbericht ({meta.get('period_days', 14)} Tage)",
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
        "piMiner Fehlerbericht",
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
    if not events:
        lines.append("(keine Fehler-Einträge in diesem Zeitraum)")
    else:
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
    lines.append("")
    lines.append(
        "Hinweis: Die Datei ist UTF-8-Text (.txt). Früher wurde .jsonl.gz gesendet,"
        " was auf vielen PCs nicht ohne Extra-Software zu öffnen war."
    )
    return "\n".join(lines) + "\n"


def send_package(token: str, chats: List[int], gz: Path, meta_path: Path) -> bool:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    # Still deliver empty reports so recipients know the job ran (0 errors).
    caption = format_caption(meta)
    txt_name = gz.name.replace(".jsonl.gz", ".txt")
    if not txt_name.endswith(".txt"):
        txt_name = f"{gz.stem}.txt"
    report = build_plaintext_report(gz, meta)

    text_ok = 0
    for cid in chats:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        if _post(url, {"chat_id": str(cid), "text": caption}):
            text_ok += 1

    doc_ok = 0
    with tempfile.TemporaryDirectory(prefix="errlog-txt-") as tmp:
        txt_path = Path(tmp) / txt_name
        txt_path.write_text(report, encoding="utf-8")
        for cid in chats:
            if _post_document(
                token,
                cid,
                txt_path,
                caption=f"Datei: {txt_name} (öffnet als Text)",
                content_type="text/plain; charset=utf-8",
                filename=txt_name,
            ):
                doc_ok += 1
    # Success only if every chat got the document (buffer retries otherwise)
    return doc_ok == len(chats) and len(chats) > 0


def mark_sent(gz: Path, meta: Path, sent_dir: Path) -> None:
    sent_dir.mkdir(parents=True, exist_ok=True)
    for p in (gz, meta):
        dest = sent_dir / p.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(p), str(dest))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get(
            "ERROR_LOG_PIPELINE_CONFIG",
            str(Path(__file__).resolve().parent / "config.json"),
        ),
    )
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    # Reload chats/token every invocation
    token = resolve_token(cfg)
    chats = resolve_chat_ids(cfg)
    outbox = Path(cfg["outbox_dir"])
    sent = Path(cfg["sent_dir"])

    if not token:
        print("missing telegram token", file=sys.stderr)
        return 2
    if not chats:
        print("no chat_ids yet — waiting for minerbot /start", file=sys.stderr)
        return 1

    pairs = pending_packages(outbox)
    if not pairs:
        print("outbox empty")
        return 0

    ok_n = 0
    for gz, meta in pairs:
        if send_package(token, chats, gz, meta):
            mark_sent(gz, meta, sent)
            ok_n += 1
            print(f"telegram sent {gz.name} to {len(chats)} chat(s)")
        else:
            print(f"kept in outbox {gz.name} (will retry)")
    print(f"done sent={ok_n}/{len(pairs)} chats={chats}")
    return 0 if ok_n == len(pairs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
