# -*- coding: utf-8 -*-
"""Manueller Steuerungsreset.

Läuft ausschließlich, wenn der Nutzer das Passwort selbst eintippt und
danach selbst bestätigt. Kein Timer, kein Start beim Import, kein stilles
Zurücksetzen im Hintergrund.

Zwei Modi:
- archiv: nur das 365-Tage-Verbrauchsarchiv (consumption_history days)
- sonst_alles: Energiestatistik 24h/30d/Jahr und Grundverbrauchsrechner-Datei

Sicherheitsdaten, Lern-Sidecar, Stundenfaktoren und Lenkungs-Lernen bleiben
unangetastet.
"""
from __future__ import annotations

import hmac
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Festes Passwort. Nicht konfigurierbar. Vier mal die 7.
RESET_PASSWORD = "7777"

MODE_ARCHIV = "archiv"
MODE_SONST = "sonst_alles"
RESET_MODES = (MODE_ARCHIV, MODE_SONST)


def password_matches(given: str) -> bool:
    candidate = str(given or "")
    if len(candidate) != len(RESET_PASSWORD):
        return False
    return hmac.compare_digest(candidate, RESET_PASSWORD)


def authorize_reset(password: str, confirm: str) -> Optional[str]:
    """None = freigegeben. Sonst deutscher Fehlertext. Setzt nichts zurück."""
    if str(confirm or "") != "1":
        return "Bitte den Reset selbst bestätigen."
    if not str(password or "").strip():
        return "Bitte das Passwort eintippen."
    if not password_matches(password):
        return "Falsches Passwort."
    return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _energy_stat_targets(base_dir: str, filename: str) -> List[Path]:
    """SD-Pfad und, falls aktiv, der RAM-Live-Pfad."""
    paths: List[Path] = [Path(base_dir) / "energy_monitor" / filename]
    try:
        from ram_cache import is_enabled, live_energy_path, sd_energy_path

        paths = [sd_energy_path(base_dir, filename)]
        if is_enabled():
            paths.append(live_energy_path(base_dir, filename))
    except Exception:
        pass
    # Eindeutig und in stabiler Reihenfolge
    seen = set()
    out: List[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _clear_energy_stats(base_dir: str) -> List[str]:
    """Leert 24h-, 30d- und Jahresstatistik. Gibt die angefassten Dateinamen zurück."""
    today = date.today()
    from energy_stats_24h import _empty_state as empty_24h
    from energy_stats_30d import _empty_state as empty_30d
    from energy_stats_year import _empty_state as empty_year

    payloads = {
        "energy_stats_24h.json": empty_24h(today.isoformat()),
        "energy_stats_30d.json": empty_30d(today),
        "energy_stats_year.json": empty_year(today),
    }
    cleared: List[str] = []
    for name, payload in payloads.items():
        for path in _energy_stat_targets(base_dir, name):
            _write_json(path, payload)
        cleared.append(name)
    return cleared


def _reset_grundverbrauch(base_dir: str) -> str:
    """Setzt die Grundverbrauchsrechner-Datei auf den leeren Standardzustand."""
    from grundverbrauch_rechner import _empty_state, _save_state, _state_path

    path = _state_path(base_dir)
    _save_state(path, _empty_state())
    return path.name


def _clear_archive(base_dir: str) -> int:
    """Löscht nur die Roh-Archivtage. Wochentagsprofile und Lernen bleiben."""
    from control_algorithm.consumption_history import load_history, save_history

    history = load_history(base_dir)
    history["days"] = []
    save_history(base_dir, history)
    return 0


def apply_manual_reset(
    base_dir: str,
    mode: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Führt den gewählten Reset aus. Nur nach authorize_reset aufrufen.

    Angefasst werden nur Archiv bzw. Energiestatistik + Grundverbrauch.
    Config, Schwellen, PV-Ratio-Grenzen, steering_learning und Stundenfaktoren
    bleiben unberührt.
    """
    if mode not in RESET_MODES:
        raise ValueError("Unbekannter Modus.")

    cleared: List[str] = []
    if mode == MODE_ARCHIV:
        archived_days = _clear_archive(base_dir)
        message = (
            "Das 365-Tage-Verbrauchsarchiv ist gelöscht. "
            "Wochentagsprofile, Energiestatistik und Grundverbrauch bleiben."
        )
    else:
        cleared = _clear_energy_stats(base_dir)
        gvr_name = _reset_grundverbrauch(base_dir)
        cleared.append(gvr_name)
        archived_days = None
        message = (
            "Energiestatistik (24h, 30 Tage, Jahr) und der Grundverbrauchsrechner "
            "sind zurückgesetzt. Zukauf-Soll, Halte-Sperren und Nutzerannahmen sind leer; "
            "Miner-Annahmen stehen wieder auf Standard. Das 365-Tage-Archiv bleibt."
        )

    state["last_manual_reset_at"] = datetime.now().isoformat(timespec="seconds")
    state["last_manual_reset_mode"] = mode

    result: Dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "message": message,
        "cleared": cleared,
    }
    if archived_days is not None:
        result["archived_days"] = archived_days
    return result
