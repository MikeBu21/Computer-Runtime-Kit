#!/usr/bin/env python3
"""Flexible Automatik-Regeln: kombinierte Quellen, Durchschnitt, Vergleich, Aktion."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from copy import deepcopy
from statistics import mean
from typing import Any, Callable, Dict, List, Optional, Tuple

COMPARE_OPS = (">", "<", ">=", "<=")
ACTIONS = ("pause", "resume")

_RULE_ID_RE = re.compile(r"^rule_[a-z0-9_]+$")


def _rule_runtime_path(backend_dir: str) -> Path:
    try:
        from ram_cache import is_enabled, rule_runtime_path

        if is_enabled():
            return rule_runtime_path(backend_dir)
    except Exception:
        pass
    return Path(backend_dir) / "energy_monitor" / "rule_runtime_status.json"


def new_rule_id() -> str:
    return "rule_" + uuid.uuid4().hex[:8]


def _parse_float(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _normalize_term(term: Any) -> Optional[Dict[str, str]]:
    if not isinstance(term, dict):
        return None
    source = str(term.get("source") or "").strip()
    if not source:
        return None
    sign = str(term.get("sign") or "+").strip()
    if sign not in ("+", "-"):
        sign = "+"
    return {"source": source, "sign": sign}


def new_clause_id() -> str:
    return "clause_" + uuid.uuid4().hex[:8]


HELP_TEXTS = {
    "plus": "Wert zur Summe addieren (z. B. PV + Wasserkraft). Alle +/−-Werte in einer Zeile werden zu einem gemeinsamen Steuerwert.",
    "minus": "Wert von der Summe abziehen (z. B. Netz − PV).",
    "und": "UND-Verknüpfung: Diese Bedingung muss zusätzlich erfüllt sein. Alle UND-Blöcke müssen gleichzeitig zutreffen.",
    "source": "Datenquelle aus Stromfluss / Statistik — derselbe Wertepool wie im Dashboard.",
    "avg": "Gleitender Durchschnitt in Minuten für diese Bedingung. 0 = Live-Momentanwert. Leer = globale Smartmeter-Einstellung.",
    "compare": "Vergleich des (Durchschnitts-)Werts mit dem Schwellwert.",
    "threshold": "Schwellwert — Regel greift wenn der Vergleich wahr ist.",
    "live": "Aktueller Momentanwert der Quelle(n).",
    "avg_val": "Ø Steuerwert — dieser Wert wird für die Entscheidung verwendet (nicht Live).",
    "action": "Pause oder Resume wird ausgelöst wenn alle Bedingungen (UND) erfüllt sind.",
}


def normalize_clause(
    clause: Any,
    *,
    valid_sources: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(clause, dict):
        return None
    cid = str(clause.get("id") or "").strip() or new_clause_id()

    terms: List[Dict[str, str]] = []
    for term in clause.get("terms") or []:
        normalized = _normalize_term(term)
        if normalized:
            if valid_sources and normalized["source"] not in valid_sources:
                continue
            terms.append(normalized)
    if not terms:
        return None

    compare = str(clause.get("compare") or ">=").strip()
    if compare not in COMPARE_OPS:
        compare = ">="

    threshold = _parse_float(clause.get("threshold"))
    if threshold is None:
        return None

    avg_raw = clause.get("avg_minutes")
    avg_minutes: Optional[int]
    if avg_raw is None or avg_raw == "":
        avg_minutes = None
    else:
        try:
            avg_minutes = max(0, int(float(avg_raw)))
        except (TypeError, ValueError):
            avg_minutes = None

    return {
        "id": cid,
        "terms": terms,
        "avg_minutes": avg_minutes,
        "compare": compare,
        "threshold": threshold,
    }


def normalize_rule(rule: Any, *, valid_sources: Optional[set] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(rule, dict):
        return None
    rid = str(rule.get("id") or "").strip() or new_rule_id()
    if not _RULE_ID_RE.match(rid):
        rid = new_rule_id()

    action = str(rule.get("action") or "pause").strip().lower()
    if action not in ACTIONS:
        action = "pause"

    label = str(rule.get("label") or "").strip()
    if not label:
        label = f"{action.capitalize()} Regel"

    clauses_raw = rule.get("clauses")
    clauses: List[Dict[str, Any]] = []
    if isinstance(clauses_raw, list) and clauses_raw:
        for clause in clauses_raw:
            normalized = normalize_clause(clause, valid_sources=valid_sources)
            if normalized:
                clauses.append(normalized)
    else:
        legacy = normalize_clause(
            {
                "id": f"{rid}_main",
                "terms": rule.get("terms") or [],
                "compare": rule.get("compare"),
                "threshold": rule.get("threshold"),
                "avg_minutes": rule.get("avg_minutes"),
            },
            valid_sources=valid_sources,
        )
        if legacy:
            clauses.append(legacy)

    if not clauses:
        return None

    mode = "and" if len(clauses) > 1 else "expr"

    out: Dict[str, Any] = {
        "id": rid,
        "enabled": bool(rule.get("enabled", True)),
        "label": label,
        "clauses": clauses,
        "mode": mode,
        "action": action,
    }
    # Abwärtskompatibilität für alte UI/API-Aufrufer
    primary = clauses[0]
    out["terms"] = primary["terms"]
    out["avg_minutes"] = primary["avg_minutes"]
    out["compare"] = primary["compare"]
    out["threshold"] = primary["threshold"]
    return out


def normalize_rules(
    rules: Any,
    *,
    valid_sources: Optional[set] = None,
) -> List[Dict[str, Any]]:
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for rule in rules:
        normalized = normalize_rule(rule, valid_sources=valid_sources)
        if normalized:
            out.append(normalized)
    return out


def rules_from_legacy_device(device: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Alte Felder (auto, pause_kw, auto_battery, …) in Regeln überführen."""
    rules: List[Dict[str, Any]] = []
    control_source = str(device.get("control_source") or "field.kelag").strip() or "field.kelag"
    battery_source = str(
        device.get("battery_control_source") or "flow.battery_soc"
    ).strip() or "flow.battery_soc"

    if device.get("auto"):
        pause_kw = _parse_float(device.get("pause_kw"))
        resume_kw = _parse_float(device.get("resume_kw"))
        if pause_kw is not None:
            rules.append(
                {
                    "id": "legacy_kw_pause",
                    "enabled": True,
                    "label": "Pause (Verbrauch)",
                    "terms": [{"source": control_source, "sign": "+"}],
                    "avg_minutes": None,
                    "compare": ">=",
                    "threshold": pause_kw,
                    "action": "pause",
                }
            )
        if resume_kw is not None or pause_kw is not None:
            if resume_kw is None and pause_kw is not None:
                resume_kw = pause_kw - 4
            if resume_kw is not None:
                rules.append(
                    {
                        "id": "legacy_kw_resume",
                        "enabled": True,
                        "label": "Resume (Verbrauch)",
                        "terms": [{"source": control_source, "sign": "+"}],
                        "avg_minutes": None,
                        "compare": "<=",
                        "threshold": resume_kw,
                        "action": "resume",
                    }
                )

    if device.get("auto_battery"):
        soc_pause = _parse_float(
            device.get("raise_kw") or device.get("soc_pause") or device.get("battery_pause")
        )
        soc_resume = _parse_float(
            device.get("lower_kw") or device.get("soc_resume") or device.get("battery_resume")
        )
        if soc_pause is not None:
            rules.append(
                {
                    "id": "legacy_soc_pause",
                    "enabled": True,
                    "label": "Beispiel",
                    "terms": [{"source": battery_source, "sign": "+"}],
                    "avg_minutes": 0,
                    "compare": "<=",
                    "threshold": soc_pause,
                    "action": "pause",
                }
            )
        if soc_resume is not None:
            rules.append(
                {
                    "id": "legacy_soc_resume",
                    "enabled": True,
                    "label": "Beispiel",
                    "terms": [{"source": battery_source, "sign": "+"}],
                    "avg_minutes": 0,
                    "compare": ">=",
                    "threshold": soc_resume,
                    "action": "resume",
                }
            )
    return rules


