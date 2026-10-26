#!/usr/bin/env python3
"""Ältestes funktionierendes enline_reader-Backup wiederherstellen."""
from pathlib import Path
import shutil
from datetime import datetime

EM = Path.home() / "miner-dashboard/backend/energy_monitor"
TARGET = EM / "enline_reader.py"


def main():
    patterns = ["enline_reader.py.bak*", "enline_reader.py.bak-*"]
    backups = []
    for pat in patterns:
        backups.extend(EM.glob(pat))
    backups = sorted(set(backups), key=lambda p: p.stat().st_mtime)

    if not backups:
        raise SystemExit(
            f"Kein Backup in {EM}\n"
            "Suche: ls -la ~/miner-dashboard/backend/energy_monitor/enline_reader*"
        )

    src = backups[-1]
    if TARGET.exists():
        keep = TARGET.with_name(f"enline_reader.py.broken-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(TARGET, keep)
        print(f"Aktuelle Datei gesichert: {keep.name}")

    shutil.copy2(src, TARGET)
    print(f"Wiederhergestellt: {src.name} -> {TARGET}")


if __name__ == "__main__":
    main()
