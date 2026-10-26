# -*- coding: utf-8 -*-
"""Status helpers for the 14-day Telegram error-report pipeline (dashboard UI)."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG = Path(
    "/home/piminer/minersteuerung/scripts/error_log_pipeline/pi/config.json"
)
PIPELINE_DIR = Path("/home/piminer/minersteuerung/scripts/error_log_pipeline/pi")
PYTHON = Path("/home/piminer/minersteuerung/venv/bin/python")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def load_pipeline_config(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = path or Path(
        os.environ.get("ERROR_LOG_PIPELINE_CONFIG", str(DEFAULT_CONFIG))
    )
    data = _load_json(cfg_path) if cfg_path.is_file() else None
    cfg: Dict[str, Any] = {
        "buffer_dir": "/home/piminer/minersteuerung/logs/error_reports/buffer",
        "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
        "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
        "window_days": 14,
        "token_file": str(
            Path(__file__).resolve().parent / "telegram_config.json"
        ),
        "chats_file": str(
            Path(__file__).resolve().parent / "telegram_chats.json"
        ),
        "chat_ids": [],
        "schedule_note": "",
        "config_path": str(cfg_path),
    }
    if isinstance(data, dict):
        cfg.update(data)
        cfg["config_path"] = str(cfg_path)
    return cfg


def resolve_chat_ids(cfg: Dict[str, Any]) -> List[int]:
    """Match send_telegram: explicit chat_ids win (no merge with chats_file)."""
    ids: set[int] = set()
    for x in cfg.get("chat_ids") or []:
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            pass
    if ids:
        return sorted(ids)
    cf = str(cfg.get("chats_file") or "").strip()
    if cf and Path(cf).is_file():
        raw = _load_json(Path(cf))
        items = raw if isinstance(raw, list) else (
            (raw or {}).get("chat_ids") if isinstance(raw, dict) else []
        ) or []
        for x in items:
            try:
                ids.add(int(x))
            except (TypeError, ValueError):
                pass
    return sorted(ids)


def _pkg_row(gz: Path, meta_path: Path, folder: str) -> Dict[str, Any]:
    meta = _load_json(meta_path) if meta_path.is_file() else {}
    if not isinstance(meta, dict):
        meta = {}
    mtime = None
    try:
        mtime = datetime.fromtimestamp(gz.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        pass
    return {
        "label": gz.name.replace(".jsonl.gz", ""),
        "filename": gz.name,
        "folder": folder,
        "size_bytes": gz.stat().st_size if gz.is_file() else 0,
        "mtime": mtime,
        "event_count": int(meta.get("event_count") or 0),
        "period_label": meta.get("period_label") or "",
        "period_days": meta.get("period_days") or cfg_window_fallback(meta),
        "window_start": meta.get("window_start") or "",
        "window_end": meta.get("window_end") or "",
        "generated_at": meta.get("generated_at") or "",
        "sources": meta.get("sources") or [],
        "top_errors": meta.get("top_errors") or [],
        "git_sha": (meta.get("software") or {}).get("git_sha") if isinstance(meta.get("software"), dict) else "",
    }


def cfg_window_fallback(meta: Dict[str, Any]) -> int:
    try:
        return int(meta.get("period_days") or 14)
    except (TypeError, ValueError):
        return 14


def list_packages(folder: Path, name: str) -> List[Dict[str, Any]]:
    if not folder.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for meta in sorted(folder.glob("piMiner-errors-*.meta.json"), reverse=True):
        label = meta.name.replace(".meta.json", "")
        gz = folder / f"{label}.jsonl.gz"
        if not gz.is_file():
            # buffer may still be uncompressed jsonl
            raw = folder / f"{label}.jsonl"
            if raw.is_file():
                rows.append(
                    {
                        "label": label,
                        "filename": raw.name,
                        "folder": name,
                        "size_bytes": raw.stat().st_size,
                        "mtime": datetime.fromtimestamp(raw.stat().st_mtime).isoformat(
                            timespec="seconds"
                        ),
                        "event_count": int((_load_json(meta) or {}).get("event_count") or 0)
                        if meta.is_file()
                        else 0,
                        "period_label": (_load_json(meta) or {}).get("period_label") or "",
                        "period_days": (_load_json(meta) or {}).get("period_days") or 14,
                        "window_start": (_load_json(meta) or {}).get("window_start") or "",
                        "window_end": (_load_json(meta) or {}).get("window_end") or "",
                        "generated_at": (_load_json(meta) or {}).get("generated_at") or "",
                        "sources": (_load_json(meta) or {}).get("sources") or [],
                        "top_errors": (_load_json(meta) or {}).get("top_errors") or [],
                        "git_sha": "",
                        "uncompressed": True,
                    }
                )
            continue
        rows.append(_pkg_row(gz, meta, name))
    return rows


def timer_status() -> Dict[str, Any]:
    """Best-effort systemd --user timer next/last times."""
    out: Dict[str, Any] = {
        "collect_next": None,
        "collect_last": None,
        "send_next": None,
        "send_last": None,
        "collect_active": False,
        "send_active": False,
    }
    try:
        r = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "error-log-collect.timer",
                "error-log-send.timer",
                "--property=Id,ActiveState,NextElapseUSecRealtime,LastTriggerUSec",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return out

    blocks = [b for b in (r.stdout or "").split("\n\n") if b.strip()]
    for block in blocks:
        props = {}
        for line in block.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        tid = props.get("Id", "")
        active = props.get("ActiveState") == "active"
        nxt = props.get("NextElapseUSecRealtime") or ""
        last = props.get("LastTriggerUSec") or ""
        if nxt in ("", "n/a", "0"):
            nxt = None
        if last in ("", "n/a", "0"):
            last = None
        if "collect" in tid:
            out["collect_active"] = active
            out["collect_next"] = nxt
            out["collect_last"] = last
        elif "send" in tid:
            out["send_active"] = active
            out["send_next"] = nxt
            out["send_last"] = last
    return out


def build_status() -> Dict[str, Any]:
    cfg = load_pipeline_config()
    chats = resolve_chat_ids(cfg)
    outbox = Path(str(cfg["outbox_dir"]))
    sent = Path(str(cfg["sent_dir"]))
    buffer = Path(str(cfg["buffer_dir"]))
    return {
        "window_days": int(cfg.get("window_days") or 14),
        "schedule_note": cfg.get("schedule_note") or "",
        "schedule_collect": "1. und 15. jeden Monats um 03:15",
        "schedule_retry": "täglich 06:30 (Outbox-Nachversuch)",
        "chat_ids": chats,
        "chats_file": cfg.get("chats_file") or "",
        "sources": [
            "Steuerungsalgorithmus.log",
            "control_algorithm_daemon.log",
        ],
        "outbox": list_packages(outbox, "outbox"),
        "sent": list_packages(sent, "sent")[:20],
        "buffer": list_packages(buffer, "buffer"),
        "timers": timer_status(),
        "config_path": cfg.get("config_path"),
    }


def run_send_now() -> Dict[str, Any]:
    """Send pending outbox packages via existing send_telegram.py."""
    script = PIPELINE_DIR / "send_telegram.py"
    cfg = DEFAULT_CONFIG
    py = PYTHON if PYTHON.is_file() else Path("python3")
    if not script.is_file():
        return {"ok": False, "message": f"Skript fehlt: {script}"}
    try:
        r = subprocess.run(
            [str(py), str(script), "--config", str(cfg)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(PIPELINE_DIR),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Senden abgebrochen (Timeout)"}
    except OSError as exc:
        return {"ok": False, "message": str(exc)}
    text = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    ok = r.returncode == 0
    if "outbox empty" in text:
        return {"ok": True, "message": "Outbox leer — nichts zu senden.", "detail": text}
    if ok:
        return {"ok": True, "message": "Telegram-Versand ausgeführt.", "detail": text}
    return {
        "ok": False,
        "message": "Versand fehlgeschlagen oder teilweise offen (siehe Detail).",
        "detail": text or f"exit {r.returncode}",
    }
