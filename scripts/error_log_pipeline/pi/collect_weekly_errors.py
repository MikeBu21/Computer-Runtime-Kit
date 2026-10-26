#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect control-service error lines for the past week → JSONL package + buffer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_LOG_DIRS = [
    "/dev/shm/minersteuerung/logs",
    "/home/piminer/minersteuerung/logs",
]
CONTROL_LOG_NAMES = (
    "Steuerungsalgorithmus.log",
    "control_algorithm_daemon.log",
    "maintenance_Steuerung.log",
    "Scheduler.log",
    "boot_repair.log",
)

ERROR_LINE = re.compile(
    r"(?i)("
    r"\btraceback\b|"
    r"\bexception\b|"
    r"\bcritical\b(?!\s+\.py\s+modules)|"  # ignore boot_repair "OK critical .py modules"
    r"(?<!ok\s)\berror\b|"
    r"fehlgeschlagen|"
    r"\bfailed\b|"
    r"\bpanic\b|"
    r"\bfatal\b"
    r")"
)
# Drop clear non-errors even if a keyword matched
SKIP_LINE = re.compile(
    r"(?i)^\s*(\[.*?\]\s*)?(OK\b|INFO\b|DEBUG\b)|OK critical \.py modules"
)
TS_BRACKET = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]\s*(?P<rest>.*)$"
)
TS_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(?P<rest>.*)$"
)


def _now_local() -> datetime:
    return datetime.now().astimezone()


def week_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime, str]:
    """Previous completed ISO week Mon 00:00 → next Mon 00:00, labeled by week-end Sunday date.

    If run mid-week with --rolling-days, use rolling window instead (see main).
    Default weekly job Sunday 03:15: covers last Mon–Sun.
    """
    now = now or _now_local()
    # Start of today local
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Monday of current week
    monday_this = today - timedelta(days=today.weekday())
    # Previous week
    start = monday_this - timedelta(days=7)
    end = monday_this
    sunday = end - timedelta(seconds=1)
    label = sunday.strftime("%Y-%m-%d")
    return start, end, label


def rolling_window(days: int, now: Optional[datetime] = None) -> Tuple[datetime, datetime, str]:
    now = now or _now_local()
    end = now
    start = now - timedelta(days=days)
    label = end.strftime("%Y-%m-%d")
    return start, end, label


def software_version(repo: Path) -> Dict[str, str]:
    out: Dict[str, str] = {"hostname": socket.gethostname()}
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r.returncode == 0:
            out["git_sha"] = r.stdout.strip()
        r2 = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ci"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if r2.returncode == 0:
            out["git_date"] = r2.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def parse_ts(line: str) -> Tuple[Optional[datetime], str]:
    m = TS_BRACKET.match(line)
    if not m:
        m = TS_ISO.match(line)
    if not m:
        return None, line
    raw = m.group("ts").replace(",", ".")
    rest = m.group("rest")
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(raw[:26], fmt)
            return dt.replace(tzinfo=_now_local().tzinfo), rest
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_now_local().tzinfo)
        return dt, rest
    except ValueError:
        return None, line


def iter_log_files(dirs: Iterable[str]) -> List[Path]:
    seen = set()
    files: List[Path] = []
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            continue
        for name in CONTROL_LOG_NAMES:
            p = root / name
            if p.is_file():
                key = (p.stat().st_ino, p.stat().st_dev)
                if key in seen:
                    continue
                seen.add(key)
                files.append(p)
        # also any *Steuerung*.log / *control_algorithm*
        for p in root.glob("*.log"):
            n = p.name.lower()
            if "steuerung" in n or "control_algorithm" in n or n.startswith("maintenance_steuerung"):
                key = (p.stat().st_ino, p.stat().st_dev)
                if key in seen:
                    continue
                seen.add(key)
                files.append(p)
    return files


