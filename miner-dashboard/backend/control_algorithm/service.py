# -*- coding: utf-8 -*-
"""Zentrale Tick-Logik für Daemon und Status-API."""
from __future__ import annotations

import sys
import threading
import time
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from control_algorithm.calibration import (
    eta_message,
    maybe_reset_on_config_change,
    maybe_revert_inconsistent_last_calibration,
    run_evening_calibration,
    store_morning_forecast,
)
from control_algorithm.config import (
    BACKEND_DIR,
    is_ha_device,
    load_config,
    load_ha_devices,
    load_miners,
    load_state,
    save_state,
)
from control_algorithm.live_data import get_actual_pv_kwh_today, load_live_snapshot
from control_algorithm.oms_pv_forecast import (
    ENGINE_NAME,
    fetch_pv_forecast,
    normalize_performance_ratio,
)
from control_algorithm.anomaly_detection import check_anomaly
from control_algorithm.consumption_history import (
    build_consumption_forecast_display,
    load_history,
    profile_sample_count,
)
from control_algorithm.energy_balance import (
    build_consumption_estimate_summary,
    build_energy_budget_snapshot,
    build_multi_day_energy_snapshots,
    build_weekly_energy_plan,
    compute_24h_absorption_plan,
    maybe_run_hourly_balance,
)
from control_algorithm.device_plan import build_device_control_plans
from control_algorithm.device_schedule_24h import (
    build_device_schedule_24h,
    persist_device_schedule_24h,
)
from control_algorithm.load_scheduler import (
    apply_device_runtime_guards,
    apply_device_state_updates,
    check_min_soc_emergency,
    plan_load_actions,
)
from control_algorithm.planner import apply_hysteresis, switch_times_from_state
from control_algorithm.pv_pattern_estimate import (
    estimate_today_pv_from_patterns,
    pv_estimate_trace_payload,
    update_state_pattern_estimate,
)
from control_algorithm.reference_anchor import (
    anchor_status_message,
    effective_perfect_day_kwh,
    is_reference_anchor_active,
    reference_anchor_hours_remaining,
)
from control_algorithm.slot_profiles import (
    format_slot_summary,
    get_actual_pv_kwh_by_hours_today,
    get_actual_pv_kwh_by_slots_today,
    normalize_sun_hours,
)
from control_algorithm.steering_history import (
    build_steering_stats_payload,
    open_pause_ips,
    record_steering_actions,
)

# Miner-Befehle: immer Backend device_registry / miner_control (SSH+gRPC+Verify).
# NICHT energy_monitor/auto_control — dort ist pause nur blindes nc→DEVNULL (None=„OK“).
_EM_DIR = BACKEND_DIR / "energy_monitor"
_backend_str = str(BACKEND_DIR)
if _backend_str not in sys.path:
    sys.path.insert(0, _backend_str)
if str(_EM_DIR) not in sys.path:
    sys.path.append(str(_EM_DIR))


def execute_miner_command(ip, action, username="root", password=""):  # type: ignore
    """Pause/Resume mit Status-Verifikation (device_registry)."""
    try:
        from device_registry import execute_miner_command as _exec

        return bool(_exec(ip, action, username, password))
    except Exception:
        # Letzter Fallback: miner_control direkt
        try:
            from miner_control import execute_miner_pause_resume

            if action in ("pause", "resume"):
                return bool(execute_miner_pause_resume(ip, action, username, password))
        except Exception:
            pass
        return False


def _miner_command_ok(result: Any) -> bool:
    """Nur explizites True zählt — None/False = fehlgeschlagen."""
    return result is True


WEATHER_INTERVAL_SECONDS = 2 * 3600  # alle 2 Stunden frische PV-Prognose
# Keine Wetter-/Anker-Schreibung um Mitternacht (Tagessprung / Cache-Label-Fehler)
WEATHER_MIDNIGHT_QUIET_START_MIN = 23 * 60 + 45  # 23:45
WEATHER_MIDNIGHT_QUIET_END_MIN = 1 * 60 + 15  # 01:15
_last_weather_fetch: float = 0
_last_action_times: Dict[str, float] = {}
_last_applied_action: Dict[str, str] = {}


def in_weather_midnight_quiet(now: Optional[datetime] = None) -> bool:
    """True in der Mitternachts-Ruhezone – kein Prognose-Schreiben/Fetch."""
    now = now or datetime.now()
    mins = now.hour * 60 + now.minute
    start = WEATHER_MIDNIGHT_QUIET_START_MIN
    end = WEATHER_MIDNIGHT_QUIET_END_MIN
    if start > end:
        return mins >= start or mins < end
    return start <= mins < end


