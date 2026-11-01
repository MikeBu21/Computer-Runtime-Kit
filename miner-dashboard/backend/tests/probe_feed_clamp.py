# -*- coding: utf-8 -*-
"""Probe: Export First + TOU discharge, does Export Surplus Power (0x008F) cap the feed-in?"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from bms_seite import _send_holding, load_settings, resolve_ha_link  # noqa: E402
from ha_device_control import read_ha_entity_state as r  # noqa: E402

sell_w = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
discharge_a = int(sys.argv[2]) if len(sys.argv) > 2 else 60
seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 30
full = len(sys.argv) <= 4 or sys.argv[4] != "short"

s = load_settings()
link = resolve_ha_link(s["inverter"], s["known_entry_ids"])
serial = int(link["serial"])
rows = []
if full:
    rows += [(0x0080, 0), (0x0082, 0), (0x008E, 0), (0x008D, 1), (0x0091, 1), (0x0092, 255), (0x00BF, 8000), (0x00E9, 800)]
    for i in range(6):
        rows += [(0x00A6 + i, 20), (0x00AC + i, 0), (0x009A + i, 12000)]
rows += [(0x008F, sell_w), (0x006D, discharge_a)]
for addr, val in rows:
    _send_holding(s["modbus_host"], s["modbus_port"], s["modbus_unit"], serial, addr, val)
print("written", rows, flush=True)
end = time.time() + seconds
while time.time() < end:
    time.sleep(5)
    print(
        time.strftime("%H:%M:%S"),
        "grid", r("sensor.192_168_1_176_grid_power"),
        "load", r("sensor.192_168_1_176_load_power"),
        "pv", r("Beispiel"),
        "battA", r("Beispiel_current"),
        flush=True,
    )
