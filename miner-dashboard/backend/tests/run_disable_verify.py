# -*- coding: utf-8 -*-
"""Vier Ausschalt-Läufe: Haltung an, dann aus, Halt-Wert weg, kein Zwangszukauf."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from bms_seite import save_settings  # noqa: E402
from ha_device_control import read_ha_entity_state  # noqa: E402

RUNS = 4
HOLD_W = 400


def snap() -> dict:
    return {
        "grid": read_ha_entity_state("sensor.192_168_1_176_grid_power"),
        "load": read_ha_entity_state("Beispiel"),
        "batt": read_ha_entity_state("Beispiel"),
        "state": read_ha_entity_state("Beispiel_state"),
        "zexp": read_ha_entity_state("number.192_168_1_176_zero_export_power"),
        "mode": read_ha_entity_state("select.192_168_1_176_work_mode"),
        "peak": read_ha_entity_state("number.192_168_1_176_grid_peak_shaving"),
        "charge": read_ha_entity_state("switch.192_168_1_176_battery_grid_charging"),
        "amps": read_ha_entity_state("number.192_168_1_176_battery_grid_charging_current"),
        "p5": read_ha_entity_state("number.192_168_1_176_program_5_soc"),
    }


def api() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:5000/api/bms-netzbezug", timeout=20) as resp:
        return json.load(resp)


def fnum(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def wait_hold() -> dict:
    save_settings({"real_mode": True, "target_grid_kw": HOLD_W / 1000.0, "peak_cap_kw": HOLD_W / 1000.0})
    last = {}
    for i in range(10):
        time.sleep(6)
        last = snap()
        print(f"  hold {i:02d} {last}", flush=True)
        grid = fnum(last["grid"])
        if grid is not None and abs(grid - HOLD_W) <= 180:
            break
    return last


def wait_off() -> dict:
    save_settings({"real_mode": False})
    last = {}
    for i in range(20):
        time.sleep(6)
        last = snap()
        print(f"  off  {i:02d} {last}", flush=True)
        zexp = fnum(last["zexp"])
        grid = fnum(last["grid"])
        if (
            zexp == 20
            and grid is not None
            and grid < 80
            and last["mode"] == "Zero Export To CT"
            and last["charge"] in ("off", "0", None)
        ):
            break
    return last


def judge(off: dict) -> tuple[bool, str]:
    settings = json.loads((BACKEND / "bms_test_settings.json").read_text())
    data = api()
    write = ((data.get("plan") or {}).get("write_text") or "")
    zexp = fnum(off["zexp"])
    grid = fnum(off["grid"])
    reasons = []
    if settings.get("real_mode"):
        reasons.append("real_mode still on")
    if settings.get("release_pending"):
        reasons.append("release still pending")
    if zexp != 20:
        reasons.append(f"zexp={off['zexp']} want 20")
    if "0x0068, Zero Export power), Wert 0" in write:
        reasons.append("reset wrote Zero Export 0")
    if grid is None or grid >= 80:
        reasons.append(f"grid still {off['grid']}")
    if off["charge"] not in ("off", "0", None):
        reasons.append(f"grid charge {off['charge']}")
    if fnum(off["amps"]) not in (None, 0.0):
        reasons.append(f"charge amps {off['amps']}")
    if off["mode"] != "Zero Export To CT":
        reasons.append(f"mode {off['mode']}")
    return not reasons, "; ".join(reasons) or "ok"


def main() -> int:
    results = []
    for run in range(1, RUNS + 1):
        print(f"RUN {run} ENABLE {HOLD_W}W", flush=True)
        wait_hold()
        print(f"RUN {run} DISABLE", flush=True)
        off = wait_off()
        ok, text = judge(off)
        print(f"VERIFY_RESULT {run} {'PASS' if ok else 'FAIL'} {text} last={off}", flush=True)
        results.append((run, ok, text, off))
        if not ok:
            return 1
    print("ALL_VERIFY_DONE", results, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
