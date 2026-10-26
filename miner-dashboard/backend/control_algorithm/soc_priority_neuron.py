# -*- coding: utf-8 -*-
"""
Lehrneuron SoC-Priorität: maximal zulässige Last-Mischung nach Priorität,
solange alle SoC-Checkpoints mit konfigurierbarer Marge erreichbar bleiben.

Kein Neural Net – gierige Geräteauswahl + simulate_battery_horizon + EMA-Feintuning.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_SHADOW_DAYS = 14

from control_algorithm.planner import enabled_devices, sort_for_resume
from control_algorithm.priority_runtime import _device_role
from control_algorithm.steering_learning import (
    _clamp_param,
    _ema,
    ensure_steering_learning,
    get_steering_params,
)

SOC_PRIO_PARAM_KEYS = (
    "soc_prio_margin_pct",
    "soc_prio_anchor_stretch",
    "soc_prio_flex_cut_bias",
    "soc_prio_forecast_trust",
)

SOC_PRIO_TODAY_KEYS = (
    "soc_prio_ticks",
    "soc_prio_miss_ticks",
    "soc_prio_tight_ticks",
    "soc_prio_anchor_cut_ticks",
    "soc_prio_export_peak_ticks",
    "soc_prio_headroom_sum",
)


def is_soc_priority_neuron_enabled(config: Dict[str, Any]) -> bool:
    return bool(config.get("soc_priority_neuron_enabled", True))


def soc_priority_shadow_days(config: Dict[str, Any]) -> int:
    raw = config.get("soc_priority_neuron_shadow_days")
    if raw is None:
        days = DEFAULT_SHADOW_DAYS
    else:
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = DEFAULT_SHADOW_DAYS
    return max(0, min(90, days))


def ensure_soc_priority_rollout(state: Dict[str, Any]) -> Dict[str, Any]:
    rollout = state.get("soc_priority_neuron_rollout")
    if not isinstance(rollout, dict):
        rollout = {
            "shadow_since": datetime.now().isoformat(timespec="seconds"),
            "live_since": None,
            "auto_promoted": False,
        }
        state["soc_priority_neuron_rollout"] = rollout
    rollout.setdefault("shadow_since", datetime.now().isoformat(timespec="seconds"))
    rollout.setdefault("live_since", None)
    rollout.setdefault("auto_promoted", False)
    return rollout


def soc_priority_rollout_status(
    config: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Shadow-Phase läuft automatisch ab (Standard 14 Tage), danach Live ohne Button.
    """
    if not is_soc_priority_neuron_enabled(config):
        return {
            "phase": "disabled",
            "shadow": False,
            "days_elapsed": 0,
            "days_remaining": 0,
            "shadow_days": soc_priority_shadow_days(config),
            "shadow_since": None,
            "live_since": None,
        }

    rollout = ensure_soc_priority_rollout(state)
    shadow_days = soc_priority_shadow_days(config)
    live_since = rollout.get("live_since")
    if live_since:
        return {
            "phase": "live",
            "shadow": False,
            "days_elapsed": shadow_days,
            "days_remaining": 0,
            "shadow_days": shadow_days,
            "shadow_since": rollout.get("shadow_since"),
            "live_since": live_since,
            "auto_promoted": bool(rollout.get("auto_promoted")),
        }

    since_raw = rollout.get("shadow_since")
    try:
        since = datetime.fromisoformat(str(since_raw))
    except (TypeError, ValueError):
        since = datetime.now()
        rollout["shadow_since"] = since.isoformat(timespec="seconds")
        state["soc_priority_neuron_rollout"] = rollout

    elapsed = max(0, (datetime.now() - since).days)
    remaining = max(0, shadow_days - elapsed)
    if elapsed >= shadow_days:
        rollout["live_since"] = datetime.now().isoformat(timespec="seconds")
        rollout["auto_promoted"] = True
        state["soc_priority_neuron_rollout"] = rollout
        return {
            "phase": "live",
            "shadow": False,
            "days_elapsed": elapsed,
            "days_remaining": 0,
            "shadow_days": shadow_days,
            "shadow_since": rollout.get("shadow_since"),
            "live_since": rollout["live_since"],
            "auto_promoted": True,
        }

    return {
        "phase": "shadow",
        "shadow": True,
        "days_elapsed": elapsed,
        "days_remaining": remaining,
        "shadow_days": shadow_days,
        "shadow_since": rollout.get("shadow_since"),
        "live_since": None,
        "auto_promoted": False,
    }


