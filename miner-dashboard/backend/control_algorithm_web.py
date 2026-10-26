# -*- coding: utf-8 -*-
"""Flask-Routen-Helfer für Steuerungsalgorithmus."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from control_algorithm.config import (
    default_config,
    load_config,
    load_miners,
    merge_devices_from_miners,
    normalize_device_priority,
    save_config,
)
from control_algorithm.oms_pv_forecast import sync_total_kwp_in_config
from control_algorithm.slot_profiles import normalize_sun_hours
from control_algorithm.service import build_status_payload
from control_algorithm.weather import geocode_city

# Verhindert, dass jeder 5s-Status-Poll einen fehlgeschlagenen Start erneut blockiert.
_LAST_DAEMON_ENSURE_AT = 0.0
_DAEMON_ENSURE_COOLDOWN_SEC = 15.0


def parse_soc_targets_from_form(form) -> List[Dict[str, Any]]:
    raw = form.get("soc_targets_json", "[]")
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            t = str(item.get("time", "")).strip()
            try:
                pct = float(item.get("soc_percent", 0))
            except (TypeError, ValueError):
                continue
            pct = max(0, min(100, pct))
            if t:
                out.append({"time": t, "soc_percent": pct})
        return sorted(out, key=lambda x: x["time"])
    except json.JSONDecodeError:
        return []


def _parse_float_de(raw: Any, default: float = 0.0) -> float:
    try:
        return float(str(raw or "").strip().replace(",", "."))
    except ValueError:
        return default


def parse_run_mode(form) -> Tuple[bool, bool]:
    """
    run_mode: off | test | active
    Returns (enabled, dry_run).
    """
    mode = (form.get("run_mode") or "off").strip()
    if mode == "test":
        return True, True
    if mode == "active":
        return True, False
    return False, True


def parse_devices_from_form(form, miners: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Miner + HA + Gibbo-Pins aus dem Steuerungsformular."""
    keys: List[str] = []
    seen = set()

    def _add(key: str) -> None:
        key = str(key or "").strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    for m in miners:
        _add(m.get("ip") or "")
    try:
        from control_algorithm.config import load_ha_devices, load_gpio_pins

        for h in load_ha_devices():
            _add(h.get("entity_id") or "")
        for g in load_gpio_pins():
            _add(g.get("id") or "")
    except Exception:
        pass
    for k in form.keys():
        name = str(k)
        if name.startswith("algo_device_"):
            _add(name[len("algo_device_") :])
        elif name.startswith("power_kw_"):
            _add(name[len("power_kw_") :])
        elif name.startswith("priority_"):
            _add(name[len("priority_") :])

    devices = []
    for ip in keys:
        enabled = f"algo_device_{ip}" in form
        power_raw = form.get(f"power_kw_{ip}", "0")
        try:
            power_kw = float(str(power_raw).replace(",", "."))
        except ValueError:
            power_kw = 0.0
        priority = normalize_device_priority(form.get(f"priority_{ip}", "5"))
        entry: Dict[str, Any] = {
            "ip": ip,
            "enabled": enabled,
            "power_kw": power_kw,
            "priority": priority,
        }
        try:
            from gpio_pins import is_gpio_device_key, parse_pin_id

            if is_gpio_device_key(ip):
                entry["type"] = "gpio"
                entry["pin"] = parse_pin_id(ip)
            elif "." in ip and not ip[0].isdigit():
                entry["type"] = "ha"
                entry["entity_id"] = ip
        except Exception:
            pass
        devices.append(entry)
    return devices


