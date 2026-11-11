# -*- coding: utf-8 -*-
"""Status helpers for the 14-day Telegram error-report pipeline (dashboard UI)."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CONFIG = Path(
    "/home/piminer/minersteuerung/scripts/error_log_pipeline/pi/config.json"
)
# Dieselbe Datei liest der Outbox-Versand und hängt sie an den Bericht.
WISHES_FILENAME = "steuerung-wuensche.txt"
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
    for meta in sorted(folder.glob("piminer-errors-*.meta.json"), reverse=True):
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


def _fmt_ts(ts: Any) -> Optional[str]:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value).isoformat(sep=" ", timespec="minutes")


def timer_status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Status of the background daemon (error-log-send.service).

    Keys keep the former timer names so the existing page renders unchanged:
    collect_* = next/last 14-day collect, send_* = pause/retry state.
    """
    cfg = cfg or load_pipeline_config()
    out: Dict[str, Any] = {
        "collect_next": None,
        "collect_last": None,
        "send_next": None,
        "send_last": None,
        "collect_active": False,
        "send_active": False,
        "last_error": "",
    }
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "error-log-send.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        active = (r.stdout or "").strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        active = False
    out["collect_active"] = out["send_active"] = active

    state_file = Path(str(cfg.get("state_file") or "/home/piminer/minersteuerung/logs/error_reports/state.json"))
    state = _load_json(state_file) if state_file.is_file() else None
    if isinstance(state, dict):
        last = state.get("last_collect")
        out["collect_last"] = _fmt_ts(last)
        try:
            days = int(cfg.get("window_days") or 14)
            out["collect_next"] = _fmt_ts(float(last) + days * 86400)
        except (TypeError, ValueError):
            pass
        out["send_last"] = state.get("last_send") or None
        out["send_next"] = _fmt_ts(state.get("pause_until"))
        out["last_error"] = state.get("last_error") or ""
    return out


def wishes_path(cfg: Optional[Dict[str, Any]] = None) -> Path:
    cfg = cfg or load_pipeline_config()
    return Path(str(cfg["outbox_dir"])) / WISHES_FILENAME


def load_wishes(cfg: Optional[Dict[str, Any]] = None) -> str:
    try:
        return wishes_path(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_wishes(text: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """In die Outbox legen. Leer räumt die Datei weg. Es wird dabei nichts gesendet."""
    cfg = cfg or load_pipeline_config()
    path = wishes_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = str(text or "").strip()
    if not cleaned:
        try:
            path.unlink()
        except OSError:
            pass
        return ""
    path.write_text(cleaned + "\n", encoding="utf-8")
    return cleaned


def build_status() -> Dict[str, Any]:
    cfg = load_pipeline_config()
    chats = resolve_chat_ids(cfg)
    outbox = Path(str(cfg["outbox_dir"]))
    sent = Path(str(cfg["sent_dir"]))
    buffer = Path(str(cfg["buffer_dir"]))
    return {
        "window_days": int(cfg.get("window_days") or 14),
        "schedule_note": cfg.get("schedule_note") or "",
        "schedule_collect": "alle 14 Tage (Hintergrunddienst, nur Berichte mit Einträgen)",
        "schedule_retry": "bei Telegram-Fehler: Pause, danach erneuter Versuch",
        "chat_ids": chats,
        "user_chat_id": str(cfg.get("user_chat_id") or ""),
        "chats_file": cfg.get("chats_file") or "",
        "sources": [
            "Steuerungsalgorithmus.log",
            "control_algorithm_daemon.log",
        ],
        "outbox": list_packages(outbox, "outbox"),
        "wishes": load_wishes(cfg),
        "sent": list_packages(sent, "sent")[:20],
        "buffer": list_packages(buffer, "buffer"),
        "timers": timer_status(cfg),
        "config_path": cfg.get("config_path"),
    }


def _run_sender(*extra: str) -> Tuple[int, str]:
    script = PIPELINE_DIR / "send_telegram.py"
    py = PYTHON if PYTHON.is_file() else Path("python3")
    if not script.is_file():
        return -1, f"Skript fehlt: {script}"
    try:
        r = subprocess.run(
            [str(py), str(script), "--config", str(DEFAULT_CONFIG), *extra],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(PIPELINE_DIR),
        )
    except subprocess.TimeoutExpired:
        return -1, "Senden abgebrochen (Timeout)"
    except OSError as exc:
        return -1, str(exc)
    return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()


def run_test() -> Dict[str, Any]:
    """Testnachricht: kurz an den Fehlerbericht-Chat, danach an die eigene Chat-ID."""
    code, text = _run_sender("--test")
    if code == 0:
        return {"ok": True, "message": "Testnachricht gesendet.", "detail": text}
    return {"ok": False, "message": "Testnachricht fehlgeschlagen (siehe Detail).", "detail": text}


def run_send_now() -> Dict[str, Any]:
    """Send pending outbox packages; with an empty outbox send a test message instead."""
    code, text = _run_sender()
    ok = code == 0
    if "outbox empty" in text:
        result = run_test()
        result["message"] = "Outbox leer — " + result["message"]
        return result
    if ok:
        return {"ok": True, "message": "Telegram-Versand ausgeführt.", "detail": text}
    return {
        "ok": False,
        "message": "Versand fehlgeschlagen oder teilweise offen (siehe Detail).",
        "detail": text or f"exit {code}",
    }