def is_soc_priority_shadow(config: Dict[str, Any], state: Dict[str, Any]) -> bool:
    return bool(soc_priority_rollout_status(config, state).get("shadow"))


def neuron_applies(config: Dict[str, Any], state: Dict[str, Any]) -> bool:
    block = state.get("soc_priority_neuron") or {}
    return (
        is_soc_priority_neuron_enabled(config)
        and not is_soc_priority_shadow(config, state)
        and bool(block.get("ok"))
    )


def soc_prio_margin_pct(state: Dict[str, Any], config: Dict[str, Any]) -> float:
    cfg = float(config.get("soc_priority_neuron_margin_pct") or 0)
    if cfg > 0:
        return cfg
    return float(get_steering_params(state).get("soc_prio_margin_pct") or 5.0)


def soc_prio_flex_cut_bias(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("soc_prio_flex_cut_bias") or 0.0)


def soc_prio_anchor_stretch(state: Dict[str, Any]) -> float:
    return float(get_steering_params(state).get("soc_prio_anchor_stretch") or 0.0)


def _device_power_kw(device: Dict[str, Any], state: Dict[str, Any]) -> float:
    ip = str(device.get("ip") or "")
    dpm = ((state.get("device_power_measure") or {}).get("by_ip") or {}).get(ip) or {}
    measured = dpm.get("measured_power_kw")
    if measured is not None:
        try:
            mv = float(measured)
            if mv > 0:
                return mv
        except (TypeError, ValueError):
            pass
    return max(0.0, float(device.get("power_kw") or 0))


def _planning_horizon_days(config: Dict[str, Any]) -> int:
    try:
        from control_algorithm.energy_balance import planning_horizon_days

        return int(planning_horizon_days(config) or 7)
    except Exception:
        return int(config.get("planning_horizon_days") or 7)