def get_device_rules(
    device: Dict[str, Any],
    *,
    valid_sources: Optional[set] = None,
) -> List[Dict[str, Any]]:
    stored = device.get("automation_rules")
    if isinstance(stored, list):
        normalized = normalize_rules(stored, valid_sources=valid_sources)
        if normalized:
            return normalized
        if not device.get("auto") and not device.get("auto_battery"):
            return []
    return normalize_rules(
        rules_from_legacy_device(device),
        valid_sources=valid_sources,
    )


def device_has_automation(device: Dict[str, Any]) -> bool:
    rules = get_device_rules(device)
    return any(r.get("enabled") for r in rules)


def compare_values(value: float, op: str, threshold: float) -> bool:
    if op == ">":
        return value > threshold
    if op == "<":
        return value < threshold
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    return False


def compute_signal_value(
    terms: List[Dict[str, str]],
    resolver: Callable[[str], Optional[float]],
) -> Optional[float]:
    total = 0.0
    used = 0
    for term in terms:
        val = resolver(term["source"])
        if val is None:
            return None
        used += 1
        if term.get("sign") == "-":
            total -= float(val)
        else:
            total += float(val)
    return total if used else None


def rule_window_samples(avg_minutes: Optional[int], interval_seconds: int) -> int:
    if avg_minutes is None:
        return 0
    if avg_minutes <= 0:
        return 0
    return max(1, int((avg_minutes * 60) / interval_seconds))


