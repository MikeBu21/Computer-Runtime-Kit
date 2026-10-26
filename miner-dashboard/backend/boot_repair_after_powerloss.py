#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boot-Reparatur nach Stromausfall / hartem Abstecken.

Prüft und repariert typische Schäden durch abrupten Stop:
- abgeschnittene / ungültige JSON-Dateien
- falsche Typen (str statt float, None wo Liste erwartet)
- leere Live-Wert-Dateien
- fehlende kritische Python-Module (.py)

Aufruf: vor den Diensten, nach RAM-Hydrate.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent.parent
RAM_ROOT = Path(os.environ.get("MINER_RAM_ROOT", "/dev/shm/minersteuerung"))
LOG_DIR = Path(os.environ.get("MINER_LOG_DIR", str(RAM_ROOT / "logs")))


def _log(msg: str, actions: List[str]) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    actions.append(line)
    print(line)


def _backup_corrupt(path: Path) -> Optional[Path]:
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = path.with_suffix(path.suffix + f".corrupt-{stamp}")
        shutil.copy2(path, dest)
        return dest
    except Exception:
        return None


def _read_json_raw(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"read_error:{exc}"
    if not raw.strip():
        return None, "empty"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        # Versuch: trailing garbage / abgeschnittenes JSON abschneiden
        for i in range(len(raw) - 1, max(len(raw) - 5000, 0), -1):
            try:
                return json.loads(raw[:i]), f"truncated_recovered_at_{i}"
            except Exception:
                continue
        return None, f"json_error:{exc}"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "" or value == "n/a":
        return default
    try:
        return float(str(value).replace(",", ".").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def _fix_list_floats(values: Any, length: int = 24) -> List[float]:
    out = [0.0] * length
    if not isinstance(values, list):
        return out
    for i, v in enumerate(values[:length]):
        out[i] = _as_float(v, 0.0)
    return out


def repair_timeseries(path: Path, actions: List[str]) -> bool:
    data, err = _read_json_raw(path)
    changed = False
    if err and data is None:
        _backup_corrupt(path)
        data = {"date": date.today().isoformat(), "samples": []}
        changed = True
        _log(f"REPAIR timeseries reset ({err}): {path}", actions)
    elif err and data is not None:
        changed = True
        _log(f"REPAIR timeseries truncated JSON ({err}): {path}", actions)

    if not isinstance(data, dict):
        _backup_corrupt(path)
        data = {"date": date.today().isoformat(), "samples": []}
        changed = True

    samples = data.get("samples")
    if not isinstance(samples, list):
        data["samples"] = []
        changed = True
        samples = data["samples"]
        _log(f"REPAIR timeseries samples type -> list: {path}", actions)

    fixed_samples: List[Dict[str, Any]] = []
    for s in samples:
        if not isinstance(s, dict):
            changed = True
            continue
        m = int(_as_float(s.get("m"), 0))
        betrieb = _as_float(s.get("betrieb"), 0.0)
        if betrieb <= 0.05:
            for key in ("line_verbrauch", "line_last", "line_wohnhaus"):
                alt = _as_float(s.get(key), 0.0)
                if alt > 0.05:
                    betrieb = alt
                    break
        item = {
            "m": max(0, min(24 * 60 - 1, m)),
            "betrieb": round(betrieb, 2),
            "miner_kw": round(_as_float(s.get("miner_kw"), 0.0), 2),
            "soc": round(_as_float(s.get("soc"), 0.0), 1) if s.get("soc") not in (None, "") else s.get("soc"),
            "erzeugung": round(_as_float(s.get("erzeugung"), 0.0), 2),
            "netz": round(_as_float(s.get("netz"), 0.0), 2),
            "battery_kw": round(_as_float(s.get("battery_kw"), 0.0), 2),
        }
        # Preserve line_* for UI
        for k, v in s.items():
            if str(k).startswith("line_") and k not in item:
                item[k] = round(_as_float(v), 2) if v not in (None, "") else v
        if item != s:
            changed = True
        fixed_samples.append(item)
    data["samples"] = fixed_samples
    if not data.get("date"):
        data["date"] = date.today().isoformat()
        changed = True
    if changed:
        _write_json(path, data)
    return changed


def repair_consumption_history(path: Path, actions: List[str]) -> bool:
    data, err = _read_json_raw(path)
    changed = False
    if data is None:
        _backup_corrupt(path) if path.is_file() else None
        data = {"days": [], "weekday_profiles": {}}
        changed = True
        _log(f"REPAIR consumption_history reset ({err}): {path}", actions)
    if not isinstance(data, dict):
        data = {"days": [], "weekday_profiles": {}}
        changed = True
    if not isinstance(data.get("days"), list):
        data["days"] = []
        changed = True
        _log(f"REPAIR consumption_history days -> list: {path}", actions)
    if not isinstance(data.get("weekday_profiles"), dict):
        data["weekday_profiles"] = {}
        changed = True
    for day in data["days"]:
        if not isinstance(day, dict):
            continue
        for key in ("hourly_base_kwh", "hourly_total_kwh", "hourly_miner_kwh"):
            if key in day and not isinstance(day.get(key), list):
                day[key] = _fix_list_floats(day.get(key))
                changed = True
            elif isinstance(day.get(key), list):
                fixed = _fix_list_floats(day.get(key))
                if fixed != day.get(key):
                    day[key] = fixed
                    changed = True
    if changed:
        _write_json(path, data)
    return changed


def repair_generic_object_json(
    path: Path,
    actions: List[str],
    *,
    default_factory: Callable[[], Dict[str, Any]],
    required_types: Optional[Dict[str, type]] = None,
) -> bool:
    data, err = _read_json_raw(path)
    changed = False
    if data is None or not isinstance(data, dict):
        if path.is_file():
            _backup_corrupt(path)
        data = default_factory()
        changed = True
        _log(f"REPAIR reset object JSON ({err}): {path}", actions)
    if required_types:
        for key, typ in required_types.items():
            if key not in data:
                continue
            if not isinstance(data[key], typ):
                # type drift after crash
                if typ is float:
                    data[key] = _as_float(data[key])
                elif typ is int:
                    data[key] = int(_as_float(data[key]))
                elif typ is list:
                    data[key] = list(data[key]) if isinstance(data[key], (list, tuple)) else []
                elif typ is dict:
                    data[key] = data[key] if isinstance(data[key], dict) else {}
                elif typ is str:
                    data[key] = "" if data[key] is None else str(data[key])
                else:
                    data[key] = default_factory().get(key)
                changed = True
                _log(f"REPAIR type {key}->{typ.__name__}: {path}", actions)
    if changed:
        _write_json(path, data)
    return changed


def repair_kw_text(path: Path, actions: List[str]) -> bool:
    if not path.is_file():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        _log(f"SKIP kw text read fail {path}: {exc}", actions)
        return False
    if raw == "":
        path.write_text("0\n", encoding="utf-8")
        _log(f"REPAIR empty kw -> 0: {path}", actions)
        return True
    try:
        float(raw.replace(",", "."))
        return False
    except ValueError:
        _backup_corrupt(path)
        path.write_text("0\n", encoding="utf-8")
        _log(f"REPAIR non-numeric kw '{raw[:40]}' -> 0: {path}", actions)
        return True


def check_critical_py(actions: List[str]) -> List[str]:
    try:
        from control_algorithm.maintenance import CRITICAL_SOURCE_MODULES, check_critical_sources

        report = check_critical_sources(BACKEND, CRITICAL_SOURCE_MODULES)
        missing = list(report.get("missing") or [])
        if missing:
            _log(f"WARN missing source modules: {missing}", actions)
        else:
            _log("OK critical .py modules present", actions)
        return missing
    except Exception as exc:
        _log(f"WARN critical module check failed: {exc}", actions)
        return []


def run_repair() -> Dict[str, Any]:
    actions: List[str] = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log("=== Boot-Reparatur Start ===", actions)

    # 1) RAM hydrate first
    try:
        os.environ.setdefault("MINER_RAM_CACHE", "1")
        os.environ.setdefault("MINER_RAM_ROOT", str(RAM_ROOT))
        sys.path.insert(0, str(BACKEND))
        from ram_cache import hydrate_after_powerloss

        hydrate_after_powerloss(str(BACKEND))
        _log("OK hydrate_after_powerloss", actions)
    except Exception as exc:
        _log(f"WARN hydrate failed: {exc}", actions)

    repaired = 0

    # 2) Critical JSON on SD + RAM
    ts_paths = [
        BACKEND / "energy_monitor" / "energy_timeseries_24h.json",
        RAM_ROOT / "energy_monitor" / "energy_timeseries_24h.json",
    ]
    for p in ts_paths:
        if p.is_file() or p.parent.exists():
            if p.parent.exists() and not p.is_file():
                # create empty valid if missing in RAM after hydrate miss
                if "shm" in str(p) or str(RAM_ROOT) in str(p):
                    _write_json(p, {"date": date.today().isoformat(), "samples": []})
                    _log(f"REPAIR create empty timeseries: {p}", actions)
                    repaired += 1
            elif p.is_file() and repair_timeseries(p, actions):
                repaired += 1

    hist = BACKEND / "energy_monitor" / "consumption_history.json"
    if hist.exists() or True:
        if repair_consumption_history(hist, actions):
            repaired += 1

    object_specs: List[Tuple[Path, Callable[[], Dict[str, Any]], Dict[str, type]]] = [
        (
            BACKEND / "control_algorithm_config.json",
            lambda: {},
            {},
        ),
        (
            BACKEND / "control_algorithm_state.json",
            lambda: {},
            {},
        ),
        (
            BACKEND / "control_algorithm_heartbeat.json",
            lambda: {"ts": datetime.now().isoformat()},
            {"ts": str},
        ),
        (
            BACKEND / "maintenance_watchdog_state.json",
            lambda: {},
            {},
        ),
        (
            BACKEND / "energy_monitor" / "energy_stats_24h.json",
            lambda: {"date": date.today().isoformat()},
            {},
        ),
        (
            BACKEND / "energy_monitor" / "enline_values.json",
            lambda: {"values_kw": {}, "updated_at": datetime.now().isoformat()},
            {"values_kw": dict},
        ),
        (
            BACKEND / "energy_monitor" / "control_latches.json",
            lambda: {},
            {},
        ),
        (
            RAM_ROOT / "energy_monitor" / "enline_values.json",
            lambda: {"values_kw": {}, "updated_at": datetime.now().isoformat()},
            {"values_kw": dict},
        ),
        (
            RAM_ROOT / "energy_monitor" / "energy_stats_24h.json",
            lambda: {"date": date.today().isoformat()},
            {},
        ),
    ]
    for path, factory, types in object_specs:
        if path.is_file() or str(RAM_ROOT) in str(path):
            if not path.is_file() and str(RAM_ROOT) in str(path):
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(path, factory())
                _log(f"REPAIR create missing RAM json: {path}", actions)
                repaired += 1
            elif path.is_file() and repair_generic_object_json(
                path, actions, default_factory=factory, required_types=types or None
            ):
                repaired += 1

    # 3) Live kW text files (SD + RAM)
    kw_names = [
        "pv_value.txt",
        "wohnhaus_value.txt",
        "energy_value.txt",
        "bezug_value.txt",
        "lieferung_value.txt",
        "eigenverbrauch_value.txt",
        "kelag_value.txt",
        "verbrauch_gesamt_value.txt",
    ]
    for name in kw_names:
        for base in (BACKEND / "energy_monitor", RAM_ROOT / "energy_monitor"):
            p = base / name
            if p.is_file() and repair_kw_text(p, actions):
                repaired += 1

    missing_py = check_critical_py(actions)

    # 4) Symlink sanity
    settings = BACKEND / "energy_monitor" / "settings"
    target = BACKEND / "shared_settings"
    if settings.is_symlink() or settings.exists():
        try:
            resolved = settings.resolve()
            if not resolved.exists() or "piMiner" in str(settings.readlink() if settings.is_symlink() else ""):
                if settings.exists() or settings.is_symlink():
                    settings.unlink()
                settings.symlink_to(target)
                _log(f"REPAIR settings symlink -> {target}", actions)
                repaired += 1
        except Exception as exc:
            _log(f"WARN symlink check: {exc}", actions)

    summary = {
        "ok": len(missing_py) == 0,
        "repaired": repaired,
        "missing_py": missing_py,
        "actions": actions,
        "finished_at": datetime.now().isoformat(),
    }
    report_path = LOG_DIR / "boot_repair_last.json"
    try:
        _write_json(report_path, summary)
    except Exception:
        report_path = BACKEND / "boot_repair_last.json"
        _write_json(report_path, summary)
    log_path = LOG_DIR / "boot_repair.log"
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(actions) + "\n")
    except Exception:
        pass
    _log(f"=== Boot-Reparatur Ende repaired={repaired} ok={summary['ok']} ===", actions)
    return summary


def main() -> int:
    try:
        summary = run_repair()
        return 0 if summary.get("ok") else 1
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
