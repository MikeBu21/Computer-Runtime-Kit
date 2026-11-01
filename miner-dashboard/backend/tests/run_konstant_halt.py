# -*- coding: utf-8 -*-
"""Konstanz-Test: fester Netzbezug 200 W, 1, 2, 6 kW, dann Einspeisung 1, 2, 6 kW.

Schaltet wie die Oberfläche (save_settings), der Schreib-Thread im Dashboard regelt.
Jede Stufe: Einschwingen, dann HOLD_SEC lang alle GAP Sekunden messen.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from bms_seite import save_settings  # noqa: E402
from ha_device_control import read_ha_entity_state  # noqa: E402

IMPORT_STAGES = [200, 1000, 2000, 6000]
FEED_STAGES = [1000, 3000, 5000]
SETTLE_SEC = 75
HOLD_SEC = 300
SHORT_HOLD_SEC = 120
GAP = 5


def num(entity: str):
    try:
        return float(read_ha_entity_state(entity))
    except (TypeError, ValueError):
        return None


def sample() -> dict:
    return {
        "grid": num("sensor.192_168_1_176_grid_power"),
        "load": num("sensor.192_168_1_176_load_power"),
        "pv": num("Beispiel"),
        "micro": num("sensor.192_168_1_176_microinverter_power"),
        "batt": num("Beispiel"),
        "soc": num("Beispiel"),
    }


def band_w(target: int) -> int:
    return max(100, int(round(abs(target) * 0.05)))


def run_stage(label: str, grid_target: int, hold_sec: int) -> dict:
    save_settings({"real_mode": True, "target_grid_kw": grid_target / 1000.0})
    print(f"STAGE {label}: Soll Netz {grid_target:+d} W, Toleranz ±{band_w(grid_target)} W", flush=True)
    end_settle = time.time() + SETTLE_SEC
    while time.time() < end_settle:
        row = sample()
        print(f"  settle grid={row['grid']} load={row['load']} batt={row['batt']}", flush=True)
        time.sleep(GAP)
    rows = []
    end = time.time() + hold_sec
    while time.time() < end:
        row = sample()
        rows.append(row)
        print(
            f"  {time.strftime('%H:%M:%S')} grid={row['grid']} load={row['load']} pv={row['pv']} "
            f"micro={row['micro']} batt={row['batt']} soc={row['soc']}",
            flush=True,
        )
        time.sleep(GAP)
    grids = [r["grid"] for r in rows if r["grid"] is not None]
    loads = [r["load"] for r in rows if r["load"] is not None]
    band = band_w(grid_target)
    inside = sum(1 for g in grids if abs(g - grid_target) <= band)
    share = inside / len(grids) if grids else 0.0
    mean = statistics.mean(grids) if grids else float("nan")
    ok = bool(grids) and share >= 0.85 and abs(mean - grid_target) <= band
    summary = {
        "label": label,
        "target": grid_target,
        "band": band,
        "n": len(grids),
        "inside": share,
        "mean": mean,
        "min": min(grids) if grids else None,
        "max": max(grids) if grids else None,
        "load_min": min(loads) if loads else None,
        "load_max": max(loads) if loads else None,
        "ok": ok,
    }
    print(
        f"RESULT {label}: {'PASS' if ok else 'FAIL'} mean={mean:.0f} W min={summary['min']} max={summary['max']} "
        f"in band {share * 100:.0f}% (n={len(grids)}), Hauslast {summary['load_min']}–{summary['load_max']} W",
        flush=True,
    )
    return summary


def main() -> int:
    results = []
    try:
        import_stages = [] if "feed" in sys.argv[1:] else IMPORT_STAGES
        for watts in import_stages:
            results.append(run_stage(f"Zukauf {watts} W", watts, HOLD_SEC))
        feed_dead = False
        picked = [int(arg) for arg in sys.argv[1:] if arg.isdigit()]
        for watts in picked or FEED_STAGES:
            hold = SHORT_HOLD_SEC if feed_dead and "short" in sys.argv[1:] else HOLD_SEC
            res = run_stage(f"Einspeisung {watts} W", -watts, hold)
            results.append(res)
            if res["mean"] > -0.1 * watts:
                feed_dead = True
    finally:
        save_settings({"real_mode": False})
        print("DISABLE real_mode=False (Freigabe folgt im Schreib-Thread)", flush=True)
    time.sleep(30)
    print("AFTER", sample(), flush=True)
    print("SUMMARY", flush=True)
    for res in results:
        print(
            f"  {res['label']}: {'PASS' if res['ok'] else 'FAIL'} mean={res['mean']:.0f} "
            f"min={res['min']} max={res['max']} in±{res['band']}W={res['inside'] * 100:.0f}%",
            flush=True,
        )
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