def format_rule_limits(rule: Dict[str, Any], effective_avg: Optional[int] = None) -> str:
    clauses = rule.get("clauses") or []
    if len(clauses) > 1:
        parts = []
        for idx, clause in enumerate(clauses):
            prefix = "UND " if idx else ""
            parts.append(prefix + _format_clause_limits(clause, effective_avg))
        return " | ".join(parts)
    if clauses:
        return _format_clause_limits(clauses[0], effective_avg)
    return format_rule_limits_legacy(rule, effective_avg)


def format_rule_limits_legacy(rule: Dict[str, Any], effective_avg: Optional[int] = None) -> str:
    return _format_clause_limits(
        {
            "terms": rule.get("terms") or [],
            "avg_minutes": rule.get("avg_minutes"),
            "compare": rule.get("compare"),
            "threshold": rule.get("threshold"),
            "label": rule.get("label"),
        },
        effective_avg,
    )


def _format_clause_limits(clause: Dict[str, Any], effective_avg: Optional[int] = None) -> str:
    parts = []
    for idx, term in enumerate(clause.get("terms") or []):
        sign = term.get("sign", "+")
        if idx == 0 and sign == "+":
            parts.append(term["source"])
        else:
            parts.append(f"{'+' if sign == '+' else '−'} {term['source']}")
    expr = " ".join(parts)
    avg = clause.get("avg_minutes")
    if avg is None and effective_avg:
        avg_label = f"Ø {effective_avg} min"
    elif avg and avg > 0:
        avg_label = f"Ø {avg} min"
    else:
        avg_label = "live"
    label = clause.get("label") or expr
    return (
        f"{label}: {avg_label} {expr} "
        f"{clause.get('compare')} {clause.get('threshold')}"
    )


def evaluate_compare_label(op: str) -> str:
    return {
        ">": "größer als",
        "<": "kleiner als",
        ">=": "größer/gleich",
        "<=": "kleiner/gleich",
    }.get(op, op)


