#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Move outbox weekly error packages to central host (HTTP upload and/or SCP).

Failed transfers stay in outbox (local buffer). Successful ones go to sent/.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load_config(path: Path) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "outbox_dir": "/home/piminer/minersteuerung/logs/error_reports/outbox",
        "sent_dir": "/home/piminer/minersteuerung/logs/error_reports/sent",
        "central_upload_url": "",
        "central_scp": "",  # e.g. user@host:/path/logs/pi
        "scp_ssh_opts": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"],
    }
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"config: {exc}", file=sys.stderr)
    # env overrides
    if os.environ.get("CENTRAL_UPLOAD_URL"):
        cfg["central_upload_url"] = os.environ["CENTRAL_UPLOAD_URL"].strip()
    if os.environ.get("CENTRAL_SCP"):
        cfg["central_scp"] = os.environ["CENTRAL_SCP"].strip()
    return cfg


def pending_packages(outbox: Path) -> List[Tuple[Path, Path]]:
    pairs = []
    for meta in sorted(outbox.glob("piMiner-errors-*.meta.json")):
        label = meta.name.replace(".meta.json", "")
        gz = outbox / f"{label}.jsonl.gz"
        if gz.is_file():
            pairs.append((gz, meta))
    return pairs


def upload_http(url: str, gz: Path, meta: Path) -> bool:
    """POST multipart/form-data with stdlib only."""
    boundary = "----ErrorLogBoundary7f3a"
    parts: List[bytes] = []

    def add_file(field: str, path: Path, content_type: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(path.read_bytes())
        parts.append(b"\r\n")

    add_file("meta", meta, "application/json")
    add_file("package", gz, "application/gzip")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
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
        return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"HTTP upload failed {gz.name}: {exc}", file=sys.stderr)
        return False


def upload_scp(dest: str, gz: Path, meta: Path, ssh_opts: List[str]) -> bool:
    # dest like user@host:/base/logs/pi  → append week label folder from meta
    try:
        meta_data = json.loads(meta.read_text(encoding="utf-8"))
        week = str(meta_data.get("week_label") or "unknown")
    except (OSError, json.JSONDecodeError):
        week = "unknown"
    if ":" not in dest:
        print("central_scp must be user@host:/path", file=sys.stderr)
        return False
    host_part, base_path = dest.split(":", 1)
    remote_dir = f"{base_path.rstrip('/')}/{week}"
    mkdir = subprocess.run(
        ["ssh", *ssh_opts, host_part, f"mkdir -p {remote_dir}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if mkdir.returncode != 0:
        print(f"scp mkdir failed: {mkdir.stderr}", file=sys.stderr)
        return False
    for local in (gz, meta):
        r = subprocess.run(
            ["scp", *ssh_opts, str(local), f"{host_part}:{remote_dir}/"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if r.returncode != 0:
            print(f"scp failed {local.name}: {r.stderr}", file=sys.stderr)
            return False
    return True


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
    outbox = Path(cfg["outbox_dir"])
    sent = Path(cfg["sent_dir"])
    url = str(cfg.get("central_upload_url") or "").strip()
    scp = str(cfg.get("central_scp") or "").strip()
    ssh_opts = list(cfg.get("scp_ssh_opts") or [])

    pairs = pending_packages(outbox)
    if not pairs:
        print("outbox empty")
        return 0

    if not url and not scp:
        print(
            "No central_upload_url / central_scp configured — "
            f"packages stay buffered in {outbox} ({len(pairs)} pending)"
        )
        return 0

    ok_n = 0
    for gz, meta in pairs:
        success = False
        if url:
            success = upload_http(url, gz, meta)
        if not success and scp:
            success = upload_scp(scp, gz, meta, ssh_opts)
        if success:
            mark_sent(gz, meta, sent)
            ok_n += 1
            print(f"sent {gz.name}")
        else:
            print(f"kept in outbox {gz.name}")
    print(f"done sent={ok_n}/{len(pairs)}")
    return 0 if ok_n == len(pairs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
