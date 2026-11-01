# -*- coding: utf-8 -*-
"""Stufentest Netzbezug: 300–1000 W, dazwischen einmal Ausschalten."""
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

STAGES = [300, 400, 500, 600, 700, 800, 1000]
SAMPLES = 16
GAP = 15
BAND = 200


def snap() -> dict:
    return {
        "grid": read_ha_entity_state("sensor.192_168_1_176_grid_power"),
        "load": read_ha_entity_state("sensor.192_168_1_176_load_power"),
        "batt": read_ha_entity_state("Beispiel"),
        "state": read_ha_entity_state("Beispiel_state"),
        "zexp": read_ha_entity_state("number.192_168_1_176_zero_export_power"),
        "mode": read_ha_entity_state("select.192_168_1_176_work_mode"),
        "peak": read_ha_entity_state("number.192_168_1_176_grid_peak_shaving"),
        "exp": read_ha_entity_state("number.192_168_1_176_grid_max_export_power"),
        "p5": read_ha_entity_state("number.192_168_1_176_program_5_soc"),
    }


def api() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:5000/api/bms-netzbezug", timeout=20) as resp:
        return json.load(resp)


def fnum(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def watch(label: str, target_w: int | None, seconds_loops: int) -> list[dict]:
    print(f"WATCH {label}", flush=True)
    rows = []
    for i in range(seconds_loops):
        row = snap()
        rows.append(row)
        print(
            f"{i:02d} grid={row['grid']} load={row['load']} batt={row['batt']} "
            f"state={row['state']} zexp={row['zexp']} peak={row['peak']} "
            f"exp={row['exp']} mode={row['mode']} p5={row['p5']}",
            flush=True,
        )
        time.sleep(GAP)
    return rows


def hold_ok(rows: list[dict], target_w: int) -> tuple[bool, str]:
    tail = rows[-6:]
    near = 0
    export_hits = 0
    for row in tail:
        grid = fnum(row["grid"])
        if grid is None:
            continue
        if grid < -50:
            export_hits += 1
        if abs(grid - target_w) <= BAND and grid >= -50:
            near += 1
    text = f"near={near}/6 export={export_hits} last={tail[-1] if tail else None}"
    return near >= 4 and export_hits == 0, text


def disable_ok(rows: list[dict]) -> tuple[bool, str]:
    last = rows[-1]
    zexp = fnum(last["zexp"])
    settings = json.loads((BACKEND / "bms_test_settings.json").read_text())
    data = api()
    write = ((data.get("plan") or {}).get("write_text") or "")
    wrote_zero = "0x0068, Zero Export power), Wert 0" in write
    real = bool(settings.get("real_mode"))
    pending = bool(settings.get("release_pending"))
    ok = (
        not real
        and not pending
        and zexp == 20
        and not wrote_zero
        and last["mode"] == "Zero Export To CT"
    )
    text = (
        f"real={real} pending={pending} zexp={last['zexp']} mode={last['mode']} "
        f"grid={last['grid']} wrote_zero_export={wrote_zero}"
    )
    return ok, text


def enable(watts: int) -> None:
    kw = watts / 1000.0
    saved = save_settings({"real_mode": True, "target_grid_kw": kw, "peak_cap_kw": kw})
    print("ENABLE", watts, {k: saved[k] for k in ("real_mode", "target_grid_kw", "peak_cap_kw")}, flush=True)
    time.sleep(12)


def disable() -> None:
    saved = save_settings({"real_mode": False})
    print("DISABLE", {k: saved[k] for k in ("real_mode", "release_pending")}, flush=True)
    time.sleep(12)


def main() -> int:
    failed = []
    enable(300)
    rows = watch("300W", 300, SAMPLES)
    ok, text = hold_ok(rows, 300)
    print("STAGE_RESULT 300W", "PASS" if ok else "FAIL", text, flush=True)
    if not ok:
        failed.append("300W")
        print("STOP_ON_FAIL 300W", flush=True)
        return 1

    disable()
    rows = watch("OFF", None, 8)
    ok, text = disable_ok(rows)
    print("STAGE_RESULT OFF", "PASS" if ok else "FAIL", text, flush=True)
    if not ok:
        failed.append("OFF")
        print("STOP_ON_FAIL OFF", flush=True)
        return 1

    for watts in STAGES[1:]:
        enable(watts)
        rows = watch(f"{watts}W", watts, SAMPLES)
        ok, text = hold_ok(rows, watts)
        print("STAGE_RESULT", f"{watts}W", "PASS" if ok else "FAIL", text, flush=True)
        if not ok:
            failed.append(f"{watts}W")
            print("STOP_ON_FAIL", watts, flush=True)
            return 1
    print("ALL_STAGES_DONE", failed or "ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
