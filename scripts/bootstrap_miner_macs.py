#!/usr/bin/env python3
"""Einmalig MAC-Adressen für bestehende Miner ermitteln."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "miner-dashboard" / "backend"
sys.path.insert(0, str(BACKEND))

from ip_recovery import (  # noqa: E402
    discover_mac_for_ip,
    load_miners,
    save_miners,
)


def main() -> int:
    miners = load_miners()
    if not miners:
        print("Keine Miner in miners.json.")
        return 0

    changed = 0
    for miner in miners:
        ip = str(miner.get("ip") or "").strip()
        if not ip:
            continue
        if miner.get("mac"):
            print(f"[OK] {ip}: MAC bereits {miner['mac']}")
            continue
        username = miner.get("username") or "root"
        password = miner.get("password") or ""
        found = discover_mac_for_ip(ip, username, password)
        if found:
            mac, source = found
            miner["mac"] = mac
            miner["mac_source"] = source
            miner.setdefault("recovery_state", "ok")
            miner.setdefault("last_seen_ip", ip)
            changed += 1
            print(f"[OK] {ip}: MAC {mac} (via {source})")
        else:
            miner["recovery_state"] = "missing"
            print(f"[FEHLER] {ip}: nicht erreichbar, recovery_state=missing")

    save_miners(miners)
    print(f"Fertig ({changed} MAC(s) neu gespeichert).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
