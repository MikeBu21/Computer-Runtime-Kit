# -*- coding: utf-8 -*-
"""Batterie-Schutz: harte Min/Max-SoC-Grenzen mit Algo-Sperre."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from control_algorithm.planner import enabled_devices, sort_for_resume

# Im Halt-Band an bleiben, solange die PV mindestens diesen Anteil der
# laufenden Miner-Leistung liefert.
HOLD_PV_KEEP_ON_FACTOR = 0.5
# Darunter ist „die Hälfte“ keine brauchbare Schwelle (Sensorrauschen).
HOLD_PV_MIN_MINER_KW = 1.0
# Max-SoC: Miner-Summe darf höchstens diesen Anteil der aktuellen PV ziehen.
MAX_SOC_PV_BUDGET_FRAC = 0.8
# Batterie lädt (Überschuss fließt in den Speicher), wenn power > epsilon.
BATTERY_CHARGE_EPS_KW = 0.05


def normalize_min_soc(config: Dict[str, Any]) -> float:
    try:
        return float(config.get("min_soc_percent") or 35.0)
    except (TypeError, ValueError):
        return 35.0


def normalize_max_soc(config: Dict[str, Any]) -> float:
    try:
        return float(config.get("max_soc_percent") or 95.0)
    except (TypeError, ValueError):
        return 95.0


def min_soc_recovery_threshold(config: Dict[str, Any]) -> float:
    """Erholung erst ab Min + 1 %-Pkt (Hysterese gegen Flattern)."""
    return normalize_min_soc(config) + 1.0


def max_soc_under_target_percent(config: Dict[str, Any]) -> float:
    """Wie weit der SoC unter das Tagesziel fallen darf, während PV den Miner hält.

    Standard 5 Prozentpunkte. 0 bis 30.
    """
    raw = config.get("max_soc_under_target_percent")
    try:
        value = 5.0 if raw is None else float(raw)
    except (TypeError, ValueError):
        value = 5.0
    return max(0.0, min(30.0, value))


def daily_target_soc_percent(config: Dict[str, Any], state: Dict[str, Any]) -> float:
    """Angezeigtes Tagesziel. Fehlt es, gilt der Halt-SoC."""
    planned = state.get("planning_target") if isinstance(state, dict) else None
    if isinstance(planned, dict) and planned.get("soc_percent") is not None:
        try:
            return float(planned["soc_percent"])
        except (TypeError, ValueError):
            pass
    return max_soc_release_percent(config)


def max_soc_release_percent(config: Dict[str, Any]) -> float:
    """Halt-SoC (Stufe 1). Strikt zwischen Min- und Max-SoC, Standard 90."""
    max_soc = normalize_max_soc(config)
    min_soc = normalize_min_soc(config)
    raw = config.get("max_soc_release_percent")
    try:
        release = 90.0 if raw is None else float(raw)
    except (TypeError, ValueError):
        release = 90.0
    hi = max_soc - 0.1
    lo = min_soc + 0.1
    if hi <= lo:
        return max(min_soc, min(max_soc, release))
    return max(lo, min(hi, release))


def max_soc_clear_threshold(config: Dict[str, Any]) -> float:
    """Max-Schutz endet am Halt-SoC (Stufe 1), nicht mehr bei Max − 0,5."""
    return max_soc_release_percent(config)


def is_min_soc_protection_active(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    from control_algorithm.load_scheduler import is_min_soc_breached

    return is_min_soc_breached(config, live, state)


def is_max_soc_protection_active(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """True wenn Speicher an/über Max-SoC – Algo darf nur noch einschalten."""
    state = state or {}
    if state.get("max_soc_protection_active"):
        return True
    if state.get("max_soc_override_active"):
        return True
    soc = live.get("soc_percent")
    if soc is None:
        return False
    max_soc = normalize_max_soc(config)
    return float(soc) >= max_soc - 0.05


def refresh_min_soc_protection(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> bool:
    """Aktualisiert min_soc_lock_active + Grund in state."""
    active = is_min_soc_protection_active(config, live, state)
    state["min_soc_lock_active"] = active
    if active:
        min_soc = normalize_min_soc(config)
        soc = live.get("soc_percent")
        state["min_soc_lock_reason"] = (
            f"SoC {float(soc):.0f}% < Min {min_soc:.0f}% (Erholung ab {min_soc_recovery_threshold(config):.0f} %)"
            if soc is not None
            else f"Min-SoC {min_soc:.0f}% verletzt"
        )
    else:
        state["min_soc_lock_reason"] = None
    return active


def sync_max_soc_protection_state(
    state: Dict[str, Any],
    config: Dict[str, Any],
    live: Dict[str, Any],
    *,
    over_max: Optional[bool] = None,
) -> bool:
    """Synchronisiert max_soc_protection_active mit Max-SoC-Latch (Hysterese)."""
    from control_algorithm.fill_schedule import apply_max_soc_override_latch

    soc = live.get("soc_percent")
    soc_f = float(soc) if soc is not None else None
    apply_max_soc_override_latch(
        state,
        config,
        soc=soc_f,
        over_max=bool(over_max) if over_max is not None else False,
    )
    active = bool(state.get("max_soc_override_active"))
    state["max_soc_protection_active"] = active
    if active:
        max_soc = normalize_max_soc(config)
        state["max_soc_protection_reason"] = (
            f"SoC {soc_f:.0f}% ≥ Max {max_soc:.0f}% – gestaffelt einschalten "
            f"(höchste Prio zuerst, dann PV×{MAX_SOC_PV_BUDGET_FRAC:.0%} bei Ladeüberschuss), "
            f"kein Abschalten (Freigabe unter Halt {max_soc_clear_threshold(config):.0f} %)"
            if soc_f is not None
            else f"Max-SoC {max_soc:.0f}% – Last gestaffelt aufnehmen"
        )
    else:
        state["max_soc_protection_reason"] = None
    return active


def preview_max_soc_protection(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> bool:
    """Vorschau vor Füll-Rechnung (Service-Tick): Latch oder SoC nah/über Max."""
    if state.get("max_soc_override_active") or state.get("max_soc_protection_active"):
        return True
    soc = live.get("soc_percent")
    if soc is None:
        return False
    return float(soc) >= normalize_max_soc(config) - 0.05


def min_soc_protection_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Unter Min-SoC: alle laufenden Geräte sofort aus."""
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    if not is_min_soc_protection_active(config, live, state):
        return actions, messages

    min_soc = normalize_min_soc(config)
    soc = live.get("soc_percent")
    devices = enabled_devices(config)
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    for d in devices:
        ip = d.get("ip")
        if not ip or miner_status.get(ip) != "running":
            continue
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": (
                    f"Batterie-Schutz Min-SoC ({min_soc:.0f}%): "
                    f"aktuell {float(soc):.0f}% – Notabschaltung"
                    if soc is not None
                    else f"Batterie-Schutz Min-SoC ({min_soc:.0f}%) – Notabschaltung"
                ),
                "force": True,
                "emergency": True,
                "source": "min_soc",
            }
        )
    if actions:
        messages.append(
            f"Batterie-Schutz Min-SoC: {len(actions)} Gerät(e) werden abgeschaltet "
            f"(SOC {float(soc):.0f}% < {min_soc:.0f}%)."
            if soc is not None
            else f"Batterie-Schutz Min-SoC: {len(actions)} Gerät(e) werden abgeschaltet."
        )
    else:
        messages.append(
            state.get("min_soc_lock_reason")
            if state and state.get("min_soc_lock_reason")
            else (
                f"Batterie-Schutz Min-SoC aktiv (SOC {float(soc):.0f}% < {min_soc:.0f}%) "
                "– keine Steuerung / kein Resume bis Erholung."
                if soc is not None
                else f"Batterie-Schutz Min-SoC aktiv – keine Steuerung bis Erholung."
            )
        )
    return actions, messages


