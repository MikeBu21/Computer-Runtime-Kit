# -*- coding: utf-8 -*-
"""
Dauerhafte Lerndaten – überleben Config-Änderungen und State-Wipes.

Gespeichert getrennt von control_algorithm_state.json, damit Fingerprint-Resets
oder teilweise State-Verluste die trainierten Parameter/History nicht löschen.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from control_algorithm.config import BACKEND_DIR

LEARNING_STORE_PATH = BACKEND_DIR / "control_algorithm_learning_store.json"

# Keys im State, die zum Trainingsgut gehören und nie stillschweigend gelöscht werden.
LEARNING_STATE_KEYS = (
    "steering_learning",
    "forecast_pace_learning",
    "yield_history",
    "soc_history",
    "performance_ratio",
    "yield_factor",
    "slot_factors",
    "hour_slot_factors",
    "accuracy_score",
    "days_since_reset",
    "base_load_scale",
    "battery_efficiency",
    "soc_accuracy_score",
    "soc_days_since_reset",
)


def extract_learning_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in LEARNING_STATE_KEYS:
        if key in state and state[key] is not None:
            out[key] = deepcopy(state[key])
    return out


def load_learning_store(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or LEARNING_STORE_PATH
    if not p.is_file():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_learning_store(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Schreibt aktuelles Trainingsgut atomar in die Sidecar-Datei."""
    p = path or LEARNING_STORE_PATH
    payload = extract_learning_payload(state)
    if not payload:
        return
    # Bestehenden Store mergen: nie kürzere History über längere schreiben
    existing = load_learning_store(p)
    merged = dict(existing)
    for key, val in payload.items():
        old = existing.get(key)
        if old is None:
            merged[key] = deepcopy(val)
            continue
        # Listen: längere behalten, sonst neu
        if isinstance(val, list) and isinstance(old, list):
            merged[key] = deepcopy(val if len(val) >= len(old) else old)
            continue
        # Dict mit history (steering / pace): History und Params zusammenführen
        if isinstance(val, dict) and isinstance(old, dict):
            combined = deepcopy(old)
            combined.update(deepcopy(val))
            old_hist = old.get("history") if isinstance(old.get("history"), list) else []
            new_hist = val.get("history") if isinstance(val.get("history"), list) else []
            if len(old_hist) > len(new_hist):
                combined["history"] = deepcopy(old_hist)
            elif new_hist:
                combined["history"] = deepcopy(new_hist)
            # Params: State gewinnt (aktuelles Training), fehlende Keys aus Store
            old_p = old.get("params") if isinstance(old.get("params"), dict) else {}
            new_p = val.get("params") if isinstance(val.get("params"), dict) else {}
            if old_p or new_p:
                params = dict(old_p)
                params.update(new_p)
                combined["params"] = params
            merged[key] = combined
            continue
        merged[key] = deepcopy(val)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        tmp.replace(p)
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except Exception:
            pass


def merge_learning_into_state(state: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Lädt Sidecar und füllt fehlende/geleerte Lerndaten im State nach.
    State-Werte mit mehr History gewinnen; leere State-History wird aus Store geheilt.
    """
    store = load_learning_store(path)
    if not store:
        return state
    for key in LEARNING_STATE_KEYS:
        stored = store.get(key)
        if stored is None:
            continue
        current = state.get(key)
        if current is None:
            state[key] = deepcopy(stored)
            continue
        if isinstance(stored, list) and isinstance(current, list):
            if len(current) == 0 and len(stored) > 0:
                state[key] = deepcopy(stored)
            elif len(stored) > len(current):
                # Store hat mehr Tage – anhängen/ersetzen mit längerer Historie
                state[key] = deepcopy(stored)
            continue
        if isinstance(stored, dict) and isinstance(current, dict):
            store_hist = stored.get("history") if isinstance(stored.get("history"), list) else []
            cur_hist = current.get("history") if isinstance(current.get("history"), list) else []
            if len(cur_hist) == 0 and len(store_hist) > 0:
                restored = deepcopy(current)
                restored["history"] = deepcopy(store_hist)
                # Params: State behalten wenn vorhanden, sonst Store
                if not restored.get("params") and stored.get("params"):
                    restored["params"] = deepcopy(stored["params"])
                elif isinstance(restored.get("params"), dict) and isinstance(
                    stored.get("params"), dict
                ):
                    # Fehlende Param-Keys aus Store nachziehen, State überschreibt
                    merged_p = dict(stored["params"])
                    merged_p.update(restored["params"])
                    restored["params"] = merged_p
                if restored.get("last_run") is None and stored.get("last_run"):
                    restored["last_run"] = stored.get("last_run")
                if not restored.get("last_adjustments") and stored.get("last_adjustments"):
                    restored["last_adjustments"] = deepcopy(stored["last_adjustments"])
                state[key] = restored
            elif len(store_hist) > len(cur_hist):
                restored = deepcopy(current)
                restored["history"] = deepcopy(store_hist)
                state[key] = restored
            continue
        # Skalare: wenn State Default-Reset-verdächtig (z. B. days_since_reset 0 und Store >0)
        if key in ("days_since_reset", "soc_days_since_reset", "accuracy_score", "soc_accuracy_score"):
            try:
                if float(current or 0) == 0 and float(stored or 0) > 0:
                    state[key] = deepcopy(stored)
            except (TypeError, ValueError):
                pass
    return state
