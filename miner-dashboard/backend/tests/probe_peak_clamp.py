# -*- coding: utf-8 -*-
"""Probe: does Grid Peak shaving cap grid charging? Writes the hold registers once, then watches."""
from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from bms_seite import _send_holding, load_settings, resolve_ha_link  # noqa: E402
from ha_device_control import read_ha_entity_state as r  # noqa: E402

peak_w = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
amps = int(sys.argv[2]) if len(sys.argv) > 2 else 60
max_import_raw = int(sys.argv[3]) if len(sys.argv) > 3 else 800
seconds = int(sys.argv[4]) if len(sys.argv) > 4 else 45

s = load_settings()
link = resolve_ha_link(s["inverter"], s["known_entry_ids"])
serial = int(link["serial"])
rows = [
    (0x008E, 2), (0x008D, 1), (0x0068, 20), (0x0092, 255),
    (0x00E9, max_import_raw), (0x00BF, peak_w), (0x007F, 100), (0x006D, 240),
]
for i in range(6):
    rows += [(0x00A6 + i, 100), (0x00AC + i, 1), (0x009A + i, 12000)]
rows += [(0x0080, amps), (0x0082, 1 if amps else 0)]
for addr, val in rows:
    _send_holding(s["modbus_host"], s["modbus_port"], s["modbus_unit"], serial, addr, val)
print("written", rows, flush=True)
end = time.time() + seconds
while time.time() < end:
    print(
        time.strftime("%H:%M:%S"),
        "grid", r("sensor.192_168_1_176_grid_power"),
        "load", r("sensor.192_168_1_176_load_power"),
        "pv", r("Beispiel"),
        "batt", r("Beispiel"),
        "soc", r("Beispiel"),
        flush=True,
    )
    time.sleep(5)
