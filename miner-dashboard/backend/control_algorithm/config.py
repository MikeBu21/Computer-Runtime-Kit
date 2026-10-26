# -*- coding: utf-8 -*-
"""Laden/Speichern der Steuerungsalgorithmus-Konfiguration."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BACKEND_DIR / "control_algorithm_config.json"
STATE_PATH = BACKEND_DIR / "control_algorithm_state.json"
MINERS_PATH = BACKEND_DIR / "miners.json"
HA_DEVICES_PATH = BACKEND_DIR / "ha_devices.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "city": "",
    "lat": None,
    "lon": None,
    "timezone": "Europe/Vienna",
    "has_storage": False,
    "storage_kwh": 0,
    "has_pv": False,
    "pv_kwp": 0,
    "pv_layout": "east_west",
    "pv_kwp_east": 95.0,
    "pv_kwp_west": 95.0,
    "pv_declination": 30.0,
    "pv_azimuth_east": 90.0,
    "pv_azimuth_west": 270.0,
    "pv_azimuth_south": 180.0,
    "pv_dc_efficiency": 0.93,
    "open_meteo_base_url": "",
    "perfect_weather_sun_hours": 6.0,
    "soc_targets": [],
    "devices": [],
    "calibration_hour": 23,
    "calibration_minute": 0,
    "min_soc_percent": 35.0,
    "max_soc_percent": 95.0,
    # Stufe 1: nach Max-SoC alles aus, sobald der Speicher hierhin zurückfällt
    "max_soc_release_percent": 90.0,
    # Halt-Band: SoC darf so viele Punkte unter das Tagesziel fallen,
    # solange die PV den laufenden Miner noch zur Hälfte deckt.
    "max_soc_under_target_percent": 5.0,
    # Autopilot-Ziele: Morgen-Headroom bei schönem Wetter / Abendfüllung
    "morning_soc_percent": 33.0,
    "evening_soc_percent": 90.0,
    "risk_factor_percent": 100.0,
    # Verbrauchsrisiko: 0% = historischer Verbrauch, 100% = doppelter Verbrauch
    "consumption_risk_percent": 0.0,
    "consumption_history_days": 60,
    # Empfohlener Dauer-Zukauf: Anzahl Geräte die 24h durchlaufen sollen
    "dauer_zukauf_always_on_count": 1,
    # Wochentags-Durchschnitt (Mo/Di/…): nur Tage innerhalb dieses Fensters
    # (unabhängig von consumption_history_days = Archiv-Länge)
    "consumption_profile_lookback_days": 62,
    "anomaly_high_enabled": True,
    "anomaly_threshold_percent": 20.0,
    "anomaly_window_hours": 1.5,
    "anomaly_min_history_days": 7,
    "anomaly_clear_stable_minutes": 20.0,
    "anomaly_resume_delay_minutes": 30.0,
    "anomaly_pause_interval_minutes": 15.0,
    # Unterverbrauch-Anomalie: zu wenig Grundlast → gestaffelt einschalten
    "anomaly_low_enabled": True,
    "anomaly_low_threshold_percent": 20.0,
    "anomaly_low_clear_stable_minutes": 20.0,
    "anomaly_low_resume_interval_minutes": 15.0,
    # Verbrauchs-Musterschätzer (Pace vs. Historie → Plan-Skalierung)
    "consumption_pattern_enabled": True,
    "consumption_pattern_engage_pct": 15.0,
    "consumption_pattern_release_pct": 10.0,
    "consumption_pattern_negative_cap_pct": 25.0,
    "consumption_pattern_positive_cap_pct": 15.0,
    "consumption_pattern_min_hour": 8,
    "consumption_pattern_min_expected_so_far_kwh": 8.0,
    "consumption_pattern_trust": 0.70,
    "min_device_runtime_hours": 1.0,
    "min_device_off_hours": 1.0,
    # False: Mindestlaufzeit und An-/Aus-Bindung des Algorithmus sind aus.
    # Die Fremdsteuerung-Sperre nach menschlichem Schalten bleibt davon unberührt.
    "algo_runtime_locks_enabled": True,
    # False: keine erzwungenen Nachtsperren-Pausen. Budget, SoC-Ziel,
    # PV-Prognose, PV-Safety und die übrigen Regeln bleiben aktiv.
    "night_lock_enabled": True,
    # Nach manueller Fremdsteuerung: Algo schaltet Gerät X Stunden nicht
    "manual_override_hold_hours": 2.0,
    # Anti-Flatter: Soft-Tagesempfehlung Ausschaltungen/Gerät (kein Hard-Block)
    "max_device_off_per_day": 3,
    # Harte Regel: max N Einschaltungen (resume) pro Gerät/Tag, dann Sperre bis Mitternacht
    "max_device_on_per_day": 3,
    "hard_daily_on_limit": True,
    # Nach Pause: Mindesthaltedauer aus, bevor Nicht-Notfall-Resume
    "switch_commitment_off_hours": 2.0,
    # Nach Resume: Mindesthaltedauer an (None/fehlend → min_device_runtime_hours)
    "switch_commitment_on_hours": None,
    # Live-Export ≥ X kW darf Aus-Commitment brechen (sonst Ping-Pong)
    "export_commitment_override_kw": 2.0,
    # Füll/Max-SoC: Aus-Commitment brechen wenn hours_to_max ≤ X oder Excess ≥ Y
    "fill_commitment_override_hours_to_max": 2.0,
    "fill_commitment_override_excess_kw": 5.0,
    # Budget/Morgen-Floor: On-Commitment brechen wenn Last−Budget ≥ X kW
    "budget_commitment_override_kw": 3.0,
    # Kurzfristige Budget-Dips: Gerät mit ≥X Plan-Stunden nicht vorzeitig aus
    # (einmal aus wenn Plan-Budget weg / Safety; kein Ein-Aus-Flattern)
    "plan_budget_hold_hours": 1.0,
    "planning_horizon_days": 7,
    # PV-Sicherheitsabschaltung (3. Wetter-Layer). Pace-Referenz ist die aktuelle
    # Live-OMS, nicht die Morgen-Freeze; diese dient nur als Fallback.
    "pv_safety_enabled": True,
    "pv_safety_threshold_percent": 20.0,
    "pv_safety_clear_percent": 12.0,
    "pv_safety_recheck_minutes": 60.0,
    "pv_safety_min_hour": 9,
    "pv_safety_confirm_hours": 2,
    "pv_safety_min_forecast_so_far_kwh": 8.0,
    # Bei PV-Ausfall / Produktionsanomalie: Abend-Lernen komplett sperren
    "steering_learning_block_on_pv_outage": True,
    # Lehrneuron SoC-Priorität: 14 Tage Shadow, danach automatisch Live
    "soc_priority_neuron_enabled": True,
    "soc_priority_neuron_shadow_days": 14,
    "soc_priority_neuron_margin_pct": 5.0,
}


def default_config() -> Dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        cfg = default_config()
        cfg.update(data)
        return cfg
    except Exception:
        return default_config()


def save_config(config: Dict[str, Any]) -> None:
    """Atomar speichern – verhindert halb geschriebene / von Races zerrissene Config."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.flush()
    tmp_path.replace(CONFIG_PATH)