class RuleSampleStore:
    """Gleitende Fenster pro Gerät+Regel."""

    def __init__(self) -> None:
        self._buffers: Dict[str, List[float]] = {}

    def _key(self, device_id: str, rule_id: str, clause_id: str) -> str:
        return f"{device_id}:{rule_id}:{clause_id}"

    def append_clause(
        self,
        device_id: str,
        rule_id: str,
        clause: Dict[str, Any],
        value: float,
        *,
        interval_seconds: int,
        default_avg_minutes: int,
    ) -> Tuple[Optional[float], bool, int, int]:
        clause_id = str(clause["id"])
        window = rule_window_samples(clause.get("avg_minutes"), interval_seconds)
        if window <= 0:
            if clause.get("avg_minutes") is None and default_avg_minutes > 0:
                window = rule_window_samples(default_avg_minutes, interval_seconds)
            else:
                return value, True, 1, 1

        key = self._key(device_id, rule_id, clause_id)
        buf = self._buffers.setdefault(key, [])
        buf.append(float(value))
        if len(buf) > window:
            self._buffers[key] = buf[-window:]
            buf = self._buffers[key]
        if len(buf) < window:
            return None, False, len(buf), window
        return round(mean(buf), 3), True, len(buf), window

    def append(
        self,
        device_id: str,
        rule: Dict[str, Any],
        value: float,
        *,
        interval_seconds: int,
        default_avg_minutes: int,
    ) -> Tuple[Optional[float], bool, int, int]:
        """Legacy: erste Klausel der Regel."""
        clause = (rule.get("clauses") or [{}])[0]
        clause_id = clause.get("id") or f"{rule['id']}_main"
        return self.append_clause(
            device_id,
            rule["id"],
            {**clause, "id": clause_id},
            value,
            interval_seconds=interval_seconds,
            default_avg_minutes=default_avg_minutes,
        )

    def trim_all(self, max_window: int) -> None:
        for key, buf in list(self._buffers.items()):
            if len(buf) > max_window:
                self._buffers[key] = buf[-max_window:]


def _default_rule_latches() -> Dict[str, bool]:
    return {"armed": True}


def get_rule_latch(latches: Dict[str, Any], device_id: str, rule_id: str) -> Dict[str, bool]:
    device_entry = latches.setdefault(device_id, {})
    rules = device_entry.setdefault("rules", {})
    if rule_id not in rules:
        rules[rule_id] = _default_rule_latches()
    return rules[rule_id]


def load_latches_from_paths(*paths: Path) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            continue
    return merged


def reset_device_rule_latches(latches: Dict[str, Any], device_id: str, rule_ids: List[str]) -> None:
    """Regel-Sperren zurücksetzen (z. B. nach Speichern neuer Regeln)."""
    entry = latches.setdefault(device_id, {})
    entry["rules"] = {rid: {"armed": True} for rid in rule_ids}


def prune_device_rule_latches(latches: Dict[str, Any], device_id: str, rule_ids: List[str]) -> None:
    allowed = set(rule_ids)
    entry = latches.get(device_id)
    if not entry:
        return
    rules = entry.get("rules") or {}
    entry["rules"] = {rid: state for rid, state in rules.items() if rid in allowed}


def save_rule_runtime_status(status: Dict[str, Any], path: Path) -> None:
    try:
        path.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_rule_runtime_status(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"devices": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"devices": {}}
    except Exception:
        return {"devices": {}}


