# -*- coding: utf-8 -*-
"""Stufentest 200–2000 W über die Dashboard-API, danach Häkchen aus und Stand prüfen."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from ha_device_control import read_ha_entity_state  # noqa: E402

STAGES = list(range(200, 2001, 100))
HOLD_SEC = 300
OFF_SEC = 180
SAMPLE_GAP = 15
API = "http://127.0.0.1:5000"
STATE_FILE = Path("/tmp/bms_watt_stufen_state.json")
LOG_FILE = Path("/tmp/bms_watt_stufen.log")


def log(text: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {text}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def fnum(raw) -> float | None:
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def band(target_w: int) -> int:
    return max(180, int(round(target_w * 0.15)))


def snap() -> dict:
    return {
        "grid": read_ha_entity_state("sensor.192_168_1_176_grid_power"),
        "load": read_ha_entity_state("sensor.192_168_1_176_load_power"),
        "batt": read_ha_entity_state("Beispiel"),
        "soc": read_ha_entity_state("Beispiel"),
        "state": read_ha_entity_state("Beispiel_state"),
        "zexp": read_ha_entity_state("number.192_168_1_176_zero_export_power"),
        "mode": read_ha_entity_state("select.192_168_1_176_work_mode"),
        "pattern": read_ha_entity_state("select.192_168_1_176_energy_pattern"),
        "peak": read_ha_entity_state("number.192_168_1_176_grid_peak_shaving"),
        "exp": read_ha_entity_state("number.192_168_1_176_grid_max_export_power"),
        "charge": read_ha_entity_state("switch.192_168_1_176_battery_grid_charging"),
        "amps": read_ha_entity_state("number.192_168_1_176_battery_grid_charging_current"),
        "p5": read_ha_entity_state("number.192_168_1_176_program_5_soc"),
        "p5c": read_ha_entity_state("select.192_168_1_176_program_5_charging"),
        "p5p": read_ha_entity_state("number.192_168_1_176_program_5_power"),
    }


def api_get() -> dict:
    with urllib.request.urlopen(f"{API}/api/bms-netzbezug", timeout=25) as resp:
        return json.load(resp)


def dashboard_save(payload: dict) -> dict:
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"{API}/api/bms-netzbezug/settings",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)


def idle_payload(data: dict) -> dict:
    settings = data.get("settings") or {}
    return {
        "idle_grid_w": settings.get("idle_grid_w", 20),
        "idle_work_mode": settings.get("idle_work_mode", 2),
        "idle_energy_pattern": settings.get("idle_energy_pattern", 1),
        "idle_peak_w": settings.get("idle_peak_w", 8000),
        "idle_tou_soc": settings.get("idle_tou_soc", 20),
        "idle_tou_charging": settings.get("idle_tou_charging", 1),
        "idle_tou_power_w": settings.get("idle_tou_power_w", 12000),
    }


def enable(watts: int) -> dict:
    data = api_get()
    payload = idle_payload(data)
    payload.update(
        {
            "real_mode": "1",
            "target_grid_kw": f"{watts / 1000.0:.1f}",
            "peak_cap_kw": f"{watts / 1000.0:.1f}",
        }
    )
    saved = dashboard_save(payload)
    log(f"ENABLE {watts}W dashboard real={saved.get('real_mode')} target={saved.get('target_grid_kw')}")
    return saved


def disable() -> dict:
    data = api_get()
    settings = data.get("settings") or {}
    payload = idle_payload(data)
    payload.update(
        {
            "real_mode": "0",
            "target_grid_kw": settings.get("target_grid_kw", 3.0),
            "peak_cap_kw": settings.get("peak_cap_kw", 3.0),
        }
    )
    saved = dashboard_save(payload)
    log(f"DISABLE dashboard real={saved.get('real_mode')} pending={saved.get('release_pending')}")
    return saved


def watch(label: str, seconds: int) -> list[dict]:
    loops = max(1, int(seconds / SAMPLE_GAP))
    rows = []
    log(f"WATCH {label} {seconds}s")
    for index in range(loops):
        row = snap()
        try:
            data = api_get()
            plan = data.get("plan") or {}
            row["write"] = plan.get("write_text")
            row["sent"] = plan.get("command_sent")
            row["real"] = (data.get("settings") or {}).get("real_mode")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            row["write"] = str(exc)
            row["sent"] = None
            row["real"] = None
        rows.append(row)
        log(
            f"{label} {index:02d} grid={row['grid']} load={row['load']} batt={row['batt']} "
            f"soc={row['soc']} zexp={row['zexp']} peak={row['peak']} mode={row['mode']} "
            f"p5={row['p5']} charge={row['charge']} sent={row['sent']}"
        )
        time.sleep(SAMPLE_GAP)
    return rows


def hold_ok(rows: list[dict], target_w: int) -> tuple[bool, str]:
    width = band(target_w)
    tail = rows[-8:]
    near = 0
    export_hits = 0
    wrote = 0
    for row in tail:
        grid = fnum(row["grid"])
        if grid is None:
            continue
        if grid < -50:
            export_hits += 1
        if abs(grid - target_w) <= width and grid >= -50:
            near += 1
        if row.get("sent"):
            wrote += 1
    last = tail[-1] if tail else {}
    peak = fnum(last.get("peak"))
    zexp = fnum(last.get("zexp"))
    reasons = []
    if near < 5:
        reasons.append(f"near={near}/8 band={width}")
    if export_hits:
        reasons.append(f"export={export_hits}")
    if last.get("mode") != "Zero Export To CT":
        reasons.append(f"mode={last.get('mode')}")
    if peak is not None and abs(peak - target_w) > 50:
        reasons.append(f"peak={last.get('peak')}")
    if zexp is not None and zexp > 40:
        reasons.append(f"zexp={last.get('zexp')} soll 20")
    text = (
        f"near={near}/8 export={export_hits} wrote={wrote} last_grid={last.get('grid')} "
        f"peak={last.get('peak')} mode={last.get('mode')} zexp={last.get('zexp')}"
    )
    return not reasons, text if not reasons else f"{'; '.join(reasons)} | {text}"


def disable_ok(rows: list[dict], last_hold_w: int) -> tuple[bool, str]:
    last = rows[-1]
    data = api_get()
    settings = data.get("settings") or {}
    write = ((data.get("plan") or {}).get("write_text") or "")
    zexp = fnum(last.get("zexp"))
    peak = fnum(last.get("peak"))
    grid = fnum(last.get("grid"))
    want_zexp = int(settings.get("idle_grid_w") or 20)
    want_peak = int(settings.get("idle_peak_w") or 8000)
    want_soc = int(settings.get("idle_tou_soc") or 20)
    reasons = []
    if settings.get("real_mode"):
        reasons.append("Häkchen noch an")
    if settings.get("release_pending"):
        reasons.append("Freigabe noch offen")
    if zexp != want_zexp:
        reasons.append(f"zexp={last.get('zexp')} soll {want_zexp}")
    if last.get("mode") != "Zero Export To CT":
        reasons.append(f"mode={last.get('mode')}")
    if last.get("pattern") not in ("Load First", None):
        reasons.append(f"pattern={last.get('pattern')}")
    if peak is not None and abs(peak - want_peak) > 50:
        reasons.append(f"peak={last.get('peak')} soll {want_peak}")
    if fnum(last.get("p5")) not in (None, float(want_soc)):
        reasons.append(f"tou-soc={last.get('p5')} soll {want_soc}")
    if last.get("charge") not in ("off", "0", None):
        reasons.append(f"netzladen={last.get('charge')}")
    if fnum(last.get("amps")) not in (None, 0.0):
        reasons.append(f"ladestrom={last.get('amps')}")
    if "0x0068, Zero Export power), Wert 0" in write:
        reasons.append("Zero Export wurde 0")
    if write and "Geschrieben" in write and "Aus." not in write:
        reasons.append(f"schreibt noch: {write[:80]}")
    if grid is not None and abs(grid - last_hold_w) <= 180:
        reasons.append(f"Netz hängt noch am Testwert {grid}")
    if grid is not None and grid >= 250:
        reasons.append(f"Netz nach Stand zu hoch {grid}")
    text = (
        f"real={settings.get('real_mode')} pending={settings.get('release_pending')} "
        f"zexp={last.get('zexp')} mode={last.get('mode')} pattern={last.get('pattern')} "
        f"peak={last.get('peak')} p5={last.get('p5')} grid={last.get('grid')} "
        f"write={write[:80]}"
    )
    return not reasons, text if not reasons else f"{'; '.join(reasons)} | {text}"


def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"done": [], "failed": [], "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    state = load_state()
    done = set(state.get("done") or [])
    log(f"START stages={STAGES} already_done={sorted(done)}")
    before = api_get().get("settings") or {}
    state["before"] = {
        "real_mode": before.get("real_mode"),
        "target_grid_kw": before.get("target_grid_kw"),
        "peak_cap_kw": before.get("peak_cap_kw"),
    }
    save_state(state)

    for watts in STAGES:
        if watts in done:
            log(f"SKIP {watts}W already done")
            continue
        enable(watts)
        time.sleep(12)
        rows = watch(f"{watts}W", HOLD_SEC)
        ok, text = hold_ok(rows, watts)
        log(f"STAGE_RESULT {watts}W {'PASS' if ok else 'FAIL'} {text}")
        state.setdefault("results", []).append({"watts": watts, "ok": ok, "text": text})
        if not ok:
            state["failed"].append(f"{watts}W")
            save_state(state)
            log(f"STOP_ON_FAIL {watts}W")
            return 1
        done.add(watts)
        state["done"] = sorted(done)
        save_state(state)

    disable()
    time.sleep(12)
    last_hold = STAGES[-1]
    rows = watch("OFF", OFF_SEC)
    ok, text = disable_ok(rows, last_hold)
    log(f"STAGE_RESULT OFF {'PASS' if ok else 'FAIL'} {text}")
    state["off"] = {"ok": ok, "text": text, "last": rows[-1] if rows else {}}
    # Zielwerte der Oberfläche zurück, Schreiben bleibt aus.
    restore = idle_payload(api_get())
    restore.update(
        {
            "real_mode": "0",
            "target_grid_kw": state.get("before", {}).get("target_grid_kw", 3.0),
            "peak_cap_kw": state.get("before", {}).get("peak_cap_kw", 3.0),
        }
    )
    dashboard_save(restore)
    save_state(state)
    if not ok:
        return 2
    log("ALL_DONE ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