def device_load_kw(device: Dict[str, Any]) -> float:
    """Leistungsaufnahme eines Geräts: Messung bevorzugt, sonst Config."""
    for key in ("measured_power_kw", "power_kw"):
        raw = device.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _live_pv_kw(live: Dict[str, Any]) -> float:
    try:
        return max(0.0, float(live.get("pv_kw") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _live_battery_charge_kw(live: Dict[str, Any]) -> float:
    """Positive Werte = Laden. Akzeptiert battery_power_kw oder battery_kw."""
    raw = live.get("battery_power_kw")
    if raw is None:
        raw = live.get("battery_kw")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def select_max_soc_resume_devices(
    config: Dict[str, Any],
    live: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """Welche pausierten Geräte bei Max-SoC jetzt eingeschaltet werden.

    - Läuft noch keiner: nur höchste Priorität (Startbedingung 95 %).
    - Läuft schon mindestens einer und die Batterie lädt noch: so viele weitere
      nach Priorität, dass laufende + neue Summe ≤ 80 % der aktuellen PV bleibt.
    - Sinkt die PV / lädt die Batterie nicht: nichts abschalten, nichts dazu.
    """
    devices = sort_for_resume(enabled_devices(config))
    if not devices:
        return [], "keine Geräte"
    miner_status: Dict[str, str] = live.get("miner_status") or {}

    def _running(d: Dict[str, Any]) -> bool:
        ip = d.get("ip")
        return bool(ip) and str(miner_status.get(ip) or "").lower() == "running"

    running = [d for d in devices if _running(d)]
    paused = [d for d in devices if d.get("ip") and not _running(d)]
    if not paused:
        return [], "alle laufen bereits"

    if not running:
        top = paused[0]
        return [top], (
            f"erster Start: höchste Priorität {top.get('ip')} "
            f"({device_load_kw(top):.2f} kW)"
        )

    charge_kw = _live_battery_charge_kw(live)
    if charge_kw <= BATTERY_CHARGE_EPS_KW:
        return [], (
            f"kein Ladeüberschuss (Batterie {charge_kw:.2f} kW) – "
            "kein weiteres Hochschalten"
        )

    pv_kw = _live_pv_kw(live)
    budget = MAX_SOC_PV_BUDGET_FRAC * pv_kw
    load = sum(device_load_kw(d) for d in running)
    selected: List[Dict[str, Any]] = []
    for d in paused:
        pwr = device_load_kw(d)
        if pwr <= 0:
            continue
        if load + pwr <= budget + 1e-9:
            selected.append(d)
            load += pwr
    if not selected:
        return [], (
            f"PV-Budget ausgeschöpft ({load:.2f}/{budget:.2f} kW "
            f"= {MAX_SOC_PV_BUDGET_FRAC:.0%} von {pv_kw:.1f} kW PV)"
        )
    return selected, (
        f"Nachschub bei Ladeüberschuss {charge_kw:.1f} kW: "
        f"{len(selected)} Gerät(e), Last danach {load:.2f}/{budget:.2f} kW "
        f"(PV {pv_kw:.1f} kW × {MAX_SOC_PV_BUDGET_FRAC:.0%})"
    )


def max_soc_protection_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Über Max-SoC: gestaffelt einschalten (höchste Prio, dann PV-Budget).

    Harte Regel: gilt auch bei aktiver Nacht-Sperre. Nie abschalten bei PV-Abfall.
    """
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    if not is_max_soc_protection_active(config, live, state):
        return actions, messages

    max_soc = normalize_max_soc(config)
    soc = live.get("soc_percent")
    targets, detail = select_max_soc_resume_devices(config, live)
    reason = (
        f"Batterie-Schutz Max-SoC ({max_soc:.0f}%): "
        f"aktuell {float(soc):.0f}% – {detail}"
        if soc is not None
        else f"Batterie-Schutz Max-SoC ({max_soc:.0f}%) – {detail}"
    )
    for d in targets:
        ip = d.get("ip")
        if not ip:
            continue
        actions.append(
            {
                "ip": ip,
                "action": "resume",
                "reason": reason,
                "force": True,
                "emergency": True,
                "urgent": True,
                "source": "max_soc_protection",
            }
        )
    if actions:
        messages.append(
            f"Batterie-Schutz Max-SoC: {len(actions)} Gerät(e) gestaffelt ein "
            f"(Speicher ≥ Max {max_soc:.0f}% · {detail})."
        )
    else:
        messages.append(
            state.get("max_soc_protection_reason")
            or (
                f"Batterie-Schutz Max-SoC aktiv – {detail}."
                if detail
                else f"Batterie-Schutz Max-SoC aktiv – kein weiteres Einschalten."
            )
        )
    return actions, messages


def _running_miner_kw(config: Dict[str, Any], live: Dict[str, Any]) -> float:
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    total = 0.0
    for device in enabled_devices(config):
        ip = device.get("ip")
        if not ip or miner_status.get(ip) != "running":
            continue
        try:
            total += float(device.get("power_kw") or 0)
        except (TypeError, ValueError):
            continue
    return total


def pv_hold_keeps_miners_on(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[bool, str]:
    """True: Halt-Abschaltung aussetzen, der laufende Miner bleibt an.

    Nur ab dem Tagesziel, und nur bis Tagesziel minus Toleranz.
    PV muss mindestens HOLD_PV_KEEP_ON_FACTOR der laufenden Miner-Leistung decken.
    Unter HOLD_PV_MIN_MINER_KW bleibt die normale Abschaltung.
    """
    try:
        soc = float(live.get("soc_percent"))
    except (TypeError, ValueError):
        return False, ""
    target = daily_target_soc_percent(config, state)
    floor = target - max_soc_under_target_percent(config)
    if soc < floor:
        return False, ""
    miner_kw = _running_miner_kw(config, live)
    if miner_kw < HOLD_PV_MIN_MINER_KW:
        return False, ""
    try:
        pv_kw = float(live.get("pv_kw") or 0)
    except (TypeError, ValueError):
        pv_kw = 0.0
    need = HOLD_PV_KEEP_ON_FACTOR * miner_kw
    if pv_kw + 1e-9 < need:
        return False, ""
    return True, (
        f"Halt-Band: Miner bleibt an – PV {pv_kw:.1f} kW deckt mindestens "
        f"{HOLD_PV_KEEP_ON_FACTOR:.0%} von {miner_kw:.1f} kW, "
        f"SoC {soc:.0f}% liegt über Tagesziel {target:.0f}% minus Toleranz "
        f"(Untergrenze {floor:.0f}%)."
    )


def max_soc_hold_pause_actions(
    config: Dict[str, Any],
    live: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Stufe 1: nach dem Maximum alles aus, sobald der Halt-SoC erreicht ist.

    Ab dem Tagesziel bleibt ein laufender Miner an, solange die PV mindestens
    die Hälfte seiner Leistung deckt und der SoC nicht unter Tagesziel minus
    Toleranz fällt.
    """
    actions: List[Dict[str, Any]] = []
    messages: List[str] = []
    if not state.get("max_soc_hold_released"):
        return actions, messages

    keep, keep_msg = pv_hold_keeps_miners_on(config, live, state)
    if keep:
        messages.append(keep_msg)
        return actions, messages

    release = max_soc_release_percent(config)
    max_soc = normalize_max_soc(config)
    soc = live.get("soc_percent")
    reason = (
        f"Stufe 1 Halt-Band: SoC {float(soc):.0f}% ≤ Halt {release:.0f}% "
        f"(Max {max_soc:.0f}%) – alle aus bis Max"
        if soc is not None
        else (
            f"Stufe 1 Halt-Band: Halt {release:.0f}% "
            f"(Max {max_soc:.0f}%) – alle aus bis Max"
        )
    )
    devices = enabled_devices(config)
    miner_status: Dict[str, str] = live.get("miner_status") or {}
    for d in devices:
        ip = d.get("ip")
        if not ip or miner_status.get(ip) != "running":
            continue
        actions.append(
            {
                "ip": ip,
                "action": "pause",
                "reason": reason,
                "force": True,
                "emergency": True,
                "source": "max_soc_hold",
            }
        )
    if actions:
        messages.append(
            f"Stufe 1 Halt-Band: {len(actions)} Gerät(e) werden abgeschaltet "
            f"(SoC ≤ Halt {release:.0f} %, wieder ein ab Max {max_soc:.0f} %)."
        )
    else:
        messages.append(
            f"Stufe 1: aus bis Max-SoC {max_soc:.0f}% – "
            "keine Füll- oder Budget-Einschaltung."
        )
    return actions, messages


def clear_anomaly_for_max_soc_protection(state: Dict[str, Any], now: Optional[datetime] = None) -> None:
    """Mehrverbrauchs-Anomalie löschen – unter Max-SoC ist Last erwünscht."""
    if not state.get("anomaly_active"):
        return
    now = now or datetime.now()
    try:
        from control_algorithm.anomaly_detection import (
            _clear_anomaly_state,
            _reset_anomaly_tracking,
        )

        _clear_anomaly_state(state, now)
        _reset_anomaly_tracking(state)
    except Exception:
        state["anomaly_active"] = False
        state["anomaly_excess_pct"] = None
