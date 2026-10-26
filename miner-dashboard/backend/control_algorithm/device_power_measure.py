# -*- coding: utf-8 -*-
"""
Ist-Leistungsverbrauch je Gerät unabhängig aus Energiedaten ermitteln.

Keine Abfrage an Miner/Geräte-APIs — funktioniert für Miner, Heizkörper,
Steckdosen und alle anderen Algo-Geräte gleichermaßen.

Methode: Bei Einzel-Umschaltung (genau ein Gerät an/aus) wird die Änderung
der Hauslast (Wohnhaus / Gesamtverbrauch) als Geräte-Leistung gelernt.
Werte werden geglättet (EMA). Weicht der Ist-Wert spürbar von ``power_kw`` ab,
wird die Config automatisch nachgezogen.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MIN_LIVE_KW = 0.15
MAX_LIVE_KW = 25.0
EMA_ALPHA = 0.45
CHANGE_REL = 0.08  # 8 %
CHANGE_ABS_KW = 0.15
# Nach Umschaltung: Hauslast erst nach N stabilen Ticks auswerten
DELTA_SETTLE_TICKS = 2
# Max. Schwankung der Hauslast zwischen Settle-Ticks (sonst verwerfen)
SETTLE_STABLE_KW = 0.35


def _f(raw: Any, default: float = 0.0) -> float:
    try:
        if raw is None or raw == "" or raw == "n/a":
            return default
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _ema(old: Optional[float], new: float, alpha: float = EMA_ALPHA) -> float:
    if old is None:
        return round(new, 3)
    return round(old * (1.0 - alpha) + new * alpha, 3)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_device_power_state(state: Dict[str, Any]) -> Dict[str, Any]:
    block = state.get("device_power_measure")
    if not isinstance(block, dict):
        block = {
            "by_ip": {},
            "prev_status": {},
            "prev_house_kw": None,
            "pending_delta": None,
            "last_run": None,
            "last_updates": [],
        }
        state["device_power_measure"] = block
    block.setdefault("by_ip", {})
    block.setdefault("prev_status", {})
    block.setdefault("prev_house_kw", None)
    block.setdefault("pending_delta", None)
    block.setdefault("last_updates", [])
    return block


def _house_load_kw(live: Dict[str, Any]) -> Optional[float]:
    """Haus-/Gesamtlast in kW — unabhängig vom Gerätetyp."""
    for key in ("wohnhaus_kw", "house_kw", "verbrauch_kw", "aktueller_verbrauch_kw"):
        raw = live.get(key)
        if raw is None or raw == "" or raw == "n/a":
            continue
        try:
            return max(0.0, float(str(raw).replace(",", ".")))
        except (TypeError, ValueError):
            continue
    return None


def _status_on(status: Optional[str]) -> bool:
    s = str(status or "").strip().lower()
    return s in ("running", "on", "home", "open")


def _significant_change(old: float, new: float) -> bool:
    if old <= 0.05:
        return abs(new - old) >= CHANGE_ABS_KW
    return abs(new - old) >= max(CHANGE_ABS_KW, old * CHANGE_REL)


def _apply_measurement(
    by_ip: Dict[str, Any],
    ip: str,
    measured_kw: float,
    source: str,
) -> Tuple[float, bool]:
    entry = by_ip.get(ip) if isinstance(by_ip.get(ip), dict) else {}
    prev = entry.get("measured_kw")
    try:
        prev_f = float(prev) if prev is not None else None
    except (TypeError, ValueError):
        prev_f = None
    ema = _ema(prev_f, measured_kw)
    samples = int(entry.get("samples") or 0) + 1
    by_ip[ip] = {
        "measured_kw": ema,
        "last_raw_kw": round(measured_kw, 3),
        "source": source,
        "samples": samples,
        "updated_at": _now_iso(),
        "live": True,
    }
    # Erste Messung reicht zum Anzeigen; Config erst ab 2 Samples robuster
    stable = samples >= 2 or prev_f is None
    return ema, True if samples >= 1 else stable


def _device_keys(config: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for d in config.get("devices") or []:
        ip = str(d.get("ip") or d.get("entity_id") or "").strip()
        if ip:
            keys.append(ip)
    return keys


def _process_house_delta(
    block: Dict[str, Any],
    live: Dict[str, Any],
    device_ips: List[str],
) -> Optional[Tuple[str, float]]:
    """
    Genau ein Gerät umgeschaltet → nach Settle-Ticks Hauslast-Delta = Geräte-kW.
    """
    status_map = live.get("miner_status") or {}
    house = _house_load_kw(live)
    prev_status = block.get("prev_status") or {}
    prev_house = block.get("prev_house_kw")
    pending = block.get("pending_delta")

    changed: List[Tuple[str, bool, bool]] = []
    for ip in device_ips:
        was_on = _status_on(prev_status.get(ip))
        is_on = _status_on(status_map.get(ip))
        if was_on != is_on:
            changed.append((ip, was_on, is_on))

    if len(changed) == 1 and house is not None and prev_house is not None:
        ip, was_on, is_on = changed[0]
        block["pending_delta"] = {
            "ip": ip,
            "was_on": was_on,
            "is_on": is_on,
            "house_before": float(prev_house),
            "house_after_samples": [],
            "ticks": 0,
        }
        pending = block["pending_delta"]
    elif len(changed) > 1:
        # Mehrere Geräte gleichzeitig → Messung verwerfen (nicht zuordenbar)
        block["pending_delta"] = None
        pending = None

    result: Optional[Tuple[str, float]] = None
    if isinstance(pending, dict) and house is not None:
        samples = list(pending.get("house_after_samples") or [])
        samples.append(float(house))
        pending["house_after_samples"] = samples[-DELTA_SETTLE_TICKS:]
        pending["ticks"] = int(pending.get("ticks") or 0) + 1

        if pending["ticks"] >= DELTA_SETTLE_TICKS and len(pending["house_after_samples"]) >= DELTA_SETTLE_TICKS:
            after_vals = [float(x) for x in pending["house_after_samples"]]
            spread = max(after_vals) - min(after_vals)
            if spread <= SETTLE_STABLE_KW:
                before = _f(pending.get("house_before"))
                after = sum(after_vals) / len(after_vals)
                delta = after - before
                ip = str(pending.get("ip") or "")
                turned_on = bool(pending.get("is_on"))
                measured = delta if turned_on else -delta
                block["pending_delta"] = None
                if ip and MIN_LIVE_KW <= measured <= MAX_LIVE_KW:
                    result = (ip, round(measured, 3))
            else:
                # Noch instabil — einen Tick weiter warten, aber nicht endlos
                if pending["ticks"] >= DELTA_SETTLE_TICKS + 3:
                    block["pending_delta"] = None

    block["prev_status"] = {
        ip: ("running" if _status_on(status_map.get(ip)) else "paused")
        for ip in device_ips
    }
    if house is not None:
        block["prev_house_kw"] = house
    return result


def update_device_power_measurements(
    config: Dict[str, Any],
    state: Dict[str, Any],
    live: Dict[str, Any],
    *,
    auto_apply_config: bool = True,
) -> Dict[str, Any]:
    """
    Lernt Ist-Leistung aus Energiedaten, schreibt State, passt ``power_kw`` an.
    """
    from control_algorithm.config import save_config

    block = ensure_device_power_state(state)
    by_ip: Dict[str, Any] = dict(block.get("by_ip") or {})
    devices = list(config.get("devices") or [])
    device_ips = _device_keys(config)
    updates: List[str] = []
    config_changed = False

    if not device_ips:
        block["last_run"] = _now_iso()
        state["device_power_measure"] = block
        return block

    # Live-Flag: Gerät gerade an?
    status_map = live.get("miner_status") or {}
    for ip in device_ips:
        entry = by_ip.get(ip) if isinstance(by_ip.get(ip), dict) else None
        if not entry:
            continue
        entry = dict(entry)
        entry["live"] = _status_on(status_map.get(ip))
        by_ip[ip] = entry

    delta = _process_house_delta(block, live, device_ips)
    if delta:
        ip, measured = delta
        ema, _stable = _apply_measurement(by_ip, ip, measured, "energy_delta")
        for d in devices:
            key = str(d.get("ip") or d.get("entity_id") or "").strip()
            if key != ip:
                continue
            configured = _f(d.get("power_kw"))
            d["measured_power_kw"] = round(ema, 2)
            d["measured_power_updated_at"] = _now_iso()
            if auto_apply_config and _significant_change(configured, ema):
                d["power_kw"] = round(ema, 2)
                config_changed = True
                updates.append(
                    f"{ip}: {configured:.2f} → {ema:.2f} kW (Energiedaten)"
                )
            break

    # Gemessene Werte in Geräte spiegeln (Anzeige)
    for d in devices:
        ip = str(d.get("ip") or d.get("entity_id") or "").strip()
        entry = by_ip.get(ip) if isinstance(by_ip.get(ip), dict) else None
        if not entry or entry.get("measured_kw") is None:
            continue
        d["measured_power_kw"] = round(float(entry["measured_kw"]), 2)
        if entry.get("updated_at"):
            d["measured_power_updated_at"] = entry.get("updated_at")

    config["devices"] = devices
    if config_changed:
        # Frische Config laden und nur Leistungsfelder mergen – sonst überschreibt
        # ein laufender Tick einen gerade gespeicherten Testmodus (dry_run) wieder.
        try:
            from control_algorithm.config import load_config

            fresh = load_config()
            fresh_by_key = {
                str(d.get("ip") or d.get("entity_id") or "").strip(): d
                for d in (fresh.get("devices") or [])
                if d.get("ip") or d.get("entity_id")
            }
            for d in devices:
                key = str(d.get("ip") or d.get("entity_id") or "").strip()
                target = fresh_by_key.get(key)
                if not target:
                    continue
                if d.get("power_kw") is not None:
                    target["power_kw"] = d.get("power_kw")
                if d.get("measured_power_kw") is not None:
                    target["measured_power_kw"] = d.get("measured_power_kw")
                if d.get("measured_power_updated_at"):
                    target["measured_power_updated_at"] = d.get(
                        "measured_power_updated_at"
                    )
            save_config(fresh)
            # Modus-Flags von Disk übernehmen; Geräte-Liste bleibt die bereits
            # aktualisierte In-Memory-Version (nicht mit fremder Disk-Liste ersetzen).
            config["enabled"] = fresh.get("enabled")
            config["dry_run"] = fresh.get("dry_run")
        except Exception as e:
            logger.warning("device power config save failed: %s", e)
            updates.append(f"Config-Speichern fehlgeschlagen: {e}")

    block["by_ip"] = by_ip
    block["last_run"] = _now_iso()
    if updates:
        block["last_updates"] = (updates + list(block.get("last_updates") or []))[:20]
    state["device_power_measure"] = block
    return block


def measured_power_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    block = ensure_device_power_state(state)
    by_ip = block.get("by_ip") or {}
    out: Dict[str, Any] = {}
    for ip, entry in by_ip.items():
        if not isinstance(entry, dict):
            continue
        out[str(ip)] = {
            "measured_kw": entry.get("measured_kw"),
            "last_raw_kw": entry.get("last_raw_kw"),
            "source": entry.get("source"),
            "live": bool(entry.get("live")),
            "samples": int(entry.get("samples") or 0),
            "updated_at": entry.get("updated_at"),
        }
    return {
        "by_ip": out,
        "last_run": block.get("last_run"),
        "last_updates": block.get("last_updates") or [],
        "pending": bool(block.get("pending_delta")),
    }