def evaluate_rule(
    device_id: str,
    rule: Dict[str, Any],
    resolver: Callable[[str], Optional[float]],
    sample_store: RuleSampleStore,
    *,
    interval_seconds: int,
    default_avg_minutes: int,
    runtime_clause_states: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[bool, bool, List[Dict[str, Any]], Optional[float]]:
    """
    Returns: (all_conditions_met, all_ready, clause_states, primary_live_instant)
    """
    clauses = rule.get("clauses") or []
    clause_states: List[Dict[str, Any]] = []
    all_ready = True
    all_met = True
    primary_live: Optional[float] = None

    for clause in clauses:
        cid = clause["id"]
        rt = (runtime_clause_states or {}).get(cid, {})
        instant = compute_signal_value(clause["terms"], resolver)
        if primary_live is None and instant is not None:
            primary_live = instant

        state = {
            "id": cid,
            "live_value": round(instant, 3) if instant is not None else None,
            "avg_value": rt.get("avg_value"),
            "avg_ready": rt.get("avg_ready", False),
            "avg_samples": rt.get("avg_samples", 0),
            "avg_window": rt.get("avg_window", 0),
            "condition_met": False,
            "compare": clause.get("compare"),
            "threshold": clause.get("threshold"),
        }

        if instant is None:
            all_ready = False
            all_met = False
            clause_states.append(state)
            continue

        effective_value, ready, sample_count, window = sample_store.append_clause(
            device_id,
            rule["id"],
            clause,
            instant,
            interval_seconds=interval_seconds,
            default_avg_minutes=default_avg_minutes,
        )
        state["avg_samples"] = sample_count
        state["avg_window"] = window
        state["avg_ready"] = ready
        if ready and effective_value is not None:
            state["avg_value"] = effective_value

        if not ready or effective_value is None:
            all_ready = False
            all_met = False
            clause_states.append(state)
            continue

        met = compare_values(
            effective_value, clause["compare"], float(clause["threshold"])
        )
        state["condition_met"] = met
        if not met:
            all_met = False
        clause_states.append(state)

    return all_met, all_ready, clause_states, primary_live


def control_devices_with_rules(
    devices: List[Dict[str, Any]],
    resolver: Callable[[str], Optional[float]],
    source_label_fn: Callable[[str], str],
    sample_store: RuleSampleStore,
    last_actions: Dict[str, Any],
    latches: Dict[str, Any],
    apply_action: Callable[..., None],
    *,
    interval_seconds: int,
    default_avg_minutes: int,
    valid_sources: Optional[set] = None,
    runtime_status: Optional[Dict[str, Any]] = None,
) -> None:
    runtime = runtime_status if runtime_status is not None else {"devices": {}}
    runtime_devices = runtime.setdefault("devices", {})

    for device in devices:
        device_id = device["id"]
        rules = [r for r in get_device_rules(device, valid_sources=valid_sources) if r.get("enabled")]
        if not rules:
            continue

        current_state = last_actions.get(device_id)
        rule_states: List[Dict[str, Any]] = []
        pause_hit = False
        resume_hit = False
        pause_meta = None
        resume_meta = None

        for rule in rules:
            latch = get_rule_latch(latches, device_id, rule["id"])
            action = rule["action"]

            all_met, all_ready, clause_states, primary_live = evaluate_rule(
                device_id,
                rule,
                resolver,
                sample_store,
                interval_seconds=interval_seconds,
                default_avg_minutes=default_avg_minutes,
            )

            primary_clause = (rule.get("clauses") or [{}])[0]
            primary_rt = clause_states[0] if clause_states else {}
            rule_state = {
                "id": rule["id"],
                "live_value": primary_live,
                "avg_value": primary_rt.get("avg_value"),
                "avg_ready": all_ready,
                "avg_samples": primary_rt.get("avg_samples", 0),
                "avg_window": primary_rt.get("avg_window", 0),
                "condition_met": all_met if all_ready else False,
                "would_trigger": False,
                "armed": latch.get("armed", True),
                "clause_states": clause_states,
                "mode": rule.get("mode", "expr"),
            }

            if not all_ready:
                rule_states.append(rule_state)
                continue

            condition = all_met
            rule_state["condition_met"] = condition

            if not condition:
                latch["armed"] = True
                rule_state["armed"] = True
                rule_states.append(rule_state)
                continue

            if action == "resume" and current_state == "pause":
                latch["armed"] = True
            if action == "pause" and current_state != "pause":
                latch["armed"] = True

            rule_state["armed"] = latch.get("armed", True)
            if not latch.get("armed", True):
                rule_states.append(rule_state)
                continue

            limits = format_rule_limits(rule, effective_avg=default_avg_minutes)
            trigger_parts = []
            for clause in rule.get("clauses") or []:
                for term in clause.get("terms") or []:
                    trigger_parts.append(source_label_fn(term["source"]))
            trigger = " UND ".join(dict.fromkeys(trigger_parts)) or rule.get("label", rule["id"])

            values_summary = []
            for cs in clause_states:
                if cs.get("avg_value") is not None:
                    values_summary.append(f"{cs['avg_value']:g}")
            value_str = " & ".join(values_summary) if values_summary else "n/a"

            meta = (rule, action, trigger, value_str, limits)
            if action == "pause" and current_state != "pause":
                pause_hit = True
                pause_meta = meta
                rule_state["would_trigger"] = True
            elif action == "resume" and current_state in (None, "pause"):
                resume_hit = True
                resume_meta = meta
                rule_state["would_trigger"] = True

            rule_states.append(rule_state)

        runtime_devices[device_id] = {
            "last_action": current_state,
            "rules": rule_states,
        }

        chosen = None
        if pause_hit:
            chosen = pause_meta
        elif resume_hit:
            chosen = resume_meta

        if not chosen:
            continue

        rule, action, trigger, value_str, limits = chosen
        get_rule_latch(latches, device_id, rule["id"])["armed"] = False
        apply_action(device, action, last_actions, trigger, value_str, limits)


def build_value_resolver(backend_dir: str):
    """Erzeugt Resolver-Funktion für resolve_source_value (kW und %)."""
    from dashboard_battery import load_battery_data
    from dashboard_config import load_config as load_dash_config
    from dashboard_sources import resolve_source_value
    from ha_entity_mapping import load_config as load_ha_config
    from miner_flow_data import build_flow_response

    dash_cfg = load_dash_config()
    ha_cfg = load_ha_config()
    bat = load_battery_data()
    flow = build_flow_response(
        backend_dir,
        battery_soc_loader=lambda: bat.get("soc"),
        battery_power_kw=bat.get("power_kw"),
    )

    def resolver(source: str) -> Optional[float]:
        return resolve_source_value(
            source,
            flow=flow,
            ha_cfg=ha_cfg,
            dash_cfg=dash_cfg,
            battery_kw=bat.get("power_kw"),
            battery_soc=bat.get("soc"),
        )

    return resolver, dash_cfg


def load_automation_payload(backend_dir: str) -> Dict[str, Any]:
    """Vollständige Automatik-Konfiguration für UI."""
    from device_registry import load_all_controllable
    from dashboard_config import (
        automation_all_source_options,
        load_config as load_dash_config,
        valid_automation_all_sources,
    )

    dash_cfg = load_dash_config()
    valid = valid_automation_all_sources(dash_cfg)
    resolver, _ = build_value_resolver(backend_dir)
    runtime = load_rule_runtime_status(_rule_runtime_path(backend_dir))
    runtime_devices = (runtime.get("devices") or {})

    avg_minutes = 45
    try:
        settings = Path(backend_dir) / "shared_settings" / "avg_minutes.txt"
        if settings.is_file():
            avg_minutes = int(settings.read_text(encoding="utf-8").strip())
    except Exception:
        pass

    devices_out = []
    for device in load_all_controllable():
        rules = get_device_rules(device, valid_sources=valid)
        dev_runtime = runtime_devices.get(device["id"], {})
        runtime_rules = {
            r.get("id"): r for r in (dev_runtime.get("rules") or []) if isinstance(r, dict)
        }
        live_rules = []
        for rule in rules:
            rt = runtime_rules.get(rule["id"], {})
            rt_clauses = {
                c.get("id"): c
                for c in (rt.get("clause_states") or [])
                if isinstance(c, dict) and c.get("id")
            }
            _, _, clause_states, primary_live = evaluate_rule(
                device["id"],
                rule,
                resolver,
                RuleSampleStore(),
                interval_seconds=6,
                default_avg_minutes=avg_minutes,
                runtime_clause_states=rt_clauses,
            )
            if not clause_states:
                for clause in rule.get("clauses") or []:
                    instant = compute_signal_value(clause["terms"], resolver)
                    cs_rt = rt_clauses.get(clause["id"], {})
                    clause_states.append(
                        {
                            "id": clause["id"],
                            "live_value": round(instant, 3) if instant is not None else None,
                            "avg_value": cs_rt.get("avg_value"),
                            "avg_ready": cs_rt.get("avg_ready", False),
                            "avg_samples": cs_rt.get("avg_samples", 0),
                            "avg_window": cs_rt.get("avg_window", 0),
                            "condition_met": cs_rt.get("condition_met", False),
                            "compare": clause.get("compare"),
                            "threshold": clause.get("threshold"),
                        }
                    )
            live_rules.append(
                {
                    **rule,
                    "live_value": primary_live,
                    "avg_value": rt.get("avg_value"),
                    "avg_ready": rt.get("avg_ready", False),
                    "avg_samples": rt.get("avg_samples", 0),
                    "avg_window": rt.get("avg_window", 0),
                    "condition_met": rt.get("condition_met", False),
                    "would_trigger": rt.get("would_trigger", False),
                    "device_last_action": dev_runtime.get("last_action"),
                    "clause_states": clause_states or rt.get("clause_states") or [],
                }
            )
        devices_out.append(
            {
                "id": device["id"],
                "type": device.get("type"),
                "label": device.get("label") or device["id"],
                "subtitle": device.get("model") or device.get("entity_id") or "",
                "rules": live_rules,
            }
        )

    return {
        "devices": devices_out,
        "source_options": automation_all_source_options(dash_cfg),
        "default_avg_minutes": avg_minutes,
        "compare_ops": list(COMPARE_OPS),
        "actions": list(ACTIONS),
        "help_texts": HELP_TEXTS,
    }


def save_automation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Speichert Regeln pro Gerät in miners.json / ha_devices.json."""
    import json

    from device_registry import MINERS_PATH, load_ha_devices_raw, load_miners_raw, save_ha_devices
    from dashboard_config import load_config as load_dash_config, valid_automation_all_sources

    dash_cfg = load_dash_config()
    valid = valid_automation_all_sources(dash_cfg)
    incoming = {
        str(item.get("id")): item
        for item in (payload.get("devices") or [])
        if isinstance(item, dict) and item.get("id")
    }

    latches_path = MINERS_PATH.parent / "energy_monitor" / "control_latches.json"
    latches = load_latches_from_paths(
        latches_path,
        MINERS_PATH.parent / "control_latches.json",
    )

    miners = load_miners_raw()
    for miner in miners:
        ip = miner.get("ip")
        if not ip or ip not in incoming:
            continue
        rules = normalize_rules(incoming[ip].get("rules") or [], valid_sources=valid)
        miner["automation_rules"] = rules
        miner["auto"] = False
        miner["auto_battery"] = False
        reset_device_rule_latches(latches, ip, [r["id"] for r in rules if r.get("enabled")])
        prune_device_rule_latches(latches, ip, [r["id"] for r in rules])

    miners_path = MINERS_PATH
    miners_path.write_text(
        json.dumps(miners, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    ha_devices = load_ha_devices_raw()
    for dev in ha_devices:
        eid = dev.get("entity_id")
        if not eid or eid not in incoming:
            continue
        rules = normalize_rules(incoming[eid].get("rules") or [], valid_sources=valid)
        dev["automation_rules"] = rules
        dev["auto"] = False
        dev["auto_battery"] = False
        reset_device_rule_latches(latches, eid, [r["id"] for r in rules if r.get("enabled")])
        prune_device_rule_latches(latches, eid, [r["id"] for r in rules])
    save_ha_devices(ha_devices)

    try:
        latches_path.write_text(json.dumps(latches, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass

    backend_dir = str(miners_path.parent)
    return load_automation_payload(backend_dir)