def save_from_form(form) -> Tuple[Dict[str, Any], str]:
    """
    Speichert Config aus POST-Formular.
    Returns (config, error_message) – error leer bei Erfolg.
    """
    cfg = load_config()
    prev_city = (cfg.get("city") or "").strip()
    prev_lat, prev_lon = cfg.get("lat"), cfg.get("lon")
    prev_display = cfg.get("city_display") or ""
    cfg["enabled"], cfg["dry_run"] = parse_run_mode(form)

    city = (form.get("city") or "").strip()
    cfg["city"] = city

    if city:
        # Geocode darf Speichern (inkl. Testmodus) nicht komplett blockieren:
        # gleiche Stadt + bekannte Koordinaten → bei API-Fehler alte Werte behalten.
        same_city = city.casefold() == prev_city.casefold()
        have_coords = prev_lat is not None and prev_lon is not None
        try:
            lat, lon, display = geocode_city(city)
            cfg["lat"] = lat
            cfg["lon"] = lon
            cfg["city_display"] = display
        except (ValueError, RuntimeError) as e:
            if same_city and have_coords:
                cfg["lat"] = prev_lat
                cfg["lon"] = prev_lon
                cfg["city_display"] = prev_display or city
            else:
                return cfg, str(e)
    else:
        cfg["lat"] = None
        cfg["lon"] = None

    cfg["has_storage"] = "has_storage" in form
    cfg["storage_kwh"] = _parse_float_de(form.get("storage_kwh"), 0)

    cfg["has_pv"] = "has_pv" in form
    cfg["pv_layout"] = (form.get("pv_layout") or "east_west").strip()
    if cfg["pv_layout"] not in ("east_west", "single"):
        cfg["pv_layout"] = "east_west"
    cfg["pv_kwp_east"] = _parse_float_de(form.get("pv_kwp_east"), 95)
    cfg["pv_kwp_west"] = _parse_float_de(form.get("pv_kwp_west"), 95)
    cfg["pv_declination"] = _parse_float_de(form.get("pv_declination"), 30)
    cfg["pv_azimuth_east"] = _parse_float_de(form.get("pv_azimuth_east"), 90)
    cfg["pv_azimuth_west"] = _parse_float_de(form.get("pv_azimuth_west"), 270)
    cfg["pv_azimuth_south"] = _parse_float_de(form.get("pv_azimuth_south"), 180)
    cfg["pv_dc_efficiency"] = max(0.5, min(1.0, _parse_float_de(form.get("pv_dc_efficiency"), 0.93)))
    cfg["open_meteo_base_url"] = (form.get("open_meteo_base_url") or "").strip()
    if cfg["pv_layout"] == "single":
        cfg["pv_kwp"] = _parse_float_de(form.get("pv_kwp"), 0)
    else:
        cfg["pv_kwp"] = round(cfg["pv_kwp_east"] + cfg["pv_kwp_west"], 2)
    sync_total_kwp_in_config(cfg)
    cfg["perfect_weather_sun_hours"] = normalize_sun_hours(
        _parse_float_de(form.get("perfect_weather_sun_hours"), 6)
    )

    cfg["soc_targets"] = parse_soc_targets_from_form(form)
    cfg["min_soc_percent"] = max(
        0.0, min(100.0, _parse_float_de(form.get("min_soc_percent"), 35.0))
    )
    cfg["max_soc_percent"] = max(
        0.0, min(100.0, _parse_float_de(form.get("max_soc_percent"), 95.0))
    )
    if cfg["max_soc_percent"] <= cfg["min_soc_percent"]:
        return cfg, "Max-SoC muss größer als Mindest-SoC sein."
    cfg["max_soc_release_percent"] = max(
        0.0, min(100.0, _parse_float_de(form.get("max_soc_release_percent"), 90.0))
    )
    cfg["max_soc_under_target_percent"] = max(
        0.0, min(30.0, _parse_float_de(form.get("max_soc_under_target_percent"), 5.0))
    )
    if not (
        cfg["min_soc_percent"]
        < cfg["max_soc_release_percent"]
        < cfg["max_soc_percent"]
    ):
        return cfg, (
            "Halt-SoC muss zwischen Mindest-SoC und Max-SoC liegen "
            f"({cfg['min_soc_percent']:.0f} % < Halt < {cfg['max_soc_percent']:.0f} %)."
        )
    for st in cfg.get("soc_targets") or []:
        try:
            pct = float(st.get("soc_percent", 0))
        except (TypeError, ValueError):
            continue
        if pct > cfg["max_soc_percent"]:
            return cfg, (
                f"SOC-Ziel {st.get('time')} ({pct:.0f} %) darf Max-SoC "
                f"({cfg['max_soc_percent']:.0f} %) nicht überschreiten."
            )
    cfg["risk_factor_percent"] = max(
        0.0, min(100.0, _parse_float_de(form.get("risk_factor_percent"), 100.0))
    )
    cfg["consumption_risk_percent"] = max(
        0.0, min(1000.0, _parse_float_de(form.get("consumption_risk_percent"), 0.0))
    )
    cfg["consumption_history_days"] = max(
        1, min(365, int(_parse_float_de(form.get("consumption_history_days"), 60)))
    )
    cfg["consumption_profile_lookback_days"] = max(
        7,
        min(
            365,
            int(
                _parse_float_de(
                    form.get("consumption_profile_lookback_days"), 62
                )
            ),
        ),
    )
    cfg["anomaly_threshold_percent"] = max(
        1.0, min(1000.0, _parse_float_de(form.get("anomaly_threshold_percent"), 20.0))
    )
    cfg["anomaly_window_hours"] = max(
        0.5, min(12.0, _parse_float_de(form.get("anomaly_window_hours"), 1.5))
    )
    cfg["anomaly_clear_stable_minutes"] = max(
        5.0, min(120.0, _parse_float_de(form.get("anomaly_clear_stable_minutes"), 20.0))
    )
    cfg["anomaly_resume_delay_minutes"] = max(
        10.0, min(120.0, _parse_float_de(form.get("anomaly_resume_delay_minutes"), 30.0))
    )
    cfg["anomaly_pause_interval_minutes"] = max(
        5.0, min(60.0, _parse_float_de(form.get("anomaly_pause_interval_minutes"), 15.0))
    )
    if "anomaly_low_enabled_present" in form:
        cfg["anomaly_low_enabled"] = "anomaly_low_enabled" in form
    cfg["anomaly_low_threshold_percent"] = max(
        1.0, min(1000.0, _parse_float_de(form.get("anomaly_low_threshold_percent"), 20.0))
    )
    cfg["anomaly_low_clear_stable_minutes"] = max(
        5.0,
        min(120.0, _parse_float_de(form.get("anomaly_low_clear_stable_minutes"), 20.0)),
    )
    cfg["anomaly_low_resume_interval_minutes"] = max(
        5.0,
        min(60.0, _parse_float_de(form.get("anomaly_low_resume_interval_minutes"), 15.0)),
    )
    if "consumption_pattern_enabled_present" in form:
        cfg["consumption_pattern_enabled"] = "consumption_pattern_enabled" in form
    cfg["consumption_pattern_engage_pct"] = max(
        5.0, min(40.0, _parse_float_de(form.get("consumption_pattern_engage_pct"), 15.0))
    )
    cfg["consumption_pattern_release_pct"] = max(
        3.0, min(30.0, _parse_float_de(form.get("consumption_pattern_release_pct"), 10.0))
    )
    if cfg["consumption_pattern_release_pct"] >= cfg["consumption_pattern_engage_pct"]:
        cfg["consumption_pattern_release_pct"] = max(
            3.0, cfg["consumption_pattern_engage_pct"] - 3.0
        )
    cfg["consumption_pattern_negative_cap_pct"] = max(
        5.0,
        min(50.0, _parse_float_de(form.get("consumption_pattern_negative_cap_pct"), 25.0)),
    )
    cfg["consumption_pattern_positive_cap_pct"] = max(
        5.0,
        min(40.0, _parse_float_de(form.get("consumption_pattern_positive_cap_pct"), 15.0)),
    )
    cfg["consumption_pattern_min_hour"] = int(
        max(6, min(14, _parse_float_de(form.get("consumption_pattern_min_hour"), 8)))
    )
    cfg["consumption_pattern_min_expected_so_far_kwh"] = max(
        3.0,
        min(
            80.0,
            _parse_float_de(form.get("consumption_pattern_min_expected_so_far_kwh"), 8.0),
        ),
    )
    cfg["consumption_pattern_trust"] = max(
        0.25, min(0.95, _parse_float_de(form.get("consumption_pattern_trust"), 0.70))
    )
    cfg["min_device_runtime_hours"] = max(
        0.5, min(24.0, _parse_float_de(form.get("min_device_runtime_hours"), 3.0))
    )
    cfg["min_device_off_hours"] = max(
        0.0, min(12.0, _parse_float_de(form.get("min_device_off_hours"), 1.0))
    )
    cfg["manual_override_hold_hours"] = max(
        0.0, min(48.0, _parse_float_de(form.get("manual_override_hold_hours"), 2.0))
    )
    if "algo_runtime_locks_present" in form:
        raw = str(form.get("algo_runtime_locks_enabled") or "").strip().lower()
        cfg["algo_runtime_locks_enabled"] = raw not in ("0", "false", "off", "aus", "nein")
    if "night_lock_present" in form:
        raw = str(form.get("night_lock_enabled") or "").strip().lower()
        cfg["night_lock_enabled"] = raw not in ("0", "false", "off", "aus", "nein")
    # UI „Ruhezeit“ steuert auch Anti-Flatter-Aus-Commitment (kein separates Feld).
    cfg["switch_commitment_off_hours"] = max(
        0.0,
        min(
            24.0,
            _parse_float_de(
                form.get("switch_commitment_off_hours"),
                cfg["min_device_off_hours"],
            ),
        ),
    )
    # PV-Sicherheitsabschaltung (neue Defaults nur wenn Felder im Formular vorhanden)
    if "pv_safety_enabled_present" in form:
        cfg["pv_safety_enabled"] = "pv_safety_enabled" in form
    cfg["pv_safety_threshold_percent"] = max(
        10.0, min(60.0, _parse_float_de(form.get("pv_safety_threshold_percent"), 20.0))
    )
    cfg["pv_safety_clear_percent"] = max(
        5.0, min(50.0, _parse_float_de(form.get("pv_safety_clear_percent"), 12.0))
    )
    cfg["pv_safety_recheck_minutes"] = max(
        15.0, min(180.0, _parse_float_de(form.get("pv_safety_recheck_minutes"), 60.0))
    )
    cfg["pv_safety_min_hour"] = int(
        max(7, min(14, _parse_float_de(form.get("pv_safety_min_hour"), 9)))
    )
    cfg["pv_safety_confirm_hours"] = int(
        max(1, min(6, _parse_float_de(form.get("pv_safety_confirm_hours"), 2)))
    )
    cfg["pv_safety_min_forecast_so_far_kwh"] = max(
        0.0,
        min(500.0, _parse_float_de(form.get("pv_safety_min_forecast_so_far_kwh"), 8.0)),
    )
    miners = load_miners()
    cfg["devices"] = parse_devices_from_form(form, miners)

    save_config(cfg)
    try:
        from control_algorithm.consumption_history import rebuild_weekday_profiles
        from control_algorithm.config import BACKEND_DIR

        rebuild_weekday_profiles(
            str(BACKEND_DIR),
            lookback_days=int(cfg["consumption_profile_lookback_days"]),
        )
    except Exception:
        pass
    return cfg, ""