def extract_errors(
    path: Path,
    start: datetime,
    end: datetime,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events

    lines = text.splitlines()
    i = 0
    last_ts: Optional[datetime] = None
    while i < len(lines):
        line = lines[i]
        ts, rest = parse_ts(line)
        if ts is not None:
            last_ts = ts
        use_ts = ts or last_ts
        if use_ts is not None and (use_ts < start or use_ts >= end):
            i += 1
            continue
        # If no timestamp ever, include only if file mtime in window
        if use_ts is None:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                mtime = None
            if mtime is None or mtime < start or mtime >= end:
                i += 1
                continue

        if not ERROR_LINE.search(line) or SKIP_LINE.search(line):
            i += 1
            continue

        block = [line]
        # Capture following Traceback / indented frames
        j = i + 1
        if re.search(r"(?i)traceback", line) or line.strip().endswith(":"):
            while j < len(lines):
                nxt = lines[j]
                if TS_BRACKET.match(nxt) or TS_ISO.match(nxt):
                    break
                if nxt.startswith(" ") or nxt.startswith("\t") or nxt.startswith("File ") or nxt.startswith("  "):
                    block.append(nxt)
                    j += 1
                    continue
                if re.match(r"^\w+(Error|Exception|Warning):", nxt):
                    block.append(nxt)
                    j += 1
                    break
                break
        msg = "\n".join(block).strip()
        fingerprint = hashlib.sha1(
            re.sub(r"\d+", "#", msg)[:500].encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        events.append(
            {
                "ts": (use_ts or start).isoformat(),
                "source": path.name,
                "path": str(path),
                "message": msg[:4000],
                "fingerprint": fingerprint,
            }
        )
        i = max(j, i + 1)
    return events


def frequency(events: List[Dict[str, Any]], top_n: int = 20) -> List[Dict[str, Any]]:
    counts: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        fp = ev["fingerprint"]
        if fp not in counts:
            counts[fp] = {
                "fingerprint": fp,
                "count": 0,
                "sample": ev["message"][:300],
                "sources": set(),
            }
        counts[fp]["count"] += 1
        counts[fp]["sources"].add(ev.get("source") or "")
    ranked = sorted(counts.values(), key=lambda x: (-x["count"], x["fingerprint"]))
    out = []
    for row in ranked[:top_n]:
        out.append(
            {
                "fingerprint": row["fingerprint"],
                "count": row["count"],
                "sample": row["sample"],
                "sources": sorted(s for s in row["sources"] if s),
            }
        )
    return out


def write_package(
    events: List[Dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    label: str,
    version: Dict[str, str],
    outbox: Path,
    buffer: Path,
) -> Tuple[Path, Path]:
    outbox.mkdir(parents=True, exist_ok=True)
    buffer.mkdir(parents=True, exist_ok=True)
    base = f"piMiner-errors-{label}"
    jsonl_path = buffer / f"{base}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    meta = {
        "host": version.get("hostname") or socket.gethostname(),
        "week_label": label,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "generated_at": _now_local().isoformat(),
        "event_count": len(events),
        "software": version,
        "top_errors": frequency(events),
        "sources_scanned": sorted({e.get("source") for e in events if e.get("source")}),
    }
    meta_path = buffer / f"{base}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    gz_path = outbox / f"{base}.jsonl.gz"
    with jsonl_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        dst.write(src.read())
    out_meta = outbox / f"{base}.meta.json"
    out_meta.write_text(meta_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Keep uncompressed copy in buffer as durable local archive
    return gz_path, out_meta


def load_config(path: Path) -> Dict[str, Any]:
    cfg = {
        "log_dirs": DEFAULT_LOG_DIRS,
        "repo": "/home/piminer/minersteuerung",
        "buffer_dir": "/home/piminer/minersteuerung/logs/error_reports/buffer",
        "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
        "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
    }
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.environ.get(
            "ERROR_LOG_PIPELINE_CONFIG",
            str(Path(__file__).resolve().parent / "config.json"),
        ),
    )
    ap.add_argument("--rolling-days", type=int, default=0, help="If >0, use rolling window instead of last ISO week")
    args = ap.parse_args()
    cfg = load_config(Path(args.config))

    if args.rolling_days > 0:
        start, end, label = rolling_window(args.rolling_days)
    else:
        start, end, label = week_window()

    version = software_version(Path(cfg["repo"]))
    files = iter_log_files(cfg.get("log_dirs") or DEFAULT_LOG_DIRS)
    events: List[Dict[str, Any]] = []
    for p in files:
        events.extend(extract_errors(p, start, end))
    events.sort(key=lambda e: e.get("ts") or "")

    gz, meta = write_package(
        events,
        start=start,
        end=end,
        label=label,
        version=version,
        outbox=Path(cfg["outbox_dir"]),
        buffer=Path(cfg["buffer_dir"]),
    )
    print(f"wrote {gz} events={len(events)} week={label} files={len(files)}")
    print(f"meta {meta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
