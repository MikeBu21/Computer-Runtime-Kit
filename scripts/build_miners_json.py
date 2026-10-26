#!/usr/bin/env python3
"""Erzeugt miners.json aus control_algorithm_config.json."""
import json
from pathlib import Path

ROOT = Path("/home/piminer/minersteuerung")
BACKEND = ROOT / "miner-dashboard" / "backend"
CONFIG = BACKEND / "control_algorithm_config.json"
OUT = ROOT / "config" / "miners.json"

DEFAULT_USER = "root"
DEFAULT_PASSWORD = "Beispiel"  # aus pause_miner.py im Repo


def main() -> None:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    miners = []
    for device in data.get("devices", []):
        if not device.get("enabled", True):
            continue
        miners.append(
            {
                "ip": device["ip"],
                "username": DEFAULT_USER,
                "password": DEFAULT_PASSWORD,
                "model": "Antminer S19",
                "note": f"priority={device.get('priority', 'medium')}",
                "auto": False,
                "time_pause": False,
                "time_ths": False,
            }
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(miners, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    backend_out = BACKEND / "miners.json"
    backend_out.write_text(OUT.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"{len(miners)} Miner nach {OUT} geschrieben")
    try:
        import sys

        sys.path.insert(0, str(BACKEND))
        from ip_recovery import ensure_all_miner_macs

        n = ensure_all_miner_macs()
        if n:
            print(f"{n} MAC-Adresse(n) automatisch ermittelt")
    except Exception as exc:
        print(f"Hinweis: MAC-Ermittlung übersprungen: {exc}")


if __name__ == "__main__":
    main()