def remove_device_from_config(device_key: str) -> bool:
    """Entfernt Miner/HA-Eintrag aus der Steuerungs-Config (nach Dashboard-Löschen)."""
    key = str(device_key or "").strip()
    if not key:
        return False
    cfg = load_config()
    before = list(cfg.get("devices") or [])
    after = [
        d
        for d in before
        if str(d.get("ip") or d.get("entity_id") or "").strip() != key
    ]
    if len(after) == len(before):
        return False
    cfg["devices"] = after
    save_config(cfg)
    return True


def migrate_device_key(old_key: str, new_key: str) -> bool:
    """Miner-IP in Steuerungs-Config und State umschlüsseln."""
    old_key = str(old_key or "").strip()
    new_key = str(new_key or "").strip()
    if not old_key or not new_key or old_key == new_key:
        return False
    changed = False
    cfg = load_config()
    for dev in cfg.get("devices") or []:
        if str(dev.get("ip") or "").strip() == old_key:
            dev["ip"] = new_key
            changed = True
    if changed:
        save_config(cfg)
    if STATE_PATH.is_file():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)

            def _rekey(obj: Any) -> Any:
                if isinstance(obj, dict):
                    out = {}
                    for k, v in obj.items():
                        nk = new_key if str(k) == old_key else k
                        out[nk] = _rekey(v)
                    return out
                if isinstance(obj, list):
                    return [_rekey(x) for x in obj]
                if isinstance(obj, str) and obj == old_key:
                    return new_key
                return obj

            new_state = _rekey(state)
            if new_state != state:
                with open(STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(new_state, f, ensure_ascii=False, separators=(",", ":"))
                changed = True
        except Exception:
            pass
    return changed


def invalidate_decision_board_cache(state: Dict[str, Any]) -> None:
    """Logikgatter/Entscheidungsboard nach Geräteänderung neu aufbauen."""
    state.pop("decision_board", None)
    state.pop("device_schedule_snapshot", None)


def config_fingerprint(config: Dict[str, Any]) -> str:
    """Hash für Config-Änderungserkennung (Lerndaten werden dabei nicht gelöscht)."""
    parts = [
        str(config.get("city", "")),
        str(config.get("lat")),
        str(config.get("lon")),
        str(config.get("storage_kwh")),
        str(config.get("has_storage")),
        str(config.get("has_pv")),
        str(config.get("pv_layout", "")),
        str(config.get("pv_kwp_east")),
        str(config.get("pv_kwp_west")),
        str(config.get("pv_kwp")),
        str(config.get("pv_declination")),
        str(config.get("pv_azimuth_east")),
        str(config.get("pv_azimuth_west")),
        str(config.get("pv_azimuth_south")),
        str(config.get("pv_dc_efficiency")),
        str(config.get("open_meteo_base_url", "")),
        str(config.get("perfect_weather_sun_hours")),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def load_state() -> Dict[str, Any]:
    default = {
        "yield_factor": 1.0,
        "performance_ratio": 1.0,
        "pv_forecast_detail": None,
        "pv_forecast_cache": None,
        "slot_factors": {
            "morning": 1.0,
            "midday": 1.0,
            "afternoon": 1.0,
            "evening": 1.0,
        },
        "hour_slot_factors": {},
        "config_fingerprint": "",
        "last_weather_fetch": None,
        "forecast_cache": None,
        "morning_pv_forecast_kwh": None,
        "morning_forecast_slots": None,
        "morning_forecast_hours": None,
        "morning_forecast_date": None,
        "yield_history": [],
        "planned_actions": [],
        "accuracy_score": 0.0,
        "days_since_reset": 0,
        "last_calibration": None,
        "pv_yield_today_kwh": None,
        "pv_yield_tomorrow_kwh": None,
        "pv_yield_day_after_kwh": None,
        "eta_days_to_90": 14,
        "daemon_status": "unknown",
        "last_tick": None,
        "live_soc": None,
        "live_pv_kw": None,
        "dry_run": True,
        "pv_reference_fingerprint": None,
        "reference_anchor_kwh": None,
        "reference_anchor_pv_kwp": None,
        "reference_anchor_sun_hours": None,
        "reference_anchor_started_at": None,
        "reference_anchor_until": None,
        "night_lock_active": False,
        "night_lock_reason": None,
        "anomaly_active": False,
        "anomaly_excess_pct": None,
        "anomaly_below_threshold_since": None,
        "anomaly_cleared_at": None,
        "anomaly_last_pause_at": None,
        "anomaly_low_active": False,
        "anomaly_deficit_pct": None,
        "anomaly_low_above_threshold_since": None,
        "anomaly_low_cleared_at": None,
        "anomaly_low_last_resume_at": None,
        "consumption_pattern_estimate": None,
        "consumption_pattern_override": None,
        "pv_safety_active": False,
        "pv_safety_reason": None,
        "pv_safety_triggered_at": None,
        "pv_safety_next_recheck_at": None,
        "pv_safety_deviation_pct": None,
        "pv_safety_cleared_at": None,
        "device_runtime": {},
        "last_hourly_balance_key": None,
        "balance_projection": None,
        "horizon_projection": None,
        "projected_soc_at_target": None,
        "budget_shed_log": [],
    }
    if not STATE_PATH.is_file():
        state = default
    else:
        try:
            from control_algorithm.slot_profiles import (
                default_hour_slot_factors,
                normalize_hour_slot_factors,
            )

            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            default.update(data)
            if not default.get("hour_slot_factors"):
                default["hour_slot_factors"] = default_hour_slot_factors()
            else:
                default["hour_slot_factors"] = normalize_hour_slot_factors(
                    default.get("hour_slot_factors")
                )
            state = default
        except Exception:
            state = default
    try:
        from control_algorithm.learning_persistence import merge_learning_into_state

        state = merge_learning_into_state(state)
    except Exception:
        pass
    return state


def save_state(state: Dict[str, Any]) -> None:
    # Kompakt: jeder Tick schreibt ~400KB – Indent kostet CPU/IO ohne Nutzen.
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    try:
        from control_algorithm.learning_persistence import save_learning_store

        save_learning_store(state)
    except Exception:
        pass


def load_miners() -> List[Dict[str, Any]]:
    if not MINERS_PATH.is_file():
        return []
    try:
        with open(MINERS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def load_ha_devices() -> List[Dict[str, Any]]:
    """Schaltbare HA-Geräte aus ha_devices.json (Dashboard-Liste)."""
    if not HA_DEVICES_PATH.is_file():
        return []
    try:
        with open(HA_DEVICES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_gpio_pins() -> List[Dict[str, Any]]:
    """Schaltbare Gibbo-/GPIO-Pins aus gpio_pins.json."""
    try:
        from gpio_pins import load_gpio_pins_raw

        return load_gpio_pins_raw()
    except Exception:
        return []


def device_key(dev: Dict[str, Any]) -> str:
    """Einheitlicher Schlüssel: Miner-IP, HA entity_id oder gpio:N."""
    return str(
        dev.get("ip") or dev.get("entity_id") or dev.get("id") or ""
    ).strip()


def is_gpio_device(dev: Dict[str, Any]) -> bool:
    if str(dev.get("type") or "").lower() == "gpio":
        return True
    if dev.get("pin") is not None:
        return True
    try:
        from gpio_pins import is_gpio_device_key

        return is_gpio_device_key(device_key(dev))
    except Exception:
        key = device_key(dev).lower()
        return key.startswith("gpio:") or key.startswith("gpio-")


def is_ha_device(dev: Dict[str, Any]) -> bool:
    if is_gpio_device(dev):
        return False
    if str(dev.get("type") or "").lower() == "ha":
        return True
    eid = str(dev.get("entity_id") or "")
    if eid and "." in eid and not eid[0].isdigit():
        return True
    key = device_key(dev)
    return bool(key and "." in key and not key[0].isdigit())


def device_label_map(
    miners: Optional[List[Dict[str, Any]]] = None,
    ha_devices: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Anzeige-Namen für Miner-IPs und HA-Entity-IDs."""
    notes: Dict[str, str] = {}
    for m in miners if miners is not None else load_miners():
        ip = m.get("ip")
        if ip:
            notes[ip] = m.get("note") or m.get("model") or ip
    for h in ha_devices if ha_devices is not None else load_ha_devices():
        eid = h.get("entity_id")
        if eid:
            notes[eid] = h.get("note") or h.get("label") or eid
    for g in load_gpio_pins():
        gid = str(g.get("id") or "")
        if gid:
            notes[gid] = g.get("note") or g.get("label") or gid
    return notes


def normalize_device_priority(raw: Any) -> float:
    """
    Geräte-Priorität 0 … 10 (höher = wichtiger).
    0 ≈ früher „nicht so wichtig“, 10 ≈ früher „hoch“.
    Akzeptiert Komma-Dezimalzahlen und Legacy-Werte high/medium/low.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        level = float(raw)
    else:
        p = str(raw if raw is not None else "5").strip().lower().replace(",", ".")
        if p in ("high", "hoch", "h"):
            level = 10.0
        elif p in ("medium", "mittel", "m", ""):
            level = 5.0
        elif p in ("low", "niedrig", "nicht_so_wichtig", "nicht so wichtig", "l"):
            level = 0.0
        else:
            try:
                level = float(p)
            except ValueError:
                level = 5.0
    level = max(0.0, min(10.0, level))
    level = round(level, 1)
    if abs(level - round(level)) < 1e-9:
        return float(int(round(level)))
    return level


def format_device_priority(raw: Any) -> str:
    """Anzeige für UI/Meldungen, z.B. '10 (hoch)' oder '7,5'."""
    level = normalize_device_priority(raw)
    if abs(level - round(level)) < 1e-9:
        text = str(int(round(level)))
    else:
        text = f"{level:.1f}".replace(".", ",")
    if level >= 9.5:
        return f"{text} (hoch)"
    if level <= 0.5:
        return f"{text} (nicht so wichtig)"
    if abs(level - 5.0) < 0.05:
        return f"{text} (mittel)"
    return text


def _priority_map_from_config(config: Dict[str, Any]) -> Dict[str, float]:
    return {
        device_key(d): normalize_device_priority(d.get("priority"))
        for d in (config.get("devices") or [])
        if device_key(d)
    }


def sort_devices_by_priority(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Höchste Priorität zuerst; bei Gleichstand nach ip/entity_id."""
    return sorted(
        devices,
        key=lambda d: (
            -normalize_device_priority(d.get("priority")),
            str(d.get("ip") or d.get("entity_id") or ""),
        ),
    )


def sort_miners_by_priority(
    miners: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if config is None:
        config = load_config()
    pri = _priority_map_from_config(config)
    return sorted(
        miners,
        key=lambda m: (-pri.get(str(m.get("ip") or ""), 5.0), str(m.get("ip") or "")),
    )


def sort_ha_devices_by_priority(
    ha_devices: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if config is None:
        config = load_config()
    pri = _priority_map_from_config(config)
    return sorted(
        ha_devices,
        key=lambda h: (
            -pri.get(str(h.get("entity_id") or ""), 5.0),
            str(h.get("entity_id") or ""),
        ),
    )


def merge_devices_from_miners(
    config: Dict[str, Any],
    miners: List[Dict[str, Any]],
    ha_devices: Optional[List[Dict[str, Any]]] = None,
    gpio_pins: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Geräte-Liste: Miner + HA + Gibbo-Pins aus dem Dashboard."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for d in config.get("devices") or []:
        key = device_key(d)
        if key:
            by_key[key] = d
    result: List[Dict[str, Any]] = []
    for m in miners:
        ip = m.get("ip")
        if not ip:
            continue
        dev = by_key.get(ip, {})
        result.append(
            {
                "ip": ip,
                "type": "miner",
                "enabled": bool(dev.get("enabled", False)),
                "power_kw": float(dev.get("power_kw") or 0),
                "measured_power_kw": (
                    float(dev["measured_power_kw"])
                    if dev.get("measured_power_kw") is not None
                    else None
                ),
                "priority": normalize_device_priority(dev.get("priority")),
                "note": m.get("note") or m.get("model") or ip,
                "username": m.get("username", "root"),
                "password": m.get("password", ""),
                "auto_conflict": bool(m.get("auto") or m.get("auto_battery")),
            }
        )
    if ha_devices is None:
        ha_devices = load_ha_devices()
    for h in ha_devices:
        eid = (h.get("entity_id") or "").strip()
        if not eid:
            continue
        dev = by_key.get(eid, {})
        label = h.get("note") or h.get("label") or eid
        result.append(
            {
                "ip": eid,
                "entity_id": eid,
                "type": "ha",
                "enabled": bool(dev.get("enabled", False)),
                "power_kw": float(dev.get("power_kw") or 0),
                "measured_power_kw": (
                    float(dev["measured_power_kw"])
                    if dev.get("measured_power_kw") is not None
                    else None
                ),
                "priority": normalize_device_priority(dev.get("priority")),
                "note": label,
                "auto_conflict": bool(h.get("auto") or h.get("auto_battery")),
            }
        )
    if gpio_pins is None:
        gpio_pins = load_gpio_pins()
    for g in gpio_pins:
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        dev = by_key.get(gid, {})
        label = g.get("note") or g.get("label") or gid
        power_default = g.get("power_kw")
        prio_default = g.get("priority")
        result.append(
            {
                "ip": gid,
                "id": gid,
                "pin": g.get("pin"),
                "type": "gpio",
                "enabled": bool(dev.get("enabled", False)),
                "power_kw": float(
                    dev.get("power_kw")
                    if dev.get("power_kw") is not None
                    else (power_default or 0)
                ),
                "measured_power_kw": (
                    float(dev["measured_power_kw"])
                    if dev.get("measured_power_kw") is not None
                    else None
                ),
                "priority": normalize_device_priority(
                    dev.get("priority") if "priority" in dev else prio_default
                ),
                "note": label,
                "auto_conflict": False,
            }
        )
    return sort_devices_by_priority(result)