def get_page_context() -> Dict[str, Any]:
    config = load_config()
    miners = load_miners()
    devices = merge_devices_from_miners(config, miners)
    try:
        from control_algorithm.config import load_state
        from control_algorithm.device_power_measure import measured_power_payload

        by_ip = (measured_power_payload(load_state()).get("by_ip") or {})
        for d in devices:
            entry = by_ip.get(str(d.get("ip") or "")) or {}
            if entry.get("measured_kw") is not None:
                d["measured_power_kw"] = round(float(entry["measured_kw"]), 2)
            elif d.get("measured_power_kw") is None and config:
                cfg_dev = next(
                    (
                        x
                        for x in (config.get("devices") or [])
                        if str(x.get("ip") or "") == str(d.get("ip") or "")
                    ),
                    None,
                )
                if cfg_dev and cfg_dev.get("measured_power_kw") is not None:
                    d["measured_power_kw"] = cfg_dev.get("measured_power_kw")
    except Exception:
        pass
    return {
        "config": config,
        "miners": miners,
        "devices": devices,
        "soc_targets": config.get("soc_targets") or [],
    }


def api_status() -> Dict[str, Any]:
    """Status für UI; nur persistierter State — kein run_tick im Request-Pfad."""
    import os
    import time
    from datetime import datetime

    from control_algorithm.config import load_state

    global _LAST_DAEMON_ENSURE_AT

    config = load_config()
    state = load_state()
    stale = True
    last = state.get("last_tick")
    try:
        tick_sec = int(os.environ.get("CONTROL_TICK_INTERVAL_SECONDS", "120") or 120)
    except (TypeError, ValueError):
        tick_sec = 120
    stale_after = max(120, int(tick_sec * 1.5) + 60)
    if last:
        try:
            dt = datetime.fromisoformat(str(last))
            stale = (datetime.now() - dt).total_seconds() > stale_after
        except Exception:
            pass

    # Wenn Steuerung aktiv ist, toten Daemon beim Status-Poll wieder anstoßen
    # (nicht erst nach 5-Min-Watchdog oder manuellem Speichern).
    if config.get("enabled"):
        now_m = time.monotonic()
        if now_m - _LAST_DAEMON_ENSURE_AT >= _DAEMON_ENSURE_COOLDOWN_SEC:
            _LAST_DAEMON_ENSURE_AT = now_m
            try:
                from control_algorithm.daemon_control import ensure_control_daemon

                ensure_control_daemon()
            except Exception:
                pass

    payload = build_status_payload(config, state, persisted_only=True)
    if stale:
        payload["stale"] = True
    payload["stale_after_seconds"] = stale_after
    return payload