def _checkpoints_ok(
    sim: Dict[str, Any],
    *,
    margin_pct: float,
    min_soc: float,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    for cp in sim.get("checkpoints") or []:
        target = float(cp.get("target_soc") or 0)
        projected = float(cp.get("projected_soc") or 0)
        if projected < target - margin_pct:
            return False, cp
    min_h = sim.get("min_soc_in_horizon")
    if min_h is not None and float(min_h) < min_soc - 0.5:
        return False, {"reason": "min_soc", "projected_soc": min_h, "target_soc": min_soc}
    if not bool(sim.get("max_soc_ok", True)):
        return False, {"reason": "max_soc", "projected_soc": sim.get("max_soc_in_horizon")}
    return True, None


def _simulate_load(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    miner_kw: float,
    now: datetime,
) -> Dict[str, Any]:
    from control_algorithm.energy_balance import simulate_battery_horizon

    storage = float(config.get("storage_kwh") or 0)
    soc = live.get("soc_percent")
    if storage <= 0 or soc is None:
        return {"all_checkpoints_ok": False, "checkpoints": [], "binding_constraint": "no_storage"}
    start_kwh = float(soc) / 100.0 * storage
    return simulate_battery_horizon(
        config,
        state,
        base_dir,
        now,
        start_kwh=start_kwh,
        miner_kw=miner_kw,
        horizon_days=_planning_horizon_days(config),
    )


def compute_soc_priority_plan(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Gierig höchste Priorität zuerst einschalten, solange SoC-Checkpoints
    mit Marge erreichbar bleiben.
    """
    now = now or datetime.now()
    devices = enabled_devices(config)
    margin = soc_prio_margin_pct(state, config)
    min_soc = float(config.get("min_soc_percent") or 35.0)
    flex_cut = soc_prio_flex_cut_bias(state)
    anchor_stretch = soc_prio_anchor_stretch(state)

    rollout = soc_priority_rollout_status(config, state)

    if not devices or float(config.get("storage_kwh") or 0) <= 0:
        return {
            "ok": False,
            "reason": "no_devices_or_storage",
            "recommended_on_ips": [],
            "recommended_continuous_kw": 0.0,
            "margin_pct": margin,
            "shadow": rollout.get("shadow"),
            "rollout": rollout,
        }

    ordered = sort_for_resume(list(devices))
    n = len(ordered)
    on_rows: List[Dict[str, Any]] = []
    current_kw = 0.0
    binding: Optional[Dict[str, Any]] = None
    last_sim: Dict[str, Any] = {}

    # Baseline ohne Miner: wenn schon 0 kW die Checkpoints verfehlt, hilft Lastabbau
    # nichts – kein Force-Pause (verhindert Fehlabschaltung bei divergenter Prognose).
    baseline_sim = _simulate_load(config, state, live, base_dir, 0.0, now)
    baseline_ok, baseline_fail = _checkpoints_ok(
        baseline_sim, margin_pct=margin, min_soc=min_soc
    )
    baseline_unreachable = not baseline_ok

    if baseline_unreachable:
        last_sim = baseline_sim
        binding = baseline_fail
    else:
        last_sim = baseline_sim
        for i, d in enumerate(ordered):
            ip = str(d.get("ip") or "")
            pwr = _device_power_kw(d, state)
            if not ip or pwr <= 0:
                continue
            trial_kw = current_kw + pwr
            sim = _simulate_load(config, state, live, base_dir, trial_kw, now)
            ok, fail_cp = _checkpoints_ok(sim, margin_pct=margin, min_soc=min_soc)
            role = _device_role(i, n)
            if ok:
                current_kw = trial_kw
                last_sim = sim
                on_rows.append(
                    {
                        "ip": ip,
                        "power_kw": round(pwr, 3),
                        "role": role,
                        "priority": d.get("priority"),
                    }
                )
            else:
                if binding is None and fail_cp:
                    binding = fail_cp

        # Flex/Spill früher kappen wenn Bias hoch und knapp
        if flex_cut >= 0.35 and len(on_rows) > 1:
            trimmed: List[Dict[str, Any]] = []
            test_kw = 0.0
            for row in on_rows:
                if row.get("role") in ("flex", "spill") and flex_cut >= 0.55:
                    continue
                test_kw += float(row.get("power_kw") or 0)
                trimmed.append(row)
            if trimmed and len(trimmed) < len(on_rows):
                sim_trim = _simulate_load(config, state, live, base_dir, test_kw, now)
                ok_trim, _ = _checkpoints_ok(sim_trim, margin_pct=margin, min_soc=min_soc)
                if ok_trim:
                    on_rows = trimmed
                    current_kw = test_kw
                    last_sim = sim_trim

        # Anker-Stretch: bei viel Headroom unter nächstem Checkpoint Anker wieder hinzu
        if anchor_stretch > 0.5 and on_rows:
            checkpoints = last_sim.get("checkpoints") or []
            headroom = None
            if checkpoints:
                worst = min(
                    float(cp.get("projected_soc") or 0) - float(cp.get("target_soc") or 0)
                    for cp in checkpoints
                )
                headroom = worst
            if headroom is not None and headroom >= 8.0 + anchor_stretch:
                on_ips_set = {r["ip"] for r in on_rows}
                for i, d in enumerate(ordered):
                    ip = str(d.get("ip") or "")
                    if ip in on_ips_set:
                        continue
                    pwr = _device_power_kw(d, state)
                    if pwr <= 0:
                        continue
                    trial_kw = current_kw + pwr
                    sim = _simulate_load(config, state, live, base_dir, trial_kw, now)
                    ok, _ = _checkpoints_ok(sim, margin_pct=margin, min_soc=min_soc)
                    if ok and _device_role(i, n) == "anchor":
                        current_kw = trial_kw
                        last_sim = sim
                        on_rows.append(
                            {
                                "ip": ip,
                                "power_kw": round(pwr, 3),
                                "role": "anchor",
                                "priority": d.get("priority"),
                                "anchor_stretch": True,
                            }
                        )
                        break

    on_ips = [r["ip"] for r in on_rows]
    headroom_pct = None
    binding_label = None
    if last_sim.get("checkpoints"):
        deltas = [
            float(cp.get("projected_soc") or 0) - float(cp.get("target_soc") or 0)
            for cp in last_sim["checkpoints"]
        ]
        if deltas:
            headroom_pct = round(min(deltas), 2)
    if binding:
        if binding.get("time"):
            binding_label = f"{binding.get('time')} → {binding.get('target_soc')}%"
        elif binding.get("reason") == "min_soc":
            binding_label = f"Min-SoC ({binding.get('projected_soc')}%)"
        else:
            binding_label = str(binding.get("reason") or "checkpoint")

    miner_status = live.get("miner_status") or {}
    running_ips = {
        str(ip)
        for ip, st in miner_status.items()
        if str(st or "").lower() == "running"
    }
    # Baseline unerreichbar: kein Force-Pause – Abschalten hilft dem Checkpoint nicht
    if baseline_unreachable:
        would_pause: List[str] = []
        would_resume = []
        reason = "baseline_unreachable"
    else:
        would_pause = sorted(running_ips - set(on_ips))
        would_resume = sorted(set(on_ips) - running_ips)
        reason = "computed"

    return {
        "ok": True,
        "reason": reason,
        "recommended_on_ips": on_ips,
        "recommended_off_ips": [
            str(d.get("ip") or "")
            for d in ordered
            if str(d.get("ip") or "") and str(d.get("ip") or "") not in on_ips
        ],
        "recommended_continuous_kw": round(current_kw, 3),
        "devices": on_rows,
        "margin_pct": margin,
        "soc_headroom_pct": headroom_pct,
        "binding_checkpoint": binding_label,
        "binding_detail": binding,
        "baseline_unreachable": baseline_unreachable,
        "all_checkpoints_ok": bool(last_sim.get("all_checkpoints_ok")),
        "checkpoints": last_sim.get("checkpoints") or [],
        "would_pause_ips": would_pause,
        "would_resume_ips": would_resume,
        "computed_at": now.isoformat(timespec="seconds"),
        "shadow": rollout.get("shadow"),
        "rollout": rollout,
    }


def merge_neuron_into_priority_plan(
    priority_plan: Dict[str, Any],
    neuron: Dict[str, Any],
) -> Dict[str, Any]:
    """Begrenzt Laufzeitplan auf neuron-sichere Geräte/kW."""
    if not neuron.get("ok"):
        return priority_plan
    allowed: Set[str] = set(neuron.get("recommended_on_ips") or [])
    if not allowed:
        out = dict(priority_plan)
        out["preferred_continuous_kw"] = 0.0
        out["soc_neuron_limited"] = True
        return out

    out = dict(priority_plan)
    by_ip = dict(out.get("by_ip") or {})
    devices = list(out.get("devices") or [])
    max_kw = float(neuron.get("recommended_continuous_kw") or 0)

    for row in devices:
        ip = str(row.get("ip") or "")
        if ip in allowed:
            continue
        row = dict(row)
        row["prefer_on"] = False
        row["planned_hours"] = 0.0
        row["planned_kwh"] = 0.0
        row["soc_neuron_blocked"] = True
        if ip in by_ip:
            by_ip[ip] = dict(by_ip[ip])
            by_ip[ip]["prefer_on"] = False
            by_ip[ip]["planned_hours"] = 0.0
            by_ip[ip]["soc_neuron_blocked"] = True

    out["devices"] = devices
    out["by_ip"] = by_ip
    pref = float(out.get("preferred_continuous_kw") or 0)
    if pref > max_kw:
        out["preferred_continuous_kw"] = max_kw
    out["prefer_ips"] = [ip for ip in (out.get("prefer_ips") or []) if ip in allowed]
    out["soc_neuron_limited"] = True
    out["soc_neuron_max_kw"] = max_kw
    return out


def device_allowed_by_neuron(
    state: Dict[str, Any],
    config: Dict[str, Any],
    ip: str,
) -> Tuple[bool, str]:
    if not neuron_applies(config, state):
        return True, "shadow_or_disabled"
    neuron = state.get("soc_priority_neuron") or {}
    if neuron.get("baseline_unreachable") or neuron.get("reason") == "baseline_unreachable":
        return True, "baseline_unreachable_no_shed"
    allowed = set(neuron.get("recommended_on_ips") or [])
    if ip in allowed:
        return True, "neuron_allows"
    return False, "soc_prio_neuron: SoC-Checkpoint bindend"


def should_pause_for_neuron(
    state: Dict[str, Any],
    config: Dict[str, Any],
    ip: str,
    *,
    miner_status: Dict[str, Any],
) -> bool:
    if not neuron_applies(config, state):
        return False
    if str(miner_status.get(ip) or "").lower() != "running":
        return False
    neuron = state.get("soc_priority_neuron") or {}
    # Checkpoint auch ohne Miner unerreichbar → Pause bringt nichts
    if neuron.get("baseline_unreachable") or neuron.get("reason") == "baseline_unreachable":
        return False
    # Explizite would_pause-Liste bevorzugen (leer bei Baseline-Guard)
    would_pause = neuron.get("would_pause_ips")
    if isinstance(would_pause, list):
        return str(ip) in {str(x) for x in would_pause}
    allowed, _ = device_allowed_by_neuron(state, config, ip)
    return not allowed


def anchor_hold_allowed_by_neuron(
    config: Dict[str, Any],
    state: Dict[str, Any],
    device: Dict[str, Any],
    devices: List[Dict[str, Any]],
) -> bool:
    """Anker länger halten wenn Neuron das Gerät in der sicheren Mischung behält."""
    if not neuron_applies(config, state):
        return False
    from control_algorithm.steering_learning import is_highest_priority_device

    if not is_highest_priority_device(device, devices):
        return False
    ip = str(device.get("ip") or "")
    allowed, _ = device_allowed_by_neuron(state, config, ip)
    if not allowed:
        return False
    neuron = state.get("soc_priority_neuron") or {}
    headroom = neuron.get("soc_headroom_pct")
    if headroom is not None and float(headroom) >= 3.0:
        return True
    return bool(neuron.get("recommended_on_ips") and ip in neuron["recommended_on_ips"])


def record_soc_priority_tick(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
) -> None:
    if not is_soc_priority_neuron_enabled(config):
        return
    sl = ensure_steering_learning(state)
    today = sl["today"]
    neuron = state.get("soc_priority_neuron") or {}
    if not neuron.get("ok"):
        return

    today["soc_prio_ticks"] = int(today.get("soc_prio_ticks") or 0) + 1
    headroom = neuron.get("soc_headroom_pct")
    if headroom is not None:
        today["soc_prio_headroom_sum"] = float(today.get("soc_prio_headroom_sum") or 0) + float(
            headroom
        )

    soc = live.get("soc_percent")
    if soc is not None:
        from control_algorithm.planner import next_soc_target

        target = next_soc_target(config.get("soc_targets") or [])
        if target:
            tgt_soc = float(target.get("soc_percent") or 0)
            if float(soc) < tgt_soc - float(neuron.get("margin_pct") or 5.0) - 2.0:
                today["soc_prio_miss_ticks"] = int(today.get("soc_prio_miss_ticks") or 0) + 1
            if headroom is not None and float(headroom) < 2.0:
                today["soc_prio_tight_ticks"] = int(today.get("soc_prio_tight_ticks") or 0) + 1

    miner_status = live.get("miner_status") or {}
    running = {
        ip for ip, st in miner_status.items() if str(st or "").lower() == "running"
    }
    recommended = set(neuron.get("recommended_on_ips") or [])
    from control_algorithm.steering_learning import is_highest_priority_device

    devices = enabled_devices(config)
    for d in devices:
        ip = str(d.get("ip") or "")
        if (
            ip in running
            and ip not in recommended
            and is_highest_priority_device(d, devices)
        ):
            today["soc_prio_anchor_cut_ticks"] = int(
                today.get("soc_prio_anchor_cut_ticks") or 0
            ) + 1
            break

    pv_kw = float(live.get("pv_kw") or 0)
    if 9 <= datetime.now().hour < 16 and pv_kw >= 3.0:
        try:
            from control_algorithm.weather_survival import live_grid_export_kw

            export_kw = float(live_grid_export_kw(live) or 0)
        except Exception:
            export_kw = max(0.0, float(live.get("export_kw") or 0))
        if export_kw >= 0.5 and recommended and len(recommended) < len(devices):
            today["soc_prio_export_peak_ticks"] = int(
                today.get("soc_prio_export_peak_ticks") or 0
            ) + 1

    state["steering_learning"] = sl


def run_soc_priority_evening_learning(
    state: Dict[str, Any],
    config: Dict[str, Any],
    analysis: Dict[str, Any],
    params: Dict[str, float],
) -> List[str]:
    """Abend-EMA für SoC-Prioritäts-Neuron."""
    sl = ensure_steering_learning(state)
    today = sl.get("today") or {}
    adjustments: List[str] = []

    miss = int(today.get("soc_prio_miss_ticks") or 0)
    tight = int(today.get("soc_prio_tight_ticks") or 0)
    anchor_cut = int(today.get("soc_prio_anchor_cut_ticks") or 0)
    export_peak = int(today.get("soc_prio_export_peak_ticks") or 0)
    ticks = int(today.get("soc_prio_ticks") or 0)

    if miss >= 3 or tight >= 5:
        params["soc_prio_margin_pct"] = _clamp_param(
            "soc_prio_margin_pct",
            _ema(params.get("soc_prio_margin_pct", 5.0), params.get("soc_prio_margin_pct", 5.0) + 1.0, 0.55),
        )
        params["soc_prio_flex_cut_bias"] = _clamp_param(
            "soc_prio_flex_cut_bias",
            _ema(params.get("soc_prio_flex_cut_bias", 0.0), min(1.0, params.get("soc_prio_flex_cut_bias", 0.0) + 0.2), 0.5),
        )
        adjustments.append(
            f"SoC-Prio-Neuron: Ziel knapp ({miss} Miss / {tight} tight) "
            f"→ Marge {params['soc_prio_margin_pct']:.1f} %, Flex-Cut {params['soc_prio_flex_cut_bias']:.2f}."
        )
    elif anchor_cut >= 2 and miss == 0:
        params["soc_prio_anchor_stretch"] = _clamp_param(
            "soc_prio_anchor_stretch",
            _ema(params.get("soc_prio_anchor_stretch", 0.0), params.get("soc_prio_anchor_stretch", 0.0) + 0.5, 0.45),
        )
        adjustments.append(
            f"SoC-Prio-Neuron: Anker zu früh ab ({anchor_cut}×) bei ok SoC "
            f"→ Stretch {params['soc_prio_anchor_stretch']:.1f} h."
        )
    elif export_peak >= 2 and miss == 0:
        params["soc_prio_flex_cut_bias"] = _clamp_param(
            "soc_prio_flex_cut_bias",
            _ema(params.get("soc_prio_flex_cut_bias", 0.0), max(0.0, params.get("soc_prio_flex_cut_bias", 0.0) - 0.15), 0.45),
        )
        adjustments.append(
            f"SoC-Prio-Neuron: Export bei Peak trotz Reserve ({export_peak}×) "
            f"→ Flex-Cut {params['soc_prio_flex_cut_bias']:.2f}."
        )
    elif ticks >= 5 and miss == 0 and tight <= 1:
        params["soc_prio_margin_pct"] = _clamp_param(
            "soc_prio_margin_pct",
            _ema(params.get("soc_prio_margin_pct", 5.0), max(2.0, params.get("soc_prio_margin_pct", 5.0) - 0.3), 0.35),
        )
        adjustments.append(
            f"SoC-Prio-Neuron: Tag stabil ({ticks} Ticks) → Marge leicht {params['soc_prio_margin_pct']:.1f} %."
        )

    if adjustments:
        sl["last_soc_prio_note"] = adjustments[-1]
    state["steering_learning"] = sl
    return adjustments


def run_soc_priority_neuron(
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    base_dir: str,
) -> Dict[str, Any]:
    """Haupt-Tick: Plan berechnen und in State schreiben."""
    if not is_soc_priority_neuron_enabled(config):
        state["soc_priority_neuron"] = {"ok": False, "reason": "disabled"}
        return state

    rollout_before = soc_priority_rollout_status(config, state)

    try:
        plan = compute_soc_priority_plan(config, state, live, base_dir)
        state["soc_priority_neuron"] = plan
        rollout_after = plan.get("rollout") or soc_priority_rollout_status(config, state)
        if (
            rollout_before.get("phase") == "shadow"
            and rollout_after.get("phase") == "live"
            and rollout_after.get("auto_promoted")
        ):
            note = (
                f"SoC-Prio-Neuron: Shadow-Phase ({rollout_after.get('shadow_days')} Tage) "
                f"beendet → ab sofort Live-Steuerung."
            )
            state.setdefault("soc_priority_neuron_notes", [])
            notes = list(state.get("soc_priority_neuron_notes") or [])
            notes.append(note)
            state["soc_priority_neuron_notes"] = notes[-5:]
            state["soc_priority_neuron_live_promoted_at"] = rollout_after.get("live_since")
        if plan.get("ok") and plan.get("binding_checkpoint"):
            state.setdefault("soc_priority_neuron_notes", [])
            notes = list(state.get("soc_priority_neuron_notes") or [])[-4:]
            note = (
                f"SoC-Prio: {plan.get('recommended_continuous_kw')} kW, "
                f"{len(plan.get('recommended_on_ips') or [])} Geräte, "
                f"bindend {plan.get('binding_checkpoint')}"
            )
            if note not in notes:
                notes.append(note)
            state["soc_priority_neuron_notes"] = notes[-5:]
    except Exception as exc:
        state["soc_priority_neuron"] = {"ok": False, "reason": str(exc)}
        state["soc_priority_neuron_error"] = str(exc)
    return state


def soc_priority_info_messages(state: Dict[str, Any]) -> List[str]:
    neuron = state.get("soc_priority_neuron") or {}
    if not neuron.get("ok"):
        return []
    rollout = neuron.get("rollout") or {}
    phase_note = "Live"
    if rollout.get("phase") == "shadow":
        remaining = int(rollout.get("days_remaining") or 0)
        phase_note = f"Shadow (noch {remaining} Tage bis Live)"
    elif rollout.get("auto_promoted"):
        phase_note = "Live (auto nach Shadow-Phase)"
    msgs = [
        (
            f"SoC-Prio-Neuron: {neuron.get('recommended_continuous_kw')} kW · "
            f"{len(neuron.get('recommended_on_ips') or [])} ON · "
            f"Headroom {neuron.get('soc_headroom_pct')}% · "
            f"Marge {neuron.get('margin_pct')}% · {phase_note}"
        )
    ]
    if neuron.get("binding_checkpoint"):
        msgs.append(f"SoC-Prio bindend: {neuron.get('binding_checkpoint')}.")
    return msgs
