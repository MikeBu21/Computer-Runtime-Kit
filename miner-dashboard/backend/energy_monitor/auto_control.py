# -*- coding: utf-8 -*-
"""
Energie-Automatik: Enline Kelag, gleitender Durchschnitt (avg_minutes.txt),
Miner-Steuerung nach Verbrauch (kW) und Batterie (SOC %).
"""
import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)
import time
import traceback
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

from enline_reader import (
    PAGE_REFRESH_INTERVAL_SECONDS,
    get_latest_kelag_value,
    refresh_power_page,
    start_session,
)
from enline_extra_values import save_dashboard_values

INTERVAL_SECONDS = 6
_EM_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _EM_DIR.parent
ACTION_FILE = _EM_DIR / "last_actions.json"
LATCHES_FILE = _EM_DIR / "control_latches.json"
ACTION_LOG_FILE = "action_log.txt"
MINER_FILE = _BACKEND_DIR / "miners.json"
VERBOSE = os.environ.get("ENERGY_MONITOR_VERBOSE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_ACTION_HEADER_PRINTED = False
_COLLECTING_ANNOUNCED = False
SETTINGS_FILE = _BACKEND_DIR / "shared_settings" / "avg_minutes.txt"
ALGORITHM_CONFIG_FILE = _BACKEND_DIR / "control_algorithm_config.json"
BATTERY_FILES = (
    _EM_DIR / "daten.json",
    _BACKEND_DIR / "daten.json",
    Path.home() / "daten.json",
)


def load_avg_minutes() -> int:
    """Minuten von der Webseite -> Anzahl Messungen (alle 6 s)."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            minutes = int(f.read().strip())
        return max(1, int((minutes * 60) / INTERVAL_SECONDS))
    except Exception as e:
        print(f"Fehler beim Lesen von avg_minutes.txt: {e}")
        return int((45 * 60) / INTERVAL_SECONDS)


def load_miners():
    try:
        with open(MINER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Fehler beim Laden der Miner-Datei: {e}")
        return []


def load_last_actions():
    try:
        with open(ACTION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_last_actions(last_actions):
    with open(ACTION_FILE, "w", encoding="utf-8") as f:
        json.dump(last_actions, f, indent=4)


def _default_latch() -> Dict[str, Any]:
    return {
        "soc": {"pause_armed": True, "resume_armed": True},
        "kw": {"pause_armed": True, "resume_armed": False, "saw_high": False},
        "soc_blocks_kw_resume": False,
    }


def load_latches() -> Dict[str, Any]:
    try:
        with open(LATCHES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_latches(latches: Dict[str, Any]) -> None:
    with open(LATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(latches, f, indent=4)


def _get_latch(latches: Dict[str, Any], ip: str) -> Dict[str, Any]:
    if ip not in latches:
        latches[ip] = _default_latch()
        return latches[ip]
    entry = latches[ip]
    for key, default in _default_latch().items():
        if key not in entry:
            entry[key] = default
        elif isinstance(default, dict):
            for sub_key, sub_val in default.items():
                entry[key].setdefault(sub_key, sub_val)
    entry.setdefault("soc_blocks_kw_resume", False)
    return entry


def _parse_soc(raw) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).strip().replace("%", "").replace(",", "."))
    except ValueError:
        return None


def _find_soc_in_json(value):
    if isinstance(value, dict):
        for key in (
            "soc", "SoC", "SOC", "battery_soc", "battery", "batterie",
            "ladestand", "percent", "percentage", "prozent", "value", "wert",
        ):
            if key in value and value[key] not in (None, ""):
                soc = _parse_soc(value[key])
                if soc is not None:
                    return soc
                nested = _find_soc_in_json(value[key])
                if nested is not None:
                    return nested
        for nested in value.values():
            found = _find_soc_in_json(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_soc_in_json(item)
            if found is not None:
                return found
    else:
        return _parse_soc(value)
    return None


def load_battery_soc() -> Optional[float]:
    try:
        import sys

        backend = Path(__file__).resolve().parent.parent
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from dashboard_battery import load_battery_soc as load_soc_str

        raw = load_soc_str()
        if raw is None:
            return None
        soc = _parse_soc(raw)
        if soc is not None and 0 <= soc <= 100:
            return soc
    except Exception:
        pass
    for path in BATTERY_FILES:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            soc = _find_soc_in_json(data)
            if soc is not None and 0 <= soc <= 100:
                return soc
        except Exception:
            continue
    return None


def _read_kelag_from_ha() -> Optional[float]:
    """Live-Kelag aus HA/RAM (Flask ha_dashboard_loop), ohne Enline."""
    try:
        import sys

        backend = str(_BACKEND_DIR)
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from ram_cache import read_json, read_text

        for name in ("kelag_value.txt", "energy_value.txt"):
            raw = (read_text(backend, name, default="", scope="energy_monitor") or "").strip()
            if not raw:
                continue
            try:
                return float(raw.replace(",", "."))
            except ValueError:
                continue
        payload = read_json(backend, "enline_values.json", default=None, scope="energy_monitor")
        if isinstance(payload, dict):
            kelag = (payload.get("values_kw") or {}).get("kelag")
            if kelag is not None:
                return float(kelag)
    except Exception:
        return None
    return None


def _write_energy_txt(name: str, value: float) -> None:
    text = str(value)
    try:
        import sys

        backend = str(_BACKEND_DIR)
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from ram_cache import is_enabled, write_text

        if is_enabled():
            write_text(backend, name, text, scope="energy_monitor")
            return
    except Exception:
        pass
    (_EM_DIR / name).write_text(text, encoding="utf-8")


def _log_verbose(msg: str) -> None:
    if VERBOSE:
        print(msg)


def _action_log_path() -> Path:
    return Path(__file__).resolve().parent / ACTION_LOG_FILE


def _print_action_header_once() -> None:
    global _ACTION_HEADER_PRINTED
    if _ACTION_HEADER_PRINTED:
        return
    _ACTION_HEADER_PRINTED = True
    print()
    print("Aktionsprotokoll (nur Pause/Resume)")
    print(
        f"{'Zeit':<20} {'Gerät':<16} {'Aktion':<7} {'Grund':<18} "
        f"{'Wert':<14} Grenzen"
    )
    print("-" * 88)


def _log_miner_action(
    ip: str,
    action: str,
    trigger: str,
    value: str,
    limits: str = "",
) -> None:
    """Übersichtliche Zeile für Terminal + action_log.txt."""
    global _ACTION_HEADER_PRINTED
    _print_action_header_once()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{ts:<20} {ip:<16} {action.upper():<7} {trigger:<18} "
        f"{value:<14} {limits}"
    ).rstrip()
    print(line)
    try:
        with open(_action_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def execute_miner_command(ip, action, username="root", password=""):
    """Pause/Resume über Backend miner_control (verifiziert); low/high weiter per control.sh."""
    if action in ("pause", "resume"):
        try:
            backend = str(_BACKEND_DIR)
            if backend not in sys.path:
                sys.path.insert(0, backend)
            from device_registry import execute_miner_command as _backend_exec

            return bool(_backend_exec(ip, action, username, password))
        except Exception:
            try:
                from miner_control import execute_miner_pause_resume

                return bool(execute_miner_pause_resume(ip, action, username, password))
            except Exception:
                return False

    if action in ("low", "high"):
        level = "60" if action == "low" else "100"
        script = str(Path(__file__).resolve().parent.parent / "control.sh")
        result = subprocess.run(
            [script, ip, username, password, level],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "control.sh %s für %s fehlgeschlagen (Code %s): %s",
                action,
                ip,
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:200],
            )
            return False
        return True
    return False


def _apply_action(
    ip: str,
    action: str,
    last_actions: dict,
    trigger: str,
    value: str,
    limits: str,
    username: str,
    password: str,
) -> None:
    _log_miner_action(ip, action, trigger, value, limits)
    execute_miner_command(ip, action, username, password)
    last_actions[ip] = action
    save_last_actions(last_actions)


def _algorithm_managed_ips() -> set:
    """IPs, die der Steuerungsalgorithmus aktiv führt – Auto-Control überspringt sie."""
    try:
        with open(ALGORITHM_CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return set()
    if not cfg.get("enabled"):
        return set()
    return {
        str(d["ip"])
        for d in (cfg.get("devices") or [])
        if d.get("enabled") and d.get("ip")
    }


def control_miners_consumption(
    miners,
    energy_average: float,
    last_actions: dict,
    latches: Dict[str, Any],
) -> None:
    """kW-Steuerung mit Hysterese: 1× Pause/Resume pro Grenzüberschreitung."""
    managed = _algorithm_managed_ips()
    for miner in miners:
        if not miner.get("auto", False):
            continue
        # Batterie-Auto hat Vorrang – kW-Pfad nicht parallel schalten
        if miner.get("auto_battery"):
            continue

        ip = miner["ip"]
        if ip in managed:
            continue
        username = miner.get("username", "root")
        password = miner.get("password", "")

        pause_kw = float(miner.get("pause_kw", 9999) or 9999)
        resume_kw = miner.get("resume_kw")
        if resume_kw is None or resume_kw == "":
            resume_kw = pause_kw - 4
        else:
            resume_kw = float(resume_kw)

        st = _get_latch(latches, ip)
        kw = st["kw"]

        # Nach Neustart: pausiert ohne vorherige Hochlast → kein Resume bei niedrigem kW
        if last_actions.get(ip) == "pause" and not kw["saw_high"]:
            kw["resume_armed"] = False

        # Pause wieder freigeben, wenn Verbrauch unter Resume-Grenze gefallen ist
        if energy_average <= resume_kw:
            kw["pause_armed"] = True

        # Hohen Verbrauch merken (nötig für späteres Resume)
        if energy_average >= pause_kw:
            kw["saw_high"] = True

        action = None
        limits = (
            f"Pause wenn Ø ≥ {pause_kw:g} kW, Resume wenn Ø ≤ {resume_kw:g} kW "
            f"(nach Hochlast)"
        )

        if energy_average >= pause_kw and kw["pause_armed"]:
            kw["pause_armed"] = False
            kw["resume_armed"] = True
            if last_actions.get(ip) != "pause":
                action = "pause"
        elif (
            energy_average <= resume_kw
            and kw["saw_high"]
            and kw["resume_armed"]
            and not st.get("soc_blocks_kw_resume")
            and last_actions.get(ip) in (None, "pause")
        ):
            action = "resume"
            kw["saw_high"] = False
            kw["resume_armed"] = False

        if action:
            _apply_action(
                ip,
                action,
                last_actions,
                "Kelag-Ø",
                f"{energy_average:g} kW",
                limits,
                username,
                password,
            )


def control_miners_battery(
    miners,
    battery_soc: Optional[float],
    last_actions: dict,
    latches: Dict[str, Any],
) -> None:
    """SOC-Steuerung mit Hysterese: 1× Pause/Resume pro Grenzüberschreitung."""
    if battery_soc is None:
        _log_verbose("Batterie-SOC: n/a (daten.json fehlt oder unlesbar)")
        return

    managed = _algorithm_managed_ips()
    for miner in miners:
        if not miner.get("auto_battery", False):
            continue

        ip = miner["ip"]
        if ip in managed:
            continue
        username = miner.get("username", "root")
        password = miner.get("password", "")

        # Web-UI: lower_kw = SOC Resume %, raise_kw = SOC Pause %
        soc_resume = _parse_soc(
            miner.get("lower_kw") or miner.get("soc_resume") or miner.get("battery_resume")
        )
        soc_pause = _parse_soc(
            miner.get("raise_kw") or miner.get("soc_pause") or miner.get("battery_pause")
        )

        if soc_pause is None and soc_resume is None:
            _log_verbose(f"{ip}: Batterie-Automatik aktiv, aber keine SOC-Grenzen gesetzt")
            continue

        st = _get_latch(latches, ip)
        soc = st["soc"]

        # Nach Neustart: bereits pausiert + SOC in Tiefzone → kein erneutes Pause-Kommando
        if (
            soc_pause is not None
            and battery_soc < soc_pause
            and last_actions.get(ip) == "pause"
        ):
            soc["pause_armed"] = False
            st["soc_blocks_kw_resume"] = True

        # Grenze wieder „scharf“: erst Überschreiten, dann darf erneut pausiert/resumed werden
        if soc_pause is not None and battery_soc >= soc_pause:
            soc["pause_armed"] = True
            st["soc_blocks_kw_resume"] = False
        if soc_resume is not None and battery_soc <= soc_resume:
            soc["resume_armed"] = True

        action = None
        parts = []
        if soc_pause is not None:
            parts.append(f"Pause < {soc_pause:g} %")
        if soc_resume is not None:
            parts.append(f"Resume > {soc_resume:g} %")

        if soc_pause is not None and battery_soc < soc_pause and soc["pause_armed"]:
            soc["pause_armed"] = False
            st["soc_blocks_kw_resume"] = True
            if last_actions.get(ip) != "pause":
                action = "pause"
        elif (
            soc_resume is not None
            and battery_soc > soc_resume
            and soc["resume_armed"]
            and last_actions.get(ip) in (None, "pause")
        ):
            action = "resume"
            soc["resume_armed"] = False

        if action:
            _apply_action(
                ip,
                action,
                last_actions,
                "Batterie-SOC",
                f"{battery_soc:g} %",
                ", ".join(parts),
                username,
                password,
            )


def trim_values(values: List[float], rolling_window: int) -> List[float]:
    if len(values) > rolling_window:
        return values[-rolling_window:]
    return values


def run_loop():
    global _COLLECTING_ANNOUNCED
    print("Energieautomatisierung wird gestartet...")
    energy_values: List[float] = []
    last_actions = load_last_actions()
    latches = load_latches()
    rolling_window = load_avg_minutes()
    avg_minutes_label = int(rolling_window * INTERVAL_SECONDS / 60)
    enline_pw = None
    enline_browser = None
    enline_page = None
    last_page_refresh = 0.0
    source_mode = "unset"

    print(
        f"Durchschnittsfenster: {avg_minutes_label} Min ({rolling_window}×{INTERVAL_SECONDS}s) | "
        f"Protokoll: {_action_log_path().name}"
    )
    print(f"Miner-Datei: {MINER_FILE}")
    print(f"Aktionen/Latches: {ACTION_FILE.name} / {LATCHES_FILE.name} in {_EM_DIR}")
    if not VERBOSE:
        print("Detail-Log: ENERGY_MONITOR_VERBOSE=1")

    try:
        ha_probe = _read_kelag_from_ha()
        if ha_probe is not None:
            print(f"HA-Live-Daten verfügbar (Kelag {ha_probe} kW) – Enline nur als Fallback.")
            source_mode = "ha"
        else:
            print("Enline-Login wird versucht (HA-Kelag fehlt)...")
            enline_pw, enline_browser, enline_page = start_session(
                "ab@buttazoni.at", "zMYBWipTVzQul7TEOktW"
            )
            print("Enline-Login erfolgreich.")
            last_page_refresh = time.time()
            source_mode = "enline"

        while True:
            try:
                if enline_page is not None and (
                    time.time() - last_page_refresh >= PAGE_REFRESH_INTERVAL_SECONDS
                ):
                    print("Elinecloud-Seite wird aktualisiert (12h-Intervall)...")
                    refresh_power_page(enline_page)
                    last_page_refresh = time.time()
                    print("Elinecloud-Seite aktualisiert.")

                new_window = load_avg_minutes()
                if new_window != rolling_window:
                    avg_minutes_label = int(new_window * INTERVAL_SECONDS / 60)
                    print(
                        f"[!] Durchschnittsfenster: {avg_minutes_label} Min "
                        f"({new_window} Messungen)"
                    )
                    rolling_window = new_window
                    energy_values = trim_values(energy_values, rolling_window)
                    _COLLECTING_ANNOUNCED = False

                miners = load_miners()
                if not miners:
                    print(
                        f"[!] Keine Miner in {MINER_FILE.name} – "
                        "nur Energiewerte, Steuerung übersprungen."
                    )

                value = _read_kelag_from_ha()
                used_source = "ha"
                if value is None:
                    if enline_page is None:
                        print("HA-Kelag fehlt – starte Enline-Fallback...")
                        enline_pw, enline_browser, enline_page = start_session(
                            "ab@buttazoni.at", "zMYBWipTVzQul7TEOktW"
                        )
                        last_page_refresh = time.time()
                        source_mode = "enline"
                    value = get_latest_kelag_value(enline_page)
                    used_source = "enline"
                    if value is None:
                        raise ValueError("Kein gültiger Kelag-Wert (HA und Enline)")
                    try:
                        extras = save_dashboard_values(enline_page)
                        if extras and VERBOSE:
                            print(
                                "Dashboard:",
                                ", ".join(
                                    f"{k}={v} kW" for k, v in sorted(extras.items())
                                ),
                            )
                    except Exception as ex:
                        print(f"[!] Dashboard-Werte: {ex}")
                else:
                    source_mode = "ha"

                _log_verbose(f"Kelag ({used_source}): {value} kW")
                _write_energy_txt("energy_value.txt", float(value))

                try:
                    import sys

                    backend = Path(__file__).resolve().parent.parent
                    if str(backend) not in sys.path:
                        sys.path.insert(0, str(backend))
                    from energy_monthly_peak_15min import update_monthly_peak
                    from energy_stats_24h import accumulate_from_flow as accumulate_24h
                    from energy_timeseries_24h import maybe_record_sample

                    # kWh-Kreisdiagramme: einmal pro Messung (~6 s)
                    accumulate_24h(str(backend))
                    update_monthly_peak(str(backend))
                    maybe_record_sample(str(backend))
                except Exception as ex:
                    _log_verbose(f"Energie-Extras: {ex}")

                energy_values.append(float(value))
                energy_values = trim_values(energy_values, rolling_window)

                avg = round(mean(energy_values), 2)
                _write_energy_txt("energy_avg.txt", avg)

                pct = min(100, int(100 * len(energy_values) / rolling_window))
                _log_verbose(
                    f"Kelag-Ø ({avg_minutes_label} min): {avg} kW "
                    f"[{len(energy_values)}/{rolling_window}, {pct}%]"
                )

                if miners and len(energy_values) >= rolling_window:
                    _COLLECTING_ANNOUNCED = False
                    battery_soc = load_battery_soc()
                    # Batterie zuerst (SOC blockiert kW-Resume in der Tiefzone)
                    control_miners_battery(miners, battery_soc, last_actions, latches)
                    control_miners_consumption(miners, avg, last_actions, latches)
                    save_latches(latches)
                elif not miners:
                    pass
                elif not _COLLECTING_ANNOUNCED:
                    print(
                        f"Warte auf {avg_minutes_label}-Minuten-Durchschnitt "
                        f"({rolling_window} Messungen à {INTERVAL_SECONDS}s)…"
                    )
                    _COLLECTING_ANNOUNCED = True

            except Exception as loop_err:
                print(f"[!] Fehler: {loop_err}")
                if VERBOSE:
                    traceback.print_exc()
                print("Neuer Versuch in 10 s…")

            time.sleep(INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[!] Automatik wurde mit STRG+C beendet.")

    except Exception:
        traceback.print_exc()

    finally:
        try:
            if enline_browser:
                enline_browser.close()
            if enline_pw:
                enline_pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    run_loop()
