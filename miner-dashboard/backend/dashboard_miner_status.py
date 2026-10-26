#!/usr/bin/env python3
"""Miner-Status aus miner_status.json (geschrieben von status.py)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUS_FILE = Path(__file__).resolve().parent / "miner_status.json"
_BASE_DIR = STATUS_FILE.parent


def _normalize_status(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("running", "run", "active"):
        return "running"
    if text in ("paused", "pause", "stopped", "stop"):
        return "paused"
    if text in ("error", "offline", "unknown"):
        return "error"
    return text or None


def _parse_hashrate_ths(entry: Dict[str, Any]) -> Optional[float]:
    for key in ("hashrate_ths", "hashrate_th", "ths", "hashrate"):
        if key not in entry or entry[key] is None:
            continue
        try:
            val = float(entry[key])
        except (TypeError, ValueError):
            continue
        if val < 0:
            continue
        if key == "hashrate" and val >= 1000:
            return round(val / 1000.0, 2)
        return round(val, 2)
    return None


def format_hashrate_ths(ths: Optional[float]) -> str:
    if ths is None:
        return "—"
    if ths >= 100:
        return f"{ths:.0f} TH/s"
    if ths >= 10:
        return f"{ths:.1f} TH/s"
    return f"{ths:.2f} TH/s"


def load_miner_status_list() -> List[Dict[str, Any]]:
    data = None
    try:
        from ram_cache import read_json, setup_runtime_dirs

        setup_runtime_dirs(str(_BASE_DIR))
        data = read_json(
            str(_BASE_DIR),
            "miner_status.json",
            default=None,
            scope="backend",
        )
    except Exception:
        data = None
    if data is None and STATUS_FILE.is_file():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    if data is None:
        return []
    entries: List[Dict[str, Any]] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        miners = data.get("miners") or data.get("status") or data.values()
        if isinstance(miners, list):
            entries = miners
        else:
            entries = [{"ip": ip, "status": status} for ip, status in data.items() if ip != "miners"]
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("ip") or "").strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        deduped.append(entry)
    return deduped


def load_miner_status_map() -> Dict[str, str]:
    """IP -> running | paused | error"""
    result: Dict[str, str] = {}
    for entry in load_miner_status_list():
        if not isinstance(entry, dict):
            continue
        ip = entry.get("ip")
        status = _normalize_status(entry.get("status"))
        if ip and status:
            result[str(ip)] = status
    return result


def load_miner_status_details() -> Dict[str, Dict[str, Any]]:
    """IP -> {status, hashrate_ths, hashrate_display, timestamp}"""
    result: Dict[str, Dict[str, Any]] = {}
    for entry in load_miner_status_list():
        if not isinstance(entry, dict):
            continue
        ip = entry.get("ip")
        if not ip:
            continue
        status = _normalize_status(entry.get("status")) or "unknown"
        ths = _parse_hashrate_ths(entry)
        if status != "running":
            ths = 0.0 if status == "paused" else None
        result[str(ip)] = {
            "status": status,
            "hashrate_ths": ths,
            "hashrate_display": format_hashrate_ths(
                ths if status == "running" else (0.0 if status == "paused" else None)
            ),
            "timestamp": entry.get("timestamp"),
        }
    return result


def update_miner_status_entry(ip: str, status: str) -> None:
    """Einzelnen Status in miner_status.json aktualisieren (Best-Effort)."""
    from datetime import datetime

    ip = str(ip)
    status_n = _normalize_status(status) or str(status)
    entries = load_miner_status_list()
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("ip")) != ip:
            continue
        entry["status"] = status_n
        entry["timestamp"] = datetime.now().isoformat()
        if status_n != "running":
            entry["hashrate_ths"] = None
        found = True
        break
    if not found:
        entries.append(
            {
                "ip": ip,
                "status": status_n,
                "hashrate_ths": None,
                "timestamp": datetime.now().isoformat(),
            }
        )
    try:
        from ram_cache import write_json_durable

        write_json_durable(str(_BASE_DIR), "miner_status.json", entries, scope="backend")
    except Exception:
        try:
            STATUS_FILE.write_text(
                json.dumps(entries, indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