def refresh_weather(config: Dict[str, Any], state: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    global _last_weather_fetch
    now = time.time()
    now_dt = datetime.now()

    # Mitternacht: keine Prognose — verhindert falsche Tag-Labels im Cache-Fallback
    if in_weather_midnight_quiet(now_dt):
        state["weather_refresh_note"] = (
            f"PV/Wetter-Prognose pausiert (Mitternacht {now_dt.strftime('%H:%M')}) "
            f"– nächster Lauf nach 01:15."
        )
        return state

    detail = state.get("pv_forecast_detail") or {}
    if not detail.get("hours_today") or not state.get("pv_forecast_today_hours"):
        force = True
    # Sticky error statuses must not sit for the full weather interval — retry
    # so recoverable cases (e.g. missing package now installed) clear promptly.
    status = str(state.get("daemon_status") or "")
    if status.startswith(("fallback:", "forecast_error:", "weather_error:", "recovering:")):
        force = True
    last_iso = state.get("last_weather_fetch")
    if last_iso and not force:
        try:
            age = (now_dt - datetime.fromisoformat(str(last_iso))).total_seconds()
            if age < WEATHER_INTERVAL_SECONDS:
                return state
        except Exception:
            pass
    elif not force and (now - _last_weather_fetch) < WEATHER_INTERVAL_SECONDS:
        return state
    lat, lon = config.get("lat"), config.get("lon")
    if lat is None or lon is None:
        state["daemon_status"] = "no_coordinates"
        return state
    try:
        result = fetch_pv_forecast(config, state)
        state["last_weather_fetch"] = now_dt.isoformat()
        _last_weather_fetch = now

        if config.get("has_pv") and float(config.get("pv_kwp") or 0) > 0:
            state["pv_yield_today_kwh"] = result.today_kwh
            state["pv_yield_tomorrow_kwh"] = result.tomorrow_kwh
            state["pv_yield_day_after_kwh"] = result.day_after_kwh
            state["pv_forecast_today_slots"] = result.today_slots
            state["pv_forecast_tomorrow_slots"] = result.tomorrow_slots
            state["pv_forecast_day_after_slots"] = result.day_after_slots
            state["pv_forecast_today_hours"] = result.today_hours
            state["pv_forecast_tomorrow_hours"] = result.tomorrow_hours
            state["pv_forecast_day_after_hours"] = result.day_after_hours
            state["pv_forecast_by_date"] = result.pv_forecast_by_date or {}
            state["pv_forecast_detail"] = result.detail
            if result.cache:
                state["pv_forecast_cache"] = result.cache
            # Referenzprognose bei jedem guten Live-Fetch aktualisieren (alle ~2 h),
            # nie aus Cache-Fallback und nie in der Mitternachts-Ruhezone.
            if (
                not result.fallback_active
                and not result.error
                and not in_weather_midnight_quiet(now_dt)
                and float(result.today_kwh or 0) > 0.05
            ):
                store_morning_forecast(
                    state,
                    result.today_kwh,
                    result.today_slots,
                    date.today(),
                    result.today_hours,
                )
                state["pv_forecast_reference_updated_at"] = now_dt.isoformat()

        if result.fallback_active:
            state["daemon_status"] = f"fallback: {result.error or 'cache'}"
        elif result.error:
            state["daemon_status"] = f"forecast_error: {result.error}"
        else:
            state["daemon_status"] = "ok"
        state["weather_refresh_note"] = (
            f"PV/Wetter-Prognose aktualisiert {now_dt.strftime('%d.%m. %H:%M')} "
            f"(heute {state.get('pv_yield_today_kwh', '?')} kWh, "
            f"morgen {state.get('pv_yield_tomorrow_kwh', '?')} kWh)"
        )
    except Exception as e:
        state["daemon_status"] = f"weather_error: {e}"
    return state


def maybe_run_calibration(
    config: Dict[str, Any],
    state: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Abend-Lernen einmal pro Kalendertag ab Kalibrierzeit.

    Kein exakter Minuten-Treffer nötig: bei 10‑Minuten-Ticks und beim
    Wartungs-Nachzug gilt das Fenster „ab calibration_hour:minute bis Mitternacht“.
    """
    from control_algorithm.maintenance import is_calibration_due

    ch = int(config.get("calibration_hour", 23))
    cm = int(config.get("calibration_minute", 0))
    now = now or datetime.now()
    key = f"cal_{now.date().isoformat()}"
    if not is_calibration_due(now, ch, cm, state.get("last_calibration_run_key")):
        return state

    # Globaler Lern-Sperrschalter: bei PV-Ausfall / Produktionsanomalie
    # keine Abend-Parameter-Anpassung (Neuronen + PV-Kalibrierung).
    learning_blocked = False
    block_reason = ""
    try:
        from control_algorithm.steering_learning import (
            learning_blocked_by_pv_outage,
        )

        learning_blocked, block_reason = learning_blocked_by_pv_outage(
            config, state
        )
    except Exception:
        learning_blocked = False
    if learning_blocked:
        state["steering_learning_skip_reason"] = block_reason
        state["last_calibration_note"] = f"skipped_pv_outage: {block_reason}"
        state["last_soc_calibration_note"] = f"skipped_pv_outage: {block_reason}"
        try:
            from control_algorithm.steering_learning import (
                run_evening_steering_learning,
            )

            # Schreibt Skip-Notiz / History, ändert keine Params
            state = run_evening_steering_learning(
                state, config, str(BACKEND_DIR)
            )
        except Exception as e:
            state["steering_learning_error"] = str(e)
    else:
        state = run_evening_calibration(state, config, str(BACKEND_DIR))
        try:
            from control_algorithm.steering_learning import (
                run_evening_steering_learning,
            )

            state = run_evening_steering_learning(
                state, config, str(BACKEND_DIR)
            )
        except Exception as e:
            state["steering_learning_error"] = str(e)
        try:
            from control_algorithm.pv_pattern_estimate import (
                run_evening_forecast_pace_learning,
            )

            state = run_evening_forecast_pace_learning(state)
        except Exception as e:
            state["forecast_pace_learning_error"] = str(e)
        try:
            from control_algorithm.soc_calibration import run_soc_calibration

            state = run_soc_calibration(state, config, str(BACKEND_DIR))
        except Exception as e:
            state["soc_calibration_error"] = str(e)
    state["last_calibration_run_key"] = key
    return state


def _append_pv_learning_messages(
    config: Dict[str, Any],
    state: Dict[str, Any],
    messages: List[str],
    base_dir: str,
) -> None:
    if not config.get("has_pv"):
        return
    pv_kwp = float(config.get("pv_kwp") or 0)
    actual_kwh = get_actual_pv_kwh_today(base_dir)
    state["live_pv_kwh_actual"] = actual_kwh
    slot_actual = get_actual_pv_kwh_by_slots_today(base_dir)
    hour_actual = get_actual_pv_kwh_by_hours_today(base_dir)
    state["slot_actual_today"] = slot_actual
    state["hour_actual_today"] = hour_actual
    update_state_pattern_estimate(state)

    detail = state.get("pv_forecast_detail") or {}
    pr = normalize_performance_ratio(state.get("performance_ratio"))
    today_slots = state.get("pv_forecast_today_slots") or {}

    messages.insert(0, f"Live PV heute (Kreisdiagramm): {actual_kwh:.1f} kWh")
    try:
        from control_algorithm.pv_pattern_estimate import forecast_pace_info_messages

        for line in forecast_pace_info_messages(state):
            messages.append(line)
    except Exception:
        pass
    sun_h = normalize_sun_hours(config.get("perfect_weather_sun_hours"))
    if pv_kwp > 0:
        ref_kwh = effective_perfect_day_kwh(state, config)
        anchor_note = " [Anker 24h aktiv]" if is_reference_anchor_active(state) else ""
        messages.insert(
            1,
            f"Vollsonne-Referenz: {ref_kwh:.0f} kWh "
            f"({pv_kwp:.0f} kWp × {sun_h:g} h){anchor_note}",
        )
    anchor_msg = anchor_status_message(state, config)
    if anchor_msg:
        messages.insert(2, anchor_msg)

    messages.append(f"PV-Engine: {detail.get('engine', ENGINE_NAME)}")
    if detail.get("formula"):
        messages.append(f"Formel: {detail['formula']}")
    if detail.get("fallback_active"):
        messages.append(f"⚠ Fallback aktiv (gespeicherte Prognose): {detail.get('error', '')}")
    raw = detail.get("today_raw_kwh")
    adj = detail.get("today_adj_kwh")
    if raw is not None and adj is not None:
        hf_count = sum(
            1 for v in (detail.get("hour_slot_factors") or {}).values()
            if abs(float(v) - 1.0) > 0.02
        )
        hour_note = f", {hf_count} Stunden-Faktoren ≠ 1" if hf_count else ""
        messages.append(
            f"Heute Roh-Prognose: {float(raw):.1f} kWh × PR {pr:.3f}{hour_note} "
            f"= {float(adj):.1f} kWh"
        )
    tm_raw = detail.get("tomorrow_raw_kwh")
    tm_adj = detail.get("tomorrow_adj_kwh")
    if tm_raw is not None and tm_adj is not None:
        messages.append(
            f"Morgen Roh-Prognose: {float(tm_raw):.1f} kWh × PR {pr:.3f} = {float(tm_adj):.1f} kWh"
        )
    if detail.get("peak_time_today"):
        messages.append(
            f"Peak heute: {detail['peak_time_today']} Uhr, "
            f"ca. {int(detail.get('peak_power_w') or 0) / 1000:.1f} kW"
        )
    if detail.get("weather_source"):
        messages.append(f"Wetter-Quelle: {detail['weather_source']}")
    if detail.get("fetched_at"):
        messages.append(f"Letzter Abruf: {detail['fetched_at']}")

    for plane in detail.get("planes") or []:
        messages.append(
            f"Ebene {plane.get('label')}: {plane.get('kwp')} kWp, "
            f"Neigung {plane.get('declination')}°, "
            f"Azimut {plane.get('azimuth_compass')}° "
            f"(Open-Meteo {plane.get('azimuth_oms')}°), "
            f"Anteil {plane.get('share_percent')}%"
        )

    for line in format_slot_summary(today_slots, slot_actual):
        messages.append(line)
    hf = detail.get("hour_slot_factors") or state.get("hour_slot_factors") or {}
    tuned = [hid for hid, v in hf.items() if abs(float(v) - 1.0) > 0.02]
    if tuned:
        messages.append(
            f"Stunden-Kalibrierung aktiv: {len(tuned)} von 18 Faktoren angepasst (05–22 Uhr)"
        )
    messages.append(f"Performance Ratio (PR): {pr:.3f}")



def _refresh_consumption_estimates_from_state(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
    *,
    run_id: str = "pre-fix",
) -> Dict[str, Any]:
    """Verbrauchskarte/Wochenplan an aktuelles Horizon-Budget koppeln (leichtgewichtig)."""
    from control_algorithm.energy_balance import _agent_debug_log

    now = now or datetime.now()
    snapshots = state.get("energy_budget_snapshots") or []
    if not snapshots:
        return state
    week_plan = build_weekly_energy_plan(config, state, live, base_dir, now)
    state["energy_week_plan"] = week_plan
    state["consumption_estimates"] = build_consumption_estimate_summary(
        snapshots, base_dir, now=now, week_plan=week_plan, config=config
    )
    d0 = (week_plan.get("days") or [{}])[0]
    # #region agent log
    _agent_debug_log(
        "H5",
        "service.py:_refresh_consumption_estimates_from_state",
        "lightweight consumption refresh",
        {
            "ui_total_today_kwh": (state.get("consumption_estimates") or {})
            .get("total_with_miners", {})
            .get("today_kwh"),
            "week_miner_kwh": (d0.get("daily_totals") or {}).get("miner_kwh"),
            "week_pv_kwh": (d0.get("daily_totals") or {}).get("pv_kwh"),
            "horizon_budget_kw": (state.get("horizon_projection") or {}).get(
                "miner_budget_kw"
            ),
        },
        run_id=run_id,
    )
    # #endregion
    return state


def _maybe_persist_energy_budget_snapshots(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Persist multi-day energy carousel in state (hourly throttle)."""
    from control_algorithm.energy_balance import _agent_debug_log

    now = now or datetime.now()
    key = f"hourly_balance_{now.date().isoformat()}_{now.hour}"
    if (
        state.get("last_energy_budget_snapshots_key") == key
        and state.get("energy_budget_snapshots")
    ):
        # #region agent log
        stale_wp = ((state.get("energy_week_plan") or {}).get("days") or [{}])[0]
        _agent_debug_log(
            "H4",
            "service.py:_maybe_persist_energy_budget_snapshots",
            "throttle skip hourly refresh",
            {
                "reason": "same_hour_key",
                "key": key,
                "stale_ui_total_kwh": (state.get("consumption_estimates") or {})
                .get("total_with_miners", {})
                .get("today_kwh"),
                "stale_week_miner_kwh": (stale_wp.get("daily_totals") or {}).get(
                    "miner_kwh"
                ),
                "fresh_horizon_budget_kw": (state.get("horizon_projection") or {}).get(
                    "miner_budget_kw"
                ),
            },
        )
        # #endregion
        return _refresh_consumption_estimates_from_state(
            config, state, live, base_dir, now, run_id="post-fix"
        )
    # Nachts / spätabends kein teures 7-Tage-Karussell (blockiert den Tick sonst Minuten)
    if state.get("energy_budget_snapshots") and (now.hour >= 21 or now.hour < 6):
        # #region agent log
        _agent_debug_log(
            "H4",
            "service.py:_maybe_persist_energy_budget_snapshots",
            "throttle skip night window",
            {"hour": now.hour},
        )
        # #endregion
        return _refresh_consumption_estimates_from_state(
            config, state, live, base_dir, now, run_id="post-fix"
        )
    try:
        pv_now = float((live or {}).get("pv_kw") or 0)
        if pv_now < 0.3 and state.get("energy_budget_snapshots"):
            # Ohne PV: Karussell kann bis zur nächsten Stunde warten
            if state.get("last_energy_budget_snapshots_key"):
                # #region agent log
                _agent_debug_log(
                    "H4",
                    "service.py:_maybe_persist_energy_budget_snapshots",
                    "throttle skip low pv",
                    {"pv_kw": pv_now},
                )
                # #endregion
                return _refresh_consumption_estimates_from_state(
                    config, state, live, base_dir, now, run_id="post-fix"
                )
    except Exception:
        pass
    # Heutige Verbrauchsdaten sichern/aktualisieren (extrapoliert), damit Profile greifen
    try:
        from control_algorithm.consumption_history import seed_today_consumption_history

        seed_today_consumption_history(base_dir)
    except Exception:
        pass
    snapshots = build_multi_day_energy_snapshots(config, state, live, base_dir, now)
    state["energy_budget_snapshots"] = snapshots
    if snapshots:
        state["energy_budget_snapshot"] = snapshots[0]
    week_plan = build_weekly_energy_plan(config, state, live, base_dir, now)
    state["energy_week_plan"] = week_plan
    state["consumption_estimates"] = build_consumption_estimate_summary(
        snapshots, base_dir, now=now, week_plan=week_plan, config=config
    )
    state["last_energy_budget_snapshots_key"] = key
    # #region agent log
    d0 = (week_plan.get("days") or [{}])[0]
    _agent_debug_log(
        "H5",
        "service.py:_maybe_persist_energy_budget_snapshots",
        "refreshed consumption estimates",
        {
            "ui_total_today_kwh": (state.get("consumption_estimates") or {})
            .get("total_with_miners", {})
            .get("today_kwh"),
            "week_miner_kwh": (d0.get("daily_totals") or {}).get("miner_kwh"),
            "week_pv_kwh": (d0.get("daily_totals") or {}).get("pv_kwh"),
        },
        run_id="post-fix",
    )
    # #endregion
    return state


def _refresh_absorption_and_device_schedule(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """24h-Absorptionsplan + Geräteplan (Budget je Verbraucher) aktualisieren."""
    now = now or datetime.now()
    week_plan = state.get("energy_week_plan")
    if not week_plan:
        try:
            week_plan = build_weekly_energy_plan(config, state, live, base_dir, now)
            state["energy_week_plan"] = week_plan
        except Exception as e:
            state["absorption_plan_24h"] = {"error": str(e)}
            return state
    try:
        absorb = compute_24h_absorption_plan(
            config, state, live, week_plan, now=now
        )
        state["absorption_plan_24h"] = absorb
    except Exception as e:
        state["absorption_plan_24h"] = {"error": str(e)}
    try:
        schedule = build_device_schedule_24h(config, state, live, now=now)
        persist_device_schedule_24h(state, schedule)
    except Exception as e:
        state["device_schedule_24h_error"] = str(e)
    return state


def run_tick(dry_run_override: Optional[bool] = None) -> Dict[str, Any]:
    global _last_action_times, _last_applied_action

    config = load_config()
    state = load_state()
    state, _ = maybe_reset_on_config_change(state, config)
    state = maybe_revert_inconsistent_last_calibration(state)
    # Pro Tick: teure required_soc-Suchen nicht N× wiederholen
    state["_required_soc_cache"] = {}

    if not config.get("enabled"):
        state["daemon_status"] = "disabled"
        state["planned_actions"] = []
        state["info_messages"] = ["Steuerungsalgorithmus ist deaktiviert."]
        state["last_tick"] = datetime.now().isoformat()
        save_state(state)
        # Daemon braucht kein UI-Status-JSON — Web-UI baut das on-demand.
        return {
            "ok": True,
            "enabled": False,
            "dry_run": bool(config.get("dry_run", True)),
            "last_tick": state.get("last_tick"),
        }

    force_weather = bool(state.pop("force_weather_refresh", False))
    state = refresh_weather(
        config,
        state,
        force=force_weather or not state.get("pv_forecast_cache"),
    )
    live = load_live_snapshot(str(BACKEND_DIR))
    state["live_soc"] = live.get("soc_percent")
    state["live_pv_kw"] = live.get("pv_kw")
    state.pop("end_of_production_target", None)
    state.pop("end_target_hold", None)
    try:
        from control_algorithm.planning_target import refresh_planning_target

        refresh_planning_target(config, state)
    except Exception:
        pass
    try:
        from control_algorithm.curve_plan import (
            clear_stale_evening_hold,
            compute_target_soc_curve,
        )

        if clear_stale_evening_hold(state):
            state["anchor_stretch_hold_cleared_at"] = datetime.now().isoformat()
        state["curve_plan"] = compute_target_soc_curve(config, state, live)
    except Exception as e:
        state["curve_plan"] = {"ok": False, "reason": str(e)}
    try:
        from control_algorithm.steering_learning import record_tick_observation

        record_tick_observation(config, state, live)
    except Exception:
        pass
    try:
        from control_algorithm.peak_balance_learning import record_peak_balance_tick

        record_peak_balance_tick(config, state, live)
    except Exception:
        pass
    try:
        from control_algorithm.storage_band_learning import record_storage_band_tick

        record_storage_band_tick(config, state, live)
    except Exception:
        pass
    try:
        from control_algorithm.steering_learning import temper_aggressive_learning_for_charge

        temper_aggressive_learning_for_charge(config=config, state=state, live=live)
    except Exception:
        pass
    try:
        from control_algorithm.device_power_measure import update_device_power_measurements

        update_device_power_measurements(config, state, live, auto_apply_config=True)
    except Exception as e:
        state["device_power_measure_error"] = str(e)

    base_dir = str(BACKEND_DIR)
    now_tick = datetime.now()
    try:
        from control_algorithm.overnight_reserve import (
            compute_overnight_reserve_plan,
            record_overnight_soc_stress,
        )

        state["overnight_reserve_plan"] = compute_overnight_reserve_plan(
            config, state, live, now=now_tick
        )
        plan_or = state["overnight_reserve_plan"] or {}
        state["overnight_priority_cap_kw"] = plan_or.get("max_night_discharge_kw")
        record_overnight_soc_stress(config, state, live, plan_or)
    except Exception as e:
        state["overnight_reserve_error"] = str(e)

    try:
        from control_algorithm.anchor_window import run_anchor_window

        # run_anchor_window kann einen neuen Dict zurückgeben und Overnight-Keys verwerfen
        aw_out = run_anchor_window(config, state, live, base_dir, now=now_tick)
        if isinstance(aw_out, dict) and aw_out is not state:
            state.update(aw_out)
        elif isinstance(aw_out, dict):
            state = aw_out
    except Exception as e:
        state["anchor_window_error"] = str(e)

    try:
        from control_algorithm.soc_priority_neuron import (
            record_soc_priority_tick,
            run_soc_priority_neuron,
        )

        sp_out = run_soc_priority_neuron(state, config, live, base_dir)
        if isinstance(sp_out, dict) and sp_out is not state:
            state.update(sp_out)
        elif isinstance(sp_out, dict):
            state = sp_out
        record_soc_priority_tick(config, state, live)
    except Exception as e:
        state["soc_priority_neuron_error"] = str(e)

    pr = normalize_performance_ratio(state.get("performance_ratio"))
    try:
        from control_algorithm.pv_pattern_estimate import (
            effective_today_pv_kwh,
            update_state_pattern_estimate,
        )

        # Ist-PV/Stunden vor Muster+Override aktualisieren
        state["live_pv_kwh_actual"] = get_actual_pv_kwh_today(base_dir)
        state["hour_actual_today"] = get_actual_pv_kwh_by_hours_today(base_dir)
        state["slot_actual_today"] = get_actual_pv_kwh_by_slots_today(base_dir)
        update_state_pattern_estimate(state)
        pv_today = float(effective_today_pv_kwh(state))
    except Exception:
        pv_today = float(state.get("pv_yield_today_kwh") or 0)
    try:
        from control_algorithm.consumption_pattern_estimate import (
            update_state_consumption_pattern,
        )

        update_state_consumption_pattern(state, config, base_dir)
    except Exception as e:
        state["consumption_pattern_error"] = str(e)
    pv_tomorrow = float(state.get("pv_yield_tomorrow_kwh") or 0)

    messages: List[str] = []
    actions: List[Dict[str, Any]] = []

    emergency_actions, emergency_msgs = check_min_soc_emergency(config, live, state)
    actions.extend(emergency_actions)
    messages.extend(emergency_msgs)

    from control_algorithm.battery_protection import (
        clear_anomaly_for_max_soc_protection,
        preview_max_soc_protection,
        refresh_min_soc_protection,
        sync_max_soc_protection_state,
    )

    soc_lock = refresh_min_soc_protection(config, live, state)
    max_soc_prot = preview_max_soc_protection(config, live, state)
    if max_soc_prot and not soc_lock:
        sync_max_soc_protection_state(state, config, live)
        clear_anomaly_for_max_soc_protection(state)
    if soc_lock:
        min_soc_cfg = float(config.get("min_soc_percent") or 35.0)
        soc_now = live.get("soc_percent")
        state["min_soc_lock_reason"] = (
            f"SOC {float(soc_now):.0f}% < Min {min_soc_cfg:.0f}%"
            if soc_now is not None
            else f"Min-SoC {min_soc_cfg:.0f}% verletzt"
        )
        # Budget für Dashboard/Cockpit auf 0 – kein Entlade-Signal unter dem Floor.
        proj = dict(state.get("balance_projection") or {})
        proj["miner_budget_kw"] = 0.0
        proj["miner_budget_kw_raw"] = 0.0
        proj["binding_constraint"] = "min_soc_emergency"
        if soc_now is not None:
            proj["projected_soc"] = float(soc_now)
            proj["target_soc"] = min_soc_cfg
        proj["near_term_plan"] = {
            "near_term_budget_kw": 0.0,
            "sustained_budget_kw": 0.0,
            "night_budget_kw": 0.0,
            "plan_ok": False,
            "blocked_by": "min_soc_emergency",
        }
        state["balance_projection"] = proj
        hp = dict(state.get("horizon_projection") or proj)
        hp["miner_budget_kw"] = 0.0
        hp["miner_budget_kw_raw"] = 0.0
        hp["binding_constraint"] = "min_soc_emergency"
        if soc_now is not None:
            hp["projected_soc"] = float(soc_now)
            hp["target_soc"] = min_soc_cfg
        hp["near_term_plan"] = dict(proj.get("near_term_plan") or {})
        state["horizon_projection"] = hp
    else:
        state["min_soc_lock_reason"] = None

    # Überschuss-Session: Notbremsen beenden sie, sonst fortschreiben.
    # Muss vor plan_load_actions laufen, damit dort schon die Rest-Menge steht.
    try:
        from control_algorithm.surplus_session import close_session, update_session

        if soc_lock:
            close_session(state, "min_soc_lock")
        elif state.get("night_lock_active"):
            close_session(state, "nacht_sperre")
    except Exception as e:
        messages.append(f"Überschuss-Session Fehler: {e}")

    if not soc_lock:
        state = maybe_run_hourly_balance(config, state, live, base_dir)
        state = _refresh_absorption_and_device_schedule(
            config, state, live, base_dir, now=datetime.now()
        )

        try:
            from control_algorithm.morning_drain import (
                compute_morning_drain_plan,
                record_morning_drain_tick,
            )

            state["morning_drain_plan"] = compute_morning_drain_plan(
                config, state, live, base_dir, now=now_tick
            )
            record_morning_drain_tick(config, state, live)
        except Exception as e:
            state["morning_drain_error"] = str(e)

        try:
            from control_algorithm.surplus_session import (
                close_session as _close_session,
                update_session as _update_session,
            )

            if state.get("night_lock_active"):
                _close_session(state, "nacht_sperre")
            else:
                _update_session(state, config, live)
        except Exception as e:
            messages.append(f"Überschuss-Session Fehler: {e}")

        try:
            from control_algorithm.load_scheduler import protection_shutdown_is_emergency
            from control_algorithm.pv_safety_watchdog import check_pv_safety_watchdog

            state, safety_actions, safety_msgs = check_pv_safety_watchdog(
                config, state, live
            )
            messages.extend(safety_msgs)
            if safety_actions and not protection_shutdown_is_emergency(
                config, live, state, "pv"
            ):
                messages.append(
                    "PV-Sicherheit: Abschaltung verworfen – SoC zwischen Min und Max, "
                    "Abweichung nicht kritisch."
                )
                safety_actions = []
            actions.extend(safety_actions)
        except Exception as e:
            messages.append(f"PV-Sicherheitsmodul Fehler: {e}")

        try:
            from control_algorithm.load_scheduler import protection_shutdown_is_emergency

            state, anomaly_actions, anomaly_msgs = check_anomaly(
                config, state, live, base_dir
            )
            messages.extend(anomaly_msgs)
            kept_anomaly = []
            dropped_pause = False
            emergency = protection_shutdown_is_emergency(
                config, live, state, "anomaly"
            )
            for act in anomaly_actions:
                if (
                    not emergency
                    and str(act.get("action") or "") == "pause"
                    and str(act.get("source") or "") == "anomaly"
                ):
                    dropped_pause = True
                    continue
                kept_anomaly.append(act)
            if dropped_pause:
                messages.append(
                    "Anomalie: Abschaltung verworfen – SoC zwischen Min und Max, "
                    "Abweichung nicht kritisch."
                )
            actions.extend(kept_anomaly)
        except Exception as e:
            messages.append(f"Anomalie-Prüfung Fehler: {e}")

    try:
        from control_algorithm.consumption_pattern_estimate import (
            consumption_pattern_info_messages,
        )

        for line in consumption_pattern_info_messages(state):
            messages.append(line)
    except Exception:
        pass

    # UI-Pfad darf das nicht rechnen (7-Tage-Binärsuche, mehrere Sekunden).
    try:
        from control_algorithm.weather_survival import build_autopilot_soc_plan

        state["autopilot_soc_plan"] = build_autopilot_soc_plan(
            config,
            state,
            live=live,
            base_dir=base_dir,
        )
    except Exception as e:
        state["autopilot_soc_plan"] = {"error": str(e), "recommendations": []}

    load_actions, load_msgs = plan_load_actions(
        config, live, state, base_dir, pv_today, pv_tomorrow, pr
    )
    actions.extend(load_actions)
    messages.extend(load_msgs)

    try:
        from control_algorithm.overnight_reserve import should_defer_overnight_resume

        plan_or = state.get("overnight_reserve_plan") or {}
        prio_plan = state.get("priority_runtime_plan") or {}
        cfg_by_ip = {
            str(d.get("ip") or ""): d
            for d in (config.get("devices") or [])
            if d.get("ip")
        }
        kept: List[Dict[str, Any]] = []
        for act in actions:
            if act.get("action") == "resume" and not act.get("force"):
                ip = str(act.get("ip") or "")
                device = cfg_by_ip.get(ip) or {"ip": ip}
                defer, why = should_defer_overnight_resume(plan_or, device, prio_plan)
                if defer:
                    if why:
                        messages.append(why)
                    continue
            kept.append(act)
        actions = kept
    except Exception as exc:
        messages.append(f"Nachtreserve-Guard: {exc}")

    try:
        from control_algorithm.anchor_window import apply_anchor_window_guards

        actions, aw_msgs = apply_anchor_window_guards(
            actions, state, config, live, now=datetime.now()
        )
        messages.extend(aw_msgs)
    except Exception as exc:
        messages.append(f"Anker-Fenster-Guard: {exc}")

    actions = apply_device_runtime_guards(actions, state, config, live=live)
    try:
        from control_algorithm.switch_commitment import apply_switch_commitment_guards

        actions, commit_msgs = apply_switch_commitment_guards(
            actions, state, config, live=live
        )
        messages.extend(commit_msgs)
    except Exception as exc:
        messages.append(f"Anti-Flatter-Commitment: {exc}")

    try:
        from control_algorithm.hard_soc_guard import seal_hard_soc_actions

        actions, seal_notes = seal_hard_soc_actions(actions, config, live)
        messages.extend(seal_notes)
    except Exception as exc:
        messages.append(f"Harte SoC-Grenze: {exc}")

    try:
        from control_algorithm.manual_override import (
            apply_manual_override_guards,
            detect_and_update_manual_holds,
        )

        miner_status_for_hold: Dict[str, Any] = dict(live.get("miner_status") or {})
        state, hold_detect_msgs = detect_and_update_manual_holds(
            state,
            miner_status_for_hold,
            config=config,
            now=datetime.now(),
            last_action_times=_last_action_times,
        )
        messages.extend(hold_detect_msgs)
        actions, hold_guard_msgs = apply_manual_override_guards(
            actions, state, config=config, now=datetime.now()
        )
        messages.extend(hold_guard_msgs)
    except Exception as exc:
        messages.append(f"Fremdsteuerung-Hold: {exc}")

    # Gleiche Aktion nicht wiederholt senden; Force-Aktionen haben Vorrang.
    # Dedup am Live-Status (nicht an veraltetem last_action) – sonst werden
    # Einschaltungen übersprungen und nie dokumentiert.
    miner_status_live: Dict[str, Any] = dict(live.get("miner_status") or {})
    deduped: Dict[str, Dict[str, Any]] = {}
    for act in actions:
        ip = act["ip"]
        prev = deduped.get(ip)
        if prev is None or act.get("force"):
            deduped[ip] = act
    actions = list(deduped.values())
    filtered_actions: List[Dict[str, Any]] = []
    skipped_live_match: List[Dict[str, Any]] = []
    for act in actions:
        ip = act["ip"]
        action = act["action"]
        last_mem = _last_applied_action.get(ip)
        status = str(miner_status_live.get(ip) or "").lower()
        already_off = status in ("paused", "stopped", "off", "idle")
        already_on = status == "running"
        # Live-Status schlägt Speicher: wenn Pause „gemerkt“ aber Miner läuft → erneut senden.
        if action == "pause" and already_off:
            _last_applied_action[ip] = "pause"
            skipped_live_match.append(act)
            continue
        if action == "resume" and already_on:
            _last_applied_action[ip] = "resume"
            skipped_live_match.append(act)
            continue
        if action == "pause" and last_mem == "pause" and not already_on:
            skipped_live_match.append(act)
            continue
        if action == "resume" and last_mem == "resume" and not already_off:
            skipped_live_match.append(act)
            continue
        filtered_actions.append(act)
    actions = filtered_actions

    forced_actions = [a for a in actions if a.get("force")]
    regular_actions = [a for a in actions if not a.get("force")]
    switch_times = {**switch_times_from_state(state), **_last_action_times}
    try:
        from control_algorithm.load_scheduler import normalize_min_runtime_hours

        # Kurze Anti-Flatter-Hysterese (nicht die alte 1h-Abschaltsperre)
        runtime_h = normalize_min_runtime_hours(config.get("min_device_runtime_hours"))
        min_gap_s = int(max(300.0, min(runtime_h * 3600.0 * 0.25, 900.0)))
    except Exception:
        min_gap_s = 300
    regular_actions = apply_hysteresis(
        regular_actions,
        _last_applied_action,
        min_interval_seconds=min_gap_s,
        last_times=switch_times,
        miner_status=miner_status_live,
    )
    actions = forced_actions + regular_actions

    _append_pv_learning_messages(config, state, messages, base_dir)
    try:
        from control_algorithm.steering_learning import learning_info_messages

        for lm in learning_info_messages(state):
            messages.append(lm)
    except Exception:
        pass
    if state.get("weather_refresh_note"):
        messages.insert(0, str(state["weather_refresh_note"]))
    if state.get("night_lock_active"):
        messages.append(f"Nacht-Sperre: {state.get('night_lock_reason', 'aktiv')}")
    if state.get("near_term_note"):
        messages.insert(0, str(state["near_term_note"]))
    if state.get("anomaly_active"):
        messages.append(
            f"Anomalie aktiv: +{state.get('anomaly_excess_pct', '?')} % über historischem Schnitt."
        )
    if state.get("anomaly_low_active"):
        messages.append(
            f"Unterverbrauch-Anomalie aktiv: −{state.get('anomaly_deficit_pct', '?')} % "
            f"unter historischem Schnitt – mehr Last freigeben."
        )
    try:
        from control_algorithm.pv_safety_watchdog import pv_safety_info_messages

        for sm in pv_safety_info_messages(state):
            messages.append(sm)
    except Exception:
        pass
    if state.get("pv_safety_active"):
        messages.append(
            f"PV-Sicherheitsabschaltung aktiv: Pace {state.get('pv_safety_deviation_pct')} % "
            f"(Recheck {state.get('pv_safety_next_recheck_at')})."
        )
    proj = state.get("balance_projection") or {}
    if proj.get("target_soc") is not None:
        messages.append(
            f"Energiebilanz: Prognose {proj.get('projected_soc')}% "
            f"um {proj.get('target_time')} (Ziel {proj.get('target_soc')}%)."
        )
    messages.append(
        f"Verbrauchshistorie: {profile_sample_count(base_dir)} Tage archiviert."
    )
    cp = state.get("curve_plan") or {}
    if cp.get("ok"):
        messages.append(f"Soll-Kurve (blau): {cp.get('formula')}")
        ang = (cp.get("consumption_angle") or {}).get("note")
        if ang:
            messages.append(f"Verbrauchswinkel (rot): {ang}")
        if cp.get("steer") and cp.get("steer") != "kurve_ok":
            messages.append(f"Kurven-Steering: {cp.get('steer')} (Fehler {cp.get('slope_error_kw')} kW).")

    state["planned_actions"] = actions
    state["info_messages"] = messages
    state["eta_message"] = eta_message(state)
    state["dry_run"] = config.get("dry_run", True) if dry_run_override is None else dry_run_override
    dry_run = bool(state.get("dry_run", True))

    try:
        from control_algorithm.decision_board import build_decision_board

        board = build_decision_board(config, state, live)
        state["decision_board"] = board
        state["device_schedule_snapshot"] = board.get("devices") or []
        if not dry_run:
            try:
                from control_algorithm.switch_commitment import sync_plan_times_into_runtime

                state = sync_plan_times_into_runtime(
                    state, list(board.get("devices") or [])
                )
            except Exception:
                pass
    except Exception as e:
        state["decision_board"] = {"error": str(e), "devices": [], "layers": [], "signals": []}
    miners = load_miners()
    creds_by_ip = {m["ip"]: m for m in miners}
    ha_entity_ids = {
        (h.get("entity_id") or "").strip()
        for h in load_ha_devices()
        if h.get("entity_id")
    }
    try:
        from control_algorithm.config import load_gpio_pins, is_gpio_device
        from gpio_pins import is_gpio_device_key

        gpio_ids = {
            str(g.get("id") or "").strip()
            for g in load_gpio_pins()
            if g.get("id")
        }
    except Exception:
        gpio_ids = set()

        def is_gpio_device(dev):  # type: ignore
            return False

        def is_gpio_device_key(key):  # type: ignore
            return False
    config_by_key = {
        str(d.get("ip") or d.get("entity_id") or ""): d
        for d in (config.get("devices") or [])
        if d.get("ip") or d.get("entity_id")
    }
    applied: List[Dict[str, Any]] = []

    # Anker (höchste Prio) zuerst schalten; Flex erst nach Live-Check dass Anker läuft
    from control_algorithm.planner import enabled_devices, sort_for_resume, priority_rank

    ordered_devs = sort_for_resume(enabled_devices(config))
    anchor_ip = str((ordered_devs[0].get("ip") if ordered_devs else "") or "")
    prio_by_ip = {
        str(d.get("ip")): priority_rank(d) for d in ordered_devs if d.get("ip")
    }

    def _fresh_status(ip: str) -> str:
        """Live nachschauen – nicht nur Plan/Cache."""
        cfg_dev = config_by_key.get(ip) or {"ip": ip}
        if ip in gpio_ids or is_gpio_device(cfg_dev) or is_gpio_device_key(ip):
            try:
                from device_registry import get_gpio_status_map

                return str(get_gpio_status_map().get(ip) or "paused").lower()
            except Exception:
                return "paused"
        if ip in ha_entity_ids or is_ha_device(cfg_dev):
            try:
                from device_registry import get_ha_device_status_map

                return str(get_ha_device_status_map().get(ip) or "paused").lower()
            except Exception:
                return "paused"
        m = creds_by_ip.get(ip) or {}
        try:
            from miner_control import read_miner_status
            from dashboard_miner_status import update_miner_status_entry

            # Kein Recovery bei Circuit/tot – sonst blockiert jeder Tick den Anker.
            try:
                from miner_control import _should_skip_unreachable

                if _should_skip_unreachable(ip):
                    raise RuntimeError("miner unreachable")
            except RuntimeError:
                pass
            else:
                st = read_miner_status(
                    ip, m.get("username", "root"), m.get("password", "")
                )
                if st:
                    try:
                        update_miner_status_entry(ip, st)
                    except Exception:
                        pass
                    return str(st).lower()
                if m.get("mac") or m:
                    try:
                        from ip_recovery import try_recover_miner

                        recovered = try_recover_miner(m)
                        if recovered.success and recovered.new_ip:
                            ip = recovered.new_ip
                            m = dict(m)
                            m["ip"] = ip
                            creds_by_ip[ip] = m
                            st = read_miner_status(
                                ip, m.get("username", "root"), m.get("password", "")
                            )
                            if st:
                                try:
                                    update_miner_status_entry(ip, st)
                                except Exception:
                                    pass
                                return str(st).lower()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            from dashboard_miner_status import load_miner_status_map

            return str(load_miner_status_map().get(ip) or "paused").lower()
        except Exception:
            return "paused"

    def _anchor_confirmed_running() -> bool:
        if not anchor_ip:
            return True
        st = _fresh_status(anchor_ip)
        return st == "running"

    # Reihenfolge: Pause niedrige Prio zuerst, dann Resume hohe Prio zuerst
    def _apply_sort_key(act: Dict[str, Any]) -> tuple:
        ip = str(act.get("ip") or "")
        pr = float(prio_by_ip.get(ip, 99))
        if act.get("action") == "pause":
            return (0, -pr, ip)  # niedrigste Prio (höherer rank) zuerst aus
        return (1, pr, ip)  # höchste Prio (niedrigerer rank) zuerst ein

    actions = sorted(actions, key=_apply_sort_key)
    flex_blocked_no_anchor = False

    for act in actions:
        ip = act["ip"]
        action = act["action"]

        # Flex/Spill-Resume nur wenn Anker wirklich läuft (frisch geprüft)
        if (
            action == "resume"
            and anchor_ip
            and ip != anchor_ip
            and float(prio_by_ip.get(ip, 99)) > float(prio_by_ip.get(anchor_ip, 0))
        ):
            if flex_blocked_no_anchor or not _anchor_confirmed_running():
                # Kurz warten und nochmal prüfen (Hydro braucht oft 1–2 s)
                if not flex_blocked_no_anchor:
                    time.sleep(1.2)
                if flex_blocked_no_anchor or not _anchor_confirmed_running():
                    flex_blocked_no_anchor = True
                    act["executed"] = False
                    act["skipped_anchor_not_running"] = True
                    messages.append(
                        f"Einschalten {ip} übersprungen – Hydro/Anker ({anchor_ip}) "
                        f"läuft live noch nicht (Zwischenprüfung)."
                    )
                    continue

        m = creds_by_ip.get(ip, {})
        ok = True
        if not dry_run:
            cfg_dev = config_by_key.get(ip) or {"ip": ip}
            if ip in gpio_ids or is_gpio_device(cfg_dev) or is_gpio_device_key(ip):
                try:
                    from device_registry import execute_device_action

                    ok = bool(
                        execute_device_action(
                            {"type": "gpio", "id": ip, "ip": ip},
                            action,
                        )
                    )
                except Exception as e:
                    ok = False
                    messages.append(f"GPIO-Schaltung {ip} ({action}) fehlgeschlagen: {e}")
                if not ok:
                    messages.append(f"GPIO-Schaltung {ip} ({action}) ohne Erfolg.")
            elif ip in ha_entity_ids or is_ha_device(cfg_dev):
                try:
                    from device_registry import execute_device_action

                    ok = bool(
                        execute_device_action(
                            {"type": "ha", "entity_id": ip, "id": ip},
                            action,
                        )
                    )
                except Exception as e:
                    ok = False
                    messages.append(f"HA-Schaltung {ip} ({action}) fehlgeschlagen: {e}")
                if not ok:
                    messages.append(f"HA-Schaltung {ip} ({action}) ohne Erfolg.")
            else:
                # Bis zu 3 Versuche – aber nicht bei totem Miner (Circuit/unreachable).
                ok = False
                last_err = ""
                skip_retries = False
                try:
                    from miner_control import _should_skip_unreachable

                    if _should_skip_unreachable(ip):
                        skip_retries = True
                except Exception:
                    try:
                        from miner_control import _host_reachable, _ssh_circuit_open

                        if _ssh_circuit_open(ip) or not _host_reachable(ip, timeout=1.0):
                            skip_retries = True
                    except Exception:
                        pass
                attempts = 1 if skip_retries else 3
                for attempt in range(attempts):
                    try:
                        ok = _miner_command_ok(
                            execute_miner_command(
                                ip,
                                action,
                                m.get("username", "root"),
                                m.get("password", ""),
                            )
                        )
                    except Exception as e:
                        ok = False
                        last_err = str(e)
                    if ok:
                        break
                    if skip_retries:
                        break
                    time.sleep(0.8 * (attempt + 1))
                if not ok:
                    tip = f" ({last_err})" if last_err else ""
                    msg = f"KRITISCH: Miner-Schaltung {ip} ({action}) ohne Erfolg{tip}."
                    messages.append(msg)
                    if act.get("force"):
                        messages.append(
                            f"Force-{action} für {ip} wird nächsten Tick erneut versucht."
                        )
            act["executed"] = ok
            if not ok:
                # Nicht als angewendet merken – sonst bleibt Gerät fälschlich „erledigt“.
                _last_applied_action.pop(ip, None)
                # Anker-Resume gescheitert → keine Flex danach
                if action == "resume" and ip == anchor_ip:
                    flex_blocked_no_anchor = True
                    messages.append(
                        f"Anker {anchor_ip} nicht eingeschaltet – Flex wartet."
                    )
                continue
            # Nach Pause: Live-Status prüfen – Schein-OK (nc ohne Wirkung) abfangen
            if action == "pause" and not (
                ip in ha_entity_ids
                or is_ha_device(cfg_dev)
                or ip in gpio_ids
                or is_gpio_device(cfg_dev)
                or is_gpio_device_key(ip)
            ):
                time.sleep(0.8)
                st_now = _fresh_status(ip)
                if st_now == "running":
                    ok = False
                    act["executed"] = False
                    _last_applied_action.pop(ip, None)
                    messages.append(
                        f"KRITISCH: Pause {ip} gemeldet OK, Live-Status aber noch "
                        f"„running“ – erneuter Versuch nächsten Tick."
                    )
                    continue
            # Nach erfolgreichem Anker-Resume: Status nochmal bestätigen
            if action == "resume" and ip == anchor_ip:
                time.sleep(1.0)
                if not _anchor_confirmed_running():
                    flex_blocked_no_anchor = True
                    messages.append(
                        f"Anker {anchor_ip}: Befehl OK, aber Live-Status noch nicht "
                        f"„running“ – Flex diesen Tick gesperrt."
                    )
                else:
                    messages.append(
                        f"Anker {anchor_ip} live bestätigt (running) – Flex darf folgen."
                    )
        else:
            act["executed"] = False
            act["dry_run"] = True
        if not dry_run:
            _last_applied_action[ip] = action
            _last_action_times[ip] = time.time()
        applied.append(act)
        if action == "resume" and act.get("executed"):
            try:
                from control_algorithm.steering_learning import record_night_device_resume

                record_night_device_resume(state, ip=ip)
            except Exception:
                pass

    miner_names = {
        m.get("ip"): m.get("note") or m.get("model") or m.get("ip")
        for m in miners
        if m.get("ip")
    }
    # Protokoll: ausgeführte Schaltungen + Lücken schließen, wenn Live schon aus
    # ist aber kein offener Aus-Eintrag existiert (z. B. nach früherem Return-None-Bug).
    # Kein Schein-„Einschalten“ durch Live↔History-Abgleich.
    to_record: List[Dict[str, Any]] = list(applied)
    try:
        from control_algorithm.steering_history import close_open_pauses_silently

        running_ips = {
            ip
            for ip, st in miner_status_live.items()
            if str(st or "").lower() == "running"
        }
        close_open_pauses_silently(base_dir, running_ips)

        open_paused = open_pause_ips(base_dir)
        applied_ips = {str(a.get("ip")) for a in to_record if a.get("ip")}
        for act in skipped_live_match:
            ip = str(act.get("ip") or "")
            if not ip or ip in applied_ips:
                continue
            if act.get("action") != "pause":
                continue
            status = str(miner_status_live.get(ip) or "").lower()
            if status not in ("paused", "stopped", "off", "idle"):
                continue
            if ip in open_paused:
                continue
            reason = str(act.get("reason") or "Steuerungsalgorithmus")
            if "[Live bereits aus" not in reason:
                reason = f"{reason} [Live bereits aus – Protokoll nachgezogen]"
            to_record.append(
                {
                    "ip": ip,
                    "action": "pause",
                    "reason": reason,
                    "force": bool(act.get("force")),
                    "dry_run": False,
                    "executed": True,
                    "protocol_backfill": True,
                }
            )
            applied_ips.add(ip)
            open_paused.add(ip)
    except Exception as exc:
        messages.append(f"Steuerungs-Protokoll-Sync: {exc}")
    try:
        record_steering_actions(base_dir, to_record, miner_names)
    except Exception as exc:
        messages.append(f"Steuerungs-Protokoll-Fehler: {exc}")

    try:
        from control_algorithm.load_scheduler import update_device_runtime

        state = update_device_runtime(state, applied)
    except Exception:
        state = apply_device_state_updates(
            state,
            [a for a in applied if a.get("executed")],
            config=config,
        )
    state["info_messages"] = messages
    state["planned_actions"] = actions

    state = maybe_run_calibration(config, state)
    state["last_tick"] = datetime.now().isoformat()
    # 7-Tage-Snapshots nur stündlich (UI); Steuerung nutzt Live + Horizon-Prognose.
    state = _maybe_persist_energy_budget_snapshots(config, state, live, base_dir)
    # Kurve NACH Wochenplan neu rechnen — sonst bleibt Bat-Plan über Nacht flach
    # auf dem veralteten SoC (Tick berechnet curve_plan ganz am Anfang).
    try:
        from control_algorithm.curve_plan import compute_target_soc_curve

        state["curve_plan"] = compute_target_soc_curve(config, state, live)
    except Exception as e:
        state["curve_plan"] = state.get("curve_plan") or {"ok": False, "reason": str(e)}
    state.pop("_required_soc_cache", None)
    # History/Messages härter begrenzen → kleinere State-Writes, weniger CPU.
    yh = state.get("yield_history")
    if isinstance(yh, list) and len(yh) > 14:
        state["yield_history"] = yh[-14:]
    msgs = state.get("info_messages")
    if isinstance(msgs, list) and len(msgs) > 40:
        state["info_messages"] = msgs[-40:]
    save_state(state)
    # Kein build_status_payload — Daemon verwirft den Return; UI liest State on-demand.
    return {
        "ok": True,
        "enabled": True,
        "dry_run": bool(state.get("dry_run", True)),
        "last_tick": state.get("last_tick"),
        "actions_applied": len(applied),
        "actions_planned": len(actions),
    }


def _steering_learning_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from control_algorithm.steering_learning import learning_status_payload

        return learning_status_payload(state)
    except Exception as e:
        return {"error": str(e)}


def _device_power_measure_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from control_algorithm.device_power_measure import measured_power_payload

        return measured_power_payload(state)
    except Exception as e:
        return {"error": str(e), "by_ip": {}}


def _enrich_devices_measured_power(
    devices: List[Dict[str, Any]], state: Dict[str, Any]
) -> None:
    """Ist-kW aus Lern-State an Status-Geräte hängen."""
    try:
        from control_algorithm.device_power_measure import measured_power_payload

        by_ip = (measured_power_payload(state).get("by_ip") or {})
    except Exception:
        by_ip = {}
    for d in devices:
        ip = str(d.get("ip") or "")
        entry = by_ip.get(ip) or {}
        mk = entry.get("measured_kw")
        if mk is None:
            mk = d.get("measured_power_kw")
        if mk is not None:
            try:
                d["measured_power_kw"] = round(float(mk), 2)
            except (TypeError, ValueError):
                pass
        if entry.get("updated_at"):
            d["measured_power_updated_at"] = entry.get("updated_at")
        if entry.get("source"):
            d["measured_power_source"] = entry.get("source")
        if "live" in entry:
            d["measured_power_live"] = bool(entry.get("live"))


_autopilot_warm_lock = threading.Lock()
_autopilot_warm_started = False


def _warm_autopilot_soc_plan_async(
    config: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> None:
    """Füllt den SoC-Plan ohne den UI-Request zu blockieren."""
    global _autopilot_warm_started
    with _autopilot_warm_lock:
        if _autopilot_warm_started:
            return
        _autopilot_warm_started = True

    def _work() -> None:
        global _autopilot_warm_started
        try:
            st = load_state()
            from control_algorithm.weather_survival import build_autopilot_soc_plan

            plan = build_autopilot_soc_plan(
                config,
                st,
                live=live,
                base_dir=str(BACKEND_DIR),
            )
            st = load_state()
            st["autopilot_soc_plan"] = plan
            save_state(st)
        except Exception:
            pass
        finally:
            with _autopilot_warm_lock:
                _autopilot_warm_started = False

    threading.Thread(target=_work, name="autopilot-soc-warm", daemon=True).start()


def _autopilot_soc_plan_payload(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Immer aus dem Daemon-Tick-Cache. Neu rechnen blockiert die Seite ~8 s.
    cached = state.get("autopilot_soc_plan")
    if isinstance(cached, dict) and (
        cached.get("recommendations") is not None
        or cached.get("error")
        or cached.get("today_recommendation")
    ):
        return cached
    _warm_autopilot_soc_plan_async(config, live)
    return {
        "pending": True,
        "recommendations": [],
        "summary": "SoC-Plan wird im Hintergrund berechnet…",
    }


def _decision_board_payload(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Signale/Score aus Tick-Cache; Geräteliste + Logikgatter immer frisch (Checkbox/Entfernen).
    cached = state.get("decision_board")
    if isinstance(cached, dict) and "devices" in cached and not cached.get("error"):
        board = dict(cached)
    else:
        try:
            from control_algorithm.decision_board import build_decision_board

            board = build_decision_board(config, state, live)
        except Exception as e:
            return {"error": str(e), "devices": [], "layers": [], "signals": [], "score": {}}
    try:
        from datetime import datetime

        from control_algorithm.decision_board import _build_devices

        live = live or {}
        score = board.get("score") or {}
        board["devices"] = _build_devices(
            config,
            state,
            live,
            datetime.now(),
            score.get("soc_gap_pct"),
        )
    except Exception:
        pass
    try:
        from control_algorithm.logic_gate import build_logic_gate

        board["logic_gate"] = build_logic_gate(
            config,
            state,
            live,
            devices=board.get("devices") or [],
            signals=board.get("signals") or [],
            score=board.get("score") or {},
        )
    except Exception as e:
        board["logic_gate"] = {
            "error": str(e),
            "title": "Logikgattersteuerungsalgorithmus Autopilot",
        }
    return board


def _evening_aim_payload(config: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """Berechnetes SoC-Ziel und das Produktionsende, auf das die Kurve zielt.

    Angezeigt wird immer der berechnete Wert. Was heute wegen zu wenig PV
    tatsächlich erreicht wird, ersetzt ihn nicht.
    """
    from datetime import datetime

    from control_algorithm.planning_target import current_target_percent
    from control_algorithm.weather_survival import last_pv_hour_for_date

    pct = current_target_percent(config, state)
    end_h = None
    try:
        end_h = int(last_pv_hour_for_date(state, datetime.now().date()))
    except Exception:
        end_h = None
    if end_h is not None and not (12 <= end_h <= 22):
        end_h = None
    return {
        "soc_percent": round(float(pct), 1) if pct is not None else None,
        "at_time": f"{end_h:02d}:00" if end_h is not None else None,
    }


def _status_release_percent(config: Dict[str, Any]) -> float:
    """Unterkante des Halt-Bands. Fehlt der Wert, gilt 90 %."""
    from control_algorithm.battery_protection import max_soc_release_percent

    return round(float(max_soc_release_percent(config)), 1)


def build_status_payload(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Optional[Dict[str, Any]] = None,
    *,
    persisted_only: bool = False,
) -> Dict[str, Any]:
    from control_algorithm.energy_balance import night_lock_enabled

    if live is None:
        live = load_live_snapshot(str(BACKEND_DIR))
    miners = load_miners()
    devices = []
    for m in miners:
        ip = m.get("ip")
        if not ip:
            continue
        dev = next((d for d in (config.get("devices") or []) if d.get("ip") == ip), {})
        devices.append(
            {
                "ip": ip,
                "note": m.get("note") or m.get("model"),
                "type": "miner",
                "enabled": bool(dev.get("enabled")),
                "power_kw": dev.get("power_kw"),
                "measured_power_kw": dev.get("measured_power_kw"),
                "priority": dev.get("priority"),
                "auto_conflict": bool(m.get("auto") or m.get("auto_battery")),
                "status": (live.get("miner_status") or {}).get(ip, "unknown"),
            }
        )
    for h in load_ha_devices():
        eid = (h.get("entity_id") or "").strip()
        if not eid:
            continue
        dev = next(
            (
                d
                for d in (config.get("devices") or [])
                if d.get("ip") == eid or d.get("entity_id") == eid
            ),
            {},
        )
        devices.append(
            {
                "ip": eid,
                "entity_id": eid,
                "note": h.get("note") or h.get("label") or eid,
                "type": "ha",
                "enabled": bool(dev.get("enabled")),
                "power_kw": dev.get("power_kw"),
                "measured_power_kw": dev.get("measured_power_kw"),
                "priority": dev.get("priority"),
                "auto_conflict": bool(h.get("auto") or h.get("auto_battery")),
                "status": (live.get("miner_status") or {}).get(eid, "unknown"),
            }
        )
    from control_algorithm.config import sort_devices_by_priority

    devices = sort_devices_by_priority(devices)
    _enrich_devices_measured_power(devices, state)
    enabled_devs = [d for d in (config.get("devices") or []) if d.get("enabled")]
    total_kw = sum(float(d.get("power_kw") or 0) for d in enabled_devs)
    pr = normalize_performance_ratio(state.get("performance_ratio"))
    pv_kwp = float(config.get("pv_kwp") or 0)
    sun_h = normalize_sun_hours(config.get("perfect_weather_sun_hours"))
    ref_kwh = effective_perfect_day_kwh(state, config)
    anchor_on = is_reference_anchor_active(state)
    detail = state.get("pv_forecast_detail") or {}
    pv_tomorrow = float(state.get("pv_yield_tomorrow_kwh") or 0)
    if persisted_only:
        device_plans = state.get("device_control_plans")
        if device_plans is None:
            device_plans = build_device_control_plans(
                config, state, live, str(BACKEND_DIR), pv_tomorrow
            )
        # Verbrauch immer live berechnen (Zeitreihe) – sonst bleiben Ist-Werte
        # bei fehlgeschlagenem Loader in State bei 0 hängen.
        consumption_forecast = build_consumption_forecast_display(str(BACKEND_DIR))
        energy_budget_snapshots = state.get("energy_budget_snapshots") or []
        energy_budget = state.get("energy_budget_snapshot")
        if energy_budget is None and energy_budget_snapshots:
            energy_budget = energy_budget_snapshots[0]
        energy_week_plan = state.get("energy_week_plan")
        if energy_week_plan is None:
            energy_week_plan = build_weekly_energy_plan(
                config, state, live, str(BACKEND_DIR)
            )
        consumption_estimates = build_consumption_estimate_summary(
            energy_budget_snapshots,
            str(BACKEND_DIR),
            week_plan=energy_week_plan,
            config=config,
        )
    else:
        device_plans = build_device_control_plans(
            config, state, live, str(BACKEND_DIR), pv_tomorrow
        )
        consumption_forecast = build_consumption_forecast_display(str(BACKEND_DIR))
        energy_budget_snapshots = build_multi_day_energy_snapshots(
            config, state, live, str(BACKEND_DIR)
        )
        energy_budget = (
            energy_budget_snapshots[0]
            if energy_budget_snapshots
            else build_energy_budget_snapshot(config, state, live, str(BACKEND_DIR))
        )
        energy_week_plan = build_weekly_energy_plan(
            config, state, live, str(BACKEND_DIR)
        )
        consumption_estimates = build_consumption_estimate_summary(
            energy_budget_snapshots,
            str(BACKEND_DIR),
            week_plan=energy_week_plan,
            config=config,
        )
    try:
        from control_algorithm.daemon_control import is_daemon_running

        daemon_running = is_daemon_running()
    except Exception:
        daemon_running = None
    pattern_est = state.get("pv_pattern_estimate")
    if not isinstance(pattern_est, dict) or pattern_est.get("estimated_kwh") is None:
        pattern_est = estimate_today_pv_from_patterns(
            forecast_total=state.get("morning_pv_forecast_kwh")
            or state.get("pv_yield_today_kwh"),
            forecast_hours=state.get("morning_forecast_hours")
            or state.get("pv_forecast_today_hours"),
            actual_hours=state.get("hour_actual_today"),
            actual_total=state.get("live_pv_kwh_actual"),
            yield_history=state.get("yield_history"),
        )
    try:
        from control_algorithm.pv_pattern_estimate import (
            effective_today_pv_kwh,
            get_pace_params,
        )

        effective_today = effective_today_pv_kwh(state)
        pace_params = get_pace_params(state)
    except Exception:
        effective_today = state.get("pv_yield_today_kwh")
        pace_params = None
    return {
        "enabled": config.get("enabled"),
        "dry_run": config.get("dry_run", True),
        "control_daemon_running": daemon_running,
        "total_miner_power_kw": round(total_kw, 2),
        "city": config.get("city"),
        "soc_percent": live.get("soc_percent") if live.get("soc_percent") is not None else state.get("live_soc"),
        "live_soc": live.get("soc_percent") if live.get("soc_percent") is not None else state.get("live_soc"),
        "pv_kw": live.get("pv_kw"),
        "live_pv_kwh_actual": state.get("live_pv_kwh_actual"),
        "pv_yield_today_kwh": state.get("pv_yield_today_kwh"),
        "pv_effective_today_kwh": effective_today,
        "pv_yield_tomorrow_kwh": state.get("pv_yield_tomorrow_kwh"),
        "pv_yield_day_after_kwh": state.get("pv_yield_day_after_kwh"),
        "pv_pattern_estimate": pattern_est,
        "pv_estimate_trace": pv_estimate_trace_payload(state, pattern_est),
        "pv_forecast_override": state.get("pv_forecast_override"),
        "forecast_pace_learning": {
            "params": pace_params,
            "last_note": (state.get("forecast_pace_learning") or {}).get("last_note"),
            "last_adjustments": (state.get("forecast_pace_learning") or {}).get(
                "last_adjustments"
            )
            or [],
            "history_days": len(
                (state.get("forecast_pace_learning") or {}).get("history") or []
            ),
        },
        "pv_forecast_today_slots": state.get("pv_forecast_today_slots"),
        "pv_forecast_tomorrow_slots": state.get("pv_forecast_tomorrow_slots"),
        "pv_forecast_day_after_slots": state.get("pv_forecast_day_after_slots"),
        "pv_forecast_today_hours": state.get("pv_forecast_today_hours"),
        "pv_forecast_tomorrow_hours": state.get("pv_forecast_tomorrow_hours"),
        "pv_forecast_day_after_hours": state.get("pv_forecast_day_after_hours"),
        "slot_actual_today": state.get("slot_actual_today"),
        "hour_actual_today": state.get("hour_actual_today"),
        "hour_slot_factors": state.get("hour_slot_factors"),
        "pv_forecast_detail": detail,
        "performance_ratio": pr,
        "pv_layout": config.get("pv_layout"),
        "pv_kwp_east": config.get("pv_kwp_east"),
        "pv_kwp_west": config.get("pv_kwp_west"),
        "pv_declination": config.get("pv_declination"),
        "pv_dc_efficiency": config.get("pv_dc_efficiency"),
        "perfect_day_kwh": round(ref_kwh, 1) if pv_kwp > 0 else None,
        "perfect_weather_sun_hours": sun_h,
        "reference_anchor_active": anchor_on,
        "reference_anchor_hours_remaining": round(reference_anchor_hours_remaining(state), 1),
        "reference_anchor_kwh": state.get("reference_anchor_kwh") if anchor_on else None,
        "yield_factor": pr,
        "accuracy_score": state.get("accuracy_score"),
        "eta_days_to_90": state.get("eta_days_to_90"),
        "eta_message": state.get("eta_message") or eta_message(state),
        "last_calibration_adjustments": state.get("last_calibration_adjustments"),
        "planned_actions": state.get("planned_actions", []),
        "device_control_plans": device_plans,
        "consumption_forecast": consumption_forecast,
        "consumption_estimates": consumption_estimates,
        "energy_budget_snapshot": energy_budget,
        "energy_budget_snapshots": energy_budget_snapshots,
        "energy_week_plan": energy_week_plan,
        "absorption_plan_24h": state.get("absorption_plan_24h"),
        "device_schedule_24h": state.get("device_schedule_24h"),
        "device_schedule_24h_updated_at": state.get("device_schedule_24h_updated_at"),
        "budget_shed_log": state.get("budget_shed_log") or [],
        "priority_runtime_plan": state.get("priority_runtime_plan"),
        "soc_priority_neuron": state.get("soc_priority_neuron"),
        "soc_priority_neuron_rollout": state.get("soc_priority_neuron_rollout"),
        "steering_stats": build_steering_stats_payload(str(BACKEND_DIR)),
        "steering_learning": _steering_learning_payload(state),
        "info_messages": state.get("info_messages", []),
        "daemon_status": state.get("daemon_status"),
        "last_tick": state.get("last_tick"),
        "tick_interval_seconds": int(
            os.environ.get("CONTROL_TICK_INTERVAL_SECONDS", "120")
        ),
        "last_weather_fetch": state.get("last_weather_fetch"),
        "weather_refresh_note": state.get("weather_refresh_note"),
        "devices": devices,
        "device_power_measure": _device_power_measure_payload(state),
        "conflicts": [d for d in devices if d.get("enabled") and d.get("auto_conflict")],
        "min_soc_percent": config.get("min_soc_percent"),
        "max_soc_percent": config.get("max_soc_percent"),
        "max_soc_release_percent": _status_release_percent(config),
        "evening_aim": _evening_aim_payload(config, state),
        "morning_soc_percent": config.get("morning_soc_percent"),
        "evening_soc_percent": config.get("evening_soc_percent"),
        "risk_factor_percent": config.get("risk_factor_percent"),
        "consumption_risk_percent": config.get("consumption_risk_percent"),
        "consumption_history_days": config.get("consumption_history_days"),
        "anomaly_threshold_percent": config.get("anomaly_threshold_percent"),
        "anomaly_window_hours": config.get("anomaly_window_hours"),
        "min_device_runtime_hours": config.get("min_device_runtime_hours"),
        "min_soc_lock_active": state.get("min_soc_lock_active"),
        "min_soc_lock_reason": state.get("min_soc_lock_reason"),
        "max_soc_protection_active": state.get("max_soc_protection_active"),
        "max_soc_protection_reason": state.get("max_soc_protection_reason"),
        "night_lock_active": state.get("night_lock_active"),
        "night_lock_reason": state.get("night_lock_reason"),
        "night_lock_enabled": night_lock_enabled(config),
        "anomaly_active": state.get("anomaly_active"),
        "anomaly_excess_pct": state.get("anomaly_excess_pct"),
        "anomaly_low_active": state.get("anomaly_low_active"),
        "anomaly_deficit_pct": state.get("anomaly_deficit_pct"),
        "anomaly_low_enabled": config.get("anomaly_low_enabled", True),
        "anomaly_low_threshold_percent": config.get("anomaly_low_threshold_percent", 20),
        "consumption_pattern_estimate": state.get("consumption_pattern_estimate"),
        "consumption_pattern_override": state.get("consumption_pattern_override"),
        "consumption_pattern_enabled": config.get("consumption_pattern_enabled", True),
        "consumption_pattern_engage_pct": config.get("consumption_pattern_engage_pct", 15),
        "pv_safety_active": state.get("pv_safety_active"),
        "pv_safety_deviation_pct": state.get("pv_safety_deviation_pct"),
        "pv_safety_next_recheck_at": state.get("pv_safety_next_recheck_at"),
        "pv_safety_reason": state.get("pv_safety_reason"),
        "pv_safety_signals": state.get("pv_safety_signals"),
        "pv_safety_enabled": config.get("pv_safety_enabled", True),
        "pv_safety_threshold_percent": config.get("pv_safety_threshold_percent", 20),
        "projected_soc_at_target": state.get("projected_soc_at_target"),
        "balance_projection": state.get("balance_projection"),
        "horizon_projection": state.get("horizon_projection"),
        "late_start_plan": state.get("late_start_plan"),
        "autopilot_soc_plan": _autopilot_soc_plan_payload(config, state, live),
        "decision_board": _decision_board_payload(config, state, live),
        "curve_plan": state.get("curve_plan"),
        "fill_schedule": state.get("fill_schedule"),
        "weekday_profile_samples": profile_sample_count(str(BACKEND_DIR)),
        "consumption_history_days_used": len(
            load_history(str(BACKEND_DIR)).get("days") or []
        ),
    }
